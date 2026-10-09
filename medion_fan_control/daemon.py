"""x10ctld - the privileged half of Erazer Control.

Runs as root under systemd, owns every hardware interface (EC through the
signed x10_ec module, lighting HID, sysfs knobs, arc-dgpu-ctl) and serves a small
JSON-lines API on a Unix socket. The socket is ``root:x10ctl 0660`` and each
connection's peer credentials are checked, so desktop users in ``x10ctl``
use the GUI without sudo while nobody gets raw hardware access.
"""
from __future__ import annotations

import argparse
import configparser
import dataclasses
import grp
import json
import logging
import os
import signal
import socket
import socketserver
import struct
import sys
import threading
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .audio import AudioError, AudioPowerBackend
from .controller import ProtocolSession
from .cpu import CpuBackend, CpuError
from .display import DisplayBackend, DisplayError
from .gpu import GpuBackend, GpuError, dgpu_config_from_mapping
from .hardware import (
    KERNEL_EC_DEVICE,
    HardwareAccessError,
    UnsupportedHardwareError,
    open_supported_protocol,
    read_platform_security,
)
from .lighting import (
    LightingAccessError,
    apply_static_lighting,
    lighting_from_document,
    lighting_to_document,
    resolve_lighting,
)
from .protocol import FanStatus, Profile, ProtocolError
from .settings import DEFAULT_STATE_PATH, SettingsError, StateStore
from .sysfs import SysfsError

logger = logging.getLogger("x10ctld")

SOCKET_PATH = Path("/run/x10ctld/x10ctld.sock")
CONFIG_PATH = Path("/etc/x10ctld.conf")
UNDERVOLT_SENTINEL = "undervolt.pending"
MAX_REQUEST_BYTES = 64 * 1024
API_VERSION = 1

EXPECTED_ERRORS = (
    AudioError, CpuError, DisplayError, GpuError, HardwareAccessError, UnsupportedHardwareError,
    LightingAccessError, ProtocolError, SettingsError, SysfsError, ValueError, KeyError, TypeError, OSError,
)


class RequestError(ValueError):
    pass


def status_to_dict(status: FanStatus) -> dict[str, Any]:
    data = dataclasses.asdict(status)
    data["profile"] = Profile(status.profile).label
    return data


class EcService:
    def __init__(self, factory: Callable = open_supported_protocol) -> None:
        self.session = ProtocolSession(factory)
        self._open = False

    def _ensure(self) -> None:
        if not self._open:
            self.session.open()
            self._open = True

    def _call(self, operation: Callable[[], FanStatus]) -> dict[str, Any]:
        self._ensure()
        try:
            return status_to_dict(operation())
        except Exception:
            # ProtocolSession closes itself on transport errors; reopen next time
            self._open = self.session._controller is not None
            raise

    def status(self) -> dict[str, Any]:
        return self._call(self.session.read_status)

    def set_profile(self, name: str) -> dict[str, Any]:
        try:
            profile = Profile[str(name).upper()]
        except KeyError as error:
            raise RequestError(f"unknown profile {name!r}") from error
        return self._call(lambda: self.session.set_profile(profile))

    def set_full_speed(self, enabled: bool) -> dict[str, Any]:
        return self._call(lambda: self.session.set_full_speed(bool(enabled)))

    def close(self) -> None:
        self.session.close()
        self._open = False


class Daemon:
    def __init__(
        self,
        *,
        config: configparser.ConfigParser | None = None,
        state: StateStore | None = None,
        ec: EcService | None = None,
        cpu: CpuBackend | None = None,
        gpu: GpuBackend | None = None,
        display: DisplayBackend | None = None,
        audio: AudioPowerBackend | None = None,
        apply_lighting: Callable = apply_static_lighting,
        state_dir: Path = DEFAULT_STATE_PATH.parent,
    ) -> None:
        self.config = config or configparser.ConfigParser()
        self.state = state or StateStore(state_dir / "state.json")
        self.ec = ec or EcService()
        self.cpu = cpu or CpuBackend()
        self.gpu = gpu or GpuBackend(arc_dgpu_ctl=dgpu_config_from_mapping(
            self.config["dgpu"] if self.config.has_section("dgpu") else None))
        self.display = display or DisplayBackend()
        self.audio = audio or AudioPowerBackend()
        self._apply_lighting = apply_lighting
        self.state_dir = state_dir
        self._locks = {name: threading.Lock() for name in ("ec", "lighting", "system")}
        self._methods: dict[str, tuple[str, Callable[[dict[str, Any]], Any]]] = {
            "system.info": ("system", lambda p: self.system_info()),
            "ec.status": ("ec", lambda p: self.ec.status()),
            "ec.set_profile": ("ec", lambda p: self.ec.set_profile(p["profile"])),
            "ec.set_full_speed": ("ec", lambda p: self.ec.set_full_speed(_bool(p, "enabled"))),
            "lighting.get": ("lighting", lambda p: self.lighting_get()),
            "lighting.apply": ("lighting", self.lighting_apply),
            "cpu.get": ("system", lambda p: self.cpu.get()),
            "cpu.set": ("system", self.cpu_set),
            "gpu.get": ("system", lambda p: self.gpu.get()),
            "gpu.set": ("system", self.gpu_set),
            "gpu.dgpu_power": ("system", lambda p: self.gpu.set_dgpu_power(_bool(p, "on"))),
            "display.get": ("system", lambda p: self.display.get()),
            "display.brightness": ("system", lambda p: self.display.set_brightness(p["percent"])),
            "display.overclock": ("system", lambda p: self.display.install_overclock(p["target_hz"])),
            "display.overclock_remove": ("system", lambda p: self.display.remove_overclock()),
            "audio.get": ("system", lambda p: self.audio.get()),
            "audio.set": ("system", self.audio_set),
            "restore.get": ("system", lambda p: self.restore_flags()),
            "restore.set": ("system", self.restore_set),
        }

    # ------------------------------------------------------------ dispatch
    def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        entry = self._methods.get(method)
        if entry is None:
            raise RequestError(f"unknown method {method!r}")
        if not isinstance(params, dict):
            raise RequestError("params must be an object")
        lock_name, handler = entry
        with self._locks[lock_name]:
            return handler(params)

    def handle_request(self, line: bytes) -> dict[str, Any]:
        request_id = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise RequestError("request must be a JSON object")
            request_id = request.get("id")
            result = self.dispatch(str(request.get("method")), request.get("params") or {})
            return {"id": request_id, "result": result}
        except json.JSONDecodeError as error:
            return {"id": request_id, "error": {"type": "RequestError", "message": f"invalid JSON: {error}"}}
        except KeyError as error:
            return {"id": request_id, "error": {"type": "RequestError", "message": f"missing parameter {error}"}}
        except EXPECTED_ERRORS as error:
            return {"id": request_id, "error": {"type": type(error).__name__, "message": str(error)}}
        except Exception as error:  # pragma: no cover - defensive
            logger.exception("unexpected failure")
            return {"id": request_id, "error": {"type": "InternalError", "message": str(error)}}

    # ------------------------------------------------------------ handlers
    def system_info(self) -> dict[str, Any]:
        try:
            security = read_platform_security()
            secure_boot, lockdown = security.secure_boot_enabled, security.lockdown_mode
        except HardwareAccessError:
            secure_boot = lockdown = None
        return {
            "version": __version__,
            "api": API_VERSION,
            "secure_boot": secure_boot,
            "lockdown": lockdown,
            "kernel_module": KERNEL_EC_DEVICE.exists(),
            "state_error": self.state.load_error,
        }

    def lighting_get(self) -> dict[str, Any]:
        saved = self.state.get("lighting")
        return saved if isinstance(saved, dict) else {"brightness": 100, "zones": {}}

    def lighting_apply(self, params: dict[str, Any]) -> dict[str, Any]:
        settings, brightness = lighting_from_document(params)
        if not settings:
            raise RequestError("select at least one lighting zone")
        self._apply_lighting(resolve_lighting(settings, brightness))
        if params.get("save", True):
            saved = self.lighting_get()
            merged_zones = {**saved.get("zones", {}), **lighting_to_document(settings, brightness)["zones"]}
            self.state.update("lighting", {"brightness": brightness, "zones": merged_zones})
        return self.lighting_get()

    def cpu_set(self, params: dict[str, Any]) -> dict[str, Any]:
        params = dict(params)
        persist = params.pop("persist", True)
        result = self.cpu.set(params)
        if persist:
            undervolt = params.pop("undervolt", None)
            if params:
                self.state.merge("cpu", params)
            if undervolt:
                self.state.merge("undervolt", undervolt)
        return result

    def gpu_set(self, params: dict[str, Any]) -> dict[str, Any]:
        params = dict(params)
        persist = params.pop("persist", True)
        result = self.gpu.set(params)
        if persist:
            card = next((g for g in result["gpus"] if g["id"] == params["id"]), None)
            if card and card.get("pci"):
                stored = {k: v for k, v in params.items() if k != "id"}
                self.state.merge("gpu", {card["pci"]: stored})
        return result

    def audio_set(self, params: dict[str, Any]) -> dict[str, Any]:
        params = dict(params)
        persist = params.pop("persist", True)
        result = self.audio.set(params)
        if persist:
            self.state.merge("audio", params)
        return result

    def restore_flags(self) -> dict[str, bool]:
        defaults = self.config["restore"] if self.config.has_section("restore") else {}
        flags = {
            key: str(defaults.get(key, "true" if key == "lighting" else "false")).lower() == "true"
            for key in ("lighting", "cpu", "gpu", "undervolt", "audio")
        }
        saved = self.state.get("restore")
        if isinstance(saved, dict):
            flags.update({k: bool(v) for k, v in saved.items() if k in flags})
        return flags

    def restore_set(self, params: dict[str, Any]) -> dict[str, bool]:
        flags = self.restore_flags()
        for key, value in params.items():
            if key not in flags or not isinstance(value, bool):
                raise RequestError(f"invalid restore flag {key!r}")
            flags[key] = value
        self.state.update("restore", flags)
        return flags

    # ------------------------------------------------------------- restore
    def restore_on_start(self, *, timer_factory: Callable = threading.Timer) -> list[str]:
        flags = self.restore_flags()
        report: list[str] = []

        def attempt(name: str, action: Callable[[], Any]) -> None:
            try:
                action()
                report.append(f"{name}: restored")
            except EXPECTED_ERRORS as error:
                report.append(f"{name}: {error}")

        lighting = self.state.get("lighting")
        if flags["lighting"] and isinstance(lighting, dict) and lighting.get("zones"):
            attempt("lighting", lambda: self.lighting_apply({**lighting, "save": False}))
        cpu = self.state.get("cpu")
        if flags["cpu"] and isinstance(cpu, dict) and cpu:
            attempt("cpu", lambda: self.cpu.set(cpu))
        gpu = self.state.get("gpu")
        if flags["gpu"] and isinstance(gpu, dict):
            cards = {g["pci"]: g["id"] for g in self.gpu.get()["gpus"] if g.get("pci")}
            for pci, params in gpu.items():
                if pci in cards:
                    attempt(f"gpu {pci}", lambda p=params, c=cards[pci]: self.gpu.set({**p, "id": c}))
        audio = self.state.get("audio")
        if flags["audio"] and isinstance(audio, dict) and audio:
            attempt("audio", lambda: self.audio.set(audio))
        undervolt = self.state.get("undervolt")
        if flags["undervolt"] and isinstance(undervolt, dict) and undervolt:
            sentinel = self.state_dir / UNDERVOLT_SENTINEL
            if sentinel.exists():
                report.append(
                    "undervolt: skipped - the previous boot did not stay up through the stability window"
                )
                sentinel.unlink(missing_ok=True)
            else:
                sentinel.parent.mkdir(parents=True, exist_ok=True)
                sentinel.write_text("pending\n", encoding="ascii")
                attempt("undervolt", lambda: self.cpu.set({"undervolt": undervolt}))
                seconds = float(self.config.get("restore", "undervolt_stability_s", fallback="120"))
                timer = timer_factory(seconds, lambda: sentinel.unlink(missing_ok=True))
                timer.daemon = True
                timer.start()
        for line in report:
            logger.info("restore %s", line)
        return report

    def close(self) -> None:
        with self._locks["ec"]:
            self.ec.close()


# ---------------------------------------------------------------- transport
def peer_credentials(connection: socket.socket) -> tuple[int, int, int]:
    data = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", data)


def process_groups(pid: int) -> set[int]:
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="ascii").splitlines():
            if line.startswith("Groups:"):
                return {int(value) for value in line.split()[1:]}
    except (OSError, ValueError):
        pass
    return set()


def is_authorized(pid: int, uid: int, gid: int, group_gid: int | None) -> bool:
    if uid == 0:
        return True
    if group_gid is None:
        return False
    return gid == group_gid or group_gid in process_groups(pid)


class _Handler(socketserver.StreamRequestHandler):
    server: "DaemonServer"

    def handle(self) -> None:
        try:
            pid, uid, gid = peer_credentials(self.connection)
        except OSError:
            return
        if not is_authorized(pid, uid, gid, self.server.group_gid):
            self._send({"id": None, "error": {"type": "PermissionError",
                                              "message": "not a member of the x10ctl group"}})
            return
        while True:
            line = self.rfile.readline(MAX_REQUEST_BYTES + 1)
            if not line:
                return
            if len(line) > MAX_REQUEST_BYTES:
                self._send({"id": None, "error": {"type": "RequestError", "message": "request too large"}})
                return
            self._send(self.server.daemon_core.handle_request(line))

    def _send(self, message: dict[str, Any]) -> None:
        self.wfile.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
        self.wfile.flush()


class DaemonServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, path: Path, core: Daemon, group: str | None) -> None:
        self.daemon_core = core
        try:
            self.group_gid = grp.getgrnam(group).gr_gid if group else None
        except KeyError:
            logger.warning("group %r does not exist; only root may connect", group)
            self.group_gid = None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        old_umask = os.umask(0o117)
        try:
            super().__init__(str(path), _Handler)
        finally:
            os.umask(old_umask)
        if self.group_gid is not None and os.geteuid() == 0:
            os.chown(path, 0, self.group_gid)
        os.chmod(path, 0o660)


def load_config(path: Path = CONFIG_PATH) -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    config.read(path, encoding="utf-8")
    return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="x10ctld", description=__doc__.splitlines()[0])
    parser.add_argument("--socket", type=Path, default=SOCKET_PATH)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--no-restore", action="store_true", help="do not re-apply saved settings")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if os.geteuid() != 0:
        print("x10ctld must run as root (it is started by systemd)", file=sys.stderr)
        return 1
    config = load_config(args.config)
    core = Daemon(config=config)
    if not args.no_restore:
        core.restore_on_start()
    group = config.get("socket", "group", fallback="x10ctl")
    server = DaemonServer(args.socket, core, group)
    logger.info("x10ctld %s listening on %s (group %s)", __version__, args.socket, group)

    def stop(_signum: int, _frame: object) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        args.socket.unlink(missing_ok=True)
        core.close()
    return 0


def _bool(params: dict[str, Any], key: str) -> bool:
    value = params[key]
    if not isinstance(value, bool):
        raise RequestError(f"{key} must be true or false")
    return value


__all__ = ["Daemon", "DaemonServer", "EcService", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
