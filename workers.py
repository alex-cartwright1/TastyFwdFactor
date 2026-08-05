"""Background workers.

Every off-GUI-thread activity in the app is a `QThread` here, and the only way
results reach the GUI is a signal. No widget is ever touched from these classes.
"""

from PySide6.QtCore import QMutex, QMutexLocker, QObject, QThread, Signal

from api import MarketDataSession, TastytradeAPI
from applog import log
from scanner import ScanAborted, resolve_position_legs, run_scan


class ApiSession(QObject):
    """Lazily-built, shared `TastytradeAPI` plus `MarketDataSession`.

    One instance is shared by the scan worker, the position resolver and both
    live clients, so construction is mutex-protected. `ensure()` performs an
    OAuth round trip and must only be called from a worker thread; so must
    `ensure_sdk()`, whose first call builds the SDK's event-loop thread.

    Both clients are backed by the same OAuth grant — the hand-rolled
    `TastytradeAPI` for chains and quote tokens, the SDK for reference data —
    so a credential change has to invalidate both together.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._api    = None
        self._sdk    = None
        self._mutex  = QMutex()
        self._secret = ''
        self._refresh = ''
        # Chain-cache TTL in seconds, needed at TastytradeAPI construction so the
        # on-disk cache can be seeded (and expired entries dropped) up front.
        self._chain_ttl = 0

    def set_chain_ttl(self, seconds):
        with QMutexLocker(self._mutex):
            self._chain_ttl = max(int(seconds), 0)
        return self

    def set_credentials(self, client_secret, refresh_token):
        with QMutexLocker(self._mutex):
            if (client_secret, refresh_token) != (self._secret, self._refresh):
                self._secret, self._refresh = client_secret, refresh_token
                self._api = None      # force re-auth with the new grant
                self._close_sdk_locked()
        return self

    @property
    def has_credentials(self):
        with QMutexLocker(self._mutex):
            return bool(self._secret and self._refresh)

    def peek(self):
        """Current API instance without authenticating. May be None."""
        with QMutexLocker(self._mutex):
            return self._api

    def ensure(self):
        with QMutexLocker(self._mutex):
            if self._api is not None:
                return self._api
            if not (self._secret and self._refresh):
                raise RuntimeError("Tastytrade OAuth credentials are not set")
            self._api = TastytradeAPI(self._secret, self._refresh,
                                      chain_ttl_secs=self._chain_ttl)
            return self._api

    def ensure_sdk(self) -> MarketDataSession:
        """Shared `tastytrade` SDK session. Worker threads only — the first call
        starts the SDK's private event-loop thread and later calls block on it."""
        with QMutexLocker(self._mutex):
            if self._sdk is not None:
                return self._sdk
            if not (self._secret and self._refresh):
                raise RuntimeError("Tastytrade OAuth credentials are not set")
            self._sdk = MarketDataSession(self._secret, self._refresh)
            return self._sdk

    def _close_sdk_locked(self):
        """Tear the SDK loop down. Caller must hold the mutex."""
        if self._sdk is not None:
            try:
                self._sdk.close()
            except Exception as exc:
                log.debug(f"SDK session shutdown: {exc}")
            self._sdk = None

    def save_chain_cache(self):
        """Persist the option-chain cache if it changed. No-op when nothing has
        been fetched yet."""
        api = self.peek()
        if api is not None:
            api.save_chains()

    def reset(self):
        # Outside the mutex, and before the instance is dropped — this is the
        # last chance to keep a session's worth of chain fetches.
        self.save_chain_cache()
        with QMutexLocker(self._mutex):
            self._api = None
            self._close_sdk_locked()


class AuthWorker(QThread):
    """Performs the login OAuth round trip off the GUI thread.

    The exchange takes a second or two against production, which is exactly long
    enough to freeze a login form — so the window shows a spinner and waits for
    one of these signals.
    """

    succeeded = Signal()
    failed = Signal(str)

    def __init__(self, session: ApiSession, parent=None):
        super().__init__(parent)
        self._session = session

    def run(self):
        try:
            self._session.ensure()
        except Exception as exc:
            log.warning(f"Authentication failed: {exc}")
            self.failed.emit(_friendly_auth_error(exc))
            return
        self.succeeded.emit()


def _friendly_auth_error(exc):
    """Turn an OAuth/transport failure into something a trader can act on."""
    text = str(exc)
    if "401" in text or "invalid_grant" in text or "invalid" in text.lower():
        return ("Tastytrade rejected these credentials. Check the client secret "
                "and refresh token, and that the grant hasn't been revoked.")
    if any(word in text.lower() for word in ("timed out", "timeout", "connection",
                                             "resolve", "network", "ssl")):
        return "Could not reach api.tastyworks.com. Check your connection."
    return text[:300]


class ScanWorker(QThread):
    """Runs the whole scan pipeline off the GUI thread.

    Doubles as the pipeline's `reporter`: `status` / `progress` / `indeterminate`
    are signal emissions, so the pipeline stays Qt-widget-free while the sidebar
    updates live.

    `only_tickers` switches to a targeted scan of those symbols instead of the
    watchlist — the single-ticker search. Same pipeline, same signals, so the
    controller wires it up identically.
    """

    statusChanged = Signal(str)
    progressChanged = Signal(int)
    indeterminateChanged = Signal(bool)
    scanFinished = Signal(list)     # list[ScanResult]
    scanFailed = Signal(str)

    def __init__(self, session: ApiSession, settings: dict, only_tickers=None,
                 stream=None, parent=None):
        super().__init__(parent)
        self._session  = session
        self._settings = dict(settings)
        self._only     = list(only_tickers) if only_tickers else None
        # The app's shared api.QuoteStream. Phase 3 subscribes on it rather than
        # opening its own sockets, and leaves the subscription up for the table.
        self._stream   = stream
        self._cancel_mutex = QMutex()
        self._cancelled = False

    @property
    def only_tickers(self):
        return list(self._only) if self._only else None

    # ── reporter protocol (called from this thread) ──

    def status(self, msg):       self.statusChanged.emit(msg)
    def progress(self, pct):     self.progressChanged.emit(int(pct))
    def indeterminate(self, on): self.indeterminateChanged.emit(bool(on))

    def cancelled(self):
        with QMutexLocker(self._cancel_mutex):
            return self._cancelled

    def cancel(self):
        with QMutexLocker(self._cancel_mutex):
            self._cancelled = True

    # ── thread body ──

    def run(self):
        try:
            self.statusChanged.emit("Authenticating with Tastytrade (OAuth)…")
            api = self._session.ensure()
            sdk = self._session.ensure_sdk()
        except Exception as exc:
            self.scanFailed.emit(f"Auth Error: {exc}")
            return

        try:
            results = run_scan(api, sdk, self._settings, reporter=self,
                               only_tickers=self._only, stream=self._stream)
        except ScanAborted as exc:
            self.scanFailed.emit(str(exc))
            return
        except Exception as exc:
            log.error(f"Scan crashed: {exc}")
            self.scanFailed.emit(f"Scan failed: {exc}")
            return
        else:
            self.scanFinished.emit(results)
        finally:
            # Persist whatever Phase 2 fetched, on this thread rather than the
            # GUI's — a wide sweep is several MB. In `finally` because a cancelled
            # or failed scan has usually still fetched thousands of chains, and
            # throwing them away would mean refetching them all next time.
            try:
                api.save_chains()
            except Exception as exc:
                log.warning(f"Could not persist chain cache: {exc}")


class PositionResolveWorker(QThread):
    """Resolves streamer symbols for positions that don't have them yet.

    Emits one `positionResolved` per position so the table can update
    incrementally, then `resolveFinished` with the symbol map the caller should
    subscribe to.
    """

    statusChanged = Signal(str)
    positionResolved = Signal(str, str, str, str)   # id, front_sym, back_sym, error
    resolveFinished = Signal()
    resolveFailed = Signal(str)

    def __init__(self, session: ApiSession, positions, chain_ttl=0, parent=None):
        super().__init__(parent)
        self._session   = session
        self._chain_ttl = chain_ttl
        # Copy only what the worker needs — the Position objects themselves stay
        # owned by the GUI thread.
        self._specs = [
            (p.id, p.ticker, float(p.strike), p.front_expiry, p.back_expiry)
            for p in positions
        ]

    def run(self):
        try:
            api = self._session.ensure()
        except Exception as exc:
            log.warning(f"Positions live: auth not ready ({exc})")
            self.resolveFailed.emit(str(exc))
            return

        for pid, ticker, strike, f_exp, b_exp in self._specs:
            self.statusChanged.emit(f"Resolving {ticker} chain…")
            try:
                res = resolve_position_legs(api, ticker, strike, f_exp, b_exp,
                                            cache_ttl=self._chain_ttl)
            except Exception as exc:
                log.error(f"Position {ticker} chain resolve failed: {exc}")
                self.positionResolved.emit(pid, '', '', str(exc))
                continue
            self.positionResolved.emit(
                pid, res['front_streamer_symbol'], res['back_streamer_symbol'], '')

        self.resolveFinished.emit()


class MarketCalendarWorker(QThread):
    """Fetches the US equity holiday calendar for the header's status light.

    The indicator is computed locally once a second, so the calendar only has to
    be fetched occasionally — this runs once at startup and then daily. A failure
    is not fatal: `market_clock.market_status` degrades to weekends + regular
    hours when it has no calendar, so the light stays broadly correct offline.
    """

    calendarReady = Signal(object)      # data_models.MarketCalendar | None

    def __init__(self, session: ApiSession, parent=None):
        super().__init__(parent)
        self._session = session

    def run(self):
        try:
            sdk = self._session.ensure_sdk()
        except Exception as exc:
            log.warning(f"Market calendar: auth not ready ({exc})")
            self.calendarReady.emit(None)
            return
        calendar = sdk.fetch_market_calendar()
        if calendar:
            log.info(f"Market calendar: {len(calendar.holidays)} holidays, "
                     f"{len(calendar.half_days)} half days")
        self.calendarReady.emit(calendar)
