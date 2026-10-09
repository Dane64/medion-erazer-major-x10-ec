import tempfile
import unittest
from pathlib import Path

from medion_fan_control.cpu import CpuBackend, CpuError
from medion_fan_control.sysfs import Sysfs

from tests.fakesys import build_cpu, read, write


class CpuBackendTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        build_cpu(self.root)
        self.cpu = CpuBackend(Sysfs(self.root))

    def test_reports_hybrid_clusters_rapl_and_undervolt(self):
        state = self.cpu.get()
        self.assertEqual([c["name"] for c in state["clusters"]], ["P-cores", "E-cores"])
        self.assertEqual(state["clusters"][0]["hw_max_khz"], 4700000)
        self.assertEqual(state["rapl"]["pl1"], {"watts": 45.0, "max_watts": 140, "window_s": 28.0})
        self.assertEqual(state["rapl"]["zones"], ["intel-rapl:0", "intel-rapl-mmio:0"])
        self.assertTrue(state["undervolt"]["available"])
        self.assertFalse(state["pstate"]["no_turbo"])

    def test_applies_validated_settings_to_every_policy_and_rapl_zone(self):
        self.cpu.set({
            "no_turbo": True, "epp": "power", "governor": "performance",
            "clusters": [{"name": "E-cores", "min_khz": 800000, "max_khz": 2000000}],
            "rapl": {"pl1": {"watts": 35, "window_s": 16}, "pl2": {"watts": 90}},
        })
        sys_cpu = "sys/devices/system/cpu"
        self.assertEqual(read(self.root, f"{sys_cpu}/intel_pstate/no_turbo"), "1")
        for index in range(4):
            self.assertEqual(read(self.root, f"{sys_cpu}/cpufreq/policy{index}/energy_performance_preference"), "power")
        self.assertEqual(read(self.root, f"{sys_cpu}/cpufreq/policy3/scaling_max_freq"), "2000000")
        self.assertEqual(read(self.root, f"{sys_cpu}/cpufreq/policy3/scaling_min_freq"), "800000")
        self.assertEqual(read(self.root, f"{sys_cpu}/cpufreq/policy0/scaling_max_freq"), "4700000")
        for zone in ("intel-rapl:0", "intel-rapl-mmio:0"):
            self.assertEqual(read(self.root, f"sys/class/powercap/{zone}/constraint_0_power_limit_uw"), "35000000")
            self.assertEqual(read(self.root, f"sys/class/powercap/{zone}/constraint_1_power_limit_uw"), "90000000")
            self.assertEqual(read(self.root, f"sys/class/powercap/{zone}/constraint_0_time_window_us"), "16000000")

    def test_invalid_requests_write_nothing(self):
        before = read(self.root, "sys/devices/system/cpu/intel_pstate/no_turbo")
        for params in (
            {"no_turbo": True, "epp": "ludicrous"},
            {"no_turbo": True, "clusters": [{"name": "P-cores", "min_khz": 5000000}]},
            {"no_turbo": True, "clusters": [{"name": "P-cores", "min_khz": 3000000, "max_khz": 1000000}]},
            {"no_turbo": True, "rapl": {"pl1": {"watts": 500}}},
            {"no_turbo": True, "rapl": {"pl1": {"watts": 100}, "pl2": {"watts": 50}}},
            {"no_turbo": True, "undervolt": {"core": 25}},
            {"no_turbo": True, "undervolt": {"core": -200}},
            {"no_turbo": True, "undervolt": {"vcore": -10}},
            {"no_turbo": True, "min_perf_pct": 0},
        ):
            with self.subTest(params=params), self.assertRaises(CpuError):
                self.cpu.set(params)
        self.assertEqual(read(self.root, "sys/devices/system/cpu/intel_pstate/no_turbo"), before)

    def test_writes_undervolt_within_module_limits(self):
        self.cpu.set({"undervolt": {"core": -80, "cache": -80}})
        self.assertEqual(read(self.root, "sys/class/misc/x10-ec/undervolt_core"), "-80")
        self.assertEqual(self.cpu.get()["undervolt"]["planes"]["cache"], -80)

    def test_undervolt_unavailable_without_module_or_when_disabled(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        build_cpu(root, undervolt=False)
        state = CpuBackend(Sysfs(root)).get()["undervolt"]
        self.assertFalse(state["available"])
        self.assertIn("not loaded", state["reason"])
        write(root, "sys/class/misc/x10-ec/undervolt_enabled", 0)
        state = CpuBackend(Sysfs(root)).get()["undervolt"]
        self.assertIn("--enable-undervolt", state["reason"])
        with self.assertRaises(CpuError):
            CpuBackend(Sysfs(root)).set({"undervolt": {"core": -10}})


if __name__ == "__main__":
    unittest.main()
