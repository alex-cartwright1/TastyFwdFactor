"""Live quote plumbing shared by the Results and Positions tables.

The legacy app carried two near-identical copies of this logic (`_live_*` and
`_pos_*`), so a fix to one had to be mirrored in the other. Here there is one
implementation, instantiated twice with different row keys.

Everything in this class runs on the GUI thread: `DXLinkLiveClient.quotesUpdated`
is a queued signal, so the quote cache needs no locking. Updates are coalesced —
each burst of quotes marks row keys dirty and (re)arms a single-shot timer, so a
50-row stream produces one repaint per `flush_ms` instead of one per tick.
"""

from PySide6.QtCore import QObject, QTimer, Signal

from api import DXLinkLiveClient
from applog import log
from data_models import Quote

FLUSH_MS = 750


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

        self._flush = QTimer(self)
        self._flush.setSingleShot(True)
        self._flush.setInterval(flush_ms)
        self._flush.timeout.connect(self._emit_dirty)

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

        if self._client is not None:
            self._subscribe()
            return

        api = self._session.peek()
        if api is None:
            self.failed.emit("not authenticated")
            return

        self.statusChanged.emit("Connecting live stream…")
        client = DXLinkLiveClient(api, parent=self)
        client.quotesUpdated.connect(self._on_quotes)
        client.ready.connect(self._on_ready)
        client.connectionFailed.connect(self._on_failed)
        self._client = client
        client.start()

    def stop(self):
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
        self._subscribe()
        self.connected.emit()
        self.statusChanged.emit(
            f"Live: {len(self._sym_map)} symbol(s) streaming")

    def _on_failed(self, message):
        log.error(f"{self._name} live stream failed: {message}")
        client, self._client = self._client, None
        if client is not None:
            # run_forever has already returned; just release the thread object so
            # a later start() isn't stacking dead clients under this parent.
            client.stop(wait_ms=1000)
            client.deleteLater()
        self.failed.emit(message)
        self.statusChanged.emit(f"Live: {message}")

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
