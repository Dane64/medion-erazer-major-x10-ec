import configparser
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from medion_fan_control.client import BackendError, DaemonClient, DemoBackend
from medion_fan_control.controller import DemoController, open_demo_protocol
from medion_fan_control.cpu import CpuBackend
from medion_fan_control.daemon import Daemon, DaemonServer, EcService, is_authorized
from medion_fan_control.lighting import LightZone, RgbColor
from medion_fan_control.settings import StateStore
from medion_fan_control.sysfs import Sysfs

from tests.fakesys import build_cpu, read


class DaemonTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        build_cpu(self.root)
        self.applied = []
        self.state_dir = self.root / "state"
        self.daemon = self.make_daemon()

    def make_daemon(self, config=None):
        return Daemon(
            config=config,
            state=StateStore(self.state_dir / "state.json"),
            ec=EcService(open_demo_protocol),
            cpu=CpuBackend(Sysfs(self.root)),
            gpu=Mock(), display=Mock(), audio=Mock(),
            apply_lighting=self.applied.append,
            state_dir=self.state_dir,
        )

    def request(self, method, **params):
        line = json.dumps({"id": 7, "method": method, "params": params}).encode()
        return self.daemon.handle_request(line)

    def test_ec_round_trip(self):
        self.assertEqual(self.request("ec.status")["result"]["profile"], "office")
        response = self.request("ec.set_profile", profile="gaming")
        self.assertEqual(response, {"id": 7, "result": response["result"]})
        self.assertEqual(response["result"]["profile"], "gaming")
        self.assertTrue(self.request("ec.set_full_speed", enabled=True)["result"]["full_speed"])

    def test_errors_are_structured(self):
        self.assertEqual(self.request("nope")["error"]["type"], "RequestError")
        self.assertEqual(self.request("ec.set_profile", profile="ludicrous")["error"]["type"], "RequestError")
        self.assertIn("missing parameter", self.request("ec.set_profile")["error"]["message"])
        self.assertEqual(self.request("ec.set_full_speed", enabled="yes")["error"]["type"], "RequestError")
        self.assertEqual(self.daemon.handle_request(b"{oops")["error"]["type"], "RequestError")
        self.assertEqual(self.request("cpu.set", epp="ludicrous")["error"]["type"], "CpuError")

    def test_lighting_off_is_sent_as_black_and_saved_with_color(self):
        result = self.request("lighting.apply", brightness=100, zones={
            "keyboard": {"color": "#112233", "on": True}, "lid": {"color": "#ff0000", "on": False}})["result"]
        self.assertEqual(self.applied[-1], {LightZone.KEYBOARD: RgbColor(0x11, 0x22, 0x33),
                                            LightZone.LID: RgbColor(0, 0, 0)})
        self.assertEqual(result["zones"]["lid"], {"color": "#ff0000", "on": False})
        self.assertEqual(StateStore(self.state_dir / "state.json").get("lighting")["zones"]["lid"]["on"], False)

    def test_cpu_settings_persist_separately_from_undervolt(self):
        self.request("cpu.set", no_turbo=True, undervolt={"core": -50})
        store = StateStore(self.state_dir / "state.json")
        self.assertEqual(store.get("cpu"), {"no_turbo": True})
        self.assertEqual(store.get("undervolt"), {"core": -50})

    def test_restore_respects_flags_and_crash_sentinel(self):
        self.request("lighting.apply", brightness=50, zones={"keyboard": {"color": "#ffffff", "on": True}})
        self.request("cpu.set", no_turbo=True, undervolt={"core": -40})
        self.request("restore.set", cpu=True, undervolt=True)
        for plane in ("core",):
            (self.root / f"sys/class/misc/x10-ec/undervolt_{plane}").write_text("0\n")
        (self.root / "sys/devices/system/cpu/intel_pstate/no_turbo").write_text("0\n")
        self.applied.clear()
        timers = []
        fresh = self.make_daemon()
        report = fresh.restore_on_start(timer_factory=lambda s, f: timers.append((s, f)) or Mock())
        self.assertEqual(self.applied[-1], {LightZone.KEYBOARD: RgbColor(128, 128, 128)})
        self.assertEqual(read(self.root, "sys/devices/system/cpu/intel_pstate/no_turbo"), "1")
        self.assertEqual(read(self.root, "sys/class/misc/x10-ec/undervolt_core"), "-40")
        self.assertTrue((self.state_dir / "undervolt.pending").exists())
        self.assertEqual(timers[0][0], 120.0)
        self.assertIn("undervolt: restored", report)
        # simulate a crash: the timer never fired, so the next start skips undervolt
        (self.root / "sys/class/misc/x10-ec/undervolt_core").write_text("0\n")
        report = self.make_daemon().restore_on_start(timer_factory=lambda s, f: Mock())
        self.assertEqual(read(self.root, "sys/class/misc/x10-ec/undervolt_core"), "0")
        self.assertTrue(any("skipped" in line for line in report))
        # after the stability window the sentinel is removed and restore resumes
        self.assertFalse((self.state_dir / "undervolt.pending").exists())

    def test_restore_flags_default_from_config(self):
        config = configparser.ConfigParser()
        config.read_string("[restore]\nlighting = false\ncpu = true\n")
        flags = self.make_daemon(config).restore_flags()
        self.assertEqual(flags, {"lighting": False, "cpu": True, "gpu": False, "undervolt": False, "audio": False})
        self.assertEqual(self.request("restore.set", bogus=True)["error"]["type"], "RequestError")


class AuthorizationTests(unittest.TestCase):
    def test_root_and_group_members_only(self):
        self.assertTrue(is_authorized(1, 0, 0, None))
        self.assertTrue(is_authorized(1, 1000, 990, 990))
        self.assertFalse(is_authorized(1, 1000, 1000, None))
        with patch("medion_fan_control.daemon.process_groups", return_value={27, 990}):
            self.assertTrue(is_authorized(1, 1000, 1000, 990))
        with patch("medion_fan_control.daemon.process_groups", return_value={27}):
            self.assertFalse(is_authorized(1, 1000, 1000, 990))


class SocketTests(unittest.TestCase):
    def test_client_server_round_trip_over_unix_socket(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        core = Daemon(state=StateStore(directory / "state.json"), ec=EcService(open_demo_protocol),
                      cpu=Mock(), gpu=Mock(), display=Mock(), audio=Mock(), state_dir=directory)
        path = directory / "x10.sock"
        with patch("medion_fan_control.daemon.is_authorized", return_value=True):
            server = DaemonServer(path, core, None)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                self.assertEqual(oct(path.stat().st_mode & 0o777), "0o660")
                client = DaemonClient(path, timeout=5)
                self.assertEqual(client.call("ec.set_profile", profile="turbo")["profile"], "turbo")
                with self.assertRaises(BackendError) as error:
                    client.call("ec.set_profile", profile="warp")
                self.assertEqual(error.exception.kind, "RequestError")
                client.close()
            finally:
                server.shutdown()
                server.server_close()

    def test_unauthorized_peer_is_rejected(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        core = Daemon(state=StateStore(directory / "state.json"), ec=Mock(), cpu=Mock(), gpu=Mock(),
                      display=Mock(), audio=Mock(), state_dir=directory)
        path = directory / "x10.sock"
        with patch("medion_fan_control.daemon.is_authorized", return_value=False):
            server = DaemonServer(path, core, None)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                with self.assertRaises(BackendError) as error:
                    DaemonClient(path, timeout=5).call("ec.status")
                self.assertEqual(error.exception.kind, "PermissionError")
            finally:
                server.shutdown()
                server.server_close()

    def test_missing_daemon_is_explained(self):
        with self.assertRaisesRegex(BackendError, "not running"):
            DaemonClient(Path("/nonexistent/x10.sock")).call("ec.status")


class DemoBackendTests(unittest.TestCase):
    def test_demo_implements_every_daemon_method(self):
        daemon = Daemon(state=Mock(), ec=Mock(), cpu=Mock(), gpu=Mock(), display=Mock(), audio=Mock())
        demo = DemoBackend()
        for method in daemon._methods:
            with self.subTest(method=method):
                self.assertTrue(hasattr(demo, "_" + method.replace(".", "_")), method)
        self.assertEqual(DemoController().read_status().profile.label, "office")


if __name__ == "__main__":
    unittest.main()
