"""`QAbstractTableModel`s for the Results and Positions tables.

The view never stores data — it renders whatever the model holds. A live quote
mutates the model's row object and the model emits `dataChanged` for that row
only, so a 50-row stream repaints a handful of cells rather than rebuilding a
table.

Both models share the `Column` descriptor: a header, a raw-value getter, a
formatter, and whether the cell flashes on change. Flashes are a
`BackgroundRole` lookup with a single expiry timer — no overlay widgets.
"""

import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer
from PySide6.QtGui import QBrush, QColor

from data_models import Position, ScanResult
from pricing import fmt_iv, fmt_market_cap, fmt_money, fmt_pct_signed

FLASH_MS       = 700
FLASH_TICK_MS  = 150
SORT_ROLE      = int(Qt.ItemDataRole.UserRole) + 1

_FLASH_UP   = QColor("#2e7d32")
_FLASH_DOWN = QColor("#c62828")
_FLASH_TEXT = QColor("#ffffff")
_POS_TEXT   = QColor("#2e7d32")
_NEG_TEXT   = QColor("#c62828")
_MUTED      = QColor("#888888")


@dataclass(frozen=True)
class Column:
    header: str
    get:    Callable          # row object -> raw value
    fmt:    Callable = str    # raw value -> display string
    width:  int = 70
    flash:  bool = False
    signed: bool = False      # colour the text by sign


def _num(v):
    """Sort key that keeps missing values at the bottom."""
    return v if isinstance(v, (int, float)) else float('-inf')


def _pct(v):
    return f"{v * 100:.2f}%" if isinstance(v, (int, float)) else "—"


class _FlashTableModel(QAbstractTableModel):
    """Shared table plumbing: column descriptors, formatting, cell flashing."""

    COLUMNS: List[Column] = []

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = []
        self._flashes = {}        # (row, col) -> (expires_at, is_up)
        self._flash_timer = QTimer(self)
        self._flash_timer.setInterval(FLASH_TICK_MS)
        self._flash_timer.timeout.connect(self._expire_flashes)

    # ── Qt interface ──

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self.COLUMNS[section].header
        return section + 1

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row, col = index.row(), index.column()
        if not (0 <= row < len(self._rows)):
            return None
        spec  = self.COLUMNS[col]
        value = spec.get(self._rows[row])

        if role == Qt.ItemDataRole.DisplayRole:
            return spec.fmt(value)
        if role == SORT_ROLE:
            return _num(value) if isinstance(value, (int, float, type(None))) else str(value)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(Qt.AlignmentFlag.AlignCenter)

        flash = self._flashes.get((row, col))
        if role == Qt.ItemDataRole.BackgroundRole and flash:
            return QBrush(_FLASH_UP if flash[1] else _FLASH_DOWN)
        if role == Qt.ItemDataRole.ForegroundRole:
            if flash:
                return QBrush(_FLASH_TEXT)
            if spec.signed and isinstance(value, (int, float)):
                return QBrush(_POS_TEXT if value >= 0 else _NEG_TEXT)
            if value is None:
                return QBrush(_MUTED)
        return None

    # ── row access ──

    @property
    def rows(self):
        return self._rows

    def row_at(self, row):
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def set_rows(self, rows):
        self.beginResetModel()
        self._rows = list(rows)
        self._flashes.clear()
        self.endResetModel()

    def emit_row_changed(self, row):
        if 0 <= row < len(self._rows):
            self.dataChanged.emit(
                self.index(row, 0),
                self.index(row, len(self.COLUMNS) - 1),
            )

    # ── flashing ──

    def flash_cells(self, row, changes):
        """`changes` maps column index -> True (up) / False (down)."""
        if not changes:
            return
        expires = time.monotonic() + FLASH_MS / 1000.0
        for col, is_up in changes.items():
            self._flashes[(row, col)] = (expires, is_up)
        if not self._flash_timer.isActive():
            self._flash_timer.start()

    def _expire_flashes(self):
        now = time.monotonic()
        expired = [key for key, (until, _) in self._flashes.items() if until <= now]
        for key in expired:
            del self._flashes[key]
        if not self._flashes:
            self._flash_timer.stop()
        for row, col in expired:
            idx = self.index(row, col)
            self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.BackgroundRole,
                                             Qt.ItemDataRole.ForegroundRole])

    def clear_flashes(self):
        if not self._flashes:
            return
        cells = list(self._flashes)
        self._flashes.clear()
        self._flash_timer.stop()
        for row, col in cells:
            idx = self.index(row, col)
            self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.BackgroundRole,
                                             Qt.ItemDataRole.ForegroundRole])


# ─── Results ─────────────────────────────────────────────────────────────────

class ScanResultsModel(_FlashTableModel):
    """Table of calendar-spread candidates, updated in place by the live feed."""

    COLUMNS = [
        Column("Ticker",     lambda r: r.ticker,     str,             width=62),
        Column("Price",      lambda r: r.price,      fmt_money,       width=72, flash=True),
        Column("Mkt Cap",    lambda r: r.market_cap, fmt_market_cap,  width=80),
        Column("Strike",     lambda r: r.strike,     fmt_money,       width=72),
        Column("F-DTE",      lambda r: r.front_dte,  str,             width=52),
        Column("B-DTE",      lambda r: r.back_dte,   str,             width=52),
        Column("F-Bid",      lambda r: r.f_bid,      fmt_money,       width=62, flash=True),
        Column("F-Ask",      lambda r: r.f_ask,      fmt_money,       width=62, flash=True),
        Column("B-Bid",      lambda r: r.b_bid,      fmt_money,       width=62, flash=True),
        Column("B-Ask",      lambda r: r.b_ask,      fmt_money,       width=62, flash=True),
        Column("Front IV",   lambda r: r.front_iv,   _pct,            width=70),
        Column("Back IV",    lambda r: r.back_iv,    _pct,            width=70),
        Column("Fwd IV",     lambda r: r.fwd_iv,     _pct,            width=70),
        Column("Fwd Factor", lambda r: r.fwd_factor, fmt_pct_signed,  width=86, signed=True),
        Column("Debit",      lambda r: r.debit,      fmt_money,       width=68),
        Column("F-Spread",   lambda r: r.f_spread,   fmt_money,       width=72),
        Column("B-Spread",   lambda r: r.b_spread,   fmt_money,       width=72),
        Column("Earnings",   lambda r: r.earnings,   str,             width=92),
    ]

    # Columns whose change triggers a green/red flash, by index.
    _FLASH_COLS = {i: c for i, c in enumerate(COLUMNS) if c.flash}

    def apply_live_update(self, row: int, price, f_bid, f_ask, b_bid, b_ask, ivs):
        """Write a fresh quote snapshot + solved metrics into one row.

        Returns True if anything changed, so the controller can decide whether a
        re-sort is warranted.
        """
        r: ScanResult = self.row_at(row)
        if r is None:
            return False

        flashes = {}
        for col, spec in self._FLASH_COLS.items():
            old = spec.get(r)
            new = {"Price": price, "F-Bid": f_bid, "F-Ask": f_ask,
                   "B-Bid": b_bid, "B-Ask": b_ask}[spec.header]
            if isinstance(old, (int, float)) and old > 0 and abs(new - old) > 1e-9:
                flashes[col] = new > old

        r.price = price
        r.f_bid, r.f_ask = f_bid, f_ask
        r.b_bid, r.b_ask = b_bid, b_ask
        r.front_iv   = ivs.front_iv
        r.back_iv    = ivs.back_iv
        r.fwd_iv     = ivs.fwd_iv
        r.fwd_factor = ivs.fwd_factor
        r.debit      = ivs.debit

        self.flash_cells(row, flashes)
        self.emit_row_changed(row)
        return True

    def index_of_ticker(self, ticker) -> Optional[int]:
        for i, r in enumerate(self._rows):
            if r.ticker == ticker:
                return i
        return None


# ─── Positions ───────────────────────────────────────────────────────────────

class PositionsModel(_FlashTableModel):
    """Open calendar positions with live P/L."""

    COLUMNS = [
        Column("Ticker",    lambda p: p.ticker,        str,           width=62),
        Column("Strike",    lambda p: p.strike,        fmt_money,     width=66),
        Column("F-Exp",     lambda p: p.front_expiry,  str,           width=88),
        Column("B-Exp",     lambda p: p.back_expiry,   str,           width=88),
        Column("F-DTE",     lambda p: p.current_dtes()[0], str,       width=52),
        Column("B-DTE",     lambda p: p.current_dtes()[1], str,       width=52),
        Column("Ctr",       lambda p: p.contracts,     str,           width=42),
        Column("Front Cr",  lambda p: p.front_credit,  fmt_money,     width=72),
        Column("Back Pd",   lambda p: p.back_paid,     fmt_money,     width=72),
        Column("F-Bid",     lambda p: p.f_bid,         fmt_money,     width=62, flash=True),
        Column("F-Ask",     lambda p: p.f_ask,         fmt_money,     width=62, flash=True),
        Column("B-Bid",     lambda p: p.b_bid,         fmt_money,     width=62, flash=True),
        Column("B-Ask",     lambda p: p.b_ask,         fmt_money,     width=62, flash=True),
        Column("Front IV",  lambda p: p.front_iv,      fmt_iv,        width=68),
        Column("Back IV",   lambda p: p.back_iv,       fmt_iv,        width=68),
        Column("Fwd IV",    lambda p: p.fwd_iv,        fmt_iv,        width=68),
        Column("Cur FF",    lambda p: p.fwd_factor,    fmt_pct_signed, width=68, signed=True),
        Column("Open FF",   lambda p: p.opened_fwd_factor, fmt_pct_signed, width=68, signed=True),
        Column("Cur Debit", lambda p: p.cur_debit,     fmt_money,     width=74),
        Column("P/L $",     lambda p: p.cur_pl,        fmt_money,     width=78, signed=True),
    ]

    _FLASH_COLS = {i: c for i, c in enumerate(COLUMNS) if c.flash}
    _LEG_ATTRS  = {"F-Bid": "f_bid", "F-Ask": "f_ask",
                   "B-Bid": "b_bid", "B-Ask": "b_ask"}

    def index_of_id(self, pid) -> Optional[int]:
        for i, p in enumerate(self._rows):
            if p.id == pid:
                return i
        return None

    def position_at(self, row) -> Optional[Position]:
        return self.row_at(row)

    def begin_leg_flash(self, row, new_legs):
        """Record which leg cells changed *before* the position is mutated."""
        p = self.row_at(row)
        if p is None:
            return
        flashes = {}
        for col, spec in self._FLASH_COLS.items():
            old = getattr(p, self._LEG_ATTRS[spec.header])
            new = new_legs.get(self._LEG_ATTRS[spec.header])
            if (isinstance(old, (int, float)) and old > 0
                    and isinstance(new, (int, float)) and new > 0
                    and abs(new - old) > 1e-9):
                flashes[col] = new > old
        self.flash_cells(row, flashes)

    def total_pl(self):
        return sum(p.cur_pl or 0.0 for p in self._rows)

    def add(self, position):
        self.beginInsertRows(QModelIndex(), len(self._rows), len(self._rows))
        self._rows.append(position)
        self.endInsertRows()

    def remove(self, row):
        if not (0 <= row < len(self._rows)):
            return None
        self.beginRemoveRows(QModelIndex(), row, row)
        p = self._rows.pop(row)
        self.endRemoveRows()
        return p
