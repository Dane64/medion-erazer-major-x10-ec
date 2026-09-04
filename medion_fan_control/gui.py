from __future__ import annotations

import argparse
import math
import sys
import time
from collections import deque
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Protocol

from PySide6.QtCore import QEvent, QObject, QPointF, Qt, QTimer
from PySide6.QtGui import QColor, QCloseEvent, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QColorDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .hardware import HardwareAccessError, open_supported_protocol, read_platform_security
from .lighting import (
    LightingAccessError,
    LightZone,
    RgbColor,
    apply_static_lighting,
    load_lighting_colors,
    save_lighting_colors,
)
from .protocol import FanStatus, InsufficientPowerError, Profile, TURBO_POWER_MESSAGE


BACKGROUND = "#edf1ed"
SURFACE = "#ffffff"
INK = "#17231e"
MUTED = "#64736c"
BORDER = "#d5ded8"
GRID = "#e1e8e3"
SIDEBAR = "#173d34"
SIDEBAR_MUTED = "#a9c0b8"
ACCENT = "#10a477"
CPU_COLOR = "#df5a3f"
GPU_COLOR = "#2475a8"
WARNING = "#e6ad3a"
ERROR = "#bd3f35"
DEFAULT_LIGHTING_COLOR = RgbColor(255, 255, 255)
LIGHTING_ZONE_ORDER = (LightZone.KEYBOARD, LightZone.LID, LightZone.LEFT_SIDE, LightZone.RIGHT_SIDE)


class FanController(Protocol):
    def read_status(self) -> FanStatus: ...

    def set_profile(self, profile: Profile) -> Profile: ...

    def set_full_speed(self, enabled: bool) -> bool: ...


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


def lighting_text_color(color: RgbColor) -> str:
    luminance = (299 * color.red + 587 * color.green + 114 * color.blue) / 1000
    return INK if luminance >= 145 else "#ffffff"


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
            cpu_rpm = 5900
            gpu_rpm = 5600
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


class ProtocolSession:
    def __init__(self, factory: Callable[[], AbstractContextManager[FanController]]) -> None:
        self._factory = factory
        self._context: AbstractContextManager[FanController] | None = None
        self._controller: FanController | None = None

    def open(self) -> FanStatus:
        if self._controller is not None:
            return self._controller.read_status()
        self._context = self._factory()
        self._controller = self._context.__enter__()
        return self._controller.read_status()

    def close(self) -> None:
        if self._context is not None:
            self._context.__exit__(None, None, None)
        self._context = None
        self._controller = None

    def read_status(self) -> FanStatus:
        return self._require_controller().read_status()

    def set_profile(self, profile: Profile) -> FanStatus:
        controller = self._require_controller()
        controller.set_profile(profile)
        return controller.read_status()

    def set_full_speed(self, enabled: bool) -> FanStatus:
        controller = self._require_controller()
        controller.set_full_speed(enabled)
        return controller.read_status()

    def _require_controller(self) -> FanController:
        if self._controller is None:
            raise RuntimeError("hardware session is not open")
        return self._controller


class HoverTabWidget(QTabWidget):
    """Select tabs on pointer hover while retaining normal click/keyboard use."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tabBar().setMouseTracking(True)
        self.tabBar().installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.tabBar() and event.type() == QEvent.Type.MouseMove:
            tab_index = self.tabBar().tabAt(event.position().toPoint())
            if tab_index >= 0:
                self.setCurrentIndex(tab_index)
        return super().eventFilter(watched, event)


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

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
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

    def set_window_seconds(self, seconds: int) -> None:
        self._window_seconds = seconds
        self.update()

    def paintEvent(self, _event) -> None:  # type: ignore[no-untyped-def]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(SURFACE))
        width = self.width()
        height = self.height()
        left, top, right, bottom = 64.0, 24.0, width - 64.0, height - 44.0
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
        for step in range(5):
            fraction = step / 4
            y = bottom - fraction * (bottom - top)
            painter.setPen(QPen(QColor(GRID), 1))
            painter.drawLine(QPointF(left, y), QPointF(right, y))
            painter.setPen(QColor(MUTED))
            painter.drawText(0, int(y - 10), int(left - 10), 20, Qt.AlignmentFlag.AlignRight, f"{round(fraction * fan_max):,}")
            painter.drawText(int(right + 10), int(y - 10), 42, 20, Qt.AlignmentFlag.AlignLeft, str(round(fraction * temperature_max)))

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
                int(left),
                int(top),
                int(right - left),
                int(bottom - top),
                Qt.AlignmentFlag.AlignCenter,
                "WAITING FOR EC TELEMETRY",
            )
            return

        start = now - duration
        self._draw_series(painter, samples, "cpu_fan_rpm", CPU_COLOR, start, duration, left, top, right, bottom, fan_max)
        self._draw_series(painter, samples, "gpu_fan_rpm", GPU_COLOR, start, duration, left, top, right, bottom, fan_max)
        self._draw_series(
            painter,
            samples,
            "cpu_temperature_c",
            CPU_COLOR,
            start,
            duration,
            left,
            top,
            right,
            bottom,
            temperature_max,
            dashed=True,
        )
        self._draw_series(
            painter,
            samples,
            "gpu_temperature_c",
            GPU_COLOR,
            start,
            duration,
            left,
            top,
            right,
            bottom,
            temperature_max,
            dashed=True,
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
            path.moveTo(point) if index == 0 else path.lineTo(point)
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


class FanControlApp(QMainWindow):
    POLL_INTERVAL_MS = 1500

    def __init__(self, *, demo: bool = False) -> None:
        super().__init__()
        self.demo = demo
        self.history = TelemetryHistory()
        factory = open_demo_protocol if demo else open_supported_protocol
        self.session = ProtocolSession(factory)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="medion-ec")
        self.lighting_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="medion-lighting")
        self._pending: Future[FanStatus] | None = None
        self._lighting_pending: Future[None] | None = None
        self._closing = False
        self._current_status: FanStatus | None = None
        self._mode_buttons: dict[Profile, QRadioButton] = {}
        self._lighting_swatches: dict[LightZone, QPushButton] = {}
        self._lighting_load_error: str | None = None
        try:
            self._lighting_colors = load_lighting_colors()
        except LightingAccessError as error:
            self._lighting_colors = {}
            self._lighting_load_error = str(error)

        self._poll_timer = QTimer(self)
        self._poll_timer.setSingleShot(True)
        self._poll_timer.setInterval(self.POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._request_status)

        self._configure_window()
        self._build_layout()
        self._set_controls_enabled(False)
        self._update_lighting_controls()
        QTimer.singleShot(0, self._open_session)

    def _configure_window(self) -> None:
        self.setWindowTitle("Medion Major X10 Control")
        self.resize(1180, 820)
        self.setMinimumSize(720, 560)
        self.setStyleSheet(
            f"""
            QMainWindow, QWidget#root {{ background: {BACKGROUND}; color: {INK}; }}
            QLabel {{ color: {INK}; }}
            QLabel#brand {{ color: white; font-size: 22px; font-weight: 800; }}
            QLabel#subtitle, QLabel#status, QLabel[role="muted"] {{ color: {MUTED}; }}
            QLabel#title {{ font-size: 25px; font-weight: 800; }}
            QLabel#sectionTitle {{ font-size: 16px; font-weight: 700; }}
            QLabel#metricName {{ color: {MUTED}; font-size: 10px; font-weight: 700; }}
            QLabel#metricValue {{ color: {INK}; font: 700 23px "DejaVu Sans Mono"; }}
            QFrame[panel="true"] {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 12px; }}
            QPushButton, QRadioButton {{
                border: 1px solid {BORDER}; border-radius: 8px; padding: 9px 14px;
                background: {SURFACE}; color: {INK}; font-weight: 600;
            }}
            QPushButton:hover, QRadioButton:hover {{ border-color: {ACCENT}; background: #f3faf7; }}
            QRadioButton::indicator {{ width: 0; height: 0; }}
            QRadioButton:checked {{ background: {SIDEBAR}; color: white; border-color: {SIDEBAR}; }}
            QPushButton[role="primary"] {{ background: {ACCENT}; color: white; border-color: {ACCENT}; }}
            QPushButton[role="primary"]:hover {{ background: #0c8c65; }}
            QPushButton:disabled, QRadioButton:disabled {{ color: #98a49f; background: #edf0ee; }}
            QCheckBox {{ spacing: 8px; font-weight: 600; }}
            QTabWidget::pane {{ border: 0; background: transparent; }}
            QTabBar::tab {{
                background: transparent; color: {MUTED}; padding: 12px 22px;
                border-bottom: 3px solid transparent; font-weight: 700;
            }}
            QTabBar::tab:hover {{ color: {ACCENT}; background: #e5eee9; }}
            QTabBar::tab:selected {{ color: {INK}; border-bottom-color: {ACCENT}; }}
            QSlider::groove:horizontal {{ height: 5px; background: {GRID}; border-radius: 2px; }}
            QSlider::handle:horizontal {{
                background: {ACCENT}; width: 16px; margin: -6px 0; border-radius: 8px;
            }}
            QScrollArea {{ border: 0; background: transparent; }}
            """
        )

    def _build_layout(self) -> None:
        root = QWidget(objectName="root")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(24, 20, 24, 14)
        root_layout.setSpacing(12)

        header = QHBoxLayout()
        brand = QLabel("MEDION  MAJOR X10", objectName="brand")
        brand.setStyleSheet(f"background: {SIDEBAR}; border-radius: 9px; padding: 10px 16px;")
        header.addWidget(brand)
        heading = QVBoxLayout()
        heading.setSpacing(0)
        heading.addWidget(QLabel("System control", objectName="title"))
        heading.addWidget(QLabel("Verified firmware telemetry, modes, and lighting", objectName="subtitle"))
        header.addLayout(heading)
        header.addStretch()
        self.profile_badge = QLabel("UNKNOWN MODE")
        self.profile_badge.setStyleSheet(
            f"background: {WARNING}; color: {INK}; border-radius: 8px; padding: 8px 12px; font-weight: 700;"
        )
        header.addWidget(self.profile_badge)
        root_layout.addLayout(header)

        self.tabs = HoverTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._scrollable_page(self._build_dashboard()), "Dashboard")
        self.tabs.addTab(self._scrollable_page(self._build_lighting_page()), "Lighting")
        self.tabs.setTabToolTip(0, "Hover to show fan controls and telemetry")
        self.tabs.setTabToolTip(1, "Hover to show static lighting controls")
        root_layout.addWidget(self.tabs, 1)

        footer = QHBoxLayout()
        self.connection_label = QLabel("CONNECTING")
        self.connection_label.setStyleSheet(f"color: {SIDEBAR}; font: 700 10px 'DejaVu Sans Mono';")
        footer.addWidget(self.connection_label)
        self.status_label = QLabel("Opening the verified EC transport...", objectName="status")
        self.status_label.setWordWrap(True)
        footer.addWidget(self.status_label, 1)
        self.retry_button = QPushButton("Retry connection")
        self.retry_button.clicked.connect(self._open_session)
        footer.addWidget(self.retry_button)
        root_layout.addLayout(footer)
        self.setCentralWidget(root)

    @staticmethod
    def _scrollable_page(content: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(content)
        return scroll

    def _build_dashboard(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(2, 8, 2, 8)
        layout.setSpacing(14)

        controls = self._panel()
        controls_layout = QHBoxLayout(controls)
        controls_layout.setContentsMargins(18, 15, 18, 15)
        profile_layout = QVBoxLayout()
        profile_layout.addWidget(QLabel("Performance mode", objectName="sectionTitle"))
        profile_buttons = QHBoxLayout()
        group = QButtonGroup(self)
        for profile in Profile:
            button = QRadioButton(profile.label.title())
            button.clicked.connect(lambda _checked=False, selected=profile: self._request_profile_change(selected))
            group.addButton(button)
            profile_buttons.addWidget(button)
            self._mode_buttons[profile] = button
        profile_layout.addLayout(profile_buttons)
        controls_layout.addLayout(profile_layout, 1)
        controls_layout.addSpacing(24)
        override_layout = QVBoxLayout()
        override_layout.addWidget(QLabel("Fan override", objectName="sectionTitle"))
        self.full_speed_check = QCheckBox("Full speed")
        self.full_speed_check.clicked.connect(self._request_full_speed_change)
        override_layout.addWidget(self.full_speed_check)
        controls_layout.addLayout(override_layout)
        layout.addWidget(controls)

        metric_specs = (
            ("CPU FAN", "cpu_rpm_label", "RPM", CPU_COLOR),
            ("GPU FAN", "gpu_rpm_label", "RPM", GPU_COLOR),
            ("CPU TEMP", "cpu_temp_label", "C", CPU_COLOR),
            ("GPU TEMP", "gpu_temp_label", "C", GPU_COLOR),
        )
        metric_cards = []
        for name, attribute, unit, color in metric_specs:
            card, value_label = self._metric_card(name, unit, color)
            setattr(self, attribute, value_label)
            metric_cards.append(card)
        layout.addWidget(ResponsiveGrid(metric_cards, wide_columns=4, medium_width=560, wide_width=920))

        graph_panel = self._panel()
        graph_layout = QVBoxLayout(graph_panel)
        graph_layout.setContentsMargins(18, 16, 18, 14)
        graph_header = QHBoxLayout()
        graph_header.addWidget(QLabel("Fan speed and temperature", objectName="sectionTitle"))
        graph_header.addStretch()
        graph_header.addWidget(QLabel("WINDOW", objectName="metricName"))
        self.window_slider = QSlider(Qt.Orientation.Horizontal)
        self.window_slider.setRange(1, 20)
        self.window_slider.setValue(6)
        self.window_slider.setMaximumWidth(180)
        self.window_slider.valueChanged.connect(self._graph_window_changed)
        graph_header.addWidget(self.window_slider)
        self.window_label = QLabel("3 min", objectName="metricName")
        self.window_label.setMinimumWidth(50)
        graph_header.addWidget(self.window_label)
        graph_layout.addLayout(graph_header)
        legend = QLabel(
            f'<span style="color:{CPU_COLOR}">━━</span> CPU fan &nbsp; '
            f'<span style="color:{GPU_COLOR}">━━</span> GPU fan &nbsp; '
            f'<span style="color:{CPU_COLOR}">┅┅</span> CPU temp &nbsp; '
            f'<span style="color:{GPU_COLOR}">┅┅</span> GPU temp'
        )
        legend.setObjectName("subtitle")
        graph_layout.addWidget(legend)
        self.graph = TelemetryGraph(self.history)
        graph_layout.addWidget(self.graph, 1)
        layout.addWidget(graph_panel, 1)
        return page

    def _build_lighting_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(2, 8, 2, 8)
        layout.setSpacing(14)

        intro = self._panel()
        intro_layout = QHBoxLayout(intro)
        intro_layout.setContentsMargins(18, 16, 18, 16)
        text = QVBoxLayout()
        text.addWidget(QLabel("Static chassis lighting", objectName="sectionTitle"))
        description = QLabel(
            "Choose colors for all zones or tune each zone independently. "
            "Changes are written only after confirmation."
        )
        description.setObjectName("subtitle")
        description.setWordWrap(True)
        text.addWidget(description)
        intro_layout.addLayout(text, 1)
        self.apply_lighting_button = QPushButton("Apply colors")
        self.apply_lighting_button.setProperty("role", "primary")
        self.apply_lighting_button.clicked.connect(self._request_lighting_change)
        intro_layout.addWidget(self.apply_lighting_button)
        layout.addWidget(intro)

        all_button = QPushButton("All zones\nSet color")
        all_button.setMinimumHeight(112)
        all_button.clicked.connect(lambda: self._choose_lighting_color(None))
        self.all_lighting_button = all_button
        zone_buttons: list[QWidget] = [all_button]
        for zone in LIGHTING_ZONE_ORDER:
            swatch = QPushButton()
            swatch.setMinimumHeight(112)
            swatch.clicked.connect(lambda _checked=False, selected=zone: self._choose_lighting_color(selected))
            self._lighting_swatches[zone] = swatch
            zone_buttons.append(swatch)
        self._refresh_lighting_swatches()
        layout.addWidget(
            ResponsiveGrid(
                zone_buttons,
                wide_columns=5,
                medium_columns=2,
                medium_width=520,
                wide_width=980,
            )
        )
        layout.addStretch()
        return page

    @staticmethod
    def _panel() -> QFrame:
        panel = QFrame()
        panel.setProperty("panel", True)
        return panel

    def _metric_card(self, name: str, unit: str, color: str) -> tuple[QFrame, QLabel]:
        card = self._panel()
        layout = QHBoxLayout(card)
        layout.setContentsMargins(14, 13, 14, 13)
        marker = QFrame()
        marker.setFixedWidth(4)
        marker.setStyleSheet(f"background: {color}; border-radius: 2px;")
        layout.addWidget(marker)
        text = QVBoxLayout()
        text.addWidget(QLabel(name, objectName="metricName"))
        row = QHBoxLayout()
        value = QLabel("--", objectName="metricValue")
        row.addWidget(value)
        row.addWidget(QLabel(unit, objectName="metricName"), alignment=Qt.AlignmentFlag.AlignBottom)
        row.addStretch()
        text.addLayout(row)
        layout.addLayout(text, 1)
        return card, value

    def _graph_window_changed(self, step: int) -> None:
        seconds = step * 30
        self.window_label.setText(format_duration(seconds))
        self.graph.set_window_seconds(seconds)

    def _choose_lighting_color(self, zone: LightZone | None) -> None:
        if zone is None:
            selected_colors = set(self._lighting_colors.values())
            current = selected_colors.pop() if len(selected_colors) == 1 else DEFAULT_LIGHTING_COLOR
            title = "All lighting zones"
        else:
            current = self._lighting_colors.get(zone, DEFAULT_LIGHTING_COLOR)
            title = f"{zone.label} color"
        selected = QColorDialog.getColor(QColor(current.hex), self, title)
        if not selected.isValid():
            return
        color = RgbColor(selected.red(), selected.green(), selected.blue())
        target_zones = LIGHTING_ZONE_ORDER if zone is None else (zone,)
        for target_zone in target_zones:
            self._lighting_colors[target_zone] = color
        self._refresh_lighting_swatches()
        self._update_lighting_controls()

    @staticmethod
    def _swatch_style(color: str, text_color: str) -> str:
        return (
            f"QPushButton {{ background: {color}; color: {text_color}; border: 1px solid {BORDER}; "
            "border-radius: 10px; font-weight: 700; }"
            f"QPushButton:hover {{ border: 2px solid {ACCENT}; background: {color}; }}"
        )

    def _refresh_lighting_swatches(self) -> None:
        for zone, swatch in self._lighting_swatches.items():
            color = self._lighting_colors.get(zone)
            if color is None:
                swatch.setText(f"{zone.label}\nChoose color")
                swatch.setStyleSheet(self._swatch_style(BORDER, INK))
                continue
            swatch.setText(f"{zone.label}\n{color.hex.upper()}")
            swatch.setStyleSheet(self._swatch_style(color.hex, lighting_text_color(color)))

        all_colors = set(self._lighting_colors.values())
        if len(self._lighting_colors) == len(LIGHTING_ZONE_ORDER) and len(all_colors) == 1:
            color = all_colors.pop()
            self.all_lighting_button.setText(f"All zones\n{color.hex.upper()}")
            self.all_lighting_button.setStyleSheet(self._swatch_style(color.hex, lighting_text_color(color)))
        else:
            self.all_lighting_button.setText("All zones\nSet color")
            self.all_lighting_button.setStyleSheet(self._swatch_style(SURFACE, INK))

    def _request_lighting_change(self) -> None:
        if self.demo or self._lighting_pending is not None or not self._lighting_colors:
            return
        colors = dict(self._lighting_colors)
        zone_names = ", ".join(zone.label for zone in colors)
        answer = QMessageBox.question(
            self,
            "Apply lighting colors",
            f"Apply static colors to {zone_names}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.status_label.setText(f"Applying static lighting to {zone_names}...")
        self._lighting_pending = self.lighting_executor.submit(apply_static_lighting, colors)
        self._update_lighting_controls()
        self._watch_lighting_future(self._lighting_pending, colors, zone_names, save_on_success=True)

    def _watch_lighting_future(
        self,
        future: Future[None],
        colors: dict[LightZone, RgbColor],
        zone_names: str,
        save_on_success: bool,
    ) -> None:
        if self._closing:
            return
        if not future.done():
            QTimer.singleShot(
                40,
                lambda: self._watch_lighting_future(future, colors, zone_names, save_on_success),
            )
            return
        self._lighting_pending = None
        try:
            future.result()
        except Exception as error:
            self.status_label.setText(str(error))
        else:
            if save_on_success:
                try:
                    save_lighting_colors(colors)
                except LightingAccessError as error:
                    self.status_label.setText(f"Lighting applied, but {error}")
                else:
                    self.status_label.setText(f"Static lighting applied and saved for {zone_names}")
            else:
                self.status_label.setText(f"Saved lighting reapplied to {zone_names}")
        self._update_lighting_controls()

    def _restore_saved_lighting(self) -> None:
        if self.demo or self._lighting_pending is not None or not self._lighting_colors:
            return
        colors = dict(self._lighting_colors)
        zone_names = ", ".join(zone.label for zone in colors)
        self.status_label.setText(f"Reapplying saved lighting to {zone_names}...")
        self._lighting_pending = self.lighting_executor.submit(apply_static_lighting, colors)
        self._update_lighting_controls()
        self._watch_lighting_future(self._lighting_pending, colors, zone_names, save_on_success=False)

    def _update_lighting_controls(self) -> None:
        swatches_enabled = not self.demo and not self._closing and self._lighting_pending is None
        self.all_lighting_button.setEnabled(swatches_enabled)
        for swatch in self._lighting_swatches.values():
            swatch.setEnabled(swatches_enabled)
        self.apply_lighting_button.setEnabled(
            swatches_enabled and bool(self._lighting_colors)
        )

    def _open_session(self) -> None:
        if self._pending is not None or self._closing:
            return
        if not self.demo and not self._platform_security_allows_ec_access():
            return
        self._set_controls_enabled(False)
        self.retry_button.setEnabled(False)
        self.connection_label.setText("CONNECTING")
        self.status_label.setText("Opening the verified EC transport...")
        self._pending = self.executor.submit(self.session.open)
        self._watch_future(self._pending, self._session_opened)

    def _platform_security_allows_ec_access(self) -> bool:
        try:
            security = read_platform_security()
        except HardwareAccessError as error:
            self.connection_label.setText("SECURITY CHECK FAILED")
            self.status_label.setText(str(error))
            QMessageBox.critical(
                self,
                "Cannot verify Secure Boot state",
                "The application could not verify that Secure Boot and kernel lockdown are disabled.\n\n"
                "EC access has been blocked for safety. Resolve the reported system error, then retry.",
            )
            self.retry_button.setEnabled(True)
            return False
        if not security.blocks_ec_access:
            return True

        detected = []
        if security.secure_boot_enabled:
            detected.append("Secure Boot is enabled")
        if security.lockdown_mode not in {None, "none"}:
            detected.append(f"kernel lockdown is in {security.lockdown_mode!r} mode")
        reason = " and ".join(detected)
        self.connection_label.setText("SECURE BOOT ENABLED")
        self.status_label.setText(f"{reason.capitalize()}; EC access is disabled.")
        QMessageBox.critical(
            self,
            "Secure Boot must be disabled",
            f"{reason.capitalize()}. This blocks the direct EC writes required by the application.\n\n"
            "To disable it:\n"
            "1. Restart the laptop and enter UEFI/BIOS setup using the key shown during startup "
            "(commonly F2).\n"
            "2. Open the Security or Boot settings.\n"
            "3. Set Secure Boot to Disabled.\n"
            "4. Save the changes, exit setup, and boot Linux again.\n\n"
            "Disk encryption or another operating system may request a recovery key after this change. "
            "Make sure any required recovery key is available before changing firmware settings.",
        )
        self.retry_button.setEnabled(True)
        return False

    def _watch_future(self, future: Future[FanStatus], callback: Callable[[Future[FanStatus]], None]) -> None:
        if self._closing:
            return
        if future.done():
            self._pending = None
            callback(future)
            return
        QTimer.singleShot(40, lambda: self._watch_future(future, callback))

    def _session_opened(self, future: Future[FanStatus]) -> None:
        try:
            status = future.result()
        except Exception as error:
            self.connection_label.setText("OFFLINE")
            self.status_label.setText(str(error))
            self.retry_button.setEnabled(True)
            return
        self.connection_label.setText("DEMO DATA" if self.demo else "EC CONNECTED")
        self.retry_button.setEnabled(False)
        self._set_controls_enabled(True)
        self._apply_status(status)
        if self._lighting_load_error is not None:
            self.status_label.setText(self._lighting_load_error)
        else:
            self._restore_saved_lighting()
        self._schedule_poll()

    def _schedule_poll(self) -> None:
        if not self._closing:
            self._poll_timer.start()

    def _request_status(self) -> None:
        if self._pending is not None or self._closing:
            self._schedule_poll()
            return
        self._pending = self.executor.submit(self.session.read_status)
        self._watch_future(self._pending, self._status_received)

    def _status_received(self, future: Future[FanStatus]) -> None:
        try:
            self._apply_status(future.result())
        except Exception as error:
            self.connection_label.setText("READ ERROR")
            self.status_label.setText(str(error))
        self._schedule_poll()

    def _request_profile_change(self, profile: Profile) -> None:
        if self._current_status is None or self._pending is not None:
            return
        previous = self._current_status.profile
        if profile == previous:
            return
        if profile == Profile.TURBO and not self._current_status.turbo_available:
            self._mode_buttons[previous].setChecked(True)
            self.status_label.setText(TURBO_POWER_MESSAGE)
            QMessageBox.warning(self, "Turbo mode unavailable", TURBO_POWER_MESSAGE)
            return
        answer = QMessageBox.question(
            self,
            "Change performance mode",
            f"Set the firmware performance mode to {profile.label.title()}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self._mode_buttons[previous].setChecked(True)
            return
        self._submit_change(
            lambda: self.session.set_profile(profile),
            f"Applying {profile.label.title()} mode...",
        )

    def _request_full_speed_change(self) -> None:
        if self._current_status is None or self._pending is not None:
            return
        requested = self.full_speed_check.isChecked()
        previous = self._current_status.full_speed
        action = "enable" if requested else "disable"
        answer = QMessageBox.question(
            self,
            "Change full-speed override",
            f"{action.title()} the firmware full-speed fan override?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self.full_speed_check.setChecked(previous)
            return
        self._submit_change(
            lambda: self.session.set_full_speed(requested),
            f"{action.title()}ing full-speed override...",
        )

    def _submit_change(self, operation: Callable[[], FanStatus], status_text: str) -> None:
        self._poll_timer.stop()
        self._set_controls_enabled(False)
        self.status_label.setText(status_text)
        self._pending = self.executor.submit(operation)
        self._watch_future(self._pending, self._change_completed)

    def _change_completed(self, future: Future[FanStatus]) -> None:
        try:
            status = future.result()
        except InsufficientPowerError as error:
            self.status_label.setText(str(error))
            QMessageBox.warning(self, "Turbo mode unavailable", str(error))
            self._restore_current_controls()
        except Exception as error:
            self.status_label.setText(str(error))
            self._restore_current_controls()
        else:
            self._apply_status(status)
        self._set_controls_enabled(True)
        self._schedule_poll()

    def _restore_current_controls(self) -> None:
        if self._current_status is not None:
            self._mode_buttons[self._current_status.profile].setChecked(True)
            self.full_speed_check.setChecked(self._current_status.full_speed)

    def _apply_status(self, status: FanStatus) -> None:
        self._current_status = status
        self.history.append(status)
        self._mode_buttons[status.profile].setChecked(True)
        self.full_speed_check.setChecked(status.full_speed)
        self.cpu_rpm_label.setText(f"{status.cpu_fan_rpm:,}")
        self.gpu_rpm_label.setText(f"{status.gpu_fan_rpm:,}")
        self.cpu_temp_label.setText(str(status.cpu_temperature_c))
        self.gpu_temp_label.setText(str(status.gpu_temperature_c))
        override = " / FULL SPEED" if status.full_speed else ""
        self.profile_badge.setText(f"{status.profile.label.upper()}{override}")
        self.connection_label.setText("DEMO DATA" if self.demo else "EC CONNECTED")
        self.status_label.setText("Firmware telemetry updated" if status.turbo_available else TURBO_POWER_MESSAGE)
        self.graph.update()

    def _set_controls_enabled(self, enabled: bool) -> None:
        for button in self._mode_buttons.values():
            button.setEnabled(enabled)
        self.full_speed_check.setEnabled(enabled)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._closing:
            event.accept()
            return
        self._closing = True
        self._poll_timer.stop()
        self.executor.submit(self.session.close)
        self.executor.shutdown(wait=False, cancel_futures=False)
        self.lighting_executor.shutdown(wait=False, cancel_futures=False)
        event.accept()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Medion Major X10 fan-control GUI")
    parser.add_argument("--demo", action="store_true", help="show simulated telemetry without hardware access")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    application = QApplication(sys.argv[:1])
    application.setApplicationName("Medion Major X10 Control")
    application.setOrganizationName("Medion Fan Control")
    window = FanControlApp(demo=args.demo)
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
