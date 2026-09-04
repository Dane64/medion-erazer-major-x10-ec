import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from medion_fan_control.gui import DemoController, FanControlApp, TelemetryHistory, format_duration
from medion_fan_control.hardware import HardwareAccessError, PlatformSecurityStatus
from medion_fan_control.protocol import FanStatus, Profile


def make_status(cpu_rpm):
    return FanStatus(
        profile=Profile.OFFICE,
        full_speed=False,
        cpu_fan_rpm=cpu_rpm,
        gpu_fan_rpm=cpu_rpm - 100,
        cpu_temperature_c=60,
        gpu_temperature_c=55,
    )


class TelemetryHistoryTests(unittest.TestCase):
    def test_keeps_only_latest_samples(self):
        history = TelemetryHistory(max_samples=2)

        history.append(make_status(1000), recorded_at=1.0)
        history.append(make_status(2000), recorded_at=2.0)
        history.append(make_status(3000), recorded_at=3.0)

        self.assertEqual([sample.status.cpu_fan_rpm for sample in history.samples], [2000, 3000])
        self.assertEqual([sample.recorded_at for sample in history.samples], [2.0, 3.0])

    def test_filters_samples_to_selected_time_window(self):
        history = TelemetryHistory()
        history.append(make_status(1000), recorded_at=10.0)
        history.append(make_status(2000), recorded_at=40.0)
        history.append(make_status(3000), recorded_at=70.0)

        samples = history.samples_for_window(30, now=70.0)

        self.assertEqual([sample.status.cpu_fan_rpm for sample in samples], [2000, 3000])

    def test_formats_graph_window_duration(self):
        self.assertEqual(format_duration(30), "30s")
        self.assertEqual(format_duration(90), "1m 30s")
        self.assertEqual(format_duration(600), "10 min")


class DemoControllerTests(unittest.TestCase):
    def test_applies_profile_and_full_speed_state(self):
        controller = DemoController()

        self.assertEqual(controller.set_profile(Profile.TURBO), Profile.TURBO)
        self.assertTrue(controller.set_full_speed(True))
        status = controller.read_status()

        self.assertEqual(status.profile, Profile.TURBO)
        self.assertTrue(status.full_speed)
        self.assertEqual(status.cpu_fan_rpm, 5900)
        self.assertEqual(status.gpu_fan_rpm, 5600)


class PlatformSecurityDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        with patch("medion_fan_control.gui.load_lighting_colors", return_value={}):
            self.window = FanControlApp(demo=True)
        self.window.demo = False

    def tearDown(self):
        self.window.close()

    def test_blocks_connection_and_shows_secure_boot_instructions(self):
        security = PlatformSecurityStatus(secure_boot_enabled=True, lockdown_mode="integrity")

        with (
            patch("medion_fan_control.gui.read_platform_security", return_value=security),
            patch.object(QMessageBox, "critical") as critical,
        ):
            allowed = self.window._platform_security_allows_ec_access()

        self.assertFalse(allowed)
        self.assertEqual(self.window.connection_label.text(), "SECURE BOOT ENABLED")
        self.assertIn("EC access is disabled", self.window.status_label.text())
        self.assertIn("Set Secure Boot to Disabled", critical.call_args.args[2])

    def test_blocks_connection_when_security_state_cannot_be_read(self):
        with (
            patch(
                "medion_fan_control.gui.read_platform_security",
                side_effect=HardwareAccessError("cannot read Secure Boot state"),
            ),
            patch.object(QMessageBox, "critical") as critical,
        ):
            allowed = self.window._platform_security_allows_ec_access()

        self.assertFalse(allowed)
        self.assertEqual(self.window.connection_label.text(), "SECURITY CHECK FAILED")
        self.assertIn("blocked for safety", critical.call_args.args[2])

    def test_allows_connection_without_a_modal_when_security_is_disabled(self):
        security = PlatformSecurityStatus(secure_boot_enabled=False, lockdown_mode="none")

        with (
            patch("medion_fan_control.gui.read_platform_security", return_value=security),
            patch.object(QMessageBox, "critical") as critical,
        ):
            allowed = self.window._platform_security_allows_ec_access()

        self.assertTrue(allowed)
        critical.assert_not_called()


if __name__ == "__main__":
    unittest.main()