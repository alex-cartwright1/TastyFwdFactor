"""Design tokens, global stylesheet, and small painted UI primitives.

One place owns every colour, radius and spacing value in the app. Widgets never
hard-code a hex value — they reference `T` or carry an object name / dynamic
property that the stylesheet targets. Changing the palette here restyles the
whole application.

The look is a dark "terminal" aesthetic: near-black chrome, one raised surface
for panels, a single blue accent, and soft green/red reserved *exclusively* for
signed financial values so P/L reads at a glance.
"""

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import (
    QColor, QFont, QFontDatabase, QIcon, QPainter, QPainterPath, QPalette,
    QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QApplication, QFrame, QGraphicsDropShadowEffect, QWidget,
)


class T:
    """Design tokens."""

    # Surfaces, darkest to lightest.
    BG          = "#0d1117"     # window background
    SURFACE     = "#151a21"     # panels, tables, cards
    SURFACE_ALT = "#191f27"     # alternating table rows
    RAISED      = "#1f262f"     # hover, inputs, headers
    BORDER      = "#262d38"
    BORDER_SOFT = "#1e242c"

    # Text.
    TEXT        = "#e6edf3"
    TEXT_DIM    = "#9aa5b1"
    TEXT_MUTED  = "#6b7684"

    # Brand accent.
    ACCENT      = "#4c8dff"
    ACCENT_HOVER = "#5f9bff"
    ACCENT_DIM  = "#2d5bb9"
    ACCENT_SOFT = "rgba(76, 141, 255, 0.14)"

    # Financial semantics — used only for signed values.
    POSITIVE    = "#3fd07f"
    NEGATIVE    = "#ff6b6b"
    WARNING     = "#e3b341"

    # Flash overlay for live cell updates.
    FLASH_UP    = QColor(63, 208, 127)
    FLASH_DOWN  = QColor(255, 107, 107)

    RADIUS      = 10
    RADIUS_SM   = 6
    PAD         = 12
    GAP         = 8

    FONT_SIZE   = 10            # points
    MONO        = "monospace"


_FONT_STACK = ("Inter", "Segoe UI", "Roboto", "Noto Sans", "Cantarell",
               "DejaVu Sans", "Helvetica Neue")


def preferred_font_family():
    """First available family from the modern sans-serif stack."""
    available = set(QFontDatabase.families())
    for name in _FONT_STACK:
        if name in available:
            return name
    return QApplication.font().family()


def qcolor(token):
    return QColor(token)


# ─── Global stylesheet ───────────────────────────────────────────────────────

def stylesheet():
    return f"""
    QWidget {{
        background-color: {T.BG};
        color: {T.TEXT};
        selection-background-color: {T.ACCENT_DIM};
        selection-color: #ffffff;
    }}
    /* Labels and toggles inherit QWidget's background otherwise, which paints
       an opaque block of the *window* colour on top of every panel. */
    QLabel, QCheckBox, QRadioButton {{
        background: transparent;
    }}
    QToolTip {{
        background-color: {T.RAISED};
        color: {T.TEXT};
        border: 1px solid {T.BORDER};
        border-radius: {T.RADIUS_SM}px;
        padding: 6px 8px;
    }}

    /* ── Structure ───────────────────────────────────────────────────── */
    #Sidebar {{
        background-color: {T.SURFACE};
        border-right: 1px solid {T.BORDER_SOFT};
    }}
    #Card, #ActionBar {{
        background-color: {T.SURFACE};
        border: 1px solid {T.BORDER_SOFT};
        border-radius: {T.RADIUS}px;
    }}
    #ActionBar {{
        border-radius: {T.RADIUS}px;
    }}
    #Separator {{
        background-color: {T.BORDER_SOFT};
        max-height: 1px;
        border: none;
    }}
    #VSeparator {{
        background-color: {T.BORDER_SOFT};
        max-width: 1px;
        border: none;
    }}

    /* ── Typography ──────────────────────────────────────────────────── */
    QLabel[role="title"] {{
        font-size: 20px;
        font-weight: 600;
        color: {T.TEXT};
    }}
    QLabel[role="subtitle"] {{
        font-size: 12px;
        color: {T.TEXT_MUTED};
    }}
    QLabel[role="section"] {{
        font-size: 11px;
        font-weight: 700;
        color: {T.TEXT_MUTED};
        letter-spacing: 1px;
    }}
    QLabel[role="hint"] {{
        color: {T.TEXT_MUTED};
        font-size: 11px;
    }}
    QLabel[role="metric"] {{
        font-size: 17px;
        font-weight: 600;
    }}
    QLabel[tone="positive"] {{ color: {T.POSITIVE}; }}
    QLabel[tone="negative"] {{ color: {T.NEGATIVE}; }}
    QLabel[tone="accent"]   {{ color: {T.ACCENT}; }}
    QLabel[tone="warning"]  {{ color: {T.WARNING}; }}

    /* ── Buttons ─────────────────────────────────────────────────────── */
    QPushButton {{
        background-color: {T.RAISED};
        color: {T.TEXT};
        border: 1px solid {T.BORDER};
        border-radius: {T.RADIUS_SM}px;
        padding: 7px 14px;
        font-weight: 500;
    }}
    QPushButton:hover  {{ background-color: #263040; border-color: #33404f; }}
    QPushButton:pressed {{ background-color: #1b222b; }}
    QPushButton:disabled {{
        background-color: {T.SURFACE};
        color: {T.TEXT_MUTED};
        border-color: {T.BORDER_SOFT};
    }}
    QPushButton[variant="primary"] {{
        background-color: {T.ACCENT};
        border-color: {T.ACCENT};
        color: #06101f;
        font-weight: 600;
    }}
    QPushButton[variant="primary"]:hover   {{ background-color: {T.ACCENT_HOVER}; }}
    QPushButton[variant="primary"]:pressed {{ background-color: {T.ACCENT_DIM}; }}
    QPushButton[variant="primary"]:disabled {{
        background-color: {T.ACCENT_DIM};
        color: rgba(255,255,255,0.5);
        border-color: {T.ACCENT_DIM};
    }}
    QPushButton[variant="danger"] {{
        color: {T.NEGATIVE};
        border-color: rgba(255,107,107,0.35);
    }}
    QPushButton[variant="danger"]:hover {{
        background-color: rgba(255,107,107,0.12);
    }}
    QPushButton[variant="ghost"] {{
        background-color: transparent;
        border-color: transparent;
        color: {T.TEXT_DIM};
    }}
    QPushButton[variant="ghost"]:hover {{
        background-color: {T.RAISED};
        color: {T.TEXT};
    }}

    /* ── Navigation ──────────────────────────────────────────────────── */
    QPushButton#NavButton {{
        background-color: transparent;
        border: none;
        border-radius: {T.RADIUS_SM}px;
        color: {T.TEXT_DIM};
        padding: 9px 10px;
        text-align: left;
        font-weight: 500;
    }}
    QPushButton#NavButton:hover {{
        background-color: {T.RAISED};
        color: {T.TEXT};
    }}
    QPushButton#NavButton:checked {{
        background-color: {T.ACCENT_SOFT};
        color: {T.ACCENT};
        font-weight: 600;
    }}

    /* ── Inputs ──────────────────────────────────────────────────────── */
    QLineEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
        background-color: {T.BG};
        border: 1px solid {T.BORDER};
        border-radius: {T.RADIUS_SM}px;
        padding: 7px 10px;
        color: {T.TEXT};
        selection-background-color: {T.ACCENT_DIM};
    }}
    QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus,
    QDoubleSpinBox:focus, QComboBox:focus {{
        border-color: {T.ACCENT};
    }}
    QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{
        color: {T.TEXT_MUTED};
        background-color: {T.SURFACE};
    }}
    QLineEdit[state="error"] {{ border-color: {T.NEGATIVE}; }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QComboBox QAbstractItemView {{
        background-color: {T.RAISED};
        border: 1px solid {T.BORDER};
        border-radius: {T.RADIUS_SM}px;
        selection-background-color: {T.ACCENT_DIM};
        outline: none;
        padding: 4px;
    }}
    QSpinBox::up-button, QSpinBox::down-button,
    QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
        background-color: {T.RAISED};
        border: none;
        width: 16px;
    }}

    QCheckBox, QRadioButton {{ color: {T.TEXT_DIM}; spacing: 8px; }}
    QCheckBox:hover, QRadioButton:hover {{ color: {T.TEXT}; }}
    QCheckBox::indicator, QRadioButton::indicator {{
        width: 15px; height: 15px;
        border: 1px solid {T.BORDER};
        background-color: {T.BG};
    }}
    QCheckBox::indicator {{ border-radius: 4px; }}
    QRadioButton::indicator {{ border-radius: 8px; }}
    QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
        background-color: {T.ACCENT};
        border-color: {T.ACCENT};
    }}
    QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
        border-color: {T.ACCENT};
    }}

    QGroupBox {{
        border: 1px solid {T.BORDER_SOFT};
        border-radius: {T.RADIUS}px;
        margin-top: 14px;
        padding: {T.PAD}px;
        background-color: {T.SURFACE};
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 12px;
        padding: 0 6px;
        color: {T.TEXT_MUTED};
        font-weight: 700;
        font-size: 11px;
    }}

    /* ── Tables ──────────────────────────────────────────────────────── */
    QTableView {{
        background-color: {T.SURFACE};
        alternate-background-color: {T.SURFACE_ALT};
        border: 1px solid {T.BORDER_SOFT};
        border-radius: {T.RADIUS}px;
        gridline-color: transparent;
        outline: none;
        selection-background-color: transparent;
    }}
    QTableView::item {{ border: none; padding: 0px; }}
    QHeaderView {{ background-color: transparent; }}
    QHeaderView::section {{
        background-color: {T.SURFACE};
        color: {T.TEXT_MUTED};
        border: none;
        border-bottom: 1px solid {T.BORDER};
        padding: 8px 6px;
        font-weight: 600;
        font-size: 11px;
    }}
    QTableCornerButton::section {{
        background-color: {T.SURFACE};
        border: none;
    }}

    /* ── Progress & scrollbars ───────────────────────────────────────── */
    QProgressBar {{
        background-color: {T.RAISED};
        border: none;
        border-radius: 3px;
        height: 6px;
        text-align: center;
        color: transparent;
    }}
    QProgressBar::chunk {{
        background-color: {T.ACCENT};
        border-radius: 3px;
    }}

    QScrollBar:vertical {{
        background: transparent; width: 10px; margin: 2px;
    }}
    QScrollBar:horizontal {{
        background: transparent; height: 10px; margin: 2px;
    }}
    QScrollBar::handle {{
        background: #2f3945; border-radius: 5px; min-height: 28px; min-width: 28px;
    }}
    QScrollBar::handle:hover {{ background: #3d4854; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

    /* ── Misc ────────────────────────────────────────────────────────── */
    QSplitter::handle {{ background-color: transparent; }}
    QDialog {{ background-color: {T.BG}; }}
    QMessageBox {{ background-color: {T.SURFACE}; }}
    QSlider::groove:horizontal {{
        height: 4px; background: {T.RAISED}; border-radius: 2px;
    }}
    QSlider::handle:horizontal {{
        background: {T.ACCENT}; width: 14px; height: 14px;
        margin: -6px 0; border-radius: 7px;
    }}
    QSlider::handle:horizontal:hover {{ background: {T.ACCENT_HOVER}; }}
    QSlider::sub-page:horizontal {{
        background: {T.ACCENT_DIM}; border-radius: 2px;
    }}
    """


def apply_theme(app):
    """Install the palette, font and stylesheet on the QApplication."""
    app.setStyle("Fusion")

    font = QFont(preferred_font_family(), T.FONT_SIZE)
    font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    app.setFont(font)

    # A matching palette keeps native-drawn bits (menus, tooltips, focus rings)
    # in step with the stylesheet instead of flashing light grey.
    p = QPalette()
    p.setColor(QPalette.ColorRole.Window,          QColor(T.BG))
    p.setColor(QPalette.ColorRole.WindowText,      QColor(T.TEXT))
    p.setColor(QPalette.ColorRole.Base,            QColor(T.SURFACE))
    p.setColor(QPalette.ColorRole.AlternateBase,   QColor(T.SURFACE_ALT))
    p.setColor(QPalette.ColorRole.Text,            QColor(T.TEXT))
    p.setColor(QPalette.ColorRole.Button,          QColor(T.RAISED))
    p.setColor(QPalette.ColorRole.ButtonText,      QColor(T.TEXT))
    p.setColor(QPalette.ColorRole.Highlight,       QColor(T.ACCENT_DIM))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    p.setColor(QPalette.ColorRole.ToolTipBase,     QColor(T.RAISED))
    p.setColor(QPalette.ColorRole.ToolTipText,     QColor(T.TEXT))
    p.setColor(QPalette.ColorRole.PlaceholderText, QColor(T.TEXT_MUTED))
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text,
               QColor(T.TEXT_MUTED))
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText,
               QColor(T.TEXT_MUTED))
    app.setPalette(p)

    app.setStyleSheet(stylesheet())


def shadow(widget, blur=28, dy=6, alpha=110):
    """Attach a soft drop shadow. Qt allows one graphics effect per widget."""
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setOffset(0, dy)
    effect.setColor(QColor(0, 0, 0, alpha))
    widget.setGraphicsEffect(effect)
    return widget


def separator(vertical=False):
    line = QFrame()
    line.setObjectName("VSeparator" if vertical else "Separator")
    line.setFrameShape(QFrame.Shape.VLine if vertical else QFrame.Shape.HLine)
    if vertical:
        line.setFixedWidth(1)
    else:
        line.setFixedHeight(1)
    return line


# ─── Painted icons ───────────────────────────────────────────────────────────
#
# The app ships no image assets, so navigation glyphs are drawn as vectors. That
# keeps them crisp at any DPI and recolourable per state, and avoids depending on
# whichever emoji/symbol font happens to be installed.

def _pen(painter, colour, width):
    pen = QPen(QColor(colour))
    pen.setWidthF(width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)


def _draw_scanner(p, c, s):
    _pen(p, c, s * 0.085)
    r = s * 0.26
    p.drawEllipse(QPointF(s * 0.44, s * 0.42), r, r)
    p.drawLine(QPointF(s * 0.63, s * 0.61), QPointF(s * 0.80, s * 0.78))


def _draw_positions(p, c, s):
    _pen(p, c, s * 0.085)
    for i, (y, w) in enumerate(((0.30, 0.52), (0.50, 0.68), (0.70, 0.40))):
        p.drawLine(QPointF(s * 0.22, s * y), QPointF(s * (0.22 + w), s * y))


def _draw_settings(p, c, s):
    _pen(p, c, s * 0.085)
    centre = QPointF(s * 0.5, s * 0.5)
    p.drawEllipse(centre, s * 0.16, s * 0.16)
    p.save()
    p.translate(centre)
    for _ in range(6):
        p.drawLine(QPointF(0, -s * 0.27), QPointF(0, -s * 0.36))
        p.rotate(60)
    p.restore()


def _draw_logs(p, c, s):
    _pen(p, c, s * 0.085)
    p.drawRoundedRect(QRectF(s * 0.24, s * 0.20, s * 0.52, s * 0.60),
                      s * 0.08, s * 0.08)
    for y in (0.36, 0.50, 0.64):
        p.drawLine(QPointF(s * 0.36, s * y), QPointF(s * 0.64, s * y))


def _draw_menu(p, c, s):
    _pen(p, c, s * 0.09)
    for y in (0.32, 0.50, 0.68):
        p.drawLine(QPointF(s * 0.24, s * y), QPointF(s * 0.76, s * y))


def _draw_chart(p, c, s):
    _pen(p, c, s * 0.085)
    path = QPainterPath(QPointF(s * 0.20, s * 0.70))
    path.lineTo(QPointF(s * 0.40, s * 0.44))
    path.lineTo(QPointF(s * 0.58, s * 0.58))
    path.lineTo(QPointF(s * 0.80, s * 0.26))
    p.drawPath(path)


def _draw_logo(p, c, s):
    _pen(p, c, s * 0.10)
    path = QPainterPath(QPointF(s * 0.16, s * 0.74))
    path.cubicTo(QPointF(s * 0.42, s * 0.74), QPointF(s * 0.40, s * 0.22),
                 QPointF(s * 0.84, s * 0.26))
    p.drawPath(path)
    p.setBrush(QColor(c))
    p.drawEllipse(QPointF(s * 0.84, s * 0.26), s * 0.07, s * 0.07)


_ICONS = {
    'scanner':   _draw_scanner,
    'positions': _draw_positions,
    'settings':  _draw_settings,
    'logs':      _draw_logs,
    'menu':      _draw_menu,
    'chart':     _draw_chart,
    'logo':      _draw_logo,
}


def make_icon(name, colour=T.TEXT_DIM, size=20) -> QIcon:
    """Vector glyph rendered to a QIcon at `size` logical pixels."""
    scale = 4                       # oversample so it stays sharp on HiDPI
    pixmap = QPixmap(size * scale, size * scale)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    draw = _ICONS.get(name, _draw_logs)
    draw(painter, colour, size * scale)
    painter.end()
    pixmap.setDevicePixelRatio(scale)
    return QIcon(pixmap)


# ─── Spinner ─────────────────────────────────────────────────────────────────

class Spinner(QWidget):
    """Indeterminate activity indicator: a rotating arc.

    Only repaints while running, and stops its timer when hidden, so an idle
    login screen costs nothing.
    """

    def __init__(self, size=22, colour=T.ACCENT, parent=None):
        super().__init__(parent)
        self._size = size
        self._colour = QColor(colour)
        self._angle = 0
        self.setFixedSize(size, size)
        self._timer = QTimer(self)
        self._timer.setInterval(16)          # ~60fps
        self._timer.timeout.connect(self._advance)
        self.hide()

    def sizeHint(self):
        return QSize(self._size, self._size)

    def start(self):
        self.show()
        if not self._timer.isActive():
            self._timer.start()

    def stop(self):
        self._timer.stop()
        self.hide()

    def _advance(self):
        self._angle = (self._angle + 6) % 360
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width = max(2.0, self._size * 0.12)
        rect = QRectF(width / 2, width / 2,
                      self._size - width, self._size - width)

        track = QPen(QColor(T.BORDER))
        track.setWidthF(width)
        painter.setPen(track)
        painter.drawEllipse(rect)

        arc = QPen(self._colour)
        arc.setWidthF(width)
        arc.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(arc)
        # Qt angles are in 1/16th of a degree, counter-clockwise.
        painter.drawArc(rect, -self._angle * 16, 100 * 16)
        painter.end()
