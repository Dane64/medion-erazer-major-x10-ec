import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from medion_fan_control.hardware import (
    DevPortIO,
    MachineIdentity,
    PlatformSecurityError,
    PlatformSecurityStatus,
    SECURE_BOOT_EFIVAR,
    SUPPORTED_IDENTITY,
    UnsupportedHardwareError,
    read_ac0_online,
    read_machine_identity,
    read_platform_security,
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
            with self.assertRaisesRegex(PlatformSecurityError, "disable Secure Boot"):
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


if __name__ == "__main__":
    unittest.main()