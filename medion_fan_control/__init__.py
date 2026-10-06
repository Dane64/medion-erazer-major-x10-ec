"""Medion Major X10 firmware profile and fan controls."""

from importlib.metadata import PackageNotFoundError, version

from .protocol import EcProtocol, FanStatus, Profile, ProtocolError

try:
    __version__ = version("medion-erazer-major-x10-ec")
except PackageNotFoundError:
    __version__ = "0+unknown"

__all__ = ["EcProtocol", "FanStatus", "Profile", "ProtocolError", "__version__"]