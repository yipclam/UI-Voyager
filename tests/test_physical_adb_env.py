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


if __name__ == "__main__":
    unittest.main()
