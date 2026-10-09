"""Bundled assets: navigation icons, the application logo and the brand font."""
from __future__ import annotations

from importlib import resources

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QFontDatabase, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from .theme import ACCENT_HI, BRAND_FONT, INK, MUTED

# 24x24 line glyphs; drawn with a single stroke colour.
GLYPHS = {
    "dashboard": '<path d="M4 16a8 8 0 0 1 16 0"/><path d="M12 16l4.5-5"/><circle cx="12" cy="16" r="1.5"/>'
                 '<path d="M6 20h12"/>',
    "cpu": '<rect x="6" y="6" width="12" height="12" rx="1.5"/><rect x="9.5" y="9.5" width="5" height="5"/>'
           '<path d="M9 2.5v3.5M15 2.5v3.5M9 18v3.5M15 18v3.5M2.5 9h3.5M2.5 15h3.5M18 9h3.5M18 15h3.5"/>',
    "gpu": '<rect x="2" y="6" width="20" height="12" rx="2"/><circle cx="9" cy="12" r="3.2"/>'
           '<path d="M15 10h4M15 14h4M5 18v2.5M10 18v2.5"/>',
    "display": '<rect x="2.5" y="4" width="19" height="12.5" rx="1.5"/><path d="M12 16.5V20M8 20.5h8"/>',
    "audio": '<path d="M4 9.5h3.5L12 6v12l-4.5-3.5H4z"/><path d="M15.5 9.5a3.5 3.5 0 0 1 0 5"/>'
             '<path d="M18 7a7 7 0 0 1 0 10"/>',
    "led": '<circle cx="12" cy="12" r="3.8"/><path d="M12 2.5v2.5M12 19v2.5M2.5 12H5M19 12h2.5'
           'M5.3 5.3l1.8 1.8M16.9 16.9l1.8 1.8M5.3 18.7l1.8-1.8M16.9 7.1l1.8-1.8"/>',
    "reload": '<path d="M20 12a8 8 0 1 1-2.35-5.65"/><path d="M20 4v5h-5"/>',
}

_ICON_SIZES = (16, 20, 24, 32, 48)


def _render(svg: str, size: int) -> QPixmap:
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    return pixmap


def _glyph_svg(name: str, color: str) -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
        f'stroke="{color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
        f"{GLYPHS[name]}</svg>"
    )


def icon(name: str, color: str = MUTED, checked_color: str = ACCENT_HI) -> QIcon:
    """Icon for `name`; checkable buttons switch to `checked_color` when on."""
    result = QIcon()
    for size in _ICON_SIZES:
        result.addPixmap(_render(_glyph_svg(name, color), size), QIcon.Mode.Normal, QIcon.State.Off)
        result.addPixmap(_render(_glyph_svg(name, INK), size), QIcon.Mode.Active, QIcon.State.Off)
        result.addPixmap(_render(_glyph_svg(name, checked_color), size), QIcon.Mode.Normal, QIcon.State.On)
        result.addPixmap(_render(_glyph_svg(name, checked_color), size), QIcon.Mode.Active, QIcon.State.On)
    return result


def logo_svg() -> str:
    return resources.files(__package__).joinpath("assets").joinpath("x10-control.svg").read_text(encoding="utf-8")


def logo_pixmap(size: int, device_pixel_ratio: float = 1.0) -> QPixmap:
    pixmap = _render(logo_svg(), round(size * device_pixel_ratio))
    pixmap.setDevicePixelRatio(device_pixel_ratio)
    return pixmap


def app_icon() -> QIcon:
    result = QIcon()
    svg = logo_svg()
    for size in (16, 24, 32, 48, 64, 128, 256):
        result.addPixmap(_render(svg, size))
    return result


def load_brand_font() -> bool:
    """Register the bundled brand font once; returns False if Qt rejected it."""
    if BRAND_FONT in QFontDatabase.families():
        return True
    data = resources.files(__package__).joinpath("assets").joinpath("fonts").joinpath("Outfit-Variable.ttf")
    return QFontDatabase.addApplicationFontFromData(QByteArray(data.read_bytes())) != -1
