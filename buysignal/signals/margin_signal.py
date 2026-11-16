"""Score indicator #3: FINRA margin debt decreasing (deleveraging)."""

from __future__ import annotations

from buysignal.collectors.margin_debt import data_freshness, margin_history
from buysignal.config import MARGIN_DELEVERAGE_MONTHS
from buysignal.signals.base import SubSignal

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
    monthly = debt.pct_change().dropna()
    yearly = float(debt.iloc[-1] / debt.iloc[-13] - 1) if len(debt) > 12 else None
    falling_months = 0
    for value in reversed(monthly.tolist()):
        if value >= 0:
            break
        falling_months += 1
    # How often a monthly decline has occurred: the rule's own base frequency.
    falling_share = float((monthly < 0).mean()) if len(monthly) >= 24 else None
    flag = _TIER_FLAG.get(tier)
    if flag:
        detail += f" | {flag}"

    return SubSignal(
        "margin_debt", latest_change_pct, state, detail, passes=deleveraging,
        observed_at=str(history["month"].iloc[-1].date()), history=history.copy(),
        metrics={"latest": float(debt.iloc[-1]), "change": float(latest_change_pct), "freshness": tier,
                 # A month past its release window cannot pass: the checklist reads it as incomplete.
                 "stale": tier == "stale",
                 "yearly": yearly, "falling_months": falling_months, "falling_share": falling_share,
                 "since": str(history["month"].iloc[0].date())},
    )
