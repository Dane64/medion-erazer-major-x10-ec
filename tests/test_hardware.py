import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from medion_fan_control.hardware import (
    DevPortIO,
    HardwareAccessError,
    MachineIdentity,
    PlatformSecurityError,
    PlatformSecurityStatus,
    SECURE_BOOT_EFIVAR,
    SUPPORTED_IDENTITY,
    UnsupportedHardwareError,
    read_ac0_online,
    read_machine_identity,
    read_platform_security,
    open_supported_protocol,
    require_ec_writes_unlocked,
    require_supported_machine,
)


class HardwareTests(unittest.TestCase):
    def test_detects_secure_boot_and_kernel_lockdown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            efivars = root / "efivars"
            efivars.mkdir()
            secure_boot = efivars / SECURE_BOOT_EFIVAR
            lockdown = root / "lockdown"

            secure_boot.write_bytes(b"\x07\x00\x00\x00\x01")
            lockdown.write_text("none [integrity] confidentiality\n", encoding="ascii")

            status = read_platform_security(efivars, lockdown)

            self.assertEqual(status, PlatformSecurityStatus(True, "integrity"))
            self.assertTrue(status.blocks_ec_access)

    def test_allows_ec_access_when_secure_boot_and_lockdown_are_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            efivars = root / "efivars"
            efivars.mkdir()
            (efivars / SECURE_BOOT_EFIVAR).write_bytes(b"\x07\x00\x00\x00\x00")
            lockdown = root / "lockdown"
            lockdown.write_text("[none] integrity confidentiality\n", encoding="ascii")

            status = read_platform_security(efivars, lockdown)

            self.assertEqual(status, PlatformSecurityStatus(False, "none"))
            self.assertFalse(status.blocks_ec_access)

    def test_treats_missing_efi_and_lockdown_interfaces_as_inactive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            status = read_platform_security(root / "missing-efivars", root / "missing-lockdown")

            self.assertEqual(status, PlatformSecurityStatus(None, None))
            self.assertFalse(status.blocks_ec_access)

    def test_rejects_ec_access_when_platform_security_is_active(self):
        status = PlatformSecurityStatus(secure_boot_enabled=True, lockdown_mode="integrity")

        with patch("medion_fan_control.hardware.read_platform_security", return_value=status):
            with self.assertRaisesRegex(PlatformSecurityError, "EC access is blocked"):
                require_ec_writes_unlocked()

    def test_accepts_ec_access_when_platform_security_is_disabled(self):
        status = PlatformSecurityStatus(secure_boot_enabled=False, lockdown_mode="none")

        with patch("medion_fan_control.hardware.read_platform_security", return_value=status):
            self.assertEqual(require_ec_writes_unlocked(), status)

    def test_reads_ac0_online_state(self):
        with tempfile.TemporaryDirectory() as directory:
            ac_root = Path(directory) / "AC0"
            ac_root.mkdir()
            (ac_root / "type").write_text("Mains\n", encoding="ascii")
            (ac_root / "online").write_text("1\n", encoding="ascii")

            self.assertTrue(read_ac0_online(Path(directory)))

            (ac_root / "online").write_text("0\n", encoding="ascii")
            self.assertFalse(read_ac0_online(Path(directory)))

    def test_reads_and_accepts_supported_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            field_names = {
                "system_vendor": "sys_vendor",
                "product_name": "product_name",
                "product_sku": "product_sku",
                "board_name": "board_name",
                "board_version": "board_version",
                "bios_version": "bios_version",
                "ec_firmware_release": "ec_firmware_release",
            }
            for field, filename in field_names.items():
                (root / filename).write_text(getattr(SUPPORTED_IDENTITY, field) + "\n", encoding="ascii")

            identity = read_machine_identity(root)
            self.assertEqual(identity, SUPPORTED_IDENTITY)
            require_supported_machine(identity)

    def test_rejects_unverified_bios(self):
        identity = MachineIdentity(
            **{**SUPPORTED_IDENTITY.__dict__, "bios_version": "UNVERIFIED"},
        )

        with self.assertRaisesRegex(UnsupportedHardwareError, "bios_version"):
            require_supported_machine(identity)

    def test_dev_port_reads_and_writes_exact_offsets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            port_path = root / "port"
            port_path.write_bytes(bytes(0x100))

            with DevPortIO(port_path=port_path, lock_path=root / "lock") as port_io:
                port_io.write_byte(0x68, 0xAB)
                self.assertEqual(port_io.read_byte(0x68), 0xAB)

    def test_rejects_every_unverified_identity_field(self):
        for field in MachineIdentity.__dataclass_fields__:
            with self.subTest(field=field):
                with self.assertRaisesRegex(UnsupportedHardwareError, field):
                    require_supported_machine(replace(SUPPORTED_IDENTITY, **{field: "UNVERIFIED"}))

    def test_missing_dmi_field_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(UnsupportedHardwareError, "sys_vendor"):
                read_machine_identity(Path(directory))

    def test_rejects_malformed_secure_boot_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for value in (b"", b"\0" * 4, b"\0\0\0\0\2"):
                with self.subTest(value=value):
                    (root / SECURE_BOOT_EFIVAR).write_bytes(value)
                    with self.assertRaisesRegex(HardwareAccessError, "invalid value"):
                        read_platform_security(root, root / "missing-lockdown")

    def test_rejects_malformed_lockdown_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for value in ("", "none integrity confidentiality", "[unknown]", "[none] [integrity]"):
                with self.subTest(value=value):
                    (root / "lockdown").write_text(value, encoding="ascii")
                    with self.assertRaisesRegex(HardwareAccessError, "invalid kernel lockdown"):
                        read_platform_security(root, root / "lockdown")

    def test_ac_state_errors_do_not_default_to_available(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(HardwareAccessError, "AC0"):
                read_ac0_online(root)
            ac = root / "AC0"
            ac.mkdir()
            for supply_type, online in (("USB", "1"), ("Mains", "yes"), ("Mains", "2")):
                with self.subTest(supply_type=supply_type, online=online):
                    (ac / "type").write_text(supply_type, encoding="ascii")
                    (ac / "online").write_text(online, encoding="ascii")
                    with self.assertRaises(HardwareAccessError):
                        read_ac0_online(root)

    def test_platform_guard_runs_before_identity_and_device_access(self):
        with (
            patch("medion_fan_control.hardware.read_platform_security", return_value=PlatformSecurityStatus(True, "integrity")),
            patch("medion_fan_control.hardware.read_machine_identity") as identity,
            patch("medion_fan_control.hardware.DevPortIO") as device,
        ):
            with self.assertRaises(PlatformSecurityError), open_supported_protocol():
                self.fail("blocked hardware session opened")
            identity.assert_not_called()
            device.assert_not_called()

    def test_identity_guard_runs_before_device_access(self):
        with (
            patch("medion_fan_control.hardware.require_ec_writes_unlocked"),
            patch("medion_fan_control.hardware.read_machine_identity", return_value=replace(SUPPORTED_IDENTITY, bios_version="other")),
            patch("medion_fan_control.hardware.DevPortIO") as device,
        ):
            with self.assertRaises(UnsupportedHardwareError), open_supported_protocol():
                self.fail("unsupported hardware session opened")
            device.assert_not_called()

    def test_supported_session_wires_live_ac_check_and_closes_transport(self):
        with (
            patch("medion_fan_control.hardware.require_ec_writes_unlocked"),
            patch("medion_fan_control.hardware.read_machine_identity", return_value=SUPPORTED_IDENTITY),
            patch("medion_fan_control.hardware.DevPortIO") as device,
            patch("medion_fan_control.hardware.read_ac0_online", side_effect=[True, False]) as power,
        ):
            with open_supported_protocol() as protocol:
                self.assertTrue(protocol.is_turbo_available())
                self.assertFalse(protocol.is_turbo_available())
            self.assertEqual(power.call_count, 2)
            device.return_value.__exit__.assert_called_once()

    def test_lock_contention_and_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            port = root / "port"
            port.write_bytes(bytes(0x100))
            first = DevPortIO(port_path=port, lock_path=root / "lock")
            second = DevPortIO(port_path=port, lock_path=root / "lock")
            with first:
                with self.assertRaisesRegex(HardwareAccessError, "another fan-control process"):
                    second.__enter__()
                first.write_byte(0x68, 0x42)
            with second:
                self.assertEqual(second.read_byte(0x68), 0x42)
            first.close()
            second.close()

    def test_failed_open_releases_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            device = DevPortIO(port_path=root / "port", lock_path=root / "lock")
            with self.assertRaises(HardwareAccessError):
                device.__enter__()
            (root / "port").write_bytes(bytes(0x100))
            with DevPortIO(port_path=root / "port", lock_path=root / "lock"):
                pass
            self.assertIsNone(device._lock_fd)
            self.assertIsNone(device._port_fd)

    def test_short_io_invalid_values_and_closed_transport_fail_explicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "port").write_bytes(b"")
            with DevPortIO(port_path=root / "port", lock_path=root / "lock") as device:
                with self.assertRaisesRegex(HardwareAccessError, "short read"):
                    device.read_byte(0x68)
                with patch("medion_fan_control.hardware.os.pwrite", return_value=0):
                    with self.assertRaisesRegex(HardwareAccessError, "short write"):
                        device.write_byte(0x68, 0x12)
                for value in (-1, 256):
                    with self.subTest(value=value), self.assertRaises(ValueError):
                        device.write_byte(0x68, value)
                with self.assertRaisesRegex(HardwareAccessError, "already open"):
                    device.__enter__()
            with self.assertRaisesRegex(HardwareAccessError, "not open"):
                device.read_byte(0x68)
            with self.assertRaisesRegex(HardwareAccessError, "not open"):
                device.write_byte(0x68, 0x12)


if __name__ == "__main__":
    unittest.main()