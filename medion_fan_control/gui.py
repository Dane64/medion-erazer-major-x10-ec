from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QCloseEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QColorDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSlider,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .controller import (
    LIGHTING_ERRORS,
    SESSION_ERRORS,
    ProtocolSession,
    apply_and_save_lighting,
    open_demo_protocol,
)
from .hardware import HardwareAccessError, open_supported_protocol, read_platform_security
from .lighting import (
    LightingAccessError,
    LightZone,
    RgbColor,
    apply_static_lighting,
)
from .protocol import FanStatus, InsufficientPowerError, Profile, TURBO_POWER_MESSAGE
from .settings import load_lighting_colors
from .telemetry import TelemetryHistory, format_duration
from .theme import (
    ACCENT,
    APP_STYLESHEET,
    BORDER,
    CPU_COLOR,
    ERROR,
    GPU_COLOR,
    INK,
    SIDEBAR,
    SURFACE,
    WARNING,
    lighting_text_color,
)
from .widgets import ResponsiveGrid, TelemetryGraph


DEFAULT_LIGHTING_COLOR = RgbColor(255, 255, 255)
LIGHTING_ZONE_ORDER = (LightZone.KEYBOARD, LightZone.LID, LightZone.LEFT_SIDE, LightZone.RIGHT_SIDE)
logger = logging.getLogger(__name__)


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
        self._change_in_progress = False
        self._current_status: FanStatus | None = None
        self._mode_buttons: dict[Profile, QRadioButton] = {}
        self._lighting_swatches: dict[LightZone, QPushButton] = {}
        self._lighting_load_error: str | None = None
        self._lighting_restore_attempted = False
        try:
            self._saved_lighting_colors = {} if demo else load_lighting_colors()
        except LightingAccessError as error:
            self._saved_lighting_colors = {}
            self._lighting_load_error = str(error)
        self._lighting_colors = dict(self._saved_lighting_colors)

        self._poll_timer = QTimer(self)
        self._poll_timer.setSingleShot(True)
        self._poll_timer.setInterval(self.POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._request_status)

        self._configure_window()
        self._build_layout()
        self._set_controls_enabled(False)
        self._update_lighting_controls()
        if self._lighting_load_error is not None:
            self.lighting_status_label.setText(self._lighting_load_error)
        QTimer.singleShot(0, self._open_session)

    def _configure_window(self) -> None:
        title = "Medion Major X10 Control"
        self.setWindowTitle(f"{title} - Demo" if self.demo else title)
        self.resize(1180, 820)
        self.setMinimumSize(720, 560)
        self.setStyleSheet(APP_STYLESHEET)

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
        subtitle = "Demo mode - no hardware access" if self.demo else "Firmware telemetry, modes, and lighting"
        subtitle_label = QLabel(subtitle, objectName="subtitle")
        subtitle_label.setWordWrap(True)
        heading.addWidget(subtitle_label)
        header.addLayout(heading, 1)
        self.profile_badge = QLabel("UNKNOWN MODE")
        self.profile_badge.setWordWrap(True)
        self.profile_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.profile_badge.setStyleSheet(
            f"background: {WARNING}; color: {INK}; border-radius: 8px; padding: 8px 12px; font-weight: 700;"
        )
        header.addWidget(self.profile_badge)
        root_layout.addLayout(header)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._scrollable_page(self._build_dashboard()), "Dashboard")
        self.tabs.addTab(self._scrollable_page(self._build_lighting_page()), "Lighting")
        self.tabs.setTabToolTip(0, "Fan controls and telemetry")
        self.tabs.setTabToolTip(1, "Static lighting controls")
        root_layout.addWidget(self.tabs, 1)

        footer = QHBoxLayout()
        self.connection_label = QLabel("CONNECTING")
        self.connection_label.setStyleSheet(f"color: {SIDEBAR}; font: 700 10px 'DejaVu Sans Mono';")
        footer.addWidget(self.connection_label)
        self.status_label = QLabel("Opening the verified EC transport...", objectName="status")
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
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
        page = QWidget(objectName="page")
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
        self.power_label = QLabel("Power supply: unknown")
        self.power_label.setProperty("role", "muted")
        self.power_label.setWordWrap(True)
        profile_layout.addWidget(self.power_label)
        controls_layout.addLayout(profile_layout, 1)
        controls_layout.addSpacing(24)
        override_layout = QVBoxLayout()
        override_layout.addWidget(QLabel("Fan override", objectName="sectionTitle"))
        self.full_speed_check = QCheckBox("Full speed")
        self.full_speed_check.setToolTip(
            "Runs both fans at full speed. Turn this off to resume the selected firmware profile. "
            "Closing the app does not turn it off."
        )
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
        self.window_slider.setAccessibleName("Telemetry time window")
        self.window_slider.setToolTip("Show the last 30 seconds to 10 minutes of telemetry")
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
        page = QWidget(objectName="page")
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
            "Apply colors asks for confirmation and saves the selection for the next launch."
            if not self.demo else
            "Preview colors for all zones or tune each zone independently. "
            "Demo mode never changes hardware or saved settings."
        )
        description.setObjectName("subtitle")
        description.setWordWrap(True)
        text.addWidget(description)
        self.lighting_status_label = QLabel(
            "Saved colors are restored once when the hardware session connects."
            if not self.demo else "Demo lighting preview - nothing will be written or saved."
        )
        self.lighting_status_label.setWordWrap(True)
        self.lighting_status_label.setTextFormat(Qt.TextFormat.PlainText)
        text.addWidget(self.lighting_status_label)
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
        self.lighting_status_label.setText("Unapplied colors. Select Apply colors to confirm the selection.")
        self._update_lighting_controls()

    @staticmethod
    def _swatch_style(color: str, text_color: str) -> str:
        return (
            f"QPushButton {{ background: {color}; color: {text_color}; border: 1px solid {BORDER}; "
            "border-radius: 10px; font-weight: 700; }"
            f"QPushButton:hover {{ border: 2px solid {ACCENT}; background: {color}; }}"
            f"QPushButton:focus {{ outline: 2px solid {INK}; outline-offset: 2px; }}"
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
        if self._closing or self._lighting_pending is not None or not self._lighting_colors:
            return
        colors = dict(self._lighting_colors)
        zone_names = ", ".join(zone.label for zone in colors)
        answer = QMessageBox.question(
            self,
            "Apply lighting colors",
            f"Preview static colors for {zone_names}? No hardware or settings will change."
            if self.demo else
            f"Apply static colors to {zone_names} and save them to restore at the next launch?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self.demo:
            self.lighting_status_label.setText(f"Demo colors applied to {zone_names}; nothing was written or saved.")
            return
        self._lighting_restore_attempted = True
        self.lighting_status_label.setText(f"Applying static lighting to {zone_names}...")
        self._lighting_pending = self.lighting_executor.submit(apply_and_save_lighting, colors)
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
        except LIGHTING_ERRORS as error:
            self.lighting_status_label.setText(str(error))
        else:
            if save_on_success:
                self._saved_lighting_colors = dict(colors)
                self.lighting_status_label.setText(f"Static lighting applied and saved for {zone_names}")
            else:
                self.lighting_status_label.setText(f"Saved lighting restored for {zone_names}")
        self._update_lighting_controls()

    def _restore_saved_lighting(self) -> None:
        if (
            self.demo or self._closing or self._lighting_restore_attempted
            or self._lighting_pending is not None or not self._saved_lighting_colors
        ):
            return
        self._lighting_restore_attempted = True
        colors = dict(self._saved_lighting_colors)
        zone_names = ", ".join(zone.label for zone in colors)
        self.lighting_status_label.setText(f"Restoring saved lighting for {zone_names}...")
        self._lighting_pending = self.lighting_executor.submit(apply_static_lighting, colors)
        self._update_lighting_controls()
        self._watch_lighting_future(self._lighting_pending, colors, zone_names, save_on_success=False)

    def _update_lighting_controls(self) -> None:
        swatches_enabled = not self._closing and self._lighting_pending is None
        self.all_lighting_button.setEnabled(swatches_enabled)
        for swatch in self._lighting_swatches.values():
            swatch.setEnabled(swatches_enabled)
        self.apply_lighting_button.setEnabled(
            swatches_enabled and bool(self._lighting_colors)
        )

    def _open_session(self) -> None:
        if self._pending is not None or self._closing:
            return
        self._poll_timer.stop()
        self._set_controls_enabled(False)
        if not self.demo and not self._platform_security_allows_ec_access():
            return
        self.retry_button.setEnabled(False)
        self.connection_label.setText("CONNECTING")
        self.status_label.setStyleSheet("")
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
        self.connection_label.setText("EC ACCESS BLOCKED")
        self.status_label.setText(f"{reason.capitalize()}; EC access is disabled.")
        guidance = (
            "Secure Boot can be disabled in UEFI/BIOS setup (commonly F2 at startup): "
            "open Security or Boot, set Secure Boot to Disabled, then save and reboot.\n\n"
            "Disabling Secure Boot reduces boot protection. Have any disk-encryption recovery keys "
            "available before changing firmware settings."
            if security.secure_boot_enabled else
            "Kernel lockdown can also be enabled independently of Secure Boot. "
            "Review your distribution's kernel and boot policy; the app cannot bypass it."
        )
        QMessageBox.critical(
            self,
            "EC access blocked",
            f"{reason.capitalize()}. This blocks the direct EC writes required by the application.\n\n"
            f"{guidance}\n\nUse --demo to explore the app without changing your system configuration.",
        )
        self.retry_button.setEnabled(True)
        return False

    def _watch_future(self, future: Future[FanStatus], callback: Callable[[Future[FanStatus]], None]) -> None:
        if self._closing or future is not self._pending:
            return
        if future.done():
            self._pending = None
            callback(future)
            return
        QTimer.singleShot(40, lambda: self._watch_future(future, callback))

    def _session_opened(self, future: Future[FanStatus]) -> None:
        try:
            status = future.result()
        except SESSION_ERRORS as error:
            self._connection_failed(str(error))
            return
        self.connection_label.setText("DEMO DATA" if self.demo else "EC CONNECTED")
        self.retry_button.setEnabled(False)
        self._apply_status(status)
        self._set_controls_enabled(True)
        self.status_label.setText(
            "Demo mode: hardware and saved settings are untouched." if self.demo else
            "Connected. Closing the app leaves the firmware in its last selected state."
        )
        if self._lighting_load_error is None:
            self._restore_saved_lighting()
        self._schedule_poll()

    def _schedule_poll(self) -> None:
        if not self._closing and self._current_status is not None:
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
        except SESSION_ERRORS as error:
            self._connection_failed(f"Telemetry unavailable: {error}")
            return
        self._set_controls_enabled(True)
        self._schedule_poll()

    def _connection_failed(self, message: str) -> None:
        self._poll_timer.stop()
        self._current_status = None
        self._change_in_progress = False
        self._set_controls_enabled(False)
        self.connection_label.setText("OFFLINE")
        self.status_label.setStyleSheet(f"color: {ERROR};")
        self.status_label.setText(f"{message} Retry the connection before making further changes.")
        self.profile_badge.setText("UNAVAILABLE")
        self.power_label.setText("Power supply: unknown")
        for label in (self.cpu_rpm_label, self.gpu_rpm_label, self.cpu_temp_label, self.gpu_temp_label):
            label.setText("--")
        self.retry_button.setEnabled(True)
        self.graph.update()

    def _request_profile_change(self, profile: Profile) -> None:
        if self._current_status is None or self._change_in_progress or self._closing:
            self._restore_current_controls()
            return
        previous = self._current_status.profile
        self._restore_current_controls()
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
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self._restore_current_controls()
            return
        self._submit_change(
            lambda: self.session.set_profile(profile),
            f"Applying {profile.label.title()} mode...",
        )

    def _request_full_speed_change(self) -> None:
        if self._current_status is None or self._change_in_progress or self._closing:
            self._restore_current_controls()
            return
        requested = self.full_speed_check.isChecked()
        self._restore_current_controls()
        action = "enable" if requested else "disable"
        answer = QMessageBox.question(
            self,
            "Change full-speed override",
            f"{action.title()} the firmware full-speed fan override?\n\n"
            "Closing the app leaves this setting unchanged.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self._restore_current_controls()
            return
        self._submit_change(
            lambda: self.session.set_full_speed(requested),
            "Enabling full-speed override..." if requested else "Disabling full-speed override...",
        )

    def _submit_change(self, operation: Callable[[], FanStatus], status_text: str) -> None:
        self._poll_timer.stop()
        self._change_in_progress = True
        self._set_controls_enabled(False)
        self.status_label.setStyleSheet("")
        self.status_label.setText(status_text)
        self._pending = self.executor.submit(operation)
        self._watch_future(self._pending, self._change_completed)

    def _change_completed(self, future: Future[FanStatus]) -> None:
        self._change_in_progress = False
        try:
            status = future.result()
        except InsufficientPowerError as error:
            self.status_label.setText(str(error))
            QMessageBox.warning(self, "Turbo mode unavailable", str(error))
            if self._current_status is not None:
                self._current_status = replace(self._current_status, turbo_available=False)
                self._update_power_status()
            self._restore_current_controls()
        except SESSION_ERRORS as error:
            self._connection_failed(f"Change could not be confirmed: {error}")
            return
        else:
            self._apply_status(status)
            self.status_label.setText("Demo setting updated." if self.demo else "Firmware change confirmed.")
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
        self._update_power_status()
        self.graph.update()

    def _update_power_status(self) -> None:
        if self._current_status is not None:
            self.power_label.setText(
                "Power supply: simulated" if self.demo else
                "AC barrel adapter connected" if self._current_status.turbo_available else
                "Turbo requires the AC barrel adapter; USB-C PD is insufficient."
            )

    def _set_controls_enabled(self, enabled: bool) -> None:
        for profile, button in self._mode_buttons.items():
            turbo_blocked = (
                profile == Profile.TURBO
                and self._current_status is not None
                and not self._current_status.turbo_available
            )
            button.setEnabled(enabled and not turbo_blocked)
            button.setToolTip(TURBO_POWER_MESSAGE if turbo_blocked else f"Select {profile.label.title()} mode")
        self.full_speed_check.setEnabled(enabled)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._closing:
            event.accept()
            return
        self._closing = True
        self._poll_timer.stop()
        self._set_controls_enabled(False)
        self._update_lighting_controls()
        for pending in (self._pending, self._lighting_pending):
            if pending is not None:
                pending.add_done_callback(self._log_shutdown_error)
        self.executor.submit(self.session.close).add_done_callback(self._log_shutdown_error)
        self.executor.shutdown(wait=False, cancel_futures=False)
        self.lighting_executor.shutdown(wait=False, cancel_futures=False)
        event.accept()

    @staticmethod
    def _log_shutdown_error(future: Future[FanStatus] | Future[None]) -> None:
        error = future.exception()
        if error is not None:
            logger.error(
                "Hardware operation failed during shutdown: %s", error,
                exc_info=(type(error), error, error.__traceback__),
            )
