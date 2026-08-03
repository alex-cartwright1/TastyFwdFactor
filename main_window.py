"""The application window — the controller in MVC.

It owns no business logic and no data: the tables render `QAbstractTableModel`s,
the scan runs in a `ScanWorker`, quotes arrive through `LiveFeedController`, and
every number is computed by `pricing` / `scanner` / `positions`. This class only
wires those together and translates user actions into calls on them.
"""

import os
import subprocess

from PySide6.QtCore import QSortFilterProxyModel, Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QFormLayout, QFrame, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QRadioButton, QSplitter,
    QTabWidget, QTableView, QVBoxLayout, QWidget,
)

from applog import LOG_PATH, log
from charts import PLChartWindow
from config import (
    KEYRING_AVAILABLE, load_credentials, load_positions_raw, load_settings,
    save_credentials, save_positions_raw,
)
from data_models import Position
from dialogs import FiltersDialog, PositionDialog
from live import LiveFeedController
from models import SORT_ROLE, PositionsModel, ScanResultsModel
from positions import update_position_metrics
from pricing import calc_implied_vol, forward_iv, solve_calendar
from workers import ApiSession, PositionResolveWorker, ScanWorker

LIVE_TOP_N = 50          # result rows kept streamed after a scan
MAX_LOG_LINES = 5000     # debug pane ring buffer


class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Calendar Spread Edge Screener (Tastytrade)")
        self.resize(1520, 880)

        self.settings = load_settings()
        self.session  = ApiSession(self)
        self._scan_worker    = None
        self._resolve_worker = None
        self._chart_windows  = []      # keep references alive; Qt won't
        self._live_iv_method = self.settings.get('iv_method', 'Midpoint')

        self.results_model = ScanResultsModel(self)
        self.positions_model = PositionsModel(self)
        self.positions_model.set_rows(
            [Position.from_dict(d) for d in load_positions_raw()])

        self.results_feed = LiveFeedController(self.session, "results", parent=self)
        self.positions_feed = LiveFeedController(self.session, "positions", parent=self)

        self._build_ui()
        self._connect_signals()

        log.info("Calendar Spread Scanner started")
        log.info(f"Log file: {LOG_PATH}")
        if not KEYRING_AVAILABLE:
            log.warning("'keyring' is not installed — credentials will fall back "
                        "to plaintext JSON")

        # Auto-start the positions stream once the window is up, so the first
        # paint isn't waiting on a network round trip.
        if self.positions_model.rows and self._credentials_present():
            QTimer.singleShot(800, self._kick_positions_live)

    # ── UI construction ──────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_sidebar())

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_results_tab(), "Results")
        self.tabs.addTab(self._build_positions_tab(), "Positions")
        self.tabs.addTab(self._build_debug_tab(), "Debug Log")
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([280, 1240])

        layout.addWidget(splitter)
        self.setCentralWidget(central)

    def _build_sidebar(self):
        panel = QWidget()
        panel.setMinimumWidth(260)
        panel.setMaximumWidth(360)
        box = QVBoxLayout(panel)

        title = QLabel("Tastytrade OAuth")
        title.setStyleSheet("font-weight: 600;")
        box.addWidget(title)

        saved_secret, saved_refresh = load_credentials()
        form = QFormLayout()
        self.client_secret = QLineEdit(saved_secret)
        self.client_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.refresh_token = QLineEdit(saved_refresh)
        self.refresh_token.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Client Secret:", self.client_secret)
        form.addRow("Refresh Token:", self.refresh_token)
        box.addLayout(form)

        if KEYRING_AVAILABLE:
            msg, colour = ("Stored in the OS keyring (Secret Service / Keychain / "
                           "Credential Manager).", "#888")
        else:
            msg, colour = ("WARNING: 'keyring' is not installed — credentials are "
                           "stored in plaintext JSON. Run: pip install keyring", "#a55")
        note = QLabel(msg)
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {colour}; font-size: 10px;")
        box.addWidget(note)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        box.addWidget(line)

        self.filters_btn = QPushButton("Filters & Scan Settings…")
        self.filters_btn.clicked.connect(self._open_filters)
        box.addWidget(self.filters_btn)

        self.run_btn = QPushButton("Run Scanner")
        self.run_btn.clicked.connect(self._toggle_scan)
        box.addWidget(self.run_btn)

        self.status_label = QLabel("Ready")
        self.status_label.setWordWrap(True)
        box.addWidget(self.status_label)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        box.addWidget(self.progress)
        box.addStretch(1)
        return panel

    def _build_results_tab(self):
        tab = QWidget()
        box = QVBoxLayout(tab)

        self.results_proxy = QSortFilterProxyModel(self)
        self.results_proxy.setSourceModel(self.results_model)
        self.results_proxy.setSortRole(SORT_ROLE)
        self.results_proxy.setDynamicSortFilter(True)

        self.results_view = self._make_table(self.results_proxy, self.results_model)
        fwd_col = next(i for i, c in enumerate(ScanResultsModel.COLUMNS)
                       if c.header == "Fwd Factor")
        self.results_view.sortByColumn(fwd_col, Qt.SortOrder.DescendingOrder)
        box.addWidget(self.results_view, stretch=1)

        self.live_resort = QCheckBox("Re-sort live as quotes update")
        self.live_resort.setChecked(True)
        self.live_resort.toggled.connect(self.results_proxy.setDynamicSortFilter)
        box.addWidget(self.live_resort)

        box.addWidget(self._build_trade_panel())
        return tab

    def _build_trade_panel(self):
        group = QGroupBox("Analyse Selected Trade")
        outer = QVBoxLayout(group)

        self.selected_label = QLabel("Select a row above to analyse a trade")
        self.selected_label.setStyleSheet("color: #888;")
        outer.addWidget(self.selected_label)

        # Three linked fields: editing either leg recomputes Net Debit; editing
        # Net Debit back-solves whichever leg is *not* locked.
        row = QHBoxLayout()
        self.back_paid_edit    = QLineEdit()
        self.front_credit_edit = QLineEdit()
        self.net_debit_edit    = QLineEdit()
        for label, edit in (("Back Leg Paid ($):", self.back_paid_edit),
                            ("Front Leg Credit ($):", self.front_credit_edit),
                            ("Net Debit ($):", self.net_debit_edit)):
            edit.setFixedWidth(90)
            row.addWidget(QLabel(label))
            row.addWidget(edit)
            row.addSpacing(12)

        self.calc_btn = QPushButton("Calculate && chart")
        self.calc_btn.clicked.connect(self._calc_real_fwd_factor)
        row.addWidget(self.calc_btn)
        row.addStretch(1)
        outer.addLayout(row)

        lock_row = QHBoxLayout()
        lock_row.addWidget(QLabel("When changing Net Debit, lock:"))
        self.lock_front = QRadioButton("Front credit")
        self.lock_back  = QRadioButton("Back paid")
        self.lock_front.setChecked(True)
        self._lock_group = QButtonGroup(self)
        self._lock_group.addButton(self.lock_front)
        self._lock_group.addButton(self.lock_back)
        lock_row.addWidget(self.lock_front)
        lock_row.addWidget(self.lock_back)
        lock_row.addSpacing(16)

        self.real_fwd_label = QLabel("")
        self.real_fwd_label.setStyleSheet("color: #1f6feb;")
        lock_row.addWidget(self.real_fwd_label)
        lock_row.addStretch(1)
        outer.addLayout(lock_row)
        return group

    def _build_positions_tab(self):
        tab = QWidget()
        box = QVBoxLayout(tab)

        bar = QHBoxLayout()
        for text, slot in (("Add Position…", self._pos_add),
                           ("Edit", self._pos_edit),
                           ("Remove", self._pos_remove),
                           ("Chart P/L", self._pos_chart)):
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            bar.addWidget(btn)
        reconnect = QPushButton("Reconnect Live")
        reconnect.clicked.connect(self._pos_reconnect_live)
        bar.addSpacing(16)
        bar.addWidget(reconnect)
        bar.addStretch(1)

        self.pos_status = QLabel("No live connection")
        self.pos_status.setStyleSheet("color: #888;")
        bar.addWidget(self.pos_status)
        box.addLayout(bar)

        self.positions_proxy = QSortFilterProxyModel(self)
        self.positions_proxy.setSourceModel(self.positions_model)
        self.positions_proxy.setSortRole(SORT_ROLE)
        self.positions_proxy.setDynamicSortFilter(True)
        self.positions_view = self._make_table(self.positions_proxy,
                                               self.positions_model)
        self.positions_view.doubleClicked.connect(lambda _: self._pos_chart())
        box.addWidget(self.positions_view, stretch=1)

        total_row = QHBoxLayout()
        label = QLabel("Total P/L:")
        label.setStyleSheet("font-weight: 600;")
        total_row.addWidget(label)
        self.pos_total = QLabel("$0.00")
        self.pos_total.setStyleSheet("font-weight: 600; color: #1f6feb;")
        total_row.addWidget(self.pos_total)
        hint = QLabel("(per current mid + live underlying)")
        hint.setStyleSheet("color: #888; font-size: 10px;")
        total_row.addWidget(hint)
        total_row.addStretch(1)
        box.addLayout(total_row)
        return tab

    def _build_debug_tab(self):
        tab = QWidget()
        box = QVBoxLayout(tab)

        bar = QHBoxLayout()
        clear = QPushButton("Clear")
        clear.clicked.connect(lambda: self.debug_view.clear())
        open_btn = QPushButton("Open log file")
        open_btn.clicked.connect(self._open_log_file)
        bar.addWidget(clear)
        bar.addWidget(open_btn)
        path = QLabel(str(LOG_PATH))
        path.setStyleSheet("color: #888;")
        bar.addWidget(path)
        bar.addStretch(1)
        box.addLayout(bar)

        self.debug_view = QPlainTextEdit()
        self.debug_view.setReadOnly(True)
        self.debug_view.setMaximumBlockCount(MAX_LOG_LINES)
        self.debug_view.setFont(QFont("monospace", 9))
        self.debug_view.setStyleSheet(
            "background-color: #1e1e1e; color: #d4d4d4;")
        box.addWidget(self.debug_view)
        return tab

    @staticmethod
    def _make_table(proxy, source_model):
        view = QTableView()
        view.setModel(proxy)
        view.setSortingEnabled(True)
        view.setAlternatingRowColors(True)
        view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        view.verticalHeader().setVisible(False)
        view.verticalHeader().setDefaultSectionSize(22)
        header = view.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        for i, col in enumerate(source_model.COLUMNS):
            view.setColumnWidth(i, col.width)
        return view

    def _connect_signals(self):
        log.messageLogged.connect(self._append_log)

        self.results_view.selectionModel().selectionChanged.connect(
            self._on_result_selected)
        self.back_paid_edit.textEdited.connect(self._on_leg_edited)
        self.front_credit_edit.textEdited.connect(self._on_leg_edited)
        self.net_debit_edit.textEdited.connect(self._on_net_debit_edited)

        self.results_feed.rowsDirty.connect(self._refresh_result_rows)
        self.results_feed.statusChanged.connect(self._set_live_status)
        self.positions_feed.rowsDirty.connect(self._refresh_position_rows)
        self.positions_feed.statusChanged.connect(self.pos_status.setText)

    # ── Debug log ────────────────────────────────────────────────────────────

    def _append_log(self, level, message):
        self.debug_view.appendPlainText(f"[{level}] {message}")

    def _open_log_file(self):
        try:
            if hasattr(os, 'startfile'):
                os.startfile(str(LOG_PATH))
            else:
                subprocess.Popen(['xdg-open', str(LOG_PATH)],
                                 stderr=subprocess.DEVNULL)
        except Exception:
            QMessageBox.information(self, "Log File", f"Log location:\n{LOG_PATH}")

    # ── Settings ─────────────────────────────────────────────────────────────

    def _open_filters(self):
        dialog = FiltersDialog(self.settings, self)
        if dialog.exec():
            self.settings = dialog.settings()
            log.info(f"Filters updated: {self.settings}")

    def _credentials_present(self):
        return bool(self.client_secret.text().strip()
                    and self.refresh_token.text().strip())

    # ── Scan ─────────────────────────────────────────────────────────────────

    def _toggle_scan(self):
        if self._scan_worker is not None and self._scan_worker.isRunning():
            self._scan_worker.cancel()
            self.run_btn.setEnabled(False)
            self.status_label.setText("Cancelling…")
            return
        self._start_scan()

    def _start_scan(self):
        secret  = self.client_secret.text().strip()
        refresh = self.refresh_token.text().strip()
        if not (secret and refresh):
            QMessageBox.warning(
                self, "Missing Credentials",
                "Enter your OAuth client_secret and refresh_token.\n\n"
                "Get them from my.tastytrade.com → My Profile → Manage → "
                "OAuth Applications → Create Grant.")
            return

        save_credentials(secret, refresh)
        self.session.set_credentials(secret, refresh)
        self._live_iv_method = self.settings.get('iv_method', 'Midpoint')

        # Tear down the previous stream before the table is rebuilt.
        self.results_feed.stop()
        self.results_model.set_rows([])

        worker = ScanWorker(self.session, self.settings, parent=self)
        worker.statusChanged.connect(self.status_label.setText)
        worker.progressChanged.connect(self._set_progress)
        worker.indeterminateChanged.connect(self._set_indeterminate)
        worker.scanFinished.connect(self._on_scan_finished)
        worker.scanFailed.connect(self._on_scan_failed)
        worker.finished.connect(self._on_scan_thread_done)
        self._scan_worker = worker

        self.run_btn.setText("Cancel Scan")
        self.filters_btn.setEnabled(False)
        worker.start()

    def _set_progress(self, value):
        if self.progress.maximum() == 0:
            self.progress.setRange(0, 100)
        self.progress.setValue(value)

    def _set_indeterminate(self, active):
        """A 0..0 range makes QProgressBar render its busy animation."""
        if active:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)

    def _on_scan_thread_done(self):
        self._scan_worker = None
        self.run_btn.setText("Run Scanner")
        self.run_btn.setEnabled(True)
        self.filters_btn.setEnabled(True)
        self._set_indeterminate(False)

    def _on_scan_failed(self, message):
        self.status_label.setText(message)
        self.progress.setValue(0)
        log.warning(f"Scan ended: {message}")

    def _on_scan_finished(self, results):
        self.progress.setValue(100)
        if not results:
            self.status_label.setText("Scan complete — no setups found.")
            QMessageBox.information(
                self, "No Results",
                "No valid calendar setups found.\n\n"
                "Check the Debug Log tab for details.")
            return

        self.results_model.set_rows(results)
        self.status_label.setText(f"Scan complete — {len(results)} setup(s) found.")
        self._start_results_live(results[:LIVE_TOP_N])

    # ── Results live stream ──────────────────────────────────────────────────

    def _start_results_live(self, top_rows):
        """Stream the top-N rows. Keys are *source* row indices, which are stable
        under proxy sorting."""
        sym_map, equity = {}, set()
        for row, r in enumerate(top_rows):
            for sym in (r.ticker, r.front_sym, r.back_sym):
                if sym:
                    sym_map.setdefault(sym, set()).add(row)
            equity.add(r.ticker)
        if sym_map:
            self.results_feed.start(sym_map, equity)

    def _set_live_status(self, message):
        base = self.status_label.text().split("  •  ")[0]
        self.status_label.setText(f"{base}  •  {message}")

    def _refresh_result_rows(self, rows):
        feed = self.results_feed
        iv_method = self._live_iv_method
        for row in rows:
            r = self.results_model.row_at(row)
            if r is None:
                continue
            eq = feed.quote(r.ticker)
            price = eq.price if eq.price > 0 else r.price
            fq, bq = feed.quote(r.front_sym), feed.quote(r.back_sym)
            f_bid = fq.bid or r.f_bid
            f_ask = fq.ask or r.f_ask
            b_bid = bq.bid or r.b_bid
            b_ask = bq.ask or r.b_ask

            ivs = solve_calendar(price, r.strike, r.front_dte, r.back_dte,
                                 f_bid, f_ask, b_bid, b_ask, iv_method)
            if ivs is None:
                continue
            self.results_model.apply_live_update(
                row, price, f_bid, f_ask, b_bid, b_ask, ivs)

    # ── Trade analysis panel ─────────────────────────────────────────────────

    def _selected_result(self):
        indexes = self.results_view.selectionModel().selectedRows()
        if not indexes:
            return None
        source = self.results_proxy.mapToSource(indexes[0])
        return self.results_model.row_at(source.row())

    def _on_result_selected(self, *_):
        r = self._selected_result()
        if r is None:
            return
        self.selected_label.setText(
            f"{r.ticker}  |  Price: ${r.price:.2f}  |  Strike: ${r.strike:.2f}  |  "
            f"F-DTE: {r.front_dte}  |  B-DTE: {r.back_dte}")
        self.real_fwd_label.setText("")

    @staticmethod
    def _try_float(text):
        try:
            return float(text)
        except (TypeError, ValueError):
            return None

    def _on_leg_edited(self, _text):
        """Either leg was typed into — recompute Net Debit. `textEdited` only
        fires for user input, so the programmatic setText below can't loop."""
        b = self._try_float(self.back_paid_edit.text())
        f = self._try_float(self.front_credit_edit.text())
        if b is None or f is None:
            return
        self.net_debit_edit.setText(f"{b - f:.2f}")

    def _on_net_debit_edited(self, _text):
        nd = self._try_float(self.net_debit_edit.text())
        if nd is None:
            return
        if self.lock_front.isChecked():
            f = self._try_float(self.front_credit_edit.text())
            if f is not None:
                self.back_paid_edit.setText(f"{nd + f:.2f}")
        else:
            b = self._try_float(self.back_paid_edit.text())
            if b is not None:
                self.front_credit_edit.setText(f"{b - nd:.2f}")

    def _calc_real_fwd_factor(self):
        r = self._selected_result()
        if r is None:
            QMessageBox.warning(self, "No Selection", "Select a trade row first.")
            return
        back_price   = self._try_float(self.back_paid_edit.text())
        front_credit = self._try_float(self.front_credit_edit.text())
        if back_price is None or front_credit is None:
            QMessageBox.warning(self, "Invalid Input",
                                "Enter numeric values for both prices.")
            return
        if back_price <= 0 or front_credit <= 0:
            QMessageBox.warning(self, "Invalid Input", "Both prices must be positive.")
            return

        t1 = r.front_dte / 365.0
        t2 = r.back_dte / 365.0
        f_iv = calc_implied_vol(front_credit, r.price, r.strike, t1)
        b_iv = calc_implied_vol(back_price,   r.price, r.strike, t2)
        fwd = forward_iv(f_iv, b_iv, t1, t2)
        if fwd is None:
            self.real_fwd_label.setText(
                "Cannot compute — negative forward variance")
            return

        ff = (f_iv - fwd) / fwd
        self.real_fwd_label.setText(
            f"Real Fwd Factor: {ff * 100:.2f}%   "
            f"(F-IV: {f_iv * 100:.1f}%  B-IV: {b_iv * 100:.1f}%  "
            f"Fwd-IV: {fwd * 100:.1f}%)")

        # Chart with the back IV solved from the actual fill, so the reference
        # line reflects what the trader would be paying.
        self._open_chart(
            ticker=r.ticker, price=r.price, strike=r.strike,
            front_dte=r.front_dte, back_dte=r.back_dte,
            back_paid=back_price, front_credit=front_credit,
            current_back_iv=b_iv, fwd_iv=fwd)

    def _open_chart(self, **kwargs):
        try:
            window = PLChartWindow(parent=self, **kwargs)
        except Exception as exc:
            log.error(f"Failed to open P/L chart: {exc}")
            QMessageBox.critical(self, "Chart error", f"Could not open chart: {exc}")
            return
        self._chart_windows.append(window)
        window.destroyed.connect(
            lambda *_, w=window: self._chart_windows.remove(w)
            if w in self._chart_windows else None)
        window.show()

    # ── Positions ────────────────────────────────────────────────────────────

    def _selected_position(self):
        indexes = self.positions_view.selectionModel().selectedRows()
        if not indexes:
            return None, -1
        row = self.positions_proxy.mapToSource(indexes[0]).row()
        return self.positions_model.position_at(row), row

    def _save_positions(self):
        save_positions_raw([p.to_dict() for p in self.positions_model.rows])

    def _pos_add(self):
        dialog = PositionDialog(parent=self)
        if not dialog.exec():
            return
        self.positions_model.add(dialog.position())
        self._save_positions()
        self._kick_positions_live()

    def _pos_edit(self):
        existing, row = self._selected_position()
        if existing is None:
            QMessageBox.information(self, "Edit Position",
                                    "Select a position to edit.")
            return
        dialog = PositionDialog(existing=existing, parent=self)
        if not dialog.exec():
            return

        updated = dialog.position()
        # A changed ticker/strike/expiry invalidates the resolved streamer
        # symbols and every live figure derived from them.
        identity = ('ticker', 'strike', 'front_expiry', 'back_expiry')
        if any(getattr(existing, k) != getattr(updated, k) for k in identity):
            updated.clear_live_state()
            updated.opened_fwd_factor = None
        else:
            for name in ('front_sym', 'back_sym', 'resolve_error',
                         'underlying_price', 'f_bid', 'f_ask', 'b_bid', 'b_ask',
                         'front_iv', 'back_iv', 'fwd_iv', 'fwd_factor',
                         'cur_debit', 'cur_pl', 'opened_underlying_price',
                         'opened_front_iv', 'opened_back_iv', 'opened_fwd_iv',
                         'opened_fwd_factor'):
                setattr(updated, name, getattr(existing, name))

        self.positions_model.rows[row] = updated
        self.positions_model.emit_row_changed(row)
        self._save_positions()
        self._kick_positions_live()

    def _pos_remove(self):
        position, row = self._selected_position()
        if position is None:
            return
        confirm = QMessageBox.question(
            self, "Remove Position",
            f"Remove {position.ticker} {position.strike:.2f} "
            f"{position.front_expiry}/{position.back_expiry}?")
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self.positions_model.remove(row)
        self._save_positions()
        self._update_positions_total()
        # Its symbols stay subscribed until the next reconnect — DXLink keeps
        # streaming them harmlessly and nothing maps them to a row any more.
        self._kick_positions_live()

    def _pos_chart(self):
        position, _ = self._selected_position()
        if position is None:
            QMessageBox.information(self, "Chart P/L", "Select a position to chart.")
            return
        price = position.chart_price
        if price <= 0:
            QMessageBox.warning(
                self, "No live price",
                f"No live underlying price yet for {position.ticker}. "
                "Wait for the live stream to populate before charting.")
            return
        f_dte, b_dte = position.current_dtes()
        if f_dte <= 0 or b_dte <= f_dte:
            QMessageBox.warning(
                self, "Bad DTE",
                f"{position.ticker}: front DTE={f_dte}, back DTE={b_dte}. "
                "The chart needs a future front expiry and back > front.")
            return
        self._open_chart(
            ticker=position.ticker, price=price, strike=position.strike,
            front_dte=f_dte, back_dte=b_dte,
            back_paid=position.back_paid, front_credit=position.front_credit,
            current_back_iv=position.back_iv or 0.0,
            fwd_iv=position.fwd_iv or 0.0)

    # ── Positions live stream ────────────────────────────────────────────────

    def _kick_positions_live(self):
        """Resolve any position lacking streamer symbols, then (re)subscribe.

        Idempotent: an existing connection is reused and only new symbols are
        added.
        """
        positions = self.positions_model.rows
        if not positions:
            return
        if not self._credentials_present():
            self.pos_status.setText("Live: credentials not set")
            return
        self.session.set_credentials(self.client_secret.text().strip(),
                                     self.refresh_token.text().strip())

        unresolved = [p for p in positions if not p.front_sym and not p.resolve_error]
        if unresolved:
            if self._resolve_worker is not None and self._resolve_worker.isRunning():
                return
            worker = PositionResolveWorker(self.session, unresolved, parent=self)
            worker.statusChanged.connect(self.pos_status.setText)
            worker.positionResolved.connect(self._on_position_resolved)
            worker.resolveFinished.connect(self._subscribe_positions)
            worker.resolveFailed.connect(
                lambda msg: self.pos_status.setText(f"Live: {msg}"))
            worker.finished.connect(self._on_resolve_thread_done)
            self._resolve_worker = worker
            worker.start()
        else:
            self._subscribe_positions()

    def _on_resolve_thread_done(self):
        self._resolve_worker = None

    def _on_position_resolved(self, pid, front_sym, back_sym, error):
        row = self.positions_model.index_of_id(pid)
        if row is None:
            return
        p = self.positions_model.position_at(row)
        p.front_sym = front_sym
        p.back_sym  = back_sym
        p.resolve_error = error or None
        self.positions_model.emit_row_changed(row)

    def _subscribe_positions(self):
        sym_map, equity = {}, set()
        for p in self.positions_model.rows:
            if p.resolve_error:
                continue
            for sym in (p.ticker, p.front_sym, p.back_sym):
                if sym:
                    sym_map.setdefault(sym, set()).add(p.id)
            equity.add(p.ticker)
        if not sym_map:
            self.pos_status.setText("Live: nothing to stream")
            return
        self.positions_feed.start(sym_map, equity)
        n = sum(1 for p in self.positions_model.rows if not p.resolve_error)
        self.pos_status.setText(f"Live: streaming {n} position(s)")

    def _pos_reconnect_live(self):
        self.positions_feed.stop()
        # Drop cached streamer symbols so they're re-resolved against a fresh chain.
        for p in self.positions_model.rows:
            p.front_sym = p.back_sym = ''
            p.resolve_error = None
        self.session.reset()
        self._kick_positions_live()

    def _refresh_position_rows(self, pids):
        feed = self.positions_feed
        changed = False
        for pid in pids:
            row = self.positions_model.index_of_id(pid)
            if row is None:
                continue
            p = self.positions_model.position_at(row)
            fq, bq = feed.quote(p.front_sym), feed.quote(p.back_sym)
            self.positions_model.begin_leg_flash(
                row, {'f_bid': fq.bid, 'f_ask': fq.ask,
                      'b_bid': bq.bid, 'b_ask': bq.ask})
            try:
                if update_position_metrics(p, feed.quote(p.ticker), fq, bq,
                                           self._live_iv_method):
                    self.positions_model.emit_row_changed(row)
                    changed = True
            except Exception as exc:
                log.debug(f"position live refresh {p.ticker}: {exc}")

        if changed:
            self._update_positions_total()
            # Persists any newly-snapshotted opened_* fields.
            self._save_positions()

    def _update_positions_total(self):
        self.pos_total.setText(f"${self.positions_model.total_pl():.2f}")

    # ── Shutdown ─────────────────────────────────────────────────────────────

    def shutdown(self):
        """Stop every worker and socket thread.

        Idempotent, and wired to both `closeEvent` and `QApplication.aboutToQuit`
        — a QThread still inside `run_forever` would otherwise keep the process
        alive after the event loop ends.
        """
        if self._scan_worker is not None and self._scan_worker.isRunning():
            self._scan_worker.cancel()
            self._scan_worker.wait(3000)
        if self._resolve_worker is not None and self._resolve_worker.isRunning():
            self._resolve_worker.wait(3000)
        self.results_feed.stop()
        self.positions_feed.stop()

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)
