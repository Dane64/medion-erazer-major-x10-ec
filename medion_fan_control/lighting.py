from __future__ import annotations

import fcntl
import json
import os
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
            LightZone.LEFT_SIDE: "Side L",
            LightZone.RIGHT_SIDE: "Side R",
            LightZone.LID: "Lid",
        }[self]


@dataclass(frozen=True)
class RgbColor:
    red: int
    green: int
    blue: int

    def __post_init__(self) -> None:
        if any(not 0 <= component <= 0xFF for component in (self.red, self.green, self.blue)):
            raise ValueError("RGB components must be between 0 and 255")

    @classmethod
    def from_hex(cls, value: str) -> RgbColor:
        if len(value) != 7 or not value.startswith("#"):
            raise ValueError("RGB color must use #rrggbb format")
        try:
            return cls(*(int(value[offset : offset + 2], 16) for offset in (1, 3, 5)))
        except ValueError as error:
            raise ValueError("RGB color must use #rrggbb format") from error

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


def lighting_config_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return root / "medion-fan-control" / "lighting.json"


def load_lighting_colors(path: Path | None = None) -> dict[LightZone, RgbColor]:
    config_path = path or lighting_config_path()
    try:
        document = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LightingAccessError(f"cannot load saved lighting colors: {error}") from error
    if not isinstance(document, dict) or not isinstance(document.get("colors"), dict):
        raise LightingAccessError("saved lighting colors have an invalid format")
    try:
        return {
            LightZone[zone_name.upper()]: RgbColor.from_hex(value)
            for zone_name, value in document["colors"].items()
        }
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise LightingAccessError(f"saved lighting colors have an invalid value: {error}") from error


def save_lighting_colors(colors: Mapping[LightZone, RgbColor], path: Path | None = None) -> None:
    config_path = path or lighting_config_path()
    document = {
        "colors": {
            zone.name.lower(): color.hex
            for zone, color in sorted(colors.items(), key=lambda item: item[0].value)
        }
    }
    temporary_path = config_path.with_suffix(".tmp")
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary_path, config_path)
    except OSError as error:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise LightingAccessError(f"cannot save lighting colors: {error}") from error


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
        if self._device_fd is not None:
            os.close(self._device_fd)
            self._device_fd = None
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    def set_static_color(self, zone: LightZone, color: RgbColor) -> None:
        if self._device_fd is None:
            raise LightingAccessError("lighting device is not open")
        report = build_static_color_report(zone, color)
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
        for attempt in range(attempts):
            for zone, color in colors.items():
                self.set_static_color(zone, color)
            if attempt + 1 < attempts:
                sleep(delay_seconds)


def apply_static_lighting(colors: Mapping[LightZone, RgbColor]) -> None:
    require_supported_machine(read_machine_identity())
    with HidLighting() as lighting:
        lighting.set_static_colors(colors)