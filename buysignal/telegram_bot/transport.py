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

load_dotenv()

log = logging.getLogger(__name__)

POLL_TIMEOUT = 30  # seconds, Telegram long-poll
RETRY_DELAY_SEC = 5


def chat_id() -> str:
    return os.environ["TELEGRAM_CHAT_ID"]


def _api() -> str:
    return f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}"


def send(text: str) -> bool:
    """Send a message. Returns whether it was delivered; a failure is logged, never raised."""
    try:
        resp = requests.post(
            f"{_api()}/sendMessage",
            json={"chat_id": chat_id(), "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
        log.error("Telegram send failed (%s: %s)", type(exc).__name__, exc)
        return False
    return True


def send_photo(png: bytes, caption: str) -> bool:
    """Send a photo. Returns whether it was delivered; a failure is logged, never raised."""
    try:
        resp = requests.post(
            f"{_api()}/sendPhoto",
            data={"chat_id": chat_id(), "caption": caption},
            files={"photo": ("chart.png", png, "image/png")},
            timeout=20,
        )
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
        log.error("Telegram sendPhoto failed (%s: %s)", type(exc).__name__, exc)
        return False
    return True


def notify_started() -> None:
    send("🟢 Runner started")


def notify_stopped() -> None:
    send("🔴 Runner stopped")


def poll_forever(handle_message: Callable[[str], str | None]) -> None:
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
                if reply is not None:
                    send(reply)
        except requests.exceptions.RequestException as exc:
            log.warning("Telegram poll failed (%s: %s); retrying.", type(exc).__name__, exc)
            time.sleep(RETRY_DELAY_SEC)


def start(handle_message: Callable[[str], str | None]) -> None:
    """Register the bot's command menu and poll for updates in a background thread."""
    try:
        resp = requests.post(f"{_api()}/setMyCommands", json={"commands": COMMANDS}, timeout=10)
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
        log.warning("Telegram setMyCommands failed (%s: %s); continuing anyway.", type(exc).__name__, exc)
    threading.Thread(target=poll_forever, args=(handle_message,), daemon=True).start()
