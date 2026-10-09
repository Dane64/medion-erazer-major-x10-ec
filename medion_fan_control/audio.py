"""Audio controls.

The HDA codec's adjustable features (amplifier/DAC volumes, mute switches,
auto-mute, loopback, mic boost, output routing ...) are published by the
kernel as ALSA mixer controls. They are enumerated at runtime with
``amixer contents`` instead of being hard-coded, so the tab shows exactly
what this codec supports. These run in the desktop session; only the HDA
power-saving module parameters go through the daemon.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable

from .sysfs import Sysfs, SysfsError

HDA_PARAMS = "/sys/module/snd_hda_intel/parameters"


class AudioError(RuntimeError):
    pass


@dataclass
class MixerControl:
    numid: int
    iface: str
    name: str
    type: str
    access: str
    values: list[str]
    minimum: int | None = None
    maximum: int | None = None
    items: list[str] = field(default_factory=list)

    @property
    def writable(self) -> bool:
        return "w" in self.access[:2]

    @property
    def category(self) -> str:
        lowered = self.name.lower()
        if "capture" in lowered or "mic" in lowered:
            return "Input"
        if "playback" in lowered or any(word in lowered for word in ("master", "headphone", "speaker", "pcm")):
            return "Output"
        return "Codec options"


_HEADER = re.compile(r"numid=(\d+),iface=(\w+),name='(.*)'(?:,index=\d+)?$")


def parse_amixer_contents(text: str) -> list[MixerControl]:
    controls: list[MixerControl] = []
    current: MixerControl | None = None
    for raw in text.splitlines():
        line = raw.strip()
        header = _HEADER.match(line)
        if header:
            current = MixerControl(int(header[1]), header[2], header[3], "", "", [])
            controls.append(current)
            continue
        if current is None:
            continue
        if line.startswith("; type="):
            fields = dict(part.split("=", 1) for part in line[2:].split(",") if "=" in part)
            current.type = fields.get("type", "")
            current.access = fields.get("access", "")
            if "min" in fields:
                current.minimum = int(fields["min"])
            if "max" in fields:
                current.maximum = int(fields["max"])
        elif line.startswith("; Item #"):
            match = re.match(r"; Item #\d+ '(.*)'$", line)
            if match:
                current.items.append(match[1])
        elif line.startswith(": values="):
            current.values = line[len(": values="):].split(",")
    return controls


Runner = Callable[[list[str]], subprocess.CompletedProcess]


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)


class AlsaMixer:
    SUPPORTED = {"BOOLEAN", "INTEGER", "ENUMERATED"}

    def __init__(self, runner: Runner = _run, which: Callable[[str], str | None] = shutil.which,
                 cards_path: str = "/proc/asound/cards") -> None:
        self._run = runner
        self._which = which
        self._cards_path = cards_path

    def cards(self) -> list[dict[str, Any]]:
        try:
            text = open(self._cards_path, encoding="utf-8").read()
        except OSError as error:
            raise AudioError(f"cannot list sound cards: {error}") from error
        cards = []
        for match in re.finditer(r"^\s*(\d+)\s+\[(\S+)\s*\]:\s*(.*?)\s*-\s*(.*)$", text, re.MULTILINE):
            cards.append({"index": int(match[1]), "id": match[2], "driver": match[3], "name": match[4]})
        return cards

    def controls(self, card: int) -> list[MixerControl]:
        if self._which("amixer") is None:
            raise AudioError("amixer not found; install alsa-utils")
        result = self._run(["amixer", "-c", str(int(card)), "contents"])
        if result.returncode != 0:
            raise AudioError((result.stderr or "amixer failed").strip())
        return [
            control for control in parse_amixer_contents(result.stdout)
            if control.iface == "MIXER" and control.type in self.SUPPORTED and control.writable
        ]

    def set(self, card: int, control: MixerControl, value: Any) -> None:
        if control.type == "BOOLEAN":
            text = "on" if value else "off"
        elif control.type == "INTEGER":
            number = int(value)
            if control.minimum is not None and control.maximum is not None:
                if not control.minimum <= number <= control.maximum:
                    raise AudioError(f"{control.name}: value must be {control.minimum}..{control.maximum}")
            text = str(number)
        elif control.type == "ENUMERATED":
            index = int(value)
            if not 0 <= index < len(control.items):
                raise AudioError(f"{control.name}: invalid option")
            text = str(index)
        else:
            raise AudioError(f"{control.name}: unsupported control type {control.type}")
        # one value per channel keeps stereo controls balanced
        joined = ",".join([text] * max(1, len(control.values)))
        result = self._run(["amixer", "-c", str(int(card)), "-q", "cset", f"numid={control.numid}", joined])
        if result.returncode != 0:
            raise AudioError((result.stderr or "amixer cset failed").strip())


class AudioPowerBackend:
    """snd_hda_intel runtime power saving (daemon side)."""

    def __init__(self, sysfs: Sysfs | None = None) -> None:
        self.sys = sysfs or Sysfs()

    def get(self) -> dict[str, Any]:
        timeout = self.sys.read_int_optional(f"{HDA_PARAMS}/power_save")
        controller = self.sys.read_optional(f"{HDA_PARAMS}/power_save_controller")
        return {
            "available": timeout is not None,
            "power_save_s": timeout,
            "power_save_controller": None if controller is None else controller in {"Y", "1"},
        }

    def set(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            if "power_save_s" in params:
                seconds = params["power_save_s"]
                if isinstance(seconds, bool) or not isinstance(seconds, int) or not 0 <= seconds <= 3600:
                    raise AudioError("power_save must be 0..3600 seconds (0 disables)")
                self.sys.write(f"{HDA_PARAMS}/power_save", seconds)
            if "power_save_controller" in params:
                self.sys.write(f"{HDA_PARAMS}/power_save_controller", "Y" if params["power_save_controller"] else "N")
        except SysfsError as error:
            raise AudioError(str(error)) from error
        return self.get()
