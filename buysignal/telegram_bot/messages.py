"""Prepared report parts; delivery happens only in the Telegram transport."""

from dataclasses import dataclass, field
from collections.abc import Callable


@dataclass
class ReportMessage:
    text: str
    key: str = ""
    photo: bytes | None = field(default=None, repr=False)
    caption: str = ""
    keyboard: dict | None = None
    panel: bool = False
    on_delivery: Callable[[dict], None] | None = field(default=None, repr=False, compare=False)


@dataclass
class Report:
    messages: list[ReportMessage]
