"""Keep credentials out of everything the bot writes or sends: logs, the refresh journal, Telegram."""
from __future__ import annotations

import logging
import os
from urllib.parse import quote

SECRET_ENV = ("TELEGRAM_BOT_TOKEN", "IBKR_FLEX_TOKEN")
_MIN_LENGTH = 8  # a short value would blank out ordinary words


def _secrets() -> set[str]:
    values = {value for name in SECRET_ENV if len(value := os.environ.get(name, "")) >= _MIN_LENGTH}
    return values | {quote(value, safe="") for value in values}  # as it appears inside a URL


def redact(text: str) -> str:
    for value in _secrets():
        text = text.replace(value, "[redacted]")
    return text


def redact_strings(value):
    """A copy of a JSON-like value with every string in it redacted."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: redact_strings(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_strings(item) for item in value]
    return value


def public_env() -> dict[str, str]:
    """The environment without credentials, for child processes that never need them (Chrome)."""
    return {name: value for name, value in os.environ.items() if name not in SECRET_ENV}


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))
