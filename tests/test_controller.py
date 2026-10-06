import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

from medion_fan_control.controller import DemoController, ProtocolSession, apply_and_save_lighting
from medion_fan_control.hardware import HardwareAccessError
from medion_fan_control.lighting import LightingAccessError, LightZone, RgbColor
from medion_fan_control.protocol import InsufficientPowerError, Profile, ProtocolError


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.controller = Mock(spec=DemoController)
        self.controller.read_status.return_value = DemoController().read_status()
        self.events = []

        @contextmanager
        def factory():
            self.events.append("open")
            try:
                yield self.controller
            finally:
                self.events.append("close")

        self.session = ProtocolSession(factory)
        self.addCleanup(self.session.close)

    def test_initial_read_failure_releases_context_and_allows_retry(self):
        status = self.controller.read_status.return_value
        self.controller.read_status.side_effect = [ProtocolError("invalid profile"), status]
        with self.assertRaisesRegex(ProtocolError, "invalid profile"):
            self.session.open()
        self.assertEqual(self.events, ["open", "close"])
        self.assertEqual(self.session.open(), status)
        self.assertEqual(self.events, ["open", "close", "open"])

    def test_context_entry_failure_does_not_leave_a_session(self):
        factory = Mock(side_effect=HardwareAccessError("port missing"))
        session = ProtocolSession(factory)
        with self.assertRaisesRegex(HardwareAccessError, "port missing"):
            session.open()
        session.close()
        with self.assertRaisesRegex(HardwareAccessError, "not open"):
            session.read_status()

    def test_retry_reopens_instead_of_reusing_the_previous_context(self):
        self.session.open()
        self.session.open()
        self.assertEqual(self.events, ["open", "close", "open"])

    def test_close_is_idempotent_and_operations_require_an_open_session(self):
        self.session.open()
        self.session.close()
        self.session.close()
        self.assertEqual(self.events, ["open", "close"])
        for operation in (
            self.session.read_status,
            lambda: self.session.set_profile(Profile.GAMING),
            lambda: self.session.set_full_speed(True),
        ):
            with self.subTest(operation=operation), self.assertRaisesRegex(HardwareAccessError, "not open"):
                operation()

    def test_failed_poll_closes_the_transport(self):
        self.session.open()
        self.controller.read_status.side_effect = OSError("short read")
        with self.assertRaisesRegex(OSError, "short read"):
            self.session.read_status()
        self.assertEqual(self.events, ["open", "close"])

    def test_failed_change_readback_closes_the_transport(self):
        self.session.open()
        self.controller.read_status.side_effect = ProtocolError("readback failed")
        with self.assertRaisesRegex(ProtocolError, "readback failed"):
            self.session.set_profile(Profile.GAMING)
        self.controller.set_profile.assert_called_once_with(Profile.GAMING)
        self.assertEqual(self.events, ["open", "close"])

    def test_insufficient_power_does_not_destroy_a_usable_session(self):
        self.session.open()
        self.controller.set_profile.side_effect = InsufficientPowerError("AC required")
        with self.assertRaises(InsufficientPowerError):
            self.session.set_profile(Profile.TURBO)
        self.assertEqual(self.events, ["open"])
        self.assertEqual(self.session.read_status(), self.controller.read_status.return_value)

    def test_changes_return_fresh_readback(self):
        self.session.open()
        self.assertEqual(self.session.set_profile(Profile.GAMING), self.controller.read_status.return_value)
        self.assertEqual(self.session.set_full_speed(True), self.controller.read_status.return_value)
        self.controller.set_profile.assert_called_once_with(Profile.GAMING)
        self.controller.set_full_speed.assert_called_once_with(True)
        self.assertEqual(self.controller.read_status.call_count, 3)


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

    def test_disabling_override_returns_to_the_selected_profile(self):
        with patch("medion_fan_control.controller.time.monotonic", return_value=10):
            controller = DemoController()
            controller.set_profile(Profile.GAMING)
            expected = controller.read_status()
            controller.set_full_speed(True)
            controller.set_full_speed(False)
            self.assertEqual(controller.read_status(), expected)


class LightingOperationTests(unittest.TestCase):
    def setUp(self):
        self.colors = {LightZone.KEYBOARD: RgbColor(1, 2, 3)}

    def test_failed_hardware_write_does_not_save_the_selection(self):
        with (
            patch("medion_fan_control.controller.apply_static_lighting", side_effect=LightingAccessError("HID failed")),
            patch("medion_fan_control.controller.save_lighting_colors") as save,
        ):
            with self.assertRaisesRegex(LightingAccessError, "HID failed"):
                apply_and_save_lighting(self.colors)
            save.assert_not_called()

    def test_save_failure_distinguishes_applied_colors_from_persisted_colors(self):
        with (
            patch("medion_fan_control.controller.apply_static_lighting") as apply,
            patch("medion_fan_control.controller.save_lighting_colors", side_effect=LightingAccessError("disk full")),
        ):
            with self.assertRaisesRegex(LightingAccessError, "Lighting applied, but.*disk full"):
                apply_and_save_lighting(self.colors)
            apply.assert_called_once_with(self.colors)

    def test_applies_before_saving(self):
        operations = Mock()
        with (
            patch("medion_fan_control.controller.apply_static_lighting", operations.apply),
            patch("medion_fan_control.controller.save_lighting_colors", operations.save),
        ):
            apply_and_save_lighting(self.colors)
        self.assertEqual([call[0] for call in operations.mock_calls], ["apply", "save"])


if __name__ == "__main__":
    unittest.main()
