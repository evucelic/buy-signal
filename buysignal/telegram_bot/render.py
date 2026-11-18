"""Telegram HTML for signals and statuses; every value, the clock included, is passed in."""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime, timedelta

from buysignal.schedule.journal import CollectorStatus
from buysignal.signals.base import format_table
from buysignal.signals.buy_signal import BuySignal
from buysignal.telegram_bot.messages import Report, ReportMessage

# Keep the menu task-oriented; detailed signal shortcuts remain available in /help.
COMMANDS = [
    {"command": "signal", "description": "Entry checklist, with chart buttons"},
    {"command": "portfolio", "description": "Value, returns, mix, tax and cash from IBKR"},
    {"command": "whattobuy", "description": "Whole-share orders for your IBKR cash, fees included"},
    {"command": "target", "description": "Set your target mix: /target VUAA 60 VWCG 20 AVWS 15 CASH 5"},
    {"command": "help", "description": "All commands"},
]

HELP_TEXT = (
    "/signal — entry checklist, with charts\n"
    "/portfolio — value, returns, mix, risk, trades, cash, tax, valuation (IBKR)\n"
    "/whattobuy — orders for your IBKR cash · /whattobuy 500 for 500 €\n"
    "/target — /target VUAA 60 VWCG 20 AVWS 15 CASH 5\n"
    "/fees — fee schedule: /fees tiered, /fees VUAA 1.70 0.05\n"
    "/refresh — fresh data · /status — bot and data health\n\n"
    "Shortcuts: /vix /fedrate /margin /dip /sector /credit /curve"
)

_STATUS_ERROR_WIDTH = 40  # a stack-trace-shaped error still has to fit a phone screen


@dataclass(frozen=True)
class RunnerStatus:
    """What /status says about the tick loop itself, as the app hands it over."""

    uptime: timedelta
    last_tick_at: datetime | None
    ok: bool
    error: str | None
    read: str | None  # the checklist's label, e.g. "3 of 5"


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def fmt(value: float | None, spec: str = ".1f") -> str:
    return "n/a" if value is None else format(value, spec)


def humanize_age(updated_at: datetime | None, now: datetime) -> str:
    if updated_at is None:
        return "no cache"
    secs = (now - updated_at).total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def signal_report(result: BuySignal, caches: list[tuple[str, datetime | None]], now: datetime) -> Report:
    from buysignal.telegram_bot.views import overview
    return Report([ReportMessage(overview(result, caches, now), key="summary")])


def collector_table(statuses: list[CollectorStatus], now: datetime) -> str:
    """Per-collector last attempt and result; unlike file ages, it tells never-tried from failing."""
    if not statuses:
        return "no refresh attempted yet"

    rows = [
        [
            status.name,
            humanize_age(status.attempted_at, now),
            "ok" if status.ok else (status.error or "failed")[:_STATUS_ERROR_WIDTH],
        ]
        for status in statuses
    ]
    return "<pre>" + esc(format_table(["collector", "last try", "result"], rows)) + "</pre>"


def status_message(status: RunnerStatus, statuses: list[CollectorStatus], now: datetime) -> str:
    last_tick = "never" if status.last_tick_at is None else f"{status.last_tick_at:%Y-%m-%d %H:%M UTC}"
    tick_status = "ok" if status.ok else f"FAILED ({status.error})"

    lines = [
        f"uptime      {status.uptime}",
        f"last tick   {last_tick} ({tick_status})",
        f"read        {status.read or 'unknown'}",
    ]
    collectors = collector_table(statuses, now)
    return "<pre>" + esc("\n".join(lines)) + "</pre>" + f"\n\n<b>Collectors</b>\n{collectors}"


def refresh_failures(failed: list[tuple[str, str]]) -> str:
    """The "⚠️ Refresh failed" block shared by the pushes and the command replies."""
    lines = "\n".join(f"• {esc(name)}: {esc(str(error).splitlines()[0][:160])}" for name, error in failed)
    return f"⚠️ <b>Refresh failed</b>\n{lines}"
