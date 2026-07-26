"""Decide WHEN to act (NYSE session, in ET), then compute and report the buy signal.

One tick() per call — hourly, the highest frequency any signal needs (VIX and market_dip
refresh every tick). `python runner.py` does a single tick, for an external scheduler (cron,
GitHub Actions, /loop); `python runner.py --loop` runs tick() forever itself, sleeping
TICK_INTERVAL_SEC between calls. Sessions come from the exchange schedule, so holidays/
early-closes/DST are handled. Full recompute pre-open and post-close; during the regular
session just track VIX, recomputing macro only on a band crossing. Each macro collector has
its own once-a-day grace period (see should_refresh() in collectors/fed_rate.py, sectors.py,
margin_debt.py), so a VIX band crossing that persists for several hourly ticks in a row only
triggers one real refresh, not one per tick. VIX is tracked across all of 04:00-20:00 ET (it
moves in extended hours too).
"""

import subprocess
import sys
from datetime import datetime, time, timedelta
from time import monotonic, sleep
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal
import requests

from buy_signal import compute_signal
from config import CF_BYPASS_CONTAINER, CF_BYPASS_IMAGE, CF_BYPASS_PORT, CF_BYPASS_URL, TICK_INTERVAL_SEC

ET = ZoneInfo("America/New_York")
_NYSE = mcal.get_calendar("XNYS")

PREMARKET_OPEN = time(4, 0)          # extended hours start (ET)
EXTENDED_AFTER = timedelta(hours=4)  # after-hours run 4h past the close

CLOSED, PRE_MARKET, REGULAR, AFTER_HOURS = "closed", "pre_market", "regular", "after_hours"


def _now_et(now: datetime | None = None) -> datetime:
    return now.astimezone(ET) if now else datetime.now(ET)


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
    """Poll the bypass service until it accepts connections, or give up after `timeout`s."""
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        try:
            requests.get(CF_BYPASS_URL, timeout=2)
            return True
        except requests.exceptions.ConnectionError:
            sleep(1)
    print(f"{CF_BYPASS_CONTAINER} never became reachable at {CF_BYPASS_URL}")
    return False


def _ensure_cf_bypass_running() -> bool:
    """Start the local Cloudflare-bypass container (for margin_debt) if it isn't up."""
    name_filter = f"name=^{CF_BYPASS_CONTAINER}$"
    try:
        running = subprocess.run(
            ["docker", "ps", "-q", "-f", name_filter],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"docker unavailable ({type(exc).__name__}); skipping margin-debt refresh.")
        return False

    if running.stdout.strip():
        return True

    # `docker start` on a stopped container can leave stale X11/Xvfb state behind
    # (the image runs a headed browser internally); recreate it fresh instead.
    subprocess.run(["docker", "rm", "-f", CF_BYPASS_CONTAINER], capture_output=True, text=True, timeout=10)

    print(f"Starting {CF_BYPASS_CONTAINER} container for margin-debt scraping...")
    result = subprocess.run(
        ["docker", "run", "-d", "--name", CF_BYPASS_CONTAINER,
         "-p", f"{CF_BYPASS_PORT}:8000", CF_BYPASS_IMAGE],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        print(f"Failed to start {CF_BYPASS_CONTAINER}: {result.stderr.strip()}")
        return False
    return _cf_bypass_ready()


def refresh_macro() -> None:
    """Refresh the slow macro indicators (#2-#5); each skips itself if already refreshed today."""
    from collectors import fed_rate, margin_debt, sectors

    if fed_rate.should_refresh():
        fed_rate.update_fed_rate_data()

    if sectors.should_refresh():
        sectors.update_sector_data()

    # should_refresh() skips most of the month, so the container starts on demand.
    if margin_debt.should_refresh() and _ensure_cf_bypass_running():
        margin_debt.update_margin_debt_data()


def tick(now: datetime | None = None):
    """One scheduled cycle. Returns the alert result, or None when the market is closed."""
    now = _now_et(now)
    session = market_session(now)
    if session == CLOSED:
        print(f"[{_stamp(now)}] Market closed — idle.")
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


def run_forever(interval_sec: float = TICK_INTERVAL_SEC, on_tick=None) -> None:
    """Tick every interval_sec, forever. A failing tick is logged and skipped, not fatal.

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
            sleep(interval_sec)
    except KeyboardInterrupt:
        print("Runner stopped.")


if __name__ == "__main__":
    if "--loop" in sys.argv:
        run_forever()
    else:
        tick()
