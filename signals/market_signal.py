"""Score indicator #6: daily/weekly/monthly % change on SPY, Nasdaq Composite, and Dow Jones."""

from collectors.market import get_latest_market_changes
from config import INDEX_TICKERS, MARKET_DIP_THRESHOLD, MARKET_PRIMARY_INDEX
from signals.base import SubSignal


def _format_index(name: str, changes: dict[str, float]) -> str:
    return f"{name} daily {changes['daily']:+.1%}, weekly {changes['weekly']:+.1%}, monthly {changes['monthly']:+.1%}"


def score() -> SubSignal:
    """Map SPY's daily change to a dip signal; NASDAQ/DOW are diagnostic only."""
    changes = get_latest_market_changes()
    daily = changes[MARKET_PRIMARY_INDEX]["daily"]
    dip = daily <= MARKET_DIP_THRESHOLD

    state = "dip" if dip else "flat"
    detail = " | ".join(_format_index(name, changes[name]) for name in INDEX_TICKERS)

    return SubSignal("market_dip", daily, state, detail, passes=dip)
