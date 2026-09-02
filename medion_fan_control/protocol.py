from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum
from typing import Protocol


class BytePortIO(Protocol):
    def read_byte(self, port: int) -> int: ...

    def write_byte(self, port: int, value: int) -> None: ...


class ProtocolError(RuntimeError):
    pass


class InsufficientPowerError(ProtocolError):
    pass


TURBO_POWER_MESSAGE = (
    "Turbo mode is not available with the current power supply. "
    "Connect the AC barrel adapter; USB-C PD does not provide enough power."
)


class Profile(IntEnum):
    OFFICE = 1
    GAMING = 2
    TURBO = 3

    @property
    def label(self) -> str:
        return self.name.lower()


@dataclass(frozen=True)
class FanStatus:
    profile: Profile
    full_speed: bool
    cpu_fan_rpm: int
    gpu_fan_rpm: int
    cpu_temperature_c: int
    gpu_temperature_c: int
    turbo_available: bool = True


class EcProtocol:
    COMMAND_PORT = 0x6C
    DATA_PORT = 0x68

    COMMAND_FAN_SPEED = 0xD5
    COMMAND_TEMPERATURE = 0xDD
    COMMAND_CONTROL = 0xDE

    INDEX_POWER_STATE = 0x05
    INDEX_FULL_SPEED = 0x10
    INDEX_PROFILE = 0x11
    INDEX_GPU_FAN_LOW = 0x16
    INDEX_GPU_FAN_HIGH = 0x17
    INDEX_CPU_FAN_LOW = 0x18
    INDEX_CPU_FAN_HIGH = 0x19
    INDEX_CPU_TEMPERATURE = 0x20
    INDEX_GPU_TEMPERATURE = 0x23

    ACTION_FULL_SPEED_ENABLE = 0x0E
    ACTION_FULL_SPEED_DISABLE = 0x0F

    def __init__(
        self,
        port_io: BytePortIO,
        *,
        turbo_power_available: Callable[[], bool] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        retries: int = 5,
        io_delay: float = 0.010,
        profile_settle_delay: float = 0.030,
    ) -> None:
        if retries < 1:
            raise ValueError("retries must be at least 1")
        self._port_io = port_io
        self._turbo_power_available = turbo_power_available or (lambda: True)
        self._sleep = sleep
        self._retries = retries
        self._io_delay = io_delay
        self._profile_settle_delay = profile_settle_delay

    def _send_value(self, command: int, value: int, *, post_delay: float = 0) -> None:
        self._port_io.write_byte(self.COMMAND_PORT, command)
        self._sleep(self._io_delay)
        self._port_io.write_byte(self.DATA_PORT, value)
        if post_delay:
            self._sleep(post_delay)

    def _read_index(self, command: int, index: int) -> int:
        self._send_value(command, index, post_delay=self._io_delay)
        return self._port_io.read_byte(self.DATA_PORT)

    def _read_bounded(self, description: str, command: int, index: int, minimum: int, maximum: int) -> int:
        last_value = -1
        for _ in range(self._retries):
            last_value = self._read_index(command, index)
            if minimum <= last_value <= maximum:
                return last_value
        raise ProtocolError(
            f"invalid {description} value after {self._retries} attempts: "
            f"0x{last_value:02x} (expected {minimum}..{maximum})"
        )

    def read_profile(self) -> Profile:
        value = self._read_bounded("profile", self.COMMAND_CONTROL, self.INDEX_PROFILE, 1, 3)
        return Profile(value)

    def read_power_state(self) -> int:
        return self._read_bounded("power state", self.COMMAND_CONTROL, self.INDEX_POWER_STATE, 0, 2)

    def is_turbo_available(self) -> bool:
        return bool(self._turbo_power_available())

    def set_profile(self, profile: Profile) -> Profile:
        requested = Profile(profile)
        if requested == Profile.TURBO and not self.is_turbo_available():
            raise InsufficientPowerError(TURBO_POWER_MESSAGE)
        last_value = -1
        for _ in range(self._retries):
            self._send_value(self.COMMAND_CONTROL, requested.value)
            self._sleep(self._profile_settle_delay)
            last_value = self._read_index(self.COMMAND_CONTROL, self.INDEX_PROFILE)
            if last_value == requested.value:
                return requested
        raise ProtocolError(
            f"profile did not change to {requested.label} after {self._retries} attempts; "
            f"last readback was 0x{last_value:02x}"
        )

    def read_full_speed(self) -> bool:
        value = self._read_bounded("full-speed status", self.COMMAND_CONTROL, self.INDEX_FULL_SPEED, 1, 2)
        return value == 1

    def set_full_speed(self, enabled: bool) -> bool:
        action = self.ACTION_FULL_SPEED_ENABLE if enabled else self.ACTION_FULL_SPEED_DISABLE
        for _ in range(2):
            self._send_value(self.COMMAND_CONTROL, action, post_delay=self._io_delay)

        actual = self.read_full_speed()
        if actual != enabled:
            raise ProtocolError(f"full-speed readback is {'enabled' if actual else 'disabled'}")
        return actual

    def _read_u16(self, command: int, low_index: int, high_index: int) -> int:
        low = self._read_index(command, low_index)
        high = self._read_index(command, high_index)
        return (high << 8) | low

    def read_gpu_fan_rpm(self) -> int:
        return self._read_u16(self.COMMAND_FAN_SPEED, self.INDEX_GPU_FAN_LOW, self.INDEX_GPU_FAN_HIGH)

    def read_cpu_fan_rpm(self) -> int:
        return self._read_u16(self.COMMAND_FAN_SPEED, self.INDEX_CPU_FAN_LOW, self.INDEX_CPU_FAN_HIGH)

    def read_gpu_temperature(self) -> int:
        return self._read_bounded(
            "GPU temperature",
            self.COMMAND_TEMPERATURE,
            self.INDEX_GPU_TEMPERATURE,
            10,
            200,
        )

    def read_cpu_temperature(self) -> int:
        return self._read_bounded(
            "CPU temperature",
            self.COMMAND_TEMPERATURE,
            self.INDEX_CPU_TEMPERATURE,
            10,
            200,
        )

    def read_status(self) -> FanStatus:
        turbo_available = self.is_turbo_available()
        return FanStatus(
            profile=self.read_profile(),
            full_speed=self.read_full_speed(),
            cpu_fan_rpm=self.read_cpu_fan_rpm(),
            gpu_fan_rpm=self.read_gpu_fan_rpm(),
            cpu_temperature_c=self.read_cpu_temperature(),
            gpu_temperature_c=self.read_gpu_temperature(),
            turbo_available=turbo_available,
        )