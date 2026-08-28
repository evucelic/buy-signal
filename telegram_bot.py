"""Long-polling Telegram bot: per-signal + aggregate commands, plus threshold-crossing pushes.

Plain synchronous requests calls to the Bot API, matching the rest of the codebase's style:
no async framework needed for a handful of commands and a push. Runs in a background thread
started by start(); the caller (bot.py) drives the actual tick loop (runner.run_forever()) and
feeds it results via handle_tick().
"""

import html
import os
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone

import requests
from dotenv import load_dotenv

import analyzer
import config
import runner
from collectors import freshness
from signals import buy_signal, margin_signal, market_signal, rate_signal, sector_signal, vix_signal, yield_curve_signal
from signals.base import SubSignal, format_table
from signals.buy_signal import BuySignal, compute_signal

load_dotenv()

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
API = f"https://api.telegram.org/bot{BOT_TOKEN}"

_POLL_TIMEOUT = 30  # seconds, Telegram long-poll
_RETRY_DELAY_SEC = 5

# Per-signal commands: score() called directly (not compute_signal()) so a broken collector
# can't block checking the others. /vix and /dip are handled separately in _handle_message
# since they take an allow_refresh flag; these three don't refetch on every call.
_SIGNAL_COMMANDS = {
    "/fedrate": rate_signal.score,
    "/margin": margin_signal.score,
    "/sector": sector_signal.score,
    "/curve": yield_curve_signal.score,
}

# Presentation only: signal logic/state strings (signals/*.py) are untouched by any of this.
_ALERT_LABELS = {
    "none": ("⚪", "No buy signal"),
    "soft": ("🟡", "Soft signal"),
    "strong": ("🟢", "STRONG BUY SIGNAL"),
}

_SIGNAL_META = {
    "vix": ("🌡️", "VIX"),
    "fed_rate": ("🏦", "Fed Rate"),
    "margin_debt": ("💳", "Margin Debt"),
    "market_dip": ("📊", "Market Dip"),
    "sector": ("🏭", "Leading Industries"),
    "yield_curve": ("〽️", "Yield Curve"),
}

_STATE_LABELS = {
    ("vix", "strong"): "🔴 High fear",
    ("vix", "soft"): "🟡 Elevated",
    ("vix", "none"): "⚪ Calm",
    ("fed_rate", "hiking"): "📈 Hiking",
    ("fed_rate", "cutting"): "📉 Cutting",
    ("fed_rate", "no_change"): "➡️ No change",
    ("fed_rate", "flat"): "🤔 Mixed",
    ("margin_debt", "deleveraging"): "📉 Deleveraging",
    ("margin_debt", "leveraging"): "📈 Leveraging up",
    ("market_dip", "dip"): "🔻 Dip",
    ("market_dip", "flat"): "➡️ Flat",
    ("market_dip", "growth"): "📈 Growth",
    ("sector", "growing"): "📈 Growing",
    ("sector", "flat"): "➡️ Flat",
    ("yield_curve", "steep"): "📈 Steep (supportive)",
    ("yield_curve", "flat"): "➡️ Flat/normal",
    ("yield_curve", "inverted"): "⚠️ Inverted",
    ("yield_curve", "deep_inversion"): "🚨 Deep inversion",
}

_COMMANDS = [
    {"command": "signal", "description": "Full current buy signal (all sub-signals)"},
    {"command": "vix", "description": "VIX level"},
    {"command": "fedrate", "description": "Fed rate trajectory (CME FedWatch)"},
    {"command": "margin", "description": "FINRA margin debt (deleveraging)"},
    {"command": "dip", "description": "SPY/NASDAQ/DOW % change (dip watch)"},
    {"command": "sector", "description": "Leading industries earnings outlook"},
    {"command": "curve", "description": "10y-3m yield curve spread (advisory)"},
    {"command": "whattobuy", "description": "Segment valuations: S&P 500 vs world small cap vs Europe"},
    {"command": "refresh", "description": "Force a fresh fetch of all data (bypass cache)"},
    {"command": "status", "description": "Runner uptime and health"},
    {"command": "help", "description": "List available commands"},
]

_HELP_TEXT = (
    "Commands:\n"
    "/signal: full current buy signal\n"
    "/vix: VIX level\n"
    "/fedrate: Fed rate trajectory\n"
    "/margin: FINRA margin debt\n"
    "/dip: SPY/NASDAQ/DOW % change\n"
    "/sector: leading industries earnings outlook\n"
    "/curve: 10y-3m yield curve spread (advisory)\n"
    "/whattobuy: segment valuations (S&P 500 / world small cap / Europe)\n"
    "/refresh: force a fresh fetch of all data (bypass cache)\n"
    "/status: runner uptime and health"
)

# Cache files backing each signal, for the "data as of" freshness table appended to every
# _format_signal() output.
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


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _send(text: str) -> None:
    try:
        resp = requests.post(
            f"{API}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
        print(f"Telegram send failed ({type(exc).__name__}: {exc})")


def _send_photo(png: bytes, caption: str) -> None:
    try:
        resp = requests.post(
            f"{API}/sendPhoto",
            data={"chat_id": CHAT_ID, "caption": caption},
            files={"photo": ("chart.png", png, "image/png")},
            timeout=20,
        )
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
        print(f"Telegram sendPhoto failed ({type(exc).__name__}: {exc})")


def _is_alerting(result: BuySignal) -> bool:
    by_name = {s.name: s for s in result.subsignals}
    vix = by_name.get("vix")
    dip = by_name.get("market_dip")
    return (vix is not None and vix.passes) or (dip is not None and dip.passes) or result.state in ("soft", "strong")


def _trigger_reasons(result: BuySignal) -> str:
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


def _bullets(detail: str) -> str:
    """Split a ' | '-joined detail string into one escaped bullet per fragment."""
    return "\n".join(f"• {_esc(part.strip())}" for part in detail.split(" | "))


def _format_subsignal(s: SubSignal) -> str:
    icon, display_name = _SIGNAL_META.get(s.name, ("•", s.name))
    mark = "ℹ️" if s.advisory else ("✅" if s.passes else "❌")
    state_label = _STATE_LABELS.get((s.name, s.state), s.state)
    header = f"{mark} {icon} <b>{_esc(display_name)}</b>: {_esc(state_label)}"
    body = f"<pre>{_esc(s.table)}</pre>" if s.table else _bullets(s.detail)
    if s.footer:
        body += f"\n{_esc(s.footer)}"
    return f"{header}\n{body}"


def _humanize_age(updated_at: datetime | None) -> str:
    if updated_at is None:
        return "no cache"
    secs = (datetime.now(timezone.utc) - updated_at).total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def _format_freshness() -> str:
    rows = [[name, _humanize_age(freshness.last_modified(path))] for name, path in _FRESHNESS_FILES.items()]
    return "<pre>" + _esc(format_table(["signal", "updated"], rows)) + "</pre>"


def _format_signal(result: BuySignal) -> str:
    emoji, label = _ALERT_LABELS.get(result.state, ("", result.state.upper()))
    header = f"{emoji} <b>{_esc(label)}</b> ({result.passing_count}/{result.required_count} conditions met)"
    blocks = [_format_subsignal(s) for s in result.subsignals]
    if result.missing_signals:
        blocks.append(f"⚠️ missing: {_esc(', '.join(result.missing_signals))}")
    blocks.append(f"🕓 <b>Data as of</b>\n{_format_freshness()}")
    return "\n\n".join([header, *blocks])


def _fmt(value: float | None, spec: str = ".1f") -> str:
    return "n/a" if value is None else format(value, spec)


# Short row labels for the monospace table only (chart legends/notes keep the full config label).
_TABLE_LABELS = {
    "sp500": "S&P 500",
    "world_small": "Small Cap",
    "world_small_value": "SC Value",
    "europe": "Europe",
}

_RATE_STATE_LABELS = {"no_change": "no change"}  # everything else already reads fine as-is


def _format_opportunity(opp: analyzer.Opportunity) -> str:
    """Everything-in-one-place valuation facts; no interpretation beyond the matrix verdict."""
    rows = [
        [
            _TABLE_LABELS.get(s.name, s.label),
            _fmt(s.fwd_pe),
            _fmt(s.trailing_pe),
            _fmt(s.ratio_vs_spx, ".2f"),
            _fmt(s.fwd_z, "+.1f"),
            _fmt(s.rel_z, "+.1f"),
        ]
        for s in opp.segments
    ]
    table = "<pre>" + _esc(format_table(["Segment", "Fwd", "Trail", "vsUS", "z", "relZ"], rows)) + "</pre>"

    lines = []
    if opp.small_cap_band is not None:
        lines.append(f"Small-cap band: {opp.small_cap_band}")

    rate = opp.rate
    if rate.horizons:
        sequence = " → ".join(_RATE_STATE_LABELS.get(h.state, h.state) for h in rate.horizons)
        support = "yes" if rate.rate_support else "no"
        lines.append(
            f"FedWatch: {rate.consecutive_easing}/{len(rate.horizons)} easing "
            f"({sequence}, rate support: {support}, need ≥{config.SMALL_CAP_EASING_OBS})"
        )

    lines.append(f"Verdict: {opp.verdict}")
    if opp.buy_signal_state is not None:
        label = _ALERT_LABELS.get(opp.buy_signal_state, ("", opp.buy_signal_state))[1]
        lines.append(f"Buy signal (context): {label}")
    facts = "\n".join(f"• {_esc(line)}" for line in lines)

    asofs = {s.asof for s in opp.segments if s.asof}
    if len(asofs) == 1:
        footer = f"🕓 MSCI data as of {_esc(asofs.pop())} (updates monthly)"
    elif asofs:
        per_segment = ", ".join(f"{s.label} {s.asof}" for s in opp.segments if s.asof)
        footer = f"🕓 MSCI data as of: {_esc(per_segment)} (updates monthly)"
    else:
        footer = ""
    notes = "\n".join(f"• {_esc(note)}" for note in opp.notes)

    blocks = ["🧭 <b>What to buy: segment valuations</b>", table, facts]
    if footer:
        blocks.append(footer)
    if notes:
        blocks.append(f"⚠️ <b>Notes</b>\n{notes}")
    return "\n\n".join(blocks)


def _format_status() -> str:
    with _lock:
        uptime = datetime.now(timezone.utc) - _state.start_time
        last_tick_at = _state.last_tick_at
        last_ok = _state.last_ok
        last_error = _state.last_error
        result = _state.last_result

    last_tick = "never" if last_tick_at is None else f"{last_tick_at:%Y-%m-%d %H:%M UTC}"
    tick_status = "ok" if last_ok else f"FAILED ({last_error})"
    alert = _ALERT_LABELS.get(result.state, ("", "unknown"))[1] if result is not None else "unknown"

    lines = [
        f"uptime      {uptime}",
        f"last tick   {last_tick} ({tick_status})",
        f"alert       {alert}",
    ]
    return "<pre>" + _esc("\n".join(lines)) + "</pre>"


def notify_started() -> None:
    _send("🟢 Runner started")


def notify_stopped() -> None:
    _send("🔴 Runner stopped")


def _report_due(now_ct: datetime, last_report_date: date | None, report_hour: int) -> bool:
    """Whether the daily report is due: the report hour, and not already sent this CT date."""
    return now_ct.hour == report_hour and last_report_date != now_ct.date()


def _report_result(
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

    now_ct = now.astimezone(runner.CT)
    with _lock:
        due = _report_due(now_ct, _state.last_report_date, config.DAILY_REPORT_HOUR_CT)
        if due:
            _state.last_report_date = now_ct.date()
        cached_result = _state.last_result
    if due:
        report_result = _report_result(result, error, cached_result, compute_signal)
        if report_result is not None:
            prefix = f"⚠️ latest tick failed ({_esc(str(error))}); showing last cached data\n\n" if error is not None else ""
            _send(f"📅 <b>End of day</b>\n\n{prefix}{_format_signal(report_result)}")

    if result is None:
        return

    alerting = _is_alerting(result)
    with _lock:
        was_alerting = _state.alerting
        _state.alerting = alerting

    if alerting and not was_alerting:
        reasons = _trigger_reasons(result)
        _send(f"🔔 <b>Signal active</b> ({_esc(reasons)})\n\n{_format_signal(result)}")
    elif was_alerting and not alerting:
        _send("Signal cleared, back to normal.")


def _format_single(score_fn) -> str:
    try:
        with buy_signal.SIGNAL_LOCK:
            s = score_fn()
    except Exception as exc:
        return f"⚠️ failed to compute ({_esc(type(exc).__name__)}: {_esc(str(exc))})"
    return _format_subsignal(s)


def _market_open() -> bool:
    return runner.market_session() != runner.CLOSED


def _handle_message(text: str) -> str | None:
    text = text.strip()
    # VIX/market_dip refetch on every call; prefer the cache while the market's closed (e.g.
    # weekends) instead of paying the fetch + anti-bot jitter delay for data that can't have moved.
    market_open = _market_open()
    if text == "/signal":
        return _format_signal(compute_signal(allow_refresh=market_open))
    if text == "/vix":
        return _format_single(lambda: vix_signal.score(allow_refresh=market_open))
    if text == "/dip":
        return _format_single(lambda: market_signal.score(allow_refresh=market_open))
    if text in _SIGNAL_COMMANDS:
        return _format_single(_SIGNAL_COMMANDS[text])
    if text == "/whattobuy":
        from collectors import valuations

        failed = []
        with buy_signal.SIGNAL_LOCK:
            if valuations.should_refresh():
                error = valuations.update_valuations_data()
                if error:
                    failed.append(("valuations", error))
            opp = analyzer.analyze()
        with _lock:
            last_result = _state.last_result
        opp.buy_signal_state = last_result.state if last_result is not None else None
        try:
            _send_photo(analyzer.render_chart(opp), "Segment valuations")
        except Exception as exc:
            opp.notes.append(f"chart rendering failed ({type(exc).__name__}: {exc})")
        reply = _format_opportunity(opp)
        if failed:
            lines = "\n".join(f"• {_esc(name)}: {_esc(error)}" for name, error in failed)
            reply = f"⚠️ <b>Refresh failed</b>\n{lines}\n\n{reply}"
        return reply
    if text == "/refresh":
        with buy_signal.SIGNAL_LOCK:
            failed = runner.refresh_macro(force=True)
        reply = _format_signal(compute_signal(allow_refresh=True))
        if failed:
            lines = "\n".join(f"• {_esc(name)}: {_esc(error)}" for name, error in failed)
            reply = f"⚠️ <b>Refresh failed</b>\n{lines}\n\n{reply}"
        return reply
    if text == "/status":
        return _format_status()
    if text in ("/start", "/help"):
        return _HELP_TEXT
    return "Unrecognized command, try /help."


def _poll_loop() -> None:
    offset = None
    while True:
        try:
            resp = requests.get(
                f"{API}/getUpdates",
                params={"timeout": _POLL_TIMEOUT, "offset": offset},
                timeout=_POLL_TIMEOUT + 10,
            )
            resp.raise_for_status()
            for update in resp.json()["result"]:
                offset = update["update_id"] + 1
                message = update.get("message")
                if not message or str(message["chat"]["id"]) != str(CHAT_ID):
                    continue
                reply = _handle_message(message.get("text", ""))
                if reply is not None:
                    _send(reply)
        except requests.exceptions.RequestException as exc:
            print(f"Telegram poll failed ({type(exc).__name__}: {exc}); retrying.")
            time.sleep(_RETRY_DELAY_SEC)


def start() -> None:
    """Register the bot's command menu and start polling for updates in a background thread."""
    try:
        resp = requests.post(f"{API}/setMyCommands", json={"commands": _COMMANDS}, timeout=10)
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
        print(f"Telegram setMyCommands failed ({type(exc).__name__}: {exc}); continuing anyway.")
    threading.Thread(target=_poll_loop, daemon=True).start()
