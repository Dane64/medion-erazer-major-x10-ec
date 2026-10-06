import unittest
from dataclasses import replace
from unittest.mock import patch

from medion_fan_control.controller import DemoController
from medion_fan_control.telemetry import TelemetryHistory, format_duration


class TelemetryHistoryTests(unittest.TestCase):
    def setUp(self):
        self.status = DemoController().read_status()

    def test_keeps_only_latest_samples(self):
        history = TelemetryHistory(max_samples=2)
        for index in range(1, 4):
            history.append(replace(self.status, cpu_fan_rpm=index * 1000), recorded_at=float(index))
        self.assertEqual([sample.status.cpu_fan_rpm for sample in history.samples], [2000, 3000])
        self.assertEqual([sample.recorded_at for sample in history.samples], [2.0, 3.0])

    def test_filters_samples_to_selected_time_window_including_boundary(self):
        history = TelemetryHistory()
        for recorded_at in (10.0, 40.0, 70.0):
            history.append(self.status, recorded_at=recorded_at)
        samples = history.samples_for_window(30, now=70.0)
        self.assertEqual([sample.recorded_at for sample in samples], [40.0, 70.0])
        self.assertEqual(history.samples_for_window(30, now=101.0), ())

    def test_uses_monotonic_time_by_default(self):
        history = TelemetryHistory()
        with patch("medion_fan_control.telemetry.time.monotonic", return_value=123.0):
            history.append(self.status)
            self.assertEqual(history.samples[0].recorded_at, 123.0)
            self.assertEqual(history.samples_for_window(1), history.samples)

    def test_rejects_invalid_capacity_and_window(self):
        for value in (0, -1):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    TelemetryHistory(max_samples=value)
                with self.assertRaises(ValueError):
                    TelemetryHistory().samples_for_window(value)

    def test_formats_graph_window_duration(self):
        self.assertEqual(format_duration(30), "30s")
        self.assertEqual(format_duration(90), "1m 30s")
        self.assertEqual(format_duration(600), "10 min")


if __name__ == "__main__":
    unittest.main()
