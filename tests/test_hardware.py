import tempfile
import unittest
from pathlib import Path

from medion_fan_control.hardware import (
    DevPortIO,
    MachineIdentity,
    SUPPORTED_IDENTITY,
    UnsupportedHardwareError,
    read_ac0_online,
    read_machine_identity,
    require_supported_machine,
)


class HardwareTests(unittest.TestCase):
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