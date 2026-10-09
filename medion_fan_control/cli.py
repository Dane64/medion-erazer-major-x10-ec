from __future__ import annotations

import argparse
import sys

from . import __version__


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="x10-control",
        description="Erazer Control: fans, CPU, GPU, display, audio and lighting for the MEDION Erazer Major X10",
    )
    parser.add_argument(
        "--demo", action="store_true",
        help="explore the full interface with simulated hardware; nothing is read, written or saved",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if sys.platform != "linux":
        print("Erazer Control currently supports Linux only.", file=sys.stderr)
        return 1

    from PySide6.QtWidgets import QApplication

    from .gui import FanControlApp
    from .icons import app_icon

    application = QApplication(sys.argv[:1])
    application.setApplicationName("Erazer Control")
    application.setDesktopFileName("x10-control")
    application.setApplicationVersion(__version__)
    application.setOrganizationName("Medion Fan Control")
    application.setWindowIcon(app_icon())
    window = FanControlApp(demo=args.demo)
    window.show()
    return application.exec()
