"""What time is it, in market terms: the NYSE session, and whether VIX is still moving.

Pure calendar, no scheduling: it holds the exchange calendar and the two timezones, and every
answer is a function of the `now` you hand it. Holidays, early closes and DST are the calendar's
problem, not the caller's. Anything that needs to know whether the market is open asks here
rather than importing the tick loop.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

from buysignal.config import VIX_WINDOW_END_HOUR_CT, VIX_WINDOW_START_HOUR_CT

ET = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")
_NYSE = mcal.get_calendar("XNYS")

EXTENDED_AFTER = timedelta(hours=4)  # after-hours run 4h past the close

CLOSED, PRE_MARKET, REGULAR, AFTER_HOURS = "closed", "pre_market", "regular", "after_hours"


def now_et(now: datetime | None = None) -> datetime:
    """`now` in ET (any tz in, ET out); the real time when given nothing."""
    return now.astimezone(ET) if now else datetime.now(ET)


def vix_active_window(now: datetime | None = None) -> bool:
    """Whether VIX is expected to still be moving (~02:00-20:00 CT on a trading day).

    Independent of market_session()'s intraday boundaries (pre-market/regular/after-hours),
    but VIX still doesn't move on weekends/holidays, so it shares the same trading-day check.
    """
    now = now_et(now)
    sched = _NYSE.schedule(start_date=now.date(), end_date=now.date())
    if sched.empty:  # weekend or holiday
        return False
    now_ct = now.astimezone(CT)
    return VIX_WINDOW_START_HOUR_CT <= now_ct.hour < VIX_WINDOW_END_HOUR_CT


def market_session(now: datetime | None = None) -> str:
    """Classify the NYSE session for `now` (any tz; defaults to real time)."""
    now = now_et(now)
    sched = _NYSE.schedule(start_date=now.date(), end_date=now.date())
    if sched.empty:  # weekend or holiday
        return CLOSED

    open_et = sched.iloc[0]["market_open"].astimezone(ET)
    close_et = sched.iloc[0]["market_close"].astimezone(ET)
    premarket = now.replace(hour=4, minute=0, second=0, microsecond=0)

    if now < premarket or now >= close_et + EXTENDED_AFTER:
        return CLOSED
    if now < open_et:
        return PRE_MARKET
    if now < close_et:
        return REGULAR
    return AFTER_HOURS


def stamp(now: datetime) -> str:
    """An ET timestamp with the reader's own clock alongside it, for log lines."""
    return f"{now:%Y-%m-%d %H:%M %Z} (local {now.astimezone():%H:%M %Z})"
