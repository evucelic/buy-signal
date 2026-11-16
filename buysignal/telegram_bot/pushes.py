"""Which push a reading calls for: a watch trigger, high fear, or a change in the checklist count.

One pure decision. decide() takes what the owner has been told so far and a new reading, and
returns the heading of the message that reading calls for ('' for none) with what the owner
will know once a message carrying that heading is delivered. Callers adopt the new Seen only
after delivery (or at once when the heading is empty), so a Telegram outage delays a push
instead of swallowing it.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date

from buysignal.config import VIX_STRONG
from buysignal.signals.buy_signal import INCOMPLETE, BuySignal, usable
from buysignal.telegram_bot.render import esc

_WATCHED = ("vix", "market_dip")


@dataclass(frozen=True)
class Seen:
    alerting: bool = False               # a watch trigger (VIX at 22+ or the SPY dip) is active
    high_fear: bool = False              # VIX was at 30+ at the last reading that knew it
    high_fear_date: date | None = None   # day of the last high-fear push: one a day at most
    count: int | None = None             # checklist count of the last complete reading


def is_alerting(result: BuySignal) -> bool:
    return any(signal.passes for signal in result.subsignals if signal.name in _WATCHED)


def high_fear(result: BuySignal) -> bool | None:
    """VIX at or above the high-fear line; None when there is no current VIX reading."""
    vix = next((s for s in result.subsignals if s.name == "vix"), None)
    return vix.score >= VIX_STRONG if usable(vix) else None


def trigger_reasons(result: BuySignal) -> str:
    by_name = {s.name: s for s in result.subsignals}
    reasons = []
    if by_name.get("vix") is not None and by_name["vix"].passes:
        reasons.append("VIX high fear" if high_fear(result) else "VIX elevated")
    if by_name.get("market_dip") is not None and by_name["market_dip"].passes:
        reasons.append("SPY dip")
    return ", ".join(reasons)


def _watch_known(result: BuySignal) -> bool:
    """Missing or stale VIX/SPY evidence must not clear a watch."""
    return not (set(result.missing_signals) & set(_WATCHED)
                or any(s.metrics.get("stale") for s in result.subsignals if s.name in _WATCHED))


def decide(seen: Seen, result: BuySignal, today: date) -> tuple[str, Seen]:
    """(heading, seen after delivery); `today` is the Central date, which also keys the daily report."""
    alerting, high = is_alerting(result), high_fear(result)
    complete = result.state != INCOMPLETE
    after = replace(seen, high_fear=seen.high_fear if high is None else high,
                    count=result.passing_count if complete else seen.count)
    lines = []
    if alerting and not seen.alerting:
        lines.append(f"🔔 <b>Watch trigger active</b> ({esc(trigger_reasons(result))})")
        after = replace(after, alerting=True, high_fear_date=today if high else seen.high_fear_date)
    elif high and not seen.high_fear and seen.high_fear_date != today:
        # Already on watch at a lower level: the one level that stands apart gets its own push.
        lines.append("🔴 <b>High fear</b> (VIX at or above 30)")
        after = replace(after, high_fear_date=today)
    elif seen.alerting and not alerting and _watch_known(result):
        lines.append("<b>Watch trigger cleared (VIX/SPY)</b>")
        after = replace(after, alerting=False)
    # The first complete reading after a start is the baseline, not a change.
    if complete and seen.count is not None and seen.count != result.passing_count:
        lines.append(f"🔢 <b>Checklist {seen.count} → {result.passing_count} of {result.required_count}</b>")
    return ("\n".join(lines) + "\n\n" if lines else ""), after
