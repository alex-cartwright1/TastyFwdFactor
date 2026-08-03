"""Interactive P/L chart at the front-leg expiration, rendered with PyQtGraph.

The three curves share one precomputed underlying-price grid. The two reference
curves (current back IV, implied forward IV) are static, so dragging the slider
only recomputes and `setData`s the single user curve — a vectorized NumPy
evaluation over 300 points, fast enough to track the slider without blocking the
event loop.
"""

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget,
)

from pricing import DAYS_PER_YEAR, PER_CONTRACT, breakevens, pl_curve

pg.setConfigOptions(antialias=True, background='w', foreground='k')

# Slider works in tenths of a percent so it moves smoothly.
_SLIDER_MIN = 50      # 5.0 %
_SLIDER_MAX = 2000    # 200.0 %

_CURRENT_PEN = pg.mkPen('#7f8c8d', width=1, style=Qt.PenStyle.DashLine)
_FWD_PEN     = pg.mkPen('#7f8c8d', width=1, style=Qt.PenStyle.DotLine)
_USER_PEN    = pg.mkPen('#1f6feb', width=2)
_ZERO_PEN    = pg.mkPen('#000000', width=1)
_STRIKE_PEN  = pg.mkPen('#95a5a6', width=1, style=Qt.PenStyle.DotLine)
_SPOT_PEN    = pg.mkPen('#566573', width=1, style=Qt.PenStyle.DashLine)


class PLChartWindow(QWidget):
    """Standalone window: P/L per contract at front expiration vs underlying."""

    def __init__(self, *, ticker, price, strike, front_dte, back_dte,
                 back_paid, front_credit, current_back_iv, fwd_iv, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle(f"P/L at Front Expiration — {ticker}")
        self.resize(980, 720)

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

        self._build_ui(ticker)
        self._on_slider(self.slider.value())

    # ── math ──

    def _curve(self, back_iv):
        return pl_curve(self.S_range, self.K, self.t_remaining, back_iv,
                        self.back_paid, self.front_credit)

    @property
    def _default_pct(self):
        iv = (self.current_back_iv if self.current_back_iv > 0
              else self.fwd_iv if self.fwd_iv > 0 else 0.35)
        return int(round(iv * 1000))

    # ── widgets ──

    def _build_ui(self, ticker):
        root = QVBoxLayout(self)

        self.plot = pg.PlotWidget()
        self.plot.setTitle(f"{ticker} calendar spread — P/L at front-leg expiration "
                           f"(per contract = {PER_CONTRACT} shares)")
        self.plot.setLabel('bottom', "Underlying price at front expiration ($)")
        self.plot.setLabel('left', "P/L ($ per contract)")
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.legend = self.plot.addLegend(offset=(10, 10))
        self.plot.setMouseEnabled(x=True, y=True)
        root.addWidget(self.plot, stretch=1)

        self.plot.addItem(pg.InfiniteLine(pos=0, angle=0, pen=_ZERO_PEN))
        self.plot.addItem(pg.InfiniteLine(
            pos=self.K, angle=90, pen=_STRIKE_PEN,
            label=f"Strike ${self.K:.2f}", labelOpts={'position': 0.92, 'color': '#7f8c8d'}))
        self.plot.addItem(pg.InfiniteLine(
            pos=self.S0, angle=90, pen=_SPOT_PEN,
            label=f"Spot ${self.S0:.2f}", labelOpts={'position': 0.85, 'color': '#566573'}))

        if self._pl_current is not None:
            self.plot.plot(self.S_range, self._pl_current, pen=_CURRENT_PEN,
                           name=f"Current back IV ({self.current_back_iv * 100:.1f}%)")
        if self._pl_fwd is not None:
            self.plot.plot(self.S_range, self._pl_fwd, pen=_FWD_PEN,
                           name=f"Implied fwd IV ({self.fwd_iv * 100:.1f}%)")

        self.user_curve = self.plot.plot([], [], pen=_USER_PEN, name="Slider IV")

        # ── controls ──
        bar = QWidget()
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(0, 0, 0, 0)
        label = QLabel("Back-leg IV at front expiration:")
        label.setStyleSheet("font-weight: 600;")
        bar_layout.addWidget(label)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(_SLIDER_MIN, _SLIDER_MAX)
        self.slider.setValue(min(max(self._default_pct, _SLIDER_MIN), _SLIDER_MAX))
        self.slider.setSingleStep(5)
        self.slider.setPageStep(50)
        self.slider.valueChanged.connect(self._on_slider)
        bar_layout.addWidget(self.slider, stretch=1)

        self.iv_label = QLabel()
        self.iv_label.setMinimumWidth(64)
        self.iv_label.setAlignment(Qt.AlignmentFlag.AlignRight |
                                   Qt.AlignmentFlag.AlignVCenter)
        self.iv_label.setStyleSheet("font-weight: 600; color: #1f6feb;")
        bar_layout.addWidget(self.iv_label)

        reset = QPushButton("Reset")
        reset.clicked.connect(lambda: self.slider.setValue(self._default_pct))
        bar_layout.addWidget(reset)
        root.addWidget(bar)

        self.metrics = QLabel()
        self.metrics.setStyleSheet("font-family: monospace;")
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
