from __future__ import annotations

import argparse
import math
import time
import tkinter as tk
from collections import deque
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from tkinter import colorchooser, messagebox, ttk
from typing import Protocol

from .hardware import open_supported_protocol
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


class TelemetryGraph(tk.Canvas):
    def __init__(self, master: tk.Misc, history: TelemetryHistory, window_seconds: tk.IntVar) -> None:
        super().__init__(master, background=SURFACE, highlightthickness=0, height=330)
        self._history = history
        self._window_seconds = window_seconds
        self.bind("<Configure>", lambda _event: self.redraw())

    def redraw(self) -> None:
        self.delete("all")
        width = max(self.winfo_width(), 640)
        height = max(self.winfo_height(), 300)
        left, top, right, bottom = 66, 28, width - 66, height - 50
        now = time.monotonic()
        duration = self._window_seconds.get()
        samples = self._history.samples_for_window(duration, now=now)
        maximum_rpm = max(
            (max(sample.status.cpu_fan_rpm, sample.status.gpu_fan_rpm) for sample in samples),
            default=5000,
        )
        fan_max = max(6000, int(math.ceil(maximum_rpm / 1000)) * 1000)
        maximum_temperature = max(
            (max(sample.status.cpu_temperature_c, sample.status.gpu_temperature_c) for sample in samples),
            default=100,
        )
        temperature_max = max(100, int(math.ceil(maximum_temperature / 20)) * 20)

        for step in range(5):
            fraction = step / 4
            y = bottom - fraction * (bottom - top)
            rpm = round(fraction * fan_max)
            temperature = round(fraction * temperature_max)
            self.create_line(left, y, right, y, fill=GRID, width=1)
            self.create_text(left - 12, y, text=f"{rpm:,}", anchor="e", fill=MUTED, font=("DejaVu Sans", 9))
            self.create_text(right + 12, y, text=str(temperature), anchor="w", fill=MUTED, font=("DejaVu Sans", 9))

        for step in range(5):
            fraction = step / 4
            x = left + fraction * (right - left)
            age = round(duration * (1 - fraction))
            self.create_line(x, top, x, bottom, fill=GRID, width=1)
            label = "NOW" if age == 0 else f"-{format_duration(age)}"
            self.create_text(x, bottom + 17, text=label, fill=MUTED, font=("DejaVu Sans", 9))

        self.create_line(left, top, left, bottom, right, bottom, fill=INK, width=2)
        self.create_line(right, top, right, bottom, fill=INK, width=2)
        self.create_text(16, (top + bottom) / 2, text="FAN RPM", angle=90, fill=MUTED, font=("DejaVu Sans", 9, "bold"))
        self.create_text(width - 16, (top + bottom) / 2, text="TEMP C", angle=90, fill=MUTED, font=("DejaVu Sans", 9, "bold"))
        self.create_text((left + right) / 2, height - 13, text="TIME", fill=MUTED, font=("DejaVu Sans", 9, "bold"))

        if not samples:
            self.create_text(
                (left + right) / 2,
                (top + bottom) / 2,
                text="WAITING FOR EC TELEMETRY",
                fill=MUTED,
                font=("DejaVu Sans", 11, "bold"),
            )
            return

        start = now - duration
        self._draw_series(samples, "cpu_fan_rpm", CPU_COLOR, start, duration, left, top, right, bottom, fan_max)
        self._draw_series(samples, "gpu_fan_rpm", GPU_COLOR, start, duration, left, top, right, bottom, fan_max)
        self._draw_series(
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
            dash=(6, 4),
        )
        self._draw_series(
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
            dash=(6, 4),
        )

    def _draw_series(
        self,
        samples: tuple[TelemetrySample, ...],
        field: str,
        color: str,
        start: float,
        duration: int,
        left: int,
        top: int,
        right: int,
        bottom: int,
        value_max: int,
        *,
        dash: tuple[int, int] | None = None,
    ) -> None:
        coordinates: list[float] = []
        for sample in samples:
            value = getattr(sample.status, field)
            coordinates.extend(
                (
                    left + (sample.recorded_at - start) / duration * (right - left),
                    bottom - min(value, value_max) / value_max * (bottom - top),
                )
            )
        if len(coordinates) >= 4:
            self.create_line(*coordinates, fill=color, width=2, smooth=True, dash=dash)
        x, y = coordinates[-2:]
        self.create_oval(x - 4, y - 4, x + 4, y + 4, fill=color, outline=SURFACE, width=1)


class FanControlApp:
    POLL_INTERVAL_MS = 1500

    def __init__(self, root: tk.Tk, *, demo: bool = False) -> None:
        self.root = root
        self.demo = demo
        self.history = TelemetryHistory()
        factory = open_demo_protocol if demo else open_supported_protocol
        self.session = ProtocolSession(factory)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="medion-ec")
        self.lighting_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="medion-lighting")
        self._pending: Future[FanStatus] | None = None
        self._lighting_pending: Future[None] | None = None
        self._poll_after_id: str | None = None
        self._closing = False
        self._current_status: FanStatus | None = None
        self._mode_buttons: list[tk.Radiobutton] = []
        self._lighting_load_error: str | None = None
        try:
            self._lighting_colors = load_lighting_colors()
        except LightingAccessError as error:
            self._lighting_colors = {}
            self._lighting_load_error = str(error)
        self._lighting_swatches: dict[LightZone, tk.Button] = {}

        self._profile_var = tk.IntVar(value=Profile.OFFICE.value)
        self._full_speed_var = tk.BooleanVar(value=False)
        self._connection_var = tk.StringVar(value="CONNECTING")
        self._status_var = tk.StringVar(value="Opening the verified EC transport...")
        self._cpu_rpm_var = tk.StringVar(value="--")
        self._gpu_rpm_var = tk.StringVar(value="--")
        self._cpu_temp_var = tk.StringVar(value="--")
        self._gpu_temp_var = tk.StringVar(value="--")
        self._profile_label_var = tk.StringVar(value="UNKNOWN MODE")
        self._graph_window_var = tk.IntVar(value=180)
        self._graph_window_label_var = tk.StringVar(value=format_duration(self._graph_window_var.get()))

        self._configure_window()
        self._build_styles()
        self._build_layout()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._set_controls_enabled(False)
        self._update_lighting_controls()
        self._open_session()

    def _configure_window(self) -> None:
        self.root.title("Medion Major X10 Control")
        self.root.geometry("1180x820")
        self.root.minsize(900, 720)
        self.root.configure(background=BACKGROUND)

    def _build_styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("App.TFrame", background=BACKGROUND)
        style.configure("Surface.TFrame", background=SURFACE)
        style.configure("Metric.TFrame", background=SURFACE, borderwidth=1, relief="solid")
        style.configure("Title.TLabel", background=BACKGROUND, foreground=INK, font=("DejaVu Sans", 24, "bold"))
        style.configure("Subtitle.TLabel", background=BACKGROUND, foreground=MUTED, font=("DejaVu Sans", 10))
        style.configure("GraphTitle.TLabel", background=SURFACE, foreground=INK, font=("DejaVu Sans", 15, "bold"))
        style.configure("GraphMeta.TLabel", background=SURFACE, foreground=MUTED, font=("DejaVu Sans", 9))
        style.configure("MetricName.TLabel", background=SURFACE, foreground=MUTED, font=("DejaVu Sans", 9, "bold"))
        style.configure("MetricValue.TLabel", background=SURFACE, foreground=INK, font=("DejaVu Sans Mono", 22, "bold"))
        style.configure("Status.TLabel", background=BACKGROUND, foreground=MUTED, font=("DejaVu Sans", 9))
        style.configure("FullSpeed.TCheckbutton", background=SIDEBAR, foreground="white", font=("DejaVu Sans", 10, "bold"))
        style.map("FullSpeed.TCheckbutton", background=[("active", SIDEBAR)], foreground=[("disabled", SIDEBAR_MUTED)])

    def _build_layout(self) -> None:
        shell = ttk.Frame(self.root, style="App.TFrame")
        shell.pack(fill="both", expand=True)
        shell.columnconfigure(1, weight=1)
        shell.rowconfigure(0, weight=1)

        sidebar = tk.Frame(shell, background=SIDEBAR, width=252)
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_propagate(False)
        sidebar.columnconfigure(0, weight=1)

        tk.Label(
            sidebar,
            text="MEDION",
            background=SIDEBAR,
            foreground="white",
            font=("DejaVu Sans", 11, "bold"),
        ).grid(row=0, column=0, sticky="w", padx=28, pady=(30, 0))
        tk.Label(
            sidebar,
            text="MAJOR X10",
            background=SIDEBAR,
            foreground="white",
            font=("DejaVu Sans", 21, "bold"),
        ).grid(row=1, column=0, sticky="w", padx=28, pady=(0, 34))

        self._sidebar_heading(sidebar, "PERFORMANCE MODE", row=2)
        for row_offset, profile in enumerate(Profile, start=3):
            button = tk.Radiobutton(
                sidebar,
                text=profile.label.upper(),
                variable=self._profile_var,
                value=profile.value,
                command=lambda selected=profile: self._request_profile_change(selected),
                indicatoron=False,
                anchor="w",
                padx=18,
                pady=11,
                borderwidth=0,
                highlightthickness=0,
                selectcolor=ACCENT,
                background="#214b41",
                activebackground="#2b5b4f",
                foreground="white",
                activeforeground="white",
                disabledforeground=SIDEBAR_MUTED,
                font=("DejaVu Sans", 10, "bold"),
            )
            button.grid(row=row_offset, column=0, sticky="ew", padx=28, pady=3)
            self._mode_buttons.append(button)

        self._sidebar_heading(sidebar, "FAN OVERRIDE", row=6, top_padding=31)
        self.full_speed_check = ttk.Checkbutton(
            sidebar,
            text="Full speed",
            variable=self._full_speed_var,
            command=self._request_full_speed_change,
            style="FullSpeed.TCheckbutton",
            cursor="hand2",
        )
        self.full_speed_check.grid(row=7, column=0, sticky="w", padx=28, pady=(10, 0))

        sidebar.rowconfigure(8, weight=1)
        connection = tk.Frame(sidebar, background="#102f28")
        connection.grid(row=9, column=0, sticky="sew", padx=18, pady=18)
        tk.Label(
            connection,
            textvariable=self._connection_var,
            background="#102f28",
            foreground="#b9d8cd",
            font=("DejaVu Sans Mono", 9, "bold"),
        ).pack(anchor="w", padx=12, pady=(10, 3))
        self.retry_button = tk.Button(
            connection,
            text="Retry connection",
            command=self._open_session,
            borderwidth=0,
            background="#29594d",
            activebackground="#34705f",
            foreground="white",
            activeforeground="white",
            font=("DejaVu Sans", 9, "bold"),
            padx=10,
            pady=7,
        )
        self.retry_button.pack(fill="x", padx=8, pady=(4, 8))

        content = ttk.Frame(shell, style="App.TFrame", padding=(28, 24, 28, 18))
        content.grid(row=0, column=1, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(3, weight=1)

        heading = ttk.Frame(content, style="App.TFrame")
        heading.grid(row=0, column=0, sticky="ew", pady=(0, 20))
        heading.columnconfigure(0, weight=1)
        ttk.Label(heading, text="Fan control", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(heading, text="Verified firmware telemetry and modes", style="Subtitle.TLabel").grid(
            row=1, column=0, sticky="w", pady=(2, 0)
        )
        tk.Label(
            heading,
            textvariable=self._profile_label_var,
            background=WARNING,
            foreground=INK,
            font=("DejaVu Sans", 9, "bold"),
            padx=12,
            pady=7,
        ).grid(row=0, column=1, rowspan=2, sticky="e")

        metrics = ttk.Frame(content, style="App.TFrame")
        metrics.grid(row=1, column=0, sticky="ew", pady=(0, 20))
        for column in range(4):
            metrics.columnconfigure(column, weight=1, uniform="metrics")
        self._metric(metrics, 0, "CPU FAN", self._cpu_rpm_var, "RPM", CPU_COLOR)
        self._metric(metrics, 1, "GPU FAN", self._gpu_rpm_var, "RPM", GPU_COLOR)
        self._metric(metrics, 2, "CPU TEMP", self._cpu_temp_var, "C", CPU_COLOR)
        self._metric(metrics, 3, "GPU TEMP", self._gpu_temp_var, "C", GPU_COLOR)

        lighting_panel = ttk.Frame(content, style="Surface.TFrame", padding=(18, 12))
        lighting_panel.grid(row=2, column=0, sticky="ew", pady=(0, 20))
        lighting_panel.columnconfigure(0, weight=1)
        ttk.Label(lighting_panel, text="Lighting", style="GraphTitle.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        zones = ttk.Frame(lighting_panel, style="Surface.TFrame")
        zones.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        for column in range(len(LIGHTING_ZONE_ORDER) + 1):
            zones.columnconfigure(column, weight=1, uniform="lighting-zones")
        self.all_lighting_button = tk.Button(
            zones,
            text="All zones\nSet color",
            command=lambda: self._choose_lighting_color(None),
            background=SURFACE,
            activebackground=GRID,
            foreground=INK,
            activeforeground=INK,
            relief="solid",
            borderwidth=1,
            cursor="hand2",
            font=("DejaVu Sans", 9, "bold"),
            pady=7,
        )
        self.all_lighting_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        for column, zone in enumerate(LIGHTING_ZONE_ORDER, start=1):
            swatch = tk.Button(
                zones,
                command=lambda selected=zone: self._choose_lighting_color(selected),
                background=BORDER,
                activebackground=BORDER,
                foreground=INK,
                activeforeground=INK,
                relief="solid",
                borderwidth=1,
                cursor="hand2",
                font=("DejaVu Sans", 9, "bold"),
                pady=7,
            )
            swatch.grid(
                row=0,
                column=column,
                sticky="ew",
                padx=(4, 0 if column == len(LIGHTING_ZONE_ORDER) else 4),
            )
            self._lighting_swatches[zone] = swatch
        self._refresh_lighting_swatches()
        self.apply_lighting_button = tk.Button(
            lighting_panel,
            text="Apply colors",
            command=self._request_lighting_change,
            borderwidth=0,
            background=ACCENT,
            activebackground="#0c8c65",
            foreground="white",
            activeforeground="white",
            disabledforeground="#d5e6df",
            font=("DejaVu Sans", 9, "bold"),
            padx=14,
            pady=8,
        )
        self.apply_lighting_button.grid(row=0, column=1, sticky="e", padx=(18, 0))

        graph_panel = ttk.Frame(content, style="Surface.TFrame", padding=(22, 18, 22, 10))
        graph_panel.grid(row=3, column=0, sticky="nsew")
        graph_panel.columnconfigure(0, weight=1)
        graph_panel.rowconfigure(2, weight=1)
        ttk.Label(graph_panel, text="Fan speed and temperature", style="GraphTitle.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        toolbar = ttk.Frame(graph_panel, style="Surface.TFrame")
        toolbar.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        toolbar.columnconfigure(0, weight=1)
        legend = ttk.Frame(toolbar, style="Surface.TFrame")
        legend.grid(row=0, column=0, sticky="w")
        self._legend_item(legend, "CPU fan", CPU_COLOR, 0)
        self._legend_item(legend, "GPU fan", GPU_COLOR, 1)
        self._legend_item(legend, "CPU temp", CPU_COLOR, 2, dash=(6, 4))
        self._legend_item(legend, "GPU temp", GPU_COLOR, 3, dash=(6, 4))
        self.graph = TelemetryGraph(graph_panel, self.history, self._graph_window_var)
        self.graph.grid(row=2, column=0, sticky="nsew", pady=(4, 0))
        window_control = ttk.Frame(toolbar, style="Surface.TFrame")
        window_control.grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Label(window_control, text="WINDOW", style="GraphMeta.TLabel").pack(side="left")
        tk.Scale(
            window_control,
            from_=30,
            to=600,
            resolution=30,
            orient="horizontal",
            showvalue=False,
            length=150,
            variable=self._graph_window_var,
            command=self._graph_window_changed,
            background=SURFACE,
            activebackground=ACCENT,
            highlightthickness=0,
            troughcolor=GRID,
        ).pack(side="left", padx=6)
        ttk.Label(
            window_control,
            textvariable=self._graph_window_label_var,
            style="GraphMeta.TLabel",
            width=7,
            anchor="e",
        ).pack(side="left")

        ttk.Label(content, textvariable=self._status_var, style="Status.TLabel", anchor="w").grid(
            row=4, column=0, sticky="ew", pady=(12, 0)
        )

    def _sidebar_heading(self, parent: tk.Misc, text: str, *, row: int, top_padding: int = 0) -> None:
        tk.Label(
            parent,
            text=text,
            background=SIDEBAR,
            foreground=SIDEBAR_MUTED,
            font=("DejaVu Sans", 8, "bold"),
        ).grid(row=row, column=0, sticky="w", padx=28, pady=(top_padding, 8))

    def _metric(
        self,
        parent: ttk.Frame,
        column: int,
        name: str,
        value: tk.StringVar,
        unit: str,
        color: str,
    ) -> None:
        card = ttk.Frame(parent, style="Metric.TFrame", padding=(15, 13))
        card.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 5, 0 if column == 3 else 5))
        tk.Frame(card, background=color, width=4, height=52).grid(row=0, column=0, rowspan=2, sticky="ns", padx=(0, 12))
        ttk.Label(card, text=name, style="MetricName.TLabel").grid(row=0, column=1, sticky="w")
        value_row = ttk.Frame(card, style="Surface.TFrame")
        value_row.grid(row=1, column=1, sticky="w")
        ttk.Label(value_row, textvariable=value, style="MetricValue.TLabel").pack(side="left")
        ttk.Label(value_row, text=f" {unit}", style="MetricName.TLabel").pack(side="left", pady=(9, 0))

    def _legend_item(
        self,
        parent: ttk.Frame,
        text: str,
        color: str,
        column: int,
        *,
        dash: tuple[int, int] | None = None,
    ) -> None:
        canvas = tk.Canvas(parent, width=18, height=12, background=SURFACE, highlightthickness=0)
        canvas.create_line(1, 6, 17, 6, fill=color, width=2, dash=dash)
        canvas.grid(row=0, column=column * 2, padx=(0 if column == 0 else 12, 4))
        ttk.Label(parent, text=text, style="GraphMeta.TLabel").grid(row=0, column=column * 2 + 1)

    def _graph_window_changed(self, value: str) -> None:
        seconds = int(float(value))
        self._graph_window_label_var.set(format_duration(seconds))
        self.graph.redraw()

    def _choose_lighting_color(self, zone: LightZone | None) -> None:
        if zone is None:
            selected_colors = set(self._lighting_colors.values())
            current = selected_colors.pop() if len(selected_colors) == 1 else DEFAULT_LIGHTING_COLOR
            title = "All lighting zones"
        else:
            current = self._lighting_colors.get(zone, DEFAULT_LIGHTING_COLOR)
            title = f"{zone.label} color"
        _rgb, selected = colorchooser.askcolor(color=current.hex, title=title, parent=self.root)
        if selected is None:
            return
        color = RgbColor.from_hex(selected)
        target_zones = LIGHTING_ZONE_ORDER if zone is None else (zone,)
        for target_zone in target_zones:
            self._lighting_colors[target_zone] = color
        self._refresh_lighting_swatches()
        self._update_lighting_controls()

    def _refresh_lighting_swatches(self) -> None:
        for zone, swatch in self._lighting_swatches.items():
            color = self._lighting_colors.get(zone)
            if color is None:
                swatch.configure(
                    text=f"{zone.label}\nChoose color",
                    background=BORDER,
                    activebackground=BORDER,
                    foreground=INK,
                    activeforeground=INK,
                )
                continue
            text_color = lighting_text_color(color)
            swatch.configure(
                text=f"{zone.label}\n{color.hex.upper()}",
                background=color.hex,
                activebackground=color.hex,
                foreground=text_color,
                activeforeground=text_color,
            )

        all_colors = set(self._lighting_colors.values())
        if len(self._lighting_colors) == len(LIGHTING_ZONE_ORDER) and len(all_colors) == 1:
            color = all_colors.pop()
            text_color = lighting_text_color(color)
            self.all_lighting_button.configure(
                text=f"All zones\n{color.hex.upper()}",
                background=color.hex,
                activebackground=color.hex,
                foreground=text_color,
                activeforeground=text_color,
            )
        else:
            self.all_lighting_button.configure(
                text="All zones\nSet color",
                background=SURFACE,
                activebackground=GRID,
                foreground=INK,
                activeforeground=INK,
            )

    def _request_lighting_change(self) -> None:
        if self.demo or self._lighting_pending is not None or not self._lighting_colors:
            return
        colors = dict(self._lighting_colors)
        zone_names = ", ".join(zone.label for zone in colors)
        confirmed = messagebox.askyesno(
            "Apply lighting colors",
            f"Apply static colors to {zone_names}?",
            parent=self.root,
        )
        if not confirmed:
            return
        self._status_var.set(f"Applying static lighting to {zone_names}...")
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
            self.root.after(
                40,
                self._watch_lighting_future,
                future,
                colors,
                zone_names,
                save_on_success,
            )
            return
        self._lighting_pending = None
        try:
            future.result()
        except Exception as error:
            self._status_var.set(str(error))
        else:
            if save_on_success:
                try:
                    save_lighting_colors(colors)
                except LightingAccessError as error:
                    self._status_var.set(f"Lighting applied, but {error}")
                else:
                    self._status_var.set(f"Static lighting applied and saved for {zone_names}")
            else:
                self._status_var.set(f"Saved lighting reapplied to {zone_names}")
        self._update_lighting_controls()

    def _restore_saved_lighting(self) -> None:
        if self.demo or self._lighting_pending is not None or not self._lighting_colors:
            return
        colors = dict(self._lighting_colors)
        zone_names = ", ".join(zone.label for zone in colors)
        self._status_var.set(f"Reapplying saved lighting to {zone_names}...")
        self._lighting_pending = self.lighting_executor.submit(apply_static_lighting, colors)
        self._update_lighting_controls()
        self._watch_lighting_future(self._lighting_pending, colors, zone_names, save_on_success=False)

    def _update_lighting_controls(self) -> None:
        swatch_state = "disabled" if self.demo or self._closing or self._lighting_pending is not None else "normal"
        self.all_lighting_button.configure(state=swatch_state)
        for swatch in self._lighting_swatches.values():
            swatch.configure(state=swatch_state)
        can_apply = not self.demo and self._lighting_pending is None and bool(self._lighting_colors)
        self.apply_lighting_button.configure(state="normal" if can_apply else "disabled")

    def _open_session(self) -> None:
        if self._pending is not None or self._closing:
            return
        self._set_controls_enabled(False)
        self.retry_button.configure(state="disabled")
        self._connection_var.set("CONNECTING")
        self._status_var.set("Opening the verified EC transport...")
        self._pending = self.executor.submit(self.session.open)
        self._watch_future(self._pending, self._session_opened)

    def _watch_future(self, future: Future[FanStatus], callback: Callable[[Future[FanStatus]], None]) -> None:
        if self._closing:
            return
        if future.done():
            self._pending = None
            callback(future)
            return
        self.root.after(40, self._watch_future, future, callback)

    def _session_opened(self, future: Future[FanStatus]) -> None:
        try:
            status = future.result()
        except Exception as error:
            self._connection_var.set("OFFLINE")
            self._status_var.set(str(error))
            self.retry_button.configure(state="normal")
            return
        self._connection_var.set("DEMO DATA" if self.demo else "EC CONNECTED")
        self.retry_button.configure(state="disabled")
        self._set_controls_enabled(True)
        self._apply_status(status)
        if self._lighting_load_error is not None:
            self._status_var.set(self._lighting_load_error)
        else:
            self._restore_saved_lighting()
        self._schedule_poll()

    def _schedule_poll(self) -> None:
        if not self._closing:
            self._poll_after_id = self.root.after(self.POLL_INTERVAL_MS, self._request_status)

    def _request_status(self) -> None:
        self._poll_after_id = None
        if self._pending is not None or self._closing:
            self._schedule_poll()
            return
        self._pending = self.executor.submit(self.session.read_status)
        self._watch_future(self._pending, self._status_received)

    def _status_received(self, future: Future[FanStatus]) -> None:
        try:
            self._apply_status(future.result())
        except Exception as error:
            self._connection_var.set("READ ERROR")
            self._status_var.set(str(error))
        self._schedule_poll()

    def _request_profile_change(self, profile: Profile) -> None:
        if self._current_status is None or self._pending is not None:
            return
        previous = self._current_status.profile
        if profile == previous:
            return
        if profile == Profile.TURBO and not self._current_status.turbo_available:
            self._profile_var.set(previous.value)
            self._status_var.set(TURBO_POWER_MESSAGE)
            messagebox.showwarning("Turbo mode unavailable", TURBO_POWER_MESSAGE, parent=self.root)
            return
        confirmed = messagebox.askyesno(
            "Change performance mode",
            f"Set the firmware performance mode to {profile.label.title()}?",
            parent=self.root,
        )
        if not confirmed:
            self._profile_var.set(previous.value)
            return
        self._submit_change(
            lambda: self.session.set_profile(profile),
            f"Applying {profile.label.title()} mode...",
        )

    def _request_full_speed_change(self) -> None:
        if self._current_status is None or self._pending is not None:
            return
        requested = self._full_speed_var.get()
        previous = self._current_status.full_speed
        action = "enable" if requested else "disable"
        confirmed = messagebox.askyesno(
            "Change full-speed override",
            f"{action.title()} the firmware full-speed fan override?",
            parent=self.root,
        )
        if not confirmed:
            self._full_speed_var.set(previous)
            return
        self._submit_change(
            lambda: self.session.set_full_speed(requested),
            f"{action.title()}ing full-speed override...",
        )

    def _submit_change(self, operation: Callable[[], FanStatus], status_text: str) -> None:
        if self._poll_after_id is not None:
            self.root.after_cancel(self._poll_after_id)
            self._poll_after_id = None
        self._set_controls_enabled(False)
        self._status_var.set(status_text)
        self._pending = self.executor.submit(operation)
        self._watch_future(self._pending, self._change_completed)

    def _change_completed(self, future: Future[FanStatus]) -> None:
        try:
            status = future.result()
        except InsufficientPowerError as error:
            self._status_var.set(str(error))
            messagebox.showwarning("Turbo mode unavailable", str(error), parent=self.root)
            if self._current_status is not None:
                self._profile_var.set(self._current_status.profile.value)
                self._full_speed_var.set(self._current_status.full_speed)
        except Exception as error:
            self._status_var.set(str(error))
            if self._current_status is not None:
                self._profile_var.set(self._current_status.profile.value)
                self._full_speed_var.set(self._current_status.full_speed)
        else:
            self._apply_status(status)
        self._set_controls_enabled(True)
        self._schedule_poll()

    def _apply_status(self, status: FanStatus) -> None:
        self._current_status = status
        self.history.append(status)
        self._profile_var.set(status.profile.value)
        self._full_speed_var.set(status.full_speed)
        self._cpu_rpm_var.set(f"{status.cpu_fan_rpm:,}")
        self._gpu_rpm_var.set(f"{status.gpu_fan_rpm:,}")
        self._cpu_temp_var.set(str(status.cpu_temperature_c))
        self._gpu_temp_var.set(str(status.gpu_temperature_c))
        override = " / FULL SPEED" if status.full_speed else ""
        self._profile_label_var.set(f"{status.profile.label.upper()}{override}")
        self._connection_var.set("DEMO DATA" if self.demo else "EC CONNECTED")
        self._status_var.set("Firmware telemetry updated" if status.turbo_available else TURBO_POWER_MESSAGE)
        self.graph.redraw()

    def _set_controls_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for button in self._mode_buttons:
            button.configure(state=state)
        self.full_speed_check.configure(state=state)

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self._poll_after_id is not None:
            self.root.after_cancel(self._poll_after_id)
        self.executor.submit(self.session.close)
        self.executor.shutdown(wait=False, cancel_futures=False)
        self.lighting_executor.shutdown(wait=False, cancel_futures=False)
        self.root.destroy()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Medion Major X10 fan-control GUI")
    parser.add_argument("--demo", action="store_true", help="show simulated telemetry without hardware access")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = tk.Tk()
    FanControlApp(root, demo=args.demo)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())