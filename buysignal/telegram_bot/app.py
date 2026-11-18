"""What the bot decides: tick state, the daily report, pushes and command dispatch."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone

from buysignal import config
from buysignal.resources import force_refreshes
from buysignal.broker import sync as broker_sync
from buysignal.portfolio import panels as portfolio_panels, plan as portfolio_plan, commands as portfolio_commands, quotes as personal_quotes, sessions as personal_sessions, presentation as personal_presentation
from buysignal.portfolio.instruments import CASH, resolve as resolve_instrument
from buysignal.portfolio.store import PortfolioStore
from buysignal.portfolio.service import PrecloseService
import os
import logging
from buysignal.analysis import checklist_log
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
    """What the daily report shows: this tick's result, a fresh compute after an idle tick, or the cache after a failed one."""
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
        # Re-scrape before the report; failures are journaled and sent after it.
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
                if _deliver_report(reported, "<b>End of day</b>\n\n" + heading, prefix, now=now):
                    with _lock:
                        _state.last_report_date = now_ct.date()
                        _state.last_result = reported
                        _state.seen = after
            _log_session(reported, now)
            result = reported

    # Marked reported only once delivered, so an outage delays the notice instead of losing it.
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
    store.mark_self_panel(token)
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
    text = render.status_message(status, statuses, datetime.now(timezone.utc))
    if broker_sync.configured():
        statement = _get_portfolio_store().account().get('statement_to', 'none yet')
        left = broker_sync.token_days_left(_utc_now().date())
        text += f"\nIBKR statement {render.esc(statement)}" + ("" if left is None else f" · token {left} days left")
    return text


_NOT_CONNECTED = ("<b>IBKR not connected</b>\nSet IBKR_FLEX_TOKEN and IBKR_FLEX_QUERY_ID in .env "
                  "(README → Interactive Brokers). Meanwhile: /whattobuy 500 plans 500 € against your target.")


def _read_account(store) -> list[str]:
    """Read IBKR now if the stored statement is behind; the failure, if any, as a note."""
    if not broker_sync.configured() or not broker_sync.demands_refresh(store):
        return []
    with buy_signal.SIGNAL_LOCK:
        failed = runner.refresh_macro(force=True, only=(broker_sync.NAME,))
    return [f"IBKR read failed ({error.splitlines()[0][:120]}); showing the statement through "
            f"{store.account().get('statement_to', 'none')}" for _, error in failed]


def _valuation(store) -> tuple:
    """(CAPE history, [(ETF, forward P/E)], (TIPS yield, date), sources, notes) for the Valuation tab.

    Only HTTP sources refresh here; the MSCI cache needs a browser and is left to the scheduled refresh.
    """
    from buysignal.collectors import cape, valuations
    with buy_signal.SIGNAL_LOCK:
        failed = runner.refresh_macro(only=("cape",))  # at most weekly, Shiller updates monthly
    notes = [f"CAPE refresh failed ({error.splitlines()[0][:120]})" for _, error in failed]
    try:
        frame = cape.history()
    except (OSError, ValueError):
        frame = None
    sources, sleeves = [], []
    try:
        sleeves, asof = valuations.latest_forward_pe()
        if asof:
            sources.append(f"MSCI {asof}")
    except (OSError, ValueError, KeyError):
        log.exception('MSCI valuations unavailable')
    real = None
    try:
        rate, observed, old = personal_quotes.real_yield(store)
        real = (rate, observed) if rate is not None else None
        if real:
            sources.append(f"TIPS {observed}")
        if old:
            notes.append("real yield refresh failed" + ("; using the last value" if rate is not None else ""))
    except Exception:
        log.exception('Real yield unavailable')
    return frame, sleeves, real, sources, notes


def _account(store, notes) -> portfolio_panels.Account:
    left = broker_sync.token_days_left(_utc_now().date())
    if left is not None and left <= broker_sync.TOKEN_WARNING_DAYS:
        notes.append(f"IBKR token expires in {left} days: generate a new one and update IBKR_FLEX_TOKEN")
    quotes = personal_quotes.all_quotes(store)
    eurusd, _, fx_stale = personal_quotes.eurusd(store)
    if fx_stale:
        notes.append("EUR/USD refresh failed; using the last rate")
    positions = store.positions()
    try:
        closes, stale = personal_quotes.daily_closes(store)
        if stale:
            notes.append("daily prices from an older copy: " + ", ".join(stale))
    except Exception:
        log.exception('Daily closes unavailable')
        closes = {}
    history, history_stale = personal_quotes.eurusd_history(store)
    if history_stale:
        notes.append("EUR/USD history from an older copy" if history is not None else "EUR/USD history unavailable")
    benchmarks, bench_stale = personal_quotes.benchmarks(store)
    if bench_stale:
        notes.append("benchmark prices from an older copy or missing: " + ", ".join(bench_stale))
    cape, sleeves, real, sources, more = _valuation(store)
    return portfolio_panels.Account(
        positions=positions, holdings=store.holdings(), cash=store.cash(), nav=store.nav(), deposits=store.deposits(),
        trades=store.trades(), meta=store.account(), targets=store.targets(), prices=portfolio_plan.prices(quotes, positions),
        quotes=quotes, eurusd=eurusd, closes=closes, eurusd_history=history, benchmarks=benchmarks,
        history_complete=store.history_complete(), cape=cape, sleeves=sleeves, real_yield=real, sources=sources,
        today=_utc_now().date(), notes=[*notes, *more],
    )


def _portfolio() -> str | Report:
    store = _get_portfolio_store()
    if not broker_sync.configured() and not store.positions():
        return _NOT_CONNECTED
    account = _account(store, _read_account(store))
    now = _utc_now()
    tabs = portfolio_panels.build(account)
    try:
        token = _snapshot_store.save_panels(tabs, portfolio_panels.keyboard, now)
    except (OSError, ValueError):
        return tabs[portfolio_panels.FIRST][0] + "\nTab buttons unavailable: the panel could not be saved."
    item = _snapshot_store.load_views(token)["views"][portfolio_panels.FIRST]
    key = item["chart_key"]
    png = _snapshot_store.chart_bytes(token, key) if key else None

    def delivered(panel):
        _snapshot_store.remember_media(token, key, panel.pop("file_id", None))
        panel.update(chart_key=key if panel.get("photo") else None, view=portfolio_panels.FIRST)
        _snapshot_store.set_panel(token, panel)
    return Report([ReportMessage(item["caption"], key="portfolio", photo=png, keyboard=item["keyboard"],
                                 panel=True, on_delivery=delivered)])


def _whattobuy(text: str) -> str:
    match = re.fullmatch(r'/whattobuy(?:\s+(\d+(?:\.\d+)?)\s*(?:eur|€)?)?', text.strip(), re.I)
    if not match:
        return 'Use: /whattobuy for the cash at IBKR, or /whattobuy 500 for 500 €'
    store = _get_portfolio_store()
    targets = store.targets()
    if not targets:
        return 'Set a target mix first: /target ' + 'VUAA 60 VWCG 20 AVWS 15 CASH 5'
    amount = float(match.group(1)) if match.group(1) else None
    if amount is None and not broker_sync.configured() and not store.cash():
        return _NOT_CONNECTED
    notes = _read_account(store) if amount is None else []
    quotes = personal_quotes.all_quotes(store)
    eurusd, _, fx_stale = personal_quotes.eurusd(store)
    if fx_stale:
        notes.append('EUR/USD refresh failed; using the last rate')
    fees = store.fees()
    if amount is None:
        cash, source = store.cash(), f"IBKR cash {store.account().get('statement_to', '')}".strip()
    else:
        # A typed amount is new money: the cash target applies to the account, not to it.
        cash, source = {"EUR": amount}, "for"
        targets = {s: w for s, w in targets.items() if s != CASH}
    result = portfolio_plan.purchase_plan(cash, store.holdings(), portfolio_plan.prices(quotes, store.positions()),
                                          eurusd, targets, fees, fees.get('FX'))
    if result is None:
        return '⚠️ A price or the EUR/USD rate is missing; no plan.'
    overlaps = {}
    if result['chosen']:
        now = _utc_now()
        for order in result['chosen']['orders']:
            instrument = resolve_instrument(order['symbol'])
            if instrument.us_hours and instrument.exchange:
                overlaps[instrument.symbol] = personal_sessions.us_overlap(now, instrument.exchange)
    return personal_presentation.orders_message(result, source=source, overlaps=overlaps, notes=notes)


def _refresh() -> Report:
    with force_refreshes():
        with buy_signal.SIGNAL_LOCK:
            failed = runner.refresh_macro(force=True)
        result = compute_signal(allow_refresh=True)
    return _signal_report(result, prefix=render.refresh_failures(failed) + "\n\n" if failed else "")


def handle_message(text: str) -> str | Report | None:
    text = text.strip()
    if text.lower().startswith(('/target', '/fees')):
        return portfolio_commands.handle(text, _get_portfolio_store())
    if text == '/portfolio':
        return _portfolio()
    # Outside market hours prefer the cache: the data cannot have moved.
    open_now = market_open()
    if text == "/signal":
        return _signal_report(compute_signal(allow_refresh=open_now))
    if text == "/vix":
        return _single_signal(lambda: vix_signal.score(allow_refresh=open_now))
    if text == "/dip":
        return _single_signal(lambda: market_signal.score(allow_refresh=open_now))
    if text in _SIGNAL_COMMANDS:
        return _single_signal(_SIGNAL_COMMANDS[text])
    if text.lower().startswith("/whattobuy"):
        return _whattobuy(text)
    if text == "/refresh":
        return _refresh()
    if text == "/status":
        return _status_message()
    if text in ("/start", "/help"):
        return render.HELP_TEXT
    return "Unrecognized command, try /help."


def handle_callback(query) -> None:
    message = query.get('message', {}).get('message_id')
    interactive.handle_callback(query, _snapshot_store, run=lambda job: transport.navigate(message, job))


def start() -> None:
    """Start polling for commands in a background thread."""
    transport.start(handle_message, handle_callback)
    try:
        _start_personal()
    except Exception:
        log.exception('Personal pre-close service could not start')
        transport.notice('Personal pre-close updates could not start. Existing market reports remain active.')
