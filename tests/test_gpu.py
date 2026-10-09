import subprocess
import tempfile
import unittest
from pathlib import Path

from medion_fan_control.gpu import ArcDgpuCtlConfig, GpuBackend, GpuError
from medion_fan_control.sysfs import Sysfs

from tests.fakesys import build_gpus, read


class GpuBackendTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        build_gpus(self.root)
        self.calls = []

        def runner(argv, timeout):
            self.calls.append((argv, timeout))
            return subprocess.CompletedProcess(argv, 0, "", "")

        self.gpu = GpuBackend(Sysfs(self.root), ArcDgpuCtlConfig(), runner, which=lambda name: f"/usr/bin/{name}")

    def test_discovers_i915_igpu_and_xe_dgpu(self):
        state = self.gpu.get()
        igpu, dgpu = state["gpus"]
        self.assertFalse(igpu["discrete"])
        self.assertEqual(igpu["freq"]["rp0"], 1400)
        self.assertTrue(dgpu["discrete"])
        self.assertEqual(dgpu["name"], "Arc A730M")
        self.assertEqual(dgpu["freq"]["rp0"], 2050)
        self.assertEqual(dgpu["power"], {"limit_w": 80.0, "rated_w": 120.0})
        self.assertEqual(state["dgpu"]["state"], "active")

    def test_sets_frequency_and_power_within_bounds(self):
        self.gpu.set({"id": "card1", "min_mhz": 600, "max_mhz": 1800, "power_limit_w": 60})
        xe = "sys/devices/pci0000:00/0000:00:01.0/0000:01:00.0/0000:02:01.0/0000:03:00.0"
        self.assertEqual(read(self.root, f"{xe}/tile0/gt0/freq0/min_freq"), "600")
        self.assertEqual(read(self.root, f"{xe}/tile0/gt0/freq0/max_freq"), "1800")
        self.assertEqual(read(self.root, f"{xe}/hwmon/hwmon5/power1_max"), "60000000")
        self.gpu.set({"id": "card0", "max_mhz": 1000, "boost_mhz": 1100})
        self.assertEqual(read(self.root, "sys/devices/drm/card0/gt_boost_freq_mhz"), "1100")

    def test_rejects_overclock_and_bad_ids(self):
        for params in ({"id": "card1", "max_mhz": 2400}, {"id": "card1", "min_mhz": 2000, "max_mhz": 1000},
                       {"id": "card1", "power_limit_w": 200}, {"id": "../x"}, {"id": "card7", "max_mhz": 1000},
                       {"id": "card1", "boost_mhz": 1000}):
            with self.subTest(params=params), self.assertRaises(GpuError):
                self.gpu.set(params)

    def test_dgpu_ctl_invocation_and_failure(self):
        self.gpu.set_dgpu_power(False)
        self.assertEqual(self.calls[-1][0], ["/usr/bin/arc-dgpu-ctl", "off"])
        failing = GpuBackend(Sysfs(self.root), ArcDgpuCtlConfig(),
                             lambda argv, t: subprocess.CompletedProcess(argv, 3, "", "device busy\n"),
                             which=lambda name: "/usr/bin/arc-dgpu-ctl")
        with self.assertRaisesRegex(GpuError, "device busy"):
            failing.set_dgpu_power(True)
        missing = GpuBackend(Sysfs(self.root), ArcDgpuCtlConfig(), which=lambda name: None)
        with self.assertRaisesRegex(GpuError, "not found"):
            missing.set_dgpu_power(True)


if __name__ == "__main__":
    unittest.main()
