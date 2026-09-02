import unittest

from medion_fan_control.protocol import EcProtocol, InsufficientPowerError, Profile, ProtocolError


class FakePortIO:
    def __init__(self, reads=()):
        self.reads = list(reads)
        self.operations = []

    def read_byte(self, port):
        self.operations.append(("read", port))
        if not self.reads:
            raise AssertionError("unexpected port read")
        return self.reads.pop(0)

    def write_byte(self, port, value):
        self.operations.append(("write", port, value))


class EcProtocolTests(unittest.TestCase):
    def make_protocol(self, reads=(), retries=5, turbo_power_available=lambda: True):
        port_io = FakePortIO(reads)
        sleeps = []
        protocol = EcProtocol(
            port_io,
            turbo_power_available=turbo_power_available,
            sleep=sleeps.append,
            retries=retries,
        )
        return protocol, port_io, sleeps

    def test_reads_profile_with_vendor_command_and_index(self):
        protocol, port_io, sleeps = self.make_protocol([2])

        self.assertEqual(protocol.read_profile(), Profile.GAMING)
        self.assertEqual(
            port_io.operations,
            [("write", 0x6C, 0xDE), ("write", 0x68, 0x11), ("read", 0x68)],
        )
        self.assertEqual(sleeps, [0.010, 0.010])

    def test_profile_write_retries_until_readback_matches(self):
        protocol, port_io, sleeps = self.make_protocol([1, 3])

        self.assertEqual(protocol.set_profile(Profile.TURBO), Profile.TURBO)
        self.assertEqual(
            port_io.operations,
            [
                ("write", 0x6C, 0xDE),
                ("write", 0x68, 0x03),
                ("write", 0x6C, 0xDE),
                ("write", 0x68, 0x11),
                ("read", 0x68),
                ("write", 0x6C, 0xDE),
                ("write", 0x68, 0x03),
                ("write", 0x6C, 0xDE),
                ("write", 0x68, 0x11),
                ("read", 0x68),
            ],
        )
        self.assertEqual(sleeps, [0.010, 0.030, 0.010, 0.010] * 2)

    def test_turbo_is_rejected_before_profile_write_when_power_is_insufficient(self):
        protocol, port_io, _ = self.make_protocol(turbo_power_available=lambda: False)

        with self.assertRaisesRegex(InsufficientPowerError, "AC barrel adapter"):
            protocol.set_profile(Profile.TURBO)

        self.assertEqual(port_io.operations, [])

    def test_reports_turbo_available_for_sufficient_power(self):
        protocol, _, _ = self.make_protocol(turbo_power_available=lambda: True)

        self.assertTrue(protocol.is_turbo_available())

    def test_full_speed_enable_is_repeated_and_verified(self):
        protocol, port_io, _ = self.make_protocol([1])

        self.assertTrue(protocol.set_full_speed(True))
        self.assertEqual(
            port_io.operations,
            [
                ("write", 0x6C, 0xDE),
                ("write", 0x68, 0x0E),
                ("write", 0x6C, 0xDE),
                ("write", 0x68, 0x0E),
                ("write", 0x6C, 0xDE),
                ("write", 0x68, 0x10),
                ("read", 0x68),
            ],
        )

    def test_reads_fan_values_as_little_endian_rpm(self):
        protocol, port_io, _ = self.make_protocol([0x34, 0x12, 0x78, 0x56])

        self.assertEqual(protocol.read_gpu_fan_rpm(), 0x1234)
        self.assertEqual(protocol.read_cpu_fan_rpm(), 0x5678)
        indices = [operation[2] for operation in port_io.operations if operation[:2] == ("write", 0x68)]
        self.assertEqual(indices, [0x16, 0x17, 0x18, 0x19])

    def test_invalid_temperature_is_bounded(self):
        protocol, _, _ = self.make_protocol([0, 255], retries=2)

        with self.assertRaisesRegex(ProtocolError, "after 2 attempts"):
            protocol.read_cpu_temperature()


if __name__ == "__main__":
    unittest.main()