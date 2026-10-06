from __future__ import annotations

import math
import time

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPaintEvent, QPen, QResizeEvent
from PySide6.QtWidgets import QGridLayout, QSizePolicy, QWidget

from .telemetry import TelemetryHistory, TelemetrySample, format_duration
from .theme import CPU_COLOR, GPU_COLOR, GRID, INK, MUTED, SURFACE


class ResponsiveGrid(QWidget):
    def __init__(
        self,
        widgets: list[QWidget],
        *,
        wide_columns: int,
        medium_columns: int = 2,
        medium_width: int = 720,
        wide_width: int = 1080,
    ) -> None:
        super().__init__()
        self._widgets = widgets
        self._wide_columns = wide_columns
        self._medium_columns = medium_columns
        self._medium_width = medium_width
        self._wide_width = wide_width
        self._columns = 0
        self._layout = QGridLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setHorizontalSpacing(12)
        self._layout.setVerticalSpacing(12)
        self._reflow(wide_columns)

    def resizeEvent(self, event: QResizeEvent) -> None:
        width = event.size().width()
        columns = (
            self._wide_columns
            if width >= self._wide_width
            else self._medium_columns
            if width >= self._medium_width
            else 1
        )
        self._reflow(columns)
        super().resizeEvent(event)

    def _reflow(self, columns: int) -> None:
        if columns == self._columns:
            return
        self._columns = columns
        for index, widget in enumerate(self._widgets):
            self._layout.addWidget(widget, index // columns, index % columns)
        for column in range(self._wide_columns):
            self._layout.setColumnStretch(column, 1 if column < columns else 0)


class TelemetryGraph(QWidget):
    def __init__(self, history: TelemetryHistory, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._history = history
        self._window_seconds = 180
        self.setMinimumHeight(280)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAccessibleName("Fan speed and temperature history")
        self.setAccessibleDescription(
            "CPU and GPU fan speeds use the left RPM axis. Temperatures use the right Celsius axis. "
            "Current readings are also displayed in the telemetry cards."
        )

    def set_window_seconds(self, seconds: int) -> None:
        if seconds < 1:
            raise ValueError("graph window must be at least 1 second")
        self._window_seconds = seconds
        self.update()

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(SURFACE))
        left, top, right, bottom = 64.0, 24.0, self.width() - 64.0, self.height() - 44.0
        if right <= left or bottom <= top:
            return

        now = time.monotonic()
        duration = self._window_seconds
        samples = self._history.samples_for_window(duration, now=now)
        maximum_rpm = max(
            (max(sample.status.cpu_fan_rpm, sample.status.gpu_fan_rpm) for sample in samples),
            default=5000,
        )
        fan_max = max(6000, math.ceil(maximum_rpm / 1000) * 1000)
        maximum_temperature = max(
            (max(sample.status.cpu_temperature_c, sample.status.gpu_temperature_c) for sample in samples),
            default=100,
        )
        temperature_max = max(100, math.ceil(maximum_temperature / 20) * 20)

        painter.setFont(QFont("DejaVu Sans", 8))
        painter.setPen(QColor(MUTED))
        painter.drawText(0, 0, 54, 18, Qt.AlignmentFlag.AlignRight, "RPM")
        painter.drawText(int(right + 10), 0, 42, 18, Qt.AlignmentFlag.AlignLeft, "C")
        for step in range(5):
            fraction = step / 4
            y = bottom - fraction * (bottom - top)
            painter.setPen(QPen(QColor(GRID), 1))
            painter.drawLine(QPointF(left, y), QPointF(right, y))
            painter.setPen(QColor(MUTED))
            painter.drawText(
                0, int(y - 10), int(left - 10), 20,
                Qt.AlignmentFlag.AlignRight, f"{round(fraction * fan_max):,}",
            )
            painter.drawText(
                int(right + 10), int(y - 10), 42, 20,
                Qt.AlignmentFlag.AlignLeft, str(round(fraction * temperature_max)),
            )

        for step in range(5):
            fraction = step / 4
            x = left + fraction * (right - left)
            age = round(duration * (1 - fraction))
            painter.setPen(QPen(QColor(GRID), 1))
            painter.drawLine(QPointF(x, top), QPointF(x, bottom))
            painter.setPen(QColor(MUTED))
            label = "NOW" if age == 0 else f"-{format_duration(age)}"
            painter.drawText(int(x - 40), int(bottom + 8), 80, 20, Qt.AlignmentFlag.AlignCenter, label)

        painter.setPen(QPen(QColor(INK), 2))
        painter.drawLine(QPointF(left, top), QPointF(left, bottom))
        painter.drawLine(QPointF(left, bottom), QPointF(right, bottom))
        painter.drawLine(QPointF(right, top), QPointF(right, bottom))

        if not samples:
            painter.setPen(QColor(MUTED))
            painter.setFont(QFont("DejaVu Sans", 10, QFont.Weight.Bold))
            painter.drawText(
                int(left), int(top), int(right - left), int(bottom - top),
                Qt.AlignmentFlag.AlignCenter, "WAITING FOR TELEMETRY",
            )
            return

        start = now - duration
        for field, color, value_max, dashed in (
            ("cpu_fan_rpm", CPU_COLOR, fan_max, False),
            ("gpu_fan_rpm", GPU_COLOR, fan_max, False),
            ("cpu_temperature_c", CPU_COLOR, temperature_max, True),
            ("gpu_temperature_c", GPU_COLOR, temperature_max, True),
        ):
            self._draw_series(
                painter, samples, field, color, start, duration, left, top, right, bottom, value_max,
                dashed=dashed,
            )

    @staticmethod
    def _draw_series(
        painter: QPainter,
        samples: tuple[TelemetrySample, ...],
        field: str,
        color: str,
        start: float,
        duration: int,
        left: float,
        top: float,
        right: float,
        bottom: float,
        value_max: int,
        *,
        dashed: bool = False,
    ) -> None:
        path = QPainterPath()
        last_point: QPointF | None = None
        for index, sample in enumerate(samples):
            value = getattr(sample.status, field)
            point = QPointF(
                left + (sample.recorded_at - start) / duration * (right - left),
                bottom - min(value, value_max) / value_max * (bottom - top),
            )
            if index == 0:
                path.moveTo(point)
            else:
                path.lineTo(point)
            last_point = point
        pen = QPen(QColor(color), 2)
        if dashed:
            pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.drawPath(path)
        if last_point is not None:
            painter.setBrush(QColor(color))
            painter.setPen(QPen(QColor(SURFACE), 1))
            painter.drawEllipse(last_point, 4, 4)
