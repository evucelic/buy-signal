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
import queue
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
_message_context = threading.local()


def current_message_key():
    return getattr(_message_context, "source_key", None)
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
            interval = .15 if method.startswith("editMessage") else 1.05
            pause = interval - (time.monotonic() - _last_delivery_at)
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
                description = _safe_error(Exception(str(body.get("description", ""))))
                if return_result and response.status_code == 400:
                    lower = description.lower()
                    if any(part in lower for part in ("message to edit not found", "message can't be edited")):
                        return {"missing": True}
                    if "wrong file identifier" in lower or "file_reference" in lower or "file reference" in lower:
                        return {"invalid_media": True}
                if method.startswith("editMessage") and body.get("description", "").startswith("Bad Request: message is not modified"):
                    return {"ok": True} if return_result else True
                if response.status_code >= 400 or body.get("ok") is not True:
                    log.warning("Telegram %s rejected (%s): %s", method, response.status_code, description[:240])
                if response.status_code >= 500 and method.startswith('editMessage') and attempt < 2:
                    time.sleep(.25 * (attempt + 1))
                    continue
                response.raise_for_status()
                if body.get("ok") is not True:
                    raise requests.exceptions.RequestException("Telegram returned ok=false")
                return (body.get("result") or {"ok": True}) if return_result else True
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                if method.startswith('editMessage') and attempt < 2:
                    time.sleep(.25 * (attempt + 1))
                    continue
                log.error("Telegram %s failed (%s: %s)", method, type(exc).__name__, _safe_error(exc))
                return False
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


def show_panel(text: str, keyboard: dict, png: bytes | str | None, existing: dict | None = None) -> dict | None:
    """Create one silent panel, then edit it in place on subsequent navigation."""
    existing = existing or {}
    message_id = existing.get("message_id")
    replace_id = message_id if existing.get('replace_with_text') else None
    if replace_id:
        message_id = None
    photo = png is not None
    payload = {"chat_id": chat_id(), "reply_markup": keyboard}
    if message_id:
        payload["message_id"] = message_id
    with _delivery_lock:
        previous = getattr(_delivery_state, "silent", False)
        _delivery_state.silent = True
        try:
            if photo:
                if isinstance(png, str):
                    if message_id:
                        payload['media'] = {'type': 'photo', 'media': png, 'caption': text, 'parse_mode': 'HTML'}
                        result = _post('editMessageMedia', json=payload, return_result=True)
                    else:
                        payload.update(photo=png, caption=text, parse_mode='HTML')
                        result = _post('sendPhoto', json=payload, return_result=True)
                elif message_id:
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
            if isinstance(result, dict) and result.get("invalid_media"):
                return result
            if isinstance(result, dict) and result.get("missing"):
                return show_panel(text, keyboard, png)
            ident = result.get("message_id", message_id) if isinstance(result, dict) else message_id
            if not ident:
                return None
            panel = {"message_id": ident, "photo": photo}
            photos = result.get('photo', []) if isinstance(result, dict) else []
            if photos and isinstance(photos[-1], dict) and photos[-1].get('file_id'):
                panel['file_id'] = photos[-1]['file_id']
            elif isinstance(png, str):
                panel['file_id'] = png
            if replace_id:
                close_panel(replace_id)
            return panel
        finally:
            _delivery_state.silent = previous


def close_panel(message_id: int) -> bool:
    return _post("deleteMessage", json={"chat_id": chat_id(), "message_id": message_id})


def _deliver(reply):
    if isinstance(reply, Report):
        send_report(reply)
    elif reply is not None:
        send(reply)


class _CommandWorker:
    """One bounded daemon worker keeps long collection commands off the polling loop."""
    def __init__(self, handler):
        self.handler = handler
        self.pending = queue.Queue(maxsize=1)
        self.active = set()
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name='telegram-commands')
        self.thread.start()

    def submit(self, text):
        with self.lock:
            if text in self.active:
                return False
            try:
                self.pending.put_nowait(text)
            except queue.Full:
                return False
            self.active.add(text)
            return True

    def _run(self):
        while not self.closed.is_set():
            try:
                text = self.pending.get(timeout=.25)
            except queue.Empty:
                continue
            try:
                _deliver(self.handler(text))
            except Exception as exc:
                log.error('Telegram command failed (%s: %s)', type(exc).__name__, _safe_error(exc))
                notice('Command failed. The previous report remains available.')
            finally:
                with self.lock:
                    self.active.discard(text)
                self.pending.task_done()

    def close(self):
        self.closed.set()


def poll_forever(handle_message: Callable[[str], str | Report | None], handle_callback=None) -> None:
    offset = None
    worker = None
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
                text = message.get('text', '').strip()
                message_key = f"telegram:{message['chat']['id']}:{message['message_id']}" if message.get('message_id') is not None else None
                if text.split(' ', 1)[0] in {'/signal', '/refresh', '/whattobuy', '/vix', '/dip', '/trend', '/fedrate', '/margin', '/sector', '/curve', '/credit', '/portfolio'}:
                    if worker is None:
                        worker = _CommandWorker(handle_message)
                    if not worker.submit(text):
                        notice('A command is already running; the current report buttons remain available.')
                else:
                    _message_context.source_key = message_key
                    try:
                        _deliver(handle_message(text))
                    finally:
                        _message_context.source_key = None
        except (KeyboardInterrupt, SystemExit):
            if worker:
                worker.close()
            raise
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
