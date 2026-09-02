import unittest

from medion_fan_control.gui import DemoController, TelemetryHistory, format_duration
from medion_fan_control.protocol import FanStatus, Profile


def make_status(cpu_rpm):
    return FanStatus(
        profile=Profile.OFFICE,
        full_speed=False,
        cpu_fan_rpm=cpu_rpm,
        gpu_fan_rpm=cpu_rpm - 100,
        cpu_temperature_c=60,
        gpu_temperature_c=55,
    )


class TelemetryHistoryTests(unittest.TestCase):
    def test_keeps_only_latest_samples(self):
        history = TelemetryHistory(max_samples=2)

        history.append(make_status(1000), recorded_at=1.0)
        history.append(make_status(2000), recorded_at=2.0)
        history.append(make_status(3000), recorded_at=3.0)

        self.assertEqual([sample.status.cpu_fan_rpm for sample in history.samples], [2000, 3000])
        self.assertEqual([sample.recorded_at for sample in history.samples], [2.0, 3.0])

    def test_filters_samples_to_selected_time_window(self):
        history = TelemetryHistory()
        history.append(make_status(1000), recorded_at=10.0)
        history.append(make_status(2000), recorded_at=40.0)
        history.append(make_status(3000), recorded_at=70.0)

        samples = history.samples_for_window(30, now=70.0)

        self.assertEqual([sample.status.cpu_fan_rpm for sample in samples], [2000, 3000])

    def test_formats_graph_window_duration(self):
        self.assertEqual(format_duration(30), "30s")
        self.assertEqual(format_duration(90), "1m 30s")
        self.assertEqual(format_duration(600), "10 min")


class DemoControllerTests(unittest.TestCase):
    def test_applies_profile_and_full_speed_state(self):
        controller = DemoController()

        self.assertEqual(controller.set_profile(Profile.TURBO), Profile.TURBO)
        self.assertTrue(controller.set_full_speed(True))
        status = controller.read_status()

        self.assertEqual(status.profile, Profile.TURBO)
        self.assertTrue(status.full_speed)
        self.assertEqual(status.cpu_fan_rpm, 5900)
        self.assertEqual(status.gpu_fan_rpm, 5600)


if __name__ == "__main__":
    unittest.main()