"""The scan pipeline: watchlist → ticker info → chains → quotes → metrics.

Pure logic — no Qt widgets, no global state. Progress is reported through a
`reporter` duck type supplying `status(msg)`, `progress(pct)`,
`indeterminate(on)` and `cancelled()`, which the worker implements as signal
emissions.

The phase ordering is a cost optimisation and must not be reordered:

1. Ticker info (Tastytrade `/market-metrics`, chunked, disk-cached with TTL) →
   pre-filter on market cap / earnings / ex-div, so excluded tickers never
   trigger a chain request. The pre-filter uses the *target* DTEs minus a 7-day
   safety buffer because the actual expiry is not yet known.
2. Option chains for survivors only (served from `api.ChainCache` when warm),
   then a post-filter that re-checks earnings/dividends against the *actual*
   front/back expiry dates.
3. Equity quotes via DXLink → pick the ATM strike → option quotes via DXLink.
4. Metrics.

Phase 3 subscribes on the app's **persistent** `api.QuoteStream` rather than
opening its own sockets: it adds symbols to the live connection as it identifies
them and blocks on the shared `QuoteStore` until they price. Nothing is torn down
at the end, so the results table is already streaming the moment it is painted.
`fetch_quotes_with_retry` remains as the fallback for when no stream is available.

`run_scan(..., only_tickers=[...])` runs the same pipeline over an explicit
symbol list with every user filter disabled — that is the single-ticker search.
"""

import os
import time
from datetime import datetime, timedelta

from api import (
    QUOTE_COVERAGE, QUOTE_QUIET_SECS, describe_diags, fetch_quotes_with_retry,
)
from applog import log
from config import load_ticker_cache, parse_iso_date, save_ticker_cache
from data_models import ChainInfo, Quote, ScanResult, TickerInfo
from pricing import solve_calendar
from qtpool import parallel_map

FALLBACK_TICKERS = ['SPY', 'QQQ', 'AAPL', 'TSLA', 'NVDA', 'IWM', 'AMD']

# Phase 2 fan-out ceiling. Chain fetches are pure network wait, so the useful
# worker count is bounded by the API's rate limiter, not by cores — but it must
# not exceed `api.HTTP_POOL_SIZE`, or the surplus workers queue on the shared
# `requests` connection pool and buy nothing.
CHAIN_WORKERS_DEFAULT = 35
CHAIN_WORKERS_MAX     = 50

# The consumer name Phase 3 registers on the shared stream. `MainWindow` releases
# it once the results table has declared its own set, so nothing the scan
# subscribed outlives the scan.
SCAN_CONSUMER = "scan"


class ScanAborted(Exception):
    """Raised to unwind the pipeline when the user cancels or a phase yields
    nothing usable. The message is shown as the final status."""


class _NullReporter:
    def status(self, msg):        log.info(msg)
    def progress(self, pct):      pass
    def indeterminate(self, on):  pass
    def cancelled(self):          return False


# ─── Watchlist ───────────────────────────────────────────────────────────────

def load_watchlist(path):
    """Lenient CSV parse: first comma-separated field of each non-empty line,
    uppercased. A header row is optional — any line starting with 'ticker' is
    skipped."""
    if not os.path.exists(path):
        log.warning(f"CSV not found: {path}")
        return list(FALLBACK_TICKERS), False
    with open(path, 'r') as fh:
        tickers = [
            line.strip().split(',')[0].upper()
            for line in fh
            if line.strip() and not line.lower().startswith('ticker')
        ]
    log.info(f"Loaded {len(tickers)} tickers from {path}")
    return tickers, True


# ─── Tastytrade market metrics: earnings / market cap / ex-dividend ──────────

def fetch_ticker_info(sdk, symbols, progress_cb=None, ttl_days=7,
                      force_refresh=False, should_cancel=None,
                      on_refreshed=None):
    """Chunked Tastytrade `/market-metrics` fetch backed by an on-disk cache.

    Cached entries younger than `ttl_days` are reused without a request. Entries
    whose cached earnings/ex-div date has already passed are re-fetched, since
    the endpoint will have rolled forward to the next event.

    `sdk` is an `api.MarketDataSession`; the fetch blocks, so this must run on a
    worker thread. Symbols the endpoint doesn't recognise come back absent and
    are cached as an empty `TickerInfo`, so a delisted ticker is not re-requested
    on every scan.

    `on_refreshed(symbols)` fires with the symbols that actually went to the
    network. `run_scan` hooks the option-chain cache to it: a ticker whose
    fundamentals just moved is exactly the one whose expirations may have moved
    too, so its cached chain must not survive.

    Returns ``{symbol: TickerInfo}``.
    """
    cache    = {} if force_refresh else load_ticker_cache()
    today    = datetime.today().date()
    fetched_at = time.time()
    ttl_secs = max(ttl_days, 0) * 86400

    out, to_fetch = {}, []
    for sym in symbols:
        entry = cache.get(sym)
        if entry and ttl_secs > 0 and (fetched_at - entry.get('fetched_at', 0)) < ttl_secs:
            ed = parse_iso_date(entry.get('earnings'))
            xd = parse_iso_date(entry.get('ex_div'))
            if (ed and ed < today) or (xd and xd < today):
                to_fetch.append(sym)
                continue
            out[sym] = TickerInfo(ed, entry.get('market_cap'), xd)
        else:
            to_fetch.append(sym)

    n_cached = len(symbols) - len(to_fetch)
    if to_fetch:
        log.info(f"Ticker info: {n_cached} from cache, {len(to_fetch)} to fetch")
    else:
        # Nothing went to the network, so nothing in `cache` changed. Rewriting
        # ticker_info.json here would serialise thousands of untouched entries on
        # every warm scan for no effect at all.
        log.info(f"Ticker info: all {n_cached} from cache — no disk write needed")
        if progress_cb:
            progress_cb(len(symbols), len(symbols))
        return out

    def _report(done, _total):
        if progress_cb:
            progress_cb(n_cached + done, len(symbols))

    fetched = sdk.fetch_ticker_info(to_fetch, progress_cb=_report,
                                    should_cancel=should_cancel)
    # A cancelled sweep leaves most symbols unrequested rather than unknown, so
    # don't record blanks for them — that would suppress the real fetch for a
    # full TTL the next time round.
    cancelled = bool(should_cancel and should_cancel())
    if on_refreshed and not cancelled:
        on_refreshed(to_fetch)
    n_unknown = n_written = 0
    for sym in to_fetch:
        info = fetched.get(sym)
        if info is None:
            if cancelled:
                continue
            # Not an error: /market-metrics simply has no row for delisted or
            # non-equity symbols. Cache the blank so the next scan skips it.
            n_unknown += 1
            info = TickerInfo()
        out[sym] = info
        cache[sym] = {
            'earnings':   info.earnings.isoformat() if info.earnings else None,
            'market_cap': info.market_cap,
            'ex_div':     info.ex_div.isoformat() if info.ex_div else None,
            'fetched_at': fetched_at,
        }
        n_written += 1

    if n_unknown:
        log.info(f"Ticker info: {n_unknown}/{len(to_fetch)} symbols unknown to "
                 f"/market-metrics")
    # Only write when the sweep actually produced entries. A cancelled sweep can
    # reach here having recorded none, and rewriting the whole file to change
    # nothing is the most expensive no-op in the pipeline.
    if n_written:
        save_ticker_cache(cache)
    else:
        log.info("Ticker info: nothing new recorded — cache left untouched")
    return out


# ─── Option chain structure ──────────────────────────────────────────────────

def get_chain_info(symbol, api, target_front_dte, target_back_dte,
                   front_dte_flex=0, back_dte_flex=0, cache_ttl=0) -> ChainInfo:
    """Pick the front/back expirations for one symbol.

    Always returns a ChainInfo; a rejection carries `skip_reason` rather than
    raising, so the scan can aggregate rejection counts for the log.

    `front_dte_flex` / `back_dte_flex`: tolerance window (days) around the
    target DTE. With flex>0 only expirations whose DTE falls within
    [target-flex, target+flex] are eligible; the nearest match in that window is
    picked. With flex==0 the "nearest match across all expirations" behaviour is
    used.
    """
    today = datetime.today()
    try:
        chain_data = api.get_option_chain(symbol, cache_ttl=cache_ttl)
        if not chain_data:
            return ChainInfo(symbol, skip_reason='no_chain_data')

        expirations = chain_data[0].get('expirations', [])
        if len(expirations) < 2:
            return ChainInfo(symbol, skip_reason=f'too_few_expirations:{len(expirations)}')

        exp_dates = [datetime.strptime(e['expiration-date'], '%Y-%m-%d') for e in expirations]
        dtes      = [(d - today).days for d in exp_dates]

        def pick(target, flex):
            """Index of the best expiration, or None if flex excludes them all."""
            if flex > 0:
                cands = [i for i, d in enumerate(dtes)
                         if d > 0 and abs(d - target) <= flex]
                if not cands:
                    return None
                return min(cands, key=lambda i: abs(dtes[i] - target))
            return min(range(len(dtes)), key=lambda i: abs(dtes[i] - target))

        front_idx = pick(target_front_dte, front_dte_flex)
        if front_idx is None:
            return ChainInfo(
                symbol,
                skip_reason=f'no_front_in_flex:{target_front_dte}±{front_dte_flex}')
        back_idx = pick(target_back_dte, back_dte_flex)
        if back_idx is None:
            return ChainInfo(
                symbol,
                skip_reason=f'no_back_in_flex:{target_back_dte}±{back_dte_flex}')

        if dtes[front_idx] <= 0:
            return ChainInfo(symbol, skip_reason=f'all_expired:max_dte={max(dtes)}')
        if back_idx <= front_idx:
            return ChainInfo(
                symbol,
                skip_reason=f'back_not_after_front:f={dtes[front_idx]},b={dtes[back_idx]}')

        # Restrict strikes to those present in BOTH expirations so any later ATM
        # pick is guaranteed to have a matching back-leg contract.
        front_raw = expirations[front_idx]['strikes']
        back_raw  = expirations[back_idx]['strikes']
        common = ({s.get('strike-price') for s in front_raw}
                  & {s.get('strike-price') for s in back_raw})
        if not common:
            return ChainInfo(symbol, skip_reason='no_common_strikes')

        return ChainInfo(
            ticker         = symbol,
            front_dte      = dtes[front_idx],
            back_dte       = dtes[back_idx],
            front_exp_date = exp_dates[front_idx].date(),
            back_exp_date  = exp_dates[back_idx].date(),
            front_strikes  = [s for s in front_raw if s.get('strike-price') in common],
            back_strikes   = [s for s in back_raw  if s.get('strike-price') in common],
        )
    except Exception as exc:
        return ChainInfo(symbol, skip_reason=f'exception:{exc}')


def _streamer_symbol(strike_entry):
    return strike_entry.get('call-streamer-symbol') or strike_entry.get('call', '')


def resolve_position_legs(api, ticker, strike, front_expiry, back_expiry,
                          cache_ttl=0):
    """Look up call-streamer-symbols + DTEs for a manually entered position.

    Raises ValueError with a human-readable message if the chain doesn't contain
    a matching expiration or strike.
    """
    chain_data = api.get_option_chain(ticker, cache_ttl=cache_ttl)
    if not chain_data:
        raise ValueError(f"No option chain returned for {ticker}")

    by_date = {e.get('expiration-date'): e
               for e in chain_data[0].get('expirations', [])}
    for label, exp in (('front', front_expiry), ('back', back_expiry)):
        if exp not in by_date:
            raise ValueError(f"{ticker}: no {label} expiration {exp} in chain")

    today  = datetime.today().date()
    f_date = datetime.strptime(front_expiry, '%Y-%m-%d').date()
    b_date = datetime.strptime(back_expiry,  '%Y-%m-%d').date()

    def find_strike(exp_entry, label):
        for s in exp_entry.get('strikes', []):
            try:
                if abs(float(s.get('strike-price', 0)) - float(strike)) < 1e-6:
                    return s
            except (TypeError, ValueError):
                continue
        raise ValueError(
            f"{ticker}: no {label} strike {strike} on {exp_entry.get('expiration-date')}")

    front_sym = _streamer_symbol(find_strike(by_date[front_expiry], 'front'))
    back_sym  = _streamer_symbol(find_strike(by_date[back_expiry],  'back'))
    if not front_sym or not back_sym:
        raise ValueError(f"{ticker}: missing call-streamer-symbol on chain entry")

    return {
        'front_streamer_symbol': front_sym,
        'back_streamer_symbol':  back_sym,
        'front_dte': max((f_date - today).days, 0),
        'back_dte':  max((b_date - today).days, 0),
    }


# ─── Metrics ─────────────────────────────────────────────────────────────────

def calculate_calendar_metrics(chain: ChainInfo, option_quotes, iv_method):
    """Build a ScanResult from a ChainInfo plus a DXLink quote snapshot, or None
    if the quotes can't support the model."""
    if not chain.front_sym or not chain.back_sym or chain.strike <= 0:
        return None

    fq = option_quotes.get(chain.front_sym, Quote())
    bq = option_quotes.get(chain.back_sym,  Quote())

    ivs = solve_calendar(chain.current_price, chain.strike,
                         chain.front_dte, chain.back_dte,
                         fq.bid, fq.ask, bq.bid, bq.ask, iv_method)
    if ivs is None:
        return None

    return ScanResult(
        ticker     = chain.ticker,
        price      = chain.current_price,
        market_cap = chain.market_cap,
        strike     = chain.strike,
        front_dte  = chain.front_dte,
        back_dte   = chain.back_dte,
        f_bid      = fq.bid, f_ask = fq.ask,
        b_bid      = bq.bid, b_ask = bq.ask,
        front_iv   = ivs.front_iv,
        back_iv    = ivs.back_iv,
        fwd_iv     = ivs.fwd_iv,
        fwd_factor = ivs.fwd_factor,
        debit      = ivs.debit,
        earnings   = chain.earnings_date.strftime('%Y-%m-%d') if chain.earnings_date else 'N/A',
        front_sym  = chain.front_sym,
        back_sym   = chain.back_sym,
    )


# ─── Pipeline ────────────────────────────────────────────────────────────────

def _stream_quotes(stream, api, subscribe, trade_syms, wait_for, predicate,
                   timeout, label):
    """Price `wait_for` on the persistent stream, falling back to a one-shot fetch.

    `subscribe` is the scan's **cumulative** symbol set — Phase 3b passes 3a's
    equities along with the option legs, so the underlying prices keep ticking
    while the legs are being priced, and the results table inherits both.

    Blocks on the shared store until `QuoteStore.wait_for`'s coverage or
    quiet-period condition is met. Nothing is closed afterwards.

    Returns ``(quotes, diags)``. `diags` is empty on the streaming path; it only
    carries content when the fallback ran, which is what `describe_diags` reads.
    """
    if stream is not None:
        stream.ensure_started()
    if stream is None or not stream.wait_ready():
        log.warning(f"{label}: persistent quote stream unavailable — falling "
                    f"back to a one-shot DXLink fetch")
        return fetch_quotes_with_retry(api, list(wait_for), timeout=timeout)

    stream.set_consumer(SCAN_CONSUMER, subscribe, trade_syms)

    wanted  = list(wait_for)
    started = time.monotonic()
    priced  = stream.store.wait_for(wanted, predicate, timeout)
    log.info(f"{label}: {priced}/{len(wanted)} priced in "
             f"{time.monotonic() - started:.1f}s "
             f"(coverage target {QUOTE_COVERAGE:.0%}, "
             f"quiet period {QUOTE_QUIET_SECS}s, timeout {timeout}s)")
    return stream.store.snapshot(wanted), []


def run_scan(api, sdk, settings, reporter=None, only_tickers=None, stream=None):
    """Execute the full scan. Returns a list of ScanResult sorted by fwd factor.

    `stream` is the app's `api.QuoteStream`. Phase 3 subscribes on it instead of
    opening its own sockets and leaves the subscription in place, so the results
    table inherits a feed that is already warm. Passing None (or a stream that
    never becomes ready) falls back to the one-shot batched fetch.

    `only_tickers` runs the same pipeline over an explicit symbol list instead of
    the watchlist CSV, which is how the single-ticker search works. In that mode
    **every user filter is skipped** — market cap, earnings, dividends and min
    price, pre- and post-chain. The user named this symbol; returning "no setups"
    because it reports earnings next week would hide the very thing they asked to
    see, and the earnings date is a column on the row anyway.

    Raises ScanAborted with a user-facing message when a phase leaves nothing to
    work with.
    """
    rep = reporter or _NullReporter()
    targeted = bool(only_tickers)

    def check_cancel():
        if rep.cancelled():
            raise ScanAborted("Scan cancelled.")

    f_dte  = int(settings['front_dte'])
    b_dte  = int(settings['back_dte'])
    f_flex = int(settings.get('front_dte_flex', 0) or 0)
    b_flex = int(settings.get('back_dte_flex', 0) or 0)
    iv_method = settings['iv_method']
    min_price = float(settings['min_price'])
    ttl_days  = int(settings.get('ticker_info_ttl_days', 7))
    chain_ttl = max(int(settings.get('chain_cache_ttl_days', 7)), 0) * 86400
    try:
        chain_workers = int(settings.get('chain_fetch_workers',
                                         CHAIN_WORKERS_DEFAULT))
    except (TypeError, ValueError):
        chain_workers = CHAIN_WORKERS_DEFAULT
    chain_workers = max(1, min(chain_workers, CHAIN_WORKERS_MAX))

    filter_f_earn  = settings['filter_front_earnings']
    filter_b_earn  = settings['filter_back_earnings']
    filter_f_div   = settings['filter_front_dividend']
    filter_b_div   = settings['filter_back_dividend']
    filter_no_earn = settings.get('filter_unknown_earnings', False)

    cap_str = settings['min_market_cap_b']
    cap_str = cap_str.strip() if isinstance(cap_str, str) else ''
    try:
        min_cap = float(cap_str) * 1e9 if cap_str else 0.0
    except ValueError:
        min_cap = 0.0

    if targeted:
        tickers = list(dict.fromkeys(t.strip().upper()
                                     for t in only_tickers if t and t.strip()))
        if not tickers:
            raise ScanAborted("No ticker to search for.")
        log.info(f"Targeted scan: {', '.join(tickers)}")
    else:
        tickers, found = load_watchlist(settings['csv_path'])
        if not found:
            rep.status(f"'{settings['csv_path']}' not found — using fallback list.")
    total = len(tickers)

    # ── Phase 1: ticker info + pre-filter ────────────────────────────────────
    rep.status(f"Phase 1/4: Ticker info ({total} tickers, cache TTL={ttl_days}d)…")
    rep.progress(0)
    log.info(f"Phase 1: ticker info fetch ({total} tickers, ttl={ttl_days}d)")

    def _ep(done, tot):
        rep.progress(int(done / tot * 20))
        rep.status(f"Phase 1/4: Ticker info {done}/{tot}…")

    def _invalidate_chains(symbols):
        dropped = api.chains.invalidate(symbols)
        if dropped:
            log.info(f"Chain cache: dropped {dropped} entries whose fundamentals "
                     f"were just refreshed")

    try:
        info_map = fetch_ticker_info(sdk, tickers, progress_cb=_ep,
                                     ttl_days=ttl_days, should_cancel=rep.cancelled,
                                     on_refreshed=_invalidate_chains)
    except Exception as exc:
        # The filters key off this data, so continuing without it would silently
        # scan tickers the user asked to exclude.
        log.error(f"Ticker info fetch failed: {exc}")
        raise ScanAborted(f"Could not fetch ticker info from Tastytrade: {exc}")
    check_cancel()

    # Conservative pre-filter cutoffs: the actual front/back expirations may
    # differ from the user's targets by a few days, so use a 7-day safety buffer
    # and drop only tickers whose earnings/ex-div falls well before the earliest
    # possible expiry. Boundary cases are caught again post-chain.
    today  = datetime.today().date()
    safety = timedelta(days=7)
    front_pre_cut = today + timedelta(days=f_dte) - safety
    back_pre_cut  = today + timedelta(days=b_dte) - safety

    survivors = []
    dropped_cap = dropped_earn = dropped_div = dropped_no_earn = 0
    for t in (() if targeted else tickers):
        info = info_map.get(t) or TickerInfo()
        if min_cap > 0 and (info.market_cap is None or info.market_cap < min_cap):
            dropped_cap += 1
            continue
        if filter_no_earn and info.earnings is None:
            dropped_no_earn += 1
            continue
        if info.earnings and ((filter_f_earn and info.earnings <= front_pre_cut) or
                              (filter_b_earn and info.earnings <= back_pre_cut)):
            dropped_earn += 1
            continue
        if info.ex_div and ((filter_f_div and info.ex_div <= front_pre_cut) or
                            (filter_b_div and info.ex_div <= back_pre_cut)):
            dropped_div += 1
            continue
        survivors.append(t)

    if targeted:
        survivors = list(tickers)

    n_dropped = dropped_cap + dropped_earn + dropped_div + dropped_no_earn
    if n_dropped:
        log.info(f"Pre-filter dropped: market_cap={dropped_cap} earnings={dropped_earn} "
                 f"dividend={dropped_div} no_earnings_date={dropped_no_earn}; "
                 f"{len(survivors)}/{total} remain")
        rep.status(f"Pre-filter dropped {n_dropped}; "
                   f"{len(survivors)} remain — fetching chains…")

    if not survivors:
        raise ScanAborted("All tickers excluded by pre-filter.")

    # ── Phase 2: option chains (survivors only) + post-filter ────────────────
    rep.status(f"Phase 2/4: Fetching option chains ({len(survivors)} tickers)…")
    rep.progress(20)
    log.info(f"Phase 2: chain fetch for {len(survivors)} survivors  "
             f"front_dte={f_dte}±{f_flex}  back_dte={b_dte}±{b_flex}  "
             f"workers={chain_workers}")

    def _fetch_chain(sym):
        return get_chain_info(sym, api, f_dte, b_dte, f_flex, b_flex,
                              cache_ttl=chain_ttl)

    def _cp(done, tot):
        if done % 10 == 0 or done == tot:
            rep.progress(20 + int(done / tot * 30))

    chains, skip_reasons = {}, {}
    for sym, chain in parallel_map(_fetch_chain, survivors, max_workers=chain_workers,
                                   progress_cb=_cp, should_cancel=rep.cancelled):
        if isinstance(chain, Exception):
            chain = ChainInfo(sym, skip_reason=f'exception:{chain}')
        if chain.ok:
            chains[chain.ticker] = chain
        else:
            key = chain.skip_reason.split(':')[0]
            skip_reasons[key] = skip_reasons.get(key, 0) + 1

    check_cancel()
    log.info(f"Phase 2 done: {len(chains)} valid, skips={skip_reasons}")
    cache_stats = api.chains.stats()
    log.info(f"Chain cache: {cache_stats}")
    rate_stats = getattr(api, 'rate', None)
    if rate_stats is not None:
        log.info(f"Chain pacing: {rate_stats.stats()}")
    if cache_stats.get('evictions'):
        # The cap is the only reason a warm rescan would refetch, so say so
        # rather than leaving the user to infer it from the phase timing.
        log.warning(
            f"Chain cache evicted {cache_stats['evictions']} entries — the "
            f"{cache_stats['max']}-entry cap is smaller than this sweep "
            f"({len(survivors)} survivors), so the next scan will refetch the "
            f"difference. Narrow the pre-filter or raise api.CHAIN_CACHE_MAX.")
    if not chains:
        reasons = ', '.join(f'{k}:{v}' for k, v in sorted(skip_reasons.items()))
        if targeted:
            raise ScanAborted(
                f"{', '.join(tickers)}: no usable option chain for "
                f"{f_dte}/{b_dte} DTE ({reasons or 'unknown'}).")
        raise ScanAborted(f"No valid option chains found. Reasons: {reasons or 'unknown'}")

    for ticker, chain in chains.items():
        info = info_map.get(ticker) or TickerInfo()
        chain.earnings_date = info.earnings
        chain.market_cap    = info.market_cap
        chain.ex_div_date   = info.ex_div

    # Post-filter against the ACTUAL expirations — the pre-filter used target
    # DTEs plus a safety buffer, so edge cases can slip through to here.
    def post_filter(pred, label):
        removed = [t for t, c in chains.items() if pred(c)]
        for t in removed:
            del chains[t]
        if removed:
            log.info(f"{label} post-filter removed {len(removed)}; {len(chains)} remain")

    if not targeted and (filter_f_earn or filter_b_earn):
        post_filter(lambda c: c.earnings_date and (
            (filter_f_earn and c.earnings_date <= c.front_exp_date) or
            (filter_b_earn and c.earnings_date <= c.back_exp_date)), "Earnings")
    if not targeted and (filter_f_div or filter_b_div):
        post_filter(lambda c: c.ex_div_date and (
            (filter_f_div and c.ex_div_date <= c.front_exp_date) or
            (filter_b_div and c.ex_div_date <= c.back_exp_date)), "Dividend")

    if not chains:
        raise ScanAborted("All tickers filtered out.")

    # ── Phase 3a: equity quotes → ATM strikes ────────────────────────────────
    equity_syms = list(chains.keys())
    rep.status(f"Phase 3/4: Equity quotes ({len(equity_syms)} symbols)…")
    rep.indeterminate(True)
    log.info(f"Phase 3a: {len(equity_syms)} equity symbols")

    eq_quotes, eq_diags = _stream_quotes(
        stream, api,
        subscribe  = set(equity_syms),
        trade_syms = set(equity_syms),
        wait_for   = equity_syms,
        predicate  = lambda q: q.price > 0,
        timeout    = 25,
        label      = "Phase 3a equity quotes",
    )
    rep.indeterminate(False)
    check_cancel()

    n_priced = sum(1 for t in equity_syms if eq_quotes.get(t, Quote()).price > 0)
    log.info(f"Equity quotes: {n_priced}/{len(equity_syms)} priced")
    rep.status(f"Equity quotes: {n_priced}/{len(equity_syms)} priced — finding ATM strikes…")

    option_syms = set()
    for chain in chains.values():
        price = eq_quotes.get(chain.ticker, Quote()).price
        if price <= 0:
            continue
        chain.current_price = price

        front_atm = min(chain.front_strikes,
                        key=lambda s: abs(float(s['strike-price']) - price))
        # Strikes were already restricted to the front/back intersection in
        # get_chain_info, so this exact-string lookup is guaranteed to match.
        back_atm = next((s for s in chain.back_strikes
                         if s['strike-price'] == front_atm['strike-price']), None)
        if back_atm is None:
            continue

        chain.front_sym = _streamer_symbol(front_atm)
        chain.back_sym  = _streamer_symbol(back_atm)
        chain.strike    = float(front_atm['strike-price'])
        option_syms.update(s for s in (chain.front_sym, chain.back_sym) if s)

    if not option_syms:
        # eq_diags is only populated by the one-shot fallback; on the streaming
        # path there is no per-socket diagnostic to quote, just an empty feed.
        why = describe_diags(eq_diags) if eq_diags else "no quotes arrived on the live stream"
        raise ScanAborted(f"No equity prices from DXLink ({why}). See Debug Log.")

    # ── Phase 3b: option quotes ──────────────────────────────────────────────
    rep.status(f"Phase 3/4: Option quotes ({len(option_syms)} contracts)…")
    rep.indeterminate(True)
    log.info(f"Phase 3b: {len(option_syms)} option symbols")

    opt_quotes, _ = _stream_quotes(
        stream, api,
        # Cumulative: the equities stay subscribed so the table's Price column is
        # live the instant it is painted, not one flush behind.
        subscribe  = set(equity_syms) | option_syms,
        trade_syms = set(equity_syms),
        wait_for   = option_syms,
        predicate  = lambda q: q.bid > 0,
        timeout    = 30,
        label      = "Phase 3b option quotes",
    )
    rep.indeterminate(False)
    check_cancel()

    n_opt = sum(1 for s in option_syms if opt_quotes.get(s, Quote()).bid > 0)
    log.info(f"Option quotes: {n_opt}/{len(option_syms)} priced")

    # ── Phase 4: metrics ─────────────────────────────────────────────────────
    rep.status("Phase 4/4: Calculating calendar spread metrics…")
    rep.progress(85)
    log.info("Phase 4: calculating metrics")

    results = []
    for chain in chains.values():
        if not targeted and chain.current_price < min_price:
            continue
        r = calculate_calendar_metrics(chain, opt_quotes, iv_method)
        if r:
            results.append(r)

    results.sort(key=lambda r: r.fwd_factor, reverse=True)
    log.info(f"Scan complete: {len(results)} setups found")
    return results
