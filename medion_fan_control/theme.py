from .lighting import RgbColor


BACKGROUND = "#edf1ed"
SURFACE = "#ffffff"
INK = "#17231e"
MUTED = "#64736c"
BORDER = "#d5ded8"
GRID = "#e1e8e3"
SIDEBAR = "#173d34"
ACCENT = "#087d5a"
CPU_COLOR = "#df5a3f"
GPU_COLOR = "#2475a8"
WARNING = "#e6ad3a"
ERROR = "#bd3f35"

APP_STYLESHEET = f"""
    QMainWindow, QWidget#root, QWidget#page {{ background: {BACKGROUND}; color: {INK}; }}
    QLabel {{ color: {INK}; }}
    QLabel#brand {{ color: white; font-size: 22px; font-weight: 800; }}
    QLabel#subtitle, QLabel#status, QLabel[role="muted"] {{ color: {MUTED}; }}
    QLabel#title {{ font-size: 25px; font-weight: 800; }}
    QLabel#sectionTitle {{ font-size: 16px; font-weight: 700; }}
    QLabel#metricName {{ color: {MUTED}; font-size: 10px; font-weight: 700; }}
    QLabel#metricValue {{ color: {INK}; font: 700 23px "DejaVu Sans Mono"; }}
    QFrame[panel="true"] {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 12px; }}
    QPushButton, QRadioButton {{
        border: 1px solid {BORDER}; border-radius: 8px; padding: 9px 14px;
        background: {SURFACE}; color: {INK}; font-weight: 600;
    }}
    QPushButton:hover, QRadioButton:hover {{ border-color: {ACCENT}; background: #f3faf7; }}
    QPushButton:focus, QRadioButton:focus, QCheckBox:focus {{
        outline: 2px solid {ACCENT}; outline-offset: 2px;
    }}
    QRadioButton::indicator {{ width: 0; height: 0; }}
    QRadioButton:checked {{ background: {SIDEBAR}; color: white; border-color: {SIDEBAR}; }}
    QPushButton[role="primary"] {{ background: {ACCENT}; color: white; border-color: {ACCENT}; }}
    QPushButton[role="primary"]:hover {{ background: #066447; }}
    QPushButton:disabled, QRadioButton:disabled {{ color: #718078; background: #edf0ee; }}
    QCheckBox {{ spacing: 8px; font-weight: 600; }}
    QTabWidget::pane {{ border: 0; background: transparent; }}
    QTabBar::tab {{
        background: transparent; color: {MUTED}; padding: 12px 22px;
        border-bottom: 3px solid transparent; font-weight: 700;
    }}
    QTabBar::tab:hover {{ color: {ACCENT}; background: #e5eee9; }}
    QTabBar::tab:selected {{ color: {INK}; border-bottom-color: {ACCENT}; }}
    QSlider::groove:horizontal {{ height: 5px; background: {GRID}; border-radius: 2px; }}
    QSlider::handle:horizontal {{
        background: {ACCENT}; width: 16px; margin: -6px 0; border-radius: 8px;
    }}
    QSlider::handle:horizontal:focus {{ border: 2px solid {SIDEBAR}; }}
    QScrollArea {{ border: 0; background: transparent; }}
"""


def lighting_text_color(color: RgbColor) -> str:
    luminance = (299 * color.red + 587 * color.green + 114 * color.blue) / 1000
    return INK if luminance >= 145 else "#ffffff"
