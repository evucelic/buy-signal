"""What the bot knows and decides: tick state, the daily report, pushes and command dispatch.

It reads what the renderer needs and hands the strings to the transport.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone

from buysignal import config
from buysignal.resources import force_refreshes
from buysignal.portfolio import plan as portfolio_plan, commands as portfolio_commands, quotes as personal_quotes, sessions as personal_sessions, presentation as personal_presentation
from buysignal.portfolio.instruments import resolve as resolve_instrument
from buysignal.portfolio.journal import PortfolioStore
from buysignal.portfolio.service import PrecloseService
import os
import logging
from buysignal.analysis import chart, checklist_log, opportunity
from buysignal.collectors import freshness
from buysignal.schedule import market_hours, runner
from buysignal.signals import buy_signal, credit_signal, margin_signal, market_signal, rate_signal, sector_signal, vix_signal, yield_curve_signal
from buysignal.signals.buy_signal import BuySignal, assess, compute_signal
from buysignal.telegram_bot import interactive, pushes, render, transport, views
from buysignal.telegram_bot.snapshots import SnapshotStore
from buysignal.telegram_bot.messages import Report, ReportMessage

# score() directly, not compute_signal(), so one broken collector can't block the others.
# /vix and /dip take allow_refresh and are handled in handle_message.
_SIGNAL_COMMANDS = {
    "/fedrate": rate_signal.score,
    "/margin": margin_signal.score,
    "/sector": sector_signal.score,
    "/curve": yield_curve_signal.score,
    "/credit": credit_signal.score,
}

# Cache files behind the overview's "Data as of" block.
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
    seen: pushes.Seen = pushes.Seen()    # what the pushes have told the owner so far
    last_report_date: date | None = None


_state = _State(start_time=datetime.now(timezone.utc))
_lock = threading.Lock()
_snapshot_store = SnapshotStore(background=True)
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
            result = assess([], [*buy_signal.REQUIRED_SIGNALS, *buy_signal.CONTEXT_SIGNALS])
        else:
            from dataclasses import replace
            result = replace(cached, state=buy_signal.INCOMPLETE)
        prefix = '⚠️ Global assessment refresh failed; showing available cached data.\n\n'
    with _notification_lock:
        with _lock:
            seen = _state.seen
        heading, after = pushes.decide(seen, result, now.astimezone(market_hours.CT).date())
        delivered = _deliver_report(result, heading, prefix=prefix, now=now, preclose=sessions)
        if delivered:
            with _lock:
                _state.last_result = result
                _state.seen = after
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


def _etf_closes(now: datetime) -> dict:
    """{symbol: (daily close, its date, trading currency)} for the owned listings, freshly downloaded."""
    return personal_quotes.session_closes(_get_portfolio_store(), now=now)


def _log_session(result: BuySignal, now: datetime) -> None:
    """Append today's checklist to the log on an NYSE session day; never fails the report."""
    # The report is due from 20:00 CT, and its Central date is the session's even after
    # midnight in New York (a late or retried report at 23:00 CT is 00:00 ET).
    session = now.astimezone(market_hours.CT).date()
    if not market_hours.is_session_day(session):
        return
    try:
        try:
            closes = _etf_closes(now)
        except Exception:
            log.exception("ETF closes unavailable for the checklist log")
            closes = {}
        checklist_log.append(checklist_log.row(result, session.isoformat(), now, closes))
    except Exception:
        log.exception("Checklist log not written")


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
            with _notification_lock:
                with _lock:
                    seen = _state.seen
                heading, after = pushes.decide(seen, reported, now_ct.date())
                if _deliver_report(reported, "📅 <b>End of day</b>\n\n" + heading, prefix, now=now):
                    with _lock:
                        _state.last_report_date = now_ct.date()
                        _state.last_result = reported
                        _state.seen = after
            _log_session(reported, now)
            result = reported

    # The journal owns the dedup (once per collector per day), so there's no state here to
    # keep in sync with it. Each failure is marked only once it has actually been delivered,
    # so a Telegram outage delays the notification rather than swallowing it.
    journal = runner.macro_journal()
    for name, failure in journal.unreported_failures(now_et):
        if transport.send(render.refresh_failures([(name, failure)])):
            journal.mark_reported(name, now_et)

    with _lock:
        reported_today = due and _state.last_report_date == now_ct.date()
    if result is None or reported_today:
        return  # the daily report just sent carried this reading's pushes

    with _notification_lock:
        with _lock:
            seen = _state.seen
        heading, after = pushes.decide(seen, result, now_ct.date())
        if not heading or _deliver_report(result, heading, now=now):
            with _lock:
                _state.seen = after


def _single_signal(score_fn) -> str | Report:
    try:
        with buy_signal.SIGNAL_LOCK:
            s = score_fn()
    except Exception as exc:
        return f"⚠️ failed to compute ({render.esc(type(exc).__name__)}: {render.esc(str(exc))})"
    now = datetime.now(timezone.utc)
    result = assess([s])
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
            read=_state.last_result.label if _state.last_result is not None else None,
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
    store = _get_portfolio_store()
    try:
        # Each ETF's own distance below its high; a failed listing is simply left blank.
        dips = personal_quotes.dips(store)
    except Exception:
        log.exception('ETF dip context unavailable')
        dips = {}
    plan, notes = None, []
    try:
        holdings = store.holdings()
        if holdings:
            rate, fx_date, fx_stale = personal_quotes.eurusd(store)
            plan = portfolio_plan.allocation(holdings, personal_quotes.all_quotes(store), rate, store.targets())
            if plan is None:
                notes.append("positions cannot be valued: a price or the EUR/USD rate is missing")
            else:
                plan["fx_date"] = fx_date
                if fx_stale:
                    notes.append("EUR/USD refresh failed; using the last rate")
                if plan["destination"] is None:
                    notes.append("no mix set: /target VUAA 60 VWCG 20 AVWS 20")
    except Exception:
        log.exception('Allocation unavailable')
        notes.append("allocation unavailable")
    try:
        transport.send_photo(chart.render_chart(opp, dips=dips), "Forward P/E by market")
    except Exception as exc:
        opp.notes.append(f"chart rendering failed ({type(exc).__name__}: {exc})")

    real = None
    try:
        rate, observed, old = personal_quotes.real_yield(store)
        if rate is not None:
            real = (rate, observed)
        if old:
            notes.append("real yield refresh failed" + ("; using the last value" if rate is not None else ""))
    except Exception:
        log.exception('Real yield unavailable')
    reply = render.opportunity_message(opp, dips, plan, notes, real)
    return f"{render.refresh_failures(failed)}\n\n{reply}" if failed else reply


def _cash(text: str) -> str:
    match = re.fullmatch(r'/cash\s+(\d+(?:\.\d+)?)(?:\s*(?:eur|€))?', text.strip(), re.I)
    if not match:
        return 'Use: /cash 500 (euros you want to invest now)'
    store = _get_portfolio_store()
    targets = store.targets()
    if not targets:
        return 'Set your mix first: /target VUAA 60 VWCG 20 AVWS 20'
    rate, _, fx_stale = personal_quotes.eurusd(store)
    fees = store.fees()
    result = portfolio_plan.purchase_plan(float(match.group(1)), store.holdings(), personal_quotes.all_quotes(store), rate,
                                          targets, fees, fees.get('FX'))
    if result is None:
        return '⚠️ A price or the EUR/USD rate is missing; no plan.'
    overlaps = {}
    if result['chosen']:
        now = _utc_now()
        for order in result['chosen']['orders']:
            instrument = resolve_instrument(order['symbol'])
            if instrument.us_hours:
                overlaps[instrument.symbol] = personal_sessions.us_overlap(now, instrument.exchange)
    reply = personal_presentation.cash_message(result, overlaps)
    notes = []
    if fx_stale:
        notes.append('⚠️ EUR/USD refresh failed; using the last rate')
    return '\n'.join([reply, *notes])


def _entries() -> str:
    store = _get_portfolio_store()
    purchases = store.purchases(limit=1000)
    closes, stale = personal_quotes.daily_closes(store) if purchases else ({}, [])
    return personal_presentation.entries_message(portfolio_plan.scorecard(purchases, closes), stale)


def _refresh() -> Report:
    with force_refreshes():
        with buy_signal.SIGNAL_LOCK:
            failed = runner.refresh_macro(force=True)
        result = compute_signal(allow_refresh=True)
    return _signal_report(result, prefix=render.refresh_failures(failed) + "\n\n" if failed else "")


def handle_message(text: str) -> str | Report | None:
    text = text.strip()
    if text.lower().startswith(('/buy', 'bought ', 'buy ', '/voidtrade', '/trades', '/holdings', '/target', '/fees')):
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
    if text in ("/dip", "/trend"):
        # /trend is an alias: trend sits in each index's panel.
        return _single_signal(lambda: market_signal.score(allow_refresh=open_now))
    if text in _SIGNAL_COMMANDS:
        return _single_signal(_SIGNAL_COMMANDS[text])
    if text == "/whattobuy":
        return _whattobuy()
    if text.lower().startswith("/cash"):
        return _cash(text)
    if text == "/entries":
        return _entries()
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
