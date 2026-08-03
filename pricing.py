"""Black-Scholes, implied-vol solving, and the forward-factor model.

    fwd_iv     = sqrt( (t2 · back_iv² − t1 · front_iv²) / (t2 − t1) )
    fwd_factor = (front_iv − fwd_iv) / fwd_iv

`t = dte / 365`, `r = 0.04`. DXLink does not stream IV, so the "Provided Data"
IV method falls back to midpoint.

Pure functions only — no Qt, no I/O, no globals. Everything here runs on worker
threads.
"""

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np
from scipy.stats import norm

RISK_FREE_RATE = 0.04
PER_CONTRACT   = 100     # share multiplier
DAYS_PER_YEAR  = 365.0

IV_BID_ASK = "Bid Front / Ask Back"


# ─── Black-Scholes ───────────────────────────────────────────────────────────

def bs_price(S, K, T, r, v, option_type='c'):
    if T <= 0 or v <= 0:
        return 0.0
    d1 = (np.log(S / K) + (r + 0.5 * v**2) * T) / (v * np.sqrt(T))
    d2 = d1 - v * np.sqrt(T)
    if option_type == 'c':
        return float(S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2))
    return float(K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1))


def bs_call_vec(S_arr, K, T, r, sigma):
    """Vectorized Black-Scholes call price. At T<=0 returns intrinsic."""
    S_arr = np.asarray(S_arr, dtype=float)
    if T <= 0 or sigma <= 0:
        return np.maximum(S_arr - K, 0.0)
    sqrtT = math.sqrt(T)
    d1 = (np.log(S_arr / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    return S_arr * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)


def calc_implied_vol(target_price, S, K, T, r=RISK_FREE_RATE):
    """Newton solve for call IV. Returns >= 0.001 so callers can reject on a
    floor test rather than on None.

    The result is coerced to a plain `float`: `sigma` picks up numpy's dtype from
    the vega term, and a `np.float64` reaching a Qt model breaks sorting outright
    — PySide can't convert it to a QVariant double, so `lessThan` ends up
    comparing opaque objects. It stays a float subclass, so nothing else notices.
    """
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
            return float(max(sigma, 0.001))
        if vega < 1e-4:
            break
        sigma += diff / vega
    return float(max(sigma, 0.001))


# ─── Forward factor ──────────────────────────────────────────────────────────

@dataclass
class CalendarIVs:
    front_iv:   float
    back_iv:    float
    fwd_iv:     float
    fwd_factor: float
    debit:      float


def forward_iv(front_iv, back_iv, t1, t2) -> Optional[float]:
    """Forward vol between t1 and t2, or None if the variance term is negative
    (an inverted term structure the calendar model can't price)."""
    if t2 <= t1:
        return None
    var_diff = t2 * back_iv**2 - t1 * front_iv**2
    if var_diff < 0:
        return None
    fwd = math.sqrt(var_diff / (t2 - t1))
    return fwd if fwd > 0 else None


def solve_calendar(price, strike, front_dte, back_dte,
                   f_bid, f_ask, b_bid, b_ask, iv_method) -> Optional[CalendarIVs]:
    """Solve both legs' IVs and the forward factor from a quote snapshot.

    Returns None whenever the snapshot can't support the model: a missing bid,
    a degenerate IV solve, or a negative forward variance. This is the single
    implementation used by the scan, the live results refresh and the positions
    refresh — they must not drift apart.
    """
    if price <= 0 or strike <= 0 or f_bid <= 0 or b_bid <= 0:
        return None

    t1 = front_dte / DAYS_PER_YEAR
    t2 = back_dte  / DAYS_PER_YEAR
    if t1 <= 0 or t2 <= t1:
        return None

    if iv_method == IV_BID_ASK:
        f_iv = calc_implied_vol(f_bid, price, strike, t1)
        b_iv = calc_implied_vol(b_ask, price, strike, t2)
    else:
        # Midpoint — also used for "Provided Data", since DXLink doesn't stream IV.
        f_iv = calc_implied_vol((f_bid + f_ask) / 2, price, strike, t1)
        b_iv = calc_implied_vol((b_bid + b_ask) / 2, price, strike, t2)

    if f_iv <= 0.01 or b_iv <= 0.01:
        return None

    fwd = forward_iv(f_iv, b_iv, t1, t2)
    if fwd is None:
        return None

    return CalendarIVs(
        front_iv   = f_iv,
        back_iv    = b_iv,
        fwd_iv     = fwd,
        fwd_factor = (f_iv - fwd) / fwd,
        debit      = (b_bid + b_ask) / 2 - (f_bid + f_ask) / 2,
    )


# ─── P/L at front expiration ─────────────────────────────────────────────────

def pl_curve(S_range, strike, t_remaining, back_iv, back_paid, front_credit,
             r=RISK_FREE_RATE):
    """P/L per contract at front expiration, as a function of underlying price.

    The short front call is at intrinsic (it expires); the long back call still
    has `t_remaining` years of extrinsic value priced at `back_iv`.
    """
    front_intrinsic = np.maximum(np.asarray(S_range, dtype=float) - strike, 0.0)
    back_value      = bs_call_vec(S_range, strike, t_remaining, r, back_iv)
    per_share = back_value - front_intrinsic - back_paid + front_credit
    return per_share * PER_CONTRACT


def breakevens(S, pl):
    """Approximate breakeven prices from sign changes in `pl`."""
    out = []
    for i in range(1, len(pl)):
        if pl[i - 1] == 0:
            out.append(S[i - 1])
        elif (pl[i - 1] < 0) != (pl[i] < 0):
            x0, x1 = S[i - 1], S[i]
            y0, y1 = pl[i - 1], pl[i]
            out.append(x0 - y0 * (x1 - x0) / (y1 - y0))
    return out


# ─── Formatting ──────────────────────────────────────────────────────────────

def fmt_market_cap(cap):
    if cap is None or not isinstance(cap, (int, float)) or cap <= 0:
        return "—"
    if cap >= 1e12: return f"${cap / 1e12:.2f}T"
    if cap >= 1e9:  return f"${cap / 1e9:.2f}B"
    if cap >= 1e6:  return f"${cap / 1e6:.0f}M"
    return f"${cap:.0f}"


def fmt_money(v):
    """Sign goes outside the currency symbol: -$15.00, not $-15.00."""
    if not isinstance(v, (int, float)):
        return "—"
    return f"-${abs(v):.2f}" if v < 0 else f"${v:.2f}"


def fmt_iv(v):
    return f"{v * 100:.2f}%" if isinstance(v, (int, float)) and v > 0 else "—"


def fmt_pct_signed(v):
    return f"{v * 100:+.2f}%" if isinstance(v, (int, float)) else "—"
