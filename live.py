"""Live quote plumbing shared by the Results and Positions tables.

The legacy app carried two near-identical copies of this logic (`_live_*` and
`_pos_*`), so a fix to one had to be mirrored in the other. Here there is one
implementation, instantiated twice with different row keys.

What this class does *not* own any more is the socket. There is one
`api.QuoteStream` for the whole app — shared with the scan pipeline — and each
controller is simply a named consumer of it, declaring which symbols it wants and
mapping them onto its table's row keys. Connecting, reconnecting and the quote
cache all live in the stream; `stop()` here releases this table's symbols and
leaves the connection up for everyone else.

Everything in this class runs on the GUI thread: `QuoteStream.quotesUpdated` is
delivered there, so the row bookkeeping needs no locking. Updates are coalesced —
each burst of quotes marks row keys dirty and (re)arms a single-shot timer, so a
50-row stream produces one repaint per `flush_ms` instead of one per tick.
"""

from PySide6.QtCore import QObject, QTimer, Signal

from applog import log
from data_models import Quote

# Coalescing window for quote-driven repaints. 750 ms made a moving market look
# a step behind on the tape; 250 ms still folds a burst of ticks into one
# repaint, and the flash fade (models.FLASH_TICK_MS, 100 ms) is the finer timer
# on the GUI thread either way.
FLUSH_MS = 250


class LiveFeedController(QObject):
    """Maps one table's rows onto the shared quote stream."""

    rowsDirty      = Signal(set)   # keys whose inputs changed since the last flush
    statusChanged  = Signal(str)
    connected      = Signal()
    failed         = Signal(str)

    def __init__(self, stream, name="live", flush_ms=FLUSH_MS, parent=None):
        super().__init__(parent)
        self._stream  = stream
        self._name    = name
        self._sym_map = {}      # symbol -> set(row key)
        self._equity  = set()
        self._dirty   = set()
        self._active  = False

        self._flush = QTimer(self)
        self._flush.setSingleShot(True)
        self._flush.setInterval(flush_ms)
        self._flush.timeout.connect(self._emit_dirty)

        stream.quotesUpdated.connect(self._on_quotes)
        stream.statusChanged.connect(self._on_status)
        stream.connected.connect(self._on_connected)
        stream.failed.connect(self._on_failed)

    # ── state ──

    @property
    def is_connected(self):
        return self._active and self._stream.is_ready

    def quote(self, symbol) -> Quote:
        """Latest merged quote. Served from the stream's store, which is the one
        cache in the app — the scan reads the same object."""
        return self._stream.quote(symbol)

    def has_quote(self, symbol, field=None) -> bool:
        """Whether a live value has been seen for `symbol` (optionally for one
        field). A row uses this to decide that a 0.0 bid is the market speaking
        rather than "nothing has arrived yet"."""
        q = self._stream.quote(symbol)
        return q.has(field) if field else bool(q.provided)

    # ── lifecycle ──

    def start(self, sym_map, equity_syms):
        """Subscribe. `sym_map` maps streamer symbol -> set of row keys it feeds;
        symbols in `equity_syms` also get Trade events.

        Safe to call repeatedly — the stream diffs this set against what is
        already on the wire and sends only the delta.
        """
        self._sym_map = dict(sym_map)
        self._equity  = set(equity_syms)
        if not self._sym_map:
            return
        self._active = True
        self._stream.set_consumer(self._name, set(self._sym_map), self._equity)

    def stop(self):
        """Release this table's symbols.

        Deliberately does not touch the connection: the other table and the next
        scan are on it. Only `QuoteStream.stop()` takes the feed down.
        """
        self._active  = False
        self._sym_map = {}
        self._dirty.clear()
        self._flush.stop()
        self._stream.release_consumer(self._name)
        self.statusChanged.emit("No live connection")

    # ── stream callbacks (GUI thread) ──

    def _on_status(self, message):
        if self._active:
            self.statusChanged.emit(message)

    def _on_connected(self):
        if self._active:
            self.connected.emit()

    def _on_failed(self, message):
        if self._active:
            log.error(f"{self._name} live stream: {message}")
            self.failed.emit(message)

    def _on_quotes(self, updates):
        """One connection feeds every consumer, so a burst carries symbols this
        table doesn't show; `_sym_map` is what filters it down to our rows."""
        touched = set()
        for symbol in updates:
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
