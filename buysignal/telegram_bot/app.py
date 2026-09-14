"""What the bot knows and what it decides: tick state, the daily report, command dispatch.

Holds the only mutable state in the package (the last tick's outcome, whether an alert is
currently active, whether today's report has gone out), reads whatever the renderer needs -
cache mtimes, the refresh journal - and hands the strings to the transport. The renderer
computes nothing and the transport decides nothing; both of those live here.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone

from buysignal import config
from buysignal.analysis import chart, opportunity
from buysignal.collectors import freshness
from buysignal.schedule import market_hours, runner
from buysignal.signals import buy_signal, margin_signal, market_signal, rate_signal, sector_signal, vix_signal, yield_curve_signal
from buysignal.signals.buy_signal import BuySignal, compute_signal
from buysignal.telegram_bot import render, transport

# Per-signal commands: score() called directly (not compute_signal()) so a broken collector
# can't block checking the others. /vix and /dip are handled separately in handle_message
# since they take an allow_refresh flag; these four don't refetch on every call.
_SIGNAL_COMMANDS = {
    "/fedrate": rate_signal.score,
    "/margin": margin_signal.score,
    "/sector": sector_signal.score,
    "/curve": yield_curve_signal.score,
}

# Cache files backing each signal, for the "data as of" freshness table appended to every
# signal message.
_FRESHNESS_FILES = {
    "vix": config.VIX_CSV,
    "market_dip": config.MARKET_CSV,
    "fed_rate": config.FEDWATCH_CSV,
    "margin_debt": config.MARGIN_DEBT_CSV,
    "sector": config.SECTORS_CSV,
    "yield_curve": config.YIELD_CURVE_CSV,
    "valuations": config.VALUATIONS_CSV,
}


@dataclass
class _State:
    start_time: datetime
    last_tick_at: datetime | None = None
    last_ok: bool = True
    last_error: str | None = None
    last_result: BuySignal | None = None
    alerting: bool = False
    last_report_date: date | None = None


_state = _State(start_time=datetime.now(timezone.utc))
_lock = threading.Lock()


def _cache_ages() -> list[tuple[str, datetime | None]]:
    return [(name, freshness.last_modified(path)) for name, path in _FRESHNESS_FILES.items()]


def _signal_message(result: BuySignal) -> str:
    return render.signal_message(result, _cache_ages(), datetime.now(timezone.utc))


def is_alerting(result: BuySignal) -> bool:
    by_name = {s.name: s for s in result.subsignals}
    vix = by_name.get("vix")
    dip = by_name.get("market_dip")
    return (vix is not None and vix.passes) or (dip is not None and dip.passes) or result.state in ("soft", "strong")


def trigger_reasons(result: BuySignal) -> str:
    by_name = {s.name: s for s in result.subsignals}
    reasons = []
    vix = by_name.get("vix")
    if vix is not None and vix.passes:
        reasons.append("VIX elevated")
    dip = by_name.get("market_dip")
    if dip is not None and dip.passes:
        reasons.append("SPY dip")
    if result.state in ("soft", "strong"):
        reasons.append(f"checklist {result.state}")
    return ", ".join(reasons)


def report_due(now_ct: datetime, last_report_date: date | None, report_hour: int) -> bool:
    """Whether the daily report is due: the report hour, and not already sent this CT date."""
    return now_ct.hour == report_hour and last_report_date != now_ct.date()


def report_result(
    result: BuySignal | None,
    error: Exception | None,
    cached: BuySignal | None,
    compute,
) -> BuySignal | None:
    """Pick what the daily report shows: this tick's result, a fresh compute, or the cache.

    The report hour can fall outside tick()'s active window, leaving `result` None with
    nothing actually wrong, so an idle tick computes fresh. A failed tick falls back to the
    cache instead, since a live compute right now would likely fail the same way.
    """
    if result is not None:
        return result
    if error is None:
        return compute()
    return cached


def handle_tick(result: BuySignal | None, error: Exception | None, now: datetime | None = None) -> None:
    """Called by runner.run_forever() after every tick attempt."""
    now = now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)
    with _lock:
        _state.last_tick_at = now
        _state.last_ok = error is None
        _state.last_error = str(error) if error else None
        if result is not None:
            _state.last_result = result

    # The journal owns the dedup (once per collector per day), so there's no state here to
    # keep in sync with it: whatever comes back has not been pushed yet.
    for failure in runner.macro_journal().take_unreported_failures(now.astimezone(market_hours.ET)):
        transport.send(render.refresh_failures([failure]))

    now_ct = now.astimezone(market_hours.CT)
    with _lock:
        due = report_due(now_ct, _state.last_report_date, config.DAILY_REPORT_HOUR_CT)
        if due:
            _state.last_report_date = now_ct.date()
        cached_result = _state.last_result
    if due:
        reported = report_result(result, error, cached_result, compute_signal)
        if reported is not None:
            prefix = (
                f"⚠️ latest tick failed ({render.esc(str(error))}); showing last cached data\n\n"
                if error is not None
                else ""
            )
            transport.send(f"📅 <b>End of day</b>\n\n{prefix}{_signal_message(reported)}")

    if result is None:
        return

    alerting = is_alerting(result)
    with _lock:
        was_alerting = _state.alerting
        _state.alerting = alerting

    if alerting and not was_alerting:
        reasons = trigger_reasons(result)
        transport.send(f"🔔 <b>Signal active</b> ({render.esc(reasons)})\n\n{_signal_message(result)}")
    elif was_alerting and not alerting:
        transport.send("Signal cleared, back to normal.")


def _single_signal(score_fn) -> str:
    try:
        with buy_signal.SIGNAL_LOCK:
            s = score_fn()
    except Exception as exc:
        return f"⚠️ failed to compute ({render.esc(type(exc).__name__)}: {render.esc(str(exc))})"
    return render.subsignal_block(s)


def market_open() -> bool:
    return market_hours.market_session() != market_hours.CLOSED


def _status_message() -> str:
    with _lock:
        status = render.RunnerStatus(
            uptime=datetime.now(timezone.utc) - _state.start_time,
            last_tick_at=_state.last_tick_at,
            ok=_state.last_ok,
            error=_state.last_error,
            alert_state=_state.last_result.state if _state.last_result is not None else None,
        )
    statuses = runner.macro_journal().statuses()
    return render.status_message(status, statuses, datetime.now(timezone.utc))


def _whattobuy() -> str:
    with buy_signal.SIGNAL_LOCK:
        failed = runner.refresh_macro(only=("valuations",))
        opp = opportunity.analyze()
    opp.notes.extend(
        render.stale_collector_notes("valuations", failed, runner.macro_journal().statuses())
    )
    with _lock:
        last_result = _state.last_result
    opp.buy_signal_state = last_result.state if last_result is not None else None
    try:
        transport.send_photo(chart.render_chart(opp), "Segment valuations")
    except Exception as exc:
        opp.notes.append(f"chart rendering failed ({type(exc).__name__}: {exc})")

    reply = render.opportunity_message(opp, config.SMALL_CAP_EASING_OBS)
    return f"{render.refresh_failures(failed)}\n\n{reply}" if failed else reply


def _refresh() -> str:
    with buy_signal.SIGNAL_LOCK:
        failed = runner.refresh_macro(force=True)
    # Shown in this reply, so don't also push them from the next tick.
    runner.macro_journal().take_unreported_failures(datetime.now(market_hours.ET))
    reply = _signal_message(compute_signal(allow_refresh=True))
    return f"{render.refresh_failures(failed)}\n\n{reply}" if failed else reply


def handle_message(text: str) -> str | None:
    text = text.strip()
    # VIX/market_dip refetch on every call; prefer the cache while the market's closed (e.g.
    # weekends) instead of paying the fetch + anti-bot jitter delay for data that can't have moved.
    open_now = market_open()
    if text == "/signal":
        return _signal_message(compute_signal(allow_refresh=open_now))
    if text == "/vix":
        return _single_signal(lambda: vix_signal.score(allow_refresh=open_now))
    if text == "/dip":
        return _single_signal(lambda: market_signal.score(allow_refresh=open_now))
    if text in _SIGNAL_COMMANDS:
        return _single_signal(_SIGNAL_COMMANDS[text])
    if text == "/whattobuy":
        return _whattobuy()
    if text == "/refresh":
        return _refresh()
    if text == "/status":
        return _status_message()
    if text in ("/start", "/help"):
        return render.HELP_TEXT
    return "Unrecognized command, try /help."


def start() -> None:
    """Start polling for commands in a background thread."""
    transport.start(handle_message)
