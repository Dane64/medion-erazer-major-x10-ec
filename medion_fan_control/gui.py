"""Erazer Control - unprivileged desktop GUI talking to x10ctld."""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from PySide6.QtCore import QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QColor, QGuiApplication
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .audio import AlsaMixer, AudioError, MixerControl
from .client import Backend, BackendError, DaemonClient, DemoBackend
from .display import DisplayError, KScreen
from .icons import app_icon, icon, load_brand_font, logo_pixmap
from .lighting import LightZone, RgbColor
from .protocol import FanStatus, Profile, TURBO_POWER_MESSAGE
from .telemetry import TelemetryHistory, format_duration
from .theme import (
    ACCENT,
    APP_STYLESHEET,
    BORDER,
    CPU_COLOR,
    ERROR,
    GPU_COLOR,
    INK,
    MUTED,
    SUCCESS,
    SURFACE_HI,
    WARNING,
    lighting_text_color,
)
from .widgets import ResponsiveGrid, TelemetryGraph, ToggleSwitch, ValueSlider

logger = logging.getLogger(__name__)

LIGHTING_ZONE_ORDER = (LightZone.KEYBOARD, LightZone.LID, LightZone.LEFT_SIDE, LightZone.RIGHT_SIDE)
DEFAULT_LIGHTING_COLOR = RgbColor(0x0A, 0x8F, 0xFF)
PAGES = ("DASHBOARD", "CPU", "GPU", "DISPLAY", "AUDIO", "LED")
PAGE_ICONS = ("dashboard", "cpu", "gpu", "display", "audio", "led")


# ------------------------------------------------------------------ plumbing
class _Bridge(QObject):
    finished = Signal(object, object, object)


class AsyncBackend:
    """Runs backend calls off the GUI thread; callbacks return on the GUI thread.

    EC calls (telemetry, profiles) use their own worker so a slow arc-dgpu-ctl run
    never stalls the dashboard.
    """

    def __init__(self, backend: Backend, on_error: Callable[[str, BackendError], None]) -> None:
        self.backend = backend
        self._on_error = on_error
        self._bridge = _Bridge()
        self._bridge.finished.connect(self._deliver)
        self._executors = {
            "ec": ThreadPoolExecutor(max_workers=1, thread_name_prefix="x10-ec"),
            "sys": ThreadPoolExecutor(max_workers=1, thread_name_prefix="x10-sys"),
        }
        self.closed = False

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        on_done: Callable[[Any], None] | None = None,
        on_error: Callable[[BackendError], None] | None = None,
    ) -> None:
        if self.closed:
            return
        executor = self._executors["ec" if method.startswith("ec.") else "sys"]

        def work() -> None:
            try:
                result = self.backend.call(method, **(params or {}))
            except BackendError as error:
                self._bridge.finished.emit(on_error or (lambda e: self._on_error(method, e)), None, error)
            except Exception as error:  # pragma: no cover - defensive
                logger.exception("backend call %s failed", method)
                wrapped = BackendError(str(error), type(error).__name__)
                self._bridge.finished.emit(on_error or (lambda e: self._on_error(method, e)), None, wrapped)
            else:
                if on_done is not None:
                    self._bridge.finished.emit(on_done, result, None)

        executor.submit(work)

    def _deliver(self, callback: Callable, result: Any, error: BackendError | None) -> None:
        if self.closed:
            return
        callback(error if error is not None else result)

    def shutdown(self, wait: bool = False) -> None:
        self.closed = True
        for executor in self._executors.values():
            executor.shutdown(wait=wait, cancel_futures=True)
        # closing may wait for an in-flight request; never block the GUI thread
        threading.Thread(target=self.backend.close, name="x10-close", daemon=True).start()


def panel(accent: bool = False) -> QFrame:
    frame = QFrame()
    frame.setProperty("panel", True)
    if accent:
        frame.setProperty("accent", True)
    return frame


def section(title: str, accent: bool = True) -> tuple[QFrame, QVBoxLayout]:
    frame = panel(accent)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(18, 14, 18, 16)
    layout.setSpacing(10)
    layout.addWidget(QLabel(title.upper(), objectName="sectionTitle"))
    return frame, layout


def muted(text: str, role: str = "muted") -> QLabel:
    label = QLabel(text)
    label.setProperty("role", role)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


def clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()
        elif item.layout() is not None:
            clear_layout(item.layout())


def scrollable(content: QWidget) -> QScrollArea:
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setWidget(content)
    return scroll


class Page(QWidget):
    title = ""
    glyph = ""
    subtitle = ""

    def __init__(self, app: "FanControlApp") -> None:
        super().__init__(objectName="page")
        self.app = app
        outer = QVBoxLayout(self)
        outer.setContentsMargins(2, 4, 2, 8)
        outer.setSpacing(14)
        heading = QHBoxLayout()
        heading.setSpacing(12)
        if self.glyph:
            badge = QLabel(objectName="pageIcon")
            badge.setPixmap(icon(self.glyph, ACCENT).pixmap(26, 26))
            badge.setFixedSize(44, 44)
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            heading.addWidget(badge, alignment=Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(0)
        text.addWidget(QLabel(self.title, objectName="title"))
        text.addWidget(muted(self.subtitle))
        heading.addLayout(text, 1)
        self.reload_button = QPushButton(icon("reload"), "Reload")
        self.reload_button.clicked.connect(self.refresh)
        heading.addWidget(self.reload_button, alignment=Qt.AlignmentFlag.AlignTop)
        outer.addLayout(heading)
        self.status = muted("")
        self.status.hide()
        outer.addWidget(self.status)
        self.body = QVBoxLayout()
        self.body.setSpacing(14)
        outer.addLayout(self.body)
        outer.addStretch()
        self.loaded = False

    def call(self, method: str, params: dict | None = None, on_done: Callable | None = None,
             busy: str | None = None) -> None:
        if busy:
            self.set_status(busy)

        def failed(error: BackendError) -> None:
            self.set_status(str(error), "error")

        self.app.backend.call(method, params, on_done, failed)

    def set_status(self, text: str, role: str = "muted") -> None:
        self.status.setVisible(bool(text))
        self.status.setText(text)
        self.status.setProperty("role", role)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    def confirm(self, title: str, text: str) -> bool:
        return QMessageBox.question(
            self, title, text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes

    def restore_checkbox(self, key: str, text: str) -> QCheckBox:
        box = ToggleSwitch(text)
        box.setChecked(bool(self.app.restore_flags.get(key)))
        box.toggled.connect(lambda checked: self.app.set_restore_flag(key, checked))
        return box

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        if not self.loaded:
            self.loaded = True
            self.refresh()

    def refresh(self) -> None:  # pragma: no cover - overridden
        pass


# ----------------------------------------------------------------- dashboard
class DashboardPage(Page):
    title = "Dashboard"
    glyph = "dashboard"
    subtitle = "Firmware fan profile, fan override and live telemetry."

    def __init__(self, app: "FanControlApp") -> None:
        super().__init__(app)
        self.reload_button.hide()
        self.loaded = True
        self.mode_buttons: dict[Profile, QRadioButton] = {}

        controls, layout = section("Performance mode")
        row = QHBoxLayout()
        group = QButtonGroup(self)
        for profile in Profile:
            button = QRadioButton(profile.label.upper())
            button.clicked.connect(lambda _c=False, p=profile: app.request_profile_change(p))
            group.addButton(button)
            row.addWidget(button)
            self.mode_buttons[profile] = button
        row.addSpacing(24)
        self.full_speed_check = ToggleSwitch("Full-speed fans")
        self.full_speed_check.setToolTip(
            "Runs both fans at full speed. Turn off to resume the selected profile. Closing the app keeps it.")
        self.full_speed_check.clicked.connect(app.request_full_speed_change)
        row.addWidget(self.full_speed_check)
        row.addStretch()
        layout.addLayout(row)
        self.power_label = muted("Power supply: unknown")
        layout.addWidget(self.power_label)
        self.body.addWidget(controls)

        cards = []
        for name, attribute, unit, color in (
            ("CPU FAN", "cpu_rpm_label", "RPM", CPU_COLOR),
            ("GPU FAN", "gpu_rpm_label", "RPM", GPU_COLOR),
            ("CPU TEMP", "cpu_temp_label", "°C", CPU_COLOR),
            ("GPU TEMP", "gpu_temp_label", "°C", GPU_COLOR),
        ):
            card, value = self._metric_card(name, unit, color)
            setattr(self, attribute, value)
            cards.append(card)
        self.body.addWidget(ResponsiveGrid(cards, wide_columns=4, medium_width=560, wide_width=900))

        graph_panel, graph_layout = section("Fan speed and temperature")
        header = QHBoxLayout()
        legend = QLabel(
            f'<span style="color:{CPU_COLOR}">━━</span> CPU fan &nbsp; '
            f'<span style="color:{GPU_COLOR}">━━</span> GPU fan &nbsp; '
            f'<span style="color:{CPU_COLOR}">┅┅</span> CPU temp &nbsp; '
            f'<span style="color:{GPU_COLOR}">┅┅</span> GPU temp'
        )
        legend.setProperty("role", "muted")
        header.addWidget(legend)
        header.addStretch()
        header.addWidget(QLabel("WINDOW", objectName="metricName"))
        self.window_slider = QSlider(Qt.Orientation.Horizontal)
        self.window_slider.setRange(1, 20)
        self.window_slider.setValue(6)
        self.window_slider.setMaximumWidth(180)
        self.window_slider.setAccessibleName("Telemetry time window")
        self.window_slider.valueChanged.connect(self._window_changed)
        header.addWidget(self.window_slider)
        self.window_label = QLabel("3 min", objectName="metricName")
        self.window_label.setMinimumWidth(50)
        header.addWidget(self.window_label)
        graph_layout.addLayout(header)
        self.graph = TelemetryGraph(app.history)
        graph_layout.addWidget(self.graph, 1)
        self.body.addWidget(graph_panel, 1)

    @staticmethod
    def _metric_card(name: str, unit: str, color: str) -> tuple[QFrame, QLabel]:
        card = panel()
        layout = QHBoxLayout(card)
        layout.setContentsMargins(14, 12, 14, 12)
        marker = QFrame()
        marker.setFixedWidth(3)
        marker.setStyleSheet(f"background: {color}; border-radius: 1px;")
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

    def _window_changed(self, step: int) -> None:
        seconds = step * 30
        self.window_label.setText(format_duration(seconds))
        self.graph.set_window_seconds(seconds)


# ----------------------------------------------------------------------- CPU
class CpuPage(Page):
    title = "CPU"
    glyph = "cpu"
    subtitle = "Turbo, frequency limits, package power (RAPL) and voltage offsets."

    def refresh(self) -> None:
        self.call("cpu.get", on_done=self.populate, busy="Reading CPU state...")

    def populate(self, state: dict[str, Any]) -> None:
        clear_layout(self.body)
        self.state = state
        self.set_status("")
        self._build_policy(state)
        self._build_frequency(state)
        self._build_rapl(state)
        apply_row = QHBoxLayout()
        apply_row.addWidget(self.restore_checkbox("cpu", "Re-apply CPU settings at startup"))
        apply_row.addStretch()
        apply = QPushButton("Apply CPU settings")
        apply.setProperty("role", "primary")
        apply.clicked.connect(self.apply)
        apply_row.addWidget(apply)
        self.body.addLayout(apply_row)
        self._build_undervolt(state)

    def _build_policy(self, state: dict[str, Any]) -> None:
        frame, layout = section("Performance policy")
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        pstate = state.get("pstate")
        self.turbo = ToggleSwitch("Turbo boost")
        self.turbo.setEnabled(pstate is not None)
        self.turbo.setChecked(bool(pstate and not pstate["no_turbo"]))
        grid.addWidget(self.turbo, 0, 0)
        self.dynamic_boost = ToggleSwitch("HWP dynamic boost")
        has_boost = bool(pstate and pstate.get("dynamic_boost") is not None)
        self.dynamic_boost.setEnabled(has_boost)
        self.dynamic_boost.setChecked(bool(has_boost and pstate["dynamic_boost"]))
        grid.addWidget(self.dynamic_boost, 0, 1)
        self.governor = self._combo(state.get("governors", []), state.get("governor"))
        grid.addWidget(QLabel("Governor"), 1, 0)
        grid.addWidget(self.governor, 1, 1)
        self.epp = self._combo(state.get("epps", []), state.get("epp"))
        grid.addWidget(QLabel("Energy/performance preference"), 2, 0)
        grid.addWidget(self.epp, 2, 1)
        self.platform_profile = self._combo(state.get("platform_profiles", []), state.get("platform_profile"))
        if self.platform_profile.isEnabled():
            grid.addWidget(QLabel("ACPI platform profile"), 3, 0)
            grid.addWidget(self.platform_profile, 3, 1)
        else:
            self.platform_profile.setParent(frame)
            self.platform_profile.hide()
        layout.addLayout(grid)
        self.min_perf = self.max_perf = None
        if pstate and pstate.get("min_perf_pct") is not None:
            self.min_perf = ValueSlider("Minimum performance", 1, 100, pstate["min_perf_pct"], unit="%")
            self.max_perf = ValueSlider("Maximum performance", 1, 100, pstate["max_perf_pct"], unit="%")
            layout.addWidget(self.min_perf)
            layout.addWidget(self.max_perf)
        self.body.addWidget(frame)

    @staticmethod
    def _combo(items: list[str], current: str | None) -> QComboBox:
        combo = QComboBox()
        combo.addItems(items)
        if current in items:
            combo.setCurrentText(current)
        combo.setEnabled(bool(items))
        return combo

    def _build_frequency(self, state: dict[str, Any]) -> None:
        frame, layout = section("Frequency limits")
        self.cluster_sliders: list[tuple[str, ValueSlider, ValueSlider]] = []
        mhz = lambda v: f"{v / 1000:,.0f} MHz"  # noqa: E731
        for cluster in state.get("clusters", []):
            layout.addWidget(QLabel(
                f"{cluster['name']}  ·  {cluster['cpus']} CPUs  ·  "
                f"{cluster['hw_min_khz'] / 1000:,.0f}–{cluster['hw_max_khz'] / 1000:,.0f} MHz",
                objectName="metricName"))
            low = ValueSlider("Minimum", cluster["hw_min_khz"], cluster["hw_max_khz"],
                              cluster.get("min_khz") or cluster["hw_min_khz"], step=100_000, formatter=mhz)
            high = ValueSlider("Maximum", cluster["hw_min_khz"], cluster["hw_max_khz"],
                               cluster.get("max_khz") or cluster["hw_max_khz"], step=100_000, formatter=mhz)
            layout.addWidget(low)
            layout.addWidget(high)
            self.cluster_sliders.append((cluster["name"], low, high))
        if not self.cluster_sliders:
            layout.addWidget(muted("No cpufreq policies found."))
        self.body.addWidget(frame)

    def _build_rapl(self, state: dict[str, Any]) -> None:
        frame, layout = section("Package power limits (RAPL)")
        rapl = state.get("rapl")
        self.pl1 = self.pl2 = self.tau = None
        if not rapl:
            layout.addWidget(muted("The intel-rapl powercap interface is not available."))
            self.body.addWidget(frame)
            return
        grid = QGridLayout()
        if rapl.get("pl1"):
            self.pl1 = self._watts(rapl["pl1"])
            grid.addWidget(QLabel("PL1 sustained"), 0, 0)
            grid.addWidget(self.pl1, 0, 1)
            self.tau = QDoubleSpinBox()
            self.tau.setRange(0.1, 128)
            self.tau.setDecimals(1)
            self.tau.setSuffix(" s")
            self.tau.setValue(rapl["pl1"].get("window_s") or 28)
            grid.addWidget(QLabel("PL1 time window (tau)"), 1, 0)
            grid.addWidget(self.tau, 1, 1)
        if rapl.get("pl2"):
            self.pl2 = self._watts(rapl["pl2"])
            grid.addWidget(QLabel("PL2 boost"), 2, 0)
            grid.addWidget(self.pl2, 2, 1)
        grid.setColumnStretch(2, 1)
        layout.addLayout(grid)
        layout.addWidget(muted(
            f"Written to {', '.join(rapl.get('zones', []))}. The EC profile can still impose its own "
            "lower limits; check the result under load."))
        self.body.addWidget(frame)

    @staticmethod
    def _watts(entry: dict[str, Any]) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(5, int(entry.get("max_watts") or 140))
        spin.setSuffix(" W")
        spin.setValue(int(round(entry.get("watts") or 45)))
        return spin

    def _build_undervolt(self, state: dict[str, Any]) -> None:
        frame, layout = section("Voltage offsets (undervolt)")
        undervolt = state.get("undervolt") or {}
        self.offsets: dict[str, QSpinBox] = {}
        if not undervolt.get("available"):
            layout.addWidget(muted(f"Unavailable: {undervolt.get('reason', 'unknown')}", "warning"))
            self.body.addWidget(frame)
            return
        layout.addWidget(muted(
            "Negative offsets only, bounded by the signed x10_ec module. Each write is read back; "
            "if the BIOS has undervolt protection the module reports it instead of pretending. "
            "Too much undervolt causes freezes - lower in 10 mV steps and stress-test."))
        grid = QGridLayout()
        labels = undervolt.get("labels", {})
        for row, (plane, value) in enumerate(undervolt.get("planes", {}).items()):
            spin = QSpinBox()
            spin.setRange(undervolt.get("min_mv", -150), undervolt.get("max_mv", 0))
            spin.setSingleStep(5)
            spin.setSuffix(" mV")
            spin.setValue(value or 0)
            spin.setEnabled(value is not None)
            grid.addWidget(QLabel(labels.get(plane, plane)), row, 0)
            grid.addWidget(spin, row, 1)
            self.offsets[plane] = spin
        grid.setColumnStretch(2, 1)
        layout.addLayout(grid)
        row = QHBoxLayout()
        row.addWidget(self.restore_checkbox(
            "undervolt", "Re-apply offsets at startup (skipped automatically after a crash)"))
        row.addStretch()
        reset = QPushButton("Reset to 0")
        reset.clicked.connect(lambda: [spin.setValue(0) for spin in self.offsets.values()])
        row.addWidget(reset)
        apply = QPushButton("Apply offsets")
        apply.setProperty("role", "primary")
        apply.clicked.connect(self.apply_undervolt)
        row.addWidget(apply)
        layout.addLayout(row)
        self.body.addWidget(frame)

    def collect(self) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if self.turbo.isEnabled():
            params["no_turbo"] = not self.turbo.isChecked()
        if self.dynamic_boost.isEnabled():
            params["dynamic_boost"] = self.dynamic_boost.isChecked()
        for key, combo in (("governor", self.governor), ("epp", self.epp),
                           ("platform_profile", self.platform_profile)):
            if combo.isEnabled() and combo.currentText():
                params[key] = combo.currentText()
        if self.min_perf and self.max_perf:
            params["min_perf_pct"] = self.min_perf.value()
            params["max_perf_pct"] = self.max_perf.value()
        params["clusters"] = [
            {"name": name, "min_khz": low.value(), "max_khz": high.value()}
            for name, low, high in self.cluster_sliders
        ]
        rapl: dict[str, Any] = {}
        if self.pl1:
            rapl["pl1"] = {"watts": self.pl1.value(), "window_s": round(self.tau.value(), 1)}
        if self.pl2:
            rapl["pl2"] = {"watts": self.pl2.value()}
        if rapl:
            params["rapl"] = rapl
        return params

    def apply(self) -> None:
        self.call("cpu.set", self.collect(), self._applied, busy="Applying CPU settings...")

    def _applied(self, state: dict[str, Any]) -> None:
        self.populate(state)
        self.set_status("CPU settings applied and read back.", "ok")

    def apply_undervolt(self) -> None:
        offsets = {plane: spin.value() for plane, spin in self.offsets.items() if spin.isEnabled()}
        summary = ", ".join(f"{plane} {mv} mV" for plane, mv in offsets.items())
        if not self.confirm("Apply voltage offsets",
                            f"Apply {summary}?\n\nAn unstable undervolt can freeze the system. "
                            "Offsets reset to the BIOS default on a cold boot unless startup restore is on."):
            return
        self.call("cpu.set", {"undervolt": offsets}, self._undervolt_applied, busy="Writing voltage offsets...")

    def _undervolt_applied(self, state: dict[str, Any]) -> None:
        self.populate(state)
        self.set_status("Voltage offsets accepted and verified by readback.", "ok")


# ----------------------------------------------------------------------- GPU
class GpuPage(Page):
    title = "GPU"
    glyph = "gpu"
    subtitle = "Arc A730M power gating (arc-dgpu-ctl), frequency limits and power limits."

    def refresh(self) -> None:
        self.call("gpu.get", on_done=self.populate, busy="Reading GPU state...")

    def populate(self, state: dict[str, Any]) -> None:
        clear_layout(self.body)
        self.set_status("")
        self.controls: dict[str, dict[str, Any]] = {}
        self._build_dgpu(state["dgpu"])
        for gpu in state.get("gpus", []):
            self._build_gpu(gpu)
        note = muted(
            "Linux i915/xe expose no voltage control or clocks above RP0 for Arc, so limits stay within "
            "the hardware range (RPn..RP0). Power limits use the driver's hwmon interface.")
        self.body.addWidget(note)
        self.body.addWidget(self.restore_checkbox("gpu", "Re-apply GPU settings at startup"))

    def _build_dgpu(self, dgpu: dict[str, Any]) -> None:
        frame, layout = section("Discrete GPU power")
        row = QHBoxLayout()
        colors = {"active": SUCCESS, "suspended": ACCENT, "off": MUTED, "unbound": WARNING}
        badge = QLabel(dgpu["state"].upper(), objectName="badge")
        badge.setStyleSheet(f"color: {colors.get(dgpu['state'], INK)};")
        row.addWidget(badge)
        detail = f"driver {dgpu['driver'] or 'none'}, runtime PM {dgpu['runtime_status'] or 'n/a'}"
        row.addWidget(muted(detail), 1)
        self.dgpu_on = QPushButton("Power on")
        self.dgpu_off = QPushButton("Power off")
        self.dgpu_off.setProperty("role", "danger")
        available = bool(dgpu.get("arc_dgpu_ctl"))
        self.dgpu_on.setEnabled(available and dgpu["state"] in {"off", "unbound"})
        self.dgpu_off.setEnabled(available and dgpu["state"] in {"active", "suspended"})
        self.dgpu_on.clicked.connect(lambda: self.set_dgpu(True))
        self.dgpu_off.clicked.connect(lambda: self.set_dgpu(False))
        row.addWidget(self.dgpu_on)
        row.addWidget(self.dgpu_off)
        layout.addLayout(row)
        layout.addWidget(muted(
            f"Using {dgpu['arc_dgpu_ctl']}" if available else
            "arc-dgpu-ctl not found in PATH; set [dgpu] command in /etc/x10ctld.conf",
            "muted" if available else "warning"))
        self.body.addWidget(frame)

    def set_dgpu(self, on: bool) -> None:
        if not on and not self.confirm(
                "Power off dGPU", "Unbind and power off the Arc dGPU?\n\nClose applications using it first."):
            return
        self.call("gpu.dgpu_power", {"on": on}, self._dgpu_done,
                  busy="Powering on the dGPU..." if on else "Powering off the dGPU...")

    def _dgpu_done(self, state: dict[str, Any]) -> None:
        self.populate(state)
        self.set_status(f"dGPU is now {state['dgpu']['state']}.", "ok")

    def _build_gpu(self, gpu: dict[str, Any]) -> None:
        frame, layout = section(gpu["name"])
        layout.addWidget(muted(
            f"{gpu['id']} · {gpu['pci']} · driver {gpu['driver']} · runtime PM {gpu['runtime_status'] or 'n/a'}"))
        controls: dict[str, Any] = {}
        freq = gpu.get("freq")
        if freq and freq.get("rpn") is not None and freq.get("rp0") is not None:
            layout.addWidget(QLabel(
                f"CURRENT {freq.get('act') or freq.get('cur') or 0} MHz  ·  RANGE {freq['rpn']}–{freq['rp0']} MHz",
                objectName="metricName"))
            for key, caption in (("min", "Minimum"), ("max", "Maximum"), ("boost", "Boost")):
                if freq.get(key) is None:
                    continue
                slider = ValueSlider(caption, freq["rpn"], freq["rp0"], freq[key], unit="MHz", step=50)
                layout.addWidget(slider)
                controls[key] = slider
        power = gpu.get("power")
        if power and power.get("limit_w") is not None:
            row = QHBoxLayout()
            row.addWidget(QLabel("Power limit"))
            spin = QSpinBox()
            spin.setRange(10, int(power.get("rated_w") or 150))
            spin.setSuffix(" W")
            spin.setValue(int(round(power["limit_w"])))
            row.addWidget(spin)
            row.addStretch()
            layout.addLayout(row)
            controls["power"] = spin
        if controls:
            apply = QPushButton("Apply")
            apply.setProperty("role", "primary")
            apply.clicked.connect(lambda _c=False, g=gpu["id"]: self.apply(g))
            layout.addWidget(apply, alignment=Qt.AlignmentFlag.AlignRight)
        else:
            layout.addWidget(muted("No adjustable controls exposed by the driver."))
        self.controls[gpu["id"]] = controls
        self.body.addWidget(frame)

    def apply(self, gpu_id: str) -> None:
        controls = self.controls[gpu_id]
        params: dict[str, Any] = {"id": gpu_id}
        if "min" in controls:
            params["min_mhz"] = controls["min"].value()
        if "max" in controls:
            params["max_mhz"] = controls["max"].value()
        if "boost" in controls:
            params["boost_mhz"] = max(controls["boost"].value(), params.get("min_mhz", 0))
        if "power" in controls:
            params["power_limit_w"] = controls["power"].value()
        self.call("gpu.set", params, self._applied, busy=f"Applying {gpu_id} limits...")

    def _applied(self, state: dict[str, Any]) -> None:
        self.populate(state)
        self.set_status("GPU limits applied and read back.", "ok")


# ------------------------------------------------------------------- Display
class DisplayPage(Page):
    title = "Display"
    glyph = "display"
    subtitle = "Backlight, refresh rate, adaptive sync and panel overclock."

    def __init__(self, app: "FanControlApp") -> None:
        super().__init__(app)
        self.kscreen = app.kscreen

    def refresh(self) -> None:
        self.call("display.get", on_done=self.populate, busy="Reading display state...")

    def populate(self, state: dict[str, Any]) -> None:
        clear_layout(self.body)
        self.set_status("")
        self._build_backlight(state.get("backlight"))
        self._build_refresh()
        self._build_overclock(state.get("panel"), state.get("kernel_parameter"))

    def _build_backlight(self, backlight: dict[str, Any] | None) -> None:
        frame, layout = section("Backlight")
        if not backlight or backlight.get("percent") is None:
            layout.addWidget(muted("No backlight device found."))
        else:
            self.brightness = ValueSlider("Brightness", 1, 100, backlight["percent"], unit="%")
            self.brightness.slider.sliderReleased.connect(self._brightness_changed)
            self.brightness.slider.valueChanged.connect(
                lambda _v: None if self.brightness.slider.isSliderDown() else self._brightness_changed())
            layout.addWidget(self.brightness)
        self.body.addWidget(frame)

    def _brightness_changed(self) -> None:
        self.call("display.brightness", {"percent": self.brightness.value()})

    def _build_refresh(self) -> None:
        frame, layout = section("Refresh rate and adaptive sync")
        try:
            self.outputs = self.kscreen.outputs()
        except DisplayError as error:
            layout.addWidget(muted(str(error), "warning"))
            self.body.addWidget(frame)
            return
        grid = QGridLayout()
        self.output_combo = QComboBox()
        for output in self.outputs:
            self.output_combo.addItem(f"{output['name']} ({output['resolution']})", output)
        self.mode_combo = QComboBox()
        self.vrr_combo = QComboBox()
        self.vrr_combo.addItems(["automatic", "always", "never"])
        self.output_combo.currentIndexChanged.connect(self._output_changed)
        grid.addWidget(QLabel("Output"), 0, 0)
        grid.addWidget(self.output_combo, 0, 1)
        grid.addWidget(QLabel("Refresh rate"), 1, 0)
        grid.addWidget(self.mode_combo, 1, 1)
        grid.addWidget(QLabel("Adaptive sync (VRR)"), 2, 0)
        grid.addWidget(self.vrr_combo, 2, 1)
        grid.setColumnStretch(2, 1)
        layout.addLayout(grid)
        apply = QPushButton("Apply")
        apply.setProperty("role", "primary")
        apply.clicked.connect(self._apply_mode)
        layout.addWidget(apply, alignment=Qt.AlignmentFlag.AlignRight)
        self._output_changed()
        self.body.addWidget(frame)

    def _output_changed(self) -> None:
        output = self.output_combo.currentData()
        self.mode_combo.clear()
        if not output:
            return
        for mode in output["modes"]:
            self.mode_combo.addItem(f"{mode['refresh']:.2f} Hz", mode["id"])
            if mode["id"] == output["current_mode"]:
                self.mode_combo.setCurrentIndex(self.mode_combo.count() - 1)
        has_vrr = output.get("vrr_policy") is not None
        self.vrr_combo.setEnabled(has_vrr)
        if has_vrr:
            names = {0: "never", 1: "always", 2: "automatic"}
            policy = output["vrr_policy"]
            self.vrr_combo.setCurrentText(names.get(policy, policy) if isinstance(policy, int) else str(policy))

    def _apply_mode(self) -> None:
        output = self.output_combo.currentData()
        try:
            self.kscreen.set_mode(output["name"], self.mode_combo.currentData())
            if self.vrr_combo.isEnabled():
                self.kscreen.set_vrr(output["name"], self.vrr_combo.currentText())
        except DisplayError as error:
            self.set_status(str(error), "error")
            return
        self.set_status(f"{output['name']} set to {self.mode_combo.currentText()}.", "ok")

    def _build_overclock(self, panel_state: dict[str, Any] | None, kernel_parameter: str | None) -> None:
        frame, layout = section("Panel overclock (EDID override)")
        if not panel_state or panel_state.get("native_hz") is None:
            layout.addWidget(muted(
                (panel_state or {}).get("error", "No internal eDP panel with a readable EDID was found."),
                "warning"))
            self.body.addWidget(frame)
            return
        if panel_state.get("blocker"):
            layout.addWidget(muted(
                f"{panel_state['connector']} native mode {panel_state['native_mode']} Hz - "
                f"overclocking unavailable: {panel_state['blocker']}.", "warning"))
            self.body.addWidget(frame)
            return
        layout.addWidget(muted(
            f"{panel_state['connector']} native mode {panel_state['native_mode']} Hz. The override copies the "
            "panel's own timing with a higher pixel clock; nothing changes until you add the kernel parameter "
            "and reboot. Not every panel accepts it - raise in small steps."))
        row = QHBoxLayout()
        row.addWidget(QLabel("Target refresh"))
        self.target_hz = QDoubleSpinBox()
        self.target_hz.setDecimals(0)
        self.target_hz.setRange(float(int(panel_state["native_hz"]) + 1), float(int(panel_state["max_hz"])))
        self.target_hz.setSuffix(" Hz")
        self.target_hz.setValue(min(self.target_hz.maximum(), int(panel_state["native_hz"]) + 10))
        row.addWidget(self.target_hz)
        row.addStretch()
        install = QPushButton("Install override")
        install.setProperty("role", "primary")
        install.clicked.connect(self._install)
        row.addWidget(install)
        remove = QPushButton("Remove override")
        remove.setProperty("role", "danger")
        remove.setEnabled(bool(panel_state.get("overrides")))
        remove.clicked.connect(self._remove)
        row.addWidget(remove)
        layout.addLayout(row)
        overrides = panel_state.get("overrides") or []
        if overrides:
            parameter = kernel_parameter or f"drm.edid_firmware={panel_state['connector']}:edid/{overrides[-1]}"
            layout.addWidget(QLabel("INSTALLED", objectName="metricName"))
            param_row = QHBoxLayout()
            param = QLabel(parameter)
            param.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            param.setStyleSheet(f"font-family: 'DejaVu Sans Mono'; background: {SURFACE_HI}; padding: 6px;")
            param_row.addWidget(param, 1)
            copy = QPushButton("Copy")
            copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(parameter))
            param_row.addWidget(copy)
            layout.addLayout(param_row)
            layout.addWidget(muted(
                "Activate: add the parameter to GRUB_CMDLINE_LINUX_DEFAULT in /etc/default/grub, then run "
                "'sudo update-initramfs -u && sudo update-grub' and reboot.\n"
                "If the panel stays black: press 'e' in the GRUB menu, delete the parameter for that boot, "
                "then remove it from /etc/default/grub."))
        self.body.addWidget(frame)

    def _install(self) -> None:
        target = int(self.target_hz.value())
        if not self.confirm("Install EDID override",
                            f"Create an EDID override for {target} Hz?\n\nIt only takes effect after you add "
                            "the kernel parameter shown afterwards and reboot."):
            return
        self.call("display.overclock", {"target_hz": target}, self._installed, busy="Writing EDID override...")

    def _installed(self, state: dict[str, Any]) -> None:
        self.populate(state)
        self.set_status(f"Override for {state.get('achieved_hz')} Hz installed as {state.get('installed')}.", "ok")

    def _remove(self) -> None:
        self.call("display.overclock_remove", on_done=self._removed)

    def _removed(self, state: dict[str, Any]) -> None:
        self.populate(state)
        self.set_status("Override removed. Also delete drm.edid_firmware=... from the kernel command line.", "ok")


# --------------------------------------------------------------------- Audio
class AudioPage(Page):
    title = "Audio"
    glyph = "audio"
    subtitle = "Codec mixer controls (amplifiers, DAC volume, routing, auto-mute) and HDA power saving."

    def __init__(self, app: "FanControlApp") -> None:
        super().__init__(app)
        self.mixer = app.mixer

    def refresh(self) -> None:
        clear_layout(self.body)
        self.set_status("")
        self._build_codec()
        self.call("audio.get", on_done=self._build_power)

    def _build_codec(self) -> None:
        frame, layout = section("Sound card")
        try:
            cards = self.mixer.cards()
        except AudioError as error:
            layout.addWidget(muted(str(error), "warning"))
            self.body.addWidget(frame)
            return
        self.card_combo = QComboBox()
        for card in cards:
            self.card_combo.addItem(f"{card['index']}: {card['name']}", card["index"])
        hda = next((i for i, c in enumerate(cards) if "HDA" in c["driver"] or "hda" in c["id"].lower()), 0)
        self.card_combo.setCurrentIndex(hda)
        layout.addWidget(self.card_combo)
        self.controls_box = QVBoxLayout()
        layout.addLayout(self.controls_box)
        self.card_combo.currentIndexChanged.connect(self._load_controls)
        self.body.addWidget(frame)
        self._load_controls()

    def _load_controls(self) -> None:
        clear_layout(self.controls_box)
        card = self.card_combo.currentData()
        if card is None:
            return
        try:
            controls = self.mixer.controls(card)
        except AudioError as error:
            self.controls_box.addWidget(muted(str(error), "warning"))
            return
        groups: dict[str, list[MixerControl]] = {}
        for control in controls:
            groups.setdefault(control.category, []).append(control)
        for category in ("Output", "Input", "Codec options"):
            if category not in groups:
                continue
            self.controls_box.addWidget(QLabel(category.upper(), objectName="metricName"))
            grid = QGridLayout()
            for row, control in enumerate(sorted(groups[category], key=lambda c: c.name)):
                grid.addWidget(QLabel(control.name), row, 0)
                grid.addWidget(self._control_widget(card, control), row, 1)
            grid.setColumnStretch(1, 1)
            self.controls_box.addLayout(grid)

    def _control_widget(self, card: int, control: MixerControl) -> QWidget:
        if control.type == "BOOLEAN":
            box = ToggleSwitch()
            box.setChecked(bool(control.values) and control.values[0] == "on")
            box.toggled.connect(lambda checked, c=control: self._set(card, c, checked))
            return box
        if control.type == "ENUMERATED":
            combo = QComboBox()
            combo.addItems(control.items)
            if control.values and control.values[0].isdigit():
                combo.setCurrentIndex(int(control.values[0]))
            combo.currentIndexChanged.connect(lambda index, c=control: self._set(card, c, index))
            return combo
        low, high = control.minimum or 0, control.maximum or 100
        current = int(control.values[0]) if control.values and control.values[0].lstrip("-").isdigit() else low
        slider = ValueSlider("", low, high, current)
        slider.caption.hide()
        slider.slider.sliderReleased.connect(lambda c=control, s=slider: self._set(card, c, s.value()))
        return slider

    def _set(self, card: int, control: MixerControl, value: Any) -> None:
        try:
            self.mixer.set(card, control, value)
        except AudioError as error:
            self.set_status(str(error), "error")
            return
        self.set_status(f"{control.name} updated.", "ok")

    def _build_power(self, state: dict[str, Any]) -> None:
        frame, layout = section("HDA power saving")
        if not state.get("available"):
            layout.addWidget(muted("snd_hda_intel is not loaded."))
            self.body.addWidget(frame)
            return
        row = QHBoxLayout()
        row.addWidget(QLabel("Codec power-down after"))
        self.power_save = QSpinBox()
        self.power_save.setRange(0, 3600)
        self.power_save.setSuffix(" s")
        self.power_save.setSpecialValueText("never (0)")
        self.power_save.setValue(state.get("power_save_s") or 0)
        row.addWidget(self.power_save)
        self.power_save_controller = ToggleSwitch("Also power down the controller")
        self.power_save_controller.setChecked(bool(state.get("power_save_controller")))
        row.addWidget(self.power_save_controller)
        row.addStretch()
        apply = QPushButton("Apply")
        apply.setProperty("role", "primary")
        apply.clicked.connect(self._apply_power)
        row.addWidget(apply)
        layout.addLayout(row)
        layout.addWidget(muted("Set 0 if you hear pops or clicks when sound starts (codec wake-up)."))
        layout.addWidget(self.restore_checkbox("audio", "Re-apply audio settings at startup"))
        self.body.addWidget(frame)

    def _apply_power(self) -> None:
        self.call("audio.set", {"power_save_s": self.power_save.value(),
                                "power_save_controller": self.power_save_controller.isChecked()},
                  lambda _s: self.set_status("HDA power saving updated.", "ok"))


# ----------------------------------------------------------------------- LED
class LedPage(Page):
    title = "LED"
    glyph = "led"
    subtitle = "Static colors per zone, on/off switches and brightness."

    def __init__(self, app: "FanControlApp") -> None:
        super().__init__(app)
        self.settings: dict[LightZone, dict[str, Any]] = {}
        self.swatches: dict[LightZone, QPushButton] = {}
        self.switches: dict[LightZone, QCheckBox] = {}
        self.brightness: ValueSlider | None = None

    def refresh(self) -> None:
        self.call("lighting.get", on_done=self.populate, busy="Loading saved lighting...")

    def populate(self, document: dict[str, Any]) -> None:
        clear_layout(self.body)
        self.set_status("Colors are written to the keyboard controller; there is no physical readback."
                        if not self.app.demo else "Demo lighting preview - nothing is written.")
        self.settings = {}
        for zone in LIGHTING_ZONE_ORDER:
            entry = (document.get("zones") or {}).get(zone.name.lower(), {})
            color = entry.get("color", DEFAULT_LIGHTING_COLOR.hex)
            self.settings[zone] = {"color": RgbColor.from_hex(color), "on": entry.get("on", True)}

        zones_frame, zones_layout = section("Zones")
        tiles: list[QWidget] = []
        all_tile = panel()
        all_layout = QVBoxLayout(all_tile)
        all_layout.addWidget(QLabel("ALL ZONES", objectName="metricName"))
        pick_all = QPushButton("Set color")
        pick_all.clicked.connect(lambda: self.choose_color(None))
        all_layout.addWidget(pick_all)
        buttons = QHBoxLayout()
        all_on = QPushButton("All on")
        all_on.clicked.connect(lambda: self.set_all_on(True))
        all_off = QPushButton("All off")
        all_off.clicked.connect(lambda: self.set_all_on(False))
        buttons.addWidget(all_on)
        buttons.addWidget(all_off)
        all_layout.addLayout(buttons)
        self.all_on_button, self.all_off_button = all_on, all_off
        tiles.append(all_tile)
        for zone in LIGHTING_ZONE_ORDER:
            tile = panel()
            tile_layout = QVBoxLayout(tile)
            header = QHBoxLayout()
            header.addWidget(QLabel(zone.label.upper(), objectName="metricName"))
            header.addStretch()
            switch = ToggleSwitch()
            switch.setAccessibleName(f"{zone.label} on")
            switch.setChecked(self.settings[zone]["on"])
            switch.toggled.connect(lambda checked, z=zone: self.set_on(z, checked))
            header.addWidget(switch)
            tile_layout.addLayout(header)
            swatch = QPushButton()
            swatch.setMinimumHeight(64)
            swatch.clicked.connect(lambda _c=False, z=zone: self.choose_color(z))
            tile_layout.addWidget(swatch)
            self.swatches[zone] = swatch
            self.switches[zone] = switch
            tiles.append(tile)
        zones_layout.addWidget(ResponsiveGrid(tiles, wide_columns=5, medium_columns=2,
                                              medium_width=520, wide_width=940))
        self.body.addWidget(zones_frame)

        bright_frame, bright_layout = section("Brightness")
        self.brightness = ValueSlider("Brightness", 0, 100, int(document.get("brightness", 100)), unit="%", step=5)
        self.brightness.slider.valueChanged.connect(lambda _v: self._dirty())
        bright_layout.addWidget(self.brightness)
        bright_layout.addWidget(muted("Brightness scales the RGB values sent to each zone; 0 % switches all off."))
        self.body.addWidget(bright_frame)

        row = QHBoxLayout()
        row.addWidget(self.restore_checkbox("lighting", "Re-apply lighting at startup"))
        row.addStretch()
        self.apply_button = QPushButton("Apply lighting")
        self.apply_button.setProperty("role", "primary")
        self.apply_button.clicked.connect(self.apply)
        row.addWidget(self.apply_button)
        self.body.addLayout(row)
        self.refresh_swatches()

    def _dirty(self) -> None:
        self.set_status("Unapplied changes - select Apply lighting.", "warning")

    def choose_color(self, zone: LightZone | None) -> None:
        current = self.settings[zone or LightZone.KEYBOARD]["color"]
        title = "All lighting zones" if zone is None else f"{zone.label} color"
        selected = QColorDialog.getColor(QColor(current.hex), self, title)
        if not selected.isValid():
            return
        color = RgbColor(selected.red(), selected.green(), selected.blue())
        for target in (LIGHTING_ZONE_ORDER if zone is None else (zone,)):
            self.settings[target]["color"] = color
            self.settings[target]["on"] = True
            self.switches[target].setChecked(True)
        self.refresh_swatches()
        self._dirty()

    def set_on(self, zone: LightZone, on: bool) -> None:
        self.settings[zone]["on"] = on
        self.refresh_swatches()
        self._dirty()

    def set_all_on(self, on: bool) -> None:
        for zone in LIGHTING_ZONE_ORDER:
            self.switches[zone].setChecked(on)

    def refresh_swatches(self) -> None:
        for zone, swatch in self.swatches.items():
            setting = self.settings[zone]
            if setting["on"]:
                color = setting["color"]
                swatch.setText(color.hex.upper())
                swatch.setStyleSheet(
                    f"QPushButton {{ background: {color.hex}; color: {lighting_text_color(color)}; "
                    f"border: 1px solid {BORDER}; border-radius: 4px; font-weight: 800; }}"
                    f"QPushButton:hover {{ border: 2px solid {ACCENT}; }}")
            else:
                swatch.setText("OFF")
                swatch.setStyleSheet(
                    f"QPushButton {{ background: #000; color: {MUTED}; border: 1px dashed {BORDER}; "
                    f"border-radius: 4px; font-weight: 800; letter-spacing: 3px; }}")

    def document(self) -> dict[str, Any]:
        return {
            "brightness": self.brightness.value() if self.brightness else 100,
            "zones": {zone.name.lower(): {"color": s["color"].hex, "on": s["on"]}
                      for zone, s in self.settings.items()},
        }

    def apply(self) -> None:
        document = self.document()
        dark = [z.label for z, s in self.settings.items() if not s["on"]]
        summary = f"\n\nSwitched off: {', '.join(dark)}" if dark else ""
        if not self.confirm("Apply lighting", f"Apply and save these lighting settings?{summary}"):
            return
        self.call("lighting.apply", {**document, "save": True},
                  lambda _d: self.set_status("Lighting applied and saved.", "ok"), busy="Applying lighting...")


# ------------------------------------------------------------- demo helpers
class _DemoMixer:
    def cards(self) -> list[dict[str, Any]]:
        return [{"index": 0, "id": "PCH", "driver": "HDA-Intel", "name": "HDA Intel PCH (demo)"}]

    def controls(self, card: int) -> list[MixerControl]:
        return [
            MixerControl(1, "MIXER", "Master Playback Volume", "INTEGER", "rw---R--", ["64", "64"], 0, 87),
            MixerControl(2, "MIXER", "Master Playback Switch", "BOOLEAN", "rw------", ["on"]),
            MixerControl(3, "MIXER", "Headphone Playback Volume", "INTEGER", "rw---R--", ["87", "87"], 0, 87),
            MixerControl(4, "MIXER", "Capture Volume", "INTEGER", "rw---R--", ["40", "40"], 0, 63),
            MixerControl(5, "MIXER", "Internal Mic Boost Volume", "INTEGER", "rw---R--", ["1", "1"], 0, 3),
            MixerControl(6, "MIXER", "Auto-Mute Mode", "ENUMERATED", "rw------", ["1"],
                         items=["Disabled", "Enabled"]),
            MixerControl(7, "MIXER", "Loopback Mixing", "ENUMERATED", "rw------", ["0"],
                         items=["Disabled", "Enabled"]),
        ]

    def set(self, card: int, control: MixerControl, value: Any) -> None:
        pass


class _DemoKScreen:
    def outputs(self) -> list[dict[str, Any]]:
        return [{"name": "eDP-1", "current_mode": "1", "resolution": "2560x1440", "vrr_policy": 2,
                 "modes": [{"id": "1", "refresh": 165.0, "name": "2560x1440@165"},
                           {"id": "2", "refresh": 60.0, "name": "2560x1440@60"}]}]

    def set_mode(self, output: str, mode_id: str) -> None:
        pass

    def set_vrr(self, output: str, policy: str) -> None:
        pass


# -------------------------------------------------------------- main window
class FanControlApp(QMainWindow):
    POLL_INTERVAL_MS = 1500

    def __init__(self, *, demo: bool = False, backend: Backend | None = None,
                 mixer: Any = None, kscreen: Any = None) -> None:
        super().__init__()
        raw = backend or (DemoBackend() if demo else DaemonClient())
        self.demo = raw.demo
        self.backend = AsyncBackend(raw, self._backend_error)
        self.mixer = mixer or (_DemoMixer() if self.demo else AlsaMixer())
        self.kscreen = kscreen or (_DemoKScreen() if self.demo else KScreen())
        self.history = TelemetryHistory()
        self.restore_flags: dict[str, bool] = {}
        self._current_status: FanStatus | None = None
        self._change_in_progress = False
        self._poll_in_flight = False
        self._closing = False

        self._poll_timer = QTimer(self)
        self._poll_timer.setSingleShot(True)
        self._poll_timer.setInterval(self.POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._request_status)

        title = "Erazer Control - Major X10"
        self.setWindowTitle(f"{title} (demo)" if self.demo else title)
        self.setWindowIcon(app_icon())
        self.resize(1240, 840)
        self.setMinimumSize(760, 560)
        load_brand_font()
        self.setStyleSheet(APP_STYLESHEET)
        self._build()
        self._set_controls_enabled(False)
        QTimer.singleShot(0, self._connect)

    # ----------------------------------------------------------- layout
    def _build(self) -> None:
        root = QWidget(objectName="root")
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        sidebar = QWidget(objectName="sidebar")
        sidebar.setFixedWidth(224)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(0, 22, 0, 16)
        side.setSpacing(2)
        brand_row = QHBoxLayout()
        brand_row.setContentsMargins(16, 0, 12, 20)
        brand_row.setSpacing(10)
        self.logo = QLabel(objectName="logo")
        self.logo.setPixmap(logo_pixmap(44, self.devicePixelRatioF()))
        self.logo.setAccessibleName("Erazer Control logo")
        brand_row.addWidget(self.logo)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        brand_text.addWidget(QLabel("ERAZER", objectName="brand"))
        brand_text.addWidget(QLabel("MAJOR X10 CONTROL", objectName="brandSub"))
        brand_row.addLayout(brand_text, 1)
        side.addLayout(brand_row)
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons: list[QPushButton] = []
        for index, (name, glyph) in enumerate(zip(PAGES, PAGE_ICONS, strict=True)):
            button = QPushButton(icon(glyph), f"  {name}")
            button.setIconSize(QSize(20, 20))
            button.setProperty("nav", True)
            button.setCheckable(True)
            button.clicked.connect(lambda _c=False, i=index: self.show_page(i))
            self.nav_group.addButton(button, index)
            self.nav_buttons.append(button)
            side.addWidget(button)
        side.addStretch()
        self.security_label = QLabel("")
        self.security_label.setObjectName("metricName")
        self.security_label.setWordWrap(True)
        self.security_label.setContentsMargins(20, 0, 12, 0)
        side.addWidget(self.security_label)
        outer.addWidget(sidebar)

        main = QVBoxLayout()
        main.setContentsMargins(24, 18, 24, 12)
        main.setSpacing(10)
        header = QHBoxLayout()
        header.addStretch()
        self.profile_badge = QLabel("UNKNOWN MODE", objectName="badge")
        header.addWidget(self.profile_badge)
        main.addLayout(header)

        self.dashboard = DashboardPage(self)
        self.cpu_page = CpuPage(self)
        self.gpu_page = GpuPage(self)
        self.display_page = DisplayPage(self)
        self.audio_page = AudioPage(self)
        self.led_page = LedPage(self)
        self.pages: list[Page] = [self.dashboard, self.cpu_page, self.gpu_page,
                                  self.display_page, self.audio_page, self.led_page]
        self.stack = QStackedWidget()
        for page in self.pages:
            self.stack.addWidget(scrollable(page))
        main.addWidget(self.stack, 1)

        footer = QHBoxLayout()
        self.connection_label = QLabel("CONNECTING", objectName="metricName")
        footer.addWidget(self.connection_label)
        self.status_label = QLabel("Connecting to x10ctld...", objectName="status")
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        footer.addWidget(self.status_label, 1)
        self.retry_button = QPushButton("Retry connection")
        self.retry_button.clicked.connect(self._connect)
        footer.addWidget(self.retry_button)
        main.addLayout(footer)

        container = QWidget()
        container.setLayout(main)
        outer.addWidget(container, 1)
        self.setCentralWidget(root)
        self.show_page(0)

    def show_page(self, index: int) -> None:
        self.nav_buttons[index].setChecked(True)
        self.stack.setCurrentIndex(index)

    # aliases kept for compatibility with earlier tests/tools
    @property
    def full_speed_check(self) -> QCheckBox:
        return self.dashboard.full_speed_check

    @property
    def _mode_buttons(self) -> dict[Profile, QRadioButton]:
        return self.dashboard.mode_buttons

    # ------------------------------------------------------- connection
    def _backend_error(self, method: str, error: BackendError) -> None:
        self.status_label.setStyleSheet(f"color: {ERROR};")
        self.status_label.setText(f"{method}: {error}")

    def _connect(self) -> None:
        if self._closing:
            return
        self.retry_button.setEnabled(False)
        self.connection_label.setText("CONNECTING")
        self.status_label.setStyleSheet("")
        self.status_label.setText("Connecting to x10ctld...")
        self.backend.call("system.info", None, self._info_received, self._connection_failed)

    def _info_received(self, info: dict[str, Any]) -> None:
        parts = []
        if info.get("secure_boot"):
            parts.append("SECURE BOOT ON")
        if info.get("kernel_module"):
            parts.append("SIGNED MODULE")
        self.security_label.setText("  ·  ".join(parts) or "LEGACY /dev/port")
        self.backend.call("restore.get", None, self._restore_received)
        self.backend.call("ec.status", None, self._session_opened, self._connection_failed)

    def _restore_received(self, flags: dict[str, bool]) -> None:
        self.restore_flags = dict(flags)

    def set_restore_flag(self, key: str, value: bool) -> None:
        self.restore_flags[key] = value
        self.backend.call("restore.set", {key: value}, self._restore_received)

    def _session_opened(self, status: dict[str, Any]) -> None:
        self.connection_label.setText("DEMO DATA" if self.demo else "EC CONNECTED")
        self.retry_button.setEnabled(False)
        self._apply_status(status)
        self._set_controls_enabled(True)
        self.status_label.setText(
            "Demo mode: hardware and saved settings are untouched." if self.demo else
            "Connected through x10ctld. Closing the app leaves the firmware in its last state.")
        self._schedule_poll()

    def _connection_failed(self, error: BackendError) -> None:
        self._poll_timer.stop()
        self._current_status = None
        self._change_in_progress = False
        self._poll_in_flight = False
        self._set_controls_enabled(False)
        self.connection_label.setText("OFFLINE")
        self.status_label.setStyleSheet(f"color: {ERROR};")
        self.status_label.setText(f"{error} Retry the connection before making further changes.")
        self.profile_badge.setText("UNAVAILABLE")
        self.dashboard.power_label.setText("Power supply: unknown")
        for label in (self.dashboard.cpu_rpm_label, self.dashboard.gpu_rpm_label,
                      self.dashboard.cpu_temp_label, self.dashboard.gpu_temp_label):
            label.setText("--")
        self.retry_button.setEnabled(True)
        self.dashboard.graph.update()

    # -------------------------------------------------------- telemetry
    def _schedule_poll(self) -> None:
        if not self._closing and self._current_status is not None:
            self._poll_timer.start()

    def _request_status(self) -> None:
        if self._closing or self._poll_in_flight or self._change_in_progress:
            self._schedule_poll()
            return
        self._poll_in_flight = True
        self.backend.call("ec.status", None, self._status_received, self._poll_failed)

    def _status_received(self, status: dict[str, Any]) -> None:
        self._poll_in_flight = False
        if self._change_in_progress:
            return
        self._apply_status(status)
        self._set_controls_enabled(True)
        self._schedule_poll()

    def _poll_failed(self, error: BackendError) -> None:
        self._poll_in_flight = False
        self._connection_failed(BackendError(f"Telemetry unavailable: {error}"))

    @staticmethod
    def _to_status(data: dict[str, Any]) -> FanStatus:
        return FanStatus(
            profile=Profile[str(data["profile"]).upper()],
            full_speed=bool(data["full_speed"]),
            cpu_fan_rpm=int(data["cpu_fan_rpm"]),
            gpu_fan_rpm=int(data["gpu_fan_rpm"]),
            cpu_temperature_c=int(data["cpu_temperature_c"]),
            gpu_temperature_c=int(data["gpu_temperature_c"]),
            turbo_available=bool(data.get("turbo_available", True)),
        )

    def _apply_status(self, data: dict[str, Any] | FanStatus) -> None:
        status = data if isinstance(data, FanStatus) else self._to_status(data)
        self._current_status = status
        self.history.append(status)
        page = self.dashboard
        page.mode_buttons[status.profile].setChecked(True)
        page.full_speed_check.setChecked(status.full_speed)
        page.cpu_rpm_label.setText(f"{status.cpu_fan_rpm:,}")
        page.gpu_rpm_label.setText(f"{status.gpu_fan_rpm:,}")
        page.cpu_temp_label.setText(str(status.cpu_temperature_c))
        page.gpu_temp_label.setText(str(status.gpu_temperature_c))
        override = " / FULL SPEED" if status.full_speed else ""
        self.profile_badge.setText(f"{status.profile.label.upper()}{override}")
        page.power_label.setText(
            "Power supply: simulated" if self.demo else
            "AC barrel adapter connected" if status.turbo_available else
            "Turbo requires the AC barrel adapter; USB-C PD is insufficient.")
        page.graph.update()

    # ---------------------------------------------------------- changes
    def _confirm(self, title: str, text: str) -> bool:
        return QMessageBox.question(
            self, title, text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes

    def _restore_current_controls(self) -> None:
        if self._current_status is not None:
            self.dashboard.mode_buttons[self._current_status.profile].setChecked(True)
            self.dashboard.full_speed_check.setChecked(self._current_status.full_speed)

    def request_profile_change(self, profile: Profile) -> None:
        if self._current_status is None or self._change_in_progress or self._closing:
            self._restore_current_controls()
            return
        previous = self._current_status.profile
        self._restore_current_controls()
        if profile == previous:
            return
        if profile == Profile.TURBO and not self._current_status.turbo_available:
            self.status_label.setText(TURBO_POWER_MESSAGE)
            QMessageBox.warning(self, "Turbo mode unavailable", TURBO_POWER_MESSAGE)
            return
        if not self._confirm("Change performance mode", f"Set the firmware performance mode to "
                                                         f"{profile.label.title()}?"):
            return
        self._submit_change("ec.set_profile", {"profile": profile.label}, f"Applying {profile.label.title()}...")

    def request_full_speed_change(self) -> None:
        if self._current_status is None or self._change_in_progress or self._closing:
            self._restore_current_controls()
            return
        requested = self.dashboard.full_speed_check.isChecked()
        self._restore_current_controls()
        action = "Enable" if requested else "Disable"
        if not self._confirm("Change full-speed override",
                             f"{action} the firmware full-speed fan override?\n\n"
                             "Closing the app leaves this setting unchanged."):
            return
        self._submit_change("ec.set_full_speed", {"enabled": requested},
                            "Enabling full-speed override..." if requested else "Disabling full-speed override...")

    def _submit_change(self, method: str, params: dict[str, Any], text: str) -> None:
        self._poll_timer.stop()
        self._change_in_progress = True
        self._set_controls_enabled(False)
        self.status_label.setStyleSheet("")
        self.status_label.setText(text)
        self.backend.call(method, params, self._change_completed, self._change_failed)

    def _change_completed(self, status: dict[str, Any]) -> None:
        self._change_in_progress = False
        self._apply_status(status)
        self.status_label.setText("Demo setting updated." if self.demo else "Firmware change confirmed.")
        self._set_controls_enabled(True)
        self._schedule_poll()

    def _change_failed(self, error: BackendError) -> None:
        self._change_in_progress = False
        if error.kind == "InsufficientPowerError":
            self.status_label.setText(str(error))
            QMessageBox.warning(self, "Turbo mode unavailable", str(error))
            self._restore_current_controls()
            self._set_controls_enabled(True)
            self._schedule_poll()
            return
        self._connection_failed(BackendError(f"Change could not be confirmed: {error}"))

    def _set_controls_enabled(self, enabled: bool) -> None:
        for profile, button in self.dashboard.mode_buttons.items():
            blocked = (profile == Profile.TURBO and self._current_status is not None
                       and not self._current_status.turbo_available)
            button.setEnabled(enabled and not blocked)
            button.setToolTip(TURBO_POWER_MESSAGE if blocked else f"Select {profile.label.title()} mode")
        self.dashboard.full_speed_check.setEnabled(enabled)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt naming
        if not self._closing:
            self._closing = True
            self._poll_timer.stop()
            self.backend.shutdown(wait=False)
        event.accept()


__all__ = ["FanControlApp", "AsyncBackend"]
