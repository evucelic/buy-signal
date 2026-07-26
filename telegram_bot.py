"""Long-polling Telegram bot: /signal and /status on request, plus threshold-crossing pushes.

Plain synchronous requests calls to the Bot API, matching the rest of the codebase's style —
no async framework needed for two commands and a push. Runs in a background thread started by
start(); the caller (bot.py) drives the actual tick loop (runner.run_forever()) and feeds it
results via handle_tick().
"""

import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

from buy_signal import BuySignal, compute_signal

load_dotenv()

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
API = f"https://api.telegram.org/bot{BOT_TOKEN}"

_POLL_TIMEOUT = 30  # seconds, Telegram long-poll
_RETRY_DELAY_SEC = 5

_COMMANDS = [
    {"command": "signal", "description": "Compute and show the current buy signal"},
    {"command": "status", "description": "Runner uptime and health"},
    {"command": "help", "description": "List available commands"},
]

_HELP_TEXT = "Commands:\n/signal — current buy signal\n/status — runner uptime and health"


@dataclass
class _State:
    start_time: datetime
    last_tick_at: datetime | None = None
    last_ok: bool = True
    last_error: str | None = None
    last_result: BuySignal | None = None
    alerting: bool = False


_state = _State(start_time=datetime.now(timezone.utc))
_lock = threading.Lock()


def _send(text: str) -> None:
    try:
        resp = requests.post(f"{API}/sendMessage", json={"chat_id": CHAT_ID, "text": text}, timeout=10)
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
        print(f"Telegram send failed ({type(exc).__name__}: {exc})")


def _is_alerting(result: BuySignal) -> bool:
    by_name = {s.name: s for s in result.subsignals}
    vix = by_name.get("vix")
    dip = by_name.get("market_dip")
    return (vix is not None and vix.passes) or (dip is not None and dip.passes) or result.state in ("soft", "strong")


def _format_signal(result: BuySignal) -> str:
    lines = [f"{result.state.upper()} ({result.passing_count}/{len(result.subsignals)} passing)"]
    for s in result.subsignals:
        mark = "✅" if s.passes else "❌"
        lines.append(f"{mark} {s.name} [{s.state}] {s.detail}")
    if result.missing_signals:
        lines.append(f"missing: {', '.join(result.missing_signals)}")
    return "\n".join(lines)


def _format_status() -> str:
    with _lock:
        uptime = datetime.now(timezone.utc) - _state.start_time
        last_tick_at = _state.last_tick_at
        last_ok = _state.last_ok
        last_error = _state.last_error
        result = _state.last_result

    lines = [f"uptime: {uptime}"]
    if last_tick_at is None:
        lines.append("last tick: none yet")
    else:
        status = "ok" if last_ok else f"FAILED ({last_error})"
        lines.append(f"last tick: {last_tick_at:%Y-%m-%d %H:%M UTC} ({status})")
    if result is not None:
        lines.append(f"current alert: {result.state.upper()}")
    return "\n".join(lines)


def handle_tick(result: BuySignal | None, error: Exception | None) -> None:
    """Called by runner.run_forever() after every tick attempt."""
    with _lock:
        _state.last_tick_at = datetime.now(timezone.utc)
        _state.last_ok = error is None
        _state.last_error = str(error) if error else None
        if result is not None:
            _state.last_result = result

    if result is None:
        return

    alerting = _is_alerting(result)
    with _lock:
        was_alerting = _state.alerting
        _state.alerting = alerting

    if alerting and not was_alerting:
        _send(f"\U0001f514 Signal active\n\n{_format_signal(result)}")
    elif was_alerting and not alerting:
        _send("✅ Back to normal.")


def _handle_message(text: str) -> str | None:
    text = text.strip()
    if text in ("/signal",):
        return _format_signal(compute_signal())
    if text in ("/status",):
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
