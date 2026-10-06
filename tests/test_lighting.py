import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from medion_fan_control.hardware import SUPPORTED_IDENTITY, UnsupportedHardwareError
from medion_fan_control.lighting import (
    HID_ID,
    HID_REPORT_DESCRIPTOR,
    HidLighting,
    LightingAccessError,
    LightZone,
    RgbColor,
    apply_static_lighting,
    build_static_color_report,
    find_lighting_device,
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

    def test_parses_and_formats_rgb_color(self):
        color = RgbColor.from_hex("#1a2B3c")

        self.assertEqual(color, RgbColor(0x1A, 0x2B, 0x3C))
        self.assertEqual(color.hex, "#1a2b3c")

    def test_rejects_non_hex_and_non_integer_colors(self):
        for value in ("blue", "#12345", "#1234567", "#+1+2+3", "# 1 2 3", "#12345g", None, 123456, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                RgbColor.from_hex(value)
        for value in (-1, 256, 0.5, True, "12"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                RgbColor(value, 0, 0)

    def test_rejects_unverified_hid_identity_interface_and_descriptor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            device = root / "devices" / "usb-interface" / "hid-device"
            device.mkdir(parents=True)
            entry = root / "hidraw" / "hidraw0"
            entry.mkdir(parents=True)
            (entry / "device").symlink_to(device)
            for hid_id, interface, descriptor in (
                ("0003:00000000:00000000", "03", HID_REPORT_DESCRIPTOR),
                (HID_ID, "02", HID_REPORT_DESCRIPTOR),
                (HID_ID, "03", b"unverified descriptor"),
            ):
                with self.subTest(hid_id=hid_id, interface=interface, descriptor=descriptor):
                    (device / "uevent").write_text(f"HID_ID={hid_id}\n", encoding="ascii")
                    (device.parent / "bInterfaceNumber").write_text(interface, encoding="ascii")
                    (device / "report_descriptor").write_bytes(descriptor)
                    with self.assertRaisesRegex(LightingAccessError, "was not found"):
                        find_lighting_device(root / "hidraw", root / "dev")

    def test_short_write_is_an_error_and_lock_is_released(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            device = root / "hidraw"
            device.write_bytes(b"")
            with self.assertRaisesRegex(LightingAccessError, "short lighting report write"):
                with HidLighting(device_path=device, lock_path=root / "lock") as lighting:
                    with patch("medion_fan_control.lighting.os.write", return_value=64):
                        lighting.set_static_color(LightZone.KEYBOARD, RgbColor(1, 2, 3))
            with HidLighting(device_path=device, lock_path=root / "lock"):
                pass

    def test_lock_contention_failed_open_and_repeated_close(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            device = root / "hidraw"
            first = HidLighting(device_path=device, lock_path=root / "lock")
            with self.assertRaises(LightingAccessError):
                first.__enter__()
            device.write_bytes(b"")
            second = HidLighting(device_path=device, lock_path=root / "lock")
            with first:
                with self.assertRaisesRegex(LightingAccessError, "another process"):
                    second.__enter__()
                with self.assertRaisesRegex(LightingAccessError, "already open"):
                    first.__enter__()
            with second:
                pass
            first.close()
            second.close()
            with self.assertRaisesRegex(LightingAccessError, "not open"):
                second.set_static_color(LightZone.KEYBOARD, RgbColor(1, 2, 3))

    def test_validates_the_entire_color_set_before_any_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            device = root / "hidraw"
            device.write_bytes(b"")
            with HidLighting(device_path=device, lock_path=root / "lock") as lighting:
                with self.assertRaises(ValueError):
                    lighting.set_static_colors({LightZone.KEYBOARD: RgbColor(1, 2, 3), 99: RgbColor(4, 5, 6)})
                for options in ({"attempts": 0}, {"delay_seconds": -0.1}):
                    with self.subTest(options=options), self.assertRaises(ValueError):
                        lighting.set_static_colors({LightZone.KEYBOARD: RgbColor(1, 2, 3)}, **options)
                with self.assertRaises(ValueError):
                    lighting.set_static_colors({})
            self.assertEqual(device.read_bytes(), b"")

    def test_machine_identity_is_required_before_opening_hid(self):
        with (
            patch("medion_fan_control.lighting.read_machine_identity", side_effect=UnsupportedHardwareError("wrong machine")),
            patch("medion_fan_control.lighting.HidLighting") as transport,
        ):
            with self.assertRaises(UnsupportedHardwareError):
                apply_static_lighting({LightZone.KEYBOARD: RgbColor(1, 2, 3)})
            transport.assert_not_called()

    def test_supported_machine_applies_colors_and_closes_hid(self):
        colors = {LightZone.KEYBOARD: RgbColor(1, 2, 3)}
        with (
            patch("medion_fan_control.lighting.read_machine_identity", return_value=SUPPORTED_IDENTITY),
            patch("medion_fan_control.lighting.HidLighting") as transport,
        ):
            apply_static_lighting(colors)
            transport.return_value.__enter__.return_value.set_static_colors.assert_called_once_with(colors)
            transport.return_value.__exit__.assert_called_once()


if __name__ == "__main__":
    unittest.main()