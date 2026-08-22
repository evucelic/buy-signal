"""Score indicator #3: FINRA margin debt decreasing (deleveraging)."""

from __future__ import annotations

from collectors.margin_debt import data_freshness, margin_history
from config import MARGIN_DELEVERAGE_MONTHS
from signals.base import SubSignal

# No dates here -- the "data as of" freshness table elsewhere already shows cache age.
_TIER_FLAG = {
    "refresh_due_soon": "refresh due soon",
    "stale": "data stale, check scraper",
}


def score() -> SubSignal:
    """Return the margin-debt sub-signal: deleveraging over the last N months."""
    history = margin_history()
    debt = history["debit_balances"]

    recent = debt.tail(MARGIN_DELEVERAGE_MONTHS + 1)
    diffs = recent.diff().dropna()
    deleveraging = bool((diffs < 0).all()) if not diffs.empty else False
    latest_change_pct = diffs.iloc[-1] / recent.iloc[-2] if not diffs.empty else 0.0

    state = "deleveraging" if deleveraging else "leveraging"
    detail = (
        f"debit balances {'decreasing' if deleveraging else 'not decreasing'} over last "
        f"{MARGIN_DELEVERAGE_MONTHS}mo (latest {debt.iloc[-1]:,.0f}, {latest_change_pct:+.1%} m/m)"
    )
    tier, _ = data_freshness(history)
    flag = _TIER_FLAG.get(tier)
    if flag:
        detail += f" | {flag}"

    return SubSignal("margin_debt", latest_change_pct, state, detail, passes=deleveraging)
