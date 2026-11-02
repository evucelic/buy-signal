"""The Telegram Bot API itself: credentials, sending, and the long-poll loop.

Plain synchronous requests calls, matching the rest of the codebase's style: no async
framework needed for a handful of commands and a push. Credentials are read at send time,
not at import, so importing anything else in this package costs nothing and needs no
environment - the command handler is passed in by the caller, so this module knows the wire
and nothing about what the bot actually says.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable

import requests
from dotenv import load_dotenv

from buysignal.telegram_bot.render import COMMANDS
from buysignal.telegram_bot.messages import Report
from buysignal.telegram_bot.html_chunks import split_html

load_dotenv()

log = logging.getLogger(__name__)

POLL_TIMEOUT = 30  # seconds, Telegram long-poll
RETRY_DELAY_SEC = 5


def chat_id() -> str:
    return os.environ["TELEGRAM_CHAT_ID"]


def _api() -> str:
    return f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}"


# A report is sent as a batch: command replies and scheduled pushes cannot interleave.
_delivery_lock = threading.RLock()
_delivery_state = threading.local()
_last_delivery_at = 0.0


def _safe_error(exc: Exception) -> str:
    message = str(exc)
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    return message.replace(token, "[redacted]") if token else message


def _post(method: str, *, json=None, data=None, files=None, timeout=10) -> bool:
    global _last_delivery_at
    with _delivery_lock:
        payload = json if json is not None else data
        payload["disable_notification"] = getattr(_delivery_state, "silent", False)
        for attempt in range(3):
            pause = 1.05 - (time.monotonic() - _last_delivery_at)
            if pause > 0:
                time.sleep(pause)
            try:
                kwargs = {"json": json} if json is not None else {"data": data, "files": files}
                response = requests.post(f"{_api()}/{method}", timeout=timeout, **kwargs)
                _last_delivery_at = time.monotonic()
                if response.status_code == 429 and attempt < 2:
                    delay = max(1, int(response.json().get("parameters", {}).get("retry_after", 2)))
                    if delay > 60:
                        log.warning("Telegram requested a long cooldown; leaving delivery unsuccessful for a later attempt")
                        return False
                    time.sleep(delay)
                    continue
                response.raise_for_status()
                if response.json().get("ok") is not True:
                    raise requests.exceptions.RequestException("Telegram returned ok=false")
                return True
            except (requests.exceptions.RequestException, ValueError, KeyError) as exc:
                log.error("Telegram %s failed (%s: %s)", method, type(exc).__name__, _safe_error(exc))
                return False
        return False


def send(text: str) -> bool:
    """Send balanced HTML chunks below Telegram's limit; return delivery success."""
    with _delivery_lock:
        for chunk in split_html(text):
            if not _post("sendMessage", json={"chat_id": chat_id(), "text": chunk, "parse_mode": "HTML"}):
                return False
    return True


def send_photo(png: bytes, caption: str) -> bool:
    return _post(
        "sendPhoto", data={"chat_id": chat_id(), "caption": caption[:900]},
        files={"photo": ("chart.png", png, "image/png")}, timeout=20,
    )


def send_report(report: Report) -> bool:
    """Text remains useful if a chart fails. Stop on text failure, keep batches ordered."""
    with _delivery_lock:
        previous_silent = getattr(_delivery_state, "silent", False)
        try:
            for index, message in enumerate(report.messages):
                _delivery_state.silent = index > 0
                if not send(message.text):
                    return False
                if message.photo is not None:
                    _delivery_state.silent = True
                    send_photo(message.photo, message.caption)
            return True
        finally:
            _delivery_state.silent = previous_silent


def notify_started() -> None:
    send("🟢 Runner started")


def notify_stopped() -> None:
    send("🔴 Runner stopped")


def poll_forever(handle_message: Callable[[str], str | Report | None]) -> None:
    offset = None
    while True:
        try:
            resp = requests.get(
                f"{_api()}/getUpdates",
                params={"timeout": POLL_TIMEOUT, "offset": offset},
                timeout=POLL_TIMEOUT + 10,
            )
            resp.raise_for_status()
            for update in resp.json()["result"]:
                offset = update["update_id"] + 1
                message = update.get("message")
                if not message or str(message["chat"]["id"]) != str(chat_id()):
                    continue
                reply = handle_message(message.get("text", ""))
                if isinstance(reply, Report):
                    send_report(reply)
                elif reply is not None:
                    send(reply)
        except requests.exceptions.RequestException as exc:
            log.warning("Telegram poll failed (%s: %s); retrying.", type(exc).__name__, _safe_error(exc))
            time.sleep(RETRY_DELAY_SEC)


def start(handle_message: Callable[[str], str | Report | None]) -> None:
    """Register the bot's command menu and poll for updates in a background thread."""
    try:
        resp = requests.post(f"{_api()}/setMyCommands", json={"commands": COMMANDS}, timeout=10)
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
        log.warning("Telegram setMyCommands failed (%s: %s); continuing anyway.", type(exc).__name__, _safe_error(exc))
    threading.Thread(target=poll_forever, args=(handle_message,), daemon=True).start()
