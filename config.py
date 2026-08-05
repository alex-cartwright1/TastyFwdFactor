"""Persistent user state.

Everything lives in ``~/.config/calendar-spread/``: ``settings.json`` (scan
params/filters), ``ticker_info.json`` (Tastytrade market-metrics cache),
``chain_cache.bin`` (option chains — binary, see :func:`save_chain_cache`),
``positions.json``, and — only when no OS keyring backend is available — a
mode-600 ``credentials.json`` fallback.

When adding a setting, extend ``DEFAULT_SETTINGS``: ``load_settings()`` merges
the defaults over the saved file, so older files stay loadable.
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path

from applog import log

try:
    import keyring
    from keyring.backends.fail import Keyring as _FailKeyring
    KEYRING_AVAILABLE = not isinstance(keyring.get_keyring(), _FailKeyring)
    del _FailKeyring
except Exception:
    KEYRING_AVAILABLE = False


CONFIG_DIR = Path.home() / ".config" / "calendar-spread"

_KEYRING_SERVICE   = "calendar-spread-tastytrade"
_LEGACY_CREDS_PATH = CONFIG_DIR / "credentials.json"
_SETTINGS_PATH     = CONFIG_DIR / "settings.json"
_POSITIONS_PATH    = CONFIG_DIR / "positions.json"
_TICKER_CACHE_PATH = CONFIG_DIR / "ticker_info.json"


# ─── Credentials (OS keyring with legacy JSON fallback) ──────────────────────

def load_credentials():
    """Return (client_secret, refresh_token). Prefer OS keyring; fall back to
    the legacy JSON file (which is migrated away next time the user saves)."""
    if KEYRING_AVAILABLE:
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
    if KEYRING_AVAILABLE:
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


def clear_credentials():
    """Forget stored credentials — used when the user unticks 'Remember me' or
    signs out. Clears both the keyring entries and the JSON fallback, since
    either may hold a value from an earlier run."""
    if KEYRING_AVAILABLE:
        for key in ("client_secret", "refresh_token"):
            try:
                keyring.delete_password(_KEYRING_SERVICE, key)
            except Exception:
                pass      # not set is the normal case, not an error
    try:
        if _LEGACY_CREDS_PATH.exists():
            _LEGACY_CREDS_PATH.unlink()
    except Exception as exc:
        log.warning(f"Could not remove credentials file: {exc}")
    log.info("Stored credentials cleared")


# ─── Settings ────────────────────────────────────────────────────────────────

DEFAULT_SETTINGS = {
    'csv_path':                str(Path(__file__).parent / "full.csv"),
    'front_dte':               21,
    'back_dte':                45,
    'front_dte_flex':          0,
    'back_dte_flex':           0,
    'iv_method':               'Midpoint',
    'min_price':               10.0,
    'min_market_cap_b':        '',
    'filter_front_earnings':   True,
    'filter_back_earnings':    True,
    'filter_front_dividend':   False,
    'filter_back_dividend':    False,
    'filter_unknown_earnings': False,
    'ticker_info_ttl_days':    7,
    # Option chains are cached on disk (see api.ChainCache) and survive a
    # restart, so the TTL is in days like the fundamentals cache above rather
    # than the minutes it used when the cache died with the process.
    'chain_cache_ttl_days':    7,
    # Phase 2 fan-out. The chain fetch is pure network wait, so the useful worker
    # count is set by the API's rate limiter rather than by the CPU. Clamped to
    # CHAIN_WORKERS_MAX in scanner.py; api.HTTP_POOL_SIZE must stay >= that, or
    # the extra workers just queue on the connection pool.
    'chain_fetch_workers':     35,
}

IV_METHODS = ("Midpoint", "Bid Front / Ask Back", "Provided Data")


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


# ─── Positions ───────────────────────────────────────────────────────────────

def load_positions_raw():
    try:
        if _POSITIONS_PATH.exists():
            data = json.loads(_POSITIONS_PATH.read_text())
            if isinstance(data, list):
                return data
    except Exception as exc:
        log.warning(f"Could not read positions file: {exc}")
    return []


def save_positions_raw(records):
    try:
        _POSITIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _POSITIONS_PATH.write_text(json.dumps(records, indent=2))
    except Exception as exc:
        log.warning(f"Could not save positions file: {exc}")


# ─── Ticker info cache (earnings / market cap / ex-div) ──────────────────────

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


# ─── Option chain cache (binary, see api.ChainCache) ─────────────────────────

# Not JSON. `api.ChainCache` already holds each chain as a zlib blob, so the file
# stores those bytes verbatim: no decompress-on-save, no recompress-on-load, and
# no base64 inflating a 6 MB cache by a third. Layout is a magic line, a decimal
# header length, a JSON index, then the blobs back to back in index order.
_CHAIN_CACHE_PATH  = CONFIG_DIR / "chain_cache.bin"
_CHAIN_CACHE_MAGIC = b"TFFCHAIN1\n"


def load_chain_cache():
    """Return ``{symbol: {'blob': bytes, 'fetched_at': float}}``, or {} on any
    problem — a cache that won't load is a slow scan, never a failed one."""
    try:
        if not _CHAIN_CACHE_PATH.exists():
            return {}
        with open(_CHAIN_CACHE_PATH, 'rb') as fh:
            if fh.read(len(_CHAIN_CACHE_MAGIC)) != _CHAIN_CACHE_MAGIC:
                log.warning("Chain cache file has an unrecognised format — ignoring")
                return {}
            index = json.loads(fh.read(int(fh.readline())))
            out = {}
            for rec in index:
                blob = fh.read(rec['n'])
                if len(blob) != rec['n']:
                    # Truncated (killed mid-write on an older build). Keep what
                    # read cleanly rather than discarding the whole file.
                    log.warning(f"Chain cache truncated after {len(out)} entries")
                    break
                out[rec['s']] = {'blob': blob, 'fetched_at': rec['t']}
        return out
    except Exception as exc:
        log.warning(f"Could not read chain cache: {exc}")
        return {}


def save_chain_cache(entries):
    """Write ``{symbol: {'blob': bytes, 'fetched_at': float}}`` atomically."""
    try:
        _CHAIN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        items  = list(entries.items())          # one snapshot: the index and the
        index  = [{'s': sym, 't': e['fetched_at'], 'n': len(e['blob'])}
                  for sym, e in items]          # blobs must stay in step
        header = json.dumps(index).encode('utf-8')
        tmp    = _CHAIN_CACHE_PATH.with_suffix('.tmp')
        try:
            with open(tmp, 'wb') as fh:
                fh.write(_CHAIN_CACHE_MAGIC)
                fh.write(f"{len(header)}\n".encode('utf-8'))
                fh.write(header)
                for _, e in items:
                    fh.write(e['blob'])
            # Atomic: a crash mid-write leaves the previous cache intact rather
            # than a half-file the next launch has to detect and discard.
            os.replace(tmp, _CHAIN_CACHE_PATH)
        except Exception:
            # Don't leave a partial .tmp behind for the next save to trip over.
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        total = sum(len(e['blob']) for _, e in items)
        log.info(f"Chain cache saved: {len(items)} chains, "
                 f"{total / 1e6:.1f} MB → {_CHAIN_CACHE_PATH}")
    except Exception as exc:
        log.warning(f"Could not save chain cache: {exc}")


def clear_chain_cache():
    try:
        if _CHAIN_CACHE_PATH.exists():
            _CHAIN_CACHE_PATH.unlink()
            log.info(f"Cleared chain cache: {_CHAIN_CACHE_PATH}")
    except Exception as exc:
        log.warning(f"Could not clear chain cache: {exc}")


def parse_iso_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


def now_ts():
    return time.time()
