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

Quotes come off **one** DXLink connection, owned by :class:`QuoteStream`:

* :class:`QuoteStream` — the single persistent connection, shared by the scan
  pipeline and both live tables. Consumers register a named symbol set; the
  union of those sets is what is actually subscribed on the wire, and the
  handshake happens once per app session rather than once per batch.
* :class:`QuoteStore` — the thread-safe symbol → :class:`Quote` map the socket
  thread writes into. Scan phases block on :meth:`QuoteStore.wait_for` rather
  than on a socket teardown.
* :class:`DXLinkLiveClient` — the ``QThread`` that owns one socket. Built and
  rebuilt by ``QuoteStream``; not used directly any more.
* :func:`fetch_quotes_with_retry` — the legacy one-shot batched fetch. It is now
  only a **fallback** for when the persistent stream can't be reached at all, so
  a scan still completes on a degraded connection.

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
import zlib
from datetime import date, datetime, timezone

import requests
from requests.adapters import HTTPAdapter
import websocket
from PySide6.QtCore import (
    QMetaObject, QMutex, QMutexLocker, QObject, QRunnable, QSemaphore, Qt,
    QThread, QThreadPool, QTimer, QWaitCondition, Signal, Slot,
)
from tastytrade import Session as SdkSession
from tastytrade.market_sessions import ExchangeType, get_market_holidays
from tastytrade.metrics import get_market_metrics

from applog import log
from config import load_chain_cache, save_chain_cache
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


# ─── Request pacing ──────────────────────────────────────────────────────────

# Tastytrade rate-limits the chain endpoint hard enough that 35 unpaced workers
# trip it inside a second. The old fixed 50–150 ms per-request jitter smoothed
# the arrival rate by accident; removing it removed the only thing keeping the
# fan-out civil, so this replaces it with something that actually models the
# constraint — an aggregate request rate, adapted to what the server will take.
# Tuned against a simulated gateway: a ×0.5 cut with a +1-per-25 recovery
# undershot the server's real limit by ~3x and stayed there, because the cut
# compounds across bursts far faster than the probe climbs back. A gentler cut
# and a quicker probe settle just under the true limit instead.
CHAIN_RATE_START = 12.0     # requests/sec at the start of a sweep
CHAIN_RATE_MIN   = 1.0
CHAIN_RATE_MAX   = 50.0
RATE_PROBE_AFTER = 10       # successes before nudging the rate back up
RATE_STEP_UP     = 1.0      # additive increase (req/s)
RATE_CUT         = 0.7      # multiplicative decrease on a 429
RATE_CUT_WINDOW  = 1.0      # at most one cut per this many seconds
RATE_PAUSE_SECS  = 1.0      # global pause on a 429 with no Retry-After


class RateLimiter:
    """Shared AIMD pacer for the chain fan-out.

    Two things make this different from the per-request sleep it replaces:

    * it bounds the **aggregate** arrival rate rather than each request's delay,
      so raising the worker count no longer raises the load on the server; and
    * a 429 pauses **every** worker. Previously each worker backed off alone
      while the other 34 kept hammering, which is why one 429 turned into 942 —
      the fan-out could never get out of the hole it had dug.

    Additive-increase / multiplicative-decrease means the sweep self-tunes to
    whatever the account's real limit is instead of hard-coding a guess.
    """

    def __init__(self, rate=CHAIN_RATE_START,
                 min_rate=CHAIN_RATE_MIN, max_rate=CHAIN_RATE_MAX):
        self._mutex     = QMutex()
        self._rate      = float(rate)
        self._min       = float(min_rate)
        self._max       = float(max_rate)
        self._next_slot = 0.0    # monotonic time the next request may go out
        self._paused_to = 0.0    # global cooldown deadline after a 429
        self._last_cut  = 0.0
        self._ok        = 0
        self.throttles  = 0

    def acquire(self):
        """Block the calling worker until its slot comes up.

        Handing out a monotonically advancing slot cursor — rather than each
        thread sleeping a random interval — is what keeps 35 workers evenly
        spaced instead of arriving in clumps, and it makes the wait fair.
        """
        with QMutexLocker(self._mutex):
            now  = time.monotonic()
            slot = max(now, self._next_slot, self._paused_to)
            self._next_slot = slot + 1.0 / self._rate
            delay = slot - now
        if delay > 0:
            time.sleep(delay)

    def throttled(self, retry_after=None):
        """Record a 429: pause everyone, and cut the rate at most once per burst."""
        with QMutexLocker(self._mutex):
            now = time.monotonic()
            self.throttles += 1
            # 35 workers can each catch a 429 from the same overload. Cutting per
            # report would collapse the rate to the floor on a single burst.
            if now - self._last_cut >= RATE_CUT_WINDOW:
                self._rate     = max(self._min, self._rate * RATE_CUT)
                self._last_cut = now
                log.debug(f"Chain rate limit hit — pacing down to "
                          f"{self._rate:.1f} req/s")
            pause = retry_after if retry_after and retry_after > 0 else RATE_PAUSE_SECS
            self._paused_to = max(self._paused_to, now + pause)
            self._ok = 0

    def succeeded(self):
        with QMutexLocker(self._mutex):
            self._ok += 1
            if self._ok >= RATE_PROBE_AFTER and self._rate < self._max:
                self._ok   = 0
                self._rate = min(self._max, self._rate + RATE_STEP_UP)

    def stats(self):
        with QMutexLocker(self._mutex):
            return {'rate': round(self._rate, 1), 'throttles': self.throttles}


# ─── Option chain cache ──────────────────────────────────────────────────────

# Entries are stored compressed (see `ChainCache._pack`), which is what makes a
# cap this size affordable: measured against realistic /nested payloads a chain
# compresses ~87x, so a typical ticker costs ~10 KiB and even an SPY-class chain
# ~50 KiB. A full 5,285-ticker watchlist is therefore ~50–280 MB rather than the
# ~2.8–15 GB the parsed structures would occupy. At 500 — the old cap, sized for
# the survivors of a market-cap pre-filter — an unfiltered sweep evicted ~90% of
# its own results before the next scan could read them.
CHAIN_CACHE_MAX = 6000

# Fields the app actually consumes: `scanner.get_chain_info` and
# `resolve_position_legs` read the expiration date, the strike price and the call
# streamer symbol, and nothing else. The /nested payload also carries put
# symbols, OCC symbols and settlement metadata, which is most of its bulk.
#
# The compaction happens in `get_option_chain`, on the miss path too, so a cache
# hit and a cache miss return exactly the same shape — a cache that quietly
# returned less than the live call would be a trap for the next consumer.
# `strike-price` is preserved **verbatim as a string**: Phase 3a matches the back
# leg to the front by string equality, so normalising it to a float would break
# the ATM lookup.


def compact_chain(items):
    """Strip a /nested chain to the fields the app reads."""
    out = []
    for root in items or ():
        expirations = []
        for exp in root.get('expirations', ()):
            strikes = []
            for s in exp.get('strikes', ()):
                sym = s.get('call-streamer-symbol') or s.get('call', '')
                strikes.append({'strike-price': s.get('strike-price'),
                                'call-streamer-symbol': sym})
            expirations.append({'expiration-date': exp.get('expiration-date'),
                                'strikes': strikes})
        out.append({'expirations': expirations})
    return out


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
        self._entries = {}          # symbol -> {'blob', 'fetched_at', 'expirations'}
        self._order   = []          # symbols, least-recently-used first
        self._max     = max_entries
        self.hits     = 0
        self.misses   = 0
        self.evictions = 0
        self.bytes     = 0
        # Set by anything that changes the contents, cleared by export_entries,
        # so shutdown after a fully-cached scan writes nothing.
        self._dirty    = False
        # True once the on-disk cache has been folded in. Until then this
        # instance holds only what *this* session fetched, so writing it out
        # verbatim would delete every other chain the file already has.
        self._loaded   = False

    # Compression level 1: the payload is highly repetitive JSON, so level 1
    # already gets ~87x and costs a fraction of what level 6 does — this runs
    # inside the fan-out, once per fetched ticker.
    @staticmethod
    def _pack(chain):
        return zlib.compress(json.dumps(chain).encode('utf-8'), 1)

    @staticmethod
    def _unpack(blob):
        return json.loads(zlib.decompress(blob))

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
                entry = self._entries.pop(symbol, None)
                if entry is not None:
                    self.bytes -= len(entry['blob'])
                self._discard_order(symbol)
                self._dirty = True
                self.misses += 1
                return None
            self._touch(symbol)
            self.hits += 1
            blob = entry['blob']
        # Decompressed outside the mutex — ~0.4 ms for a typical chain, and the
        # fan-out hits this from dozens of threads at once.
        return self._unpack(blob)

    def put(self, symbol, chain):
        if not chain:
            return
        blob = self._pack(chain)
        expirations = self._expirations(chain)
        with QMutexLocker(self._mutex):
            old = self._entries.get(symbol)
            if old is not None:
                self.bytes -= len(old['blob'])
            self._entries[symbol] = {
                'blob':        blob,
                'fetched_at':  time.time(),
                'expirations': expirations,
            }
            self.bytes += len(blob)
            self._dirty = True
            self._touch(symbol)
            while len(self._order) > self._max:
                evicted = self._order.pop(0)
                dropped = self._entries.pop(evicted, None)
                if dropped is not None:
                    self.bytes -= len(dropped['blob'])
                self.evictions += 1
                self._dirty = True

    def invalidate(self, symbols):
        """Drop specific tickers — used when their fundamentals are refreshed,
        since a corporate action that moves an earnings date can also add or
        remove expirations."""
        with QMutexLocker(self._mutex):
            dropped = 0
            for sym in symbols:
                entry = self._entries.pop(sym, None)
                if entry is not None:
                    self.bytes -= len(entry['blob'])
                    self._discard_order(sym)
                    dropped += 1
            if dropped:
                self._dirty = True
        return dropped

    def clear(self):
        with QMutexLocker(self._mutex):
            n = len(self._entries)
            self._entries.clear()
            self._order.clear()
            self.hits = self.misses = self.evictions = self.bytes = 0
            self._dirty = True
            # A wipe is deliberate, so the next save must not merge the file's
            # entries back in — this instance is now the whole truth.
            self._loaded = True
        return n

    def stats(self):
        with QMutexLocker(self._mutex):
            return {'entries': len(self._entries), 'hits': self.hits,
                    'misses': self.misses, 'evictions': self.evictions,
                    'bytes': self.bytes, 'mb': round(self.bytes / 1e6, 1),
                    'max': self._max}

    # ── persistence ──

    def import_entries(self, entries, ttl_secs):
        """Seed the cache from disk, dropping anything already past `ttl_secs`.

        Expired entries are filtered here rather than left for `get` to reject,
        so a stale file doesn't occupy the LRU budget that live chains need.
        Insertion order is oldest-first, which lines the LRU up with age when the
        file holds more than the cap.
        """
        now   = time.time()
        fresh = [(sym, e) for sym, e in entries.items()
                 if ttl_secs > 0 and (now - e.get('fetched_at', 0)) < ttl_secs]
        fresh.sort(key=lambda kv: kv[1].get('fetched_at', 0))
        with QMutexLocker(self._mutex):
            # Anything already here was fetched this session and is therefore at
            # least as fresh as the file — so a reload (the TTL setting arriving
            # after construction) tops the cache up rather than overwriting it.
            had_unsaved = self._dirty
            for sym, e in fresh[-self._max:]:
                old = self._entries.get(sym)
                if old is not None:
                    if old['fetched_at'] >= e['fetched_at']:
                        continue
                    self.bytes -= len(old['blob'])
                self._entries[sym] = {'blob':        e['blob'],
                                      'fetched_at':  e['fetched_at'],
                                      'expirations': []}
                self._touch(sym)
                self.bytes += len(e['blob'])
            while len(self._order) > self._max:
                evicted = self._order.pop(0)
                dropped = self._entries.pop(evicted, None)
                if dropped is not None:
                    self.bytes -= len(dropped['blob'])
                self.evictions += 1
            # Loading is not itself a change worth writing back, but it must not
            # discard fetches that haven't been saved yet.
            self._dirty  = had_unsaved
            self._loaded = True
            loaded, skipped = len(self._entries), len(entries) - len(fresh)
        return loaded, skipped

    @property
    def loaded_from_disk(self):
        """Whether this instance was seeded from `chain_cache.bin`.

        False means a save must *merge* rather than replace: the in-memory set is
        only this session's fetches, and clobbering the file with it would throw
        away every chain a previous session cached. Also set by `clear()`, which
        is a deliberate wipe of both.
        """
        with QMutexLocker(self._mutex):
            return self._loaded

    def export_entries(self):
        """Snapshot for `config.save_chain_cache`, or None if nothing changed
        since the last save — rewriting several MB to no effect is the same
        pointless I/O the ticker cache used to do on every warm scan."""
        with QMutexLocker(self._mutex):
            if not self._dirty:
                return None
            self._dirty = False
            return {sym: {'blob': e['blob'], 'fetched_at': e['fetched_at']}
                    for sym, e in self._entries.items()}

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

# Connection pool size for the shared `requests.Session`. Must be >= the widest
# concurrent fan-out over it, which is Phase 2's chain fetch.
HTTP_POOL_SIZE = 50

# Chain-cache TTL assumed when the caller doesn't supply one. Mirrors config's
# `chain_cache_ttl_days` default: defaulting to 0 meant an API built before the
# setting was read cached nothing *and* seeded nothing from disk, so the first
# scan of every launch refetched the whole watchlist.
CHAIN_TTL_DEFAULT_SECS = 7 * 86400

# DXLink quote tokens are reusable until they expire. The response carries an
# `expires-at`; when it doesn't, assume this and refresh well inside it.
QUOTE_TOKEN_TTL_FALLBACK = 15 * 60
QUOTE_TOKEN_MARGIN       = 60


class TastytradeAPI:
    """Exchanges the long-lived refresh token for ~15-minute access tokens.

    A single instance is shared across the scan's worker pool, so
    `_ensure_access_token()` is mutex-protected and must be called at the top of
    every REST method.
    """

    def __init__(self, client_secret, refresh_token,
                 chain_ttl_secs=CHAIN_TTL_DEFAULT_SECS):
        self.session = requests.Session()
        # urllib3's default pool is 10 connections. Phase 2 fans chain requests
        # out over CHAIN_WORKERS threads on this one Session, and every worker
        # beyond the pool size either blocks or (worse) has its connection
        # discarded and re-handshaked — so the pool has to be at least as large
        # as the widest fan-out, or the extra threads buy nothing but TLS churn.
        adapter = HTTPAdapter(pool_connections=HTTP_POOL_SIZE,
                              pool_maxsize=HTTP_POOL_SIZE)
        self.session.mount("https://", adapter)
        self.session.mount("http://",  adapter)

        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._access_token  = None
        self._access_token_expiry = 0.0
        self._token_mutex = QMutex()

        # DXLink quote token, cached until just before it expires.
        self._quote_token   = None
        self._quote_expiry  = 0.0
        self._quote_mutex   = QMutex()

        # Shared by the scan pool and the single-ticker search — one instance per
        # API session, so a search warms the cache the next scan reads.
        self.chains = ChainCache()
        # One pacer for the whole fan-out; the point is the aggregate rate.
        self.rate   = RateLimiter()
        self._load_chains(chain_ttl_secs)
        self._refresh_access_token()

    def _load_chains(self, ttl_secs):
        """Seed the chain cache from disk.

        This is what makes the cache worth a multi-day TTL: it used to die with
        the process, so the first scan after every launch refetched the whole
        watchlist no matter how recently it had been fetched.
        """
        if ttl_secs <= 0:
            return
        loaded, skipped = self.chains.import_entries(load_chain_cache(), ttl_secs)
        if loaded or skipped:
            log.info(f"Chain cache: loaded {loaded} chains from disk "
                     f"({self.chains.stats()['mb']} MB), {skipped} expired")

    def save_chains(self):
        """Persist the chain cache if it changed. Safe to call from any thread —
        it snapshots under the cache mutex and writes outside it.

        When the cache was never seeded from disk, the snapshot is only this
        session's fetches; the file is merged under it instead of being replaced,
        because writing a partial snapshot verbatim would delete every chain a
        previous session had cached.
        """
        entries = self.chains.export_entries()
        if entries is None:
            return
        if not self.chains.loaded_from_disk:
            on_disk = load_chain_cache()
            if on_disk:
                log.debug(f"Chain cache: merging {len(entries)} in-memory chain(s) "
                          f"over {len(on_disk)} on disk (cache was never seeded)")
                merged = dict(on_disk)
                merged.update(entries)      # this session's fetches are newer
                entries = merged
        save_chain_cache(entries)

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
        """Option chain for `symbol`, compacted to the fields the app reads and
        served from `self.chains` when a live entry exists.

        `cache_ttl` is in seconds; 0 bypasses the cache. Requests are paced by
        the shared `self.rate` limiter — see :class:`RateLimiter` for why the
        old per-request jitter wasn't enough once the fan-out widened.
        """
        cached = self.chains.get(symbol, cache_ttl)
        if cached is not None:
            return cached

        self._ensure_access_token()
        url = f"{BASE}/option-chains/{symbol}/nested"
        for attempt in range(retries):
            # Blocks until this worker's slot, including any global pause a 429
            # elsewhere in the fan-out has put everyone into.
            self.rate.acquire()
            try:
                r = self.session.get(url, timeout=timeout)
                if r.status_code == 200:
                    chain = compact_chain(r.json()['data']['items'])
                    self.chains.put(symbol, chain)
                    self.rate.succeeded()
                    return chain
                if r.status_code == 429:
                    # Don't log the server's HTML body — 900 of these turned the
                    # debug log into five screens of <title>429</title>.
                    retry_after = r.headers.get('Retry-After')
                    try:
                        pause = float(retry_after) if retry_after is not None else None
                    except ValueError:
                        pause = None
                    self.rate.throttled(pause)
                    log.debug(f"Chain {symbol}: rate limited (attempt {attempt+1})")
                    continue
                log.debug(f"Chain {symbol}: HTTP {r.status_code} "
                          f"(attempt {attempt+1}) — {r.text[:120]}")
                # Retry transient server errors; give up on other client errors
                # (404, 401, etc).
                if r.status_code < 500:
                    break
                if attempt < retries - 1:
                    time.sleep(2 ** attempt + random.uniform(0, 0.5))
            except Exception as exc:
                log.debug(f"Chain {symbol} error (attempt {attempt+1}): {exc}")
                if attempt < retries - 1:
                    time.sleep(2 ** attempt + random.uniform(0, 0.5))
        return []

    def get_quote_token(self, force_refresh=False):
        """DXLink auth token + WebSocket URL, cached until it nears expiry.

        The token outlives a single socket by a wide margin, so re-fetching it
        before every quote operation was a round trip bought for nothing. It is
        still refreshed on demand: `force_refresh=True` is what a socket that
        connected but never reached AUTHORIZED asks for, which is the one failure
        mode a stale token actually produces.

        Mutex-held across the request — one instance is shared by the scan pool
        and the stream, and a thundering herd on a cold cache would otherwise
        fetch a dozen tokens to throw eleven away.
        """
        with QMutexLocker(self._quote_mutex):
            if (not force_refresh and self._quote_token is not None
                    and time.time() < self._quote_expiry):
                return self._quote_token

            self._ensure_access_token()
            url = f"{BASE}/api-quote-tokens"
            log.info(f"GET {url}")
            try:
                r = self.session.get(url, timeout=12)
                log.info(f"Quote token {r.status_code}: {r.text[:500]}")
                if r.status_code == 200:
                    data = r.json()['data']
                    log.info(f"Token data keys: {list(data.keys())}")
                    self._quote_token  = data
                    self._quote_expiry = _token_expiry(data.get('expires-at'))
                    return data
                log.error(f"Quote token request failed: {r.status_code} {r.text[:200]}")
            except Exception as exc:
                log.error(f"get_quote_token exception: {exc}")
            # Don't serve a token we just failed to renew.
            self._quote_token, self._quote_expiry = None, 0.0
            return None


def _token_expiry(expires_at):
    """Monotonic-ish wall-clock deadline for a quote token, with a safety margin.

    `expires-at` is ISO-8601 and sometimes carries a trailing `Z` that
    `fromisoformat` rejects before 3.11, so it is normalised first. Anything
    unparseable falls back to a conservative TTL — over-refreshing a token costs
    one request, under-refreshing costs a dead feed.
    """
    if isinstance(expires_at, str) and expires_at:
        try:
            dt = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            deadline = dt.timestamp() - QUOTE_TOKEN_MARGIN
            if deadline > time.time():
                return deadline
            log.debug(f"Quote token expires-at={expires_at!r} is already past — "
                      f"using the fallback TTL")
        except ValueError:
            log.debug(f"Unparseable quote-token expires-at={expires_at!r}")
    return time.time() + QUOTE_TOKEN_TTL_FALLBACK - QUOTE_TOKEN_MARGIN


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


def _quote_float(val):
    """Feed value -> float, or None when the field is absent or not a real price.

    Distinct from `_safe_float`, which folds both "no value" and a genuine zero
    into 0.0. That is exactly the distinction a merge needs: DXLink sends NaN for
    a field it has nothing for (an illiquid contract with no bid at all), and
    overwriting a good price with 0.0 on every such tick would blank the row —
    but a bid that really goes to 0.0 must replace the previous number.
    """
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f) or f < 0:
        return None
    return f


def _apply_event(event_type, ev, quotes, touched):
    """Merge one decoded event into `quotes` (symbol -> Quote); record the
    symbol in `touched`.

    Only the fields the event actually carried a value for are marked provided,
    so `Quote.merge` overwrites those and leaves the rest alone.
    """
    sym = ev.get('eventSymbol')
    if not sym:
        return
    update, provided = Quote(), set()
    if event_type == 'Quote':
        fields = (('bid', 'bidPrice'), ('ask', 'askPrice'))
    elif event_type == 'Trade':
        fields = (('last', 'price'),)
    else:
        return
    for name, key in fields:
        value = _quote_float(ev.get(key))
        if value is not None:
            setattr(update, name, value)
            provided.add(name)
    if not provided:
        return
    update.provided = frozenset(provided)
    quotes.setdefault(sym, Quote()).merge(update)
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


# ─── Shared quote store ──────────────────────────────────────────────────────

# Phase 3 completion, replacing "every single symbol has a bid". That test made
# the scan hostage to its least liquid contract: one option that never quotes
# held the whole phase to the full timeout, every run. These two say "we have
# what we came for" instead.
QUOTE_COVERAGE   = 0.95    # fraction of target symbols priced
QUOTE_QUIET_SECS = 1.5     # ...or no *new* symbol priced for this long


class QuoteStore:
    """Thread-safe ``symbol -> Quote`` map behind the persistent feed.

    Written from the DXLink socket thread, read from the GUI thread (table
    repaints) and from scan worker threads (phase completion), so every access
    is under one mutex.

    `wait_for` is what replaced closing a socket to find out whether the data
    had arrived: a worker blocks here until enough of its symbols are priced,
    while the same connection keeps feeding everyone else.
    """

    def __init__(self):
        self._mutex  = QMutex()
        self._cond   = QWaitCondition()
        self._quotes = {}

    def merge(self, updates):
        """Apply a decoded FEED_DATA delta and wake anything waiting on it."""
        if not updates:
            return
        with QMutexLocker(self._mutex):
            for sym, quote in updates.items():
                self._quotes.setdefault(sym, Quote()).merge(quote)
            self._cond.wakeAll()

    def get(self, symbol) -> Quote:
        with QMutexLocker(self._mutex):
            q = self._quotes.get(symbol)
            # A copy: the caller reads it off-mutex, and the socket thread would
            # otherwise mutate it underneath them mid-calculation.
            # `provided` rides along: the caller needs it to tell a live 0.0 from
            # a field this symbol has never quoted.
            return Quote(q.bid, q.ask, q.last, q.provided) if q else Quote()

    def snapshot(self, symbols=None):
        with QMutexLocker(self._mutex):
            keys = list(self._quotes) if symbols is None else symbols
            return {s: Quote(q.bid, q.ask, q.last, q.provided)
                    for s in keys
                    for q in (self._quotes.get(s),) if q is not None}

    def clear(self):
        with QMutexLocker(self._mutex):
            self._quotes.clear()

    def retain(self, symbols):
        """Drop every symbol outside `symbols`.

        The store used to die with its socket; on a connection that never closes,
        a quote for an unsubscribed symbol would sit there forever looking fresh.
        That is a trap for the *next* scan, whose completion test would count the
        stale entry as priced and whose snapshot would then hand a months-old
        bid/ask to `solve_calendar`. Keeping the store to exactly what is on the
        wire is what makes "never disconnect" safe.
        """
        keep = set(symbols)
        with QMutexLocker(self._mutex):
            stale = [s for s in self._quotes if s not in keep]
            for s in stale:
                del self._quotes[s]
        return len(stale)

    def priced(self, symbols, predicate):
        with QMutexLocker(self._mutex):
            return sum(1 for s in symbols
                       if predicate(self._quotes.get(s) or Quote()))

    def wait_for(self, symbols, predicate, timeout,
                 coverage=QUOTE_COVERAGE, quiet=QUOTE_QUIET_SECS):
        """Block until enough of `symbols` satisfy `predicate`. Worker threads only.

        Returns the number priced. Three ways out, in priority order:

        1. **Coverage** — `coverage` of the symbols are priced. The normal exit.
        2. **Quiet period** — at least one symbol is priced and no *additional*
           one has been priced for `quiet` seconds, i.e. the feed has delivered
           what it is going to deliver. Progress is measured against the target
           set rather than raw tick arrivals, so an unrelated table streaming on
           the same connection can't hold this open.
        3. **Timeout** — the backstop, and now the rare case rather than the norm.
        """
        symbols = list(dict.fromkeys(symbols))
        if not symbols:
            return 0
        need     = max(1, math.ceil(len(symbols) * coverage))
        deadline = time.monotonic() + timeout
        last_ready, last_progress = -1, time.monotonic()

        with QMutexLocker(self._mutex):
            while True:
                ready = sum(1 for s in symbols
                            if predicate(self._quotes.get(s) or Quote()))
                if ready >= need:
                    return ready
                now = time.monotonic()
                if ready != last_ready:
                    last_ready, last_progress = ready, now
                if ready > 0 and (now - last_progress) >= quiet:
                    return ready
                remaining = deadline - now
                if remaining <= 0:
                    return ready
                # Capped so the quiet test still fires on a feed that has gone
                # completely silent — there'd be no wake-up to evaluate it on.
                self._cond.wait(self._mutex,
                                max(1, int(min(remaining, quiet) * 1000)))


# Symbols per FEED_SUBSCRIPTION frame. A full scan streams every result row at
# 3 symbols each, so one frame carrying the lot would be needlessly large.
SUBSCRIBE_CHUNK = 200

# Concurrent one-shot DXLink connections. Each quote token supports a limited
# number of streamer sessions, and every socket costs a thread parked on a
# semaphore, so this is deliberately well under the batch count — the win is
# hiding one batch's timeout behind another's, not opening 25 sockets.
QUOTE_SOCKETS = 4


def _subscription_frames(quote_syms, trade_syms, chunk=SUBSCRIBE_CHUNK):
    """FEED_SUBSCRIPTION `add` frames covering these symbols, chunked.

    There is deliberately no `remove` counterpart. DXLink snapshots a symbol only
    on its *first* subscription to a channel, so a remove/re-add cycle leaves it
    silent until it ticks — `QuoteStream._sync` explains why that made every
    rescan return one row. Symbols are shed by recycling the connection instead.
    """
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
        # Only the retries force a new token: a cached one is exactly what the
        # first attempt should use, and "connected but never AUTHORIZED" is the
        # single symptom that a stale token actually produces.
        token_data = api.get_quote_token(force_refresh=attempt > 0)
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
    """One DXLink socket, streaming quotes until stopped.

    Owned and rebuilt by :class:`QuoteStream` — nothing else should construct one
    directly. Lifecycle is fully signal-driven, so nothing blocks the GUI:

        client = DXLinkLiveClient(api, store)
        client.ready.connect(...)          # safe to subscribe() from here on
        client.quotesUpdated.connect(...)  # {symbol: Quote}, queued to the GUI thread
        client.connectionFailed.connect(...)
        client.start()

    Decoded events are merged into the shared `QuoteStore` **on the socket
    thread**, before `quotesUpdated` is emitted. That ordering is what lets a
    scan worker block on the store while the GUI takes the same data through a
    queued signal: neither waits on the other.

    The socket loop owns this QThread; `subscribe()` and the
    keepalive are called from the owner's thread, which is safe because
    websocket-client guards frame writes with its own lock.
    """

    ready            = Signal()
    connectionFailed = Signal(str)
    quotesUpdated    = Signal(dict)
    disconnected     = Signal()

    def __init__(self, api, store: QuoteStore, force_token=False, parent=None):
        super().__init__(parent)
        self._api   = api
        self._store = store
        # A reconnect that follows a failure can't trust the cached quote token —
        # a rejected token is the one thing that looks exactly like this.
        self._force_token = force_token
        self._ws    = None
        self._token = None
        self._url   = None
        self._field_map = {}
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
        token_data = self._api.get_quote_token(force_refresh=self._force_token)
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
                # Decode into a fresh dict so it is both the store delta and the
                # signal payload; the store owns the accumulated state.
                delta = {}
                _parse_feed_data(data.get('data', []), self._field_map, delta)
                if delta:
                    self._store.merge(delta)
                    self.quotesUpdated.emit(delta)
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

    def subscribed_symbols(self):
        """Everything currently subscribed for Quote events, sent or queued."""
        with QMutexLocker(self._sub_mutex):
            return set(self._subscribed_quote)

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


# ─── The persistent connection ───────────────────────────────────────────────

# Reconnect backoff. A feed that stays down through a network blip is a worse
# failure than a reconnect loop, so retries keep going at the ceiling forever and
# the status badge says what is happening.
RECONNECT_MS     = 2_000
RECONNECT_MAX_MS = 30_000

# Symbols that may stay subscribed on one connection before `release_consumer`
# recycles it to shed the ones nobody wants. Generous on purpose: recycling is
# what re-snapshots everything, and a scan that keeps landing just over the line
# would pay for it on every run. A wide scan peaks around 2,000 equities plus
# their legs, so this absorbs a couple of those before pruning.
SUBSCRIPTION_HIGH_WATER = 4000


class QuoteStream(QObject):
    """The app's one DXLink connection, shared by the scan and both tables.

    Every quote consumer registers a **named symbol set**; the union of those
    sets is what is subscribed on the wire. Registering, replacing or releasing a
    set diffs against that union and sends only the delta, so:

    * the handshake (``SETUP`` → ``AUTH`` → ``CHANNEL_REQUEST``) happens once per
      app session instead of once per 200-symbol batch — the old one-shot fetch
      paid for a full connection, auth round trip and teardown per batch, twice
      per scan;
    * the scan's Phase 3 symbols are already streaming when the results table
      appears, so the table is live on its first paint instead of being frozen at
      scan-time values until a second connection catches up;
    * nothing the scan subscribed outlives the scan — `release_consumer` drops
      whatever no remaining consumer still wants.

    Thread affinity: this object lives on the GUI thread, because the client it
    owns hangs a keepalive `QTimer` off its creating thread and that thread needs
    an event loop. Worker threads may call `ensure_started`, `wait_ready`,
    `set_consumer` and `release_consumer` — connection work is bounced to the GUI
    thread through a queued invocation, and subscription frames are sent inline
    (websocket-client locks its own writes). Everything else is GUI-thread only.
    """

    quotesUpdated = Signal(dict)     # {symbol: Quote} delta
    statusChanged = Signal(str)
    connected     = Signal()
    failed        = Signal(str)

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self._session   = session
        self._store     = QuoteStore()
        self._client    = None
        self._consumers = {}         # name -> (set(quote syms), set(trade syms))
        self._mutex     = QMutex()   # guards _consumers, _client and _ready
        self._ready_cv  = QWaitCondition()
        self._is_ready  = False
        self._stopped   = True
        self._retry_ms  = RECONNECT_MS
        self._force_token = False

        self._retry = QTimer(self)
        self._retry.setSingleShot(True)
        self._retry.timeout.connect(self._reconnect)

    # ── state ──

    @property
    def store(self) -> QuoteStore:
        return self._store

    @property
    def is_ready(self):
        with QMutexLocker(self._mutex):
            return self._is_ready

    def quote(self, symbol) -> Quote:
        return self._store.get(symbol)

    def _union(self):
        """(quote symbols, trade symbols) wanted by all consumers. Caller holds
        the mutex."""
        q, t = set(), set()
        for syms, trades in self._consumers.values():
            q |= syms
            t |= trades
        return q, t

    # ── lifecycle ──

    def ensure_started(self):
        """Connect if not already connected. Callable from any thread."""
        with QMutexLocker(self._mutex):
            self._stopped = False
            if self._client is not None:
                return
        if QThread.currentThread() is self.thread():
            self._ensure_connected()
        else:
            # Building the client (and its keepalive QTimer) has to happen on
            # this object's own thread; a worker thread has no event loop to run
            # the timer on.
            QMetaObject.invokeMethod(self, "_ensure_connected",
                                     Qt.ConnectionType.QueuedConnection)

    @Slot()
    def _ensure_connected(self):
        with QMutexLocker(self._mutex):
            if self._stopped or self._client is not None:
                return
        self._connect("Connecting live stream…")

    def _connect(self, status):
        api = self._session.peek()
        if api is None:
            self.failed.emit("not authenticated")
            self._schedule_reconnect("not authenticated")
            return

        self.statusChanged.emit(status)
        client = DXLinkLiveClient(api, self._store,
                                  force_token=self._force_token, parent=self)
        client.quotesUpdated.connect(self._on_quotes)
        client.ready.connect(self._on_ready)
        client.connectionFailed.connect(self._on_failed)
        # Bound method, not a lambda: a functor with no context object would be a
        # DirectConnection and run this on the WebSocket thread.
        client.disconnected.connect(self._on_disconnected)
        with QMutexLocker(self._mutex):
            self._client   = client
            self._is_ready = False
        client.start()

    def wait_ready(self, timeout_ms=20_000):
        """Block until the feed channel is open. Worker threads only.

        Returns False on timeout, which is a scan's cue to fall back to the
        one-shot fetch rather than sit on a connection that isn't coming.
        """
        deadline = time.monotonic() + timeout_ms / 1000.0
        with QMutexLocker(self._mutex):
            while not self._is_ready and not self._stopped:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._ready_cv.wait(self._mutex, max(1, int(remaining * 1000)))
            return self._is_ready

    def stop(self):
        """Tear the connection down for good — app shutdown or sign-out."""
        with QMutexLocker(self._mutex):
            self._stopped  = True
            self._is_ready = False
            self._retry_ms = RECONNECT_MS
            self._consumers.clear()
            client, self._client = self._client, None
            self._ready_cv.wakeAll()
        self._retry.stop()
        self._store.clear()
        if client is not None:
            try:
                client.stop()
            except Exception as exc:
                log.warning(f"Error closing quote stream: {exc}")
            client.deleteLater()
        self.statusChanged.emit("No live connection")

    # ── consumers ──

    def set_consumer(self, name, symbols, trade_syms=()):
        """Declare what `name` wants streamed, replacing its previous set.

        Callable from any thread. Connects on first use, so a caller never has to
        sequence `ensure_started` against this itself.
        """
        symbols    = set(s for s in symbols if s)
        trade_syms = set(s for s in trade_syms if s) & symbols
        with QMutexLocker(self._mutex):
            if self._consumers.get(name) == (symbols, trade_syms):
                return
            self._consumers[name] = (symbols, trade_syms)
        self.ensure_started()
        self._sync()

    def release_consumer(self, name):
        """Forget `name`'s symbols.

        Orphaned symbols keep streaming (see `_sync`) rather than being
        unsubscribed, so the subscription set only ever grows within a
        connection. This is where that growth is bounded: past
        `SUBSCRIPTION_HIGH_WATER` the connection is recycled, which is the only
        way to shed symbols *and* be certain the ones we keep get a fresh
        snapshot — a new channel snapshots everything it is subscribed to.

        Only ever called between scans (`MainWindow` releases the scan consumer
        in `_on_scan_thread_done`, the table in `LiveFeedController.stop`), so a
        recycle can never pull the feed out from under a running Phase 3.
        """
        with QMutexLocker(self._mutex):
            if self._consumers.pop(name, None) is None:
                return
            client = self._client
            want_q, _ = self._union()

        if client is not None:
            subscribed = client.subscribed_symbols()
            if (len(subscribed) > SUBSCRIPTION_HIGH_WATER
                    and len(subscribed - want_q) > 0):
                log.info(
                    f"Quote stream: {len(subscribed)} symbols subscribed, "
                    f"{len(want_q)} still wanted — recycling the connection to "
                    f"shed the rest")
                self._recycle()
                return
        self._sync()

    def _recycle(self):
        """Drop and immediately rebuild the connection.

        Reuses the reconnect path wholesale: `_on_ready` re-subscribes the union
        from scratch on the new channel, so every remaining symbol is snapshotted
        again. Cheaper to reason about than any remove/re-add dance, and it is
        the one sequence the server is guaranteed to honour.
        """
        self._store.clear()
        self._teardown_client()
        with QMutexLocker(self._mutex):
            # Not a failure, so don't make the next connect force a fresh token
            # or advance the backoff — this is a deliberate, immediate rebuild.
            self._force_token = False
            self._retry_ms    = RECONNECT_MS
            stopped = self._stopped
        if not stopped:
            self._connect("Refreshing live stream…")

    def _sync(self):
        """Add the union of the consumer sets to the wire. **Never removes.**

        Unsubscribing eagerly looks tidy and is a trap. DXLink sends a snapshot
        when a symbol is *first* subscribed on a channel; a remove followed by a
        re-add does not produce a second one, so the symbol stays silent until it
        happens to tick. That is exactly what a rescan does — the table releases
        its rows, then Phase 3a asks for most of the same symbols two seconds
        later — and it made every scan after the first return one row.

        So orphaned symbols are left streaming. They cost a little bandwidth and
        nothing else, they are usually wanted again by the next scan, and their
        quotes stay genuinely live rather than becoming stale — which is what
        makes it safe for `wait_for` to count them as already priced. Growth is
        bounded by `release_consumer`, which recycles the connection once the
        subscription set gets large.
        """
        with QMutexLocker(self._mutex):
            client = self._client
            want_q, want_t = self._union()
        if client is None:
            return                      # a fresh client re-syncs from _on_ready
        client.subscribe(want_q, with_trade_for=want_t)
        # Keep the store to exactly what is on the wire. Tying it to the
        # *subscribed* set rather than the *wanted* set is the point: a
        # subscribed symbol's quote is live, so retaining it is correct, while an
        # unsubscribed one would freeze and later be mistaken for fresh.
        dropped = self._store.retain(client.subscribed_symbols())
        if dropped:
            log.debug(f"Quote store: dropped {dropped} unsubscribed symbol(s)")

    # ── client callbacks (GUI thread) ──

    def _on_ready(self):
        with QMutexLocker(self._mutex):
            if self.sender() is not self._client:
                return                  # a superseded client finishing late
            self._is_ready    = True
            self._retry_ms    = RECONNECT_MS    # it held; start the backoff over
            self._force_token = False
            n = sum(len(q) for q, _ in self._consumers.values())
            self._ready_cv.wakeAll()
        self._sync()
        self.connected.emit()
        self.statusChanged.emit(f"Live: {n} symbol(s) streaming")

    def _on_quotes(self, delta):
        # The store was already updated on the socket thread; this is the GUI-side
        # fan-out only.
        self.quotesUpdated.emit(delta)

    def _on_failed(self, message):
        log.error(f"Quote stream failed: {message}")
        if not self._teardown_client():
            return
        self.failed.emit(message)
        self.statusChanged.emit(f"Live: {message}")
        self._schedule_reconnect(message)

    def _on_disconnected(self):
        """`run()` returned without an error ever reaching `_on_failed` — a socket
        that closed cleanly under us. Without this the feed would sit there looking
        connected while no quotes arrived."""
        with QMutexLocker(self._mutex):
            if self._stopped or self._client is None:
                return          # deliberate stop, or _on_failed already handled it
            if self.sender() is not self._client:
                return          # a superseded client finishing late
        log.warning("Quote stream disconnected")
        if not self._teardown_client():
            return
        self.failed.emit("disconnected")
        self._schedule_reconnect("disconnected")

    def _teardown_client(self):
        """Release the dead client. Returns False if there was nothing to release
        — i.e. someone else already handled this drop."""
        with QMutexLocker(self._mutex):
            client, self._client = self._client, None
            self._is_ready = False
            # A reconnect after a drop can't trust the cached quote token.
            self._force_token = True
            self._ready_cv.wakeAll()
        if client is None:
            return False
        # run_forever has already returned; just release the thread object so a
        # later reconnect isn't stacking dead clients under this parent.
        client.stop(wait_ms=1000)
        client.deleteLater()
        return True

    def _schedule_reconnect(self, reason):
        if self._retry.isActive():
            return          # checked before the backoff advances, not after
        with QMutexLocker(self._mutex):
            if self._stopped or not self._consumers:
                return
            delay, self._retry_ms = self._retry_ms, min(self._retry_ms * 2,
                                                        RECONNECT_MAX_MS)
        log.info(f"Quote stream reconnecting in {delay} ms ({reason})")
        self.statusChanged.emit(
            f"Live: {reason} — reconnecting in {round(delay / 1000)}s")
        self._retry.start(delay)

    def _reconnect(self):
        with QMutexLocker(self._mutex):
            if self._stopped or not self._consumers or self._client is not None:
                return
        self._connect("Reconnecting live stream…")
