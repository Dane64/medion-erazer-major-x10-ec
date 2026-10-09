import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from medion_fan_control.display import (
    DisplayBackend, DisplayError, KScreen, build_overclocked_edid, native_timing, overclock_blocker,
)
from medion_fan_control.sysfs import Sysfs

from tests.fakesys import make_edid, read, write


class EdidTests(unittest.TestCase):
    def test_parses_native_timing(self):
        timing = native_timing(make_edid())
        self.assertEqual((timing.h_active, timing.v_active), (2560, 1440))
        self.assertAlmostEqual(timing.refresh_hz, 645_000_000 / (2720 * 1481), places=3)

    def test_overclock_scales_pixel_clock_and_fixes_checksum(self):
        edid = make_edid(pixel_clock_10khz=24150, h_active=1920, h_blank=160, v_active=1080, v_blank=36)  # 104 Hz
        native = native_timing(edid).refresh_hz
        data, _timing, achieved = build_overclocked_edid(edid, 120)
        self.assertEqual(sum(data) % 256, 0)
        self.assertAlmostEqual(achieved, 120, delta=0.1)
        self.assertAlmostEqual(native_timing(data).refresh_hz, achieved, places=3)
        self.assertEqual(data[56:72], edid[56:72])  # only the clock bytes change
        for target in (native - 1, native * 1.3):
            with self.subTest(target=target), self.assertRaises(DisplayError):
                build_overclocked_edid(edid, target)

    def test_refuses_without_dtd_headroom(self):
        edid = make_edid()  # 2560x1440 @ ~160 Hz at 645 MHz: max 655.35 MHz
        self.assertIsNone(overclock_blocker(edid))  # ~2.6 Hz headroom remains
        tight = make_edid(pixel_clock_10khz=65500)
        self.assertIn("no headroom", overclock_blocker(tight))
        with self.assertRaises(DisplayError):
            build_overclocked_edid(tight, 170)

    def test_refuses_when_top_mode_is_in_a_displayid_extension(self):
        base = bytearray(make_edid(pixel_clock_10khz=24150, h_active=1920, h_blank=160, v_active=1080, v_blank=36))
        base[126] = 1
        base[127] = (-sum(base[:127])) & 0xFF
        ext = bytearray(128)
        ext[0], ext[1], ext[2] = 0x70, 0x20, 3 + 20 + 5
        timing = bytearray(20)
        clock = 593_000 - 1  # Type VII, 1 kHz units: 1920x1080 @ ~240 Hz
        timing[0:3] = clock.to_bytes(3, "little")
        for offset, value in ((4, 1920 - 1), (6, 160 - 1), (12, 1080 - 1), (14, 36 - 1)):
            timing[offset:offset + 2] = value.to_bytes(2, "little")
        ext[5:8] = bytes((0x22, 0x00, 20))
        ext[8:28] = timing
        message = overclock_blocker(bytes(base) + bytes(ext))
        self.assertIn("extension block", message)

    def test_rejects_invalid_edid(self):
        with self.assertRaises(DisplayError):
            native_timing(b"\x00" * 128)


class DisplayBackendTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        write(self.root, "sys/class/backlight/intel_backlight/max_brightness", 400)
        write(self.root, "sys/class/backlight/intel_backlight/brightness", 200)
        write(self.root, "sys/class/drm/card0-eDP-1/status", "connected")
        write(self.root, "sys/class/drm/card0-eDP-1/edid",
              make_edid(pixel_clock_10khz=24150, h_active=1920, h_blank=160, v_active=1080, v_blank=36))
        self.display = DisplayBackend(Sysfs(self.root))

    def test_backlight(self):
        self.assertEqual(self.display.get()["backlight"]["percent"], 50)
        self.display.set_brightness(25)
        self.assertEqual(read(self.root, "sys/class/backlight/intel_backlight/brightness"), "100")
        with self.assertRaises(DisplayError):
            self.display.set_brightness(0)

    def test_install_and_remove_override(self):
        result = self.display.install_overclock(120)
        self.assertEqual(result["installed"], "x10-eDP-1-120hz.bin")
        self.assertEqual(result["kernel_parameter"], "drm.edid_firmware=eDP-1:edid/x10-eDP-1-120hz.bin")
        self.display.install_overclock(110)
        files = sorted(p.name for p in (self.root / "usr/lib/firmware/edid").iterdir())
        self.assertEqual(files, ["x10-eDP-1-110hz.bin"])
        self.display.remove_overclock()
        self.assertEqual(list((self.root / "usr/lib/firmware/edid").iterdir()), [])


class KScreenTests(unittest.TestCase):
    def test_parses_modes_for_current_resolution(self):
        document = {"outputs": [{
            "name": "eDP-1", "connected": True, "enabled": True, "currentModeId": "1", "vrrPolicy": 2,
            "modes": [
                {"id": "1", "refreshRate": 165.0, "size": {"width": 2560, "height": 1440}},
                {"id": "2", "refreshRate": 60.0, "size": {"width": 2560, "height": 1440}},
                {"id": "3", "refreshRate": 60.0, "size": {"width": 1920, "height": 1080}},
            ]}]}
        calls = []

        def runner(argv):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, json.dumps(document), "")

        kscreen = KScreen(runner, which=lambda name: "/usr/bin/kscreen-doctor")
        output = kscreen.outputs()[0]
        self.assertEqual([m["id"] for m in output["modes"]], ["1", "2"])
        kscreen.set_mode("eDP-1", "2")
        self.assertEqual(calls[-1], ["kscreen-doctor", "output.eDP-1.mode.2"])
        with self.assertRaises(DisplayError):
            kscreen.set_mode("eDP-1; rm", "2")

    def test_missing_kscreen_is_reported(self):
        with self.assertRaisesRegex(DisplayError, "KDE Plasma"):
            KScreen(which=lambda name: None).outputs()


if __name__ == "__main__":
    unittest.main()
