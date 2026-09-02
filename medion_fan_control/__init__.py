"""Medion Major X10 firmware profile and fan controls."""

from .protocol import EcProtocol, FanStatus, Profile, ProtocolError

__all__ = ["EcProtocol", "FanStatus", "Profile", "ProtocolError"]