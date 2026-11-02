"""Prepared report parts; delivery happens only in the Telegram transport."""

from dataclasses import dataclass, field


@dataclass
class ReportMessage:
    text: str
    key: str = ""
    photo: bytes | None = field(default=None, repr=False)
    caption: str = ""


@dataclass
class Report:
    messages: list[ReportMessage]
