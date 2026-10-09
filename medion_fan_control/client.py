"""GUI-side access to x10ctld, plus an in-process simulation for --demo."""
from __future__ import annotations

import copy
import itertools
import json
import math
import socket
import threading
import time
from pathlib import Path
from typing import Any, Protocol

from .daemon import SOCKET_PATH


class BackendError(RuntimeError):
    def __init__(self, message: str, kind: str = "Error") -> None:
        super().__init__(message)
        self.kind = kind


class DaemonUnavailable(BackendError):
    pass


class Backend(Protocol):
    demo: bool

    def call(self, method: str, **params: Any) -> Any: ...

    def close(self) -> None: ...


class DaemonClient:
    demo = False

    def __init__(self, path: Path = SOCKET_PATH, timeout: float = 45.0) -> None:
        self.path = path
        self.timeout = timeout
        self._socket: socket.socket | None = None
        self._reader = None
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def _connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(str(self.path))
        except FileNotFoundError as error:
            sock.close()
            raise DaemonUnavailable(
                "x10ctld is not running (systemctl status x10ctld)", "DaemonUnavailable") from error
        except PermissionError as error:
            sock.close()
            raise DaemonUnavailable(
                "permission denied: add your user to the x10ctl group and log in again",
                "PermissionError") from error
        except OSError as error:
            sock.close()
            raise DaemonUnavailable(f"cannot reach x10ctld: {error}", "DaemonUnavailable") from error
        self._socket = sock
        self._reader = sock.makefile("rb")

    def call(self, method: str, **params: Any) -> Any:
        with self._lock:
            for attempt in range(2):
                if self._socket is None:
                    self._connect()
                request_id = next(self._ids)
                payload = json.dumps({"id": request_id, "method": method, "params": params}).encode() + b"\n"
                try:
                    assert self._socket is not None and self._reader is not None
                    self._socket.sendall(payload)
                    line = self._reader.readline()
                except OSError as error:
                    self._disconnect()
                    if attempt == 0:
                        continue
                    raise DaemonUnavailable(f"connection to x10ctld lost: {error}") from error
                if not line:
                    self._disconnect()
                    if attempt == 0:
                        continue
                    raise DaemonUnavailable("x10ctld closed the connection")
                response = json.loads(line)
                if "error" in response:
                    error = response["error"] or {}
                    if error.get("type") == "PermissionError":
                        self._disconnect()
                    raise BackendError(error.get("message", "unknown error"), error.get("type", "Error"))
                return response.get("result")
        raise DaemonUnavailable("x10ctld is unavailable")  # pragma: no cover

    def _disconnect(self) -> None:
        sock, self._socket = self._socket, None
        reader, self._reader = self._reader, None
        for item in (reader, sock):
            if item is not None:
                try:
                    item.close()
                except OSError:
                    pass

    def close(self) -> None:
        with self._lock:
            self._disconnect()


class DemoBackend:
    """Simulates the daemon so the full UI can be explored without hardware."""

    demo = True

    def __init__(self) -> None:
        self._started = time.monotonic()
        self._lock = threading.Lock()
        self.profile = "office"
        self.full_speed = False
        self.lighting: dict[str, Any] = {"brightness": 100, "zones": {}}
        self.restore = {"lighting": True, "cpu": False, "gpu": False, "undervolt": False, "audio": False}
        self.cpu = {
            "pstate": {"status": "active", "no_turbo": False, "min_perf_pct": 9, "max_perf_pct": 100,
                       "dynamic_boost": False},
            "governor": "powersave", "governors": ["performance", "powersave"],
            "epp": "balance_performance",
            "epps": ["default", "performance", "balance_performance", "balance_power", "power"],
            "platform_profile": None, "platform_profiles": [],
            "clusters": [
                {"name": "P-cores", "cpus": 12, "hw_min_khz": 400000, "hw_max_khz": 4700000,
                 "min_khz": 400000, "max_khz": 4700000, "cur_khz": 0},
                {"name": "E-cores", "cpus": 8, "hw_min_khz": 400000, "hw_max_khz": 3500000,
                 "min_khz": 400000, "max_khz": 3500000, "cur_khz": 0},
            ],
            "rapl": {"zones": ["intel-rapl:0", "intel-rapl-mmio:0"], "enabled": True,
                     "pl1": {"watts": 45.0, "max_watts": 140, "window_s": 28.0},
                     "pl2": {"watts": 115.0, "max_watts": 140, "window_s": 0.002}},
            "undervolt": {"available": True, "min_mv": -150, "max_mv": 0,
                          "planes": {"core": 0, "cache": 0, "gpu": 0, "uncore": 0, "analogio": 0},
                          "labels": {"core": "CPU core", "cache": "CPU cache / ring", "gpu": "Integrated GPU",
                                     "uncore": "System agent", "analogio": "Analog I/O"}},
        }
        self.gpus = [
            {"id": "card0", "name": "Intel Iris Xe (iGPU)", "pci": "0000:00:02.0", "device_id": "0x46a6",
             "driver": "i915", "discrete": False, "runtime_status": "active",
             "freq": {"min": 100, "max": 1400, "boost": 1400, "rp0": 1400, "rpn": 100, "cur": 300, "act": 300},
             "power": None},
            {"id": "card1", "name": "Arc A730M", "pci": "0000:03:00.0", "device_id": "0x5691",
             "driver": "i915", "discrete": True, "runtime_status": "suspended",
             "freq": {"min": 300, "max": 2050, "boost": 2050, "rp0": 2050, "rpn": 300, "cur": 0, "act": 0},
             "power": {"limit_w": 80.0, "rated_w": 120.0}},
        ]
        self.dgpu_on = True
        self.display = {"backlight": {"device": "intel_backlight", "percent": 70},
                        "panel": {"connector": "eDP-1", "overrides": [], "native_mode": "2560x1440@165.00",
                                  "native_hz": 165.0, "max_hz": 167.0, "blocker": None}}
        self.audio = {"available": True, "power_save_s": 1, "power_save_controller": True}

    def close(self) -> None:
        pass

    def call(self, method: str, **params: Any) -> Any:
        with self._lock:
            handler = getattr(self, "_" + method.replace(".", "_"), None)
            if handler is None:
                raise BackendError(f"unknown method {method!r}", "RequestError")
            return copy.deepcopy(handler(params))

    # handlers ---------------------------------------------------------
    def _system_info(self, _p: dict) -> dict:
        return {"version": "demo", "api": 1, "secure_boot": True, "lockdown": "integrity",
                "kernel_module": True, "state_error": None}

    def _ec_status(self, _p: dict) -> dict:
        elapsed = time.monotonic() - self._started
        offset = ["office", "gaming", "turbo"].index(self.profile) * 450
        cpu_t = round(58 + 11 * math.sin(elapsed / 11))
        gpu_t = round(50 + 9 * math.sin(elapsed / 14 + 0.8))
        cpu_rpm, gpu_rpm = (5900, 5600) if self.full_speed else (
            max(1250, round(1150 + cpu_t * 28 + offset)), max(1100, round(1000 + gpu_t * 26 + offset)))
        return {"profile": self.profile, "full_speed": self.full_speed, "cpu_fan_rpm": cpu_rpm,
                "gpu_fan_rpm": gpu_rpm, "cpu_temperature_c": cpu_t, "gpu_temperature_c": gpu_t,
                "turbo_available": True}

    def _ec_set_profile(self, p: dict) -> dict:
        if p["profile"] not in {"office", "gaming", "turbo"}:
            raise BackendError("unknown profile", "RequestError")
        self.profile = p["profile"]
        return self._ec_status(p)

    def _ec_set_full_speed(self, p: dict) -> dict:
        self.full_speed = bool(p["enabled"])
        return self._ec_status(p)

    def _lighting_get(self, _p: dict) -> dict:
        return self.lighting

    def _lighting_apply(self, p: dict) -> dict:
        self.lighting = {"brightness": p.get("brightness", 100),
                         "zones": {**self.lighting["zones"], **p["zones"]}}
        return self.lighting

    def _cpu_get(self, _p: dict) -> dict:
        return self.cpu

    def _cpu_set(self, p: dict) -> dict:
        p = {k: v for k, v in p.items() if k != "persist"}
        for key in ("no_turbo", "min_perf_pct", "max_perf_pct", "dynamic_boost"):
            if key in p:
                self.cpu["pstate"][key] = p[key]
        for key in ("governor", "epp"):
            if key in p:
                self.cpu[key] = p[key]
        for request in p.get("clusters", []):
            for cluster in self.cpu["clusters"]:
                if cluster["name"] == request["name"]:
                    cluster["min_khz"] = request.get("min_khz", cluster["min_khz"])
                    cluster["max_khz"] = request.get("max_khz", cluster["max_khz"])
        for key, value in (p.get("rapl") or {}).items():
            self.cpu["rapl"][key].update(value)
        for plane, mv in (p.get("undervolt") or {}).items():
            if not -150 <= mv <= 0:
                raise BackendError(f"{plane} offset must be between -150 and 0", "CpuError")
            self.cpu["undervolt"]["planes"][plane] = mv
        return self.cpu

    def _gpu_get(self, _p: dict) -> dict:
        gpus = [g for g in self.gpus if self.dgpu_on or not g["discrete"]]
        return {"gpus": gpus, "dgpu": {"present": self.dgpu_on, "state": "suspended" if self.dgpu_on else "off",
                                       "driver": "i915" if self.dgpu_on else None,
                                       "runtime_status": "suspended" if self.dgpu_on else None,
                                       "arc_dgpu_ctl": "/usr/local/bin/arc-dgpu-ctl"}}

    def _gpu_set(self, p: dict) -> dict:
        gpu = next(g for g in self.gpus if g["id"] == p["id"])
        mapping = {"min_mhz": "min", "max_mhz": "max", "boost_mhz": "boost"}
        for key, field in mapping.items():
            if key in p:
                gpu["freq"][field] = p[key]
        if "power_limit_w" in p and gpu["power"]:
            gpu["power"]["limit_w"] = p["power_limit_w"]
        return self._gpu_get(p)

    def _gpu_dgpu_power(self, p: dict) -> dict:
        self.dgpu_on = bool(p["on"])
        return self._gpu_get(p)

    def _display_get(self, _p: dict) -> dict:
        return self.display

    def _display_brightness(self, p: dict) -> dict:
        self.display["backlight"]["percent"] = p["percent"]
        return self.display

    def _display_overclock(self, p: dict) -> dict:
        name = f"x10-eDP-1-{round(p['target_hz'])}hz.bin"
        self.display["panel"]["overrides"] = [name]
        return {**self.display, "installed": name, "achieved_hz": float(p["target_hz"]), "native_hz": 165.0,
                "kernel_parameter": f"drm.edid_firmware=eDP-1:edid/{name}"}

    def _display_overclock_remove(self, _p: dict) -> dict:
        self.display["panel"]["overrides"] = []
        return self.display

    def _audio_get(self, _p: dict) -> dict:
        return self.audio

    def _audio_set(self, p: dict) -> dict:
        self.audio.update({k: v for k, v in p.items() if k != "persist"})
        return self.audio

    def _restore_get(self, _p: dict) -> dict:
        return self.restore

    def _restore_set(self, p: dict) -> dict:
        self.restore.update(p)
        return self.restore
