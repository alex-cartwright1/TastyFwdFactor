"""Tastytrade production REST API and the DXLink WebSocket quote feed.

Read-only: nothing here places orders.

Two DXLink consumers live in this module:

* :func:`fetch_quotes_with_retry` — a one-shot batched fetch used by the scan.
  It opens a connection, waits for enough data (or a timeout), and closes.
* :class:`DXLinkLiveClient` — a persistent ``QThread`` that stays connected and
  emits ``quotesUpdated`` for as long as the GUI wants live prices.

Handshake for both:
``SETUP`` → ``AUTH`` → ``AUTH_STATE: AUTHORIZED`` → ``CHANNEL_REQUEST``
(channel 1, FEED) → ``CHANNEL_OPENED`` → ``FEED_SETUP`` (COMPACT) →
``FEED_CONFIG`` → ``FEED_SUBSCRIPTION``.
"""

import json
import math
import random
import time

import requests
import websocket
from PySide6.QtCore import (
    QMutex, QMutexLocker, QRunnable, QSemaphore, QThread, QThreadPool,
    QTimer, Signal,
)

from applog import log
from data_models import Quote

BASE = "https://api.tastyworks.com"

# The /api-quote-tokens response uses different key names depending on the API
# version, so try all known variants rather than assuming one.
_TOKEN_KEYS = ('token', 'streamer-token', 'websocket-token', 'dxlink-token', 'access-token')
_URL_KEYS   = ('dxlink-url', 'websocket-url', 'streamer-url', 'url')

# Server drops the connection at 60s without a KEEPALIVE; send well inside that.
KEEPALIVE_MS = 30_000

# DXLink coalesces events over this window before sending them, and the unit is
# SECONDS. It is a throttle, not a batching hint: at 10 every symbol is capped to
# one update per 10s, which reads as a frozen quote feed and also starves the
# scan's one-shot fetch inside its 25s timeout. 0.1 is what Tastytrade's own
# streamer docs use for real-time data.
AGGREGATION_PERIOD = 0.1

_FEED_SETUP = {
    "type": "FEED_SETUP", "channel": 1,
    "acceptAggregationPeriod": AGGREGATION_PERIOD,
    "acceptDataFormat": "COMPACT",
    "acceptEventFields": {
        "Quote": ["eventSymbol", "bidPrice", "askPrice"],
        "Trade": ["eventSymbol", "price"],
    },
}


# ─── REST ────────────────────────────────────────────────────────────────────

class TastytradeAPI:
    """Exchanges the long-lived refresh token for ~15-minute access tokens.

    A single instance is shared across the scan's worker pool, so
    `_ensure_access_token()` is mutex-protected and must be called at the top of
    every REST method.
    """

    def __init__(self, client_secret, refresh_token):
        self.session = requests.Session()
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._access_token  = None
        self._access_token_expiry = 0.0
        self._token_mutex = QMutex()
        self._refresh_access_token()

    def _refresh_access_token(self):
        """Body matches both official SDKs: client_id and redirect_uri are not
        required."""
        url = f"{BASE}/oauth/token"
        log.info(f"POST {url} (grant_type=refresh_token)")
        # Bare requests.post so we don't send a stale Bearer header during refresh.
        r = requests.post(
            url,
            json={
                "grant_type":    "refresh_token",
                "client_secret": self._client_secret,
                "refresh_token": self._refresh_token,
            },
            timeout=15,
        )
        log.debug(f"OAuth response {r.status_code}: {r.text[:400]}")
        if r.status_code != 200:
            raise RuntimeError(f"OAuth refresh failed ({r.status_code}): {r.text[:200]}")
        body = r.json()
        self._access_token = body['access_token']
        expires_in = int(body.get('expires_in', 900))
        # 60-second safety margin so we don't race a request against expiry
        self._access_token_expiry = time.time() + expires_in - 60
        self.session.headers.update({"Authorization": f"Bearer {self._access_token}"})
        log.info(f"OAuth refresh OK — access_token acquired (expires_in={expires_in}s)")

    def _ensure_access_token(self):
        with QMutexLocker(self._token_mutex):
            if time.time() >= self._access_token_expiry:
                log.info("Access token near/past expiry — refreshing")
                self._refresh_access_token()

    def get_option_chain(self, symbol, retries=5, timeout=30):
        self._ensure_access_token()
        url = f"{BASE}/option-chains/{symbol}/nested"
        for attempt in range(retries):
            try:
                r = self.session.get(url, timeout=timeout)
                if r.status_code == 200:
                    return r.json()['data']['items']
                log.debug(f"Chain {symbol}: HTTP {r.status_code} (attempt {attempt+1}) — {r.text[:120]}")
                # Retry on rate limiting (429) and transient server errors;
                # give up on other client errors (404, 401, etc).
                if r.status_code != 429 and r.status_code < 500:
                    break
                if attempt < retries - 1:
                    # Honor Retry-After if the API sends one; otherwise back off
                    # exponentially. Jitter desyncs the worker pool so retries
                    # don't all land on the same instant and re-trip the limit.
                    retry_after = r.headers.get('Retry-After')
                    try:
                        delay = float(retry_after) if retry_after is not None else 2 ** attempt
                    except ValueError:
                        delay = 2 ** attempt
                    time.sleep(delay + random.uniform(0, 0.5))
            except Exception as exc:
                log.debug(f"Chain {symbol} error (attempt {attempt+1}): {exc}")
                if attempt < retries - 1:
                    time.sleep(2 ** attempt + random.uniform(0, 0.5))
        return []

    def get_quote_token(self):
        """Fetch a fresh DXLink auth token + WebSocket URL. Tokens are short-lived."""
        self._ensure_access_token()
        url = f"{BASE}/api-quote-tokens"
        log.info(f"GET {url}")
        try:
            r = self.session.get(url, timeout=12)
            log.info(f"Quote token {r.status_code}: {r.text[:500]}")
            if r.status_code == 200:
                data = r.json()['data']
                log.info(f"Token data keys: {list(data.keys())}")
                return data
            log.error(f"Quote token request failed: {r.status_code} {r.text[:200]}")
        except Exception as exc:
            log.error(f"get_quote_token exception: {exc}")
        return None


# ─── DXLink shared helpers ───────────────────────────────────────────────────

def _extract_token(data):
    for k in _TOKEN_KEYS:
        v = data.get(k, '')
        if v:
            log.info(f"DXLink token found under key='{k}' (length={len(str(v))})")
            return str(v)
    log.error(f"No token key found in token_data. Keys present: {list(data.keys())}")
    return ''


def _extract_url(data):
    for k in _URL_KEYS:
        v = data.get(k, '')
        if v:
            log.info(f"DXLink URL found under key='{k}': {v}")
            return str(v)
    log.error(f"No URL key found in token_data. Keys present: {list(data.keys())}")
    return ''


def _describe_ws_error(error):
    """Render whatever websocket-client caught as something a user can read.

    A clean server-side close arrives here as a raw ABNF frame whose `str()` is
    `fin=1 opcode=8 data=b'\\x03\\xe9'` — that must never reach the status badge.
    """
    if getattr(error, 'opcode', None) == 8:
        return "connection closed by server"
    text = str(error).strip()
    return text or error.__class__.__name__


def _safe_float(val):
    try:
        f = float(val)
        return 0.0 if (math.isnan(f) or math.isinf(f) or f < 0) else f
    except (TypeError, ValueError):
        return 0.0


def _apply_event(event_type, ev, quotes, touched):
    """Merge one decoded event into `quotes` (symbol -> Quote); record the
    symbol in `touched`."""
    sym = ev.get('eventSymbol')
    if not sym:
        return
    q = quotes.setdefault(sym, Quote())
    if event_type == 'Quote':
        q.merge(Quote(bid=_safe_float(ev.get('bidPrice')),
                      ask=_safe_float(ev.get('askPrice'))))
    elif event_type == 'Trade':
        q.merge(Quote(last=_safe_float(ev.get('price'))))
    else:
        return
    touched.add(sym)


def _parse_feed_data(raw_data, field_map, quotes):
    """Decode FEED_DATA — handles both COMPACT (array) and FULL (dict) formats.

    COMPACT payloads are flat value arrays, so `field_map` (from FEED_CONFIG)
    is required to name the columns; without it nothing can be decoded.
    Returns the set of symbols touched.
    """
    touched = set()
    if not isinstance(raw_data, list):
        return touched
    i = 0
    while i < len(raw_data):
        item = raw_data[i]
        if isinstance(item, str):
            event_type = item
            i += 1
            if i < len(raw_data) and isinstance(raw_data[i], list):
                values = raw_data[i]
                fields = field_map.get(event_type, [])
                if fields:
                    n = len(fields)
                    for j in range(0, len(values), n):
                        chunk = values[j:j + n]
                        if len(chunk) == n:
                            _apply_event(event_type, dict(zip(fields, chunk)),
                                         quotes, touched)
                i += 1
        elif isinstance(item, dict):
            etype = item.get('eventType') or item.get('type', '')
            _apply_event(etype, item, quotes, touched)
            i += 1
        else:
            i += 1
    return touched


# Symbols per FEED_SUBSCRIPTION frame. A full scan streams every result row at
# 3 symbols each, so one frame carrying the lot would be needlessly large.
SUBSCRIBE_CHUNK = 200


def _subscription_frames(quote_syms, trade_syms, chunk=SUBSCRIBE_CHUNK):
    """FEED_SUBSCRIPTION frames covering these symbols, chunked."""
    subs  = [{"type": "Quote", "symbol": s} for s in quote_syms]
    subs += [{"type": "Trade", "symbol": s} for s in trade_syms]
    return [
        json.dumps({"type": "FEED_SUBSCRIPTION", "channel": 1,
                    "add": subs[i:i + chunk]})
        for i in range(0, len(subs), chunk)
    ]


# ─── One-shot fetch (used by the scan) ───────────────────────────────────────

class _SocketRunnable(QRunnable):
    """Runs a WebSocketApp's blocking loop on the global QThreadPool."""

    def __init__(self, ws_app):
        super().__init__()
        self._ws_app = ws_app
        self.setAutoDelete(True)

    def run(self):
        try:
            self._ws_app.run_forever()
        except Exception as exc:
            log.error(f"DXLink socket loop crashed: {exc}")


def fetch_quotes_dxlink(token_data, symbols, timeout=25):
    """Open a DXLink socket, subscribe to Quote+Trade, return once every symbol
    is priced or `timeout` elapses.

    Returns ``(quotes, diag)`` where quotes maps symbol -> Quote.

    The server may emit ``AUTH_STATE: UNAUTHORIZED`` as an initial greeting that
    races with — and arrives after — our own AUTH frame, and there's no reliable
    way to tell that greeting apart from a real token rejection. So we never fail
    on UNAUTHORIZED; we wait for AUTHORIZED and let the timeout plus
    :func:`fetch_quotes_with_retry` handle a genuinely bad token.
    """
    if not token_data or not symbols:
        return {}, {'error': 'No token_data or symbols provided'}

    dxlink_url = _extract_url(token_data)
    token      = _extract_token(token_data)
    if not dxlink_url or not token:
        return {}, {'error': f'Missing URL or token. Keys in token_data: {list(token_data.keys())}'}

    quotes    = {}
    field_map = {}
    done      = QSemaphore(0)
    diag = {
        'connected': False, 'authorized': False,
        'channel_opened': False, 'error': None,
        'raw_msgs': [],   # first 10 raw messages — invaluable for debugging auth
    }

    option_syms = {s for s in symbols if s.startswith('.') or '/' in s}
    equity_syms = {s for s in symbols if s not in option_syms}

    def has_sufficient_data():
        eq_ok  = all(quotes.get(s, Quote()).price > 0 for s in equity_syms)
        opt_ok = all(quotes.get(s, Quote()).bid   > 0 for s in option_syms)
        return eq_ok and opt_ok

    def on_open(ws):
        diag['connected'] = True
        log.debug("DXLink WS opened — sending SETUP")
        ws.send(json.dumps({
            "type": "SETUP", "channel": 0,
            "keepaliveTimeout": 60, "acceptKeepaliveTimeout": 60,
            "version": "0.1",
        }))

    def on_message(ws, message):
        try:
            if len(diag['raw_msgs']) < 10:
                diag['raw_msgs'].append(message[:400])
                log.debug(f"DXLink raw #{len(diag['raw_msgs'])}: {message[:400]}")

            data  = json.loads(message)
            mtype = data.get('type')

            if mtype == 'SETUP':
                log.debug("DXLink received SETUP ack — sending AUTH")
                ws.send(json.dumps({"type": "AUTH", "channel": 0, "token": token}))

            elif mtype == 'AUTH_STATE':
                state = data.get('state', '')
                log.info(f"DXLink AUTH_STATE={state!r}")
                if state == 'AUTHORIZED':
                    diag['authorized'] = True
                    log.debug("Authorized — requesting FEED channel")
                    ws.send(json.dumps({
                        "type": "CHANNEL_REQUEST", "channel": 1,
                        "service": "FEED", "parameters": {"contract": "AUTO"},
                    }))
                else:
                    log.debug(f"AUTH_STATE={state!r} — waiting for AUTHORIZED")

            elif mtype == 'CHANNEL_OPENED' and data.get('channel') == 1:
                diag['channel_opened'] = True
                log.debug("CHANNEL_OPENED — sending FEED_SETUP + subscriptions")
                ws.send(json.dumps(_FEED_SETUP))
                for frame in _subscription_frames(symbols, equity_syms):
                    ws.send(frame)
                log.debug(f"Subscribed across {len(symbols)} symbols")

            elif mtype == 'FEED_CONFIG' and data.get('channel') == 1:
                for etype, fields in data.get('eventFields', {}).items():
                    field_map[etype] = fields
                log.debug(f"FEED_CONFIG field_map keys: {list(field_map.keys())}")

            elif mtype == 'FEED_DATA' and data.get('channel') == 1:
                _parse_feed_data(data.get('data', []), field_map, quotes)
                if symbols and has_sufficient_data():
                    log.debug(f"All data received — {len(quotes)} symbols priced")
                    done.release()

            elif mtype == 'KEEPALIVE':
                ws.send(json.dumps({"type": "KEEPALIVE", "channel": 0}))

            elif mtype == 'ERROR':
                diag['error'] = f"Server ERROR: {data.get('message', data)}"
                log.error(f"DXLink server ERROR: {data}")
                done.release()

        except Exception as exc:
            diag['error'] = f'on_message exception: {exc}'
            log.error(f"DXLink on_message exception: {exc}")

    def on_error(ws, error):
        diag['error'] = str(error)
        log.error(f"DXLink WS error: {error}")
        done.release()

    def on_close(ws, code, msg):
        log.debug(f"DXLink WS closed: code={code}")
        done.release()

    ws_app = websocket.WebSocketApp(
        dxlink_url,
        on_open=on_open, on_message=on_message,
        on_error=on_error, on_close=on_close,
    )
    QThreadPool.globalInstance().start(_SocketRunnable(ws_app))
    done.tryAcquire(1, int(timeout * 1000))

    try:
        ws_app.close()
    except Exception:
        pass

    n_priced = sum(1 for q in quotes.values() if q.bid > 0 or q.last > 0)
    log.info(
        f"DXLink session done: {n_priced}/{len(symbols)} priced  "
        f"connected={diag['connected']} authorized={diag['authorized']} "
        f"channel={diag['channel_opened']} error={diag['error']!r}"
    )
    return quotes, diag


def fetch_quotes_batched(token_data, symbols, batch_size=200, timeout=25):
    """Batch DXLink requests (200 symbols per connection); returns merged quotes
    plus the per-batch diagnostics."""
    all_quotes, all_diags = {}, []
    sym_list  = list(symbols)
    n_batches = math.ceil(len(sym_list) / batch_size) if sym_list else 0
    for i in range(0, len(sym_list), batch_size):
        batch = sym_list[i:i + batch_size]
        log.info(f"DXLink batch {i // batch_size + 1}/{n_batches}: {len(batch)} symbols")
        q, d = fetch_quotes_dxlink(token_data, batch, timeout=timeout)
        all_quotes.update(q)
        all_diags.append(d)
    return all_quotes, all_diags


def fetch_quotes_with_retry(api, symbols, batch_size=200, timeout=25, max_retries=2):
    """Fetch quotes, refreshing the (short-lived) DXLink token and retrying when
    a batch connects but never reaches AUTHORIZED.

    Returns ``(quotes, diags)``.
    """
    last_result, last_diags = {}, []
    for attempt in range(max_retries + 1):
        token_data = api.get_quote_token()
        if not token_data:
            log.error(f"Could not get quote token (attempt {attempt + 1})")
            return {}, [{'error': 'Could not get quote token'}]

        result, diags = fetch_quotes_batched(token_data, symbols, batch_size, timeout)
        last_result, last_diags = result, diags

        # Covers both explicit UNAUTHORIZED errors and silent auth timeouts.
        auth_failed = any(
            isinstance(d, dict) and d.get('connected') and not d.get('authorized')
            for d in diags
        )
        if not auth_failed:
            return result, diags

        if attempt < max_retries:
            log.warning(
                f"DXLink auth did not complete on attempt {attempt + 1}/{max_retries + 1} — "
                f"refreshing token and retrying in 2 s"
            )
            time.sleep(2)
        else:
            log.error(f"DXLink auth never completed after {max_retries + 1} attempts")

    return last_result, last_diags


def describe_diags(diags):
    """Human-readable summary of why a batch produced no prices."""
    parts = []
    for d in diags:
        if not isinstance(d, dict):
            continue
        if d.get('error'):                parts.append(f"WS error: {d['error']}")
        elif not d.get('connected'):      parts.append("WS did not connect")
        elif not d.get('authorized'):     parts.append("WS connected but auth failed")
        elif not d.get('channel_opened'): parts.append("channel did not open")
        else:                             parts.append("WS ok but no prices")
    return '; '.join(parts) if parts else 'timeout with no data'


# ─── Persistent streaming client ─────────────────────────────────────────────

class DXLinkLiveClient(QThread):
    """Persistent DXLink connection that streams quotes until stopped.

    Lifecycle is fully signal-driven — nothing blocks the GUI:

        client = DXLinkLiveClient(api)
        client.ready.connect(...)          # safe to subscribe() from here on
        client.quotesUpdated.connect(...)  # {symbol: Quote}, queued to the GUI thread
        client.connectionFailed.connect(...)
        client.start()

    The socket loop owns this QThread; `subscribe()` and the keepalive are
    called from the GUI thread, which is safe because websocket-client guards
    frame writes with its own lock.
    """

    ready            = Signal()
    connectionFailed = Signal(str)
    quotesUpdated    = Signal(dict)
    disconnected     = Signal()

    def __init__(self, api, parent=None):
        super().__init__(parent)
        self._api   = api
        self._ws    = None
        self._token = None
        self._url   = None
        self._field_map = {}
        self._quotes = {}                # WS-thread-only cache of merged quotes
        self._subscribed_quote = set()
        self._subscribed_trade = set()
        self._pending = []               # subscriptions requested before the channel opened
        self._channel_open = False
        self._sub_mutex = QMutex()
        self._stopping = False

        # DXLink negotiates a 60s KEEPALIVE timeout in SETUP; miss it and the
        # server drops us with 'TIMEOUT The timeout for KEEPALIVE has been
        # reached'. The timer lives on the GUI thread — no extra thread needed.
        self._keepalive = QTimer(self)
        self._keepalive.setInterval(KEEPALIVE_MS)
        self._keepalive.timeout.connect(self._send_keepalive)
        self.ready.connect(self._keepalive.start)

    # ── thread body ──

    def run(self):
        token_data = self._api.get_quote_token()
        if not token_data:
            self.connectionFailed.emit("Could not get DXLink quote token")
            return
        self._url   = _extract_url(token_data)
        self._token = _extract_token(token_data)
        if not self._url or not self._token:
            self.connectionFailed.emit(
                f"Missing DXLink URL or token: keys={list(token_data.keys())}")
            return

        self._ws = websocket.WebSocketApp(
            self._url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        try:
            self._ws.run_forever()
        except Exception as exc:
            log.error(f"DXLinkLive socket loop crashed: {exc}")
            self.connectionFailed.emit(str(exc))
        finally:
            self.disconnected.emit()

    # ── socket callbacks (WS thread) ──

    def _on_open(self, ws):
        log.debug("DXLinkLive: WS open — sending SETUP")
        ws.send(json.dumps({
            "type": "SETUP", "channel": 0,
            "keepaliveTimeout": 60, "acceptKeepaliveTimeout": 60,
            "version": "0.1",
        }))

    def _on_message(self, ws, message):
        try:
            data  = json.loads(message)
            mtype = data.get('type')
            if mtype == 'SETUP':
                ws.send(json.dumps({"type": "AUTH", "channel": 0, "token": self._token}))
            elif mtype == 'AUTH_STATE':
                if data.get('state') == 'AUTHORIZED':
                    ws.send(json.dumps({
                        "type": "CHANNEL_REQUEST", "channel": 1,
                        "service": "FEED", "parameters": {"contract": "AUTO"},
                    }))
            elif mtype == 'CHANNEL_OPENED' and data.get('channel') == 1:
                ws.send(json.dumps(_FEED_SETUP))
                self._open_channel()
            elif mtype == 'FEED_CONFIG' and data.get('channel') == 1:
                self._install_field_map(data.get('eventFields', {}))
            elif mtype == 'FEED_DATA' and data.get('channel') == 1:
                touched = _parse_feed_data(data.get('data', []), self._field_map,
                                           self._quotes)
                if touched:
                    self.quotesUpdated.emit({
                        s: Quote(self._quotes[s].bid, self._quotes[s].ask,
                                 self._quotes[s].last)
                        for s in touched
                    })
            elif mtype == 'KEEPALIVE':
                ws.send(json.dumps({"type": "KEEPALIVE", "channel": 0}))
            elif mtype == 'ERROR':
                log.error(f"DXLinkLive server ERROR: {data}")
        except Exception as exc:
            log.error(f"DXLinkLive on_message exception: {exc}")

    def _on_error(self, ws, error):
        log.error(f"DXLinkLive WS error: {error!r}")
        if not self._stopping:
            self.connectionFailed.emit(_describe_ws_error(error))

    def _on_close(self, ws, code, msg):
        log.debug(f"DXLinkLive WS closed: code={code}")

    # ── subscriptions (GUI thread) ──

    def subscribe(self, symbols, with_trade_for=None):
        """Subscribe to Quote events for `symbols`, and Trade events for those
        also in `with_trade_for` (typically the equity set).

        Calls made before the feed channel is open are queued and flushed by
        `_open_channel`.
        """
        if not symbols or self._stopping:
            return
        with_trade_for = set(with_trade_for or ())
        with QMutexLocker(self._sub_mutex):
            new_q = [s for s in symbols if s not in self._subscribed_quote]
            new_t = [s for s in symbols
                     if s in with_trade_for and s not in self._subscribed_trade]
            if not new_q and not new_t:
                return
            self._subscribed_quote.update(new_q)
            self._subscribed_trade.update(new_t)
            if not self._channel_open:
                self._pending.append((new_q, new_t))
                return
        self._send_subscription(new_q, new_t)

    def _open_channel(self):
        """FEED_SETUP is away, so subscriptions may now be sent; drain the queue.

        Deliberately *not* gated on FEED_CONFIG. The server announces the COMPACT
        field ordering for an event type only once something is subscribed to that
        type — the first FEED_CONFIG carries no `eventFields` at all — so waiting
        for a populated field map before subscribing deadlocks: we wait for fields
        the server will never send until we subscribe. Decoding stays safe because
        the populated FEED_CONFIG always precedes the FEED_DATA it describes.

        Draining under the mutex means a concurrent subscribe() either queues (and
        is flushed here) or sends directly — never both, never neither.
        """
        with QMutexLocker(self._sub_mutex):
            self._channel_open = True
            pending, self._pending = self._pending, []
        for new_q, new_t in pending:
            self._send_subscription(new_q, new_t)
        self.ready.emit()

    def _install_field_map(self, event_fields):
        """Merge one FEED_CONFIG's field ordering.

        These arrive incrementally — an initial config with no `eventFields`,
        then one per event type as subscriptions are made — so this merges rather
        than replaces, and an empty payload is normal rather than a failure.
        Touched only from the socket thread, so it needs no lock.
        """
        if not event_fields:
            log.debug("DXLinkLive FEED_CONFIG carried no eventFields (expected "
                      "before the first subscription)")
            return
        for etype, fields in event_fields.items():
            self._field_map[etype] = fields
        log.debug(f"DXLinkLive FEED_CONFIG field_map keys: {list(self._field_map.keys())}")

    def _send_subscription(self, new_q, new_t):
        try:
            for frame in _subscription_frames(new_q, new_t):
                self._ws.send(frame)
        except Exception as exc:
            log.warning(f"DXLinkLive subscribe failed: {exc}")
            return
        log.info(f"DXLinkLive subscribed: +{len(new_q)} Quote, +{len(new_t)} Trade "
                 f"(total {len(self._subscribed_quote)} Quote / "
                 f"{len(self._subscribed_trade)} Trade)")

    def _send_keepalive(self):
        if self._stopping or self._ws is None:
            return
        try:
            self._ws.send(json.dumps({"type": "KEEPALIVE", "channel": 0}))
        except Exception as exc:
            log.warning(f"DXLinkLive keepalive send failed: {exc}")
            self._keepalive.stop()

    # ── teardown ──

    def stop(self, wait_ms=3000):
        if self._stopping:
            return
        self._stopping = True
        self._keepalive.stop()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
        self.wait(wait_ms)
        log.info("DXLinkLiveClient closed")
