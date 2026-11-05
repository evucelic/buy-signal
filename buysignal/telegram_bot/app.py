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
from buysignal.resources import force_refreshes
from buysignal.portfolio import commands as portfolio_commands, quotes as personal_quotes, sessions as personal_sessions, presentation as personal_presentation
from buysignal.portfolio.journal import PortfolioStore
from buysignal.portfolio.service import PrecloseService
import os
import logging
from buysignal.analysis import chart, opportunity, signal_charts
from buysignal.collectors import freshness
from buysignal.schedule import market_hours, runner
from buysignal.signals import buy_signal, credit_signal, margin_signal, market_signal, rate_signal, sector_signal, trend_signal, vix_signal, yield_curve_signal
from buysignal.signals.buy_signal import BuySignal, compute_signal
from buysignal.telegram_bot import render, transport, views, interactive
from buysignal.telegram_bot.snapshots import SnapshotStore
from buysignal.telegram_bot.messages import Report, ReportMessage

# Per-signal commands: score() called directly (not compute_signal()) so a broken collector
# can't block checking the others. /vix and /dip are handled separately in handle_message
# since they take an allow_refresh flag; these four don't refetch on every call.
_SIGNAL_COMMANDS = {
    "/fedrate": rate_signal.score,
    "/margin": margin_signal.score,
    "/sector": sector_signal.score,
    "/curve": yield_curve_signal.score,
    "/credit": credit_signal.score,
}

# Cache files backing each signal, for the "data as of" freshness table appended to every
# signal message.
_FRESHNESS_FILES = {
    "vix": config.VIX_CSV,
    "market_dip": config.MARKET_DAILY_CSV,
    "credit": config.CREDIT_CSV,
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
_snapshot_store = SnapshotStore()
_portfolio_store = None
_personal_service = None
_portfolio_lock = threading.Lock()
_notification_lock = threading.RLock()
log = logging.getLogger(__name__)


def _utc_now():
    return datetime.now(timezone.utc)


def _get_portfolio_store():
    global _portfolio_store
    with _portfolio_lock:
        if _portfolio_store is None:
            _portfolio_store = PortfolioStore()
        return _portfolio_store


def _deliver_report(result, heading='', prefix='', *, now=None, preclose=None):
    now = now or _utc_now()
    with _notification_lock:
        upcoming = preclose
        if upcoming is None and _personal_service is not None:
            upcoming = personal_sessions.due(now, _get_portfolio_store())
        if upcoming:
            prices = personal_quotes.all_quotes(_get_portfolio_store(), now=now)
            heading = personal_presentation.preclose_heading(upcoming) + heading
            prefix = personal_presentation.quote_block(prices) + '\n\n' + prefix
        report = _signal_report(result, heading, prefix)
        if preclose and (_personal_service is not None and _personal_service.stopped.is_set()
                         or all(s.closes <= _utc_now() for s in preclose)):
            return False
        delivered = transport.send_report(report)
        if delivered and upcoming and preclose is None:
            _get_portfolio_store().mark_delivered(upcoming, _utc_now())
        return delivered


def _deliver_preclose(sessions, now):
    try:
        result = compute_signal(allow_refresh=True)
        prefix = ''
    except Exception:
        log.exception('Pre-close global assessment failed')
        with _lock:
            cached = _state.last_result
        if cached is None:
            result = BuySignal(0, 'incomplete', 0, [], missing_signals=['vix','fed_rate','margin_debt','market_dip','sector'])
        else:
            from dataclasses import replace
            result = replace(cached, state='incomplete')
        prefix = '⚠️ Global assessment refresh failed; showing available cached data.\n\n'
    delivered = _deliver_report(result, prefix=prefix, now=now, preclose=sessions)
    if delivered:
        with _lock:
            _state.last_result = result
            if is_alerting(result):
                _state.alerting = True
    return delivered


def _start_personal():
    global _personal_service
    if _personal_service is not None and not _personal_service.stopped.is_set():
        return
    if os.environ.get('PRECLOSE_ENABLED', '1') == '0':
        return
    _personal_service = PrecloseService(_get_portfolio_store(), _deliver_preclose, lock=_notification_lock)
    _personal_service.start()


def stop():
    if _personal_service is not None:
        _personal_service.stop()


def _cache_ages() -> list[tuple[str, datetime | None]]:
    return [(name, freshness.last_modified(path)) for name, path in _FRESHNESS_FILES.items()]


def _signal_report(result: BuySignal, heading: str = "", prefix: str = "") -> Report:
    report = render.signal_report(result, _cache_ages(), datetime.now(timezone.utc))
    if heading or prefix:
        report.messages[0].text = heading + prefix + report.messages[0].text
    try:
        token = _snapshot_store.save(result, datetime.now(timezone.utc))
        report.messages[0].keyboard = views.keyboard(token)
    except (OSError, ValueError):
        report.messages[0].text += "\nChart buttons unavailable: report snapshot could not be saved."
    return report


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
    return 0 <= report_hour <= now_ct.hour and last_report_date != now_ct.date()


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

    now_et = now.astimezone(market_hours.ET)
    now_ct = now.astimezone(market_hours.CT)
    with _lock:
        due = report_due(now_ct, _state.last_report_date, config.DAILY_REPORT_HOUR_CT)
        cached_result = _state.last_result
    if due:
        # The tick's result predates any refresh, and the macro caches may be hours old or
        # behind a failed scrape: re-scrape everything, then report on what that produced.
        # Its failures are journaled and sent below, right after the report.
        runner.refresh_macro(force=True, now=now_et)
        reported = report_result(None, error, cached_result, compute_signal)
        if reported is not None:
            prefix = (
                f"⚠️ latest tick failed ({render.esc(str(error))}); showing last cached data\n\n"
                if error is not None
                else ""
            )
            delivered = _deliver_report(reported, "📅 <b>End of day</b>\n\n", prefix, now=now)
            if delivered:
                with _lock:
                    _state.last_report_date = now_ct.date()
                    _state.last_result = reported
            result = reported

    # The journal owns the dedup (once per collector per day), so there's no state here to
    # keep in sync with it. Each failure is marked only once it has actually been delivered,
    # so a Telegram outage delays the notification rather than swallowing it.
    journal = runner.macro_journal()
    for name, failure in journal.unreported_failures(now_et):
        if transport.send(render.refresh_failures([(name, failure)])):
            journal.mark_reported(name, now_et)

    if result is None:
        return

    with _notification_lock:
        alerting = is_alerting(result)
        with _lock:
            was_alerting = _state.alerting

        if alerting and not was_alerting:
            reasons = trigger_reasons(result)
            if not due:
                delivered = _deliver_report(result, f"🔔 <b>Watch trigger active</b> ({render.esc(reasons)})\n\n", now=now)
            else:
                # The full report already carries this tick's entry conditions.
                delivered = _state.last_report_date == now_ct.date()
            if delivered:
                with _lock:
                    _state.alerting = True
        elif was_alerting and not alerting and not (
            any(s.metrics.get('stale') for s in result.subsignals if s.name in ('vix', 'market_dip'))
            or set(result.missing_signals) & {'vix', 'market_dip'}
        ):
            if _deliver_report(result, "<b>Watch trigger cleared (VIX/SPY)</b>\n\n", now=now):
                with _lock:
                    _state.alerting = False


def _single_signal(score_fn) -> str | Report:
    try:
        with buy_signal.SIGNAL_LOCK:
            s = score_fn()
    except Exception as exc:
        return f"⚠️ failed to compute ({render.esc(type(exc).__name__)}: {render.esc(str(exc))})"
    now = datetime.now(timezone.utc)
    if s.name == 'trend':
        return f"<b>SPY trend</b> · {render.esc(s.detail.replace(' | ', ' · '))}\nData {views.stamp(s.observed_at)}\nSent {views.stamp(now)}"
    result = BuySignal(0, 'none', int(s.passes), [s])
    store = _snapshot_store
    try:
        token = store.save(result, now)
    except (OSError, ValueError):
        return views.panel_caption(s, s.name, now.isoformat())
    manifest = store.load_views(token)
    item = manifest['views'].get(s.name)
    if item is None:
        return views.panel_caption(s, s.name, now.isoformat())
    key = item['chart_key']
    png = store.chart_bytes(token, key) if key else None
    def delivered(panel):
        file_id = panel.pop('file_id', None)
        store.remember_media(token, key, file_id)
        panel['chart_key'] = key if panel.get('photo') else None
        store.set_panel(token, panel)
    return Report([ReportMessage(
        item['caption'], key=s.name, photo=png,
        keyboard=item['keyboard'], panel=True, on_delivery=delivered,
    )])


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


def _refresh() -> Report:
    with force_refreshes():
        with buy_signal.SIGNAL_LOCK:
            failed = runner.refresh_macro(force=True)
        result = compute_signal(allow_refresh=True)
    return _signal_report(result, prefix=render.refresh_failures(failed) + "\n\n" if failed else "")


def handle_message(text: str) -> str | Report | None:
    text = text.strip()
    if text.lower().startswith(('/buy', 'bought ', 'buy ', '/voidtrade', '/trades', '/holdings')):
        reply = portfolio_commands.handle(text, _get_portfolio_store(), source_key=transport.current_message_key())
        if reply is not None:
            return reply
    if text == '/portfolio':
        store = _get_portfolio_store()
        return personal_presentation.portfolio_message(personal_quotes.all_quotes(store), store.holdings())
    # VIX/market_dip refetch on every call; prefer the cache while the market's closed (e.g.
    # weekends) instead of paying the fetch + anti-bot jitter delay for data that can't have moved.
    open_now = market_open()
    if text == "/signal":
        return _signal_report(compute_signal(allow_refresh=open_now))
    if text == "/vix":
        return _single_signal(lambda: vix_signal.score(allow_refresh=open_now))
    if text == "/dip":
        return _single_signal(lambda: market_signal.score(allow_refresh=open_now))
    if text == "/trend":
        return _single_signal(lambda: trend_signal.from_market(market_signal.score(allow_refresh=open_now)))
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


def handle_callback(query) -> None:
    interactive.handle_callback(query, _snapshot_store)


def start() -> None:
    """Start polling for commands in a background thread."""
    transport.start(handle_message, handle_callback)
    try:
        _start_personal()
    except Exception:
        log.exception('Personal pre-close service could not start')
        transport.notice('Personal pre-close updates could not start. Existing market reports remain active.')
