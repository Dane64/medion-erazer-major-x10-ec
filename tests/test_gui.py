import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from medion_fan_control.client import BackendError, DemoBackend
from medion_fan_control.gui import FanControlApp, PAGES
from medion_fan_control.lighting import LightZone
from medion_fan_control.protocol import Profile


def wait_for(predicate, timeout_ms=3000):
    for _ in range(timeout_ms // 10):
        if predicate():
            return True
        QTest.qWait(10)
    return predicate()


class RecordingBackend(DemoBackend):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.fail = {}

    def call(self, method, **params):
        self.calls.append((method, params))
        if method in self.fail:
            raise self.fail[method]
        return super().call(method, **params)


class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.question = self.enterContext(
            patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No))
        self.warning = self.enterContext(patch.object(QMessageBox, "warning"))
        self.backend = RecordingBackend()
        self.window = FanControlApp(backend=self.backend)
        self.window.show()
        self.assertTrue(wait_for(lambda: self.window._current_status is not None))

    def tearDown(self):
        self.window.close()
        self.application.processEvents()

    def methods(self):
        return [method for method, _ in self.backend.calls]

    def test_connects_through_backend_and_shows_security_state(self):
        self.assertEqual(self.window.connection_label.text(), "DEMO DATA")
        self.assertIn("SIGNED MODULE", self.window.security_label.text())
        self.assertEqual(self.methods()[:1], ["system.info"])

    def test_branding_and_navigation_icons_render(self):
        self.assertFalse(self.window.logo.pixmap().isNull())
        self.assertFalse(self.window.windowIcon().isNull())
        for button in self.window.nav_buttons:
            with self.subTest(page=button.text()):
                self.assertFalse(button.icon().pixmap(20, 20).isNull())

    def test_every_page_renders_from_the_sidebar(self):
        self.assertEqual(len(PAGES), 6)
        for index in range(len(PAGES)):
            with self.subTest(page=PAGES[index]):
                self.window.nav_buttons[index].click()
                self.assertEqual(self.window.stack.currentIndex(), index)
                QTest.qWait(50)
                self.assertFalse(self.window.grab().isNull())
        for method in ("cpu.get", "gpu.get", "display.get", "audio.get", "lighting.get"):
            self.assertTrue(wait_for(lambda m=method: m in self.methods()), method)

    def test_cancelled_profile_change_sends_nothing(self):
        self.window._mode_buttons[Profile.GAMING].click()
        self.assertTrue(self.window._mode_buttons[Profile.OFFICE].isChecked())
        self.assertNotIn("ec.set_profile", self.methods())

    def test_confirmed_profile_and_full_speed_changes(self):
        self.question.return_value = QMessageBox.StandardButton.Yes
        self.window._mode_buttons[Profile.TURBO].click()
        self.assertTrue(wait_for(lambda: self.window._current_status.profile == Profile.TURBO))
        self.window.full_speed_check.click()
        self.assertTrue(wait_for(lambda: self.window._current_status.full_speed))
        self.assertIn("FULL SPEED", self.window.profile_badge.text())

    def test_failed_change_disables_controls_until_retry(self):
        self.question.return_value = QMessageBox.StandardButton.Yes
        self.backend.fail["ec.set_profile"] = BackendError("readback failed", "ProtocolError")
        self.window._mode_buttons[Profile.GAMING].click()
        self.assertTrue(wait_for(lambda: self.window.connection_label.text() == "OFFLINE"))
        self.assertFalse(self.window.full_speed_check.isEnabled())
        self.assertEqual(self.window.dashboard.cpu_rpm_label.text(), "--")
        del self.backend.fail["ec.set_profile"]
        self.window.retry_button.click()
        self.assertTrue(wait_for(lambda: self.window.full_speed_check.isEnabled()))

    def test_led_zone_can_be_switched_off_and_applied(self):
        self.window.show_page(5)
        page = self.window.led_page
        self.assertTrue(wait_for(lambda: bool(page.switches)))
        page.switches[LightZone.LID].setChecked(False)
        self.assertEqual(page.swatches[LightZone.LID].text(), "OFF")
        with patch("medion_fan_control.gui.QColorDialog.getColor", return_value=QColor("#123456")):
            page.choose_color(LightZone.KEYBOARD)
        self.question.return_value = QMessageBox.StandardButton.Yes
        page.apply()
        self.assertTrue(wait_for(lambda: "lighting.apply" in self.methods()))
        params = dict(self.backend.calls)["lighting.apply"]
        self.assertEqual(params["zones"]["lid"]["on"], False)
        self.assertEqual(params["zones"]["keyboard"], {"color": "#123456", "on": True})
        page.set_all_on(False)
        self.assertTrue(all(not s["on"] for s in page.settings.values()))

    def test_undervolt_requires_confirmation(self):
        self.window.show_page(1)
        page = self.window.cpu_page
        self.assertTrue(wait_for(lambda: bool(getattr(page, "offsets", None))))
        page.offsets["core"].setValue(-50)
        page.apply_undervolt()
        self.assertNotIn("cpu.set", self.methods())
        self.question.return_value = QMessageBox.StandardButton.Yes
        page.apply_undervolt()
        self.assertTrue(wait_for(lambda: self.backend.cpu["undervolt"]["planes"]["core"] == -50))

    def test_cpu_apply_sends_validated_shape(self):
        self.window.show_page(1)
        page = self.window.cpu_page
        self.assertTrue(wait_for(lambda: bool(getattr(page, "cluster_sliders", None))))
        page.turbo.setChecked(False)
        params = page.collect()
        self.assertTrue(params["no_turbo"])
        self.assertEqual([c["name"] for c in params["clusters"]], ["P-cores", "E-cores"])
        self.assertIn("pl1", params["rapl"])
        page.apply()
        self.assertTrue(wait_for(lambda: self.backend.cpu["pstate"]["no_turbo"] is True))

    def test_gpu_power_off_uses_dgpu_ctl_after_confirmation(self):
        self.window.show_page(2)
        page = self.window.gpu_page
        self.assertTrue(wait_for(lambda: hasattr(page, "dgpu_off")))
        self.question.return_value = QMessageBox.StandardButton.Yes
        page.set_dgpu(False)
        self.assertTrue(wait_for(lambda: self.backend.dgpu_on is False))

    def test_page_errors_are_shown_on_the_page(self):
        self.backend.fail["gpu.get"] = BackendError("permission denied", "PermissionError")
        self.window.show_page(2)
        self.assertTrue(wait_for(lambda: "permission denied" in self.window.gpu_page.status.text()))


if __name__ == "__main__":
    unittest.main()
