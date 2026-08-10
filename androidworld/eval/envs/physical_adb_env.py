"""Screenshot-only AndroidWorld environment for an existing physical device."""

from __future__ import annotations

import base64
import io
import os
from pathlib import Path
import subprocess
import time
from typing import Mapping

import numpy as np
from PIL import Image
from PIL import UnidentifiedImageError

from android_world.env import interface
from android_world.env import json_action


class PhysicalAdbEnv(interface.AsyncEnv):
    """Minimal AsyncEnv backed by an already-connected ADB device.

    UI-Voyager is screenshot-native, so a gRPC AndroidEnv emulator is not
    required. This adapter intentionally exposes no accessibility tree and
    executes the official JSONAction space directly through ADB.
    """

    _SCREENSHOT_ATTEMPTS = 10

    def __init__(
        self,
        adb_path: str | Path,
        device: str,
        *,
        adb_server_port: int = 5037,
        app_packages: Mapping[str, str] | None = None,
        wait_seconds: float = 2.0,
    ) -> None:
        self._adb_path = str(Path(adb_path).expanduser().resolve())
        self._device = device
        self._adb_server_port = adb_server_port
        self._app_packages = {
            name.casefold(): package for name, package in (app_packages or {}).items()
        }
        self._wait_seconds = wait_seconds
        self._interaction_cache = ""
        self._closed = False

        state = self._run("get-state", text=True).strip()
        if state != "device":
            raise RuntimeError(
                f"ADB device {device!r} is in state {state!r}, expected 'device'."
            )
        screenshot = self._screenshot()
        self._height, self._width = screenshot.shape[:2]

    def _prefix(self) -> list[str]:
        prefix = [self._adb_path]
        if not os.environ.get("ADB_SERVER_SOCKET"):
            prefix.extend(["-P", str(self._adb_server_port)])
        return [*prefix, "-s", self._device]

    def _run(
        self,
        *args: str,
        text: bool = False,
        timeout: float = 120.0,
        check: bool = True,
    ) -> bytes | str:
        result = subprocess.run(
            [*self._prefix(), *args],
            check=check,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=text,
            timeout=timeout,
        )
        return result.stdout

    def _screenshot(self) -> np.ndarray:
        last_error: Exception | None = None
        for attempt in range(self._SCREENSHOT_ATTEMPTS):
            payload = self._run("exec-out", "screencap", "-p")
            assert isinstance(payload, bytes)
            try:
                with Image.open(io.BytesIO(payload)) as image:
                    return np.asarray(image.convert("RGB")).copy()
            except (OSError, UnidentifiedImageError) as exc:
                last_error = exc
                if attempt < self._SCREENSHOT_ATTEMPTS - 1:
                    time.sleep(min(0.5 * (attempt + 1), 2.0))
        raise RuntimeError(
            f"ADB returned an invalid screenshot {self._SCREENSHOT_ATTEMPTS} times."
        ) from last_error

    @property
    def controller(self):
        """Physical mode deliberately has no AndroidEnv controller."""
        return None

    def reset(self, go_home: bool = False) -> interface.State:
        self._interaction_cache = ""
        if go_home:
            self._run("shell", "input", "keyevent", "KEYCODE_HOME")
        return self.get_state(wait_to_stabilize=False)

    def get_state(self, wait_to_stabilize: bool = False) -> interface.State:
        if wait_to_stabilize:
            time.sleep(self._wait_seconds)
        return interface.State(
            pixels=self._screenshot(),
            forest=[],
            ui_elements=[],
            auxiliaries={"device": self._device},
        )

    def _type_text(self, text: str) -> None:
        original_ime = str(
            self._run(
                "shell", "settings", "get", "secure", "default_input_method",
                text=True,
            )
        ).strip()
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        try:
            self._run(
                "shell", "ime", "enable", "com.android.adbkeyboard/.AdbIME"
            )
            self._run("shell", "ime", "set", "com.android.adbkeyboard/.AdbIME")
            # IME activation and broadcast handling are asynchronous on some
            # physical devices. Restoring the original IME immediately can
            # truncate or corrupt CJK input even though `am broadcast` exits 0.
            time.sleep(0.5)
            self._run(
                "shell", "am", "broadcast", "-a", "ADB_INPUT_B64",
                "--es", "msg", encoded,
            )
            time.sleep(0.2)
        finally:
            if original_ime and original_ime != "null":
                self._run("shell", "ime", "set", original_ime, check=False)
            self._run(
                "shell", "ime", "disable", "com.android.adbkeyboard/.AdbIME",
                check=False,
            )

    def execute_action(self, action: json_action.JSONAction) -> None:
        kind = action.action_type
        if kind in (json_action.STATUS, json_action.UNKNOWN):
            return
        if kind == json_action.ANSWER:
            self._interaction_cache = action.text or ""
            return
        if kind == json_action.WAIT:
            time.sleep(self._wait_seconds)
            return
        if kind == json_action.CLICK:
            self._run("shell", "input", "tap", str(action.x), str(action.y))
            return
        if kind == json_action.DOUBLE_TAP:
            for _ in range(2):
                self._run("shell", "input", "tap", str(action.x), str(action.y))
                time.sleep(0.1)
            return
        if kind == json_action.LONG_PRESS:
            self._run(
                "shell", "input", "swipe", str(action.x), str(action.y),
                str(action.x), str(action.y), "800",
            )
            return
        if kind == json_action.SWIPE:
            self._run(
                "shell", "input", "swipe", str(action.x), str(action.y),
                str(action.x_), str(action.y_), "500",
            )
            return
        if kind == json_action.SCROLL:
            start_x, start_y = self._width // 2, self._height // 2
            offsets = {
                "up": (0, -self._height // 3),
                "down": (0, self._height // 3),
                "left": (-self._width // 3, 0),
                "right": (self._width // 3, 0),
            }
            dx, dy = offsets[action.direction or "up"]
            self._run(
                "shell", "input", "swipe", str(start_x), str(start_y),
                str(start_x + dx), str(start_y + dy), "500",
            )
            return
        if kind == json_action.INPUT_TEXT:
            self._type_text(action.text or "")
            return
        keycodes = {
            json_action.NAVIGATE_BACK: "KEYCODE_BACK",
            json_action.NAVIGATE_HOME: "KEYCODE_HOME",
            json_action.KEYBOARD_ENTER: "KEYCODE_ENTER",
        }
        if kind in keycodes:
            self._run("shell", "input", "keyevent", keycodes[kind])
            return
        if kind == json_action.OPEN_APP:
            requested = (action.app_name or "").casefold()
            package = self._app_packages.get(requested, action.app_name or "")
            if not package:
                raise ValueError("open_app action did not provide an app name.")
            self._run(
                "shell", "monkey", "-p", package,
                "-c", "android.intent.category.LAUNCHER", "1",
            )
            return
        raise ValueError(f"Unsupported physical-device action: {kind!r}")

    @property
    def foreground_activity_name(self) -> str:
        output = str(
            self._run("shell", "dumpsys", "window", "windows", text=True)
        )
        for line in output.splitlines():
            if "mCurrentFocus" in line:
                return line.strip()
        return ""

    @property
    def device_screen_size(self) -> tuple[int, int]:
        return self._width, self._height

    @property
    def logical_screen_size(self) -> tuple[int, int]:
        return self.device_screen_size

    def display_message(self, message: str, header: str = "") -> None:
        del message, header

    def ask_question(self, question: str, timeout_seconds: float = -1.0):
        del question, timeout_seconds
        return None

    @property
    def interaction_cache(self) -> str:
        return self._interaction_cache

    @interaction_cache.setter
    def interaction_cache(self, value: str) -> None:
        self._interaction_cache = value

    def hide_automation_ui(self) -> None:
        self._run("shell", "settings", "put", "system", "pointer_location", "0")

    @property
    def orientation(self) -> int:
        return 0

    @property
    def physical_frame_boundary(self) -> tuple[int, int, int, int]:
        return 0, 0, self._width, self._height

    def close(self) -> None:
        self._closed = True
