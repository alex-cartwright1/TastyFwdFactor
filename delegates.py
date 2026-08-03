"""Custom painting for the table views.

`QStyledItemDelegate` takes over cell rendering so the tables can do things the
stylesheet can't: colour a value by its sign, and fade a coloured wash out of a
cell over ~900 ms when a live WebSocket tick changes it.

The delegate reads only model roles (`SIGNED_ROLE`, `FLASH_ROLE`,
`NUMERIC_ROLE`), so it works unchanged through a `QSortFilterProxyModel` and
never needs to know which model it is decorating.
"""

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QPainter, QPen, QPolygonF,
)
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QHeaderView, QStyle, QStyledItemDelegate

from models import FLASH_ROLE, NUMERIC_ROLE, SIGNED_ROLE
from theme import T

_CELL_RADIUS   = 5
_FLASH_ALPHA   = 90     # peak wash opacity, 0-255
_SELECT_COLOUR = QColor(T.ACCENT)


def _blend(base: QColor, over: QColor, amount: float) -> QColor:
    """Linear blend of `over` onto `base` by `amount` in 0..1."""
    amount = max(0.0, min(1.0, amount))
    return QColor(
        int(base.red()   + (over.red()   - base.red())   * amount),
        int(base.green() + (over.green() - base.green()) * amount),
        int(base.blue()  + (over.blue()  - base.blue())  * amount),
    )


class SignedFlashDelegate(QStyledItemDelegate):
    """Paints every cell: background, flash wash, then sign-coloured text.

    Painting the background ourselves means alternating rows, selection and the
    flash overlay compose predictably instead of fighting the stylesheet.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._base      = QColor(T.SURFACE)
        self._alt       = QColor(T.SURFACE_ALT)
        self._text      = QColor(T.TEXT)
        self._dim       = QColor(T.TEXT_MUTED)
        self._positive  = QColor(T.POSITIVE)
        self._negative  = QColor(T.NEGATIVE)
        self._selection = QColor(T.ACCENT)
        self._selection.setAlpha(56)
        self._focus     = QColor(T.ACCENT)
        self._focus.setAlpha(90)

    def paint(self, painter: QPainter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(option.rect)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)

        # ── background ──
        background = self._alt if index.row() % 2 else self._base
        painter.fillRect(rect, background)

        flash = index.data(FLASH_ROLE)
        if flash:
            progress, is_up = flash
            if progress > 0:
                wash = QColor(T.FLASH_UP if is_up else T.FLASH_DOWN)
                wash.setAlpha(int(_FLASH_ALPHA * progress))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(wash)
                painter.drawRoundedRect(rect.adjusted(1, 1.5, -1, -1.5),
                                        _CELL_RADIUS, _CELL_RADIUS)

        if selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._selection)
            painter.drawRoundedRect(rect.adjusted(0, 0.5, 0, -0.5),
                                    _CELL_RADIUS, _CELL_RADIUS)

        # ── text ──
        text = index.data(Qt.ItemDataRole.DisplayRole)
        if text:
            colour = self._text
            value  = index.data(NUMERIC_ROLE)
            if index.data(SIGNED_ROLE) and value is not None:
                colour = self._positive if value >= 0 else self._negative
            elif text == "—":
                colour = self._dim

            font: QFont = QFont(option.font)
            if flash and flash[0] > 0:
                # Lift the text toward white as the wash peaks, so the change
                # reads even on a column that is already coloured.
                colour = _blend(colour, QColor("#ffffff"), 0.55 * flash[0])
                font.setWeight(QFont.Weight.DemiBold)
            elif index.data(SIGNED_ROLE):
                font.setWeight(QFont.Weight.Medium)

            painter.setFont(font)
            painter.setPen(QPen(colour))
            alignment = index.data(Qt.ItemDataRole.TextAlignmentRole)
            alignment = (Qt.AlignmentFlag(alignment) if alignment
                         else Qt.AlignmentFlag.AlignCenter)
            text_rect = option.rect.adjusted(6, 0, -6, 0)
            label = QFontMetrics(font).elidedText(
                str(text), Qt.TextElideMode.ElideRight, text_rect.width())
            painter.drawText(text_rect, int(alignment), label)

        painter.restore()

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        size.setHeight(max(size.height(), 26))
        return size


class SortHeaderView(QHeaderView):
    """Header that draws its own sort indicator.

    Qt's built-in arrow is a style primitive that ignores the stylesheet's
    colours, so it is suppressed and replaced with a small accent triangle that
    matches the theme and clearly shows sort direction.
    """

    def __init__(self, orientation=Qt.Orientation.Horizontal, parent=None):
        super().__init__(orientation, parent)
        self.setSectionsClickable(True)
        self.setHighlightSections(False)
        self.setSortIndicatorShown(True)
        self.setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedHeight(34)

    def paintSection(self, painter: QPainter, rect, logical_index):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        painter.fillRect(rect, QColor(T.SURFACE))
        painter.setPen(QPen(QColor(T.BORDER)))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())

        model = self.model()
        text = model.headerData(logical_index, self.orientation(),
                                Qt.ItemDataRole.DisplayRole) if model else ""
        is_sorted = (self.isSortIndicatorShown()
                     and self.sortIndicatorSection() == logical_index)

        font = QFont(self.font())
        font.setWeight(QFont.Weight.DemiBold)
        font.setPointSizeF(max(8.0, self.font().pointSizeF() - 0.5))
        painter.setFont(font)
        painter.setPen(QPen(QColor(T.ACCENT if is_sorted else T.TEXT_MUTED)))

        # Elide rather than clip, so a narrow column shows "Fwd Fac…" instead of
        # a header chopped mid-glyph.
        text_rect = rect.adjusted(5, 0, -14 if is_sorted else -5, 0)
        label = str(text) if text is not None else ""
        label = QFontMetrics(font).elidedText(
            label, Qt.TextElideMode.ElideRight, text_rect.width())
        painter.drawText(text_rect, int(Qt.AlignmentFlag.AlignCenter), label)

        if is_sorted:
            self._draw_arrow(painter, rect,
                             self.sortIndicatorOrder() == Qt.SortOrder.AscendingOrder)
        painter.restore()

    @staticmethod
    def _draw_arrow(painter: QPainter, rect, ascending):
        cx = rect.right() - 8
        cy = rect.center().y() + 1
        w, h = 4.0, 3.0
        if ascending:
            points = [QPointF(cx - w, cy + h / 2), QPointF(cx + w, cy + h / 2),
                      QPointF(cx, cy - h)]
        else:
            points = [QPointF(cx - w, cy - h / 2), QPointF(cx + w, cy - h / 2),
                      QPointF(cx, cy + h)]
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(T.ACCENT))
        painter.drawPolygon(QPolygonF(points))
