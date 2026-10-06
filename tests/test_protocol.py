import unittest
from unittest.mock import Mock

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

    def test_turbo_without_a_power_provider_fails_closed(self):
        port = FakePortIO()
        protocol = EcProtocol(port, sleep=lambda _delay: None)
        with self.assertRaises(InsufficientPowerError):
            protocol.set_profile(Profile.TURBO)
        self.assertEqual(port.operations, [])

    def test_turbo_rechecks_power_before_every_write_attempt(self):
        power = Mock(side_effect=[True, False])
        protocol, port, _ = self.make_protocol([1], turbo_power_available=power)
        with self.assertRaises(InsufficientPowerError):
            protocol.set_profile(Profile.TURBO)
        self.assertEqual(power.call_count, 2)
        self.assertEqual(
            [operation for operation in port.operations if operation == ("write", 0x68, 0x03)],
            [("write", 0x68, 0x03)],
        )

    def test_power_status_failure_prevents_turbo_writes(self):
        power = Mock(side_effect=OSError("AC status unavailable"))
        protocol, port, _ = self.make_protocol(turbo_power_available=power)
        with self.assertRaisesRegex(OSError, "AC status unavailable"):
            protocol.set_profile(Profile.TURBO)
        self.assertEqual(port.operations, [])

    def test_profile_read_retries_invalid_values(self):
        protocol, port, _ = self.make_protocol([0, 255, 2], retries=3)
        self.assertEqual(protocol.read_profile(), Profile.GAMING)
        self.assertEqual(port.operations.count(("read", 0x68)), 3)

    def test_profile_write_stops_after_the_retry_limit(self):
        protocol, port, _ = self.make_protocol([1, 1], retries=2)
        with self.assertRaisesRegex(ProtocolError, "after 2 attempts"):
            protocol.set_profile(Profile.GAMING)
        self.assertEqual(port.operations.count(("write", 0x68, 0x02)), 2)

    def test_invalid_profile_never_reaches_the_transport(self):
        protocol, port, _ = self.make_protocol()
        with self.assertRaises(ValueError):
            protocol.set_profile(4)
        self.assertEqual(port.operations, [])

    def test_full_speed_disable_is_repeated_and_verified(self):
        protocol, port, sleeps = self.make_protocol([2])
        self.assertFalse(protocol.set_full_speed(False))
        self.assertEqual(port.operations.count(("write", 0x68, 0x0F)), 2)
        self.assertEqual(sleeps, [0.010] * 6)

    def test_full_speed_readback_must_match_the_requested_state(self):
        protocol, _, _ = self.make_protocol([2])
        with self.assertRaisesRegex(ProtocolError, "disabled"):
            protocol.set_full_speed(True)

    def test_read_status_uses_verified_selectors_and_reports_live_power(self):
        protocol, port, _ = self.make_protocol(
            [2, 1, 0x34, 0x12, 0x78, 0x56, 61, 52],
            turbo_power_available=lambda: False,
        )
        status = protocol.read_status()
        self.assertEqual(status.profile, Profile.GAMING)
        self.assertTrue(status.full_speed)
        self.assertEqual(status.cpu_fan_rpm, 0x1234)
        self.assertEqual(status.gpu_fan_rpm, 0x5678)
        self.assertEqual((status.cpu_temperature_c, status.gpu_temperature_c), (61, 52))
        self.assertFalse(status.turbo_available)
        indices = [operation[2] for operation in port.operations if operation[:2] == ("write", 0x68)]
        self.assertEqual(indices, [0x11, 0x10, 0x18, 0x19, 0x16, 0x17, 0x20, 0x23])

    def test_bounded_reads_accept_endpoints_and_reject_out_of_range_values(self):
        for method, valid_values, invalid_values in (
            ("read_profile", (1, 3), (0, 4)),
            ("read_full_speed", (1, 2), (0, 3)),
            ("read_power_state", (0, 2), (3, 255)),
            ("read_cpu_temperature", (10, 200), (9, 201)),
            ("read_gpu_temperature", (10, 200), (9, 201)),
        ):
            for value in valid_values + invalid_values:
                with self.subTest(method=method, value=value):
                    protocol, port, _ = self.make_protocol([value], retries=1)
                    if value in invalid_values:
                        with self.assertRaises(ProtocolError):
                            getattr(protocol, method)()
                    else:
                        getattr(protocol, method)()
                    self.assertEqual(port.operations.count(("read", 0x68)), 1)

    def test_invalid_retry_and_delay_options_fail_before_io(self):
        for options in ({"retries": 0}, {"io_delay": -0.1}, {"profile_settle_delay": -0.1}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                EcProtocol(FakePortIO(), **options)


if __name__ == "__main__":
    unittest.main()