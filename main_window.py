"""The application window — the controller in MVC.

It owns no business logic and no data: the tables render `QAbstractTableModel`s,
the scan runs in a `ScanWorker`, quotes arrive through `LiveFeedController`, and
every number is computed by `pricing` / `scanner` / `positions`. This class only
wires those together and translates user actions into calls on them.

Layout is a top status bar (New York clock + market open/closed light) above a
collapsible left rail (Scanner / Positions / Settings / Logs) driving a
`QStackedWidget`. Each view carries its own action bar at the top, so the
controls on screen are always the ones that apply to what's on screen.

The window is constructed with an already-authenticated `ApiSession` handed over
by `LoginWindow`, which is why nothing here asks for credentials.
"""

import os
import subprocess
import sys

from PySide6.QtCore import (
    QEasingCurve, QParallelAnimationGroup, QPropertyAnimation, QSize,
    QSortFilterProxyModel, Qt, QTimer, Signal,
)
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QCompleter, QFrame, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QRadioButton, QSizePolicy, QStackedWidget,
    QTableView, QVBoxLayout, QWidget,
)

from api import QuoteStream
from applog import LOG_PATH, log
from charts import PLChartWindow, SetupDetailWindow
from config import KEYRING_AVAILABLE, clear_chain_cache, clear_ticker_cache, \
    load_positions_raw, load_settings, save_positions_raw
from data_models import Position
from delegates import SignedFlashDelegate, SortHeaderView
from dialogs import FiltersDialog, PositionDialog
from live import LiveFeedController
from market_clock import OPEN, format_nyc, market_status
from models import SORT_ROLE, PositionsModel, ScanResultsModel
from positions import update_position_metrics
from pricing import solve_calendar, solve_fills
from scanner import SCAN_CONSUMER, load_watchlist
from theme import T, Spinner, StatusDot, make_icon, separator, shadow
from workers import (
    ApiSession, MarketCalendarWorker, PositionResolveWorker, ScanWorker,
)

# Every result row streams. Anything less silently mixes live rows with rows
# frozen at scan time, which makes sorting by a live column (Fwd Factor above
# all) interleave fresh and stale values. The cap is a blow-up guard, not a
# policy — it costs 3 symbols per row — and truncating it is logged rather than
# done quietly.
LIVE_MAX_ROWS  = 300
MAX_LOG_LINES  = 5000    # debug pane ring buffer
RAIL_EXPANDED  = 208
RAIL_COLLAPSED = 56

# The holiday calendar changes at most once a year, so re-fetching daily is
# already generous; the open/closed decision itself is recomputed locally every
# second and costs nothing.
CALENDAR_REFRESH_MS = 24 * 60 * 60 * 1000

# Column the ticker search filters on.
_TICKER_COL = next(i for i, c in enumerate(ScanResultsModel.COLUMNS)
                   if c.header == "Ticker")


class NavRail(QWidget):
    """Collapsible left navigation. Emits the index of the selected view."""

    selected = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setFixedWidth(RAIL_EXPANDED)
        self._expanded = True
        self._buttons = []

        box = QVBoxLayout(self)
        box.setContentsMargins(10, 14, 10, 14)
        box.setSpacing(4)

        header = QHBoxLayout()
        header.setSpacing(10)
        self._logo = QLabel()
        self._logo.setPixmap(make_icon('logo', T.ACCENT, 22).pixmap(22, 22))
        self._logo.setFixedWidth(24)
        header.addWidget(self._logo)
        self._wordmark = QLabel("Edge Screener")
        self._wordmark.setStyleSheet(
            f"font-weight: 600; font-size: 13px; color: {T.TEXT};")
        header.addWidget(self._wordmark)
        header.addStretch(1)
        box.addLayout(header)
        box.addSpacing(14)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        for index, (icon, label) in enumerate((
            ('scanner',   "Scanner"),
            ('positions', "Positions"),
            ('settings',  "Settings"),
            ('logs',      "Logs"),
        )):
            button = QPushButton(f"  {label}")
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.setIcon(make_icon(icon, T.TEXT_DIM, 18))
            button.setIconSize(QSize(18, 18))
            button.setMinimumHeight(38)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip(label)
            button.clicked.connect(lambda _c, i=index: self.selected.emit(i))
            self._group.addButton(button, index)
            self._buttons.append((button, label))
            box.addWidget(button)

        box.addStretch(1)
        box.addWidget(separator())
        box.addSpacing(6)

        self._toggle = QPushButton("  Collapse")
        self._toggle.setObjectName("NavButton")
        self._toggle.setIcon(make_icon('menu', T.TEXT_MUTED, 18))
        self._toggle.setIconSize(QSize(18, 18))
        self._toggle.setMinimumHeight(34)
        self._toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._toggle.clicked.connect(self.toggle)
        box.addWidget(self._toggle)

        # Both bounds have to move together, otherwise setFixedWidth's min==max
        # constraint fights the animation and the rail snaps instead of sliding.
        self._animation = QParallelAnimationGroup(self)
        for prop in (b"minimumWidth", b"maximumWidth"):
            animation = QPropertyAnimation(self, prop, self)
            animation.setDuration(170)
            animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
            self._animation.addAnimation(animation)

    def set_current(self, index):
        button = self._group.button(index)
        if button:
            button.setChecked(True)

    def toggle(self):
        self._expanded = not self._expanded
        width = RAIL_EXPANDED if self._expanded else RAIL_COLLAPSED

        for button, label in self._buttons:
            button.setText(f"  {label}" if self._expanded else "")
        self._toggle.setText("  Collapse" if self._expanded else "")
        self._wordmark.setVisible(self._expanded)

        self._animation.stop()
        start = self.width()
        for i in range(self._animation.animationCount()):
            animation = self._animation.animationAt(i)
            animation.setStartValue(start)
            animation.setEndValue(width)
        self._animation.start()


class ViewHeader(QWidget):
    """Title + subtitle + a right-aligned slot for that view's actions."""

    def __init__(self, title, subtitle, parent=None):
        super().__init__(parent)
        box = QHBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(12)

        text = QVBoxLayout()
        text.setSpacing(2)
        heading = QLabel(title)
        heading.setProperty("role", "title")
        text.addWidget(heading)
        self.subtitle = QLabel(subtitle)
        self.subtitle.setProperty("role", "subtitle")
        text.addWidget(self.subtitle)
        box.addLayout(text)
        box.addStretch(1)

        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        box.addLayout(self.actions)


def action_bar():
    """A rounded, shadowed strip that holds a view's controls."""
    bar = QFrame()
    bar.setObjectName("ActionBar")
    layout = QHBoxLayout(bar)
    layout.setContentsMargins(12, 10, 12, 10)
    layout.setSpacing(8)
    shadow(bar, blur=20, dy=3, alpha=70)
    return bar, layout


class MarketStatusBar(QWidget):
    """Top chrome: the New York wall clock and the market open/closed light.

    Owns the one-second tick itself. The status decision is a pure function of
    the clock plus a holiday calendar (`market_clock.market_status`), so this
    never touches the network — the calendar is pushed in from a worker whenever
    one lands, and until then the light already reads correctly for weekends and
    regular trading hours.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("TopBar")
        self._calendar = None
        self._state    = None      # last painted state; guards needless repaints

        row = QHBoxLayout(self)
        row.setContentsMargins(16, 8, 16, 8)
        row.setSpacing(10)

        self.title = QLabel("TastyFwdFactor")
        self.title.setObjectName("TopBarTitle")
        row.addWidget(self.title)
        row.addStretch(1)

        self.clock = QLabel()
        self.clock.setObjectName("TopBarClock")
        row.addWidget(self.clock)

        self.dot = StatusDot(12, T.MARKET_CLOSED, self)
        row.addWidget(self.dot)

        self.state_label = QLabel()
        self.state_label.setObjectName("TopBarState")
        row.addWidget(self.state_label)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._tick()

    def set_calendar(self, calendar):
        self._calendar = calendar
        self._state = None         # force a repaint: holidays may change the light
        self._tick()

    def _tick(self):
        self.clock.setText(format_nyc())

        state, tooltip = market_status(calendar=self._calendar)
        if state == self._state:
            return
        self._state = state

        is_open = state == OPEN
        self.dot.set_colour(T.MARKET_OPEN if is_open else T.MARKET_CLOSED)
        self.state_label.setText("OPEN" if is_open else "CLOSED")
        self.state_label.setProperty("tone", "open" if is_open else "closed")
        # A dynamic property only takes effect after a re-polish.
        self.state_label.style().unpolish(self.state_label)
        self.state_label.style().polish(self.state_label)

        for widget in (self.dot, self.state_label, self.clock, self):
            widget.setToolTip(tooltip)


class MainWindow(QMainWindow):

    signOutRequested = Signal()

    def __init__(self, session: ApiSession):
        super().__init__()
        self.setWindowTitle("TastyFwdFactor")
        self.resize(1580, 920)
        self.setMinimumSize(1100, 700)

        self.settings = load_settings()
        self.session  = session
        # Must be set before anything triggers `ensure()`, since the TTL decides
        # what the on-disk chain cache seeds at construction.
        self.session.set_chain_ttl(self._chain_cache_ttl_secs())
        self._scan_worker    = None
        self._resolve_worker = None
        self._calendar_worker = None
        self._scan_targets   = None    # symbols of the in-flight targeted search
        self._chart_windows  = []      # keep references alive; Qt won't
        self._live_iv_method = self.settings.get('iv_method', 'Midpoint')
        self._signing_out    = False

        self.results_model = ScanResultsModel(self)
        self.positions_model = PositionsModel(self)
        self.positions_model.set_rows(
            [Position.from_dict(d) for d in load_positions_raw()])

        # One DXLink connection for the whole app: the scan's Phase 3, the
        # results table and the positions table are all consumers of it, so the
        # handshake happens once per session and a finished scan hands its
        # already-streaming symbols straight to the table.
        self.quote_stream = QuoteStream(self.session, parent=self)
        self.results_feed = LiveFeedController(self.quote_stream, "results", parent=self)
        self.positions_feed = LiveFeedController(self.quote_stream, "positions", parent=self)

        self._build_ui()
        self._connect_signals()

        log.info("TastyFwdFactor started")
        log.info(f"Log file: {LOG_PATH}")
        if not KEYRING_AVAILABLE:
            log.warning("'keyring' is not installed — credentials will fall back "
                        "to plaintext JSON")

        self._update_positions_total()
        # Auto-start the positions stream once the window is up, so the first
        # paint isn't waiting on a network round trip.
        if self.positions_model.rows:
            QTimer.singleShot(600, self._kick_positions_live)

        # Same reasoning for the holiday calendar: the light is already painted
        # from the local clock, so this only ever refines it.
        self._calendar_timer = QTimer(self)
        self._calendar_timer.setInterval(CALENDAR_REFRESH_MS)
        self._calendar_timer.timeout.connect(self._fetch_market_calendar)
        self._calendar_timer.start()
        QTimer.singleShot(300, self._fetch_market_calendar)

    # ── UI construction ──────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        shell = QVBoxLayout(central)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        self.status_bar = MarketStatusBar()
        shell.addWidget(self.status_bar)

        body = QWidget()
        # A bare QWidget container inherits the sheet's window background, which
        # is what we want here — but say so rather than relying on it.
        layout = QHBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.nav = NavRail()
        self.nav.selected.connect(self._switch_view)
        layout.addWidget(self.nav)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_scanner_view())
        self.stack.addWidget(self._build_positions_view())
        self.stack.addWidget(self._build_settings_view())
        self.stack.addWidget(self._build_logs_view())
        layout.addWidget(self.stack, 1)

        shell.addWidget(body, 1)

        self.nav.set_current(0)
        self.setCentralWidget(central)

    # ── Market status ────────────────────────────────────────────────────────

    def _fetch_market_calendar(self):
        """Refresh the holiday calendar behind the header light.

        Skipped while one is already in flight — a stale calendar is harmless,
        and the header never blocks on this.
        """
        if self._calendar_worker is not None and self._calendar_worker.isRunning():
            return
        self._calendar_worker = MarketCalendarWorker(self.session, self)
        self._calendar_worker.calendarReady.connect(self._on_calendar_ready)
        self._calendar_worker.start()

    def _on_calendar_ready(self, calendar):
        if calendar is None:
            log.debug("Market calendar unavailable — status light using local "
                      "hours and weekends only")
            return
        self.status_bar.set_calendar(calendar)

    def _switch_view(self, index):
        self.stack.setCurrentIndex(index)
        self.nav.set_current(index)      # keeps the rail in step with programmatic switches

    @staticmethod
    def _page(title, subtitle):
        """A view: outer margins, header, then caller-supplied content."""
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(24, 20, 24, 20)
        box.setSpacing(14)
        header = ViewHeader(title, subtitle)
        box.addWidget(header)
        return page, box, header

    # ── Scanner view ─────────────────────────────────────────────────────────

    def _build_scanner_view(self):
        page, box, header = self._page(
            "Scanner", "Rank calendar spreads across the watchlist by forward factor")

        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("TickerSearch")
        self.search_edit.setPlaceholderText("Search ticker…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setFixedWidth(190)
        self.search_edit.setToolTip(
            "Type to filter the results table.\n"
            "Press Enter to scan this ticker on demand if it isn't in the table.")
        # Completer over the configured watchlist, so the symbol you want is one
        # or two keystrokes away out of ~5,000.
        completer = QCompleter(self._watchlist_symbols(), self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchStartsWith)
        completer.setMaxVisibleItems(12)
        self.search_edit.setCompleter(completer)
        header.actions.addWidget(self.search_edit)

        # The line edit's own clear button only appears once there is text, and
        # it is easy to miss that a stale filter is why the table looks empty
        # after a scan — so the filter also has an explicit control of its own,
        # paired with the state badge in the action bar below.
        self.search_clear_btn = QPushButton("Clear")
        self.search_clear_btn.setProperty("variant", "ghost")
        self.search_clear_btn.setToolTip(
            "Clear the ticker filter and show every scanned row")
        self.search_clear_btn.setEnabled(False)
        self.search_clear_btn.clicked.connect(self._clear_search_filter)
        header.actions.addWidget(self.search_clear_btn)

        self.search_btn = QPushButton("Search")
        self.search_btn.setToolTip("Scan just this ticker, ignoring the filters")
        self.search_btn.clicked.connect(self._search_ticker)
        header.actions.addWidget(self.search_btn)

        self.filters_btn = QPushButton("Filters")
        self.filters_btn.setIcon(make_icon('settings', T.TEXT_DIM, 16))
        self.filters_btn.clicked.connect(self._open_filters)
        header.actions.addWidget(self.filters_btn)

        self.run_btn = QPushButton("Run Scan")
        self.run_btn.setProperty("variant", "primary")
        self.run_btn.setMinimumWidth(120)
        self.run_btn.clicked.connect(self._toggle_scan)
        header.actions.addWidget(self.run_btn)

        # Action bar: live scan telemetry + view options.
        bar, bar_layout = action_bar()
        self.scan_spinner = Spinner(16, T.ACCENT)
        bar_layout.addWidget(self.scan_spinner)

        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet(f"color: {T.TEXT_DIM};")
        self.status_label.setSizePolicy(QSizePolicy.Policy.Expanding,
                                        QSizePolicy.Policy.Preferred)
        bar_layout.addWidget(self.status_label, 1)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setFixedWidth(220)
        self.progress.setTextVisible(False)
        bar_layout.addWidget(self.progress)

        bar_layout.addWidget(separator(vertical=True))
        # Says whether the table is showing everything or a filtered subset —
        # the row count alone can't distinguish "the scan found 3" from "the
        # filter is hiding 300".
        self.filter_badge = QLabel("No rows")
        self.filter_badge.setStyleSheet(self._badge_style(T.TEXT_MUTED))
        bar_layout.addWidget(self.filter_badge)

        bar_layout.addWidget(separator(vertical=True))
        self.live_badge = QLabel("Idle")
        self.live_badge.setStyleSheet(self._badge_style(T.TEXT_MUTED))
        bar_layout.addWidget(self.live_badge)

        self.live_resort = QCheckBox("Live re-sort")
        self.live_resort.setChecked(True)
        self.live_resort.setToolTip(
            "Keep the table sorted as quotes update. Turn off to stop rows "
            "moving under the cursor.")
        bar_layout.addWidget(self.live_resort)
        box.addWidget(bar)

        # Table.
        self.results_proxy = QSortFilterProxyModel(self)
        self.results_proxy.setSourceModel(self.results_model)
        self.results_proxy.setSortRole(SORT_ROLE)
        self.results_proxy.setDynamicSortFilter(True)
        # The search box filters on the Ticker column only. Source row indices
        # are unchanged by filtering, so the live feed's row keys stay valid and
        # hidden rows keep streaming — unhiding one shows a current price, not a
        # stale one.
        self.results_proxy.setFilterKeyColumn(_TICKER_COL)
        self.results_proxy.setFilterCaseSensitivity(
            Qt.CaseSensitivity.CaseInsensitive)
        self.live_resort.toggled.connect(self.results_proxy.setDynamicSortFilter)

        self.results_view = self._make_table(self.results_proxy, self.results_model)
        fwd_col = next(i for i, c in enumerate(ScanResultsModel.COLUMNS)
                       if c.header == "Fwd Factor")
        self.results_view.sortByColumn(fwd_col, Qt.SortOrder.DescendingOrder)
        self.results_view.doubleClicked.connect(self._open_setup_detail)

        # The table and its empty state swap places rather than overlaying, so a
        # "nothing here" message can never be painted on top of live rows.
        self.results_stack = QStackedWidget()
        self.results_stack.addWidget(self.results_view)
        self.results_empty = QLabel()
        self.results_empty.setObjectName("EmptyState")
        self.results_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.results_empty.setWordWrap(True)
        self.results_stack.addWidget(self.results_empty)
        box.addWidget(self.results_stack, 1)

        box.addWidget(self._build_trade_panel())
        return page

    def _chain_cache_ttl_secs(self):
        return max(int(self.settings.get('chain_cache_ttl_days', 7)), 0) * 86400

    def _watchlist_symbols(self):
        """Symbols for the search completer. A failed read is not worth blocking
        the window for — the box still accepts free text."""
        try:
            symbols, _ = load_watchlist(self.settings['csv_path'])
            return sorted(set(symbols))
        except Exception as exc:
            log.debug(f"Completer watchlist load failed: {exc}")
            return []

    @staticmethod
    def _badge_style(colour, background=None):
        return (f"color: {colour}; background-color: "
                f"{background or 'rgba(255,255,255,0.04)'}; "
                f"border-radius: 9px; padding: 3px 10px; font-size: 11px; "
                f"font-weight: 600;")

    def _build_trade_panel(self):
        panel = QFrame()
        panel.setObjectName("Card")
        shadow(panel, blur=24, dy=4, alpha=80)
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(10)

        top = QHBoxLayout()
        caption = QLabel("ANALYSE SELECTED TRADE")
        caption.setProperty("role", "section")
        top.addWidget(caption)
        top.addSpacing(12)
        self.selected_label = QLabel("Select a row to model a fill")
        self.selected_label.setStyleSheet(f"color: {T.TEXT_DIM};")
        top.addWidget(self.selected_label)
        top.addStretch(1)
        self.real_fwd_label = QLabel("")
        self.real_fwd_label.setStyleSheet(
            f"color: {T.ACCENT}; font-weight: 600;")
        top.addWidget(self.real_fwd_label)
        outer.addLayout(top)

        # Three linked fields: editing either leg recomputes Net Debit; editing
        # Net Debit back-solves whichever leg is *not* locked.
        row = QHBoxLayout()
        row.setSpacing(8)
        self.back_paid_edit    = self._money_field(row, "Back leg paid")
        self.front_credit_edit = self._money_field(row, "Front leg credit")
        self.net_debit_edit    = self._money_field(row, "Net debit")

        row.addSpacing(6)
        row.addWidget(separator(vertical=True))
        row.addSpacing(6)

        lock_caption = QLabel("Lock")
        lock_caption.setProperty("role", "hint")
        row.addWidget(lock_caption)
        self.lock_front = QRadioButton("Front")
        self.lock_back  = QRadioButton("Back")
        self.lock_front.setChecked(True)
        self.lock_front.setToolTip(
            "Which leg keeps its price when you edit the net debit")
        self.lock_back.setToolTip(self.lock_front.toolTip())
        self._lock_group = QButtonGroup(self)
        self._lock_group.addButton(self.lock_front)
        self._lock_group.addButton(self.lock_back)
        row.addWidget(self.lock_front)
        row.addWidget(self.lock_back)
        row.addStretch(1)

        self.calc_btn = QPushButton("Calculate && chart")
        self.calc_btn.setProperty("variant", "primary")
        self.calc_btn.setIcon(make_icon('chart', "#06101f", 16))
        self.calc_btn.clicked.connect(self._calc_real_fwd_factor)
        row.addWidget(self.calc_btn)
        outer.addLayout(row)
        return panel

    @staticmethod
    def _money_field(layout, label_text):
        wrapper = QVBoxLayout()
        wrapper.setSpacing(3)
        label = QLabel(label_text.upper())
        label.setProperty("role", "hint")
        wrapper.addWidget(label)
        edit = QLineEdit()
        edit.setPlaceholderText("0.00")
        edit.setFixedWidth(108)
        wrapper.addWidget(edit)
        layout.addLayout(wrapper)
        return edit

    # ── Positions view ───────────────────────────────────────────────────────

    def _build_positions_view(self):
        page, box, header = self._page(
            "Positions", "Open calendar spreads with live P/L")

        for text, slot, variant in (("Add Position", self._pos_add, "primary"),
                                    ("Edit", self._pos_edit, None),
                                    ("Remove", self._pos_remove, "danger")):
            button = QPushButton(text)
            if variant:
                button.setProperty("variant", variant)
            button.clicked.connect(slot)
            header.actions.addWidget(button)

        bar, bar_layout = action_bar()
        chart_btn = QPushButton("Chart P/L")
        chart_btn.setIcon(make_icon('chart', T.TEXT_DIM, 16))
        chart_btn.clicked.connect(self._pos_chart)
        bar_layout.addWidget(chart_btn)

        reconnect = QPushButton("Reconnect live")
        reconnect.clicked.connect(self._pos_reconnect_live)
        bar_layout.addWidget(reconnect)

        bar_layout.addWidget(separator(vertical=True))
        self.pos_status = QLabel("No live connection")
        self.pos_status.setStyleSheet(f"color: {T.TEXT_DIM};")
        bar_layout.addWidget(self.pos_status)
        bar_layout.addStretch(1)

        total_caption = QLabel("TOTAL P/L")
        total_caption.setProperty("role", "hint")
        bar_layout.addWidget(total_caption)
        self.pos_total = QLabel("$0.00")
        self.pos_total.setProperty("role", "metric")
        bar_layout.addWidget(self.pos_total)
        box.addWidget(bar)

        self.positions_proxy = QSortFilterProxyModel(self)
        self.positions_proxy.setSourceModel(self.positions_model)
        self.positions_proxy.setSortRole(SORT_ROLE)
        self.positions_proxy.setDynamicSortFilter(True)
        self.positions_view = self._make_table(self.positions_proxy,
                                               self.positions_model)
        self.positions_view.doubleClicked.connect(lambda _: self._pos_chart())
        box.addWidget(self.positions_view, 1)

        empty_hint = QLabel(
            "Positions are matched against the live option chain on connect. "
            "Expiry and strike must exist in the chain.")
        empty_hint.setProperty("role", "hint")
        box.addWidget(empty_hint)
        return page

    # ── Settings view ────────────────────────────────────────────────────────

    def _build_settings_view(self):
        page, box, header = self._page(
            "Settings", "Session, scan parameters and cached data")

        card, layout = self._settings_card(
            "SESSION",
            "Signed in to Tastytrade production with a read-only OAuth grant. "
            f"Credentials are stored in "
            f"{'the OS keyring' if KEYRING_AVAILABLE else 'a mode-600 JSON file'}.")
        sign_out = QPushButton("Sign out")
        sign_out.setProperty("variant", "danger")
        sign_out.clicked.connect(self._sign_out)
        layout.addWidget(sign_out, 0, Qt.AlignmentFlag.AlignLeft)
        box.addWidget(card)

        card, layout = self._settings_card(
            "SCAN PARAMETERS",
            "Target DTEs, IV method, price/market-cap floors and the "
            "earnings/dividend exclusions used by the scanner.")
        self.settings_summary = QLabel()
        self.settings_summary.setProperty("role", "hint")
        self.settings_summary.setWordWrap(True)
        layout.addWidget(self.settings_summary)
        open_filters = QPushButton("Open filters && scan settings")
        open_filters.clicked.connect(self._open_filters)
        layout.addWidget(open_filters, 0, Qt.AlignmentFlag.AlignLeft)
        box.addWidget(card)

        card, layout = self._settings_card(
            "TICKER DATA CACHE",
            "Earnings, market cap and ex-dividend dates from Tastytrade market "
            "metrics are cached on disk. Clearing forces a full refetch on the "
            "next scan.")
        clear_btn = QPushButton("Clear ticker cache")
        clear_btn.clicked.connect(self._clear_cache)
        layout.addWidget(clear_btn, 0, Qt.AlignmentFlag.AlignLeft)
        box.addWidget(card)

        box.addStretch(1)
        self._refresh_settings_summary()
        return page

    @staticmethod
    def _settings_card(title, description):
        card = QFrame()
        card.setObjectName("Card")
        shadow(card, blur=24, dy=4, alpha=80)
        box = QVBoxLayout(card)
        box.setContentsMargins(18, 16, 18, 16)
        box.setSpacing(8)

        heading = QLabel(title)
        heading.setProperty("role", "section")
        box.addWidget(heading)

        body = QLabel(description)
        body.setWordWrap(True)
        body.setStyleSheet(f"color: {T.TEXT_DIM};")
        box.addWidget(body)
        return card, box

    def _refresh_settings_summary(self):
        s = self.settings
        self.settings_summary.setText(
            f"Front {s['front_dte']}±{s.get('front_dte_flex', 0)} DTE  ·  "
            f"Back {s['back_dte']}±{s.get('back_dte_flex', 0)} DTE  ·  "
            f"{s['iv_method']}  ·  min ${s['min_price']:.2f}  ·  "
            f"cache TTL {s.get('ticker_info_ttl_days', 7)}d")

    # ── Logs view ────────────────────────────────────────────────────────────

    def _build_logs_view(self):
        page, box, header = self._page(
            "Logs", f"Every scan phase and DXLink frame · {LOG_PATH}")

        clear = QPushButton("Clear")
        clear.clicked.connect(lambda: self.debug_view.clear())
        header.actions.addWidget(clear)
        open_btn = QPushButton("Open log file")
        open_btn.clicked.connect(self._open_log_file)
        header.actions.addWidget(open_btn)

        self.debug_view = QPlainTextEdit()
        self.debug_view.setReadOnly(True)
        self.debug_view.setMaximumBlockCount(MAX_LOG_LINES)
        self.debug_view.setStyleSheet(f"""
            QPlainTextEdit {{
                background-color: {T.SURFACE};
                color: {T.TEXT_DIM};
                border: 1px solid {T.BORDER_SOFT};
                border-radius: {T.RADIUS}px;
                padding: 12px;
                font-family: {T.MONO};
                font-size: 12px;
            }}
        """)
        box.addWidget(self.debug_view, 1)
        return page

    # ── Tables ───────────────────────────────────────────────────────────────

    def _make_table(self, proxy, source_model):
        view = QTableView()
        view.setModel(proxy)
        view.setItemDelegate(SignedFlashDelegate(view))
        view.setHorizontalHeader(SortHeaderView(Qt.Orientation.Horizontal, view))
        view.setSortingEnabled(True)
        view.setShowGrid(False)
        view.setAlternatingRowColors(False)   # the delegate paints row striping
        view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        view.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        view.verticalHeader().setVisible(False)
        view.verticalHeader().setDefaultSectionSize(28)
        view.setWordWrap(False)

        header = view.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        header.setSortIndicator(0, Qt.SortOrder.AscendingOrder)

        # `Column.width` is a preference, not a promise: widen any column whose
        # header wouldn't fit in the font actually resolved on this machine, so
        # headings never elide just because Inter fell back to something wider.
        metrics = QFontMetrics(header.font())
        for i, col in enumerate(source_model.COLUMNS):
            needed = metrics.horizontalAdvance(col.header) + 34   # padding + sort arrow
            view.setColumnWidth(i, max(col.width, needed))
        return view

    def _connect_signals(self):
        log.messageLogged.connect(self._append_log)

        self.results_view.selectionModel().selectionChanged.connect(
            self._on_result_selected)
        self.search_edit.textChanged.connect(self._on_search_text)
        self.search_edit.returnPressed.connect(self._search_submit)
        # Keep the empty state honest when rows appear or vanish for any reason
        # — a scan finishing, a merge, or the filter changing.
        self.results_proxy.rowsInserted.connect(
            lambda *_: self._update_results_empty_state())
        self.results_proxy.rowsRemoved.connect(
            lambda *_: self._update_results_empty_state())
        self.results_proxy.modelReset.connect(
            lambda: self._update_results_empty_state())
        self.back_paid_edit.textEdited.connect(self._on_leg_edited)
        self.front_credit_edit.textEdited.connect(self._on_leg_edited)
        self.net_debit_edit.textEdited.connect(self._on_net_debit_edited)

        self.results_feed.rowsDirty.connect(self._refresh_result_rows)
        # Colour comes from the feed's own state signals, not from parsing text.
        self.results_feed.statusChanged.connect(
            lambda msg: self._set_live_badge(msg))
        self.results_feed.connected.connect(
            lambda: self._set_live_badge(None, "live"))
        self.results_feed.failed.connect(
            lambda msg: self._set_live_badge(f"Live: {msg}", "error"))
        self.positions_feed.rowsDirty.connect(self._refresh_position_rows)
        self.positions_feed.statusChanged.connect(self.pos_status.setText)

    # ── Logs ─────────────────────────────────────────────────────────────────

    def _append_log(self, level, message):
        self.debug_view.appendPlainText(f"[{level}] {message}")

    def _open_log_file(self):
        try:
            if hasattr(os, 'startfile'):
                os.startfile(str(LOG_PATH))
            else:
                opener = 'open' if sys.platform == 'darwin' else 'xdg-open'
                subprocess.Popen([opener, str(LOG_PATH)],
                                 stderr=subprocess.DEVNULL)
        except Exception:
            QMessageBox.information(self, "Log File", f"Log location:\n{LOG_PATH}")

    # ── Settings ─────────────────────────────────────────────────────────────

    def _open_filters(self):
        dialog = FiltersDialog(self.settings, self)
        if dialog.exec():
            self.settings = dialog.settings()
            self.session.set_chain_ttl(self._chain_cache_ttl_secs())
            self._refresh_settings_summary()
            log.info(f"Filters updated: {self.settings}")

    def _clear_cache(self):
        # The in-memory cache goes first: clearing only the files would leave the
        # live instance holding the same chains, and its next save would write
        # them straight back over the files we just deleted.
        api = self.session.peek()
        n_chains = api.chains.clear() if api is not None else 0
        clear_ticker_cache()
        # The chain cache goes with it: the fundamentals refresh is exactly the
        # event that can invalidate a cached expiration list, and leaving stale
        # chains behind would undo the point of clearing.
        clear_chain_cache()
        log.info(f"Caches cleared: ticker info + {n_chains} option chain(s) "
                 f"(memory and disk)")
        QMessageBox.information(
            self, "Cache cleared",
            f"Ticker info cache cleared, along with {n_chains} cached option "
            f"chain(s). The next scan will refetch earnings, dividends, market "
            f"caps and chains for all tickers.")

    def _sign_out(self):
        confirm = QMessageBox.question(
            self, "Sign out",
            "Sign out and return to the login screen?\n\n"
            "Live streams will be disconnected.")
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self._signing_out = True
        self.signOutRequested.emit()
        self.close()

    # ── Scan ─────────────────────────────────────────────────────────────────

    def _toggle_scan(self):
        if self._scan_worker is not None and self._scan_worker.isRunning():
            self._scan_worker.cancel()
            self.run_btn.setEnabled(False)
            self.status_label.setText("Cancelling…")
            return
        self._start_scan()

    def _start_scan(self, only_tickers=None):
        self._live_iv_method = self.settings.get('iv_method', 'Midpoint')
        self._scan_targets   = list(only_tickers) if only_tickers else None

        # Tear down the previous stream before the table is rebuilt. A targeted
        # search keeps the existing rows — its results are merged in, so wiping a
        # full scan just to look up one symbol would be a bad trade.
        self.results_feed.stop()
        if not only_tickers:
            self.results_model.set_rows([])
            # A full scan shows everything it finds. A filter left over from an
            # earlier search would hide most of the new results and read as a
            # scan that found nothing, so it is dropped here rather than left for
            # the user to notice. `blockSignals` avoids a filter round trip
            # against the table we are about to clear anyway.
            if self.search_edit.text():
                self.search_edit.blockSignals(True)
                self.search_edit.clear()
                self.search_edit.blockSignals(False)
            self.results_proxy.setFilterFixedString('')
        self._set_live_badge("Idle", state=None)
        self._update_results_empty_state()

        # Warm the connection now rather than at Phase 3: the handshake overlaps
        # with Phases 1–2 instead of adding to the critical path.
        self.quote_stream.ensure_started()
        worker = ScanWorker(self.session, self.settings,
                            only_tickers=only_tickers,
                            stream=self.quote_stream, parent=self)
        worker.statusChanged.connect(self.status_label.setText)
        worker.progressChanged.connect(self._set_progress)
        worker.indeterminateChanged.connect(self._set_indeterminate)
        worker.scanFinished.connect(self._on_scan_finished)
        worker.scanFailed.connect(self._on_scan_failed)
        worker.finished.connect(self._on_scan_thread_done)
        self._scan_worker = worker

        self.run_btn.setText("Cancel")
        self.run_btn.setProperty("variant", "danger")
        self._repolish(self.run_btn)
        self.filters_btn.setEnabled(False)
        self.search_btn.setEnabled(False)
        self.scan_spinner.start()
        if only_tickers:
            self.status_label.setText(f"Searching {', '.join(only_tickers)}…")
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

    @staticmethod
    def _repolish(widget):
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def _on_scan_thread_done(self):
        # Release the scan's subscription set. `scanFinished` is emitted from
        # inside run(), so it — and therefore `_start_results_live` — has already
        # been delivered by the time this queued signal arrives: the table has
        # declared what it wants, and this drops only the symbols nothing shows
        # any more. Doing it here rather than in `_on_scan_finished` also covers
        # the cancelled and failed paths.
        self.quote_stream.release_consumer(SCAN_CONSUMER)
        self._scan_worker = None
        self.run_btn.setText("Run Scan")
        self.run_btn.setProperty("variant", "primary")
        self._repolish(self.run_btn)
        self.run_btn.setEnabled(True)
        self.filters_btn.setEnabled(True)
        self.search_btn.setEnabled(True)
        self.scan_spinner.stop()
        self._set_indeterminate(False)
        self._update_results_empty_state()

    def _on_scan_failed(self, message):
        self.status_label.setText(message)
        self.progress.setValue(0)
        log.warning(f"Scan ended: {message}")
        # A targeted search that found nothing is an ordinary outcome, not an
        # error dialog — the empty state carries the reason instead.
        if self._scan_targets:
            self.results_empty.setText(
                f"{message}\n\nTry a different DTE target in Filters, or check "
                f"the Logs view for the per-phase detail.")
            self.results_stack.setCurrentWidget(self.results_empty)

    def _on_scan_finished(self, results):
        self.progress.setValue(100)
        targets = self._scan_targets

        if not results:
            if targets:
                self.status_label.setText(
                    f"No calendar setups for {', '.join(targets)}.")
                self.results_empty.setText(
                    f"No viable calendar setup for {', '.join(targets)} at "
                    f"{self.settings['front_dte']}/{self.settings['back_dte']} DTE.\n"
                    f"The chain may lack a matching expiry pair, or neither leg "
                    f"is quoting a bid.")
                self.results_stack.setCurrentWidget(self.results_empty)
                return
            self.status_label.setText("Scan complete — no setups found.")
            QMessageBox.information(
                self, "No Results",
                "No valid calendar setups found.\n\n"
                "Check the Logs view for per-ticker rejection reasons.")
            return

        if targets:
            rows = self._merge_results(results, targets)
            # The search leaves the ticker filter on, so say so — otherwise the
            # merged row count and the visible row count disagree with no
            # explanation on screen.
            filtered = self.search_edit.text().strip()
            suffix = (f" Filter “{filtered}” is on — press Clear to see all "
                      f"{len(rows)}." if filtered else "")
            self.status_label.setText(
                f"{len(results)} setup(s) for {', '.join(targets)} — "
                f"{len(rows)} row(s) in the table.{suffix}")
        else:
            rows = list(results)
            self.results_model.set_rows(rows)
            self.status_label.setText(
                f"Scan complete — {len(rows)} setup(s) found.")

        self._update_results_empty_state()
        self._start_results_live(rows)

    def _merge_results(self, results, targets):
        """Fold targeted results into the table, replacing that ticker's rows.

        The whole row list is then re-streamed: `_start_results_live` rebuilds
        the symbol map from scratch, so the merged rows and the pre-existing ones
        end up on one feed rather than two half-live tables.
        """
        replaced = {t.upper() for t in targets}
        kept = [r for r in self.results_model.rows if r.ticker not in replaced]
        rows = kept + list(results)
        rows.sort(key=lambda r: r.fwd_factor, reverse=True)
        self.results_model.set_rows(rows)
        return rows

    # ── Results live stream ──────────────────────────────────────────────────

    def _start_results_live(self, rows):
        """Stream every result row. Keys are *source* row indices, which are
        stable under proxy sorting."""
        if len(rows) > LIVE_MAX_ROWS:
            log.warning(
                f"{len(rows)} results exceeds the {LIVE_MAX_ROWS}-row live cap — "
                f"rows beyond it keep their scan-time values and will sort "
                f"against live rows on quote-derived columns")
            rows = rows[:LIVE_MAX_ROWS]

        sym_map, equity = {}, set()
        for row, r in enumerate(rows):
            for sym in (r.ticker, r.front_sym, r.back_sym):
                if sym:
                    sym_map.setdefault(sym, set()).add(row)
            equity.add(r.ticker)
        if sym_map:
            log.info(f"Streaming {len(rows)} result row(s) / {len(sym_map)} symbols")
            self.results_feed.start(sym_map, equity)

    def _set_live_badge(self, message=None, state=None):
        """`state` is 'live' / 'error' / None (neutral). Passing only a message
        updates the text and keeps the current state colour."""
        if state is not None:
            self._live_state = state
        state = getattr(self, '_live_state', None)
        colour, background = {
            'live':  (T.POSITIVE, "rgba(63,208,127,0.12)"),
            'error': (T.NEGATIVE, "rgba(255,107,107,0.12)"),
        }.get(state, (T.TEXT_MUTED, None))
        if message is not None:
            self.live_badge.setText(message)
        self.live_badge.setStyleSheet(self._badge_style(colour, background))

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
            # `has(field)` rather than a truthiness test: a leg whose bid is
            # pulled quotes a real 0.0, and `or r.f_bid` pinned that cell to the
            # scan-time number for the rest of the session. Fall back only while
            # the field has genuinely never been quoted.
            f_bid = fq.bid if fq.has('bid') else r.f_bid
            f_ask = fq.ask if fq.has('ask') else r.f_ask
            b_bid = bq.bid if bq.has('bid') else r.b_bid
            b_ask = bq.ask if bq.has('ask') else r.b_ask

            # A None solve still updates the quote columns; the metrics simply
            # hold their previous values.
            ivs = solve_calendar(price, r.strike, r.front_dte, r.back_dte,
                                 f_bid, f_ask, b_bid, b_ask, iv_method)
            self.results_model.apply_live_update(
                row, price, f_bid, f_ask, b_bid, b_ask, ivs)

    # ── Ticker search ────────────────────────────────────────────────────────

    def _on_search_text(self, text):
        """Uppercase as you type, then filter the table live.

        Filtering is free — the rows are already in the model — so it happens on
        every keystroke; the on-demand scan is reserved for Enter / the button.
        """
        upper = text.upper()
        if upper != text:
            pos = self.search_edit.cursorPosition()
            self.search_edit.setText(upper)     # re-enters once, then upper == text
            self.search_edit.setCursorPosition(pos)
            return
        self._apply_ticker_filter(upper)

    def _apply_ticker_filter(self, symbol):
        # A fixed string, not a pattern: a ticker like BRK.B contains regex
        # metacharacters and would otherwise match the wrong rows.
        self.results_proxy.setFilterFixedString(symbol)
        self._update_results_empty_state(symbol)

    def _clear_search_filter(self):
        """Clear button: drop the filter and show the whole table again."""
        if self.search_edit.text():
            self.search_edit.clear()        # textChanged re-applies an empty filter
        else:
            self._apply_ticker_filter('')

    def _update_filter_badge(self, symbol=None):
        """Report whether the proxy filter is hiding anything."""
        symbol = (self.search_edit.text() if symbol is None else symbol).strip().upper()
        total  = self.results_model.rowCount()
        shown  = self.results_proxy.rowCount()
        self.search_clear_btn.setEnabled(bool(symbol))
        if symbol:
            self.filter_badge.setText(f"Filtered: {symbol} — {shown} of {total}")
            self.filter_badge.setStyleSheet(
                self._badge_style(T.ACCENT, "rgba(76,141,255,0.12)"))
            self.filter_badge.setToolTip(
                f"Showing only rows for {symbol}. Press Clear to show all "
                f"{total} row(s).")
        else:
            self.filter_badge.setText(f"All rows ({total})" if total else "No rows")
            self.filter_badge.setStyleSheet(self._badge_style(T.TEXT_MUTED))
            self.filter_badge.setToolTip("No ticker filter — every scanned row is shown.")

    def _update_results_empty_state(self, symbol=None):
        """Swap between the table and the empty-state message."""
        symbol = self.search_edit.text().strip() if symbol is None else symbol
        self._update_filter_badge(symbol)
        if self.results_proxy.rowCount() > 0:
            self.results_stack.setCurrentWidget(self.results_view)
            return

        if self.results_model.rowCount() == 0:
            message = ("No scan results yet.\n"
                       "Run a scan, or search a ticker to look at just that symbol.")
        elif symbol:
            message = (f"No rows for “{symbol}” in the current results.\n"
                       f"Press Enter to scan {symbol} on demand.")
        else:
            message = "No results."
        self.results_empty.setText(message)
        self.results_stack.setCurrentWidget(self.results_empty)

    def _search_submit(self):
        """Enter in the search box: show what we already have, and only go to the
        network when we have nothing for this ticker."""
        symbol = self.search_edit.text().strip().upper()
        if not symbol:
            self._apply_ticker_filter('')
            return
        self._apply_ticker_filter(symbol)
        if self.results_proxy.rowCount() > 0:
            self.status_label.setText(
                f"{self.results_proxy.rowCount()} row(s) for {symbol} from the "
                f"last scan — press Search to refresh from the market.")
            return
        self._search_ticker()

    def _search_ticker(self):
        """Search button: always run a targeted scan for the typed symbol."""
        symbol = self.search_edit.text().strip().upper()
        if not symbol:
            self.search_edit.setFocus()
            return
        if self._scan_worker is not None and self._scan_worker.isRunning():
            QMessageBox.information(
                self, "Scan in progress",
                "A scan is already running. Cancel it before searching a ticker.")
            return
        self._apply_ticker_filter(symbol)
        self._start_scan(only_tickers=[symbol])

    # ── Trade analysis panel ─────────────────────────────────────────────────

    def _open_setup_detail(self, index):
        """Double-click a row: open the interactive fill/P-L window for it."""
        if not index.isValid():
            return
        source = self.results_proxy.mapToSource(index)
        result = self.results_model.row_at(source.row())
        if result is None:
            return
        try:
            window = SetupDetailWindow(result, parent=self)
        except Exception as exc:
            log.error(f"Failed to open setup detail for {result.ticker}: {exc}")
            QMessageBox.critical(self, "Setup detail",
                                 f"Could not open this setup: {exc}")
            return
        self._chart_windows.append(window)     # keep the reference alive
        window.show()
        window.raise_()

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
            f"{r.ticker}  ·  ${r.price:.2f}  ·  strike ${r.strike:.2f}  ·  "
            f"{r.front_dte}/{r.back_dte} DTE")
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

        ivs = solve_fills(r.price, r.strike, r.front_dte, r.back_dte,
                          front_credit, back_price)
        if ivs is None:
            self.real_fwd_label.setText("Cannot compute — negative forward variance")
            self.real_fwd_label.setStyleSheet(
                f"color: {T.NEGATIVE}; font-weight: 600;")
            return

        self.real_fwd_label.setStyleSheet(
            f"color: {T.POSITIVE if ivs.fwd_factor >= 0 else T.NEGATIVE}; "
            f"font-weight: 600;")
        self.real_fwd_label.setText(
            f"Real fwd factor {ivs.fwd_factor * 100:+.2f}%   "
            f"(F {ivs.front_iv * 100:.1f}%  ·  B {ivs.back_iv * 100:.1f}%  ·  "
            f"Fwd {ivs.fwd_iv * 100:.1f}%)")

        # Chart with the back IV solved from the actual fill, so the reference
        # line reflects what the trader would be paying.
        self._open_chart(
            ticker=r.ticker, price=r.price, strike=r.strike,
            front_dte=r.front_dte, back_dte=r.back_dte,
            back_paid=back_price, front_credit=front_credit,
            current_back_iv=ivs.back_iv, fwd_iv=ivs.fwd_iv)

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
            QMessageBox.information(self, "Remove Position",
                                    "Select a position to remove.")
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

        unresolved = [p for p in positions if not p.front_sym and not p.resolve_error]
        if unresolved:
            if self._resolve_worker is not None and self._resolve_worker.isRunning():
                return
            worker = PositionResolveWorker(
                self.session, unresolved,
                chain_ttl=self._chain_cache_ttl_secs(), parent=self)
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
        total = self.positions_model.total_pl()
        self.pos_total.setText(f"${total:,.2f}")
        tone = "positive" if total > 0 else "negative" if total < 0 else None
        self.pos_total.setProperty("tone", tone or "")
        self._repolish(self.pos_total)

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
        if self._calendar_worker is not None and self._calendar_worker.isRunning():
            self._calendar_worker.wait(3000)
        self.results_feed.stop()
        self.positions_feed.stop()
        # The controllers only release their symbols — the socket belongs to the
        # shared stream, and its QThread would otherwise sit in run_forever() and
        # keep the process alive.
        self.quote_stream.stop()
        # Same for the SDK's event-loop thread.
        self.session.reset()

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)
