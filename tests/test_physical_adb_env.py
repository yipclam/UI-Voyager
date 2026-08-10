import io
import os
from pathlib import Path
import subprocess
import sys
from unittest import mock
import unittest

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "androidworld"))

from android_world.env import json_action  # noqa: E402
from eval.envs.physical_adb_env import PhysicalAdbEnv  # noqa: E402


def _png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (3, 2), color=(1, 2, 3)).save(output, "PNG")
    return output.getvalue()


def _result(stdout=b""):
    return subprocess.CompletedProcess([], 0, stdout=stdout)


class PhysicalAdbEnvTest(unittest.TestCase):
    @mock.patch.object(subprocess, "run")
    def test_socket_selects_serial_without_overriding_port(self, run):
        run.side_effect = [_result("device\n"), _result(_png()), _result(_png())]
        with mock.patch.dict(
            os.environ, {"ADB_SERVER_SOCKET": "tcp:127.0.0.1:15038"}, clear=False
        ):
            env = PhysicalAdbEnv("/sdk/adb", "phone")
            state = env.get_state()

        self.assertEqual(state.pixels.shape, (2, 3, 3))
        self.assertEqual(run.call_args_list[0].args[0], ["/sdk/adb", "-s", "phone", "get-state"])

    @mock.patch.object(subprocess, "run")
    def test_click_uses_device_pixels(self, run):
        run.side_effect = [_result("device\n"), _result(_png()), _result()]
        env = PhysicalAdbEnv("/sdk/adb", "phone", adb_server_port=15038)
        env.execute_action(json_action.JSONAction(action_type="click", x=2, y=1))

        self.assertEqual(
            run.call_args_list[-1].args[0],
            ["/sdk/adb", "-P", "15038", "-s", "phone", "shell", "input", "tap", "2", "1"],
        )

    @mock.patch("eval.envs.physical_adb_env.time.sleep")
    @mock.patch.object(subprocess, "run")
    def test_screenshot_retries_transient_invalid_payload(self, run, sleep):
        run.side_effect = [
            _result("device\n"),
            _result(_png()),
            _result(b"transient adb tunnel error"),
            _result(_png()),
        ]
        env = PhysicalAdbEnv("/sdk/adb", "phone", adb_server_port=15038)
        state = env.get_state()

        self.assertEqual(state.pixels.shape, (2, 3, 3))
        sleep.assert_called_once_with(0.5)

    @mock.patch("eval.envs.physical_adb_env.time.sleep")
    @mock.patch.object(subprocess, "run")
    def test_utf8_input_waits_for_ime_and_broadcast(self, run, sleep):
        run.side_effect = [
            _result("device\n"),
            _result(_png()),
            _result("original/.Ime\n"),
            _result(),
            _result(),
            _result(),
            _result(),
            _result(),
        ]
        env = PhysicalAdbEnv("/sdk/adb", "phone", adb_server_port=15038)
        env.execute_action(
            json_action.JSONAction(action_type="input_text", text="九重紫")
        )

        self.assertEqual(sleep.call_args_list, [mock.call(0.5), mock.call(0.2)])
        self.assertIn(
            ["shell", "am", "broadcast", "-a", "ADB_INPUT_B64", "--es", "msg", "5Lmd6YeN57Sr"],
            [call.args[0][-8:] for call in run.call_args_list],
        )


if __name__ == "__main__":
    unittest.main()
