"""GPU controls for the Alder Lake iGPU and the Arc A730M dGPU.

Frequencies use the i915 (``gt_*_freq_mhz``) or xe (``tile0/gt0/freq0``)
sysfs interfaces; power limits use the driver's hwmon ``power1_max``.
Neither driver exposes voltage or above-RP0 overclocking on Linux, so the
UI is bounded to RPn..RP0. dGPU power gating is delegated to arc-dgpu-ctl.
"""
from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable

from .sysfs import Sysfs, SysfsError

DRM = "/sys/class/drm"
PCI = "/sys/bus/pci/devices"
INTEL_VENDOR = "0x8086"
# Arc Alchemist (DG2) mobile/desktop device IDs share the 0x56xx range.
DG2_NAMES = {
    "0x5690": "Arc A770M", "0x5691": "Arc A730M", "0x5692": "Arc A550M",
    "0x5693": "Arc A370M", "0x5694": "Arc A350M", "0x56a0": "Arc A770",
    "0x56a1": "Arc A750", "0x56a5": "Arc A380",
}


class GpuError(RuntimeError):
    pass


@dataclass
class ArcDgpuCtlConfig:
    command: str = "arc-dgpu-ctl"
    on_args: list[str] = field(default_factory=lambda: ["on"])
    off_args: list[str] = field(default_factory=lambda: ["off"])
    timeout_s: float = 30.0


Runner = Callable[[list[str], float], subprocess.CompletedProcess]


def _default_runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)


class GpuBackend:
    def __init__(
        self,
        sysfs: Sysfs | None = None,
        arc_dgpu_ctl: ArcDgpuCtlConfig | None = None,
        runner: Runner = _default_runner,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self.sys = sysfs or Sysfs()
        self.arc_dgpu_ctl = arc_dgpu_ctl or ArcDgpuCtlConfig()
        self._run = runner
        self._which = which

    # ------------------------------------------------------------ discovery
    def _cards(self) -> list[str]:
        cards = []
        for entry in self.sys.glob(DRM, "card*"):
            if re.fullmatch(r"card\d+", entry.name):
                cards.append(str(self.sys.relative(entry)))
        return cards

    def _pci_address(self, card: str) -> str | None:
        try:
            return self.sys.path(f"{card}/device").resolve(strict=True).name
        except OSError:
            return None

    @staticmethod
    def _is_discrete(pci_address: str | None) -> bool:
        return bool(pci_address) and not pci_address.split(":")[1] == "00"

    def _freq_paths(self, card: str) -> dict[str, str] | None:
        if self.sys.exists(f"{card}/gt_max_freq_mhz"):
            return {
                "min": f"{card}/gt_min_freq_mhz", "max": f"{card}/gt_max_freq_mhz",
                "boost": f"{card}/gt_boost_freq_mhz", "rp0": f"{card}/gt_RP0_freq_mhz",
                "rpn": f"{card}/gt_RPn_freq_mhz", "cur": f"{card}/gt_cur_freq_mhz",
                "act": f"{card}/gt_act_freq_mhz",
            }
        xe = f"{card}/device/tile0/gt0/freq0"
        if self.sys.exists(f"{xe}/max_freq"):
            return {
                "min": f"{xe}/min_freq", "max": f"{xe}/max_freq", "rp0": f"{xe}/rp0_freq",
                "rpn": f"{xe}/rpn_freq", "cur": f"{xe}/cur_freq", "act": f"{xe}/act_freq",
            }
        return None

    def _hwmon(self, card: str) -> str | None:
        for hwmon in self.sys.glob(f"{card}/device/hwmon", "hwmon*"):
            relative = str(self.sys.relative(hwmon))
            if self.sys.exists(f"{relative}/power1_max"):
                return relative
        return None

    def _card_state(self, card: str) -> dict[str, Any]:
        pci = self._pci_address(card)
        device_id = self.sys.read_optional(f"{card}/device/device")
        driver = None
        try:
            driver = self.sys.path(f"{card}/device/driver").resolve(strict=True).name
        except OSError:
            pass
        discrete = self._is_discrete(pci)
        name = DG2_NAMES.get(device_id or "", "Intel Arc dGPU" if discrete else "Intel Iris Xe (iGPU)")
        state: dict[str, Any] = {
            "id": card.rsplit("/", 1)[-1],
            "name": name,
            "pci": pci,
            "device_id": device_id,
            "driver": driver,
            "discrete": discrete,
            "runtime_status": self.sys.read_optional(f"{card}/device/power/runtime_status"),
            "freq": None,
            "power": None,
        }
        paths = self._freq_paths(card)
        if paths:
            state["freq"] = {key: self.sys.read_int_optional(path) for key, path in paths.items()}
        hwmon = self._hwmon(card)
        if hwmon:
            limit = self.sys.read_int_optional(f"{hwmon}/power1_max")
            rated = self.sys.read_int_optional(f"{hwmon}/power1_rated_max")
            state["power"] = {
                "limit_w": None if limit is None else round(limit / 1e6, 1),
                "rated_w": None if not rated else round(rated / 1e6, 1),
            }
        return state

    def _dgpu_pci(self) -> list[str]:
        devices = []
        for device in self.sys.glob(PCI, "*"):
            relative = str(self.sys.relative(device))
            if self.sys.read_optional(f"{relative}/vendor") != INTEL_VENDOR:
                continue
            if not (self.sys.read_optional(f"{relative}/class") or "").startswith("0x03"):
                continue
            if self._is_discrete(device.name):
                devices.append(relative)
        return devices

    def dgpu_status(self) -> dict[str, Any]:
        command = self._which(shlex.split(self.arc_dgpu_ctl.command)[0]) if self.arc_dgpu_ctl.command else None
        devices = self._dgpu_pci()
        if not devices:
            state = "off"  # removed from the PCI bus (arc-dgpu-ctl power-off)
            driver = runtime = None
        else:
            device = devices[0]
            try:
                driver = self.sys.path(f"{device}/driver").resolve(strict=True).name
            except OSError:
                driver = None
            runtime = self.sys.read_optional(f"{device}/power/runtime_status")
            state = "unbound" if driver is None else ("suspended" if runtime == "suspended" else "active")
        return {
            "present": bool(devices),
            "state": state,
            "driver": driver,
            "runtime_status": runtime,
            "arc_dgpu_ctl": command,
        }

    def get(self) -> dict[str, Any]:
        return {
            "gpus": [self._card_state(card) for card in self._cards()],
            "dgpu": self.dgpu_status(),
        }

    # ---------------------------------------------------------------- write
    def set(self, params: dict[str, Any]) -> dict[str, Any]:
        card_id = params.get("id")
        if not isinstance(card_id, str) or not re.fullmatch(r"card\d+", card_id):
            raise GpuError("a GPU id such as 'card1' is required")
        card = f"{DRM}/{card_id}"
        if not self.sys.exists(card):
            raise GpuError(f"{card_id} is not present (is the dGPU powered off?)")
        paths = self._freq_paths(card)
        plan: list[tuple[str, int]] = []
        if any(key in params for key in ("min_mhz", "max_mhz", "boost_mhz")):
            if paths is None:
                raise GpuError(f"{card_id} exposes no frequency controls")
            rpn = self.sys.read_int(paths["rpn"])
            rp0 = self.sys.read_int(paths["rp0"])
            low = _bounded(params.get("min_mhz", self.sys.read_int(paths["min"])), rpn, rp0, "minimum MHz")
            high = _bounded(params.get("max_mhz", self.sys.read_int(paths["max"])), rpn, rp0, "maximum MHz")
            if low > high:
                raise GpuError("minimum frequency exceeds maximum")
            # widen, then narrow, so min <= max holds at every step
            plan += [(paths["max"], rp0), (paths["min"], low), (paths["max"], high)]
            if "boost_mhz" in params:
                if "boost" not in paths:
                    raise GpuError("this driver has no boost frequency control")
                plan.append((paths["boost"], _bounded(params["boost_mhz"], low, rp0, "boost MHz")))
        if "power_limit_w" in params:
            hwmon = self._hwmon(card)
            if hwmon is None:
                raise GpuError(f"{card_id} exposes no power limit")
            rated = self.sys.read_int_optional(f"{hwmon}/power1_rated_max")
            ceiling = rated / 1e6 if rated else 150.0
            watts = params["power_limit_w"]
            if isinstance(watts, bool) or not isinstance(watts, (int, float)) or not 10 <= watts <= ceiling:
                raise GpuError(f"power limit must be between 10 and {ceiling:g} W")
            plan.append((f"{hwmon}/power1_max", int(watts * 1_000_000)))
        for path, value in plan:
            try:
                self.sys.write(path, value)
            except SysfsError as error:
                raise GpuError(str(error)) from error
        return self.get()

    def set_dgpu_power(self, on: bool) -> dict[str, Any]:
        config = self.arc_dgpu_ctl
        executable = self._which(shlex.split(config.command)[0]) if config.command else None
        if executable is None:
            raise GpuError(f"{config.command!r} was not found; install arc-dgpu-ctl or set [dgpu] command")
        argv = [executable, *shlex.split(config.command)[1:], *(config.on_args if on else config.off_args)]
        try:
            result = self._run(argv, config.timeout_s)
        except subprocess.TimeoutExpired as error:
            raise GpuError(f"arc-dgpu-ctl timed out after {config.timeout_s:g} s") from error
        except OSError as error:
            raise GpuError(f"cannot run arc-dgpu-ctl: {error}") from error
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            raise GpuError(f"arc-dgpu-ctl exited with {result.returncode}: {detail[-1] if detail else 'no output'}")
        return self.get()


def _bounded(value: object, low: int, high: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
        raise GpuError(f"{name} must be an integer")
    if not low <= int(value) <= high:
        raise GpuError(f"{name} must be between {low} and {high}")
    return int(value)


def dgpu_config_from_mapping(section: Any) -> ArcDgpuCtlConfig:
    if section is None:
        return ArcDgpuCtlConfig()
    return ArcDgpuCtlConfig(
        command=section.get("command", "arc-dgpu-ctl"),
        on_args=shlex.split(section.get("on_args", "on")),
        off_args=shlex.split(section.get("off_args", "off")),
        timeout_s=float(section.get("timeout_s", "30")),
    )


__all__ = ["GpuBackend", "GpuError", "ArcDgpuCtlConfig", "dgpu_config_from_mapping"]
