"""Turn signals, statuses and opportunities into Telegram HTML. Nothing else.

Pure by construction: every value this module renders is passed in, including the clock. It
reads no files, no journal and no environment, so a change to a label is a change to a string
function - testable with literals, and free of the credentials the transport needs.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime, timedelta

from buysignal.schedule.journal import CollectorStatus
from buysignal.signals.base import format_table
from buysignal.signals.buy_signal import BuySignal
from buysignal.telegram_bot.messages import Report, ReportMessage

# Presentation only: signal logic/state strings (signals/*.py) are untouched by any of this.
ALERT_LABELS = {
    "none": ("⚪", "No buy signal"),
    "soft": ("🟡", "Soft signal"),
    "strong": ("🟢", "Entry conditions aligned"),
    "incomplete": ("⚠️", "Incomplete entry assessment"),
}

# Short row labels for the monospace table only (chart legends/notes keep the full config label).
TABLE_LABELS = {
    "sp500": "S&P 500",
    "world_small": "Small Cap",
    "world_small_value": "SC Value",
    "europe": "Europe",
}

# Keep the menu task-oriented; detailed signal shortcuts remain available in /help.
COMMANDS = [
    {"command": "signal", "description": "Entry checklist, with chart buttons"},
    {"command": "portfolio", "description": "Your ETF prices and recorded positions"},
    {"command": "whattobuy", "description": "Compare segment valuations"},
    {"command": "buy", "description": "Record a purchase: /buy 10 VUAA @ 151.25"},
    {"command": "trades", "description": "Purchase journal and record IDs"},
    {"command": "refresh", "description": "Fetch fresh data"},
    {"command": "status", "description": "Data collection and bot health"},
    {"command": "help", "description": "Commands and purchase examples"},
]

HELP_TEXT = (
    "Commands:\n"
    "/signal — entry checklist, with charts\n"
    "/portfolio — your ETF prices and positions\n"
    "/whattobuy — segment valuations\n"
    "/trades — purchase history\n"
    "/refresh — fresh data\n"
    "/status — bot and data health\n\n"
    "Record a purchase:\n"
    "bought 10 VUAA @ 151.25\n"
    "or /buy 10 VUAA @ 151.25 USD on 2026-11-05 fees 1.00\n"
    "No date = today. /voidtrade ID corrects a record.\n\n"
    "Shortcuts: /holdings /vix /fedrate /margin /dip /sector /credit /curve"
)

_STATUS_ERROR_WIDTH = 40  # a stack-trace-shaped error still has to fit a phone screen


@dataclass(frozen=True)
class RunnerStatus:
    """What /status says about the tick loop itself, as the app hands it over."""

    uptime: timedelta
    last_tick_at: datetime | None
    ok: bool
    error: str | None
    alert_state: str | None


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


_RATE_WORDS = {"ease": "lower", "no_change": "the same", "hike": "higher"}


def _own_history_word(z: float) -> str:
    return "cheap" if z >= 1 else "rich" if z <= -1 else "usual"


def opportunity_message(opp, easing_obs_needed: int, dips: dict | None = None) -> str:
    """Forward valuations of the markets behind the owner's ETFs, with each ETF's own dip."""
    dips = dips or {}
    rows = [("", "P/E", "vs US", "Dip")]
    # The owner's ETFs first, then indexes shown only for comparison.
    for s in sorted(opp.segments, key=lambda view: view.etf is None):
        name = s.etf or TABLE_LABELS.get(s.name, s.label)
        versus = "—" if s.name == "sp500" or s.discount_vs_spx is None else f"{-s.discount_vs_spx:+.0%}"
        dip = dips.get(s.etf, {}).get("drawdown") if s.etf else None
        rows.append((name, fmt(s.fwd_pe), versus, "" if dip is None else "at high" if dip == 0 else f"{dip:+.1%}"))
    table = "<pre>" + esc("\n".join(f"{name:<10}{pe:>5}{versus:>7}{dip:>8}".rstrip() for name, pe, versus, dip in rows)) + "</pre>"

    lines = []
    judged = [(s.etf or TABLE_LABELS.get(s.name, s.label), s.fwd_z) for s in opp.segments if s.fwd_z is not None]
    if judged:
        lines.append("Against its own past: " + " · ".join(f"{name} {_own_history_word(z)}" for name, z in judged))
    rate = opp.rate
    if rate.horizons:
        nearest = rate.horizons[0]
        odds = {"ease": nearest.ease, "no_change": nearest.no_change, "hike": nearest.hike}
        if nearest.state in _RATE_WORDS:
            lines.append(f"Fed rate at the next meeting: {_RATE_WORDS[nearest.state]} ({odds[nearest.state]:.0%})")
        else:
            lines.append("Fed rate at the next meeting: no clear favourite")
    if opp.buy_signal_state is not None:
        lines.append("Market read: " + ALERT_LABELS.get(opp.buy_signal_state, ("", opp.buy_signal_state))[1])

    asofs = {s.asof for s in opp.segments if s.asof}
    if len(asofs) == 1:
        lines.append(f"MSCI index data, {asofs.pop()} · updates monthly")
    elif asofs:
        lines.append("MSCI index data: " + ", ".join(f"{s.etf or s.label} {s.asof}" for s in opp.segments if s.asof))
    stale = [symbol for symbol, item in dips.items() if item.get("refresh_failed")]
    if stale:
        lines.append("⚠️ Dip from cached prices: " + ", ".join(stale))
    lines.extend("⚠️ " + note for note in opp.notes)

    blocks = [
        "<b>What to buy</b> · how each market is priced",
        table,
        "<i>P/E = price ÷ next year's expected earnings; lower is cheaper. Dip = below the ETF's own 3-month high.</i>",
        "\n".join(esc(line) for line in lines),
    ]
    return "\n".join(block for block in blocks if block)


def collector_table(statuses: list[CollectorStatus], now: datetime) -> str:
    """Per-collector last attempt and result, from the refresh journal the caller read.

    The freshness table only shows each cache's mtime, i.e. its last *successful* write, so
    "never attempted" and "attempted hourly and failing" look identical there; this is the
    view that tells them apart.
    """
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
    alert = ALERT_LABELS.get(status.alert_state, ("", "unknown"))[1] if status.alert_state else "unknown"

    lines = [
        f"uptime      {status.uptime}",
        f"last tick   {last_tick} ({tick_status})",
        f"alert       {alert}",
    ]
    collectors = collector_table(statuses, now)
    return "<pre>" + esc("\n".join(lines)) + "</pre>" + f"\n\n🔄 <b>Collectors</b>\n{collectors}"


def stale_collector_notes(
    name: str, failed: list[tuple[str, str]], statuses: list[CollectorStatus]
) -> list[str]:
    """Why a command's numbers may be stale when it refreshed nothing and reported nothing.

    A collector inside its retry backoff is skipped silently, which would otherwise render an
    old snapshot as if it were current.
    """
    if any(collector == name for collector, _ in failed):
        return []
    status = next((s for s in statuses if s.name == name), None)
    if status is None or status.ok:
        return []
    return [f"{name} refresh is failing ({status.error}); showing the last good data"]


def refresh_failures(failed: list[tuple[str, str]]) -> str:
    """The "⚠️ Refresh failed" block shared by the pushes and the command replies."""
    lines = "\n".join(f"• {esc(name)}: {esc(str(error).splitlines()[0][:160])}" for name, error in failed)
    return f"⚠️ <b>Refresh failed</b>\n{lines}"
