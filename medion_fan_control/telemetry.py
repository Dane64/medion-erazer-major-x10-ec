from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

from .protocol import FanStatus


@dataclass(frozen=True)
class TelemetrySample:
    recorded_at: float
    status: FanStatus


class TelemetryHistory:
    def __init__(self, max_samples: int = 480) -> None:
        if max_samples < 1:
            raise ValueError("max_samples must be at least 1")
        self._samples: deque[TelemetrySample] = deque(maxlen=max_samples)

    @property
    def samples(self) -> tuple[TelemetrySample, ...]:
        return tuple(self._samples)

    def append(self, status: FanStatus, recorded_at: float | None = None) -> None:
        self._samples.append(TelemetrySample(time.monotonic() if recorded_at is None else recorded_at, status))

    def samples_for_window(
        self,
        duration_seconds: int,
        *,
        now: float | None = None,
    ) -> tuple[TelemetrySample, ...]:
        if duration_seconds < 1:
            raise ValueError("duration must be at least 1 second")
        cutoff = (time.monotonic() if now is None else now) - duration_seconds
        return tuple(sample for sample in self._samples if sample.recorded_at >= cutoff)


def format_duration(seconds: int) -> str:
    minutes, remaining_seconds = divmod(seconds, 60)
    if not minutes:
        return f"{remaining_seconds}s"
    if not remaining_seconds:
        return f"{minutes} min"
    return f"{minutes}m {remaining_seconds}s"
