"""Modal dialogs: scan settings/filters and add/edit position."""

import uuid
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QFrame, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from config import IV_METHODS, clear_ticker_cache, save_settings
from data_models import Position


def _hint(text):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color: #888; font-size: 10px;")
    return label


def _section(text):
    label = QLabel(text)
    label.setStyleSheet("font-weight: 600;")
    return label


class FiltersDialog(QDialog):
    """Scan parameters and filters. Persists to settings.json on accept."""

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Filters & Scan Settings")
        self.setMinimumWidth(460)
        self._settings = dict(settings)

        root = QVBoxLayout(self)
        root.addWidget(_section("Scan Parameters"))

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        root.addLayout(form)

        self.csv_edit = QLineEdit(settings['csv_path'])
        browse = QPushButton("…")
        browse.setFixedWidth(32)
        browse.clicked.connect(self._browse)
        csv_row = QWidget()
        csv_layout = QHBoxLayout(csv_row)
        csv_layout.setContentsMargins(0, 0, 0, 0)
        csv_layout.addWidget(self.csv_edit)
        csv_layout.addWidget(browse)
        form.addRow("Watchlist CSV:", csv_row)

        self.front_dte = self._int_spin(settings['front_dte'], 1, 3650)
        self.front_flex = self._int_spin(int(settings.get('front_dte_flex', 0) or 0), 0, 365)
        form.addRow("Target Front DTE:", self._dte_row(self.front_dte, self.front_flex))

        self.back_dte = self._int_spin(settings['back_dte'], 1, 3650)
        self.back_flex = self._int_spin(int(settings.get('back_dte_flex', 0) or 0), 0, 365)
        form.addRow("Target Back DTE:", self._dte_row(self.back_dte, self.back_flex))

        root.addWidget(_hint("Flex 0 = pick the nearest expiration; >0 = restrict to "
                             "target ± flex days."))

        self.iv_method = QComboBox()
        self.iv_method.addItems(IV_METHODS)
        idx = self.iv_method.findText(settings['iv_method'])
        self.iv_method.setCurrentIndex(max(idx, 0))
        form2 = QFormLayout()
        form2.addRow("IV Method:", self.iv_method)
        root.addLayout(form2)

        root.addWidget(self._separator())
        root.addWidget(_section("Filters"))

        filters_form = QFormLayout()
        self.min_price = QDoubleSpinBox()
        self.min_price.setRange(0.0, 100000.0)
        self.min_price.setDecimals(2)
        self.min_price.setValue(float(settings['min_price']))
        filters_form.addRow("Min Price ($):", self.min_price)

        self.min_cap = QLineEdit(str(settings['min_market_cap_b']))
        self.min_cap.setPlaceholderText("blank = no filter")
        filters_form.addRow("Min Market Cap ($B):", self.min_cap)
        root.addLayout(filters_form)

        self.f_earn = self._check("Exclude tickers with earnings before front-leg expiry",
                                  settings['filter_front_earnings'], root)
        self.b_earn = self._check("Exclude tickers with earnings before back-leg expiry",
                                  settings['filter_back_earnings'], root)
        self.f_div = self._check("Exclude tickers with ex-dividend before front-leg expiry",
                                 settings['filter_front_dividend'], root)
        self.b_div = self._check("Exclude tickers with ex-dividend before back-leg expiry",
                                 settings['filter_back_dividend'], root)
        self.no_earn = self._check(
            "Exclude tickers with no recorded earnings date  (ETFs, leveraged funds)",
            settings.get('filter_unknown_earnings', False), root)

        root.addWidget(self._separator())
        cache_box = QGroupBox("Ticker Info Cache")
        cache_layout = QVBoxLayout(cache_box)
        ttl_form = QFormLayout()
        self.ttl_days = self._int_spin(int(settings.get('ticker_info_ttl_days', 7)), 0, 365)
        ttl_form.addRow("Refresh after (days, 0 = always):", self.ttl_days)
        cache_layout.addLayout(ttl_form)
        refresh_btn = QPushButton("Refresh ticker data now (clear cache)")
        refresh_btn.clicked.connect(self._refresh_cache)
        cache_layout.addWidget(refresh_btn)
        root.addWidget(cache_box)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save |
                                   QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # ── helpers ──

    @staticmethod
    def _int_spin(value, lo, hi):
        spin = QSpinBox()
        spin.setRange(lo, hi)
        spin.setValue(int(value))
        return spin

    @staticmethod
    def _dte_row(dte_spin, flex_spin):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(dte_spin)
        layout.addWidget(QLabel("± flex (days):"))
        layout.addWidget(flex_spin)
        layout.addStretch(1)
        return row

    @staticmethod
    def _check(text, checked, layout):
        box = QCheckBox(text)
        box.setChecked(bool(checked))
        layout.addWidget(box)
        return box

    @staticmethod
    def _separator():
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        return line

    def _browse(self):
        fn, _ = QFileDialog.getOpenFileName(
            self, "Select watchlist CSV", self.csv_edit.text(),
            "CSV files (*.csv);;All files (*)")
        if fn:
            self.csv_edit.setText(fn)

    def _refresh_cache(self):
        clear_ticker_cache()
        QMessageBox.information(
            self, "Cache cleared",
            "Ticker info cache cleared. The next scan will re-scrape earnings, "
            "dividends and market caps for all tickers.")

    # ── result ──

    def _save(self):
        if self.front_dte.value() >= self.back_dte.value():
            QMessageBox.warning(self, "Invalid DTEs",
                                "Back DTE must be greater than Front DTE.")
            return
        cap = self.min_cap.text().strip()
        if cap:
            try:
                float(cap)
            except ValueError:
                QMessageBox.warning(self, "Invalid Input",
                                    "Min Market Cap must be a number (or blank).")
                return

        self._settings.update({
            'csv_path':                self.csv_edit.text(),
            'front_dte':               self.front_dte.value(),
            'back_dte':                self.back_dte.value(),
            'front_dte_flex':          self.front_flex.value(),
            'back_dte_flex':           self.back_flex.value(),
            'iv_method':               self.iv_method.currentText(),
            'min_price':               self.min_price.value(),
            'min_market_cap_b':        cap,
            'filter_front_earnings':   self.f_earn.isChecked(),
            'filter_back_earnings':    self.b_earn.isChecked(),
            'filter_front_dividend':   self.f_div.isChecked(),
            'filter_back_dividend':    self.b_div.isChecked(),
            'filter_unknown_earnings': self.no_earn.isChecked(),
            'ticker_info_ttl_days':    self.ttl_days.value(),
        })
        save_settings(self._settings)
        self.accept()

    def settings(self):
        return self._settings


class PositionDialog(QDialog):
    """Enter or edit a calendar-spread position."""

    def __init__(self, existing: Position = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Position" if existing else "Add Position")
        self._existing = existing
        self._result = None

        root = QVBoxLayout(self)
        form = QFormLayout()
        root.addLayout(form)

        e = existing
        self.ticker = QLineEdit(e.ticker if e else '')
        self.strike = QLineEdit(f"{e.strike}" if e else '')
        self.front_expiry = QLineEdit(e.front_expiry if e else '')
        self.back_expiry  = QLineEdit(e.back_expiry if e else '')
        self.front_credit = QLineEdit(f"{e.front_credit}" if e else '')
        self.back_paid    = QLineEdit(f"{e.back_paid}" if e else '')
        self.contracts    = QSpinBox()
        self.contracts.setRange(1, 10000)
        self.contracts.setValue(int(e.contracts) if e else 1)
        self.notes = QLineEdit(e.notes if e else '')

        for label, widget in (
            ("Ticker:", self.ticker),
            ("Strike ($):", self.strike),
            ("Front expiry (YYYY-MM-DD):", self.front_expiry),
            ("Back expiry (YYYY-MM-DD):", self.back_expiry),
            ("Front leg credit ($):", self.front_credit),
            ("Back leg paid ($):", self.back_paid),
            ("Contracts:", self.contracts),
            ("Notes (optional):", self.notes),
        ):
            form.addRow(label, widget)

        root.addWidget(_hint(
            "Expirations must match a date present in the option chain, and the "
            "strike must match a listed strike. Credits are per-share "
            "(e.g. 1.50 means $150 per contract)."))

        buttons = QDialogButtonBox(
            (QDialogButtonBox.StandardButton.Save if existing
             else QDialogButtonBox.StandardButton.Ok) |
            QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _save(self):
        ticker = self.ticker.text().strip().upper()
        if not ticker:
            QMessageBox.warning(self, "Invalid Input", "Ticker is required.")
            return
        try:
            strike = float(self.strike.text())
            f_cred = float(self.front_credit.text())
            b_paid = float(self.back_paid.text())
        except (TypeError, ValueError) as exc:
            QMessageBox.warning(self, "Invalid Input", f"Check numeric fields: {exc}")
            return

        f_exp = self.front_expiry.text().strip()
        b_exp = self.back_expiry.text().strip()
        try:
            f_date = datetime.strptime(f_exp, '%Y-%m-%d').date()
            b_date = datetime.strptime(b_exp, '%Y-%m-%d').date()
        except ValueError:
            QMessageBox.warning(self, "Invalid Input", "Expirations must be YYYY-MM-DD.")
            return
        if b_date <= f_date:
            QMessageBox.warning(self, "Invalid Input",
                                "Back expiry must be after front expiry.")
            return

        # Always hand back a fresh Position — the controller diffs it against the
        # original to decide whether the cached streamer symbols are still valid.
        e = self._existing
        self._result = Position(
            id           = e.id if e else uuid.uuid4().hex[:12],
            ticker       = ticker,
            strike       = strike,
            front_expiry = f_exp,
            back_expiry  = b_exp,
            front_credit = f_cred,
            back_paid    = b_paid,
            contracts    = self.contracts.value(),
            notes        = self.notes.text().strip(),
            opened_on    = e.opened_on if e else datetime.today().date().isoformat(),
        )
        self.accept()

    def position(self):
        return self._result
