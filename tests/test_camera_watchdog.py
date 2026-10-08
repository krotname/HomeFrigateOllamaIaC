"""Exercise real Bash probes and the camera runtime API across power cycles."""

import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest


SCRIPT = Path(os.environ.get(
    "CAMERA_WATCHDOG_TEST_SCRIPT",
    Path(__file__).parents[1] / "ansible/roles/frigate_vm/files/camera-watchdog.sh",
))

DOCKER = r'''#!/usr/bin/env python3
import json, os, pathlib, sys

path = pathlib.Path(os.environ["FAKE_FRIGATE_STATE"])
state = json.loads(path.read_text())
args = sys.argv[1:]
if args[0] == "inspect":
    print("running|" + state.get("health", "healthy"))
elif args[-1].endswith("/api/config"):
    print(json.dumps({"cameras": state["cameras"]}))
elif "/api/camera/" in args[-1] and args[-1].endswith("/set/enabled"):
    name = args[-1].split("/api/camera/")[1].split("/")[0]
    value = json.loads(args[args.index("-d") + 1])["value"]
    assert value in ("ON", "OFF")
    assert state["yaml_enabled"][name], "startup workers were not configured"
    state["commands"].append([name, value])
    if state.get("fail_update"):
        path.write_text(json.dumps(state))
        sys.exit(22)
    state["cameras"][name]["enabled"] = value == "ON"
    path.write_text(json.dumps(state))
    print('{"success":true}')
else:
    sys.exit("Unexpected API call: " + args[-1])
'''


@unittest.skipUnless(os.name == "posix", "watchdog runs on the Linux VM")
class CameraWatchdogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        docker = self.root / "docker"
        docker.write_text(DOCKER)
        docker.chmod(0o700)
        self.state_path = self.root / "frigate.json"
        self.state_path.write_text(json.dumps({
            "cameras": {"fisheye": {"enabled": True}, "driveway": {"enabled": True}},
            "yaml_enabled": {"fisheye": True, "driveway": True},
            "commands": [],
        }))
        # A bound but non-listening socket refuses connections; listen() restores
        # the very same camera address without replacing the watchdog's state.
        self.camera = socket.socket()
        self.addCleanup(self.camera.close)
        self.camera.bind(("127.0.0.1", 0))
        self.env = {
            **os.environ,
            "PATH": str(self.root) + os.pathsep + os.environ["PATH"],
            "FAKE_FRIGATE_STATE": str(self.state_path),
            "CAMERA_WATCHDOG_STATE_DIR": str(self.root / "counters"),
            "CAMERA_WATCHDOG_TARGETS": "fisheye=127.0.0.1",
            "CAMERA_WATCHDOG_RTSP_PORT": str(self.camera.getsockname()[1]),
            "CAMERA_WATCHDOG_OFFLINE_THRESHOLD": "2",
            "CAMERA_WATCHDOG_ONLINE_THRESHOLD": "2",
            "CAMERA_WATCHDOG_PROBE_TIMEOUT_SECONDS": "1",
        }

    def state(self):
        return json.loads(self.state_path.read_text())

    def tick(self, expected=0):
        result = subprocess.run(
            ["bash", str(SCRIPT)], env=self.env, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(expected, result.returncode, result.stdout + result.stderr)

    def test_power_return_restores_camera_without_rewriting_startup_config(self):
        self.tick()
        self.assertEqual([], self.state()["commands"])
        self.tick()
        self.assertFalse(self.state()["cameras"]["fisheye"]["enabled"])
        self.assertTrue(self.state()["yaml_enabled"]["fisheye"])

        self.camera.listen(16)
        self.tick()
        self.assertFalse(self.state()["cameras"]["fisheye"]["enabled"])
        self.tick()
        state = self.state()
        self.assertTrue(state["cameras"]["fisheye"]["enabled"])
        self.assertTrue(state["cameras"]["driveway"]["enabled"])
        self.assertEqual([["fisheye", "OFF"], ["fisheye", "ON"]], state["commands"])
        self.assertEqual({"fisheye": True, "driveway": True}, state["yaml_enabled"])

    def test_last_camera_is_not_disabled(self):
        state = self.state()
        del state["cameras"]["driveway"]
        self.state_path.write_text(json.dumps(state))
        self.tick()
        self.tick()
        self.assertEqual([], self.state()["commands"])
        self.assertTrue(self.state()["cameras"]["fisheye"]["enabled"])

    def test_failed_runtime_update_is_retried(self):
        state = self.state()
        state["fail_update"] = True
        self.state_path.write_text(json.dumps(state))
        self.tick()
        self.tick(expected=1)
        state = self.state()
        self.assertTrue(state["cameras"]["fisheye"]["enabled"])
        state["fail_update"] = False
        self.state_path.write_text(json.dumps(state))
        self.tick()
        self.assertFalse(self.state()["cameras"]["fisheye"]["enabled"])


if __name__ == "__main__":
    unittest.main()
