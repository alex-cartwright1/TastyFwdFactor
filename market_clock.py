"""New York wall clock and US equity market status.

Pure logic — no Qt, no I/O. The header clock calls this once a second, so it has
to be cheap and it has to work with no network: the holiday calendar is passed
in (fetched once from Tastytrade, see `api.MarketDataSession.fetch_market_calendar`)
and everything else is derived locally from the `America/New_York` zone.

Passing `None` for the calendar is a supported, degraded mode — weekends and
regular trading hours are still honoured, only holidays are missed — so the
indicator lights up correctly on the first paint, before the calendar has landed.
"""

from datetime import datetime, time as _time
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

# Regular Trading Hours. Half days close at 13:00 ET; the early close is the only
# way a half day differs, so it needs no separate open time.
RTH_OPEN       = _time(9, 30)
RTH_CLOSE      = _time(16, 0)
HALF_DAY_CLOSE = _time(13, 0)

OPEN     = "open"
CLOSED   = "closed"


def nyc_now():
    """Current wall-clock time in New York, tz-aware."""
    return datetime.now(NY)


def format_nyc(moment=None):
    """`YYYY-MM-DD HH:MM:SS EST` / `… EDT` — the zone abbreviation flips with DST
    because it is read off the tzdata offset rather than hard-coded."""
    moment = moment or nyc_now()
    return f"{moment:%Y-%m-%d %H:%M:%S} {moment.tzname()}"


def market_status(moment=None, calendar=None):
    """Return ``(state, tooltip)`` where state is `OPEN` or `CLOSED`.

    `calendar` is a `data_models.MarketCalendar` or None.
    """
    moment = moment or nyc_now()
    today  = moment.date()
    clock  = moment.time()

    if moment.weekday() >= 5:                       # Saturday / Sunday
        return CLOSED, "Market Closed (Weekend)"

    if calendar and today in calendar.holidays:
        return CLOSED, "Market Closed (Market Holiday)"

    half  = bool(calendar and today in calendar.half_days)
    close = HALF_DAY_CLOSE if half else RTH_CLOSE

    if clock < RTH_OPEN:
        return CLOSED, f"Market Closed (Pre-market — opens {RTH_OPEN:%H:%M} ET)"
    if clock >= close:
        return CLOSED, f"Market Closed (After hours — closed {close:%H:%M} ET)"

    if half:
        return OPEN, f"Market Open (Half Day — closes {close:%H:%M} ET)"
    return OPEN, "Market Open (Regular Hours)"
