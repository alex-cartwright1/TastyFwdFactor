"""Plain-Python data carriers shared by the scan pipeline, the Qt table models
and the persistence layer. No Qt and no I/O in here.

`Position` distinguishes *persisted* fields (written to positions.json) from
*live* fields (recomputed from the quote stream on every launch). Only the
former survive `to_dict()`.
"""

import math
from dataclasses import dataclass, field, asdict, fields
from datetime import datetime, date
from typing import Optional, List, Dict, Any


# ─── Quotes ──────────────────────────────────────────────────────────────────

@dataclass
class Quote:
    """Latest bid/ask/last for one streamer symbol. 0.0 means 'not yet seen'."""
    bid:  float = 0.0
    ask:  float = 0.0
    last: float = 0.0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2 if self.bid > 0 and self.ask > 0 else 0.0

    @property
    def price(self) -> float:
        """Best available underlying price: last trade, else the mid."""
        return self.last if self.last > 0 else self.mid

    def merge(self, other: "Quote") -> None:
        """Apply non-zero fields of `other` on top of this quote in place."""
        if other.bid  > 0: self.bid  = other.bid
        if other.ask  > 0: self.ask  = other.ask
        if other.last > 0: self.last = other.last


# ─── Reference data (Tastytrade /market-metrics, /market-time) ───────────────

@dataclass
class TickerInfo:
    """Per-symbol reference data used by the scan's pre/post filters.

    Sourced from the Tastytrade SDK's `market-metrics` endpoint. The fields are
    deliberately the three the filters actually consume — the endpoint returns
    far more, but persisting only these keeps `ticker_info.json` compatible with
    files written before the yfinance → Tastytrade migration.
    """
    earnings:   Optional[date]  = None
    market_cap: Optional[float] = None
    ex_div:     Optional[date]  = None

    @classmethod
    def from_metric(cls, metric, today: date) -> "TickerInfo":
        """Map one `tastytrade.metrics.MarketMetricInfo` onto this carrier.

        Everything is coerced out of `Decimal` and pydantic models here, at the
        boundary, so nothing downstream — least of all `SORT_ROLE` — ever sees a
        non-native numeric type.
        """
        def _future(value):
            """Tastytrade reports the *last* ex-div as well as the next one; a
            past date is not a filterable event, so drop it."""
            return value if isinstance(value, date) and value >= today else None

        earnings = getattr(metric, 'earnings', None)
        cap      = getattr(metric, 'market_cap', None)
        return cls(
            earnings   = _future(getattr(earnings, 'expected_report_date', None)),
            market_cap = float(cap) if cap is not None else None,
            # dividend_next_date is the forward-looking field; dividend_ex_date
            # is usually the most recent one, so it is only a fallback.
            ex_div     = (_future(getattr(metric, 'dividend_next_date', None))
                          or _future(getattr(metric, 'dividend_ex_date', None))),
        )


@dataclass
class MarketCalendar:
    """US equity market holidays and half days, as published by Tastytrade."""
    holidays:  List[date] = field(default_factory=list)
    half_days: List[date] = field(default_factory=list)


# ─── Option chain ────────────────────────────────────────────────────────────

@dataclass
class ChainInfo:
    """Front/back leg structure for one ticker, as picked from the nested chain.

    A skipped ticker is represented by an instance carrying only `ticker` and
    `skip_reason`, so the scan can aggregate rejection counts instead of having
    to catch exceptions per symbol.
    """
    ticker:          str
    skip_reason:     Optional[str] = None
    front_dte:       int   = 0
    back_dte:        int   = 0
    front_exp_date:  Optional[date] = None
    back_exp_date:   Optional[date] = None
    front_strikes:   List[Dict[str, Any]] = field(default_factory=list)
    back_strikes:    List[Dict[str, Any]] = field(default_factory=list)

    # Filled in later by the scan once quotes are known.
    earnings_date:   Optional[date] = None
    ex_div_date:     Optional[date] = None
    market_cap:      Optional[float] = None
    current_price:   float = 0.0
    strike:          float = 0.0
    front_sym:       str = ''
    back_sym:        str = ''

    @property
    def ok(self) -> bool:
        return self.skip_reason is None


# ─── Scan results ────────────────────────────────────────────────────────────

@dataclass
class ScanResult:
    """One calendar-spread candidate row."""
    ticker:     str
    price:      float
    market_cap: Optional[float]
    strike:     float
    front_dte:  int
    back_dte:   int
    f_bid:      float
    f_ask:      float
    b_bid:      float
    b_ask:      float
    front_iv:   float
    back_iv:    float
    fwd_iv:     float
    fwd_factor: float
    debit:      float
    earnings:   str
    front_sym:  str
    back_sym:   str

    @property
    def f_spread(self) -> float:
        return self.f_ask - self.f_bid

    @property
    def b_spread(self) -> float:
        return self.b_ask - self.b_bid


# ─── Positions ───────────────────────────────────────────────────────────────

# Fields written to positions.json. Everything else on Position is live state
# recomputed from the quote stream and deliberately not persisted.
_PERSISTED = (
    'id', 'ticker', 'strike', 'front_expiry', 'back_expiry',
    'front_credit', 'back_paid', 'contracts', 'notes', 'opened_on',
    'opened_underlying_price', 'opened_front_iv', 'opened_back_iv',
    'opened_fwd_iv', 'opened_fwd_factor',
)


@dataclass
class Position:
    id:           str
    ticker:       str
    strike:       float
    front_expiry: str          # 'YYYY-MM-DD'
    back_expiry:  str
    front_credit: float
    back_paid:    float
    contracts:    int = 1
    notes:        str = ''
    opened_on:    Optional[str] = None

    # Snapshot of the metrics implied at trade open, computed once the first
    # live quote arrives and persisted from then on.
    opened_underlying_price: Optional[float] = None
    opened_front_iv:         Optional[float] = None
    opened_back_iv:          Optional[float] = None
    opened_fwd_iv:           Optional[float] = None
    opened_fwd_factor:       Optional[float] = None

    # ── live state (never persisted) ──
    front_sym:        str = ''
    back_sym:         str = ''
    resolve_error:    Optional[str] = None
    underlying_price: Optional[float] = None
    f_bid: Optional[float] = None
    f_ask: Optional[float] = None
    b_bid: Optional[float] = None
    b_ask: Optional[float] = None
    front_iv:   Optional[float] = None
    back_iv:    Optional[float] = None
    fwd_iv:     Optional[float] = None
    fwd_factor: Optional[float] = None
    cur_debit:  Optional[float] = None
    cur_pl:     Optional[float] = None

    # ── persistence ──

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Position":
        known = {f.name for f in fields(cls)}
        clean = {k: v for k, v in d.items() if k in known}
        clean.setdefault('contracts', 1)
        return cls(**clean)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return {k: d[k] for k in _PERSISTED}

    def clear_live_state(self) -> None:
        """Drop resolved symbols and live metrics — used when a chain-identifying
        field changes and the legs must be re-resolved from a fresh chain."""
        self.front_sym = self.back_sym = ''
        self.resolve_error = None
        for name in ('underlying_price', 'f_bid', 'f_ask', 'b_bid', 'b_ask',
                     'front_iv', 'back_iv', 'fwd_iv', 'fwd_factor',
                     'cur_debit', 'cur_pl'):
            setattr(self, name, None)

    # ── derived ──

    def current_dtes(self, today: Optional[date] = None):
        today = today or datetime.today().date()
        try:
            f = datetime.strptime(self.front_expiry, '%Y-%m-%d').date()
            b = datetime.strptime(self.back_expiry,  '%Y-%m-%d').date()
        except (TypeError, ValueError):
            return 0, 0
        return max((f - today).days, 0), max((b - today).days, 0)

    @property
    def entry_debit(self) -> float:
        return float(self.back_paid) - float(self.front_credit)

    @property
    def has_full_legs(self) -> bool:
        return all(isinstance(v, (int, float)) and v > 0
                   for v in (self.f_bid, self.f_ask, self.b_bid, self.b_ask))

    @property
    def chart_price(self) -> float:
        p = self.underlying_price or self.opened_underlying_price or 0.0
        return p if isinstance(p, (int, float)) and not math.isnan(p) else 0.0
