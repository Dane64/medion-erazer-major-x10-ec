"""Display controls.

* Backlight (daemon, sysfs).
* Refresh-rate / VRR selection through KDE's ``kscreen-doctor`` (desktop
  session, unprivileged).
* Panel overclock through an EDID override: the native detailed timing is
  copied with a scaled pixel clock and installed in /usr/lib/firmware/edid.
  The kernel only uses it after ``drm.edid_firmware=<connector>:edid/<file>``
  is added to the command line, so a failed overclock is reverted by simply
  removing that parameter (or editing it out once in the boot menu).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Callable

from .sysfs import Sysfs, SysfsError

BACKLIGHT = "/sys/class/backlight"
DRM = "/sys/class/drm"
EDID_DIR = "/usr/lib/firmware/edid"
EDID_PREFIX = "x10-"
MAX_OVERCLOCK_RATIO = 1.20


class DisplayError(RuntimeError):
    pass


# ---------------------------------------------------------------- EDID math
@dataclass(frozen=True)
class DetailedTiming:
    offset: int
    pixel_clock_khz: int
    h_active: int
    h_blank: int
    v_active: int
    v_blank: int

    @property
    def refresh_hz(self) -> float:
        total = (self.h_active + self.h_blank) * (self.v_active + self.v_blank)
        return self.pixel_clock_khz * 1000 / total

    @property
    def mode_name(self) -> str:
        return f"{self.h_active}x{self.v_active}@{self.refresh_hz:.2f}"


def parse_detailed_timings(edid: bytes) -> list[DetailedTiming]:
    if len(edid) < 128 or edid[:8] != bytes.fromhex("00ffffffffffff00"):
        raise DisplayError("not a valid EDID base block")
    timings = []
    for offset in (54, 72, 90, 108):
        block = edid[offset:offset + 18]
        clock = block[0] | (block[1] << 8)
        if clock == 0:
            continue  # display descriptor, not a timing
        timings.append(DetailedTiming(
            offset=offset,
            pixel_clock_khz=clock * 10,
            h_active=block[2] | ((block[4] & 0xF0) << 4),
            h_blank=block[3] | ((block[4] & 0x0F) << 8),
            v_active=block[5] | ((block[7] & 0xF0) << 4),
            v_blank=block[6] | ((block[7] & 0x0F) << 8),
        ))
    if not timings:
        raise DisplayError("EDID base block contains no detailed timing")
    return timings


DTD_MAX_CLOCK_KHZ = 0xFFFF * 10


def _extension_refresh_rates(edid: bytes) -> list[float]:
    """Refresh rates of timings stored in CTA-861 or DisplayID extension blocks."""
    rates: list[float] = []
    for start in range(128, len(edid) - 127, 128):
        block = edid[start:start + 128]
        if block[0] == 0x02 and block[2] >= 4:  # CTA-861: 18-byte DTDs from byte d
            for offset in range(block[2], 127 - 17, 18):
                dtd = block[offset:offset + 18]
                clock = dtd[0] | (dtd[1] << 8)
                if clock:
                    h_total = (dtd[2] | ((dtd[4] & 0xF0) << 4)) + (dtd[3] | ((dtd[4] & 0x0F) << 8))
                    v_total = (dtd[5] | ((dtd[7] & 0xF0) << 4)) + (dtd[6] | ((dtd[7] & 0x0F) << 8))
                    if h_total and v_total:
                        rates.append(clock * 10_000 / (h_total * v_total))
        elif block[0] == 0x70:  # DisplayID section
            end = min(5 + block[2], 127)
            offset = 5
            while offset + 3 <= end:
                tag, length = block[offset], block[offset + 2]
                payload = block[offset + 3:offset + 3 + length]
                if tag in (0x03, 0x22):  # Type I (10 kHz) / Type VII (1 kHz) detailed timings
                    unit = 10_000 if tag == 0x03 else 1_000
                    for d in range(0, len(payload) - 19, 20):
                        t = payload[d:d + 20]
                        clock = (t[0] | (t[1] << 8) | (t[2] << 16)) + 1
                        h_total = (t[4] | (t[5] << 8)) + 1 + (t[6] | (t[7] << 8)) + 1
                        v_total = (t[12] | (t[13] << 8)) + 1 + (t[14] | (t[15] << 8)) + 1
                        rates.append(clock * unit / (h_total * v_total))
                if length == 0 and tag == 0:
                    break
                offset += 3 + length
    return rates


def native_timing(edid: bytes) -> DetailedTiming:
    return max(parse_detailed_timings(edid), key=lambda timing: timing.refresh_hz)


def overclock_limit_hz(timing: DetailedTiming) -> float:
    total = (timing.h_active + timing.h_blank) * (timing.v_active + timing.v_blank)
    return min(timing.refresh_hz * MAX_OVERCLOCK_RATIO, DTD_MAX_CLOCK_KHZ * 1000 / total)


def overclock_blocker(edid: bytes) -> str | None:
    """Explain why the base-block timing cannot be overclocked safely, if so."""
    timing = native_timing(edid)
    higher = [rate for rate in _extension_refresh_rates(edid) if rate > timing.refresh_hz + 0.5]
    if higher:
        return (
            f"the panel's fastest mode ({max(higher):.0f} Hz) lives in an EDID extension block; "
            "editing extension timings is not supported, and overclocking the base "
            f"{timing.refresh_hz:.0f} Hz timing would lower your refresh rate"
        )
    if overclock_limit_hz(timing) < timing.refresh_hz + 1:
        return (
            f"no headroom: {timing.mode_name} already needs {timing.pixel_clock_khz / 1000:.1f} MHz and an "
            "EDID detailed timing tops out at 655.35 MHz"
        )
    return None


def build_overclocked_edid(edid: bytes, target_hz: float) -> tuple[bytes, DetailedTiming, float]:
    """Return (new EDID, original timing, achieved refresh)."""
    blocker = overclock_blocker(edid)
    if blocker:
        raise DisplayError(f"cannot overclock: {blocker}")
    timing = native_timing(edid)
    limit = overclock_limit_hz(timing)
    if not timing.refresh_hz < target_hz <= limit + 1e-6:
        raise DisplayError(
            f"target must be above {timing.refresh_hz:.1f} Hz and at most {limit:.1f} Hz"
        )
    clock_units = min(round(timing.pixel_clock_khz * target_hz / timing.refresh_hz / 10), 0xFFFF)
    data = bytearray(edid)
    data[timing.offset] = clock_units & 0xFF
    data[timing.offset + 1] = clock_units >> 8
    data[127] = (-sum(data[:127])) & 0xFF
    achieved = clock_units * 10_000 / ((timing.h_active + timing.h_blank) * (timing.v_active + timing.v_blank))
    return bytes(data), timing, achieved


# ---------------------------------------------------------- daemon backend
class DisplayBackend:
    def __init__(self, sysfs: Sysfs | None = None) -> None:
        self.sys = sysfs or Sysfs()

    def _backlight(self) -> str | None:
        devices = self.sys.glob(BACKLIGHT, "*")
        preferred = [d for d in devices if d.name.startswith("intel_backlight")] or devices
        return str(self.sys.relative(preferred[0])) if preferred else None

    def _panel(self) -> tuple[str, str] | None:
        for connector in self.sys.glob(DRM, "card*-eDP-*"):
            relative = str(self.sys.relative(connector))
            if (self.sys.read_optional(f"{relative}/status") or "connected") == "connected":
                name = connector.name.split("-", 1)[1]
                return relative, name
        return None

    def get(self) -> dict[str, Any]:
        backlight = self._backlight()
        state: dict[str, Any] = {"backlight": None, "panel": None}
        if backlight:
            maximum = self.sys.read_int_optional(f"{backlight}/max_brightness") or 0
            current = self.sys.read_int_optional(f"{backlight}/brightness") or 0
            state["backlight"] = {
                "device": backlight.rsplit("/", 1)[-1],
                "percent": round(current * 100 / maximum) if maximum else None,
            }
        panel = self._panel()
        if panel:
            path, connector = panel
            panel_state: dict[str, Any] = {"connector": connector, "overrides": self._overrides(connector)}
            try:
                edid = self.sys.path(f"{path}/edid").read_bytes()
                timing = native_timing(edid)
                panel_state.update({
                    "native_mode": timing.mode_name,
                    "native_hz": round(timing.refresh_hz, 3),
                    "max_hz": round(overclock_limit_hz(timing), 1),
                    "blocker": overclock_blocker(edid),
                })
            except (OSError, DisplayError) as error:
                panel_state["error"] = str(error)
            state["panel"] = panel_state
        return state

    def set_brightness(self, percent: object) -> dict[str, Any]:
        if isinstance(percent, bool) or not isinstance(percent, (int, float)) or not 1 <= percent <= 100:
            raise DisplayError("brightness must be between 1 and 100 %")
        backlight = self._backlight()
        if backlight is None:
            raise DisplayError("no backlight device found")
        maximum = self.sys.read_int(f"{backlight}/max_brightness")
        try:
            self.sys.write(f"{backlight}/brightness", max(1, round(maximum * percent / 100)))
        except SysfsError as error:
            raise DisplayError(str(error)) from error
        return self.get()

    def _overrides(self, connector: str) -> list[str]:
        directory = self.sys.path(EDID_DIR)
        if not directory.is_dir():
            return []
        return sorted(p.name for p in directory.glob(f"{EDID_PREFIX}{connector}-*.bin"))

    def install_overclock(self, target_hz: object) -> dict[str, Any]:
        if isinstance(target_hz, bool) or not isinstance(target_hz, (int, float)):
            raise DisplayError("target refresh must be a number")
        panel = self._panel()
        if panel is None:
            raise DisplayError("no connected eDP panel found")
        path, connector = panel
        try:
            edid = self.sys.path(f"{path}/edid").read_bytes()
        except OSError as error:
            raise DisplayError(f"cannot read panel EDID: {error}") from error
        data, timing, achieved = build_overclocked_edid(edid, float(target_hz))
        name = f"{EDID_PREFIX}{connector}-{round(achieved)}hz.bin"
        directory = self.sys.path(EDID_DIR)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            for old in directory.glob(f"{EDID_PREFIX}{connector}-*.bin"):
                old.unlink()
            (directory / name).write_bytes(data)
        except OSError as error:
            raise DisplayError(f"cannot install EDID override: {error}") from error
        return {
            **self.get(),
            "installed": name,
            "achieved_hz": round(achieved, 2),
            "native_hz": round(timing.refresh_hz, 2),
            "kernel_parameter": f"drm.edid_firmware={connector}:edid/{name}",
        }

    def remove_overclock(self) -> dict[str, Any]:
        panel = self._panel()
        connector = panel[1] if panel else "*"
        directory = self.sys.path(EDID_DIR)
        if directory.is_dir():
            for old in directory.glob(f"{EDID_PREFIX}{connector}-*.bin"):
                try:
                    old.unlink()
                except OSError as error:
                    raise DisplayError(f"cannot remove {old.name}: {error}") from error
        return self.get()


# ------------------------------------------------- desktop-session (KDE)
Runner = Callable[[list[str]], subprocess.CompletedProcess]


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)


class KScreen:
    """Refresh-rate and VRR control in the user's Plasma session."""

    def __init__(self, runner: Runner = _run, which: Callable[[str], str | None] = shutil.which) -> None:
        self._run = runner
        self._which = which

    @property
    def available(self) -> bool:
        return self._which("kscreen-doctor") is not None

    def outputs(self) -> list[dict[str, Any]]:
        if not self.available:
            raise DisplayError("kscreen-doctor not found; refresh selection needs a KDE Plasma session")
        result = self._run(["kscreen-doctor", "-j"])
        if result.returncode != 0:
            raise DisplayError((result.stderr or "kscreen-doctor failed").strip())
        try:
            document = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise DisplayError(f"unexpected kscreen-doctor output: {error}") from error
        outputs = []
        for output in document.get("outputs", []):
            if not output.get("connected", True) or not output.get("enabled", True):
                continue
            current_id = str(output.get("currentModeId", ""))
            current = next((m for m in output.get("modes", []) if str(m.get("id")) == current_id), None)
            size = (current or {}).get("size", {})
            modes = [
                {"id": str(m.get("id")), "refresh": float(m.get("refreshRate", 0)), "name": m.get("name", "")}
                for m in output.get("modes", [])
                if m.get("size", {}) == size
            ]
            modes.sort(key=lambda mode: -mode["refresh"])
            outputs.append({
                "name": output.get("name"),
                "current_mode": current_id,
                "resolution": f"{size.get('width', '?')}x{size.get('height', '?')}",
                "modes": modes,
                "vrr_policy": output.get("vrrPolicy"),
            })
        return outputs

    def set_mode(self, output: str, mode_id: str) -> None:
        if not re.fullmatch(r"[\w.-]+", output) or not re.fullmatch(r"[\w.-]+", mode_id):
            raise DisplayError("invalid output or mode id")
        self._check(self._run(["kscreen-doctor", f"output.{output}.mode.{mode_id}"]))

    def set_vrr(self, output: str, policy: str) -> None:
        if policy not in {"never", "always", "automatic"} or not re.fullmatch(r"[\w.-]+", output):
            raise DisplayError("invalid VRR policy")
        self._check(self._run(["kscreen-doctor", f"output.{output}.vrrpolicy.{policy}"]))

    @staticmethod
    def _check(result: subprocess.CompletedProcess) -> None:
        if result.returncode != 0:
            raise DisplayError((result.stderr or result.stdout or "kscreen-doctor failed").strip())


__all__ = [
    "DisplayBackend", "DisplayError", "KScreen", "build_overclocked_edid", "overclock_blocker",
    "native_timing", "parse_detailed_timings",
]
