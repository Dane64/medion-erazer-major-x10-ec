"""Erazer black-and-blue presentation."""
from .lighting import RgbColor

BACKGROUND = "#06080c"
SIDEBAR = "#0a0d13"
SURFACE = "#0f131b"
SURFACE_HI = "#151b26"
BORDER = "#1d2532"
GRID = "#1a2130"
INK = "#e8eef7"
MUTED = "#7f8ca3"
ACCENT = "#0a8fff"
ACCENT_HI = "#3fb0ff"
ACCENT_DIM = "#08355f"
CPU_COLOR = "#00c8ff"
GPU_COLOR = "#5b6dff"
WARNING = "#ffb020"
ERROR = "#ff4d5e"
SUCCESS = "#22d3a6"
# Outfit is the typeface on medion.com; bundled under the SIL OFL (assets/fonts/OFL.txt).
BRAND_FONT = "Outfit"
MONO_FONT = "DejaVu Sans Mono"

APP_STYLESHEET = f"""
    QMainWindow, QWidget#root, QWidget#page, QStackedWidget {{ background: {BACKGROUND}; color: {INK}; }}
    QWidget {{ color: {INK}; font-family: "{BRAND_FONT}"; font-size: 13px; }}
    QToolTip {{ background: {SURFACE_HI}; color: {INK}; border: 1px solid {ACCENT}; padding: 4px; }}
    QLabel {{ color: {INK}; background: transparent; }}
    QLabel#brand {{
        color: {INK}; font-size: 22px; font-weight: 900; font-style: italic; letter-spacing: 3px;
    }}
    QLabel#brandSub {{ color: {ACCENT_HI}; font-size: 9px; font-weight: 800; letter-spacing: 2px; }}
    QLabel#title {{ font-size: 22px; font-weight: 800; letter-spacing: 1px; }}
    QLabel#pageIcon {{ background: {ACCENT_DIM}; border: 1px solid {ACCENT}; border-radius: 10px; }}
    QLabel#subtitle, QLabel#status, QLabel[role="muted"] {{ color: {MUTED}; }}
    QLabel#sectionTitle {{
        font-size: 12px; font-weight: 800; letter-spacing: 2px; color: {ACCENT_HI};
    }}
    QLabel#metricName {{ color: {MUTED}; font-size: 10px; font-weight: 800; letter-spacing: 2px; }}
    QLabel#metricValue {{ color: {INK}; font: 700 26px "{MONO_FONT}"; }}
    QLabel#badge {{
        background: {ACCENT_DIM}; color: {ACCENT_HI}; border: 1px solid {ACCENT};
        border-radius: 4px; padding: 6px 12px; font-weight: 800; letter-spacing: 2px;
    }}
    QLabel[role="warning"] {{ color: {WARNING}; }}
    QLabel[role="error"] {{ color: {ERROR}; }}
    QLabel[role="ok"] {{ color: {SUCCESS}; }}

    QWidget#sidebar {{ background: {SIDEBAR}; border-right: 1px solid {BORDER}; }}
    QPushButton[nav="true"] {{
        background: transparent; color: {MUTED}; border: 0; border-left: 3px solid transparent;
        border-radius: 0; padding: 12px 18px; text-align: left; font-weight: 800; letter-spacing: 2px;
    }}
    QPushButton[nav="true"]:hover {{ color: {INK}; background: {SURFACE}; }}
    QPushButton[nav="true"]:focus {{ border-color: transparent; }}
    QPushButton[nav="true"]:checked {{
        color: {INK}; background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {ACCENT_DIM}, stop:1 {SIDEBAR});
        border-left: 3px solid {ACCENT};
    }}

    QFrame[panel="true"] {{
        background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 6px;
    }}
    QFrame[panel="true"][accent="true"] {{ border-top: 2px solid {ACCENT}; }}

    QPushButton, QRadioButton {{
        border: 1px solid {BORDER}; border-radius: 4px; padding: 8px 14px;
        background: {SURFACE_HI}; color: {INK}; font-weight: 700;
    }}
    QPushButton:hover, QRadioButton:hover {{ border-color: {ACCENT}; }}
    QPushButton:pressed {{ background: {ACCENT_DIM}; }}
    QPushButton:focus, QRadioButton:focus, QCheckBox:focus {{ outline: none; border-color: {ACCENT_HI}; }}
    QRadioButton::indicator {{ width: 0; height: 0; }}
    QRadioButton:checked {{ background: {ACCENT}; color: white; border-color: {ACCENT_HI}; }}
    QPushButton[role="primary"] {{ background: {ACCENT}; color: white; border-color: {ACCENT_HI}; }}
    QPushButton[role="primary"]:hover {{ background: {ACCENT_HI}; }}
    QPushButton[role="danger"] {{ border-color: {ERROR}; color: {ERROR}; }}
    QPushButton:disabled, QRadioButton:disabled {{ color: #46516a; background: {SURFACE}; border-color: {BORDER}; }}

    QCheckBox {{ spacing: 8px; font-weight: 600; }}
    QCheckBox::indicator {{
        width: 34px; height: 18px; border-radius: 9px; background: {BORDER}; border: 1px solid {BORDER};
    }}
    QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT_HI}; }}
    QCheckBox::indicator:disabled {{ background: {SURFACE_HI}; }}

    QComboBox, QSpinBox, QDoubleSpinBox {{
        background: {SURFACE_HI}; border: 1px solid {BORDER}; border-radius: 4px; padding: 5px 8px;
        color: {INK}; min-height: 20px;
    }}
    QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover {{ border-color: {ACCENT}; }}
    QComboBox QAbstractItemView {{
        background: {SURFACE_HI}; color: {INK}; selection-background-color: {ACCENT}; border: 1px solid {ACCENT};
    }}

    QSlider::groove:horizontal {{ height: 4px; background: {BORDER}; border-radius: 2px; }}
    QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
    QSlider::handle:horizontal {{
        background: {INK}; border: 2px solid {ACCENT}; width: 14px; margin: -7px 0; border-radius: 9px;
    }}
    QSlider::handle:horizontal:hover {{ background: {ACCENT_HI}; }}
    QSlider::groove:horizontal:disabled {{ background: {SURFACE_HI}; }}
    QSlider::sub-page:horizontal:disabled {{ background: #26324a; }}

    QScrollArea {{ border: 0; background: transparent; }}
    QScrollBar:vertical {{ background: {BACKGROUND}; width: 10px; }}
    QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 5px; min-height: 30px; }}
    QScrollBar::handle:vertical:hover {{ background: {ACCENT_DIM}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
"""


def lighting_text_color(color: RgbColor) -> str:
    luminance = (299 * color.red + 587 * color.green + 114 * color.blue) / 1000
    return "#06080c" if luminance >= 145 else "#ffffff"
