from __future__ import annotations

import fcntl
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

from .hardware import read_machine_identity, require_supported_machine


class LightingAccessError(RuntimeError):
    pass


class LightZone(IntEnum):
    KEYBOARD = 0
    LEFT_SIDE = 1
    RIGHT_SIDE = 2
    LID = 3

    @property
    def label(self) -> str:
        return {
            LightZone.KEYBOARD: "Keyboard",
            LightZone.LEFT_SIDE: "Left side",
            LightZone.RIGHT_SIDE: "Right side",
            LightZone.LID: "Lid logo",
        }[self]


@dataclass(frozen=True)
class RgbColor:
    red: int
    green: int
    blue: int

    def __post_init__(self) -> None:
        if any(type(component) is not int or not 0 <= component <= 0xFF for component in (self.red, self.green, self.blue)):
            raise ValueError("RGB components must be integers between 0 and 255")

    @classmethod
    def from_hex(cls, value: str) -> RgbColor:
        if not isinstance(value, str) or re.fullmatch(r"#[0-9a-fA-F]{6}", value) is None:
            raise ValueError("RGB color must use #rrggbb format")
        return cls(*(int(value[offset : offset + 2], 16) for offset in (1, 3, 5)))

    @property
    def hex(self) -> str:
        return f"#{self.red:02x}{self.green:02x}{self.blue:02x}"


HID_ID = "0003:00001A2C:00001512"
HID_INTERFACE = "03"
STATIC_COLOR_APPLY_ATTEMPTS = 3
STATIC_COLOR_APPLY_DELAY_SECONDS = 0.050
HID_REPORT_DESCRIPTOR = bytes.fromhex(
    "06 00 ff 09 02 a1 01 19 01 29 40 15 00 26 ff 00 "
    "75 08 95 40 81 00 19 01 29 40 91 00 75 08 96 08 "
    "02 15 00 26 ff 00 09 02 b1 02 c0"
)


def find_lighting_device(
    hidraw_root: Path = Path("/sys/class/hidraw"),
    device_root: Path = Path("/dev"),
) -> Path:
    for hidraw_entry in sorted(hidraw_root.glob("hidraw*")):
        try:
            hid_device = (hidraw_entry / "device").resolve(strict=True)
            properties = dict(
                line.split("=", 1)
                for line in (hid_device / "uevent").read_text(encoding="ascii").splitlines()
                if "=" in line
            )
            interface = (hid_device.parent / "bInterfaceNumber").read_text(encoding="ascii").strip()
            descriptor = (hid_device / "report_descriptor").read_bytes()
        except (OSError, UnicodeError, ValueError):
            continue
        if properties.get("HID_ID") == HID_ID and interface == HID_INTERFACE and descriptor == HID_REPORT_DESCRIPTOR:
            return device_root / hidraw_entry.name
    raise LightingAccessError("verified lighting HID interface 1a2c:1512/03 was not found")


def build_static_color_report(
    zone: LightZone,
    color: RgbColor,
    *,
    brightness: int = 0x80,
    speed: int = 2,
    orientation: int = 2,
) -> bytes:
    if not 0 <= brightness <= 0xFF:
        raise ValueError("brightness must be between 0 and 255")
    if not 0 <= speed <= 0x0F or not 0 <= orientation <= 0x0F:
        raise ValueError("speed and orientation must fit in four bits")
    report = bytearray(65)
    report[1:3] = b"\x03\xc0"
    report[3] = LightZone(zone).value
    report[5] = 0x80
    report[6] = brightness
    report[7] = brightness
    report[8] = (orientation << 4) | speed
    report[9:12] = bytes((color.red, color.green, color.blue))
    return bytes(report)


class HidLighting:
    def __init__(
        self,
        device_path: Path | None = None,
        lock_path: Path = Path("/run/lock/medion-fan-control-lighting.lock"),
    ) -> None:
        self._device_path = device_path
        self._lock_path = lock_path
        self._device_fd: int | None = None
        self._lock_fd: int | None = None

    def __enter__(self) -> HidLighting:
        if self._device_fd is not None or self._lock_fd is not None:
            raise LightingAccessError("lighting device is already open")
        device_path = self._device_path or find_lighting_device()
        try:
            self._lock_fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._device_fd = os.open(device_path, os.O_WRONLY | os.O_CLOEXEC)
        except PermissionError as error:
            self.close()
            raise LightingAccessError(f"permission denied opening lighting device {device_path}") from error
        except BlockingIOError as error:
            self.close()
            raise LightingAccessError("another process is controlling the laptop lighting") from error
        except OSError as error:
            self.close()
            raise LightingAccessError(f"cannot open lighting device {device_path}: {error}") from error
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        device_fd, self._device_fd = self._device_fd, None
        lock_fd, self._lock_fd = self._lock_fd, None
        try:
            if device_fd is not None:
                os.close(device_fd)
        finally:
            if lock_fd is not None:
                os.close(lock_fd)

    def set_static_color(self, zone: LightZone, color: RgbColor) -> None:
        self._write_report(build_static_color_report(zone, color))

    def _write_report(self, report: bytes) -> None:
        if self._device_fd is None:
            raise LightingAccessError("lighting device is not open")
        written = os.write(self._device_fd, report)
        if written != len(report):
            raise LightingAccessError(f"short lighting report write: {written} of {len(report)} bytes")

    def set_static_colors(
        self,
        colors: Mapping[LightZone, RgbColor],
        *,
        attempts: int = STATIC_COLOR_APPLY_ATTEMPTS,
        delay_seconds: float = STATIC_COLOR_APPLY_DELAY_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if attempts < 1:
            raise ValueError("attempts must be at least 1")
        if delay_seconds < 0:
            raise ValueError("delay_seconds cannot be negative")
        if not colors:
            raise ValueError("select at least one lighting zone")
        reports = [build_static_color_report(zone, color) for zone, color in colors.items()]
        for attempt in range(attempts):
            for report in reports:
                self._write_report(report)
            if attempt + 1 < attempts:
                sleep(delay_seconds)


def apply_static_lighting(colors: Mapping[LightZone, RgbColor]) -> None:
    require_supported_machine(read_machine_identity())
    with HidLighting() as lighting:
        lighting.set_static_colors(colors)


OFF = RgbColor(0, 0, 0)


@dataclass(frozen=True)
class ZoneSetting:
    """A zone's chosen color plus an on/off switch.

    Switching a zone off sends black (R=G=B=0): with no LED current the zone
    is physically dark, and this needs no unverified firmware command. The
    chosen color is kept so switching on restores it.
    """

    color: RgbColor
    on: bool = True


def effective_color(setting: ZoneSetting, brightness_percent: int = 100) -> RgbColor:
    if not 0 <= brightness_percent <= 100:
        raise ValueError("brightness must be between 0 and 100 %")
    if not setting.on or brightness_percent == 0:
        return OFF
    scale = brightness_percent / 100
    color = setting.color
    return RgbColor(*(round(component * scale) for component in (color.red, color.green, color.blue)))


def resolve_lighting(
    settings: Mapping[LightZone, ZoneSetting], brightness_percent: int = 100,
) -> dict[LightZone, RgbColor]:
    return {LightZone(zone): effective_color(setting, brightness_percent) for zone, setting in settings.items()}


def lighting_to_document(settings: Mapping[LightZone, ZoneSetting], brightness_percent: int) -> dict:
    return {
        "brightness": brightness_percent,
        "zones": {
            zone.name.lower(): {"color": setting.color.hex, "on": setting.on}
            for zone, setting in sorted(settings.items(), key=lambda item: item[0].value)
        },
    }


def lighting_from_document(document: object) -> tuple[dict[LightZone, ZoneSetting], int]:
    if not isinstance(document, dict) or not isinstance(document.get("zones"), dict):
        raise ValueError("lighting document must contain a zones mapping")
    brightness = document.get("brightness", 100)
    if isinstance(brightness, bool) or not isinstance(brightness, int) or not 0 <= brightness <= 100:
        raise ValueError("brightness must be an integer between 0 and 100")
    settings: dict[LightZone, ZoneSetting] = {}
    for name, entry in document["zones"].items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            raise ValueError("invalid zone entry")
        try:
            zone = LightZone[name.upper()]
        except KeyError as error:
            raise ValueError(f"unknown lighting zone {name!r}") from error
        on = entry.get("on", True)
        if not isinstance(on, bool):
            raise ValueError("zone 'on' must be true or false")
        settings[zone] = ZoneSetting(RgbColor.from_hex(entry.get("color")), on)
    return settings, brightness