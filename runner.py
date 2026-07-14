"""Decide WHEN to act (NYSE session, in ET), then compute and report the buy signal.

One tick() per call, driven by an external scheduler (cron, /loop, a loop). Sessions come
from the exchange schedule, so holidays/early-closes/DST are handled. Full recompute pre-open
and post-close; during the regular session just track VIX, recomputing macro only on a band
crossing. VIX is tracked across all of 04:00-20:00 ET (it moves in extended hours too).
"""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

from buy_signal import compute_signal
from collectors import vix
from config import VIX_SOFT

ET = ZoneInfo("America/New_York")
_NYSE = mcal.get_calendar("XNYS")

PREMARKET_OPEN = time(4, 0)          # extended hours start (ET)
EXTENDED_AFTER = timedelta(hours=4)  # after-hours run 4h past the close

CLOSED, PRE_MARKET, REGULAR, AFTER_HOURS = "closed", "pre_market", "regular", "after_hours"


def _now_et(now=None):
    return now.astimezone(ET) if now else datetime.now(ET)


def market_session(now=None):
    """Classify the NYSE session for `now` (any tz; defaults to real time)."""
    now = _now_et(now)
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


def refresh_macro():
    """Refresh the slow macro indicators (#2-#5); skip ones not built yet."""
    from collectors import fed_rate, margin_debt, sectors

    for update in (fed_rate.update_fed_rate_data,
                   margin_debt.update_margin_debt_data,
                   sectors.update_sector_data):
        try:
            update()
        except NotImplementedError:
            pass


def tick(now=None):
    """One scheduled cycle. Returns the BuySignal, or None when the market is closed."""
    now = _now_et(now)
    session = market_session(now)
    if session == CLOSED:
        print(f"[{_stamp(now)}] Market closed — idle.")
        return None

    current_vix = vix.fetch_vix()
    if session in (PRE_MARKET, AFTER_HOURS) or current_vix >= VIX_SOFT:
        refresh_macro()

    result = compute_signal(vix=current_vix)
    report(result, session, now)
    return result


def _stamp(now_et):
    return f"{now_et:%Y-%m-%d %H:%M %Z} (local {now_et.astimezone():%H:%M %Z})"


def report(result, session, now_et=None):
    now_et = _now_et(now_et)
    print(f"[{_stamp(now_et)}] session={session}")
    print(f"  Signal: {result.state.upper()}  (score {result.score:+.2f})")
    for s in result.subsignals:
        print(f"    - {s.name:12s} {s.state:6s} {s.detail}")


if __name__ == "__main__":
    tick()
