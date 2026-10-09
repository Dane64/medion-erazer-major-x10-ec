"""CPU controls: intel_pstate, cpufreq clusters, RAPL power limits and
voltage offsets exposed by the signed x10_ec module."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from .sysfs import Sysfs, SysfsError

PSTATE = "/sys/devices/system/cpu/intel_pstate"
CPUFREQ = "/sys/devices/system/cpu/cpufreq"
PLATFORM_PROFILE = "/sys/firmware/acpi/platform_profile"
POWERCAP = "/sys/class/powercap"
X10_MISC = "/sys/class/misc/x10-ec"
UNDERVOLT_PLANES = ("core", "cache", "gpu", "uncore", "analogio")
UNDERVOLT_LABELS = {
    "core": "CPU core",
    "cache": "CPU cache / ring",
    "gpu": "Integrated GPU",
    "uncore": "System agent",
    "analogio": "Analog I/O",
}
# Generous absolute ceiling for a 45 W H-class mobile CPU; the firmware's own
# constraint_*_max_power_uw is preferred when it is published.
RAPL_ABSOLUTE_MAX_W = 140


class CpuError(RuntimeError):
    pass


@dataclass(frozen=True)
class Cluster:
    name: str
    policies: tuple[str, ...]
    hw_min_khz: int
    hw_max_khz: int


class CpuBackend:
    def __init__(self, sysfs: Sysfs | None = None) -> None:
        self.sys = sysfs or Sysfs()

    # ------------------------------------------------------------------ read
    def clusters(self) -> list[Cluster]:
        groups: dict[tuple[int, int], list[str]] = defaultdict(list)
        for policy in self.sys.glob(CPUFREQ, "policy*"):
            relative = self.sys.relative(policy)
            hw_min = self.sys.read_int_optional(relative / "cpuinfo_min_freq")
            hw_max = self.sys.read_int_optional(relative / "cpuinfo_max_freq")
            if hw_min is None or hw_max is None:
                continue
            groups[(hw_min, hw_max)].append(str(relative))
        ordered = sorted(groups.items(), key=lambda item: -item[0][1])
        hybrid = len(ordered) > 1
        clusters = []
        for index, ((hw_min, hw_max), policies) in enumerate(ordered):
            if hybrid:
                name = "P-cores" if index == 0 else "E-cores" if index == 1 else f"Cluster {index + 1}"
            else:
                name = "All cores"
            clusters.append(Cluster(name, tuple(sorted(policies, key=_policy_key)), hw_min, hw_max))
        return clusters

    def _cluster_state(self, cluster: Cluster) -> dict[str, Any]:
        first = cluster.policies[0]
        return {
            "name": cluster.name,
            "cpus": len(cluster.policies),
            "hw_min_khz": cluster.hw_min_khz,
            "hw_max_khz": cluster.hw_max_khz,
            "min_khz": self.sys.read_int_optional(f"{first}/scaling_min_freq"),
            "max_khz": self.sys.read_int_optional(f"{first}/scaling_max_freq"),
            "cur_khz": max(
                (self.sys.read_int_optional(f"{p}/scaling_cur_freq") or 0) for p in cluster.policies
            ),
        }

    def _rapl_zones(self) -> list[str]:
        zones = []
        for prefix in ("intel-rapl:", "intel-rapl-mmio:"):
            for zone in self.sys.glob(POWERCAP, f"{prefix}[0-9]"):
                relative = str(self.sys.relative(zone))
                if (self.sys.read_optional(f"{relative}/name") or "").startswith("package"):
                    zones.append(relative)
        return zones

    def _rapl_state(self) -> dict[str, Any] | None:
        zones = self._rapl_zones()
        if not zones:
            return None
        zone = zones[0]
        state: dict[str, Any] = {"zones": [z.rsplit("/", 1)[-1] for z in zones]}
        for index in range(3):
            name = self.sys.read_optional(f"{zone}/constraint_{index}_name")
            if name not in {"long_term", "short_term"}:
                continue
            key = "pl1" if name == "long_term" else "pl2"
            limit = self.sys.read_int_optional(f"{zone}/constraint_{index}_power_limit_uw")
            maximum = self.sys.read_int_optional(f"{zone}/constraint_{index}_max_power_uw")
            window = self.sys.read_int_optional(f"{zone}/constraint_{index}_time_window_us")
            state[key] = {
                "watts": None if limit is None else round(limit / 1e6, 1),
                "max_watts": _rapl_max_watts(maximum),
                "window_s": None if window is None else round(window / 1e6, 3),
            }
        enabled = self.sys.read_optional(f"{zone}/enabled")
        state["enabled"] = enabled == "1" if enabled is not None else None
        return state

    def _undervolt_state(self) -> dict[str, Any]:
        enabled = self.sys.read_optional(f"{X10_MISC}/undervolt_enabled")
        if enabled is None:
            return {"available": False, "reason": "x10_ec kernel module is not loaded"}
        if enabled != "1":
            return {
                "available": False,
                "reason": "disabled; reinstall with --enable-undervolt (sets allow_undervolt=1)",
            }
        limits = (self.sys.read_optional(f"{X10_MISC}/undervolt_limits") or "-150 0").split()
        planes: dict[str, int | None] = {}
        errors: dict[str, str] = {}
        for plane in UNDERVOLT_PLANES:
            try:
                planes[plane] = self.sys.read_int(f"{X10_MISC}/undervolt_{plane}")
            except SysfsError as error:
                planes[plane] = None
                errors[plane] = str(error)
        state: dict[str, Any] = {
            "available": any(value is not None for value in planes.values()),
            "min_mv": int(limits[0]),
            "max_mv": int(limits[1]) if len(limits) > 1 else 0,
            "planes": planes,
            "labels": UNDERVOLT_LABELS,
        }
        if errors:
            state["errors"] = errors
        if not state["available"]:
            state["reason"] = "the OC mailbox did not respond (BIOS undervolt protection?)"
        return state

    def get(self) -> dict[str, Any]:
        pstate: dict[str, Any] | None = None
        if self.sys.exists(PSTATE):
            pstate = {
                "status": self.sys.read_optional(f"{PSTATE}/status"),
                "no_turbo": self.sys.read_optional(f"{PSTATE}/no_turbo") == "1",
                "min_perf_pct": self.sys.read_int_optional(f"{PSTATE}/min_perf_pct"),
                "max_perf_pct": self.sys.read_int_optional(f"{PSTATE}/max_perf_pct"),
                "dynamic_boost": _bool_or_none(self.sys.read_optional(f"{PSTATE}/hwp_dynamic_boost")),
            }
        clusters = self.clusters()
        first = clusters[0].policies[0] if clusters else None
        governor = epp = None
        governors: list[str] = []
        epps: list[str] = []
        if first:
            governor = self.sys.read_optional(f"{first}/scaling_governor")
            governors = (self.sys.read_optional(f"{first}/scaling_available_governors") or "").split()
            epp = self.sys.read_optional(f"{first}/energy_performance_preference")
            epps = (self.sys.read_optional(f"{first}/energy_performance_available_preferences") or "").split()
        profile = self.sys.read_optional(PLATFORM_PROFILE)
        return {
            "pstate": pstate,
            "governor": governor,
            "governors": governors,
            "epp": epp,
            "epps": epps,
            "platform_profile": profile,
            "platform_profiles": (self.sys.read_optional(f"{PLATFORM_PROFILE}_choices") or "").split(),
            "clusters": [self._cluster_state(cluster) for cluster in clusters],
            "rapl": self._rapl_state(),
            "undervolt": self._undervolt_state(),
        }

    # ----------------------------------------------------------------- write
    def set(self, params: dict[str, Any]) -> dict[str, Any]:
        """Apply a partial update. Every value is validated before any write."""
        plan: list[tuple[str, object]] = []
        current = self.get()
        clusters = {cluster.name: cluster for cluster in self.clusters()}

        if "no_turbo" in params:
            _require(current["pstate"] is not None, "intel_pstate is not active")
            plan.append((f"{PSTATE}/no_turbo", 1 if params["no_turbo"] else 0))
        for key in ("min_perf_pct", "max_perf_pct"):
            if key in params:
                _require(current["pstate"] is not None, "intel_pstate is not active")
                value = _int_in(params[key], 1, 100, key)
                plan.append((f"{PSTATE}/{key}", value))
        if "min_perf_pct" in params and "max_perf_pct" in params:
            _require(int(params["min_perf_pct"]) <= int(params["max_perf_pct"]), "min_perf_pct exceeds max_perf_pct")
        if "dynamic_boost" in params:
            _require(current["pstate"] and current["pstate"]["dynamic_boost"] is not None, "HWP dynamic boost unavailable")
            plan.append((f"{PSTATE}/hwp_dynamic_boost", 1 if params["dynamic_boost"] else 0))
        if "platform_profile" in params:
            _require(params["platform_profile"] in current["platform_profiles"], "unsupported platform profile")
            plan.append((PLATFORM_PROFILE, params["platform_profile"]))

        all_policies = [policy for cluster in clusters.values() for policy in cluster.policies]
        if "governor" in params:
            _require(params["governor"] in current["governors"], "unsupported governor")
            plan.extend((f"{policy}/scaling_governor", params["governor"]) for policy in all_policies)
        if "epp" in params:
            _require(params["epp"] in current["epps"], "unsupported energy-performance preference")
            plan.extend((f"{policy}/energy_performance_preference", params["epp"]) for policy in all_policies)

        for request in params.get("clusters", []):
            cluster = clusters.get(request.get("name"))
            _require(cluster is not None, f"unknown CPU cluster {request.get('name')!r}")
            low = _int_in(request.get("min_khz", cluster.hw_min_khz), cluster.hw_min_khz, cluster.hw_max_khz, "min_khz")
            high = _int_in(request.get("max_khz", cluster.hw_max_khz), cluster.hw_min_khz, cluster.hw_max_khz, "max_khz")
            _require(low <= high, "minimum frequency exceeds maximum")
            for policy in cluster.policies:
                # widen first so the kernel never sees min > max mid-update
                plan.append((f"{policy}/scaling_max_freq", cluster.hw_max_khz))
                plan.append((f"{policy}/scaling_min_freq", low))
                plan.append((f"{policy}/scaling_max_freq", high))

        rapl = params.get("rapl")
        if rapl:
            _require(current["rapl"] is not None, "RAPL powercap interface not found")
            plan.extend(self._rapl_plan(rapl, current["rapl"]))

        undervolt = params.get("undervolt")
        if undervolt:
            state = current["undervolt"]
            _require(state.get("available"), state.get("reason", "voltage offsets unavailable"))
            for plane, mv in undervolt.items():
                _require(plane in UNDERVOLT_PLANES, f"unknown voltage plane {plane!r}")
                mv = _int_in(mv, state["min_mv"], state["max_mv"], f"{plane} offset")
                plan.append((f"{X10_MISC}/undervolt_{plane}", mv))

        for path, value in plan:
            try:
                self.sys.write(path, value)
            except SysfsError as error:
                if "undervolt_" in path and "Operation not permitted" in str(error):
                    raise CpuError(
                        "the firmware ignored the voltage offset (undervolt protection / OC lock in BIOS)"
                    ) from error
                raise CpuError(str(error)) from error
        return self.get()

    def _rapl_plan(self, rapl: dict[str, Any], state: dict[str, Any]) -> list[tuple[str, object]]:
        plan = []
        for zone in self._rapl_zones():
            for index in range(3):
                name = self.sys.read_optional(f"{zone}/constraint_{index}_name")
                key = {"long_term": "pl1", "short_term": "pl2"}.get(name or "")
                if key is None or key not in rapl:
                    continue
                request = rapl[key]
                ceiling = (state.get(key) or {}).get("max_watts") or RAPL_ABSOLUTE_MAX_W
                if "watts" in request:
                    watts = _float_in(request["watts"], 5, ceiling, f"{key.upper()} watts")
                    plan.append((f"{zone}/constraint_{index}_power_limit_uw", int(watts * 1_000_000)))
                if "window_s" in request and key == "pl1":
                    window = _float_in(request["window_s"], 0.001, 128, "PL1 time window")
                    plan.append((f"{zone}/constraint_{index}_time_window_us", int(window * 1_000_000)))
        if "pl1" in rapl and "pl2" in rapl and "watts" in rapl["pl1"] and "watts" in rapl["pl2"]:
            _require(float(rapl["pl1"]["watts"]) <= float(rapl["pl2"]["watts"]), "PL1 must not exceed PL2")
        return plan


def _policy_key(path: str) -> int:
    digits = "".join(ch for ch in path.rsplit("policy", 1)[-1] if ch.isdigit())
    return int(digits or 0)


def _rapl_max_watts(max_uw: int | None) -> float:
    if not max_uw or max_uw <= 0:
        return RAPL_ABSOLUTE_MAX_W
    return min(round(max_uw / 1e6, 1), RAPL_ABSOLUTE_MAX_W)


def _bool_or_none(value: str | None) -> bool | None:
    return None if value is None else value == "1"


def _require(condition: object, message: str) -> None:
    if not condition:
        raise CpuError(message)


def _int_in(value: object, low: int, high: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
        raise CpuError(f"{name} must be an integer")
    value = int(value)
    if not low <= value <= high:
        raise CpuError(f"{name} must be between {low} and {high}")
    return value


def _float_in(value: object, low: float, high: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CpuError(f"{name} must be a number")
    if not low <= float(value) <= high:
        raise CpuError(f"{name} must be between {low} and {high}")
    return float(value)
