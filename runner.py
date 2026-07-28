"""Decide WHEN to act (NYSE session, in ET), then compute and report the buy signal.

One tick() per call — hourly, the highest frequency any signal needs (VIX and market_dip
refresh every tick). `python runner.py` does a single tick, for an external scheduler (cron,
GitHub Actions, /loop); `python runner.py --loop` runs tick() forever itself, sleeping until
the next wall-clock hour between calls. Sessions come from the exchange schedule, so holidays/
early-closes/DST are handled. Full recompute pre-open and post-close; during the regular
session just track VIX, recomputing macro only on a band crossing. Each macro collector has
its own once-a-day grace period (see should_refresh() in collectors/fed_rate.py, sectors.py,
margin_debt.py), so a VIX band crossing that persists for several hourly ticks in a row only
triggers one real refresh, not one per tick. VIX moves on its own clock, independent of the
NYSE session — see vix_active_window() (roughly 02:00-20:00 CT) — so tick() stays live outside
market hours whenever VIX might still be moving, even though the other signals (which don't
move outside NYSE hours) end up recomputed as a harmless no-op in that window.
"""

import sys
from datetime import datetime, timedelta
from time import monotonic, sleep, time as wall_time
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal
import requests

from config import (
    CF_BYPASS_URL,
    TICK_INTERVAL_SEC,
    VIX_WINDOW_END_HOUR_CT,
    VIX_WINDOW_START_HOUR_CT,
)
from signals.buy_signal import compute_signal

ET = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")
_NYSE = mcal.get_calendar("XNYS")

EXTENDED_AFTER = timedelta(hours=4)  # after-hours run 4h past the close

CLOSED, PRE_MARKET, REGULAR, AFTER_HOURS = "closed", "pre_market", "regular", "after_hours"


def _now_et(now: datetime | None = None) -> datetime:
    return now.astimezone(ET) if now else datetime.now(ET)


def vix_active_window(now: datetime | None = None) -> bool:
    """Whether VIX is expected to still be moving (~02:00-20:00 CT on a trading day).

    Independent of market_session()'s intraday boundaries (pre-market/regular/after-hours),
    but VIX still doesn't move on weekends/holidays, so it shares the same trading-day check.
    """
    now = _now_et(now)
    sched = _NYSE.schedule(start_date=now.date(), end_date=now.date())
    if sched.empty:  # weekend or holiday
        return False
    now_ct = now.astimezone(CT)
    return VIX_WINDOW_START_HOUR_CT <= now_ct.hour < VIX_WINDOW_END_HOUR_CT


def market_session(now: datetime | None = None) -> str:
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


def _cf_bypass_ready(timeout: float = 30.0) -> bool:
    """Poll the (compose-managed, always-running) bypass service until it accepts connections."""
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        try:
            requests.get(CF_BYPASS_URL, timeout=2)
            return True
        except requests.exceptions.ConnectionError:
            sleep(1)
    print(f"cfbypass never became reachable at {CF_BYPASS_URL}")
    return False


def refresh_macro(force: bool = False) -> None:
    """Refresh the slow macro indicators (#2-#5); each skips itself if already refreshed today.

    force=True bypasses each collector's own should_refresh() gate (used by the Telegram
    /refresh command).
    """
    from collectors import fed_rate, margin_debt, sectors

    if force or fed_rate.should_refresh():
        fed_rate.update_fed_rate_data()

    if force or sectors.should_refresh():
        sectors.update_sector_data()

    if (force or margin_debt.should_refresh()) and _cf_bypass_ready():
        margin_debt.update_margin_debt_data()


def tick(now: datetime | None = None):
    """One scheduled cycle. Returns the alert result, or None when nothing needs checking.

    Skipped only when the NYSE session is CLOSED and VIX isn't in its active window either —
    VIX moves outside NYSE hours, so tick() stays live for that on its own clock.
    """
    now = _now_et(now)
    session = market_session(now)
    if session == CLOSED and not vix_active_window(now):
        print(f"[{_stamp(now)}] Market closed, VIX quiet — idle.")
        return None

    result = compute_signal()
    current_vix = next((signal for signal in result.subsignals if signal.name == "vix"), None)
    if current_vix is not None and (session in (PRE_MARKET, AFTER_HOURS) or current_vix.passes):
        refresh_macro()

    report(result, session, now)
    return result


def _stamp(now_et: datetime) -> str:
    return f"{now_et:%Y-%m-%d %H:%M %Z} (local {now_et.astimezone():%H:%M %Z})"


def report(result, session: str, now_et: datetime | None = None) -> None:
    now_et = _now_et(now_et)
    print(f"[{_stamp(now_et)}] session={session}")
    print(f"  Alert: {result.state.upper()}  (passing {result.passing_count}/{len(result.subsignals)}; score {result.score:+.2f})")
    print(f"  Rule: {result.detail}")
    if result.missing_signals:
        print(f"  Missing: {', '.join(result.missing_signals)}")
    for s in result.subsignals:
        status = "PASS" if s.passes else "FAIL"
        print(f"    - {s.name:12s} {status:4s} {s.state:11s} {s.detail}")


def _seconds_until_next_boundary(interval_sec: float) -> float:
    """Seconds until the next wall-clock multiple of interval_sec (e.g. next top-of-hour for
    3600), so ticks land on full hours instead of drifting from whenever the process started.
    """
    if interval_sec <= 0:
        return interval_sec
    return interval_sec - (wall_time() % interval_sec)


def run_forever(interval_sec: float = TICK_INTERVAL_SEC, on_tick=None) -> None:
    """Tick on wall-clock interval_sec boundaries, forever. A failing tick is logged, not fatal.

    If given, on_tick(result, error) is called after every attempt (result is tick()'s return,
    None when the market's closed or the tick failed; error is the caught exception, or None).
    A failing callback is itself logged and skipped, same as a failing tick.
    """
    print(f"Runner starting: tick every {interval_sec / 60:.0f}m (Ctrl+C to stop).")
    try:
        while True:
            result, error = None, None
            try:
                result = tick()
            except Exception as exc:
                error = exc
                print(f"[{_stamp(_now_et())}] tick failed ({type(exc).__name__}: {exc}); retrying next cycle.")
            if on_tick is not None:
                try:
                    on_tick(result, error)
                except Exception as exc:
                    print(f"[{_stamp(_now_et())}] on_tick callback failed ({type(exc).__name__}: {exc})")
            sleep(_seconds_until_next_boundary(interval_sec))
    except KeyboardInterrupt:
        print("Runner stopped.")


if __name__ == "__main__":
    if "--loop" in sys.argv:
        run_forever()
    else:
        tick()
