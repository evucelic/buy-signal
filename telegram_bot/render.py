"""Turn signals, statuses and opportunities into Telegram HTML. Nothing else.

Pure by construction: every value this module renders is passed in, including the clock. It
reads no files, no journal and no environment, so a change to a label is a change to a string
function - testable with literals, and free of the credentials the transport needs.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime, timedelta

from refresh_journal import CollectorStatus
from signals.base import SubSignal, format_table
from signals.buy_signal import BuySignal

# Presentation only: signal logic/state strings (signals/*.py) are untouched by any of this.
ALERT_LABELS = {
    "none": ("⚪", "No buy signal"),
    "soft": ("🟡", "Soft signal"),
    "strong": ("🟢", "STRONG BUY SIGNAL"),
}

SIGNAL_META = {
    "vix": ("🌡️", "VIX"),
    "fed_rate": ("🏦", "Fed Rate"),
    "margin_debt": ("💳", "Margin Debt"),
    "market_dip": ("📊", "Market Dip"),
    "sector": ("🏭", "Leading Industries"),
    "yield_curve": ("〽️", "Yield Curve"),
}

STATE_LABELS = {
    ("vix", "strong"): "🔴 High fear",
    ("vix", "soft"): "🟡 Elevated",
    ("vix", "none"): "⚪ Calm",
    ("fed_rate", "hiking"): "📈 Hiking",
    ("fed_rate", "cutting"): "📉 Cutting",
    ("fed_rate", "no_change"): "➡️ No change",
    ("fed_rate", "flat"): "🤔 Mixed",
    ("margin_debt", "deleveraging"): "📉 Deleveraging",
    ("margin_debt", "leveraging"): "📈 Leveraging up",
    ("market_dip", "dip"): "🔻 Dip",
    ("market_dip", "flat"): "➡️ Flat",
    ("market_dip", "growth"): "📈 Growth",
    ("sector", "growing"): "📈 Growing",
    ("sector", "flat"): "➡️ Flat",
    ("yield_curve", "steep"): "📈 Steep (supportive)",
    ("yield_curve", "flat"): "➡️ Flat/normal",
    ("yield_curve", "inverted"): "⚠️ Inverted",
    ("yield_curve", "deep_inversion"): "🚨 Deep inversion",
}

# Short row labels for the monospace table only (chart legends/notes keep the full config label).
TABLE_LABELS = {
    "sp500": "S&P 500",
    "world_small": "Small Cap",
    "world_small_value": "SC Value",
    "europe": "Europe",
}

RATE_STATE_LABELS = {"no_change": "no change"}  # everything else already reads fine as-is

# The command menu Telegram renders, and the same list as a message. Keep the two in step.
COMMANDS = [
    {"command": "signal", "description": "Full current buy signal (all sub-signals)"},
    {"command": "vix", "description": "VIX level"},
    {"command": "fedrate", "description": "Fed rate trajectory (CME FedWatch)"},
    {"command": "margin", "description": "FINRA margin debt (deleveraging)"},
    {"command": "dip", "description": "SPY/NASDAQ/DOW % change (dip watch)"},
    {"command": "sector", "description": "Leading industries earnings outlook"},
    {"command": "curve", "description": "10y-3m yield curve spread (advisory)"},
    {"command": "whattobuy", "description": "Segment valuations: S&P 500 vs world small cap vs Europe"},
    {"command": "refresh", "description": "Force a fresh fetch of all data (bypass cache)"},
    {"command": "status", "description": "Runner uptime and health"},
    {"command": "help", "description": "List available commands"},
]

HELP_TEXT = (
    "Commands:\n"
    "/signal: full current buy signal\n"
    "/vix: VIX level\n"
    "/fedrate: Fed rate trajectory\n"
    "/margin: FINRA margin debt\n"
    "/dip: SPY/NASDAQ/DOW % change\n"
    "/sector: leading industries earnings outlook\n"
    "/curve: 10y-3m yield curve spread (advisory)\n"
    "/whattobuy: segment valuations (S&P 500 / world small cap / Europe)\n"
    "/refresh: force a fresh fetch of all data (bypass cache)\n"
    "/status: runner uptime and health"
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


def _bullets(detail: str) -> str:
    """Split a ' | '-joined detail string into one escaped bullet per fragment."""
    return "\n".join(f"• {esc(part.strip())}" for part in detail.split(" | "))


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


def subsignal_block(s: SubSignal) -> str:
    icon, display_name = SIGNAL_META.get(s.name, ("•", s.name))
    mark = "ℹ️" if s.advisory else ("✅" if s.passes else "❌")
    state_label = STATE_LABELS.get((s.name, s.state), s.state)
    header = f"{mark} {icon} <b>{esc(display_name)}</b>: {esc(state_label)}"
    body = f"<pre>{esc(s.table)}</pre>" if s.table else _bullets(s.detail)
    if s.footer:
        body += f"\n{esc(s.footer)}"
    return f"{header}\n{body}"


def freshness_table(caches: list[tuple[str, datetime | None]], now: datetime) -> str:
    """How old each cache is, from the (name, last write) pairs the caller looked up."""
    rows = [[name, humanize_age(updated_at, now)] for name, updated_at in caches]
    return "<pre>" + esc(format_table(["signal", "updated"], rows)) + "</pre>"


def signal_message(result: BuySignal, caches: list[tuple[str, datetime | None]], now: datetime) -> str:
    emoji, label = ALERT_LABELS.get(result.state, ("", result.state.upper()))
    header = f"{emoji} <b>{esc(label)}</b> ({result.passing_count}/{result.required_count} conditions met)"
    blocks = [subsignal_block(s) for s in result.subsignals]
    if result.missing_signals:
        blocks.append(f"⚠️ missing: {esc(', '.join(result.missing_signals))}")
    blocks.append(f"🕓 <b>Data as of</b>\n{freshness_table(caches, now)}")
    return "\n\n".join([header, *blocks])


def opportunity_message(opp, easing_obs_needed: int) -> str:
    """Everything-in-one-place valuation facts; no interpretation beyond the matrix verdict."""
    rows = [
        [
            TABLE_LABELS.get(s.name, s.label),
            fmt(s.fwd_pe),
            fmt(s.trailing_pe),
            fmt(s.ratio_vs_spx, ".2f"),
            fmt(s.fwd_z, "+.1f"),
            fmt(s.rel_z, "+.1f"),
        ]
        for s in opp.segments
    ]
    table = "<pre>" + esc(format_table(["Segment", "Fwd", "Trail", "vsUS", "z", "relZ"], rows)) + "</pre>"

    lines = []
    if opp.small_cap_band is not None:
        lines.append(f"Small-cap band: {opp.small_cap_band}")

    rate = opp.rate
    if rate.horizons:
        sequence = " → ".join(RATE_STATE_LABELS.get(h.state, h.state) for h in rate.horizons)
        support = "yes" if rate.rate_support else "no"
        lines.append(
            f"FedWatch: {rate.consecutive_easing}/{len(rate.horizons)} easing "
            f"({sequence}, rate support: {support}, need ≥{easing_obs_needed})"
        )

    lines.append(f"Verdict: {opp.verdict}")
    if opp.buy_signal_state is not None:
        label = ALERT_LABELS.get(opp.buy_signal_state, ("", opp.buy_signal_state))[1]
        lines.append(f"Buy signal (context): {label}")
    facts = "\n".join(f"• {esc(line)}" for line in lines)

    asofs = {s.asof for s in opp.segments if s.asof}
    if len(asofs) == 1:
        footer = f"🕓 MSCI data as of {esc(asofs.pop())} (updates monthly)"
    elif asofs:
        per_segment = ", ".join(f"{s.label} {s.asof}" for s in opp.segments if s.asof)
        footer = f"🕓 MSCI data as of: {esc(per_segment)} (updates monthly)"
    else:
        footer = ""
    notes = "\n".join(f"• {esc(note)}" for note in opp.notes)

    blocks = ["🧭 <b>What to buy: segment valuations</b>", table, facts]
    if footer:
        blocks.append(footer)
    if notes:
        blocks.append(f"⚠️ <b>Notes</b>\n{notes}")
    return "\n\n".join(blocks)


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
    lines = "\n".join(f"• {esc(name)}: {esc(error)}" for name, error in failed)
    return f"⚠️ <b>Refresh failed</b>\n{lines}"
