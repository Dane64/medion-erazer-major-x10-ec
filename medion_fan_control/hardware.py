from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .protocol import EcProtocol


class UnsupportedHardwareError(RuntimeError):
    pass


class HardwareAccessError(RuntimeError):
    pass


@dataclass(frozen=True)
class MachineIdentity:
    system_vendor: str
    product_name: str
    product_sku: str
    board_name: str
    board_version: str
    bios_version: str
    ec_firmware_release: str


SUPPORTED_IDENTITY = MachineIdentity(
    system_vendor="MEDION",
    product_name="Major X10",
    product_sku="ML-210015 30034642",
    board_name="N68630",
    board_version="1.0",
    bios_version="M1IB008",
    ec_firmware_release="0.8",
)


def read_ac0_online(power_supply_root: Path = Path("/sys/class/power_supply")) -> bool:
    ac_root = power_supply_root / "AC0"
    try:
        supply_type = (ac_root / "type").read_text(encoding="ascii").strip()
        online = (ac_root / "online").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as error:
        raise HardwareAccessError(f"cannot read AC0 power status: {error}") from error
    if supply_type != "Mains":
        raise HardwareAccessError(f"unexpected AC0 power-supply type: {supply_type!r}")
    if online not in {"0", "1"}:
        raise HardwareAccessError(f"invalid AC0 online state: {online!r}")
    return online == "1"


def read_machine_identity(dmi_root: Path = Path("/sys/class/dmi/id")) -> MachineIdentity:
    def read_value(name: str) -> str:
        try:
            return (dmi_root / name).read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as error:
            raise UnsupportedHardwareError(f"cannot read DMI field {name}: {error}") from error

    return MachineIdentity(
        system_vendor=read_value("sys_vendor"),
        product_name=read_value("product_name"),
        product_sku=read_value("product_sku"),
        board_name=read_value("board_name"),
        board_version=read_value("board_version"),
        bios_version=read_value("bios_version"),
        ec_firmware_release=read_value("ec_firmware_release"),
    )


def require_supported_machine(identity: MachineIdentity) -> None:
    mismatches = [
        f"{field}: expected {getattr(SUPPORTED_IDENTITY, field)!r}, got {getattr(identity, field)!r}"
        for field in MachineIdentity.__dataclass_fields__
        if getattr(identity, field) != getattr(SUPPORTED_IDENTITY, field)
    ]
    if mismatches:
        raise UnsupportedHardwareError("unsupported machine or firmware: " + "; ".join(mismatches))


class DevPortIO:
    def __init__(
        self,
        port_path: Path = Path("/dev/port"),
        lock_path: Path = Path("/run/lock/medion-fan-control.lock"),
    ) -> None:
        self._port_path = port_path
        self._lock_path = lock_path
        self._port_fd: int | None = None
        self._lock_fd: int | None = None

    def __enter__(self) -> DevPortIO:
        try:
            self._lock_fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._port_fd = os.open(self._port_path, os.O_RDWR | os.O_CLOEXEC)
        except PermissionError as error:
            self.close()
            raise HardwareAccessError("administrator access is required to open /dev/port") from error
        except BlockingIOError as error:
            self.close()
            raise HardwareAccessError("another fan-control process is already using the EC ports") from error
        except OSError as error:
            self.close()
            raise HardwareAccessError(f"cannot open {self._port_path}: {error}") from error
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        if self._port_fd is not None:
            os.close(self._port_fd)
            self._port_fd = None
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    def read_byte(self, port: int) -> int:
        if self._port_fd is None:
            raise HardwareAccessError("port device is not open")
        data = os.pread(self._port_fd, 1, port)
        if len(data) != 1:
            raise HardwareAccessError(f"short read from port 0x{port:02x}")
        return data[0]

    def write_byte(self, port: int, value: int) -> None:
        if self._port_fd is None:
            raise HardwareAccessError("port device is not open")
        if not 0 <= value <= 0xFF:
            raise ValueError("port value must fit in one byte")
        written = os.pwrite(self._port_fd, bytes((value,)), port)
        if written != 1:
            raise HardwareAccessError(f"short write to port 0x{port:02x}")


@contextmanager
def open_supported_protocol() -> Iterator[EcProtocol]:
    require_supported_machine(read_machine_identity())
    with DevPortIO() as port_io:
        yield EcProtocol(port_io, turbo_power_available=read_ac0_online)