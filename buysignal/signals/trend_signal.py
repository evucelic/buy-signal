"""Advisory price-trend context derived from the same market observation as drawdown."""

from buysignal.signals.base import SubSignal

_DESCRIPTIONS = {
    "uptrend": "Price ≥ SMA200; SMA200 rising",
    "under_pressure": "Price < SMA200; SMA200 rising",
    "downtrend": "Price < SMA200; SMA200 falling",
    "recovering": "Price ≥ SMA200; SMA200 falling",
    "unknown": "SMA200 / slope20 unavailable: history <220 sessions",
}


def from_market(market: SubSignal) -> SubSignal:
    facts = market.metrics
    state = facts.get("trend", "unknown")
    detail = _DESCRIPTIONS[state]
    if facts.get("ma200") is not None:
        detail += f" | 200-session average ${facts['ma200']:,.2f}; average changed {facts['ma_slope']:+.2%} over 20 sessions"
    return SubSignal(
        "trend", facts.get("ma_slope") or 0.0, state, detail, advisory=True,
        observed_at=market.observed_at,
        footer="Trend: context",
    )
