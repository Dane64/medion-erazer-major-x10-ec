from __future__ import annotations

import math
import time

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPaintEvent, QPen, QResizeEvent
from PySide6.QtWidgets import QCheckBox, QGridLayout, QSizePolicy, QWidget

from .telemetry import TelemetryHistory, TelemetrySample, format_duration
from .theme import (
    ACCENT, ACCENT_HI, BORDER, BRAND_FONT, CPU_COLOR, GPU_COLOR, GRID, INK, MUTED, SURFACE, SURFACE_HI,
)


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

        painter.setFont(QFont(BRAND_FONT, 8))
        painter.setPen(QColor(MUTED))
        painter.drawText(0, 0, 54, 18, Qt.AlignmentFlag.AlignRight, "RPM")
        painter.drawText(int(right + 10), 0, 42, 18, Qt.AlignmentFlag.AlignLeft, "°C")
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

        painter.setPen(QPen(QColor(MUTED), 1))
        painter.drawLine(QPointF(left, top), QPointF(left, bottom))
        painter.drawLine(QPointF(left, bottom), QPointF(right, bottom))
        painter.drawLine(QPointF(right, top), QPointF(right, bottom))

        if not samples:
            painter.setPen(QColor(MUTED))
            painter.setFont(QFont(BRAND_FONT, 10, QFont.Weight.Bold))
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
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        if last_point is not None:
            painter.setBrush(QColor(color))
            painter.setPen(QPen(QColor(SURFACE), 1))
            painter.drawEllipse(last_point, 4, 4)


class ValueSlider(QWidget):
    """Slider with a caption and a live value readout."""

    def __init__(
        self,
        caption: str,
        minimum: int,
        maximum: int,
        value: int,
        *,
        unit: str = "",
        step: int = 1,
        formatter=None,
        parent: QWidget | None = None,
    ) -> None:
        from PySide6.QtWidgets import QHBoxLayout, QLabel, QSlider

        super().__init__(parent)
        self._step = max(1, step)
        self._unit = unit
        self._formatter = formatter or (lambda v: f"{v:,}{(' ' + unit) if unit else ''}")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.caption = QLabel(caption)
        self.caption.setMinimumWidth(150)
        layout.addWidget(self.caption)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(minimum // self._step, maximum // self._step)
        self.slider.setValue(value // self._step)
        self.slider.setAccessibleName(caption)
        layout.addWidget(self.slider, 1)
        self.readout = QLabel()
        self.readout.setMinimumWidth(90)
        self.readout.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.readout.setObjectName("metricName")
        layout.addWidget(self.readout)
        self.slider.valueChanged.connect(self._update)
        self._update()

    def _update(self) -> None:
        self.readout.setText(self._formatter(self.value()))

    def value(self) -> int:
        return self.slider.value() * self._step

    def setValue(self, value: int) -> None:  # noqa: N802 - Qt naming
        self.slider.setValue(value // self._step)


class ToggleSwitch(QCheckBox):
    """On/off switch with a sliding knob, drawn in the Erazer palette."""

    TRACK_W, TRACK_H = 38, 20

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        text_width = self.fontMetrics().horizontalAdvance(self.text()) + 10 if self.text() else 0
        return QSize(self.TRACK_W + 4 + text_width, max(self.TRACK_H + 4, self.fontMetrics().height() + 4))

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return self.sizeHint()

    def hitButton(self, pos) -> bool:  # noqa: N802
        return self.rect().contains(pos)

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        top = (self.height() - self.TRACK_H) / 2
        track = QRectF(2, top, self.TRACK_W, self.TRACK_H)
        enabled = self.isEnabled()
        on = self.isChecked()
        track_color = QColor(ACCENT if on else BORDER)
        if not enabled:
            track_color = QColor("#26324a" if on else SURFACE_HI)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(track_color)
        painter.drawRoundedRect(track, self.TRACK_H / 2, self.TRACK_H / 2)
        knob = self.TRACK_H - 6
        x = track.right() - knob - 3 if on else track.left() + 3
        painter.setBrush(QColor(INK if enabled else MUTED))
        painter.drawEllipse(QRectF(x, top + 3, knob, knob))
        if self.hasFocus():
            painter.setPen(QPen(QColor(ACCENT_HI), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(track.adjusted(-1, -1, 1, 1), self.TRACK_H / 2, self.TRACK_H / 2)
        if self.text():
            painter.setPen(QColor(INK if enabled else MUTED))
            font = painter.font()
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(
                QRectF(track.right() + 10, 0, self.width() - track.right() - 10, self.height()),
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), self.text(),
            )
