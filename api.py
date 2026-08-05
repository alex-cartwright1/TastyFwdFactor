"""Tastytrade production REST API and the DXLink WebSocket quote feed.

Read-only: nothing here places orders.

Three Tastytrade clients live in this module:

* :class:`TastytradeAPI` — the hand-rolled ``requests`` client used for option
  chains and quote tokens.
* :class:`MarketDataSession` — the official ``tastytrade`` SDK, which is async
  (httpx + asyncio). It supplies the reference data the app used to scrape from
  yfinance: market cap, earnings dates, ex-dividend dates and the equity market
  calendar. It owns a private asyncio loop on its own thread so callers get a
  plain blocking API and the GUI thread never sees a coroutine.
* the DXLink feed below, unchanged — streaming quotes still come from DXLink.

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

import asyncio
import json
import math
import random
import time
from datetime import date

import requests
import websocket
from PySide6.QtCore import (
    QMutex, QMutexLocker, QRunnable, QSemaphore, QThread, QThreadPool,
    QTimer, Signal,
)
from tastytrade import Session as SdkSession
from tastytrade.market_sessions import ExchangeType, get_market_holidays
from tastytrade.metrics import get_market_metrics

from applog import log
from data_models import MarketCalendar, Quote, TickerInfo
from qtpool import parallel_map

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


# ─── Option chain cache ──────────────────────────────────────────────────────

# In-memory only, and capped. A nested chain is a large JSON blob (hundreds of
# strikes across dozens of expirations), so caching a full 5,000-ticker sweep
# would cost more memory than the scan saves in time. The cap keeps the entries
# that actually get reused — the survivors of the pre-filter, and whatever the
# user is searching — and evicts least-recently-used beyond that.
CHAIN_CACHE_MAX = 500


class ChainCache:
    """Thread-safe TTL + LRU cache of nested option chains, keyed by ticker.

    Every scan worker and the single-ticker search share one instance through
    `TastytradeAPI`, and `parallel_map` hits it from ten threads at once, so all
    access is under one mutex. Entries carry the fetch timestamp and the
    expiration dates present in the payload, which is what makes a stale entry
    identifiable in the log rather than just silently replaced.
    """

    def __init__(self, max_entries=CHAIN_CACHE_MAX):
        self._mutex   = QMutex()
        self._entries = {}          # symbol -> {'chain', 'fetched_at', 'expirations'}
        self._order   = []          # symbols, least-recently-used first
        self._max     = max_entries
        self.hits     = 0
        self.misses   = 0

    @staticmethod
    def _expirations(chain):
        try:
            return [e.get('expiration-date') for e in chain[0].get('expirations', [])]
        except (IndexError, AttributeError, TypeError):
            return []

    def get(self, symbol, ttl_secs):
        """Cached chain for `symbol`, or None on miss/expiry. `ttl_secs <= 0`
        disables the cache entirely."""
        if ttl_secs <= 0:
            return None
        with QMutexLocker(self._mutex):
            entry = self._entries.get(symbol)
            if entry is None:
                self.misses += 1
                return None
            if time.time() - entry['fetched_at'] >= ttl_secs:
                # Expired: drop it now so a failed refetch doesn't keep serving
                # a chain whose front expiration may already have passed.
                self._entries.pop(symbol, None)
                self._discard_order(symbol)
                self.misses += 1
                return None
            self._touch(symbol)
            self.hits += 1
            return entry['chain']

    def put(self, symbol, chain):
        if not chain:
            return
        with QMutexLocker(self._mutex):
            self._entries[symbol] = {
                'chain':       chain,
                'fetched_at':  time.time(),
                'expirations': self._expirations(chain),
            }
            self._touch(symbol)
            while len(self._order) > self._max:
                evicted = self._order.pop(0)
                self._entries.pop(evicted, None)

    def invalidate(self, symbols):
        """Drop specific tickers — used when their fundamentals are refreshed,
        since a corporate action that moves an earnings date can also add or
        remove expirations."""
        with QMutexLocker(self._mutex):
            dropped = 0
            for sym in symbols:
                if self._entries.pop(sym, None) is not None:
                    self._discard_order(sym)
                    dropped += 1
        return dropped

    def clear(self):
        with QMutexLocker(self._mutex):
            n = len(self._entries)
            self._entries.clear()
            self._order.clear()
            self.hits = self.misses = 0
        return n

    def stats(self):
        with QMutexLocker(self._mutex):
            return {'entries': len(self._entries),
                    'hits': self.hits, 'misses': self.misses}

    # Both helpers assume the caller holds the mutex.

    def _touch(self, symbol):
        self._discard_order(symbol)
        self._order.append(symbol)

    def _discard_order(self, symbol):
        try:
            self._order.remove(symbol)
        except ValueError:
            pass


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
        # Shared by the scan pool and the single-ticker search — one instance per
        # API session, so a search warms the cache the next scan reads.
        self.chains = ChainCache()
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

    def get_option_chain(self, symbol, retries=5, timeout=30, cache_ttl=0):
        """Nested option chain for `symbol`, served from `self.chains` when a
        live entry exists. `cache_ttl` is in seconds; 0 bypasses the cache."""
        cached = self.chains.get(symbol, cache_ttl)
        if cached is not None:
            return cached

        # Jitter before the request, not before the cache lookup: it exists to
        # desync the ten pool workers so they don't all hit the rate limit on the
        # same instant, and a cache hit does no I/O to stagger.
        time.sleep(random.uniform(0.05, 0.15))
        self._ensure_access_token()
        url = f"{BASE}/option-chains/{symbol}/nested"
        for attempt in range(retries):
            try:
                r = self.session.get(url, timeout=timeout)
                if r.status_code == 200:
                    chain = r.json()['data']['items']
                    self.chains.put(symbol, chain)
                    return chain
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


# ─── tastytrade SDK (reference data) ─────────────────────────────────────────

# /market-metrics takes a comma-separated symbol list. 100 is the largest chunk
# that stays comfortably inside the URL length limit and the endpoint's own cap;
# a full 5,000-ticker watchlist is therefore ~50 requests rather than 5,000.
METRICS_CHUNK = 100

# Concurrent in-flight chunks. The endpoint is rate limited per session, and 8
# has been the sweet spot: enough to saturate the link, few enough that a full
# watchlist sweep doesn't start collecting 429s.
METRICS_CONCURRENCY = 8

# How long a blocking SDK call may take before the caller gives up. Generous,
# because a cold 5,000-ticker sweep is 50 sequential-ish round trips.
SDK_CALL_TIMEOUT = 300


class MarketDataSession:
    """Blocking façade over the async `tastytrade` SDK.

    The SDK is httpx/asyncio all the way down, but every consumer in this app is
    either a `QThread` or `parallel_map`. Rather than sprinkle `asyncio.run` at
    the call sites — which would build (and re-authenticate) a fresh HTTP client
    per call — this owns one long-lived event loop on a private thread, and one
    `tastytrade.Session` living on it.

    Thread affinity: the loop thread is the only thread that ever touches the
    SDK session or its httpx client, which is what makes the whole thing safe to
    share. `call()` hands a coroutine over and blocks, so — exactly like
    `qtpool.parallel_map` — **it may only be called from a worker thread**.
    """

    def __init__(self, client_secret, refresh_token):
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._loop     = None
        self._session  = None
        self._ready    = QSemaphore(0)
        self._thread   = QThread()
        self._thread.run = self._run_loop        # no subclass needed for a body this small
        self._thread.start()
        # The loop must exist before anything can be submitted to it.
        if not self._ready.tryAcquire(1, 10_000):
            raise RuntimeError("tastytrade SDK event loop failed to start")

    # ── loop lifecycle ──

    def _run_loop(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.release()
        try:
            loop.run_forever()
        finally:
            try:
                loop.run_until_complete(self._aclose())
            except Exception as exc:
                log.debug(f"SDK session close failed: {exc}")
            loop.close()
            log.debug("tastytrade SDK event loop stopped")

    async def _aclose(self):
        if self._session is not None:
            await self._session._client.aclose()
            self._session = None

    async def _ensure_session(self):
        """Build the SDK session lazily, on the loop thread.

        `Session.__init__` does no I/O but does construct an httpx `AsyncClient`,
        which binds to whatever loop is running when it is first used — so it has
        to be created here, not in `__init__`.
        """
        if self._session is None:
            self._session = SdkSession(provider_secret=self._client_secret,
                                       refresh_token=self._refresh_token)
            log.info("tastytrade SDK session created (OAuth refresh grant)")
        return self._session

    def call(self, coro_factory, timeout=SDK_CALL_TIMEOUT):
        """Run `coro_factory(session)` on the loop thread and return its result.

        Blocks the calling thread — worker threads only, never the GUI thread.
        """
        async def _wrapped():
            return await coro_factory(await self._ensure_session())

        # run_coroutine_threadsafe hands back a concurrent.futures.Future. It is
        # the sanctioned asyncio↔thread bridge and the only place in the app that
        # sees one; it never leaves this method.
        future = asyncio.run_coroutine_threadsafe(_wrapped(), self._loop)
        return future.result(timeout)

    def close(self):
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.quit()
        self._thread.wait(5000)

    # ── reference data ──

    def fetch_ticker_info(self, symbols, progress_cb=None, should_cancel=None):
        """Market cap / next earnings / next ex-dividend for many symbols.

        Returns ``{symbol: TickerInfo}``, omitting symbols the endpoint doesn't
        know (delisted tickers, non-equities). Chunks are issued concurrently
        under a semaphore; one failing chunk is logged and skipped rather than
        sinking the whole sweep, because a single bad symbol in a 5,000-line
        watchlist should not abort a scan.

        If *every* chunk fails this raises instead, since that means the session
        is broken rather than the data being thin — and the caller caches what it
        gets, so silently returning nothing would poison the ticker cache with
        blanks for the whole TTL.
        """
        symbols = list(symbols)
        if not symbols:
            return {}
        chunks = [symbols[i:i + METRICS_CHUNK]
                  for i in range(0, len(symbols), METRICS_CHUNK)]
        today  = date.today()

        async def _run(session):
            gate = asyncio.Semaphore(METRICS_CONCURRENCY)
            out  = {}
            done = failed = 0
            last_error = None

            async def _one(chunk):
                nonlocal done, failed, last_error
                if should_cancel and should_cancel():
                    return
                async with gate:
                    try:
                        metrics = await get_market_metrics(session, chunk)
                    except Exception as exc:
                        log.debug(f"market-metrics chunk of {len(chunk)} failed: {exc}")
                        failed += 1
                        last_error = exc
                        metrics = []
                for m in metrics:
                    out[m.symbol] = TickerInfo.from_metric(m, today)
                done += len(chunk)
                if progress_cb:
                    progress_cb(done, len(symbols))

            await asyncio.gather(*(_one(c) for c in chunks))
            if failed:
                log.warning(f"market-metrics: {failed}/{len(chunks)} chunks failed")
            if failed == len(chunks):
                raise RuntimeError(
                    f"Tastytrade market metrics unavailable: {last_error}")
            return out

        return self.call(_run)

    def fetch_market_calendar(self):
        """US equity holidays + half days. Returns None if the call fails."""
        async def _run(session):
            cal = await get_market_holidays(session)
            return MarketCalendar(holidays=list(cal.holidays),
                                  half_days=list(cal.half_days))
        try:
            return self.call(_run, timeout=30)
        except Exception as exc:
            log.warning(f"Market calendar fetch failed: {exc}")
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

# Concurrent one-shot DXLink connections. Each quote token supports a limited
# number of streamer sessions, and every socket costs a thread parked on a
# semaphore, so this is deliberately well under the batch count — the win is
# hiding one batch's timeout behind another's, not opening 25 sockets.
QUOTE_SOCKETS = 4


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


def fetch_quotes_batched(token_data, symbols, batch_size=200, timeout=25,
                         max_sockets=QUOTE_SOCKETS):
    """Batch DXLink requests (200 symbols per connection); returns merged quotes
    plus the per-batch diagnostics.

    Batches run **concurrently**. Each one blocks for as long as its slowest
    symbol takes to price — up to the full timeout when a batch contains an
    illiquid contract that never ticks — so running 25 batches in sequence cost
    the scan minutes of dead wall-clock. They are independent connections with
    disjoint symbol sets, so there is nothing to serialise.

    `fetch_quotes_dxlink` blocks its caller and runs its socket loop on the
    *global* QThreadPool, while `parallel_map` uses a private pool; the two pools
    are what keep this from deadlocking against itself.
    """
    sym_list  = list(symbols)
    if not sym_list:
        return {}, []
    batches   = [sym_list[i:i + batch_size]
                 for i in range(0, len(sym_list), batch_size)]
    n_batches = len(batches)

    if n_batches == 1:
        # Don't pay for a thread pool to run one socket.
        q, d = fetch_quotes_dxlink(token_data, batches[0], timeout=timeout)
        return q, [d]

    workers = max(1, min(max_sockets, n_batches))
    log.info(f"DXLink: {len(sym_list)} symbols in {n_batches} batches, "
             f"{workers} sockets in parallel")

    def _one(batch):
        return fetch_quotes_dxlink(token_data, batch, timeout=timeout)

    all_quotes, all_diags = {}, []
    for batch, result in parallel_map(_one, batches, max_workers=workers):
        if isinstance(result, Exception):
            log.error(f"DXLink batch of {len(batch)} raised: {result}")
            all_diags.append({'error': str(result)})
            continue
        quotes, diag = result
        all_quotes.update(quotes)
        all_diags.append(diag)
    return all_quotes, all_diags


def fetch_quotes_with_retry(api, symbols, batch_size=200, timeout=25, max_retries=2):
    """Fetch quotes, refreshing the (short-lived) DXLink token and retrying when
    a batch connects but never reaches AUTHORIZED.

    Returns ``(quotes, diags)``.

    Symbols are de-duplicated first. Setups routinely share legs — two rows on
    the same ticker and strike, or a position whose back leg is another row's
    front leg — and paying for the same contract twice inflates both the batch
    count and the time each batch waits to be fully priced.
    """
    symbols = list(dict.fromkeys(symbols))       # dedupe, order preserved
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
