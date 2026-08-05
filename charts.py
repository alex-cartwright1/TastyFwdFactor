"""Interactive P/L chart at the front-leg expiration, rendered with PyQtGraph.

The three curves share one precomputed underlying-price grid. The two reference
curves (current back IV, implied forward IV) are static, so dragging the slider
only recomputes and `setData`s the single user curve — a vectorized NumPy
evaluation over 300 points, fast enough to track the slider without blocking the
event loop.

Three things live here:

* :class:`PLChartPanel` — the plot, the IV slider and the metrics line, as an
  embeddable widget. `set_fills()` re-bases every curve on new leg prices.
* :class:`PLChartWindow` — that panel in a standalone window. Unchanged API.
* :class:`SetupDetailWindow` — the double-click destination: editable front/back
  leg prices above a live panel, so typing a fill re-renders the payoff and the
  forward factor as you type.
"""

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSlider,
    QVBoxLayout, QWidget,
)

from pricing import (
    DAYS_PER_YEAR, PER_CONTRACT, breakevens, fmt_money, pl_curve, solve_fills,
)
from theme import T

# The plot canvas is the app's panel surface, so the chart reads as part of the
# window rather than a pasted-in white rectangle.
pg.setConfigOptions(antialias=True, background=T.SURFACE, foreground=T.TEXT_DIM)

# Slider works in tenths of a percent so it moves smoothly.
_SLIDER_MIN = 50      # 5.0 %
_SLIDER_MAX = 2000    # 200.0 %

# Reference curves stay muted; the interactive curve is the bright one.
_CURRENT_PEN = pg.mkPen(T.TEXT_MUTED, width=1.4, style=Qt.PenStyle.DashLine)
_FWD_PEN     = pg.mkPen(T.WARNING,    width=1.4, style=Qt.PenStyle.DotLine)
_USER_PEN    = pg.mkPen(T.ACCENT,     width=2.4)
_ZERO_PEN    = pg.mkPen("#3a4552",    width=1)
_STRIKE_PEN  = pg.mkPen("#4b5563",    width=1, style=Qt.PenStyle.DotLine)
_SPOT_PEN    = pg.mkPen(T.TEXT_MUTED, width=1, style=Qt.PenStyle.DashLine)
_FILL        = pg.mkBrush(76, 141, 255, 30)   # soft accent under the user curve


class PLChartPanel(QWidget):
    """P/L per contract at front expiration vs underlying, as an embeddable widget.

    `set_fills()` exists because the leg prices only shift the curves vertically
    — the Black-Scholes back-leg valuation doesn't depend on what was paid — so
    re-basing on a new fill is the same 300-point vectorized evaluation as moving
    the slider, and cheap enough to run on every keystroke.
    """

    def __init__(self, *, ticker, price, strike, front_dte, back_dte,
                 back_paid, front_credit, current_back_iv, fwd_iv,
                 show_heading=True, parent=None):
        super().__init__(parent)

        self.K  = float(strike)
        self.S0 = float(price)
        # Time the back leg still has to run once the front leg expires.
        self.t_remaining  = max((back_dte - front_dte) / DAYS_PER_YEAR, 1e-6)
        self.back_paid    = float(back_paid)
        self.front_credit = float(front_credit)
        self.current_back_iv = float(current_back_iv) if current_back_iv > 0 else 0.0
        self.fwd_iv          = float(fwd_iv) if fwd_iv > 0 else 0.0

        # Underlying price grid: ±35% around spot.
        self.S_range = np.linspace(max(self.S0 * 0.65, 0.01), self.S0 * 1.35, 300)

        self._pl_current = (self._curve(self.current_back_iv)
                            if self.current_back_iv > 0 else None)
        self._pl_fwd = self._curve(self.fwd_iv) if self.fwd_iv > 0 else None

        self._build_ui(ticker, show_heading)
        self._on_slider(self.slider.value())

    # ── math ──

    def set_fills(self, back_paid, front_credit,
                  current_back_iv=None, fwd_iv=None):
        """Re-base every curve on new leg prices and repaint.

        The IVs are optional because a fill implies them: the setup detail window
        re-solves both legs from what you typed and passes the result, so the
        reference lines track the trade you are actually modelling rather than
        the market snapshot the row was scanned at.
        """
        self.back_paid    = float(back_paid)
        self.front_credit = float(front_credit)
        if current_back_iv is not None:
            self.current_back_iv = float(current_back_iv) if current_back_iv > 0 else 0.0
        if fwd_iv is not None:
            self.fwd_iv = float(fwd_iv) if fwd_iv > 0 else 0.0

        self._pl_current = (self._curve(self.current_back_iv)
                            if self.current_back_iv > 0 else None)
        self._pl_fwd = self._curve(self.fwd_iv) if self.fwd_iv > 0 else None
        self._redraw_reference_curves()
        self._on_slider(self.slider.value())

    def _redraw_reference_curves(self):
        """Push new data into the two static curves rather than re-adding them —
        re-plotting would stack duplicate legend entries on every keystroke."""
        for curve, data, iv, label in (
            (self._current_curve, self._pl_current, self.current_back_iv, "Current back IV"),
            (self._fwd_curve,     self._pl_fwd,     self.fwd_iv,          "Implied fwd IV"),
        ):
            if data is None:
                curve.setData([], [])
                continue
            curve.setData(self.S_range, data)
            self._set_legend_text(curve, f"{label} ({iv * 100:.1f}%)")

    def _curve(self, back_iv):
        return pl_curve(self.S_range, self.K, self.t_remaining, back_iv,
                        self.back_paid, self.front_credit)

    @property
    def _default_pct(self):
        iv = (self.current_back_iv if self.current_back_iv > 0
              else self.fwd_iv if self.fwd_iv > 0 else 0.35)
        return int(round(iv * 1000))

    # ── widgets ──

    def _build_ui(self, ticker, show_heading=True):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16) if show_heading else \
            root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        if show_heading:
            heading = QLabel(f"{ticker} — P/L at front-leg expiration")
            heading.setProperty("role", "title")
            root.addWidget(heading)
            sub = QLabel(f"Per contract = {PER_CONTRACT} shares · "
                         f"back leg keeps {self.t_remaining * DAYS_PER_YEAR:.0f} days "
                         f"of extrinsic value")
            sub.setProperty("role", "subtitle")
            root.addWidget(sub)

        self.plot = pg.PlotWidget()
        self.plot.setLabel('bottom', "Underlying price at front expiration ($)")
        self.plot.setLabel('left', "P/L ($ per contract)")
        self.plot.showGrid(x=True, y=True, alpha=0.18)
        self.plot.setMouseEnabled(x=True, y=True)
        self.plot.setStyleSheet(
            f"border: 1px solid {T.BORDER_SOFT}; border-radius: {T.RADIUS}px;")
        for axis in ('bottom', 'left'):
            self.plot.getAxis(axis).setPen(pg.mkPen(T.BORDER))
            self.plot.getAxis(axis).setTextPen(pg.mkPen(T.TEXT_MUTED))
        self.legend = self.plot.addLegend(offset=(12, 12),
                                          labelTextColor=T.TEXT_DIM,
                                          brush=pg.mkBrush(21, 26, 33, 210),
                                          pen=pg.mkPen(T.BORDER))
        root.addWidget(self.plot, stretch=1)

        self.plot.addItem(pg.InfiniteLine(pos=0, angle=0, pen=_ZERO_PEN))
        self.plot.addItem(pg.InfiniteLine(
            pos=self.K, angle=90, pen=_STRIKE_PEN,
            label=f"Strike ${self.K:.2f}",
            labelOpts={'position': 0.93, 'color': T.TEXT_MUTED}))
        self.plot.addItem(pg.InfiniteLine(
            pos=self.S0, angle=90, pen=_SPOT_PEN,
            label=f"Spot ${self.S0:.2f}",
            labelOpts={'position': 0.86, 'color': T.TEXT_DIM}))

        # Both reference curves are created unconditionally, empty if there is no
        # data yet, so `set_fills` has something to `setData` into later.
        self._current_curve = self.plot.plot(
            [], [], pen=_CURRENT_PEN,
            name=f"Current back IV ({self.current_back_iv * 100:.1f}%)")
        self._fwd_curve = self.plot.plot(
            [], [], pen=_FWD_PEN,
            name=f"Implied fwd IV ({self.fwd_iv * 100:.1f}%)")
        self._redraw_reference_curves()

        # Shade profit/loss against the zero line so the payoff region reads at
        # a glance. The zero baseline is a flat curve the fill can reference.
        self._zero_curve = pg.PlotCurveItem(self.S_range,
                                            np.zeros_like(self.S_range))
        self._zero_curve.setVisible(False)
        self.plot.addItem(self._zero_curve)
        self.user_curve = self.plot.plot([], [], pen=_USER_PEN, name="Slider IV")
        self._fill = pg.FillBetweenItem(self.user_curve, self._zero_curve,
                                        brush=_FILL)
        self.plot.addItem(self._fill)

        # ── controls ──
        bar = QWidget()
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(0, 0, 0, 0)
        bar_layout.setSpacing(12)
        label = QLabel("BACK-LEG IV AT FRONT EXPIRATION")
        label.setProperty("role", "section")
        bar_layout.addWidget(label)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(_SLIDER_MIN, _SLIDER_MAX)
        self.slider.setValue(min(max(self._default_pct, _SLIDER_MIN), _SLIDER_MAX))
        self.slider.setSingleStep(5)
        self.slider.setPageStep(50)
        self.slider.valueChanged.connect(self._on_slider)
        bar_layout.addWidget(self.slider, stretch=1)

        self.iv_label = QLabel()
        self.iv_label.setMinimumWidth(70)
        self.iv_label.setAlignment(Qt.AlignmentFlag.AlignRight |
                                   Qt.AlignmentFlag.AlignVCenter)
        self.iv_label.setProperty("role", "metric")
        self.iv_label.setStyleSheet(f"color: {T.ACCENT}; font-weight: 600;")
        bar_layout.addWidget(self.iv_label)

        reset = QPushButton("Reset")
        reset.clicked.connect(lambda: self.slider.setValue(self._default_pct))
        bar_layout.addWidget(reset)
        root.addWidget(bar)

        self.metrics = QLabel()
        self.metrics.setStyleSheet(
            f"color: {T.TEXT_DIM}; font-family: {T.MONO}; font-size: 12px;")
        self.metrics.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.metrics)

    def _set_legend_text(self, curve, text):
        """Retitle a curve's legend entry. `LegendItem.items` is a list of
        (sample, label) pairs and the sample's `item` is the curve."""
        for sample, label in getattr(self.legend, 'items', []):
            if getattr(sample, 'item', None) is curve:
                label.setText(text)
                return

    # ── slider ──

    def _on_slider(self, value):
        iv_pct = value / 10.0
        self.iv_label.setText(f"{iv_pct:.1f}%")

        # Only the user curve is recomputed — the two reference curves are static.
        pl = self._curve(max(iv_pct / 100.0, 1e-4))
        self.user_curve.setData(self.S_range, pl)
        self._set_legend_text(self.user_curve, f"Slider back IV ({iv_pct:.1f}%)")

        stacks = [pl]
        if self._pl_current is not None: stacks.append(self._pl_current)
        if self._pl_fwd is not None:     stacks.append(self._pl_fwd)
        ymin = float(min(v.min() for v in stacks))
        ymax = float(max(v.max() for v in stacks))
        margin = max((ymax - ymin) * 0.1, 1.0)
        self.plot.setXRange(self.S_range[0], self.S_range[-1], padding=0)
        self.plot.setYRange(ymin - margin, ymax + margin, padding=0)

        max_profit = float(pl.max())
        max_at = float(self.S_range[int(np.argmax(pl))])
        bes = breakevens(self.S_range, pl)
        be_str = ", ".join(f"${b:.2f}" for b in bes) if bes else "none in range"
        net_debit = (self.back_paid - self.front_credit) * PER_CONTRACT
        self.metrics.setText(
            f"Net debit (entry):  ${net_debit:.2f} per contract   |   "
            f"Max profit on slider line: ${max_profit:.2f} at S=${max_at:.2f}\n"
            f"Breakevens: {be_str}"
        )


class PLChartWindow(QWidget):
    """Standalone window wrapping a `PLChartPanel`.

    Kept as its own class so existing call sites (`MainWindow._open_chart`) are
    untouched by the panel extraction.
    """

    def __init__(self, *, ticker, parent=None, **kwargs):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle(f"P/L at Front Expiration — {ticker}")
        self.resize(980, 720)

        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        self.panel = PLChartPanel(ticker=ticker, **kwargs)
        box.addWidget(self.panel)


# ─── Setup detail: editable fills driving the chart ──────────────────────────

# Typing is recomputed after a short idle rather than on every keystroke. The
# maths is microseconds, but a partial entry ("1", "1.", "1.2") would otherwise
# repaint three times and flash the legend on the way to one intended value.
_RECALC_DEBOUNCE_MS = 120


class SetupDetailWindow(QWidget):
    """Double-click destination for a scanner row: model a fill on this setup.

    Front and back leg prices are editable; every edit re-solves both legs' IVs
    from the entered prices (`pricing.solve_fills` — the same model the scan
    uses, so the numbers are comparable), updates the net debit / max risk /
    forward factor readout, and re-bases the P/L curves underneath.

    Seeded from the row's live mid prices, so opening it and changing nothing
    shows the setup exactly as the scanner ranked it.
    """

    def __init__(self, result, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.r = result
        self.setWindowTitle(f"Setup — {result.ticker} ${result.strike:.2f} "
                            f"{result.front_dte}/{result.back_dte} DTE")
        self.resize(1020, 820)

        # Mid prices are the natural starting fill: the scanner ranked the row on
        # them, so the panel opens showing the setup as listed.
        self._front0 = self._mid(result.f_bid, result.f_ask)
        self._back0  = self._mid(result.b_bid, result.b_ask)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(_RECALC_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._recalc)

        self._build_ui()
        self._reset()

    @staticmethod
    def _mid(bid, ask):
        if bid > 0 and ask > 0:
            return (bid + ask) / 2
        return bid or ask or 0.0

    @staticmethod
    def _try_float(text):
        try:
            return float(text)
        except (TypeError, ValueError):
            return None

    # ── widgets ──

    def _build_ui(self):
        r = self.r
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        heading = QLabel(f"{r.ticker} — model a fill")
        heading.setProperty("role", "title")
        root.addWidget(heading)
        sub = QLabel(f"${r.price:.2f} spot · strike ${r.strike:.2f} · "
                     f"{r.front_dte}/{r.back_dte} DTE · "
                     f"scanned fwd factor {r.fwd_factor * 100:+.2f}%")
        sub.setProperty("role", "subtitle")
        root.addWidget(sub)

        inputs = QWidget()
        inputs.setObjectName("Card")
        grid = QGridLayout(inputs)
        grid.setContentsMargins(14, 12, 14, 12)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(6)

        self.front_edit = self._price_field(
            grid, 0, "Front leg price (credit)",
            f"market {fmt_money(self._front0)}")
        self.back_edit = self._price_field(
            grid, 1, "Back leg price (paid)",
            f"market {fmt_money(self._back0)}")

        reset = QPushButton("Reset to market")
        reset.clicked.connect(self._reset)
        grid.addWidget(reset, 0, 2, 2, 1, Qt.AlignmentFlag.AlignVCenter)

        self.readout = QLabel()
        self.readout.setStyleSheet(
            f"color: {T.TEXT_DIM}; font-family: {T.MONO}; font-size: 12px;")
        self.readout.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        grid.addWidget(self.readout, 0, 3, 2, 1)
        grid.setColumnStretch(3, 1)
        root.addWidget(inputs)

        self.panel = PLChartPanel(
            ticker=r.ticker, price=r.price, strike=r.strike,
            front_dte=r.front_dte, back_dte=r.back_dte,
            back_paid=self._back0, front_credit=self._front0,
            current_back_iv=r.back_iv, fwd_iv=r.fwd_iv,
            show_heading=False, parent=self)
        root.addWidget(self.panel, stretch=1)

    def _price_field(self, grid, row, label_text, hint):
        caption = QLabel(label_text.upper())
        caption.setProperty("role", "hint")
        grid.addWidget(caption, row, 0)

        edit = QLineEdit()
        edit.setPlaceholderText("0.00")
        edit.setFixedWidth(120)
        edit.setToolTip(hint)
        # textEdited, not textChanged: the programmatic setText in _reset must
        # not re-enter the debounce and fight the user's cursor.
        edit.textEdited.connect(self._on_edited)
        grid.addWidget(edit, row, 1)
        return edit

    # ── recalculation ──

    def _reset(self):
        self.front_edit.setText(f"{self._front0:.2f}")
        self.back_edit.setText(f"{self._back0:.2f}")
        self._recalc()

    def _on_edited(self, _text):
        self._debounce.start()

    def _recalc(self):
        r = self.r
        front = self._try_float(self.front_edit.text())
        back  = self._try_float(self.back_edit.text())
        if front is None or back is None or front <= 0 or back <= 0:
            self._show_invalid("Enter a positive price for both legs")
            return

        ivs = solve_fills(r.price, r.strike, r.front_dte, r.back_dte, front, back)
        if ivs is None:
            # Most often a back leg cheaper than the front, which inverts the
            # term structure and leaves no real forward vol to quote.
            self._show_invalid("No forward vol at these prices "
                               "(inverted term structure)")
            # Still re-base the curves: the payoff is well defined even when the
            # forward factor isn't.
            self.panel.set_fills(back, front)
            return

        colour = T.POSITIVE if ivs.fwd_factor >= 0 else T.NEGATIVE
        self.readout.setStyleSheet(
            f"color: {T.TEXT_DIM}; font-family: {T.MONO}; font-size: 12px;")
        self.readout.setText(
            f"<span style='color:{colour}; font-weight:600'>"
            f"Fwd factor {ivs.fwd_factor * 100:+.2f}%</span>"
            f" &nbsp;·&nbsp; front IV {ivs.front_iv * 100:.1f}%"
            f" &nbsp;·&nbsp; back IV {ivs.back_iv * 100:.1f}%"
            f" &nbsp;·&nbsp; fwd IV {ivs.fwd_iv * 100:.1f}%<br>"
            f"Net debit {fmt_money(ivs.debit)}/share"
            f" &nbsp;·&nbsp; entry cost {fmt_money(ivs.debit * PER_CONTRACT)}"
            f" &nbsp;·&nbsp; max risk {fmt_money(ivs.max_risk)}")
        self.panel.set_fills(back, front,
                             current_back_iv=ivs.back_iv, fwd_iv=ivs.fwd_iv)

    def _show_invalid(self, message):
        self.readout.setText(
            f"<span style='color:{T.WARNING}'>{message}</span>")
