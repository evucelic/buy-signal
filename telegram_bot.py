"""Long-polling Telegram bot: per-signal + aggregate commands, plus threshold-crossing pushes.

Plain synchronous requests calls to the Bot API, matching the rest of the codebase's style —
no async framework needed for a handful of commands and a push. Runs in a background thread
started by start(); the caller (bot.py) drives the actual tick loop (runner.run_forever()) and
feeds it results via handle_tick().
"""

import html
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

import runner
from signals import buy_signal, margin_signal, market_signal, rate_signal, sector_signal, vix_signal
from signals.base import SubSignal
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
}

# Presentation only — signal logic/state strings (signals/*.py) are untouched by any of this.
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
}

_STATE_LABELS = {
    ("vix", "strong"): "🔴 High fear",
    ("vix", "soft"): "🟡 Elevated",
    ("vix", "none"): "⚪ Calm",
    ("fed_rate", "hiking"): "📈 Hiking",
    ("fed_rate", "cutting"): "📉 Cutting",
    ("fed_rate", "flat"): "➡️ Flat",
    ("margin_debt", "deleveraging"): "📉 Deleveraging",
    ("margin_debt", "leveraging"): "📈 Leveraging up",
    ("market_dip", "dip"): "🔻 Dip",
    ("market_dip", "flat"): "➡️ Flat",
    ("market_dip", "growth"): "📈 Growth",
    ("sector", "growing"): "📈 Growing",
    ("sector", "flat"): "➡️ Flat",
}

_COMMANDS = [
    {"command": "signal", "description": "Full current buy signal (all sub-signals)"},
    {"command": "vix", "description": "VIX level"},
    {"command": "fedrate", "description": "Fed rate trajectory (CME FedWatch)"},
    {"command": "margin", "description": "FINRA margin debt (deleveraging)"},
    {"command": "dip", "description": "SPY/NASDAQ/DOW % change (dip watch)"},
    {"command": "sector", "description": "Leading industries earnings outlook"},
    {"command": "status", "description": "Runner uptime and health"},
    {"command": "help", "description": "List available commands"},
]

_HELP_TEXT = (
    "Commands:\n"
    "/signal — full current buy signal\n"
    "/vix — VIX level\n"
    "/fedrate — Fed rate trajectory\n"
    "/margin — FINRA margin debt\n"
    "/dip — SPY/NASDAQ/DOW % change\n"
    "/sector — leading industries earnings outlook\n"
    "/status — runner uptime and health"
)


@dataclass
class _State:
    start_time: datetime
    last_tick_at: datetime | None = None
    last_ok: bool = True
    last_error: str | None = None
    last_result: BuySignal | None = None
    alerting: bool = False
    last_session: str | None = None


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
    mark = "✅" if s.passes else "❌"
    state_label = _STATE_LABELS.get((s.name, s.state), s.state)
    header = f"{mark} {icon} <b>{_esc(display_name)}</b> — {_esc(state_label)}"
    body = f"<pre>{_esc(s.table)}</pre>" if s.table else _bullets(s.detail)
    return f"{header}\n{body}"


def _format_signal(result: BuySignal) -> str:
    emoji, label = _ALERT_LABELS.get(result.state, ("", result.state.upper()))
    header = f"{emoji} <b>{_esc(label)}</b> ({result.passing_count}/{len(result.subsignals)} conditions met)"
    blocks = [_format_subsignal(s) for s in result.subsignals]
    if result.missing_signals:
        blocks.append(f"⚠️ missing: {_esc(', '.join(result.missing_signals))}")
    return "\n\n".join([header, *blocks])


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


def handle_tick(result: BuySignal | None, error: Exception | None) -> None:
    """Called by runner.run_forever() after every tick attempt."""
    with _lock:
        _state.last_tick_at = datetime.now(timezone.utc)
        _state.last_ok = error is None
        _state.last_error = str(error) if error else None
        if result is not None:
            _state.last_result = result

    # Daily report: fires once at the regular-session close, regardless of pass/fail.
    session = runner.market_session()
    with _lock:
        was_regular = _state.last_session == runner.REGULAR
        _state.last_session = session
    if was_regular and session != runner.REGULAR and result is not None:
        _send(f"📅 <b>End of day</b>\n\n{_format_signal(result)}")

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
        _send("Signal cleared — back to normal.")


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
