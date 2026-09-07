import os
import threading
import unittest
from concurrent.futures import Future, wait
from dataclasses import replace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from medion_fan_control.gui import FanControlApp
from medion_fan_control.hardware import HardwareAccessError, PlatformSecurityStatus
from medion_fan_control.lighting import LightingAccessError, LightZone, RgbColor
from medion_fan_control.protocol import FanStatus, InsufficientPowerError, Profile, ProtocolError


def make_status(cpu_rpm=2000):
    return FanStatus(
        profile=Profile.OFFICE,
        full_speed=False,
        cpu_fan_rpm=cpu_rpm,
        gpu_fan_rpm=cpu_rpm - 100,
        cpu_temperature_c=60,
        gpu_temperature_c=55,
    )


def completed_future(*, status=None, error=None):
    future = Future()
    if error is None:
        future.set_result(status)
    else:
        future.set_exception(error)
    return future


class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.hardware = self.enterContext(
            patch("medion_fan_control.gui.open_supported_protocol", side_effect=AssertionError("real EC access"))
        )
        self.lighting = self.enterContext(
            patch("medion_fan_control.gui.apply_static_lighting", side_effect=AssertionError("real HID access"))
        )
        self.save_lighting = self.enterContext(
            patch("medion_fan_control.gui.apply_and_save_lighting", side_effect=AssertionError("real HID access"))
        )
        self.load_colors = self.enterContext(patch("medion_fan_control.gui.load_lighting_colors", return_value={}))
        self.question = self.enterContext(patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No))
        self.warning = self.enterContext(patch.object(QMessageBox, "warning"))
        self.critical = self.enterContext(patch.object(QMessageBox, "critical"))
        with patch.object(QTimer, "singleShot"):
            self.window = FanControlApp(demo=True)

    def tearDown(self):
        self.window.close()
        self.window.executor.shutdown(wait=True)
        self.window.lighting_executor.shutdown(wait=True)
        self.application.processEvents()

    def connect_demo(self):
        status = self.window.executor.submit(self.window.session.open).result(timeout=3)
        self.window._session_opened(completed_future(status=status))
        self.window._poll_timer.stop()

    def finish_change(self):
        future = self.window._pending
        if future is not None:
            wait([future], timeout=3)
            self.assertTrue(future.done(), "controller worker did not complete")
            self.window._watch_future(future, self.window._change_completed)
        self.window._poll_timer.stop()

    def test_demo_does_not_load_settings_or_access_hardware(self):
        self.window._open_session()
        future = self.window._pending
        if future is not None:
            wait([future], timeout=3)
            self.assertTrue(future.done())
            self.window._watch_future(future, self.window._session_opened)
        self.assertEqual(self.window.connection_label.text(), "DEMO DATA")
        self.load_colors.assert_not_called()
        self.hardware.assert_not_called()
        self.lighting.assert_not_called()
        self.save_lighting.assert_not_called()

    def test_demo_opens_polls_and_closes_through_the_event_loop(self):
        self.window.show()
        self.window._open_session()
        for _ in range(100):
            if self.window._current_status is not None:
                break
            QTest.qWait(10)
        self.assertIsNotNone(self.window._current_status)
        initial_samples = len(self.window.history.samples)
        self.window._poll_timer.stop()
        self.window._request_status()
        for _ in range(100):
            if len(self.window.history.samples) > initial_samples:
                break
            QTest.qWait(10)
        self.assertGreater(len(self.window.history.samples), initial_samples)
        self.assertFalse(self.window.grab().isNull())
        self.window.close()
        self.assertFalse(self.window._poll_timer.isActive())
        self.hardware.assert_not_called()

    def test_tabs_do_not_change_on_hover_and_support_keyboard_navigation(self):
        self.window.show()
        self.application.processEvents()
        bar = self.window.tabs.tabBar()
        QTest.mouseMove(bar, bar.tabRect(1).center())
        self.assertEqual(self.window.tabs.currentIndex(), 0)
        bar.setFocus()
        QTest.keyClick(bar, Qt.Key.Key_Right)
        self.assertEqual(self.window.tabs.currentIndex(), 1)

    def test_small_window_keeps_both_pages_usable_without_horizontal_scrolling(self):
        self.connect_demo()
        self.window._apply_status(replace(make_status(), profile=Profile.GAMING, full_speed=True))
        self.window.resize(720, 560)
        self.window.show()
        for tab_index in range(self.window.tabs.count()):
            with self.subTest(tab_index=tab_index):
                self.window.tabs.setCurrentIndex(tab_index)
                self.application.processEvents()
                self.assertEqual(self.window.width(), 720)
                self.assertEqual(self.window.tabs.widget(tab_index).horizontalScrollBar().maximum(), 0)
                self.assertFalse(self.window.grab().isNull())

    def test_graph_slider_updates_the_time_window_and_label(self):
        self.window.window_slider.setValue(1)
        self.assertEqual(self.window.graph._window_seconds, 30)
        self.assertEqual(self.window.window_label.text(), "30s")
        self.window.window_slider.setValue(20)
        self.assertEqual(self.window.graph._window_seconds, 600)
        self.assertEqual(self.window.window_label.text(), "10 min")
        with self.assertRaises(ValueError):
            self.window.graph.set_window_seconds(0)

    def test_shutdown_errors_are_logged(self):
        with self.assertLogs("medion_fan_control.gui", level="ERROR") as logs:
            self.window._log_shutdown_error(completed_future(error=HardwareAccessError("close failed")))
        self.assertIn("close failed", logs.output[0])

    def test_close_logs_lighting_failure_even_without_a_ui_callback(self):
        self.window._lighting_pending = completed_future(error=LightingAccessError("HID write failed"))
        with self.assertLogs("medion_fan_control.gui", level="ERROR") as logs:
            self.window.close()
        self.assertIn("HID write failed", logs.output[0])

    def test_cancelling_profile_change_restores_confirmed_selection(self):
        self.connect_demo()
        self.window._mode_buttons[Profile.GAMING].click()
        self.assertTrue(self.window._mode_buttons[Profile.OFFICE].isChecked())
        self.assertEqual(self.window._current_status.profile, Profile.OFFICE)
        self.assertIsNone(self.window._pending)
        self.assertEqual(self.question.call_args.args[-1], QMessageBox.StandardButton.No)

    def test_cancelling_full_speed_restores_confirmed_state(self):
        self.connect_demo()
        self.window.full_speed_check.click()
        self.assertFalse(self.window.full_speed_check.isChecked())
        self.assertFalse(self.window._current_status.full_speed)
        self.assertIsNone(self.window._pending)
        self.assertEqual(self.question.call_args.args[-1], QMessageBox.StandardButton.No)

    def test_confirmed_profile_and_override_changes_update_demo(self):
        self.connect_demo()
        self.question.return_value = QMessageBox.StandardButton.Yes
        self.window._mode_buttons[Profile.GAMING].click()
        self.finish_change()
        self.assertEqual(self.window._current_status.profile, Profile.GAMING)
        self.window.full_speed_check.click()
        self.finish_change()
        self.assertTrue(self.window._current_status.full_speed)
        self.assertTrue(self.window.full_speed_check.isChecked())
        self.assertIn("FULL SPEED", self.window.profile_badge.text())

    def test_change_during_poll_is_serialized_and_stale_callback_is_ignored(self):
        self.connect_demo()
        entered, release = threading.Event(), threading.Event()

        def delayed_read():
            entered.set()
            if not release.wait(timeout=3):
                raise AssertionError("poll was not released")
            return make_status()

        with patch.object(self.window.session, "read_status", side_effect=delayed_read):
            try:
                self.window._request_status()
                self.assertTrue(entered.wait(timeout=3))
                poll = self.window._pending
                self.question.return_value = QMessageBox.StandardButton.Yes
                self.window._mode_buttons[Profile.GAMING].click()
                self.assertIsNot(self.window._pending, poll)
                release.set()
                self.finish_change()
                callback = Mock()
                self.window._watch_future(poll, callback)
                callback.assert_not_called()
                self.assertEqual(self.window._current_status.profile, Profile.GAMING)
                self.assertFalse(self.window._change_in_progress)
            finally:
                release.set()

    def test_read_error_disables_changes_and_clears_stale_readings(self):
        self.connect_demo()
        self.window._status_received(completed_future(error=HardwareAccessError("device disappeared")))
        self.assertIsNone(self.window._current_status)
        self.assertFalse(self.window.full_speed_check.isEnabled())
        self.assertTrue(all(not button.isEnabled() for button in self.window._mode_buttons.values()))
        self.assertEqual(self.window.cpu_rpm_label.text(), "--")
        self.assertEqual(self.window.profile_badge.text(), "UNAVAILABLE")
        self.assertTrue(self.window.retry_button.isEnabled())
        self.assertFalse(self.window._poll_timer.isActive())
        self.assertIn("device disappeared", self.window.status_label.text())

    def test_failed_change_does_not_claim_the_previous_state_is_still_current(self):
        self.connect_demo()
        self.window._change_completed(completed_future(error=ProtocolError("readback failed")))
        self.assertIsNone(self.window._current_status)
        self.assertEqual(self.window.connection_label.text(), "OFFLINE")
        self.assertIn("Change could not be confirmed", self.window.status_label.text())
        self.assertFalse(self.window.full_speed_check.isEnabled())

    def test_reconnection_restores_controls_after_failure(self):
        self.connect_demo()
        self.window._status_received(completed_future(error=OSError("read failed")))
        self.window._open_session()
        future = self.window._pending
        if future is not None:
            wait([future], timeout=3)
            self.assertTrue(future.done())
            self.window._watch_future(future, self.window._session_opened)
        self.assertEqual(self.window.connection_label.text(), "DEMO DATA")
        self.assertTrue(self.window.full_speed_check.isEnabled())
        self.assertFalse(self.window.retry_button.isEnabled())
        self.assertNotEqual(self.window.cpu_rpm_label.text(), "--")

    def test_turbo_button_tracks_power_availability(self):
        self.connect_demo()
        self.window.demo = False
        self.window._status_received(completed_future(status=replace(make_status(), turbo_available=False)))
        turbo = self.window._mode_buttons[Profile.TURBO]
        self.assertFalse(turbo.isEnabled())
        self.assertIn("USB-C PD", turbo.toolTip())
        self.assertIn("AC barrel adapter", self.window.power_label.text())
        self.window._status_received(completed_future(status=make_status()))
        self.assertTrue(turbo.isEnabled())

    def test_lost_turbo_power_restores_controls_without_disconnecting(self):
        self.connect_demo()
        self.window._change_completed(completed_future(error=InsufficientPowerError("connect AC barrel adapter")))
        self.assertEqual(self.window._current_status.profile, Profile.OFFICE)
        self.assertTrue(self.window._mode_buttons[Profile.OFFICE].isEnabled())
        self.assertFalse(self.window._mode_buttons[Profile.TURBO].isEnabled())
        self.warning.assert_called_once()

    def test_demo_lighting_can_be_previewed_without_writes(self):
        with patch("medion_fan_control.gui.QColorDialog.getColor", return_value=QColor("#123456")):
            self.window._choose_lighting_color(None)
        self.assertEqual(len(self.window._lighting_colors), 4)
        self.assertTrue(self.window.apply_lighting_button.isEnabled())
        self.question.return_value = QMessageBox.StandardButton.Yes
        self.window._request_lighting_change()
        self.assertIn("nothing was written or saved", self.window.lighting_status_label.text())
        self.lighting.assert_not_called()
        self.save_lighting.assert_not_called()

    def test_cancelling_lighting_does_not_submit_a_write(self):
        self.window.demo = False
        self.window._lighting_colors = {LightZone.KEYBOARD: RgbColor(1, 2, 3)}
        self.window._request_lighting_change()
        self.save_lighting.assert_not_called()
        self.assertIsNone(self.window._lighting_pending)
        self.assertEqual(self.question.call_args.args[-1], QMessageBox.StandardButton.No)

    def test_restore_uses_saved_colors_not_unconfirmed_edits_and_runs_once(self):
        self.window.demo = False
        saved = {LightZone.KEYBOARD: RgbColor(1, 2, 3)}
        self.window._saved_lighting_colors = saved
        self.window._lighting_colors = {LightZone.KEYBOARD: RgbColor(4, 5, 6)}
        self.lighting.side_effect = None
        self.window._restore_saved_lighting()
        future = self.window._lighting_pending
        if future is not None:
            wait([future], timeout=3)
            self.assertTrue(future.done())
            self.window._watch_lighting_future(future, saved, "Keyboard", False)
        self.window._restore_saved_lighting()
        self.lighting.assert_called_once_with(saved)
        self.assertEqual(self.window._lighting_colors[LightZone.KEYBOARD], RgbColor(4, 5, 6))

    def test_lighting_errors_remain_visible_after_telemetry_updates(self):
        self.window._watch_lighting_future(
            completed_future(error=LightingAccessError("HID write failed")), {}, "Keyboard", True,
        )
        self.window._apply_status(make_status())
        self.assertEqual(self.window.lighting_status_label.text(), "HID write failed")

    def test_invalid_saved_colors_are_visible_without_auto_restore(self):
        self.window.close()
        self.window.executor.shutdown(wait=True)
        self.window.lighting_executor.shutdown(wait=True)
        self.load_colors.side_effect = LightingAccessError("saved colors are corrupt")
        with patch.object(QTimer, "singleShot"):
            self.window = FanControlApp(demo=False)
        self.assertEqual(self.window.lighting_status_label.text(), "saved colors are corrupt")
        self.window._restore_saved_lighting()
        self.lighting.assert_not_called()

    def test_closing_finishes_and_saves_an_already_confirmed_lighting_change(self):
        from medion_fan_control.controller import apply_and_save_lighting

        self.window.demo = False
        colors = {LightZone.KEYBOARD: RgbColor(1, 2, 3)}
        self.window._lighting_colors = colors
        self.question.return_value = QMessageBox.StandardButton.Yes
        entered, release = threading.Event(), threading.Event()

        def delayed_apply(_colors):
            entered.set()
            if not release.wait(timeout=3):
                raise AssertionError("lighting write was not released")

        with (
            patch("medion_fan_control.gui.apply_and_save_lighting", side_effect=apply_and_save_lighting),
            patch("medion_fan_control.controller.apply_static_lighting", side_effect=delayed_apply),
            patch("medion_fan_control.controller.save_lighting_colors") as save,
        ):
            try:
                self.window._request_lighting_change()
                self.assertTrue(entered.wait(timeout=3))
                self.window.close()
            finally:
                release.set()
                self.window.lighting_executor.shutdown(wait=True)
            save.assert_called_once_with(colors)

    def test_blocks_connection_and_shows_secure_boot_instructions(self):
        security = PlatformSecurityStatus(secure_boot_enabled=True, lockdown_mode="integrity")

        with (
            patch("medion_fan_control.gui.read_platform_security", return_value=security),
            patch.object(QMessageBox, "critical") as critical,
        ):
            allowed = self.window._platform_security_allows_ec_access()

        self.assertFalse(allowed)
        self.assertEqual(self.window.connection_label.text(), "EC ACCESS BLOCKED")
        self.assertIn("EC access is disabled", self.window.status_label.text())
        self.assertIn("set Secure Boot to Disabled", critical.call_args.args[2])

    def test_lockdown_without_secure_boot_has_accurate_guidance(self):
        security = PlatformSecurityStatus(secure_boot_enabled=False, lockdown_mode="integrity")
        with patch("medion_fan_control.gui.read_platform_security", return_value=security):
            self.assertFalse(self.window._platform_security_allows_ec_access())
        self.assertIn("independently of Secure Boot", self.critical.call_args.args[2])
        self.assertNotIn("set Secure Boot to Disabled", self.critical.call_args.args[2])

    def test_blocked_platform_never_submits_a_hardware_connection(self):
        self.window.demo = False
        security = PlatformSecurityStatus(secure_boot_enabled=True, lockdown_mode="integrity")
        with (
            patch("medion_fan_control.gui.read_platform_security", return_value=security),
            patch.object(self.window.executor, "submit") as submit,
        ):
            self.window._open_session()
            submit.assert_not_called()
        self.hardware.assert_not_called()

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