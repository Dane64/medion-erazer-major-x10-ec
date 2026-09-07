from __future__ import annotations

import argparse
import sys

from . import __version__


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="medion-fan-control",
        description="Medion Major X10 fan, profile, telemetry, and lighting control",
    )
    parser.add_argument(
        "--demo", action="store_true",
        help="preview simulated telemetry and lighting without hardware access or saved settings",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if sys.platform != "linux":
        print("Medion Major X10 Control currently supports Linux only.", file=sys.stderr)
        return 1

    from PySide6.QtWidgets import QApplication

    from .gui import FanControlApp

    application = QApplication(sys.argv[:1])
    application.setApplicationName("Medion Major X10 Control")
    application.setApplicationVersion(__version__)
    application.setOrganizationName("Medion Fan Control")
    window = FanControlApp(demo=args.demo)
    window.show()
    return application.exec()
