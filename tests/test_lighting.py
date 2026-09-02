import tempfile
import unittest
from pathlib import Path

from medion_fan_control.lighting import (
    HID_ID,
    HID_REPORT_DESCRIPTOR,
    HidLighting,
    LightingAccessError,
    LightZone,
    RgbColor,
    build_static_color_report,
    find_lighting_device,
    load_lighting_colors,
    save_lighting_colors,
)


class LightingTests(unittest.TestCase):
    def test_builds_vendor_static_color_report(self):
        report = build_static_color_report(LightZone.LID, RgbColor(0x12, 0x34, 0x56))

        self.assertEqual(len(report), 65)
        self.assertEqual(report[:15], bytes.fromhex("00 03 c0 03 00 80 80 80 22 12 34 56 00 00 00"))
        self.assertEqual(report[15:], bytes(50))

    def test_finds_only_verified_hid_interface(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hidraw_root = root / "sys" / "class" / "hidraw"
            hid_device = root / "devices" / "3-2:1.3" / "0003:1A2C:1512.0005"
            hid_device.mkdir(parents=True)
            (hid_device / "uevent").write_text(f"HID_ID={HID_ID}\n", encoding="ascii")
            (hid_device / "report_descriptor").write_bytes(HID_REPORT_DESCRIPTOR)
            (hid_device.parent / "bInterfaceNumber").write_text("03\n", encoding="ascii")
            hidraw_entry = hidraw_root / "hidraw4"
            hidraw_entry.mkdir(parents=True)
            (hidraw_entry / "device").symlink_to(hid_device)

            device = find_lighting_device(hidraw_root, root / "dev")

            self.assertEqual(device, root / "dev" / "hidraw4")

    def test_writes_complete_hidraw_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            device_path = root / "hidraw"
            device_path.write_bytes(b"")

            with HidLighting(device_path=device_path, lock_path=root / "lock") as lighting:
                lighting.set_static_color(LightZone.LEFT_SIDE, RgbColor(1, 2, 3))

            self.assertEqual(
                device_path.read_bytes(),
                build_static_color_report(LightZone.LEFT_SIDE, RgbColor(1, 2, 3)),
            )

    def test_repeats_complete_color_set_for_firmware_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            device_path = root / "hidraw"
            device_path.write_bytes(b"")
            colors = {
                LightZone.KEYBOARD: RgbColor(1, 2, 3),
                LightZone.LID: RgbColor(4, 5, 6),
            }
            sleeps = []

            with HidLighting(device_path=device_path, lock_path=root / "lock") as lighting:
                lighting.set_static_colors(colors, attempts=3, delay_seconds=0.025, sleep=sleeps.append)

            expected_pass = b"".join(build_static_color_report(zone, color) for zone, color in colors.items())
            self.assertEqual(device_path.read_bytes(), expected_pass * 3)
            self.assertEqual(sleeps, [0.025, 0.025])

    def test_saves_and_loads_selected_colors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config" / "lighting.json"
            colors = {
                LightZone.KEYBOARD: RgbColor(0x12, 0x34, 0x56),
                LightZone.RIGHT_SIDE: RgbColor(0xAB, 0xCD, 0xEF),
            }

            save_lighting_colors(colors, path)

            self.assertEqual(load_lighting_colors(path), colors)

    def test_rejects_invalid_saved_color(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lighting.json"
            path.write_text('{"colors": {"keyboard": "blue"}}', encoding="utf-8")

            with self.assertRaisesRegex(LightingAccessError, "invalid value"):
                load_lighting_colors(path)

    def test_parses_and_formats_rgb_color(self):
        color = RgbColor.from_hex("#1a2B3c")

        self.assertEqual(color, RgbColor(0x1A, 0x2B, 0x3C))
        self.assertEqual(color.hex, "#1a2b3c")


if __name__ == "__main__":
    unittest.main()