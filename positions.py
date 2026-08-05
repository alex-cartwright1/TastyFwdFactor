"""Position bookkeeping: turning a live quote snapshot into IVs, current debit
and P/L. Pure functions over `Position` — no Qt, no I/O."""

from datetime import datetime

from config import parse_iso_date
from data_models import Position, Quote
from pricing import (
    DAYS_PER_YEAR, PER_CONTRACT, calc_implied_vol, forward_iv, solve_calendar,
)


def update_position_metrics(p: Position, eq: Quote, fq: Quote, bq: Quote,
                            iv_method) -> bool:
    """Fold a quote snapshot into `p`. Returns True if anything was updated.

    Leg prices are sticky: a quote that arrives with only a bid must not wipe
    the last known ask, or the P/L would flicker between "priced" and "—". That
    is what `Quote.has` tests — a field the feed has actually quoted is written
    whatever its value, so a bid pulled to 0.0 shows as 0.0, while a field the
    feed has never carried leaves the last known price alone.
    """
    price = eq.price
    if price <= 0:
        return False

    p.underlying_price = price
    if fq.has('bid'): p.f_bid = fq.bid
    if fq.has('ask'): p.f_ask = fq.ask
    if bq.has('bid'): p.b_bid = bq.bid
    if bq.has('ask'): p.b_ask = bq.ask

    if not p.has_full_legs:
        return True

    f_dte, b_dte = p.current_dtes()
    strike = float(p.strike)

    ivs = solve_calendar(price, strike, f_dte, b_dte,
                         p.f_bid, p.f_ask, p.b_bid, p.b_ask, iv_method)
    if ivs is not None:
        p.front_iv   = ivs.front_iv
        p.back_iv    = ivs.back_iv
        p.fwd_iv     = ivs.fwd_iv
        p.fwd_factor = ivs.fwd_factor

    # Current net debit (mid) and total P/L against the entry fill.
    cur_debit = (p.b_bid + p.b_ask) / 2 - (p.f_bid + p.f_ask) / 2
    p.cur_debit = cur_debit
    p.cur_pl = (cur_debit - p.entry_debit) * PER_CONTRACT * int(p.contracts or 1)

    _snapshot_open_metrics(p, price, f_dte, b_dte)
    return True


def _snapshot_open_metrics(p: Position, price, f_dte_cur, b_dte_cur) -> None:
    """One-shot: record the forward factor implied by the entry fill.

    Computed the first time live data arrives (and persisted from then on) using
    today's underlying price with the DTEs *as of the open date*, so the opened
    figure stays comparable to the live one as the position ages.
    """
    if p.opened_fwd_factor is not None or p.fwd_factor is None:
        return

    opened_on = parse_iso_date(p.opened_on)
    if opened_on is not None:
        f_date = datetime.strptime(p.front_expiry, '%Y-%m-%d').date()
        b_date = datetime.strptime(p.back_expiry,  '%Y-%m-%d').date()
        f_dte0 = max((f_date - opened_on).days, 1)
        b_dte0 = max((b_date - opened_on).days, f_dte0 + 1)
    else:
        f_dte0 = max(f_dte_cur, 1)
        b_dte0 = max(b_dte_cur, f_dte_cur + 1)

    t1, t2 = f_dte0 / DAYS_PER_YEAR, b_dte0 / DAYS_PER_YEAR
    strike = float(p.strike)
    f_iv = calc_implied_vol(float(p.front_credit), price, strike, t1)
    b_iv = calc_implied_vol(float(p.back_paid),    price, strike, t2)
    if f_iv <= 0.01 or b_iv <= 0.01:
        return

    fwd = forward_iv(f_iv, b_iv, t1, t2)
    if fwd is None:
        return

    p.opened_underlying_price = price
    p.opened_front_iv   = f_iv
    p.opened_back_iv    = b_iv
    p.opened_fwd_iv     = fwd
    p.opened_fwd_factor = (f_iv - fwd) / fwd
