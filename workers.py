"""Background workers.

Every off-GUI-thread activity in the app is a `QThread` here, and the only way
results reach the GUI is a signal. No widget is ever touched from these classes.
"""

from PySide6.QtCore import QMutex, QMutexLocker, QObject, QThread, Signal

from api import TastytradeAPI
from applog import log
from scanner import ScanAborted, resolve_position_legs, run_scan


class ApiSession(QObject):
    """Lazily-built, shared `TastytradeAPI`.

    One instance is shared by the scan worker, the position resolver and both
    live clients, so construction is mutex-protected. `ensure()` performs an
    OAuth round trip and must only be called from a worker thread.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._api    = None
        self._mutex  = QMutex()
        self._secret = ''
        self._refresh = ''

    def set_credentials(self, client_secret, refresh_token):
        with QMutexLocker(self._mutex):
            if (client_secret, refresh_token) != (self._secret, self._refresh):
                self._secret, self._refresh = client_secret, refresh_token
                self._api = None      # force re-auth with the new grant
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
            self._api = TastytradeAPI(self._secret, self._refresh)
            return self._api

    def reset(self):
        with QMutexLocker(self._mutex):
            self._api = None


class ScanWorker(QThread):
    """Runs the whole scan pipeline off the GUI thread.

    Doubles as the pipeline's `reporter`: `status` / `progress` / `indeterminate`
    are signal emissions, so the pipeline stays Qt-widget-free while the sidebar
    updates live.
    """

    statusChanged = Signal(str)
    progressChanged = Signal(int)
    indeterminateChanged = Signal(bool)
    scanFinished = Signal(list)     # list[ScanResult]
    scanFailed = Signal(str)

    def __init__(self, session: ApiSession, settings: dict, parent=None):
        super().__init__(parent)
        self._session  = session
        self._settings = dict(settings)
        self._cancel_mutex = QMutex()
        self._cancelled = False

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
        except Exception as exc:
            self.scanFailed.emit(f"Auth Error: {exc}")
            return

        try:
            results = run_scan(api, self._settings, reporter=self)
        except ScanAborted as exc:
            self.scanFailed.emit(str(exc))
            return
        except Exception as exc:
            log.error(f"Scan crashed: {exc}")
            self.scanFailed.emit(f"Scan failed: {exc}")
            return

        self.scanFinished.emit(results)


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

    def __init__(self, session: ApiSession, positions, parent=None):
        super().__init__(parent)
        self._session = session
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
                res = resolve_position_legs(api, ticker, strike, f_exp, b_exp)
            except Exception as exc:
                log.error(f"Position {ticker} chain resolve failed: {exc}")
                self.positionResolved.emit(pid, '', '', str(exc))
                continue
            self.positionResolved.emit(
                pid, res['front_streamer_symbol'], res['back_streamer_symbol'], '')

        self.resolveFinished.emit()
