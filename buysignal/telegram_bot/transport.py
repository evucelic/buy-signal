"""The Telegram Bot API itself: credentials, sending, and the long-poll loop.

Plain synchronous requests calls, matching the rest of the codebase's style: no async
framework needed for a handful of commands and a push. Credentials are read at send time,
not at import, so importing anything else in this package costs nothing and needs no
environment - the command handler is passed in by the caller, so this module knows the wire
and nothing about what the bot actually says.
"""

from __future__ import annotations

import logging
import json as jsonlib
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


def _post(method: str, *, json=None, data=None, files=None, timeout=10, return_result=False):
    global _last_delivery_at
    with _delivery_lock:
        payload = json if json is not None else data
        if method in ("sendMessage", "sendPhoto"):
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
                body = response.json()
                if return_result and response.status_code == 400 and "message to edit not found" in body.get("description", "").lower():
                    return {"missing": True}
                if method.startswith("editMessage") and body.get("description", "").startswith("Bad Request: message is not modified"):
                    return {"ok": True} if return_result else True
                response.raise_for_status()
                if body.get("ok") is not True:
                    raise requests.exceptions.RequestException("Telegram returned ok=false")
                return (body.get("result") or {"ok": True}) if return_result else True
            except (requests.exceptions.RequestException, ValueError, KeyError) as exc:
                log.error("Telegram %s failed (%s: %s)", method, type(exc).__name__, _safe_error(exc))
                return False
        return False


def send(text: str, keyboard: dict | None = None) -> bool:
    """Send balanced HTML chunks below Telegram's limit; return delivery success."""
    with _delivery_lock:
        chunks = split_html(text)
        for index, chunk in enumerate(chunks):
            payload = {"chat_id": chat_id(), "text": chunk, "parse_mode": "HTML"}
            if keyboard and index == len(chunks) - 1:
                payload["reply_markup"] = keyboard
            if not _post("sendMessage", json=payload):
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
                if message.panel:
                    delivered_panel = show_panel(message.text, message.keyboard or {}, message.photo)
                    if not delivered_panel:
                        return False
                    if message.on_delivery:
                        message.on_delivery(delivered_panel)
                    continue
                delivered = send(message.text, keyboard=message.keyboard) if message.keyboard else send(message.text)
                if not delivered:
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


def answer_callback(query_id: str, text: str | None = None) -> None:
    """Acknowledge immediately, before any chart work or delivery pacing."""
    payload = {"callback_query_id": query_id}
    if text:
        payload["text"] = text[:180]
    try:
        response = requests.post(f"{_api()}/answerCallbackQuery", json=payload, timeout=5)
        response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        log.warning("Telegram callback acknowledgement failed (%s)", _safe_error(exc))


def notice(text: str) -> None:
    """A brief silent operational error after a callback has already been answered."""
    with _delivery_lock:
        previous = getattr(_delivery_state, "silent", False)
        _delivery_state.silent = True
        try:
            send(text)
        finally:
            _delivery_state.silent = previous


def show_panel(text: str, keyboard: dict, png: bytes | None, existing: dict | None = None) -> dict | None:
    """Create one silent panel, then edit it in place on subsequent navigation."""
    existing = existing or {}
    message_id = existing.get("message_id")
    photo = png is not None
    payload = {"chat_id": chat_id(), "reply_markup": keyboard}
    if message_id:
        payload["message_id"] = message_id
    with _delivery_lock:
        previous = getattr(_delivery_state, "silent", False)
        _delivery_state.silent = True
        try:
            if photo:
                if message_id:
                    payload["media"] = jsonlib.dumps({"type": "photo", "media": "attach://chart", "caption": text, "parse_mode": "HTML"})
                    payload["reply_markup"] = jsonlib.dumps(keyboard)
                    result = _post("editMessageMedia", data=payload, files={"chart": ("chart.png", png, "image/png")}, timeout=20, return_result=True)
                else:
                    payload.update(caption=text, parse_mode="HTML", reply_markup=jsonlib.dumps(keyboard))
                    result = _post("sendPhoto", data=payload, files={"photo": ("chart.png", png, "image/png")}, timeout=20, return_result=True)
            elif message_id and existing.get("photo"):
                # Detail toggles reuse the same chart and edit only its caption.
                payload.update(caption=text, parse_mode="HTML")
                result = _post("editMessageCaption", json=payload, return_result=True)
                photo = True
            else:
                payload.update(text=text, parse_mode="HTML")
                result = _post("editMessageText" if message_id else "sendMessage", json=payload, return_result=True)
            if not result:
                return None
            if isinstance(result, dict) and result.get("missing"):
                return show_panel(text, keyboard, png)
            ident = result.get("message_id", message_id) if isinstance(result, dict) else message_id
            return {"message_id": ident, "photo": photo} if ident else None
        finally:
            _delivery_state.silent = previous


def close_panel(message_id: int) -> bool:
    return _post("deleteMessage", json={"chat_id": chat_id(), "message_id": message_id})


def poll_forever(handle_message: Callable[[str], str | Report | None], handle_callback=None) -> None:
    offset = None
    while True:
        try:
            resp = requests.get(
                f"{_api()}/getUpdates",
                params={"timeout": POLL_TIMEOUT, "offset": offset, "allowed_updates": jsonlib.dumps(["message", "callback_query"])},
                timeout=POLL_TIMEOUT + 10,
            )
            resp.raise_for_status()
            for update in resp.json()["result"]:
                offset = update["update_id"] + 1
                callback = update.get("callback_query")
                if callback:
                    if str(callback.get("message", {}).get("chat", {}).get("id")) != str(chat_id()):
                        continue
                    if handle_callback:
                        try:
                            handle_callback(callback)
                        except Exception as exc:
                            log.error("Callback failed (%s: %s)", type(exc).__name__, _safe_error(exc))
                    else:
                        answer_callback(callback["id"])
                    continue
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


def start(handle_message: Callable[[str], str | Report | None], handle_callback=None) -> None:
    """Register the bot's command menu and poll for updates in a background thread."""
    try:
        resp = requests.post(f"{_api()}/setMyCommands", json={"commands": COMMANDS}, timeout=10)
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
        log.warning("Telegram setMyCommands failed (%s: %s); continuing anyway.", type(exc).__name__, _safe_error(exc))
    threading.Thread(target=poll_forever, args=(handle_message, handle_callback), daemon=True).start()
