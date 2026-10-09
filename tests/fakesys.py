"""Build fake /sys trees for backend tests."""
from pathlib import Path


def write(root: Path, relative: str, value) -> Path:
    path = root / relative.lstrip("/")
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, bytes):
        path.write_bytes(value)
    else:
        path.write_text(f"{value}\n", encoding="utf-8")
    return path


def read(root: Path, relative: str) -> str:
    return (root / relative.lstrip("/")).read_text(encoding="utf-8").strip()


def build_cpu(root: Path, *, undervolt: bool = True) -> None:
    pstate = "sys/devices/system/cpu/intel_pstate"
    for name, value in (("status", "active"), ("no_turbo", 0), ("min_perf_pct", 9),
                        ("max_perf_pct", 100), ("hwp_dynamic_boost", 0)):
        write(root, f"{pstate}/{name}", value)
    for index in range(4):
        policy = f"sys/devices/system/cpu/cpufreq/policy{index}"
        p_core = index < 2
        write(root, f"{policy}/cpuinfo_min_freq", 400000)
        write(root, f"{policy}/cpuinfo_max_freq", 4700000 if p_core else 3500000)
        write(root, f"{policy}/scaling_min_freq", 400000)
        write(root, f"{policy}/scaling_max_freq", 4700000 if p_core else 3500000)
        write(root, f"{policy}/scaling_cur_freq", 1200000)
        write(root, f"{policy}/scaling_governor", "powersave")
        write(root, f"{policy}/scaling_available_governors", "performance powersave")
        write(root, f"{policy}/energy_performance_preference", "balance_performance")
        write(root, f"{policy}/energy_performance_available_preferences",
              "default performance balance_performance balance_power power")
    for zone in ("intel-rapl:0", "intel-rapl-mmio:0"):
        base = f"sys/class/powercap/{zone}"
        write(root, f"{base}/name", "package-0")
        write(root, f"{base}/enabled", 1)
        write(root, f"{base}/constraint_0_name", "long_term")
        write(root, f"{base}/constraint_0_power_limit_uw", 45000000)
        write(root, f"{base}/constraint_0_time_window_us", 28000000)
        write(root, f"{base}/constraint_0_max_power_uw", 0)
        write(root, f"{base}/constraint_1_name", "short_term")
        write(root, f"{base}/constraint_1_power_limit_uw", 115000000)
        write(root, f"{base}/constraint_1_time_window_us", 2440)
    if undervolt:
        misc = "sys/class/misc/x10-ec"
        write(root, f"{misc}/undervolt_enabled", 1)
        write(root, f"{misc}/undervolt_limits", "-150 0")
        for plane in ("core", "cache", "gpu", "uncore", "analogio"):
            write(root, f"{misc}/undervolt_{plane}", 0)


def build_gpus(root: Path) -> None:
    pci = root / "sys/devices/pci0000:00"
    igpu = pci / "0000:00:02.0"
    dgpu = pci / "0000:00:01.0/0000:01:00.0/0000:02:01.0/0000:03:00.0"
    drivers = root / "sys/bus/pci/drivers/i915"
    drivers.mkdir(parents=True, exist_ok=True)
    for device, device_id in ((igpu, "0x46a6"), (dgpu, "0x5691")):
        device.mkdir(parents=True, exist_ok=True)
        write(root, str(device.relative_to(root) / "vendor"), "0x8086")
        write(root, str(device.relative_to(root) / "device"), device_id)
        write(root, str(device.relative_to(root) / "class"), "0x030000")
        write(root, str(device.relative_to(root) / "power/runtime_status"), "active")
        (device / "driver").symlink_to(drivers)
        bus = root / "sys/bus/pci/devices"
        bus.mkdir(parents=True, exist_ok=True)
        (bus / device.name).symlink_to(device)
    drm = root / "sys/class/drm"
    drm.mkdir(parents=True, exist_ok=True)
    card0 = root / "sys/devices/drm/card0"
    card0.mkdir(parents=True)
    (card0 / "device").symlink_to(igpu)
    (drm / "card0").symlink_to(card0)
    for name, value in (("min", 100), ("max", 1400), ("boost", 1400), ("RP0", 1400), ("RPn", 100),
                        ("cur", 300), ("act", 300)):
        write(root, f"sys/devices/drm/card0/gt_{name}_freq_mhz", value)
    card1 = root / "sys/devices/drm/card1"
    card1.mkdir(parents=True)
    (card1 / "device").symlink_to(dgpu)
    (drm / "card1").symlink_to(card1)
    xe = str(dgpu.relative_to(root)) + "/tile0/gt0/freq0"
    for name, value in (("min_freq", 300), ("max_freq", 2050), ("rp0_freq", 2050), ("rpn_freq", 300),
                        ("cur_freq", 0), ("act_freq", 0)):
        write(root, f"{xe}/{name}", value)
    hwmon = str(dgpu.relative_to(root)) + "/hwmon/hwmon5"
    write(root, f"{hwmon}/power1_max", 80000000)
    write(root, f"{hwmon}/power1_rated_max", 120000000)


def make_edid(pixel_clock_10khz: int = 64500, h_active=2560, h_blank=160, v_active=1440, v_blank=41) -> bytes:
    edid = bytearray(128)
    edid[:8] = bytes.fromhex("00ffffffffffff00")
    dtd = bytearray(18)
    dtd[0] = pixel_clock_10khz & 0xFF
    dtd[1] = pixel_clock_10khz >> 8
    dtd[2] = h_active & 0xFF
    dtd[3] = h_blank & 0xFF
    dtd[4] = ((h_active >> 8) << 4) | (h_blank >> 8)
    dtd[5] = v_active & 0xFF
    dtd[6] = v_blank & 0xFF
    dtd[7] = ((v_active >> 8) << 4) | (v_blank >> 8)
    edid[54:72] = dtd
    edid[72:75] = b"\x00\x00\x00"  # descriptor, not a timing
    edid[127] = (-sum(edid[:127])) & 0xFF
    return bytes(edid)
