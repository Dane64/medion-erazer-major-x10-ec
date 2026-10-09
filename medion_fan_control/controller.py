from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from typing import Protocol

from .hardware import HardwareAccessError, UnsupportedHardwareError
from .protocol import FanStatus, InsufficientPowerError, Profile, ProtocolError


SESSION_ERRORS = (HardwareAccessError, UnsupportedHardwareError, ProtocolError, OSError)


class FanController(Protocol):
    def read_status(self) -> FanStatus: ...

    def set_profile(self, profile: Profile) -> Profile: ...

    def set_full_speed(self, enabled: bool) -> bool: ...


class ProtocolSession:
    """Own a controller context; call all methods on the same worker thread."""

    def __init__(self, factory: Callable[[], AbstractContextManager[FanController]]) -> None:
        self._factory = factory
        self._context: ExitStack | None = None
        self._controller: FanController | None = None

    def open(self) -> FanStatus:
        self.close()
        with ExitStack() as stack:
            controller = stack.enter_context(self._factory())
            status = controller.read_status()
            self._controller = controller
            self._context = stack.pop_all()
        return status

    def close(self) -> None:
        context, self._context = self._context, None
        self._controller = None
        if context is not None:
            context.close()

    @contextmanager
    def _operation(self) -> Iterator[FanController]:
        if self._controller is None:
            raise HardwareAccessError("hardware session is not open; reconnect before making changes")
        try:
            yield self._controller
        except InsufficientPowerError:
            raise
        except SESSION_ERRORS:
            self.close()
            raise

    def read_status(self) -> FanStatus:
        with self._operation() as controller:
            return controller.read_status()

    def set_profile(self, profile: Profile) -> FanStatus:
        with self._operation() as controller:
            controller.set_profile(profile)
            return controller.read_status()

    def set_full_speed(self, enabled: bool) -> FanStatus:
        with self._operation() as controller:
            controller.set_full_speed(enabled)
            return controller.read_status()


class DemoController:
    def __init__(self) -> None:
        self.profile = Profile.OFFICE
        self.full_speed = False
        self._started_at = time.monotonic()

    def read_status(self) -> FanStatus:
        elapsed = time.monotonic() - self._started_at
        profile_offset = (self.profile.value - 1) * 450
        cpu_temperature = round(58 + 11 * math.sin(elapsed / 11))
        gpu_temperature = round(50 + 9 * math.sin(elapsed / 14 + 0.8))
        if self.full_speed:
            cpu_rpm, gpu_rpm = 5900, 5600
        else:
            cpu_rpm = max(1250, round(1150 + cpu_temperature * 28 + profile_offset))
            gpu_rpm = max(1100, round(1000 + gpu_temperature * 26 + profile_offset))
        return FanStatus(
            profile=self.profile,
            full_speed=self.full_speed,
            cpu_fan_rpm=cpu_rpm,
            gpu_fan_rpm=gpu_rpm,
            cpu_temperature_c=cpu_temperature,
            gpu_temperature_c=gpu_temperature,
        )

    def set_profile(self, profile: Profile) -> Profile:
        self.profile = Profile(profile)
        return self.profile

    def set_full_speed(self, enabled: bool) -> bool:
        self.full_speed = bool(enabled)
        return self.full_speed


@contextmanager
def open_demo_protocol() -> Iterator[DemoController]:
    yield DemoController()
