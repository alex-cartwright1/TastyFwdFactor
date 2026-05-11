# Required packages: pip install requests scipy numpy websocket-client pandas yfinance matplotlib keyring
"""
Calendar Spread Edge Screener — Tastytrade Production
Refactored for reliability and debuggability.
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import numpy as np
import pandas as pd
import math
import time
import random
import requests
import json
import threading
import websocket
from datetime import datetime, timedelta
import concurrent.futures
import os
import logging
import subprocess
from pathlib import Path
from scipy.stats import norm
import yfinance as yf

try:
    import keyring
    from keyring.backends.fail import Keyring as _FailKeyring
    _KEYRING_AVAILABLE = not isinstance(keyring.get_keyring(), _FailKeyring)
    del _FailKeyring
except Exception:
    _KEYRING_AVAILABLE = False

try:
    import matplotlib
    matplotlib.use('TkAgg')
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import (
        FigureCanvasTkAgg, NavigationToolbar2Tk,
    )
    _MPL_AVAILABLE = True
except Exception:
    _MPL_AVAILABLE = False


# ─── Logging ─────────────────────────────────────────────────────────────────

_LOG_PATH = Path(__file__).parent / "debug.log"

_file_logger = logging.getLogger("cal_spread")
_file_logger.setLevel(logging.DEBUG)
if not _file_logger.handlers:
    _fh = logging.FileHandler(_LOG_PATH, mode='w', encoding='utf-8')
    _fh.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)-7s %(message)s', datefmt='%H:%M:%S'))
    _file_logger.addHandler(_fh)


class AppLog:
    """Writes to debug.log and, when attached, also appends to a Tkinter Text widget."""

    def __init__(self):
        self._widget = None

    def attach(self, widget):
        self._widget = widget

    def _write(self, level, msg):
        getattr(_file_logger, level)(msg)
        if self._widget is not None:
            line = f"[{level.upper()[:4]}] {msg}\n"
            try:
                self._widget.after(0, self._append_gui, line)
            except Exception:
                pass

    def _append_gui(self, line):
        try:
            w = self._widget
            w.configure(state='normal')
            w.insert(tk.END, line)
            w.see(tk.END)
            w.configure(state='disabled')
        except Exception:
            pass

    def debug(self, msg):   self._write('debug',   msg)
    def info(self, msg):    self._write('info',    msg)
    def warning(self, msg): self._write('warning', msg)
    def error(self, msg):   self._write('error',   msg)


log = AppLog()


# ─── Credentials persistence (OS keyring with legacy JSON fallback) ──────────

_KEYRING_SERVICE = "calendar-spread-tastytrade"
_LEGACY_CREDS_PATH = Path.home() / ".config" / "calendar-spread" / "credentials.json"


def load_credentials():
    """Return (client_secret, refresh_token). Prefer OS keyring; fall back to
    legacy JSON file (and migrate it next time the user saves)."""
    if _KEYRING_AVAILABLE:
        try:
            secret  = keyring.get_password(_KEYRING_SERVICE, "client_secret")  or ''
            refresh = keyring.get_password(_KEYRING_SERVICE, "refresh_token") or ''
            if secret or refresh:
                return secret, refresh
        except Exception as exc:
            log.warning(f"Could not read keyring: {exc}")

    if _LEGACY_CREDS_PATH.exists():
        try:
            data = json.loads(_LEGACY_CREDS_PATH.read_text())
            return data.get('client_secret', ''), data.get('refresh_token', '')
        except Exception as exc:
            log.warning(f"Could not read legacy credentials file: {exc}")
    return '', ''


def save_credentials(client_secret, refresh_token):
    """Write to OS keyring if available; otherwise fall back to legacy JSON."""
    if _KEYRING_AVAILABLE:
        try:
            keyring.set_password(_KEYRING_SERVICE, "client_secret",  client_secret)
            keyring.set_password(_KEYRING_SERVICE, "refresh_token", refresh_token)
            log.info("Credentials saved to OS keyring")
            # One-shot migration: remove the plaintext legacy file.
            if _LEGACY_CREDS_PATH.exists():
                try:
                    _LEGACY_CREDS_PATH.unlink()
                    log.info(f"Removed legacy credentials file {_LEGACY_CREDS_PATH}")
                except Exception as exc:
                    log.warning(f"Could not remove legacy credentials file: {exc}")
            return
        except Exception as exc:
            log.warning(f"Could not save to keyring ({exc}); falling back to JSON")

    try:
        _LEGACY_CREDS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _LEGACY_CREDS_PATH.write_text(json.dumps({
            'client_secret': client_secret,
            'refresh_token': refresh_token,
        }))
        try:
            os.chmod(_LEGACY_CREDS_PATH, 0o600)
        except OSError:
            pass  # Windows / non-POSIX filesystems
    except Exception as exc:
        log.warning(f"Could not save credentials file: {exc}")


# ─── Settings persistence (everything except secrets) ────────────────────────

_SETTINGS_PATH = Path.home() / ".config" / "calendar-spread" / "settings.json"

DEFAULT_SETTINGS = {
    'csv_path':              str(Path(__file__).parent / "full.csv"),
    'front_dte':             21,
    'back_dte':              45,
    'front_dte_flex':        0,
    'back_dte_flex':         0,
    'iv_method':             'Midpoint',
    'min_price':             10.0,
    'min_market_cap_b':      '',
    'filter_front_earnings':   True,
    'filter_back_earnings':    True,
    'filter_front_dividend':   False,
    'filter_back_dividend':    False,
    'filter_unknown_earnings': False,
    'ticker_info_ttl_days':    7,
}


def load_settings():
    try:
        if _SETTINGS_PATH.exists():
            data = json.loads(_SETTINGS_PATH.read_text())
            return {**DEFAULT_SETTINGS, **data}
    except Exception as exc:
        log.warning(f"Could not read settings file: {exc}")
    return dict(DEFAULT_SETTINGS)


def save_settings(settings):
    try:
        _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SETTINGS_PATH.write_text(json.dumps(settings, indent=2))
    except Exception as exc:
        log.warning(f"Could not save settings file: {exc}")


# ─── Ticker info cache (earnings / market cap / ex-div) ──────────────────────

_TICKER_CACHE_PATH = Path.home() / ".config" / "calendar-spread" / "ticker_info.json"


def load_ticker_cache():
    try:
        if _TICKER_CACHE_PATH.exists():
            return json.loads(_TICKER_CACHE_PATH.read_text())
    except Exception as exc:
        log.warning(f"Could not read ticker cache: {exc}")
    return {}


def save_ticker_cache(cache):
    try:
        _TICKER_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _TICKER_CACHE_PATH.write_text(json.dumps(cache))
    except Exception as exc:
        log.warning(f"Could not save ticker cache: {exc}")


def clear_ticker_cache():
    try:
        if _TICKER_CACHE_PATH.exists():
            _TICKER_CACHE_PATH.unlink()
            log.info(f"Cleared ticker cache: {_TICKER_CACHE_PATH}")
    except Exception as exc:
        log.warning(f"Could not clear ticker cache: {exc}")


def _parse_iso_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


# ─── Black-Scholes / IV ──────────────────────────────────────────────────────

def bs_price(S, K, T, r, v, option_type='c'):
    if T <= 0 or v <= 0:
        return 0.0
    d1 = (np.log(S / K) + (r + 0.5 * v**2) * T) / (v * np.sqrt(T))
    d2 = d1 - v * np.sqrt(T)
    if option_type == 'c':
        return float(S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2))
    return float(K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1))


def calc_implied_vol(target_price, S, K, T, r=0.04):
    if target_price <= 0 or S <= 0 or K <= 0 or T <= 0:
        return 0.001
    sigma = 0.5
    for _ in range(100):
        price = bs_price(S, K, T, r, sigma)
        vega = (S * norm.pdf(
            (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
        ) * np.sqrt(T))
        diff = target_price - price
        if abs(diff) < 1e-5:
            return max(sigma, 0.001)
        if vega < 1e-4:
            break
        sigma += diff / vega
    return max(sigma, 0.001)


# ─── Tastytrade REST API ──────────────────────────────────────────────────────

class TastytradeAPI:
    BASE = "https://api.tastyworks.com"

    def __init__(self, client_secret, refresh_token):
        self.session = requests.Session()
        self._client_secret = client_secret
        self._refresh_token  = refresh_token
        self._access_token   = None
        self._access_token_expiry = 0.0
        self._token_lock = threading.Lock()
        self._refresh_access_token()

    def _refresh_access_token(self):
        """Exchange the long-lived refresh_token for a 15-min access_token.
        Body matches both official SDKs: client_id and redirect_uri are not required."""
        url = f"{self.BASE}/oauth/token"
        log.info(f"POST {url} (grant_type=refresh_token)")
        # Use a bare requests.post so we don't send a stale Bearer header during refresh.
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
        """Refresh the access_token if it's expired or near expiry. Thread-safe."""
        with self._token_lock:
            if time.time() >= self._access_token_expiry:
                log.info("Access token near/past expiry — refreshing")
                self._refresh_access_token()

    def get_option_chain(self, symbol, retries=3, timeout=30):
        self._ensure_access_token()
        url = f"{self.BASE}/option-chains/{symbol}/nested"
        for attempt in range(retries):
            try:
                r = self.session.get(url, timeout=timeout)
                if r.status_code == 200:
                    return r.json()['data']['items']
                log.debug(f"Chain {symbol}: HTTP {r.status_code} (attempt {attempt+1}) — {r.text[:120]}")
                # Retry on transient server errors; give up on client errors
                if r.status_code < 500:
                    break
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)   # 1 s, 2 s backoff
            except Exception as exc:
                log.debug(f"Chain {symbol} error (attempt {attempt+1}): {exc}")
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
        return []

    def get_quote_token(self):
        """Fetch a fresh DXLink auth token + WebSocket URL from Tastytrade."""
        self._ensure_access_token()
        url = f"{self.BASE}/api-quote-tokens"
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


# ─── DXLink WebSocket Feed ────────────────────────────────────────────────────

# The Tastytrade sandbox /api-quote-tokens response may use different key names
# depending on the API version.  Try all known variants.
_TOKEN_KEYS = ('token', 'streamer-token', 'websocket-token', 'dxlink-token', 'access-token')
_URL_KEYS   = ('dxlink-url', 'websocket-url', 'streamer-url', 'url')


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


def _safe_float(val):
    try:
        f = float(val)
        return 0.0 if (math.isnan(f) or math.isinf(f) or f < 0) else f
    except (TypeError, ValueError):
        return 0.0


def _process_event(event_type, event_dict, quotes):
    sym = event_dict.get('eventSymbol')
    if not sym:
        return
    q = quotes.setdefault(sym, {})
    if event_type == 'Quote':
        bid = _safe_float(event_dict.get('bidPrice'))
        ask = _safe_float(event_dict.get('askPrice'))
        if bid > 0: q['bid'] = bid
        if ask > 0: q['ask'] = ask
    elif event_type == 'Trade':
        price = _safe_float(event_dict.get('price'))
        if price > 0: q['last'] = price


def _parse_feed_data(raw_data, field_map, quotes):
    """Parse FEED_DATA — handles both COMPACT (array) and FULL (dict) formats."""
    if not isinstance(raw_data, list):
        return
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
                            _process_event(event_type, dict(zip(fields, chunk)), quotes)
            i += 1
        elif isinstance(item, dict):
            etype = item.get('eventType') or item.get('type', '')
            _process_event(etype, item, quotes)
            i += 1
        else:
            i += 1


def fetch_quotes_dxlink(token_data, symbols, timeout=25):
    """
    Open a DXLink WebSocket, subscribe to Quote+Trade events for symbols.
    Returns: dict of symbol -> {bid, ask, last}  plus '__diag__' key.

    Auth flow:
      1. Client sends SETUP
      2. Server sends SETUP ack  (may also send AUTH_STATE:UNAUTHORIZED as greeting)
      3. Client sends AUTH with token
      4. Server sends AUTH_STATE:AUTHORIZED
      5. Client opens channel and subscribes
    The initial UNAUTHORIZED greeting is ignored; only UNAUTHORIZED *after*
    we send AUTH is treated as a real failure.
    """
    if not token_data or not symbols:
        return {'__diag__': {'error': 'No token_data or symbols provided'}}

    dxlink_url = _extract_url(token_data)
    token      = _extract_token(token_data)

    if not dxlink_url or not token:
        return {'__diag__': {
            'error': f'Missing URL or token. Keys in token_data: {list(token_data.keys())}',
        }}

    quotes    = {}
    field_map = {}
    done_event = threading.Event()
    diag = {
        'connected': False, 'authorized': False,
        'channel_opened': False, 'error': None,
        'raw_msgs': [],   # first 10 raw messages — invaluable for debugging auth
    }
    auth_sent = [False]

    option_syms = {s for s in symbols if s.startswith('.') or '/' in s}
    equity_syms = {s for s in symbols if s not in option_syms}

    def has_sufficient_data():
        eq_ok = all(
            quotes.get(s, {}).get('last', 0) > 0
            or (quotes.get(s, {}).get('bid', 0) > 0 and quotes.get(s, {}).get('ask', 0) > 0)
            for s in equity_syms
        )
        opt_ok = all(quotes.get(s, {}).get('bid', 0) > 0 for s in option_syms)
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
                auth_sent[0] = True
                ws.send(json.dumps({"type": "AUTH", "channel": 0, "token": token}))

            elif mtype == 'AUTH_STATE':
                state = data.get('state', '')
                log.info(f"DXLink AUTH_STATE={state!r}  auth_sent={auth_sent[0]}")
                if state == 'AUTHORIZED':
                    diag['authorized'] = True
                    log.debug("Authorized — requesting FEED channel")
                    ws.send(json.dumps({
                        "type": "CHANNEL_REQUEST", "channel": 1,
                        "service": "FEED", "parameters": {"contract": "AUTO"},
                    }))
                else:
                    # The server may emit AUTH_STATE:UNAUTHORIZED as an initial greeting
                    # that races with — and arrives after — our own AUTH frame. There's no
                    # reliable way to tell that greeting apart from a real token rejection
                    # from the message alone, so don't fail here. Wait for AUTHORIZED; if
                    # AUTH genuinely failed, the overall WebSocket timeout will catch it
                    # and fetch_quotes_with_retry will refresh the token and retry.
                    log.debug(f"AUTH_STATE={state!r} — waiting for AUTHORIZED")

            elif mtype == 'CHANNEL_OPENED' and data.get('channel') == 1:
                diag['channel_opened'] = True
                log.debug("CHANNEL_OPENED — sending FEED_SETUP + subscriptions")
                ws.send(json.dumps({
                    "type": "FEED_SETUP", "channel": 1,
                    "acceptAggregationPeriod": 10,
                    "acceptDataFormat": "COMPACT",
                    "acceptEventFields": {
                        "Quote": ["eventSymbol", "bidPrice", "askPrice"],
                        "Trade": ["eventSymbol", "price"],
                    },
                }))
                subs = [{"type": "Quote", "symbol": s} for s in symbols]
                subs += [{"type": "Trade", "symbol": s} for s in equity_syms]
                ws.send(json.dumps({"type": "FEED_SUBSCRIPTION", "channel": 1, "add": subs}))
                log.debug(f"Subscribed: {len(subs)} events across {len(symbols)} symbols")

            elif mtype == 'FEED_CONFIG' and data.get('channel') == 1:
                for etype, fields in data.get('eventFields', {}).items():
                    field_map[etype] = fields
                log.debug(f"FEED_CONFIG field_map keys: {list(field_map.keys())}")

            elif mtype == 'FEED_DATA' and data.get('channel') == 1:
                _parse_feed_data(data.get('data', []), field_map, quotes)
                if symbols and has_sufficient_data():
                    log.debug(f"All data received — {len(quotes)} symbols priced")
                    done_event.set()

            elif mtype == 'KEEPALIVE':
                ws.send(json.dumps({"type": "KEEPALIVE", "channel": 0}))

            elif mtype == 'ERROR':
                diag['error'] = f"Server ERROR: {data.get('message', data)}"
                log.error(f"DXLink server ERROR: {data}")
                done_event.set()

        except Exception as exc:
            diag['error'] = f'on_message exception: {exc}'
            log.error(f"DXLink on_message exception: {exc}")

    def on_error(ws, error):
        diag['error'] = str(error)
        log.error(f"DXLink WS error: {error}")
        done_event.set()

    def on_close(ws, code, msg):
        log.debug(f"DXLink WS closed: code={code}")
        done_event.set()

    ws_app = websocket.WebSocketApp(
        dxlink_url,
        on_open=on_open, on_message=on_message,
        on_error=on_error, on_close=on_close,
    )
    thread = threading.Thread(target=ws_app.run_forever, daemon=True)
    thread.start()
    done_event.wait(timeout=timeout)

    try:
        ws_app.close()
    except Exception:
        pass
    thread.join(timeout=3)

    n_priced = sum(
        1 for v in quotes.values()
        if isinstance(v, dict) and (v.get('bid', 0) > 0 or v.get('last', 0) > 0)
    )
    log.info(
        f"DXLink session done: {n_priced}/{len(symbols)} priced  "
        f"connected={diag['connected']} authorized={diag['authorized']} "
        f"channel={diag['channel_opened']} error={diag['error']!r}"
    )

    quotes['__diag__'] = diag
    return quotes


def _fetch_batch(token_data, symbols, batch_num, n_batches, timeout):
    log.info(f"DXLink batch {batch_num}/{n_batches}: {len(symbols)} symbols")
    return fetch_quotes_dxlink(token_data, symbols, timeout=timeout)


def fetch_quotes_batched(token_data, symbols, batch_size=200, timeout=25):
    """Batch DXLink requests; returns merged quotes dict + list of per-batch diag dicts."""
    all_quotes = {}
    all_diags  = []
    sym_list   = list(symbols)
    n_batches  = math.ceil(len(sym_list) / batch_size) if sym_list else 0
    for i in range(0, len(sym_list), batch_size):
        batch = sym_list[i:i + batch_size]
        q = _fetch_batch(token_data, batch, i // batch_size + 1, n_batches, timeout)
        d = q.pop('__diag__', None)
        if d is not None:
            all_diags.append(d)
        all_quotes.update(q)
    all_quotes['__diag__'] = all_diags
    return all_quotes


def fetch_quotes_with_retry(api, symbols, batch_size=200, timeout=25, max_retries=2):
    """
    Fetch quotes for a symbol list, automatically refreshing the DXLink token
    and retrying if auth fails (up to max_retries extra attempts).
    Returns: (quotes_dict, diags_list)
    """
    last_result, last_diags = {}, []
    for attempt in range(max_retries + 1):
        token_data = api.get_quote_token()
        if not token_data:
            log.error(f"Could not get quote token (attempt {attempt + 1})")
            return {}, [{'error': 'Could not get quote token'}]

        result = fetch_quotes_batched(token_data, symbols, batch_size, timeout)
        diags  = result.pop('__diag__', [])
        last_result, last_diags = result, diags

        # Retry if any batch connected but never reached AUTHORIZED state. This
        # covers both explicit UNAUTHORIZED errors and silent auth timeouts.
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


# ─── Persistent DXLink streaming client ──────────────────────────────────────

class DXLinkLiveClient:
    """A persistent DXLink WebSocket client for live quote streaming.

    Unlike `fetch_quotes_dxlink` (one-shot, opens then closes), this client
    stays connected so the GUI can keep receiving quote updates after a scan
    completes.

    Usage:
        client = DXLinkLiveClient(api)
        client.set_quote_callback(lambda sym, q: ...)
        client.connect(timeout=15)
        client.subscribe(symbols, with_trade_for=equity_set)
        ...
        client.close()

    The callback fires from the WebSocket thread; do not touch Tkinter widgets
    directly — schedule via `root.after(...)`.
    """

    def __init__(self, api):
        self._api = api
        self._ws = None
        self._thread = None
        self._token = None
        self._url = None
        self._field_map = {}
        self._on_quote = None
        self._lock = threading.Lock()
        self._last_quotes = {}
        self._subscribed_quote = set()
        self._subscribed_trade = set()
        self._auth_ev = threading.Event()
        self._ready_ev = threading.Event()   # set after FEED_CONFIG arrives
        self._closed = False
        # Heartbeat: DXLink negotiates a 60s KEEPALIVE timeout in SETUP; if we
        # don't send within that window the server drops the connection with
        # 'TIMEOUT The timeout for KEEPALIVE has been reached'.
        self._keepalive_thread = None
        self._keepalive_stop   = threading.Event()

    def set_quote_callback(self, cb):
        self._on_quote = cb

    def get_quote(self, symbol):
        with self._lock:
            return dict(self._last_quotes.get(symbol, {}))

    def connect(self, timeout=30):
        token_data = self._api.get_quote_token()
        if not token_data:
            raise RuntimeError("Could not get DXLink quote token")
        self._url   = _extract_url(token_data)
        self._token = _extract_token(token_data)
        if not self._url or not self._token:
            raise RuntimeError(f"Missing DXLink URL or token: keys={list(token_data.keys())}")

        self._ws = websocket.WebSocketApp(
            self._url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._thread = threading.Thread(target=self._ws.run_forever, daemon=True)
        self._thread.start()
        if not self._ready_ev.wait(timeout=timeout):
            self.close()
            raise RuntimeError("DXLink connect timed out before FEED_CONFIG")
        log.info("DXLinkLiveClient ready for subscriptions")

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
                    self._auth_ev.set()
                    ws.send(json.dumps({
                        "type": "CHANNEL_REQUEST", "channel": 1,
                        "service": "FEED", "parameters": {"contract": "AUTO"},
                    }))
            elif mtype == 'CHANNEL_OPENED' and data.get('channel') == 1:
                ws.send(json.dumps({
                    "type": "FEED_SETUP", "channel": 1,
                    "acceptAggregationPeriod": 10,
                    "acceptDataFormat": "COMPACT",
                    "acceptEventFields": {
                        "Quote": ["eventSymbol", "bidPrice", "askPrice"],
                        "Trade": ["eventSymbol", "price"],
                    },
                }))
            elif mtype == 'FEED_CONFIG' and data.get('channel') == 1:
                for etype, fields in data.get('eventFields', {}).items():
                    self._field_map[etype] = fields
                self._ready_ev.set()
                self._start_keepalive()
            elif mtype == 'FEED_DATA' and data.get('channel') == 1:
                self._handle_feed_data(data.get('data', []))
            elif mtype == 'KEEPALIVE':
                ws.send(json.dumps({"type": "KEEPALIVE", "channel": 0}))
            elif mtype == 'ERROR':
                log.error(f"DXLinkLive server ERROR: {data}")
        except Exception as exc:
            log.error(f"DXLinkLive on_message exception: {exc}")

    def _handle_feed_data(self, raw_data):
        if not isinstance(raw_data, list):
            return
        updates = {}
        i = 0
        while i < len(raw_data):
            item = raw_data[i]
            if isinstance(item, str):
                event_type = item
                i += 1
                if i < len(raw_data) and isinstance(raw_data[i], list):
                    values = raw_data[i]
                    fields = self._field_map.get(event_type, [])
                    if fields:
                        n = len(fields)
                        for j in range(0, len(values), n):
                            chunk = values[j:j + n]
                            if len(chunk) == n:
                                self._apply_event(event_type, dict(zip(fields, chunk)), updates)
                    i += 1
            elif isinstance(item, dict):
                etype = item.get('eventType') or item.get('type', '')
                self._apply_event(etype, item, updates)
                i += 1
            else:
                i += 1
        if self._on_quote and updates:
            for sym, q in updates.items():
                try:
                    self._on_quote(sym, q)
                except Exception as exc:
                    log.error(f"DXLinkLive quote callback error: {exc}")

    def _apply_event(self, event_type, ev, updates):
        sym = ev.get('eventSymbol')
        if not sym:
            return
        with self._lock:
            q = self._last_quotes.setdefault(sym, {})
            if event_type == 'Quote':
                bid = _safe_float(ev.get('bidPrice'))
                ask = _safe_float(ev.get('askPrice'))
                if bid > 0: q['bid'] = bid
                if ask > 0: q['ask'] = ask
            elif event_type == 'Trade':
                price = _safe_float(ev.get('price'))
                if price > 0: q['last'] = price
            updates[sym] = dict(q)

    def _on_error(self, ws, error):
        log.error(f"DXLinkLive WS error: {error}")

    def _on_close(self, ws, code, msg):
        log.debug(f"DXLinkLive WS closed: code={code}")

    def subscribe(self, symbols, with_trade_for=None):
        """Subscribe to Quote events for all `symbols`; Trade events only for
        symbols also present in `with_trade_for` (typically the equity set)."""
        if not symbols or self._closed:
            return
        with_trade_for = set(with_trade_for or ())
        new_q = [s for s in symbols if s not in self._subscribed_quote]
        new_t = [s for s in symbols if s in with_trade_for and s not in self._subscribed_trade]
        if not new_q and not new_t:
            return
        subs = [{"type": "Quote", "symbol": s} for s in new_q]
        subs += [{"type": "Trade", "symbol": s} for s in new_t]
        try:
            self._ws.send(json.dumps({"type": "FEED_SUBSCRIPTION", "channel": 1, "add": subs}))
        except Exception as exc:
            log.warning(f"DXLinkLive subscribe failed: {exc}")
            return
        self._subscribed_quote.update(new_q)
        self._subscribed_trade.update(new_t)
        log.info(f"DXLinkLive subscribed: +{len(new_q)} Quote, +{len(new_t)} Trade "
                 f"(total {len(self._subscribed_quote)} Quote / {len(self._subscribed_trade)} Trade)")

    def _start_keepalive(self):
        if self._keepalive_thread is not None:
            return
        self._keepalive_thread = threading.Thread(
            target=self._keepalive_loop, daemon=True,
            name="DXLinkLive-keepalive",
        )
        self._keepalive_thread.start()
        log.debug("DXLinkLive: keepalive thread started")

    def _keepalive_loop(self):
        # Server timeout is 60s — send well inside that.
        while not self._keepalive_stop.wait(30):
            if self._closed or not self._ws:
                return
            try:
                self._ws.send(json.dumps({"type": "KEEPALIVE", "channel": 0}))
            except Exception as exc:
                log.warning(f"DXLinkLive keepalive send failed: {exc}")
                return

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._keepalive_stop.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
        if self._thread:
            self._thread.join(timeout=3)
        log.info("DXLinkLiveClient closed")


# ─── yfinance ────────────────────────────────────────────────────────────────

def _calendar_dates(cal, key):
    """Pull a list of dates out of a yfinance calendar (handles dict / DataFrame)."""
    if cal is None:
        return []
    try:
        if hasattr(cal, 'loc') and hasattr(cal, 'index'):
            return cal.loc[key].tolist() if key in cal.index else []
        return cal.get(key, []) or []
    except Exception:
        return []


def _next_future_date(candidates, today):
    """First date in candidates that is >= today; returns date or None."""
    if not isinstance(candidates, (list, tuple)):
        candidates = [candidates]
    for d in candidates:
        if hasattr(d, 'date'):
            d = d.date()
        elif isinstance(d, (int, float)):
            try:
                d = datetime.fromtimestamp(d).date()
            except (ValueError, OSError):
                continue
        if hasattr(d, 'year') and d >= today:
            return d
    return None


def _fetch_single_ticker_info(symbol):
    today         = datetime.today().date()
    earnings_date = None
    market_cap    = None
    ex_div_date   = None
    try:
        t    = yf.Ticker(symbol)
        cal  = t.calendar
        info = t.info
        market_cap = info.get('marketCap')

        # Earnings: prefer calendar, fall back to info
        earnings_date = _next_future_date(_calendar_dates(cal, 'Earnings Date'), today)
        if earnings_date is None:
            ed = info.get('earningsDate') or info.get('earningsTimestamps')
            earnings_date = _next_future_date(ed, today)

        # Ex-dividend: same approach. info['exDividendDate'] is a unix timestamp
        # of the *most recent* ex-div in many versions of yfinance, so we filter
        # by today before accepting it.
        ex_div_date = _next_future_date(_calendar_dates(cal, 'Ex-Dividend Date'), today)
        if ex_div_date is None:
            ed = info.get('exDividendDate')
            ex_div_date = _next_future_date(ed, today)
    except Exception:
        pass
    return symbol, earnings_date, market_cap, ex_div_date


def fetch_ticker_info_concurrent(symbols, max_workers=15, progress_cb=None,
                                 ttl_days=7, force_refresh=False):
    """Concurrent yfinance fetch with on-disk cache.

    Cached entries younger than `ttl_days` are reused without scraping. Entries
    whose cached earnings/ex-div date has already passed are also re-fetched.
    `force_refresh=True` ignores the cache entirely.

    Returns (earnings, market_caps, ex_div_dates).
    """
    cache    = {} if force_refresh else load_ticker_cache()
    today    = datetime.today().date()
    now_ts   = time.time()
    ttl_secs = max(ttl_days, 0) * 86400

    earnings, market_caps, ex_div_dates = {}, {}, {}
    to_fetch = []

    for sym in symbols:
        entry = cache.get(sym)
        if entry and ttl_secs > 0 and (now_ts - entry.get('fetched_at', 0)) < ttl_secs:
            ed = _parse_iso_date(entry.get('earnings'))
            xd = _parse_iso_date(entry.get('ex_div'))
            mc = entry.get('market_cap')
            # Re-scrape if a cached date is already in the past — yfinance may
            # have updated to the next event.
            if (ed and ed < today) or (xd and xd < today):
                to_fetch.append(sym)
                continue
            earnings[sym]     = ed
            market_caps[sym]  = mc
            ex_div_dates[sym] = xd
        else:
            to_fetch.append(sym)

    n_cached = len(symbols) - len(to_fetch)
    if to_fetch:
        log.info(f"Ticker info: {n_cached} from cache, {len(to_fetch)} to scrape")
    else:
        log.info(f"Ticker info: all {n_cached} from cache")

    if not to_fetch:
        if progress_cb:
            progress_cb(len(symbols), len(symbols))
        return earnings, market_caps, ex_div_dates

    completed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(_fetch_single_ticker_info, s): s for s in to_fetch}
        for fut in concurrent.futures.as_completed(futs):
            sym, ed, mc, xd = fut.result()
            earnings[sym]     = ed
            market_caps[sym]  = mc
            ex_div_dates[sym] = xd
            cache[sym] = {
                'earnings':   ed.isoformat() if ed else None,
                'market_cap': mc,
                'ex_div':     xd.isoformat() if xd else None,
                'fetched_at': now_ts,
            }
            completed += 1
            if progress_cb:
                progress_cb(n_cached + completed, len(symbols))

    save_ticker_cache(cache)
    return earnings, market_caps, ex_div_dates


# ─── Scan pipeline ────────────────────────────────────────────────────────────

def get_chain_info(symbol, api, target_front_dte, target_back_dte,
                   front_dte_flex=0, back_dte_flex=0):
    """
    Fetch option chain structure for one symbol.
    Always returns a dict; check '_skip_reason' key for failure details.

    `front_dte_flex` / `back_dte_flex`: tolerance window (days) around the
    target DTE. With flex>0 only expirations whose DTE falls within
    [target-flex, target+flex] are eligible; the nearest match in that window
    is picked. With flex==0 the legacy "nearest match across all expirations"
    behaviour is used.
    """
    time.sleep(random.uniform(0.05, 0.15))
    today = datetime.today()
    try:
        chain_data = api.get_option_chain(symbol)
        if not chain_data:
            return {'ticker': symbol, '_skip_reason': 'no_chain_data'}

        expirations = chain_data[0].get('expirations', [])
        if len(expirations) < 2:
            return {'ticker': symbol, '_skip_reason': f'too_few_expirations:{len(expirations)}'}

        exp_dates = [datetime.strptime(e['expiration-date'], '%Y-%m-%d') for e in expirations]
        dtes      = [(d - today).days for d in exp_dates]

        if front_dte_flex > 0:
            cands = [i for i, d in enumerate(dtes)
                     if d > 0 and abs(d - target_front_dte) <= front_dte_flex]
            if not cands:
                return {'ticker': symbol,
                        '_skip_reason': f'no_front_in_flex:{target_front_dte}±{front_dte_flex}'}
            front_idx = min(cands, key=lambda i: abs(dtes[i] - target_front_dte))
        else:
            front_idx = min(range(len(dtes)), key=lambda i: abs(dtes[i] - target_front_dte))

        if back_dte_flex > 0:
            cands = [i for i, d in enumerate(dtes)
                     if d > 0 and abs(d - target_back_dte) <= back_dte_flex]
            if not cands:
                return {'ticker': symbol,
                        '_skip_reason': f'no_back_in_flex:{target_back_dte}±{back_dte_flex}'}
            back_idx = min(cands, key=lambda i: abs(dtes[i] - target_back_dte))
        else:
            back_idx = min(range(len(dtes)), key=lambda i: abs(dtes[i] - target_back_dte))

        if dtes[front_idx] <= 0:
            return {'ticker': symbol, '_skip_reason': f'all_expired:max_dte={max(dtes)}'}
        if back_idx <= front_idx:
            return {'ticker': symbol,
                    '_skip_reason': f'back_not_after_front:f={dtes[front_idx]},b={dtes[back_idx]}'}

        # Restrict strikes to those present in BOTH expirations so any later
        # ATM pick is guaranteed to have a matching back-leg contract.
        front_raw = expirations[front_idx]['strikes']
        back_raw  = expirations[back_idx]['strikes']
        common    = ({s.get('strike-price') for s in front_raw}
                     & {s.get('strike-price') for s in back_raw})
        if not common:
            return {'ticker': symbol, '_skip_reason': 'no_common_strikes'}
        front_strikes = [s for s in front_raw if s.get('strike-price') in common]
        back_strikes  = [s for s in back_raw  if s.get('strike-price') in common]

        return {
            'ticker':        symbol,
            'front_dte':     dtes[front_idx],
            'back_dte':      dtes[back_idx],
            'front_exp_date': exp_dates[front_idx].date(),
            'back_exp_date':  exp_dates[back_idx].date(),
            'front_strikes': front_strikes,
            'back_strikes':  back_strikes,
        }
    except Exception as exc:
        return {'ticker': symbol, '_skip_reason': f'exception:{exc}'}


def calculate_calendar_metrics(ticker, info, option_quotes, iv_method):
    """Calculate calendar spread metrics using pre-fetched DXLink quotes."""
    try:
        price  = info.get('current_price', 0)
        front_sym = info.get('front_streamer_symbol', '')
        back_sym  = info.get('back_streamer_symbol', '')
        strike = info.get('strike', 0)

        if price <= 0 or not front_sym or not back_sym or strike <= 0:
            return None

        fq = option_quotes.get(front_sym, {})
        bq = option_quotes.get(back_sym, {})

        f_bid, f_ask = fq.get('bid', 0), fq.get('ask', 0)
        b_bid, b_ask = bq.get('bid', 0), bq.get('ask', 0)

        if f_bid <= 0 or b_bid <= 0:
            return None

        t1, t2 = info['front_dte'] / 365.0, info['back_dte'] / 365.0

        if iv_method == "Bid Front / Ask Back":
            f_iv = calc_implied_vol(f_bid, price, strike, t1)
            b_iv = calc_implied_vol(b_ask, price, strike, t2)
        else:
            # Midpoint (also used for "Provided Data" since DXLink doesn't stream IV)
            f_iv = calc_implied_vol((f_bid + f_ask) / 2, price, strike, t1)
            b_iv = calc_implied_vol((b_bid + b_ask) / 2, price, strike, t2)

        if f_iv <= 0.01 or b_iv <= 0.01:
            return None

        var_diff = t2 * b_iv**2 - t1 * f_iv**2
        if var_diff < 0:
            return None

        fwd_iv = math.sqrt(var_diff / (t2 - t1))
        if fwd_iv <= 0:
            return None

        ed = info.get('earnings_date')
        return {
            "Ticker":     ticker,
            "Price":      price,
            "Mkt Cap":    info.get('market_cap'),
            "Strike":     strike,
            "F-DTE":      info['front_dte'],
            "B-DTE":      info['back_dte'],
            "F-Bid":      f_bid,
            "F-Ask":      f_ask,
            "B-Bid":      b_bid,
            "B-Ask":      b_ask,
            "Front IV":   f_iv,
            "Back IV":    b_iv,
            "Fwd IV":     fwd_iv,
            "Fwd Factor": (f_iv - fwd_iv) / fwd_iv,
            "Debit":      (b_bid + b_ask) / 2 - (f_bid + f_ask) / 2,
            "F-Spread":   f_ask - f_bid,
            "B-Spread":   b_ask - b_bid,
            "Earnings":   ed.strftime('%Y-%m-%d') if ed else 'N/A',
            "_front_sym": front_sym,
            "_back_sym":  back_sym,
        }
    except Exception as exc:
        log.debug(f"calculate_calendar_metrics {ticker}: {exc}")
        return None


def fmt_market_cap(cap):
    if cap is None or not isinstance(cap, (int, float)) or cap <= 0:
        return "—"
    if cap >= 1e12: return f"${cap / 1e12:.2f}T"
    if cap >= 1e9:  return f"${cap / 1e9:.2f}B"
    if cap >= 1e6:  return f"${cap / 1e6:.0f}M"
    return f"${cap:.0f}"


# ─── GUI ──────────────────────────────────────────────────────────────────────

class FiltersWindow:
    """Modal dialog for scan parameters and filters. Persists to settings.json."""

    IV_METHODS = ("Midpoint", "Bid Front / Ask Back", "Provided Data")

    def __init__(self, parent, settings, on_save):
        self.on_save = on_save
        self.window = tk.Toplevel(parent)
        self.window.title("Filters & Scan Settings")
        self.window.geometry("460x800")
        self.window.minsize(440, 760)
        self.window.transient(parent)
        self.window.grab_set()

        f = ttk.Frame(self.window, padding=14)
        f.pack(fill="both", expand=True)

        ttk.Label(f, text="Scan Parameters",
                  font=("Helvetica", 11, "bold")).pack(anchor="w", pady=(0, 8))

        ttk.Label(f, text="Watchlist CSV:").pack(anchor="w")
        self.csv_path_var = tk.StringVar(value=settings['csv_path'])
        csv_row = ttk.Frame(f)
        csv_row.pack(fill="x", pady=(0, 8))
        ttk.Entry(csv_row, textvariable=self.csv_path_var).pack(side="left", fill="x", expand=True)
        ttk.Button(csv_row, text="…", width=3, command=self._browse).pack(side="right")

        ttk.Label(f, text="Target Front DTE:").pack(anchor="w")
        front_row = ttk.Frame(f)
        front_row.pack(fill="x", pady=(0, 4))
        self.front_dte_var = tk.IntVar(value=settings['front_dte'])
        ttk.Entry(front_row, textvariable=self.front_dte_var,
                  width=8).pack(side="left")
        ttk.Label(front_row, text="  ± flex (days):").pack(side="left")
        self.front_dte_flex_var = tk.IntVar(value=int(settings.get('front_dte_flex', 0) or 0))
        ttk.Entry(front_row, textvariable=self.front_dte_flex_var,
                  width=6).pack(side="left", padx=(4, 0))
        ttk.Label(f, text="0 = pick nearest expiration; >0 = restrict to target ± flex days",
                  foreground="#666", font=("Helvetica", 8)).pack(anchor="w", pady=(0, 8))

        ttk.Label(f, text="Target Back DTE:").pack(anchor="w")
        back_row = ttk.Frame(f)
        back_row.pack(fill="x", pady=(0, 4))
        self.back_dte_var = tk.IntVar(value=settings['back_dte'])
        ttk.Entry(back_row, textvariable=self.back_dte_var,
                  width=8).pack(side="left")
        ttk.Label(back_row, text="  ± flex (days):").pack(side="left")
        self.back_dte_flex_var = tk.IntVar(value=int(settings.get('back_dte_flex', 0) or 0))
        ttk.Entry(back_row, textvariable=self.back_dte_flex_var,
                  width=6).pack(side="left", padx=(4, 0))
        ttk.Label(f, text="0 = pick nearest expiration; >0 = restrict to target ± flex days",
                  foreground="#666", font=("Helvetica", 8)).pack(anchor="w", pady=(0, 8))

        ttk.Label(f, text="IV Method:").pack(anchor="w")
        self.iv_method_var = tk.StringVar(value=settings['iv_method'])
        cb = ttk.Combobox(f, textvariable=self.iv_method_var, state="readonly")
        cb['values'] = self.IV_METHODS
        cb.pack(fill="x", pady=(0, 8))

        ttk.Separator(f, orient='horizontal').pack(fill='x', pady=8)
        ttk.Label(f, text="Filters",
                  font=("Helvetica", 11, "bold")).pack(anchor="w", pady=(0, 8))

        ttk.Label(f, text="Min Price ($):").pack(anchor="w")
        self.min_price_var = tk.DoubleVar(value=settings['min_price'])
        ttk.Entry(f, textvariable=self.min_price_var).pack(fill="x", pady=(0, 8))

        ttk.Label(f, text="Min Market Cap ($B, blank = no filter):").pack(anchor="w")
        self.min_market_cap_var = tk.StringVar(value=settings['min_market_cap_b'])
        ttk.Entry(f, textvariable=self.min_market_cap_var).pack(fill="x", pady=(0, 8))

        self.filter_front_earnings_var = tk.BooleanVar(value=settings['filter_front_earnings'])
        ttk.Checkbutton(f, text="Exclude tickers with earnings before front-leg expiry",
                        variable=self.filter_front_earnings_var).pack(anchor="w", pady=2)

        self.filter_back_earnings_var = tk.BooleanVar(value=settings['filter_back_earnings'])
        ttk.Checkbutton(f, text="Exclude tickers with earnings before back-leg expiry",
                        variable=self.filter_back_earnings_var).pack(anchor="w", pady=2)

        self.filter_front_dividend_var = tk.BooleanVar(value=settings['filter_front_dividend'])
        ttk.Checkbutton(f, text="Exclude tickers with ex-dividend before front-leg expiry",
                        variable=self.filter_front_dividend_var).pack(anchor="w", pady=2)

        self.filter_back_dividend_var = tk.BooleanVar(value=settings['filter_back_dividend'])
        ttk.Checkbutton(f, text="Exclude tickers with ex-dividend before back-leg expiry",
                        variable=self.filter_back_dividend_var).pack(anchor="w", pady=2)

        self.filter_unknown_earnings_var = tk.BooleanVar(
            value=settings.get('filter_unknown_earnings', False))
        ttk.Checkbutton(
            f, text="Exclude tickers with no recorded earnings date  (ETFs, leveraged funds)",
            variable=self.filter_unknown_earnings_var).pack(anchor="w", pady=2)

        ttk.Separator(f, orient='horizontal').pack(fill='x', pady=8)
        ttk.Label(f, text="Ticker Info Cache",
                  font=("Helvetica", 11, "bold")).pack(anchor="w", pady=(0, 8))

        ttk.Label(f, text="Refresh after (days, 0 = always re-fetch):").pack(anchor="w")
        self.ticker_ttl_var = tk.IntVar(value=settings.get('ticker_info_ttl_days', 7))
        ttk.Entry(f, textvariable=self.ticker_ttl_var).pack(fill="x", pady=(0, 4))
        ttk.Button(f, text="Refresh ticker data now (clear cache)",
                   command=self._refresh_cache).pack(fill="x", pady=(0, 8))

        # Buttons
        btn_row = ttk.Frame(f)
        btn_row.pack(fill="x", side="bottom", pady=(16, 0))
        ttk.Button(btn_row, text="Cancel",
                   command=self.window.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(btn_row, text="Save",
                   command=self._save).pack(side="right")

    def _browse(self):
        fn = filedialog.askopenfilename(
            parent=self.window,
            filetypes=[("CSV", "*.csv"), ("All", "*.*")],
        )
        if fn:
            self.csv_path_var.set(fn)

    def _refresh_cache(self):
        clear_ticker_cache()
        messagebox.showinfo(
            "Cache cleared",
            "Ticker info cache cleared. The next scan will re-scrape "
            "earnings, dividends and market caps for all tickers.",
            parent=self.window,
        )

    def _save(self):
        try:
            settings = {
                'csv_path':              self.csv_path_var.get(),
                'front_dte':             int(self.front_dte_var.get()),
                'back_dte':              int(self.back_dte_var.get()),
                'front_dte_flex':        max(int(self.front_dte_flex_var.get()), 0),
                'back_dte_flex':         max(int(self.back_dte_flex_var.get()), 0),
                'iv_method':             self.iv_method_var.get(),
                'min_price':             float(self.min_price_var.get()),
                'min_market_cap_b':      self.min_market_cap_var.get().strip(),
                'filter_front_earnings': self.filter_front_earnings_var.get(),
                'filter_back_earnings':  self.filter_back_earnings_var.get(),
                'filter_front_dividend':   self.filter_front_dividend_var.get(),
                'filter_back_dividend':    self.filter_back_dividend_var.get(),
                'filter_unknown_earnings': self.filter_unknown_earnings_var.get(),
                'ticker_info_ttl_days':    max(int(self.ticker_ttl_var.get()), 0),
            }
        except (TypeError, ValueError) as e:
            messagebox.showwarning(
                "Invalid Input",
                f"Check your numeric fields: {e}",
                parent=self.window,
            )
            return
        if settings['front_dte'] >= settings['back_dte']:
            messagebox.showwarning(
                "Invalid DTEs",
                "Back DTE must be greater than Front DTE.",
                parent=self.window,
            )
            return
        save_settings(settings)
        self.on_save(settings)
        self.window.destroy()


# ─── P/L chart window ────────────────────────────────────────────────────────

def _bs_call_vec(S_arr, K, T, r, sigma):
    """Vectorized Black-Scholes call price. At T<=0 returns intrinsic."""
    S_arr = np.asarray(S_arr, dtype=float)
    if T <= 0 or sigma <= 0:
        return np.maximum(S_arr - K, 0.0)
    sqrtT = math.sqrt(T)
    d1 = (np.log(S_arr / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    return S_arr * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)


class PLChartWindow:
    """Interactive P/L chart at the front-leg expiration date."""

    R = 0.04            # risk-free rate
    PER_CONTRACT = 100  # share multiplier

    def __init__(self, parent, *, ticker, price, strike, front_dte, back_dte,
                 back_paid, front_credit, current_back_iv, fwd_iv):
        self.window = tk.Toplevel(parent)
        self.window.title(f"P/L at Front Expiration — {ticker}")
        self.window.geometry("960x720")

        self.K               = float(strike)
        self.S0              = float(price)
        self.t_remaining     = max((back_dte - front_dte) / 365.0, 1e-6)
        self.back_paid       = float(back_paid)
        self.front_credit    = float(front_credit)
        self.current_back_iv = float(current_back_iv) if current_back_iv > 0 else 0.0
        self.fwd_iv          = float(fwd_iv) if fwd_iv > 0 else 0.0

        # Underlying price grid: ±35% around current price
        lo = max(self.S0 * 0.65, 0.01)
        hi = self.S0 * 1.35
        self.S_range = np.linspace(lo, hi, 300)

        # Cache the static reference curves
        self._pl_current = (self._pl_curve(self.current_back_iv)
                            if self.current_back_iv > 0 else None)
        self._pl_fwd     = (self._pl_curve(self.fwd_iv)
                            if self.fwd_iv > 0 else None)

        self._build_chart(ticker)
        self._build_controls()
        self._update()

    # ── chart math ────────────────────────────────────────────────────────────

    def _pl_curve(self, back_iv):
        """P/L per contract (× 100) at front expiration, as a function of S."""
        front_intrinsic = np.maximum(self.S_range - self.K, 0.0)
        back_value = _bs_call_vec(
            self.S_range, self.K, self.t_remaining, self.R, back_iv,
        )
        per_share = back_value - front_intrinsic - self.back_paid + self.front_credit
        return per_share * self.PER_CONTRACT

    @staticmethod
    def _breakevens(S, pl):
        """Approximate breakeven prices from sign changes in pl."""
        bes = []
        for i in range(1, len(pl)):
            if pl[i - 1] == 0:
                bes.append(S[i - 1])
            elif (pl[i - 1] < 0) != (pl[i] < 0):
                # Linear interp between the two points
                x0, x1 = S[i - 1], S[i]
                y0, y1 = pl[i - 1], pl[i]
                bes.append(x0 - y0 * (x1 - x0) / (y1 - y0))
        return bes

    # ── widgets ───────────────────────────────────────────────────────────────

    def _build_chart(self, ticker):
        canvas_frame = ttk.Frame(self.window)
        canvas_frame.pack(fill="both", expand=True)

        self.fig = Figure(figsize=(9, 5), dpi=100)
        self.ax  = self.fig.add_subplot(111)

        ax = self.ax
        ax.set_title(
            f"{ticker} calendar spread — P/L at front-leg expiration "
            f"(per contract = 100 shares)"
        )
        ax.set_xlabel("Underlying price at front expiration ($)")
        ax.set_ylabel("P/L ($ per contract)")
        ax.axhline(0, color='black', linewidth=0.7)
        ax.axvline(self.K, color='gray', linewidth=0.7, linestyle=':',
                   label=f'Strike ${self.K:.2f}')
        ax.axvline(self.S0, color='dimgray', linewidth=0.7, linestyle='--',
                   label=f'Spot ${self.S0:.2f}')

        if self._pl_current is not None:
            ax.plot(self.S_range, self._pl_current, color='gray', linestyle='--',
                    linewidth=1.0,
                    label=f'Current back IV ({self.current_back_iv * 100:.1f}%)')

        if self._pl_fwd is not None:
            ax.plot(self.S_range, self._pl_fwd, color='gray', linestyle=':',
                    linewidth=1.0,
                    label=f'Implied fwd IV ({self.fwd_iv * 100:.1f}%)')

        # Slider-controlled line. Updated in _update.
        (self.user_line,) = ax.plot(
            [], [], color='royalblue', linewidth=2.0, label='Slider IV',
        )
        ax.grid(True, alpha=0.3)

        self.canvas = FigureCanvasTkAgg(self.fig, master=canvas_frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

        toolbar = NavigationToolbar2Tk(self.canvas, canvas_frame)
        toolbar.update()

    def _build_controls(self):
        bar = ttk.Frame(self.window, padding=10)
        bar.pack(fill="x")

        # Sensible default for the slider: current back IV if known, else fwd IV, else 35%
        default_iv_pct = (self.current_back_iv if self.current_back_iv > 0
                          else self.fwd_iv if self.fwd_iv > 0 else 0.35) * 100

        ttk.Label(bar, text="Back-leg IV at front expiration:",
                  font=("Helvetica", 10, "bold")).pack(side="left")

        self.iv_var = tk.DoubleVar(value=default_iv_pct)
        self.iv_label_var = tk.StringVar()
        ttk.Label(bar, textvariable=self.iv_label_var, width=8,
                  font=("Helvetica", 10, "bold"),
                  foreground="royalblue").pack(side="right", padx=(8, 0))

        slider = tk.Scale(
            bar, from_=5.0, to=200.0, resolution=0.5,
            variable=self.iv_var, orient="horizontal",
            showvalue=False, length=400,
        )
        slider.pack(side="left", fill="x", expand=True, padx=(10, 10))

        ttk.Button(bar, text="Reset",
                   command=lambda: self.iv_var.set(default_iv_pct)).pack(side="right")

        self.iv_var.trace_add("write", lambda *_: self._update())

        self.metrics_var = tk.StringVar()
        ttk.Label(self.window, textvariable=self.metrics_var,
                  font=("Courier", 9), padding=(10, 0, 10, 8),
                  justify="left").pack(fill="x", anchor="w")

    def _update(self):
        try:
            iv_pct = float(self.iv_var.get())
        except (tk.TclError, ValueError):
            return
        slider_iv = max(iv_pct / 100.0, 1e-4)
        self.iv_label_var.set(f"{iv_pct:.1f}%")

        pl_user = self._pl_curve(slider_iv)
        self.user_line.set_data(self.S_range, pl_user)
        self.user_line.set_label(f'Slider back IV ({iv_pct:.1f}%)')

        # Y-axis bounds across all visible curves
        stacks = [pl_user]
        if self._pl_current is not None: stacks.append(self._pl_current)
        if self._pl_fwd     is not None: stacks.append(self._pl_fwd)
        ymin = float(min(v.min() for v in stacks))
        ymax = float(max(v.max() for v in stacks))
        margin = max((ymax - ymin) * 0.1, 1.0)
        self.ax.set_xlim(self.S_range[0], self.S_range[-1])
        self.ax.set_ylim(ymin - margin, ymax + margin)
        self.ax.legend(loc='upper left', fontsize=8)

        # Metrics panel
        max_profit = float(pl_user.max())
        max_S = float(self.S_range[int(np.argmax(pl_user))])
        bes = self._breakevens(self.S_range, pl_user)
        be_str = ", ".join(f"${b:.2f}" for b in bes) if bes else "none in range"
        net_debit_pc = (self.back_paid - self.front_credit) * self.PER_CONTRACT
        self.metrics_var.set(
            f"Net debit (entry):  ${net_debit_pc:.2f} per contract"
            f"   |   Max profit on slider line: ${max_profit:.2f} at S=${max_S:.2f}"
            f"\nBreakevens: {be_str}"
        )

        self.canvas.draw_idle()


# ─── Main scanner app ────────────────────────────────────────────────────────

class CalendarScannerApp:
    LIVE_TOP_N      = 50      # number of result rows to keep streamed
    LIVE_FLUSH_MS   = 750     # coalesce live updates to one UI flush every N ms

    def __init__(self, root):
        self.root = root
        self.root.title("Calendar Spread Edge Screener (Tastytrade)")
        self.root.geometry("1480x840")
        self._row_data = {}
        self.settings = load_settings()
        self._api = None

        # Live streaming state
        self._live_client          = None
        self._live_iv_method       = self.settings.get('iv_method', 'Midpoint')
        self._live_sym_to_items    = {}    # streamer-symbol -> set(item_id) it feeds
        self._live_dirty_items     = set() # item_ids needing UI refresh
        self._live_flush_pending   = False
        self._live_lock            = threading.Lock()
        self._flash_widgets        = {}    # (iid, column_id) -> Tk Label overlay
        self._flash_columns        = ("Price", "F-Bid", "F-Ask", "B-Bid", "B-Ask")
        self._auto_sort_active     = False  # toggled by Fwd Factor header click

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        style = ttk.Style()
        style.theme_use('clam')

        self.left_frame  = ttk.Frame(root, padding="10")
        self.right_frame = ttk.Frame(root, padding="10")
        self.left_frame.pack(side="left",  fill="y",    expand=False)
        self.right_frame.pack(side="right", fill="both", expand=True)

        self._build_sidebar()
        self._build_main_area()

        log.attach(self._debug_text)
        log.info("Calendar Spread Scanner started")
        log.info(f"Log file: {_LOG_PATH}")
        if not _KEYRING_AVAILABLE:
            log.warning("'keyring' is not installed — credentials will fall back to plaintext JSON")
        if not _MPL_AVAILABLE:
            log.warning("'matplotlib' is not installed — P/L chart will be unavailable")

    # ── Sidebar ──────────────────────────────────────────────────────────────

    def _build_sidebar(self):
        f = self.left_frame

        ttk.Label(f, text="Tastytrade OAuth",
                  font=("Helvetica", 11, "bold")).pack(anchor="w", pady=(0, 8))

        saved_secret, saved_refresh = load_credentials()

        ttk.Label(f, text="Client Secret:").pack(anchor="w")
        self.client_secret_var = tk.StringVar(value=saved_secret)
        ttk.Entry(f, textvariable=self.client_secret_var, show="*",
                  width=30).pack(fill="x", pady=(0, 8))

        ttk.Label(f, text="Refresh Token:").pack(anchor="w")
        self.refresh_token_var = tk.StringVar(value=saved_refresh)
        ttk.Entry(f, textvariable=self.refresh_token_var, show="*",
                  width=30).pack(fill="x", pady=(0, 4))

        if _KEYRING_AVAILABLE:
            auth_msg = "Stored in OS keyring (Linux Secret Service / Keychain / Credential Manager)."
            auth_color = "#666"
        else:
            auth_msg = "WARNING: 'keyring' not installed — credentials stored in plaintext JSON. Run: pip install keyring"
            auth_color = "#a55"
        ttk.Label(f, text=auth_msg, foreground=auth_color, wraplength=230,
                  font=("Helvetica", 8)).pack(anchor="w", pady=(0, 8))

        ttk.Separator(f, orient='horizontal').pack(fill='x', pady=8)

        ttk.Button(f, text="Filters & Scan Settings…",
                   command=self._open_filters).pack(fill="x", pady=4)

        self.run_btn = ttk.Button(f, text="Run Scanner", command=self.start_scan)
        self.run_btn.pack(fill="x", pady=4)

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(f, textvariable=self.status_var,
                  wraplength=230).pack(anchor="w", pady=8)

        self.progress = ttk.Progressbar(f, mode='determinate')
        self.progress.pack(fill="x")

    def _open_filters(self):
        FiltersWindow(self.root, self.settings, on_save=self._on_filters_saved)

    def _on_filters_saved(self, new_settings):
        self.settings = new_settings
        log.info(f"Filters updated: {new_settings}")

    # ── Main area: Results tab + Debug Log tab ────────────────────────────────

    def _build_main_area(self):
        nb = ttk.Notebook(self.right_frame)
        nb.pack(fill="both", expand=True)

        results_tab = ttk.Frame(nb)
        debug_tab   = ttk.Frame(nb)
        nb.add(results_tab, text="  Results  ")
        nb.add(debug_tab,   text="  Debug Log  ")

        self._build_results_tab(results_tab)
        self._build_debug_tab(debug_tab)

    def _build_results_tab(self, parent):
        tree_frame = ttk.Frame(parent)
        tree_frame.pack(fill="both", expand=True)

        columns = ("Ticker", "Price", "Mkt Cap", "Strike", "F-DTE", "B-DTE",
                   "F-Bid", "F-Ask", "B-Bid", "B-Ask",
                   "Front IV", "Back IV", "Fwd IV", "Fwd Factor",
                   "Debit", "F-Spread", "B-Spread", "Earnings")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings")

        col_widths = {
            "Ticker": 60, "Price": 70, "Mkt Cap": 78, "Strike": 70,
            "F-DTE": 50, "B-DTE": 50,
            "F-Bid": 60, "F-Ask": 60, "B-Bid": 60, "B-Ask": 60,
            "Front IV": 68, "Back IV": 68, "Fwd IV": 68, "Fwd Factor": 80,
            "Debit": 64, "F-Spread": 68, "B-Spread": 68, "Earnings": 90,
        }
        for col in columns:
            if col == "Fwd Factor":
                self.tree.heading(col, text=col,
                                  command=self._toggle_fwd_factor_sort)
            else:
                self.tree.heading(col, text=col)
            self.tree.column(col, width=col_widths.get(col, 70), anchor="center")

        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL,
                            command=self._on_tree_yscroll)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_row_select)
        # Flash overlays are placed at absolute (x,y) over the Treeview, so any
        # scroll/resize would leave them on the wrong cell — clear them on
        # those events.
        for evt in ("<MouseWheel>", "<Button-4>", "<Button-5>", "<Configure>"):
            self.tree.bind(evt, lambda _e: self._clear_all_flashes(), add="+")

        # Trade analysis panel
        trade_frame = ttk.LabelFrame(parent, text="Analyse Selected Trade", padding="8")
        trade_frame.pack(fill="x", pady=(8, 0))

        self.selected_label_var = tk.StringVar(value="Select a row above to analyse a trade")
        ttk.Label(trade_frame, textvariable=self.selected_label_var,
                  foreground="gray").grid(row=0, column=0, columnspan=8, sticky="w", pady=(0, 4))

        # Row 1: three linked entries — Back Paid, Front Credit, Net Debit.
        # Editing any leg recomputes Net Debit; editing Net Debit recomputes
        # whichever leg is *not* locked (see radio buttons on row 2).
        self._trade_updating = False  # re-entrancy guard for the trace handlers

        ttk.Label(trade_frame, text="Back Leg Paid ($):").grid(
            row=1, column=0, sticky="e", padx=(0, 4))
        self.back_price_var = tk.StringVar()
        ttk.Entry(trade_frame, textvariable=self.back_price_var, width=8).grid(
            row=1, column=1, padx=(0, 12))

        ttk.Label(trade_frame, text="Front Leg Credit ($):").grid(
            row=1, column=2, sticky="e", padx=(0, 4))
        self.front_credit_var = tk.StringVar()
        ttk.Entry(trade_frame, textvariable=self.front_credit_var, width=8).grid(
            row=1, column=3, padx=(0, 12))

        ttk.Label(trade_frame, text="Net Debit ($):").grid(
            row=1, column=4, sticky="e", padx=(0, 4))
        self.net_debit_var = tk.StringVar()
        ttk.Entry(trade_frame, textvariable=self.net_debit_var, width=8).grid(
            row=1, column=5, padx=(0, 12))

        ttk.Button(trade_frame, text="Calculate & chart",
                   command=self._calc_real_fwd_factor).grid(row=1, column=6, padx=(0, 10))

        # Row 2: lock selector + result label.
        ttk.Label(trade_frame, text="When changing Net Debit, lock:").grid(
            row=2, column=0, columnspan=2, sticky="e", padx=(0, 4), pady=(6, 0))
        self.trade_lock_var = tk.StringVar(value="front")
        ttk.Radiobutton(trade_frame, text="Front credit",
                        variable=self.trade_lock_var, value="front").grid(
            row=2, column=2, sticky="w", pady=(6, 0))
        ttk.Radiobutton(trade_frame, text="Back paid",
                        variable=self.trade_lock_var, value="back").grid(
            row=2, column=3, sticky="w", pady=(6, 0))

        self.real_fwd_result_var = tk.StringVar(value="")
        ttk.Label(trade_frame, textvariable=self.real_fwd_result_var,
                  foreground="blue").grid(
            row=2, column=4, columnspan=4, sticky="w", pady=(6, 0))

        self.back_price_var.trace_add("write", self._on_leg_var_change)
        self.front_credit_var.trace_add("write", self._on_leg_var_change)
        self.net_debit_var.trace_add("write", self._on_net_debit_change)

    def _build_debug_tab(self, parent):
        toolbar = ttk.Frame(parent)
        toolbar.pack(fill="x", pady=(4, 2))
        ttk.Button(toolbar, text="Clear",
                   command=self._clear_debug).pack(side="left", padx=4)
        ttk.Button(toolbar, text="Open log file",
                   command=self._open_log_file).pack(side="left", padx=4)
        ttk.Label(toolbar, text=str(_LOG_PATH),
                  foreground="#888888").pack(side="left", padx=8)

        self._debug_text = tk.Text(
            parent, wrap="word", state='disabled',
            font=("Courier", 9), bg="#1e1e1e", fg="#d4d4d4",
        )
        vsb = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=self._debug_text.yview)
        self._debug_text.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self._debug_text.pack(fill="both", expand=True)

    def _clear_debug(self):
        self._debug_text.configure(state='normal')
        self._debug_text.delete('1.0', tk.END)
        self._debug_text.configure(state='disabled')

    def _open_log_file(self):
        try:
            if hasattr(os, 'startfile'):
                os.startfile(str(_LOG_PATH))
            else:
                subprocess.Popen(['xdg-open', str(_LOG_PATH)],
                                 stderr=subprocess.DEVNULL)
        except Exception:
            messagebox.showinfo("Log File", f"Log location:\n{_LOG_PATH}")

    # ── Row select / trade analysis ───────────────────────────────────────────

    def _on_row_select(self, event):
        sel = self.tree.selection()
        if not sel:
            return
        data = self._row_data.get(sel[0])
        if not data:
            return
        self.selected_label_var.set(
            f"{data['ticker']}  |  Price: ${data['price']:.2f}  |  "
            f"Strike: ${data['strike']:.2f}  |  "
            f"F-DTE: {data['front_dte']}  |  B-DTE: {data['back_dte']}"
        )
        self.real_fwd_result_var.set("")

    @staticmethod
    def _try_float(s):
        try:
            return float(s)
        except (TypeError, ValueError):
            return None

    def _on_leg_var_change(self, *_):
        """User typed in Back Paid or Front Credit -> recompute Net Debit."""
        if self._trade_updating:
            return
        b = self._try_float(self.back_price_var.get())
        f = self._try_float(self.front_credit_var.get())
        if b is None or f is None:
            return
        self._trade_updating = True
        try:
            self.net_debit_var.set(f"{b - f:.2f}")
        finally:
            self._trade_updating = False

    def _on_net_debit_change(self, *_):
        """User typed in Net Debit -> back-solve the *unlocked* leg so the
        locked leg's price stays fixed (per the radio selection)."""
        if self._trade_updating:
            return
        nd = self._try_float(self.net_debit_var.get())
        if nd is None:
            return
        lock = self.trade_lock_var.get()
        self._trade_updating = True
        try:
            if lock == "front":
                f = self._try_float(self.front_credit_var.get())
                if f is not None:
                    self.back_price_var.set(f"{nd + f:.2f}")
            elif lock == "back":
                b = self._try_float(self.back_price_var.get())
                if b is not None:
                    self.front_credit_var.set(f"{b - nd:.2f}")
        finally:
            self._trade_updating = False

    def _calc_real_fwd_factor(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("No Selection", "Select a trade row first.")
            return
        data = self._row_data.get(sel[0])
        if not data:
            return
        try:
            back_price   = float(self.back_price_var.get())
            front_credit = float(self.front_credit_var.get())
        except ValueError:
            messagebox.showwarning("Invalid Input", "Enter numeric values for both prices.")
            return
        if back_price <= 0 or front_credit <= 0:
            messagebox.showwarning("Invalid Input", "Both prices must be positive.")
            return

        price  = data['price']
        strike = data['strike']
        t1, t2 = data['front_dte'] / 365.0, data['back_dte'] / 365.0

        f_iv = calc_implied_vol(front_credit, price, strike, t1)
        b_iv = calc_implied_vol(back_price,   price, strike, t2)

        var_diff = t2 * b_iv ** 2 - t1 * f_iv ** 2
        if var_diff < 0:
            self.real_fwd_result_var.set("Cannot compute — negative variance diff")
            return
        fwd_iv = math.sqrt(var_diff / (t2 - t1))
        if fwd_iv <= 0:
            self.real_fwd_result_var.set("Cannot compute — zero forward IV")
            return

        ff = (f_iv - fwd_iv) / fwd_iv
        self.real_fwd_result_var.set(
            f"Real Fwd Factor: {ff * 100:.2f}%   "
            f"(F-IV: {f_iv * 100:.1f}%  B-IV: {b_iv * 100:.1f}%  Fwd-IV: {fwd_iv * 100:.1f}%)"
        )

        # Open the interactive P/L chart. Use the back IV solved from the actual
        # fill so the chart's reference line reflects what the trader paid for.
        if not _MPL_AVAILABLE:
            messagebox.showinfo(
                "Chart unavailable",
                "matplotlib is not installed.\nRun:  pip install matplotlib",
            )
            return
        try:
            PLChartWindow(
                self.root,
                ticker=data['ticker'],
                price=price,
                strike=strike,
                front_dte=data['front_dte'],
                back_dte=data['back_dte'],
                back_paid=back_price,
                front_credit=front_credit,
                current_back_iv=b_iv,
                fwd_iv=fwd_iv,
            )
        except Exception as exc:
            log.error(f"Failed to open P/L chart: {exc}")
            messagebox.showerror("Chart error", f"Could not open chart: {exc}")

    # ── Scan entry ────────────────────────────────────────────────────────────

    def start_scan(self):
        client_secret = self.client_secret_var.get().strip()
        refresh_token = self.refresh_token_var.get().strip()
        if not client_secret or not refresh_token:
            messagebox.showwarning(
                "Missing Credentials",
                "Enter your OAuth client_secret and refresh_token.\n\n"
                "Get them from my.tastytrade.com → My Profile → Manage → "
                "OAuth Applications → Create Grant.",
            )
            return
        save_credentials(client_secret, refresh_token)
        self.run_btn.config(state="disabled")
        # Tear down any active live stream before rebuilding the table.
        self._stop_live_streaming()
        for item in self.tree.get_children():
            self.tree.delete(item)
        self._row_data.clear()
        threading.Thread(target=self._scan_logic, daemon=True).start()

    # ── Scan logic (background thread) ───────────────────────────────────────

    def _scan_logic(self):
        s = self.settings

        # Auth
        try:
            self._status("Authenticating with Tastytrade (OAuth)…")
            api = TastytradeAPI(self.client_secret_var.get().strip(),
                                self.refresh_token_var.get().strip())
            self._api = api
        except Exception as e:
            self._status(f"Auth Error: {e}", done=True)
            return

        # Load tickers
        file_path = s['csv_path']
        if os.path.exists(file_path):
            with open(file_path, 'r') as fh:
                tickers = [
                    line.strip().split(',')[0].upper()
                    for line in fh
                    if line.strip() and not line.lower().startswith('ticker')
                ]
            log.info(f"Loaded {len(tickers)} tickers from {file_path}")
        else:
            self._status(f"'{file_path}' not found — using fallback list.")
            log.warning(f"CSV not found: {file_path}")
            tickers = ['SPY', 'QQQ', 'AAPL', 'TSLA', 'NVDA', 'IWM', 'AMD']

        f_dte         = s['front_dte']
        b_dte         = s['back_dte']
        f_flex        = int(s.get('front_dte_flex', 0) or 0)
        b_flex        = int(s.get('back_dte_flex', 0) or 0)
        iv_method     = s['iv_method']
        min_price     = float(s['min_price'])
        filter_f_earn = s['filter_front_earnings']
        filter_b_earn = s['filter_back_earnings']
        filter_f_div  = s['filter_front_dividend']
        filter_b_div  = s['filter_back_dividend']
        filter_no_earn = s.get('filter_unknown_earnings', False)
        ttl_days      = int(s.get('ticker_info_ttl_days', 7))
        cap_str       = s['min_market_cap_b'].strip() if isinstance(s['min_market_cap_b'], str) else ''
        try:
            min_cap = float(cap_str) * 1e9 if cap_str else 0.0
        except ValueError:
            min_cap = 0.0
        total = len(tickers)

        # ── Phase 0: Ticker info (cached) + pre-filter ───────────────────────
        # Fetch earnings/market-cap/ex-div before option chains so we can drop
        # excluded tickers BEFORE doing expensive chain requests.
        self._status(f"Phase 1/4: Ticker info ({total} tickers, cache TTL={ttl_days}d)…")
        self._progress(0)
        log.info(f"Phase 0: ticker info fetch ({total} tickers, ttl={ttl_days}d)")

        def _ep(done, tot):
            self._progress(int(done / tot * 20))
            self._status(f"Phase 1/4: Ticker info {done}/{tot}…")

        earnings_map, cap_map, ex_div_map = fetch_ticker_info_concurrent(
            tickers, progress_cb=_ep, ttl_days=ttl_days,
        )

        # Conservative pre-filter cutoffs: actual front/back expirations may
        # differ from the user's targets by a few days, so we use a 7-day
        # safety buffer (drop only tickers whose earnings/ex-div is well before
        # the *earliest possible* expiry). Edge cases at the actual-expiry
        # boundary are caught again post-chain.
        today = datetime.today().date()
        safety = timedelta(days=7)
        front_pre_cut = today + timedelta(days=f_dte) - safety
        back_pre_cut  = today + timedelta(days=b_dte) - safety

        survivors = []
        dropped_cap = dropped_earn = dropped_div = dropped_no_earn = 0
        for t in tickers:
            mc = cap_map.get(t)
            if min_cap > 0 and (mc is None or mc < min_cap):
                dropped_cap += 1
                continue
            ed = earnings_map.get(t)
            if filter_no_earn and ed is None:
                dropped_no_earn += 1
                continue
            if ed and (
                (filter_f_earn and ed <= front_pre_cut) or
                (filter_b_earn and ed <= back_pre_cut)
            ):
                dropped_earn += 1
                continue
            xd = ex_div_map.get(t)
            if xd and (
                (filter_f_div and xd <= front_pre_cut) or
                (filter_b_div and xd <= back_pre_cut)
            ):
                dropped_div += 1
                continue
            survivors.append(t)

        n_dropped = dropped_cap + dropped_earn + dropped_div + dropped_no_earn
        if n_dropped:
            log.info(
                f"Pre-filter dropped: market_cap={dropped_cap} "
                f"earnings={dropped_earn} dividend={dropped_div} "
                f"no_earnings_date={dropped_no_earn}; "
                f"{len(survivors)}/{total} remain"
            )
            self._status(
                f"Pre-filter dropped {n_dropped}; "
                f"{len(survivors)} remain — fetching chains…"
            )

        if not survivors:
            self._status("All tickers excluded by pre-filter.", done=True)
            return

        # ── Phase 1: Option chain structure (survivors only) ─────────────────
        self._status(f"Phase 2/4: Fetching option chains ({len(survivors)} tickers)…")
        self._progress(20)
        log.info(f"Phase 1: chain fetch for {len(survivors)} survivors  "
                 f"front_dte={f_dte}±{f_flex}  back_dte={b_dte}±{b_flex}")

        chain_infos  = {}
        skip_reasons = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(get_chain_info, t, api, f_dte, b_dte,
                                       f_flex, b_flex): t for t in survivors}
            n_total = len(survivors)
            for i, future in enumerate(concurrent.futures.as_completed(futures)):
                result = future.result()
                if result:
                    if '_skip_reason' in result:
                        key = result['_skip_reason'].split(':')[0]
                        skip_reasons[key] = skip_reasons.get(key, 0) + 1
                    else:
                        chain_infos[result['ticker']] = result
                if (i + 1) % 10 == 0 or (i + 1) == n_total:
                    self._progress(20 + int((i + 1) / n_total * 30))

        log.info(f"Phase 1 done: {len(chain_infos)} valid, skips={skip_reasons}")

        if not chain_infos:
            reasons = ', '.join(f'{k}:{v}' for k, v in sorted(skip_reasons.items()))
            self._status(f"No valid option chains found. Reasons: {reasons or 'unknown'}",
                         done=True)
            return

        # Attach ticker info to surviving chains
        for ticker, info in chain_infos.items():
            info['earnings_date'] = earnings_map.get(ticker)
            info['market_cap']    = cap_map.get(ticker)
            info['ex_div_date']   = ex_div_map.get(ticker)

        # Post-filter against the ACTUAL expiration dates — pre-filter used the
        # target DTEs with a safety buffer, so a few edge-case tickers may slip
        # through and need to be dropped here.
        if filter_f_earn or filter_b_earn:
            removed = [
                t for t, info in chain_infos.items()
                if info['earnings_date'] and (
                    (filter_f_earn and info['earnings_date'] <= info['front_exp_date']) or
                    (filter_b_earn and info['earnings_date'] <= info['back_exp_date'])
                )
            ]
            for t in removed:
                del chain_infos[t]
            if removed:
                log.info(f"Earnings post-filter removed {len(removed)}; {len(chain_infos)} remain")

        if filter_f_div or filter_b_div:
            removed = [
                t for t, info in chain_infos.items()
                if info['ex_div_date'] and (
                    (filter_f_div and info['ex_div_date'] <= info['front_exp_date']) or
                    (filter_b_div and info['ex_div_date'] <= info['back_exp_date'])
                )
            ]
            for t in removed:
                del chain_infos[t]
            if removed:
                log.info(f"Dividend post-filter removed {len(removed)}; {len(chain_infos)} remain")

        if not chain_infos:
            self._status("All tickers filtered out.", done=True)
            return

        # Phase 2a: Equity quotes
        equity_syms = list(chain_infos.keys())
        self._status(f"Phase 3/4: Equity quotes ({len(equity_syms)} symbols)…")
        self._indeterminate(True)
        log.info(f"Phase 2a: {len(equity_syms)} equity symbols")

        eq_quotes, eq_diags = fetch_quotes_with_retry(api, equity_syms, timeout=25)
        self._indeterminate(False)

        n_priced = sum(
            1 for t in equity_syms
            if eq_quotes.get(t, {}).get('last', 0) > 0
            or (eq_quotes.get(t, {}).get('bid', 0) > 0 and eq_quotes.get(t, {}).get('ask', 0) > 0)
        )
        log.info(f"Equity quotes: {n_priced}/{len(equity_syms)} priced")
        self._status(f"Equity quotes: {n_priced}/{len(equity_syms)} priced — finding ATM strikes…")

        # Find ATM strikes and collect option symbols
        option_syms_needed = set()
        for ticker, info in chain_infos.items():
            eq   = eq_quotes.get(ticker, {})
            last = eq.get('last', 0)
            bid, ask = eq.get('bid', 0), eq.get('ask', 0)
            price = last if last > 0 else ((bid + ask) / 2 if bid > 0 and ask > 0 else 0)
            if price <= 0:
                continue

            info['current_price'] = price
            front_atm = min(info['front_strikes'], key=lambda x: abs(float(x['strike-price']) - price))
            # Match back-leg by exact strike-price string — strikes are already
            # filtered to the front/back intersection in get_chain_info, so this
            # lookup is guaranteed to succeed.
            front_strike_key = front_atm['strike-price']
            back_atm = next(
                (s for s in info['back_strikes'] if s['strike-price'] == front_strike_key),
                None,
            )
            if back_atm is None:
                continue

            info['front_streamer_symbol'] = (front_atm.get('call-streamer-symbol')
                                             or front_atm.get('call', ''))
            info['back_streamer_symbol']  = (back_atm.get('call-streamer-symbol')
                                             or back_atm.get('call', ''))
            info['strike'] = float(front_atm['strike-price'])

            if info['front_streamer_symbol']: option_syms_needed.add(info['front_streamer_symbol'])
            if info['back_streamer_symbol']:  option_syms_needed.add(info['back_streamer_symbol'])

        if not option_syms_needed:
            parts = []
            for d in eq_diags:
                if isinstance(d, dict):
                    if d.get('error'):       parts.append(f"WS error: {d['error']}")
                    elif not d.get('connected'):   parts.append("WS did not connect")
                    elif not d.get('authorized'):  parts.append("WS connected but auth failed")
                    elif not d.get('channel_opened'): parts.append("channel did not open")
                    else:                          parts.append("WS ok but no prices")
            msg = '; '.join(parts) if parts else 'timeout with no data'
            self._status(f"No equity prices from DXLink ({msg}). See Debug Log.", done=True)
            return

        # Phase 2b: Option quotes
        self._status(f"Phase 3/4: Option quotes ({len(option_syms_needed)} contracts)…")
        self._indeterminate(True)
        log.info(f"Phase 2b: {len(option_syms_needed)} option symbols")

        opt_quotes, _ = fetch_quotes_with_retry(api, list(option_syms_needed), timeout=30)
        self._indeterminate(False)

        n_opt = sum(1 for s_ in option_syms_needed if opt_quotes.get(s_, {}).get('bid', 0) > 0)
        log.info(f"Option quotes: {n_opt}/{len(option_syms_needed)} priced")
        self._status(f"Option quotes: {n_opt}/{len(option_syms_needed)} priced — calculating metrics…")

        # Phase 3: Metrics
        self._status("Phase 4/4: Calculating calendar spread metrics…")
        self._progress(85)
        log.info("Phase 3: calculating metrics")

        results = []
        for ticker, info in chain_infos.items():
            if info.get('current_price', 0) < min_price:
                continue
            r = calculate_calendar_metrics(ticker, info, opt_quotes, iv_method)
            if r:
                results.append(r)

        log.info(f"Scan complete: {len(results)} setups found")
        self.root.after(0, self._finish_scan, results)

    # ── Status / progress helpers ─────────────────────────────────────────────

    def _status(self, msg, done=False):
        self.root.after(0, self._set_status, msg, done)

    def _set_status(self, msg, done):
        self.status_var.set(msg)
        if done:
            self.run_btn.config(state="normal")
            self._set_indeterminate(False)

    def _progress(self, value):
        self.root.after(0, lambda: self.progress.configure(value=value))

    def _indeterminate(self, active):
        self.root.after(0, self._set_indeterminate, active)

    def _set_indeterminate(self, active):
        if active:
            self.progress.configure(mode='indeterminate')
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.configure(mode='determinate')

    # ── Live streaming ────────────────────────────────────────────────────────

    def _start_live_streaming(self, item_ids):
        """Open a persistent DXLink subscription for the given Treeview rows.

        The DXLink handshake can take >10s in some sandbox conditions, so we run
        connect() in a background thread to keep the UI responsive."""
        if not item_ids or self._api is None:
            return

        sym_to_items = {}
        equity_set   = set()
        for iid in item_ids:
            data = self._row_data.get(iid)
            if not data:
                continue
            for sym in (data['ticker'], data['front_sym'], data['back_sym']):
                if sym:
                    sym_to_items.setdefault(sym, set()).add(iid)
            equity_set.add(data['ticker'])

        if not sym_to_items:
            return

        self._live_sym_to_items = sym_to_items
        n_rows = len(item_ids)
        self.root.after(0, lambda: self.status_var.set(
            f"{self.status_var.get()}  •  Live: connecting…"))
        threading.Thread(
            target=self._connect_live_in_bg,
            args=(sym_to_items, equity_set, n_rows),
            daemon=True,
        ).start()

    def _connect_live_in_bg(self, sym_to_items, equity_set, n_rows):
        try:
            client = DXLinkLiveClient(self._api)
            client.set_quote_callback(self._on_live_quote)
            client.connect(timeout=30)
            client.subscribe(list(sym_to_items.keys()), with_trade_for=equity_set)
        except Exception as exc:
            log.error(f"Could not start live stream: {exc}")
            self._live_client = None
            self.root.after(0, lambda: self.status_var.set(
                f"Scan complete — live stream failed: {exc}"))
            return
        self._live_client = client
        n_syms = len(sym_to_items)
        self.root.after(0, lambda: self.status_var.set(
            f"Scan complete — Live: {n_rows} rows / {n_syms} symbols"))

    def _stop_live_streaming(self):
        client = self._live_client
        self._live_client = None
        self._live_sym_to_items = {}
        with self._live_lock:
            self._live_dirty_items.clear()
            self._live_flush_pending = False
        self._clear_all_flashes()
        if client is not None:
            try:
                client.close()
            except Exception as exc:
                log.warning(f"Error closing live client: {exc}")

    def _on_live_quote(self, symbol, _quote):
        """Called from the WS thread on every quote update."""
        items = self._live_sym_to_items.get(symbol)
        if not items:
            return
        with self._live_lock:
            self._live_dirty_items.update(items)
            if self._live_flush_pending:
                return
            self._live_flush_pending = True
        self.root.after(self.LIVE_FLUSH_MS, self._flush_live_updates)

    def _flush_live_updates(self):
        """UI thread: recompute metrics and refresh dirty rows."""
        with self._live_lock:
            dirty = list(self._live_dirty_items)
            self._live_dirty_items.clear()
            self._live_flush_pending = False

        client = self._live_client
        if client is None or not dirty:
            return

        iv_method = self._live_iv_method
        for iid in dirty:
            data = self._row_data.get(iid)
            if not data:
                continue
            try:
                self._refresh_row_from_live(iid, data, client, iv_method)
            except Exception as exc:
                log.debug(f"live row refresh {data.get('ticker')}: {exc}")

        if self._auto_sort_active:
            self._sort_tree_by_fwd_factor()

    def _refresh_row_from_live(self, iid, data, client, iv_method):
        eq = client.get_quote(data['ticker'])
        last  = eq.get('last', 0)
        e_bid, e_ask = eq.get('bid', 0), eq.get('ask', 0)
        price = (last if last > 0
                 else ((e_bid + e_ask) / 2 if e_bid > 0 and e_ask > 0 else data['price']))

        fq = client.get_quote(data['front_sym'])
        bq = client.get_quote(data['back_sym'])
        f_bid = fq.get('bid', data['f_bid'])
        f_ask = fq.get('ask', data['f_ask'])
        b_bid = bq.get('bid', data['b_bid'])
        b_ask = bq.get('ask', data['b_ask'])
        if f_bid <= 0 or b_bid <= 0:
            return

        strike = data['strike']
        t1 = data['front_dte'] / 365.0
        t2 = data['back_dte']  / 365.0
        if iv_method == "Bid Front / Ask Back":
            f_iv = calc_implied_vol(f_bid,           price, strike, t1)
            b_iv = calc_implied_vol(b_ask,           price, strike, t2)
        else:
            f_iv = calc_implied_vol((f_bid + f_ask) / 2, price, strike, t1)
            b_iv = calc_implied_vol((b_bid + b_ask) / 2, price, strike, t2)
        if f_iv <= 0.01 or b_iv <= 0.01:
            return
        var_diff = t2 * b_iv ** 2 - t1 * f_iv ** 2
        if var_diff < 0:
            return
        fwd_iv = math.sqrt(var_diff / (t2 - t1))
        if fwd_iv <= 0:
            return
        fwd_factor = (f_iv - fwd_iv) / fwd_iv
        debit  = (b_bid + b_ask) / 2 - (f_bid + f_ask) / 2

        # Price-style cells get a brief green/red flash on change.
        self._set_with_flash(iid, "Price", f"${price:.2f}", price, data['price'])
        self._set_with_flash(iid, "F-Bid", f"${f_bid:.2f}", f_bid, data['f_bid'])
        self._set_with_flash(iid, "F-Ask", f"${f_ask:.2f}", f_ask, data['f_ask'])
        self._set_with_flash(iid, "B-Bid", f"${b_bid:.2f}", b_bid, data['b_bid'])
        self._set_with_flash(iid, "B-Ask", f"${b_ask:.2f}", b_ask, data['b_ask'])
        # Derived metrics — no flash.
        self.tree.set(iid, "Front IV",   f"{f_iv * 100:.2f}%")
        self.tree.set(iid, "Back IV",    f"{b_iv * 100:.2f}%")
        self.tree.set(iid, "Fwd IV",     f"{fwd_iv * 100:.2f}%")
        self.tree.set(iid, "Fwd Factor", f"{fwd_factor * 100:.2f}%")
        self.tree.set(iid, "Debit",      f"${debit:.2f}")
        self.tree.set(iid, "F-Spread",   f"${f_ask - f_bid:.2f}")
        self.tree.set(iid, "B-Spread",   f"${b_ask - b_bid:.2f}")

        data.update({
            'price':      price,
            'f_bid':      f_bid, 'f_ask': f_ask,
            'b_bid':      b_bid, 'b_ask': b_ask,
            'front_iv':   f_iv,  'back_iv': b_iv, 'fwd_iv': fwd_iv,
            'fwd_factor': fwd_factor,
        })

    # ── Live sort by Fwd Factor ───────────────────────────────────────────────

    def _toggle_fwd_factor_sort(self):
        self._auto_sort_active = not self._auto_sort_active
        if self._auto_sort_active:
            self.tree.heading("Fwd Factor", text="Fwd Factor ▼")
            self._sort_tree_by_fwd_factor()
        else:
            self.tree.heading("Fwd Factor", text="Fwd Factor")

    def _sort_tree_by_fwd_factor(self):
        """Reorder Treeview rows by fwd_factor descending. Clears flash overlays
        first since their absolute positions would no longer match their rows."""
        self._clear_all_flashes()
        decorated = []
        for iid in self.tree.get_children(''):
            data = self._row_data.get(iid)
            ff = data.get('fwd_factor', 0.0) if data else 0.0
            try:
                decorated.append((float(ff), iid))
            except (TypeError, ValueError):
                decorated.append((0.0, iid))
        decorated.sort(reverse=True)
        for idx, (_, iid) in enumerate(decorated):
            self.tree.move(iid, '', idx)

    # ── Cell flash overlays ───────────────────────────────────────────────────

    def _on_tree_yscroll(self, *args):
        self.tree.yview(*args)
        self._clear_all_flashes()

    def _clear_all_flashes(self):
        for w in list(self._flash_widgets.values()):
            try: w.destroy()
            except tk.TclError: pass
        self._flash_widgets.clear()

    def _set_with_flash(self, iid, column, text, new_val, old_val):
        """Set a Treeview cell's text and, if the value actually changed, briefly
        overlay a green (up) or red (down) Label on the cell."""
        self.tree.set(iid, column, text)
        # Auto-sort moves rows after the update, so flash overlays would end up
        # over the wrong cell — skip them in that mode.
        if self._auto_sort_active:
            return
        try:
            new_f = float(new_val)
            old_f = float(old_val)
        except (TypeError, ValueError):
            return
        if old_f <= 0 or abs(new_f - old_f) < 1e-9:
            return
        self._flash_cell(iid, column, text, 'up' if new_f > old_f else 'down')

    def _flash_cell(self, iid, column, text, direction):
        if not self.tree.exists(iid):
            return
        try:
            bbox = self.tree.bbox(iid, column=column)
        except tk.TclError:
            return
        if not bbox:
            return  # row scrolled out of view
        x, y, w, h = bbox
        bg = '#5cb85c' if direction == 'up' else '#d9534f'

        key = (iid, column)
        old = self._flash_widgets.pop(key, None)
        if old is not None:
            try: old.destroy()
            except tk.TclError: pass

        flash = tk.Label(
            self.tree.master, text=text,
            background=bg, foreground='white',
            borderwidth=0, anchor='center',
            font=('TkDefaultFont', 9, 'bold'),
        )
        flash.place(in_=self.tree, x=x, y=y, width=w, height=h)
        self._flash_widgets[key] = flash

        def cleanup(k=key, w=flash):
            if self._flash_widgets.get(k) is w:
                self._flash_widgets.pop(k, None)
            try: w.destroy()
            except tk.TclError: pass
        self.root.after(700, cleanup)

    # ── Window close ──────────────────────────────────────────────────────────

    def _on_close(self):
        self._stop_live_streaming()
        self.root.destroy()

    # ── Finish scan ───────────────────────────────────────────────────────────

    def _finish_scan(self, results):
        self._set_indeterminate(False)
        self.progress.configure(value=100)
        self.run_btn.config(state="normal")

        if not results:
            self.status_var.set("Scan complete — no setups found.")
            messagebox.showinfo(
                "No Results",
                "No valid calendar setups found.\n\n"
                "Check the Debug Log tab for details.",
            )
            return

        df = pd.DataFrame(results).sort_values("Fwd Factor", ascending=False)
        self.status_var.set(f"Scan complete — {len(df)} setup(s) found.")

        for _, row in df.iterrows():
            item_id = self.tree.insert("", tk.END, values=(
                row['Ticker'],
                f"${row['Price']:.2f}",
                fmt_market_cap(row['Mkt Cap']),
                f"${row['Strike']:.2f}",
                row['F-DTE'],
                row['B-DTE'],
                f"${row['F-Bid']:.2f}",
                f"${row['F-Ask']:.2f}",
                f"${row['B-Bid']:.2f}",
                f"${row['B-Ask']:.2f}",
                f"{row['Front IV'] * 100:.2f}%",
                f"{row['Back IV'] * 100:.2f}%",
                f"{row['Fwd IV'] * 100:.2f}%",
                f"{row['Fwd Factor'] * 100:.2f}%",
                f"${row['Debit']:.2f}",
                f"${row['F-Spread']:.2f}",
                f"${row['B-Spread']:.2f}",
                row['Earnings'],
            ))
            self._row_data[item_id] = {
                'ticker':     row['Ticker'],
                'price':      row['Price'],
                'strike':     row['Strike'],
                'front_dte':  row['F-DTE'],
                'back_dte':   row['B-DTE'],
                'f_bid':      row['F-Bid'],
                'f_ask':      row['F-Ask'],
                'b_bid':      row['B-Bid'],
                'b_ask':      row['B-Ask'],
                'front_iv':   row['Front IV'],
                'back_iv':    row['Back IV'],
                'fwd_iv':     row['Fwd IV'],
                'fwd_factor': row['Fwd Factor'],
                'front_sym':  row['_front_sym'],
                'back_sym':   row['_back_sym'],
            }

        # Kick off live streaming for the top-N rows by Fwd Factor.
        self._live_iv_method = self.settings.get('iv_method', 'Midpoint')
        self._start_live_streaming(list(self.tree.get_children())[: self.LIVE_TOP_N])


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    root = tk.Tk()
    CalendarScannerApp(root)
    root.mainloop()
