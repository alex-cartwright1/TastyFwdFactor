"""Live quote plumbing shared by the Results and Positions tables.

The legacy app carried two near-identical copies of this logic (`_live_*` and
`_pos_*`), so a fix to one had to be mirrored in the other. Here there is one
implementation, instantiated twice with different row keys.

Everything in this class runs on the GUI thread: `DXLinkLiveClient.quotesUpdated`
is a queued signal, so the quote cache needs no locking. Updates are coalesced —
each burst of quotes marks row keys dirty and (re)arms a single-shot timer, so a
50-row stream produces one repaint per `flush_ms` instead of one per tick.

The controller also owns reconnection. A dropped socket surfaces either as
`connectionFailed` or as a bare `disconnected`, and both are treated as a drop:
the dead client is released and a fresh one (with a fresh quote token) is built
after a backoff. Only `stop()` makes the feed stay down.
"""

from PySide6.QtCore import QObject, QTimer, Signal

from api import DXLinkLiveClient
from applog import log
from data_models import Quote

FLUSH_MS = 750

# A dropped socket used to be terminal: the client was torn down, nothing
# rebuilt it, and the table quietly showed scan-time prices forever. Retries
# back off exponentially from RECONNECT_MS to RECONNECT_MAX_MS and then keep
# trying at that ceiling — a feed that stays down through a network blip is a
# worse failure than a reconnect loop, and the status badge shows what's going on.
RECONNECT_MS     = 2_000
RECONNECT_MAX_MS = 30_000


class LiveFeedController(QObject):
    """Owns one persistent DXLink connection and the fan-out to row keys."""

    rowsDirty      = Signal(set)   # keys whose inputs changed since the last flush
    statusChanged  = Signal(str)
    connected      = Signal()
    failed         = Signal(str)

    def __init__(self, session, name="live", flush_ms=FLUSH_MS, parent=None):
        super().__init__(parent)
        self._session = session
        self._name    = name
        self._client  = None
        self._quotes  = {}      # symbol -> Quote
        self._sym_map = {}      # symbol -> set(row key)
        self._equity  = set()
        self._dirty   = set()

        # True whenever the feed is deliberately idle, so a teardown we asked for
        # is never mistaken for a drop worth reconnecting.
        self._stopped  = True
        self._retry_ms = RECONNECT_MS

        self._flush = QTimer(self)
        self._flush.setSingleShot(True)
        self._flush.setInterval(flush_ms)
        self._flush.timeout.connect(self._emit_dirty)

        self._retry = QTimer(self)
        self._retry.setSingleShot(True)
        self._retry.timeout.connect(self._reconnect)

    # ── state ──

    @property
    def is_connected(self):
        return self._client is not None

    def quote(self, symbol) -> Quote:
        return self._quotes.get(symbol, Quote())

    # ── lifecycle ──

    def start(self, sym_map, equity_syms):
        """Connect (if needed) and subscribe. `sym_map` maps streamer symbol ->
        set of row keys it feeds; symbols in `equity_syms` also get Trade events.

        Safe to call repeatedly — an existing connection is reused and only the
        new symbols are subscribed.
        """
        self._sym_map = dict(sym_map)
        self._equity  = set(equity_syms)
        if not self._sym_map:
            return

        self._stopped  = False
        self._retry_ms = RECONNECT_MS
        self._retry.stop()

        if self._client is not None:
            self._subscribe()
            return

        self._connect("Connecting live stream…")

    def _connect(self, status):
        """Build a fresh client. Always a new one — `DXLinkLiveClient` fetches its
        own (short-lived) quote token in `run()`, so reconnecting this way picks up
        a valid token rather than replaying the expired one."""
        api = self._session.peek()
        if api is None:
            self.failed.emit("not authenticated")
            self._schedule_reconnect("not authenticated")
            return

        self.statusChanged.emit(status)
        client = DXLinkLiveClient(api, parent=self)
        client.quotesUpdated.connect(self._on_quotes)
        client.ready.connect(self._on_ready)
        client.connectionFailed.connect(self._on_failed)
        # Bound method, not a lambda: a functor with no context object would be a
        # DirectConnection and run this on the WebSocket thread.
        client.disconnected.connect(self._on_disconnected)
        self._client = client
        client.start()

    def stop(self):
        self._stopped  = True
        self._retry_ms = RECONNECT_MS
        self._retry.stop()
        client, self._client = self._client, None
        self._sym_map = {}
        self._dirty.clear()
        self._flush.stop()
        self._quotes.clear()
        if client is not None:
            try:
                client.stop()
            except Exception as exc:
                log.warning(f"Error closing {self._name} live client: {exc}")
            client.deleteLater()
        self.statusChanged.emit("No live connection")

    # ── client callbacks (GUI thread) ──

    def _on_ready(self):
        self._retry_ms = RECONNECT_MS   # the connection held; start over on backoff
        self._subscribe()
        self.connected.emit()
        self.statusChanged.emit(
            f"Live: {len(self._sym_map)} symbol(s) streaming")

    def _on_failed(self, message):
        log.error(f"{self._name} live stream failed: {message}")
        self._teardown_client()
        self.failed.emit(message)
        self.statusChanged.emit(f"Live: {message}")
        self._schedule_reconnect(message)

    def _on_disconnected(self):
        """`run()` returned without an error ever reaching `_on_failed` — a socket
        that closed cleanly under us. Without this the feed would sit there looking
        connected while no quotes arrived."""
        if self._stopped or self._client is None:
            return          # deliberate stop, or _on_failed already handled it
        if self.sender() is not self._client:
            return          # a superseded client finishing late
        log.warning(f"{self._name} live stream disconnected")
        self._teardown_client()
        self.failed.emit("disconnected")
        self._schedule_reconnect("disconnected")

    def _teardown_client(self):
        client, self._client = self._client, None
        if client is not None:
            # run_forever has already returned; just release the thread object so
            # a later start() isn't stacking dead clients under this parent.
            client.stop(wait_ms=1000)
            client.deleteLater()

    def _schedule_reconnect(self, reason):
        if self._stopped or not self._sym_map or self._retry.isActive():
            return
        delay, self._retry_ms = self._retry_ms, min(self._retry_ms * 2,
                                                    RECONNECT_MAX_MS)
        log.info(f"{self._name} live stream reconnecting in {delay} ms ({reason})")
        self.statusChanged.emit(
            f"Live: {reason} — reconnecting in {round(delay / 1000)}s")
        self._retry.start(delay)

    def _reconnect(self):
        if self._stopped or not self._sym_map or self._client is not None:
            return
        self._connect("Reconnecting live stream…")

    def _subscribe(self):
        if self._client is None:
            return
        self._client.subscribe(list(self._sym_map), with_trade_for=self._equity)

    def _on_quotes(self, updates):
        touched = set()
        for symbol, quote in updates.items():
            self._quotes.setdefault(symbol, Quote()).merge(quote)
            touched |= self._sym_map.get(symbol, set())
        if not touched:
            return
        self._dirty |= touched
        if not self._flush.isActive():
            self._flush.start()

    def _emit_dirty(self):
        dirty, self._dirty = self._dirty, set()
        if dirty:
            self.rowsDirty.emit(dirty)
