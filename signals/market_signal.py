"""Score indicator #6: daily/weekly/monthly % change on SPY, Nasdaq Composite, and Dow Jones."""

from collectors.market import get_latest_market_changes, latest_changes
from config import INDEX_TICKERS, MARKET_DIP_THRESHOLD, MARKET_GROWTH_THRESHOLD, MARKET_PRIMARY_INDEX
from signals.base import SubSignal, format_table


def _format_index(name: str, changes: dict[str, float]) -> str:
    return f"{name} daily {changes['daily']:+.1%}, weekly {changes['weekly']:+.1%}, monthly {changes['monthly']:+.1%}"


def _table(changes: dict[str, dict[str, float]]) -> str:
    rows = [
        [name, f"{c['daily']:+.1%}", f"{c['weekly']:+.1%}", f"{c['monthly']:+.1%}"]
        for name, c in changes.items()
    ]
    return format_table(["Index", "Daily", "Weekly", "Monthly"], rows)


def score(allow_refresh: bool = True) -> SubSignal:
    """Map SPY's daily change to a dip signal; NASDAQ/DOW are diagnostic only.

    allow_refresh=False serves the cached values without fetching (e.g. the market's closed,
    so nothing new to find) — falls back to a live fetch if there's no cache yet.
    """
    changes = get_latest_market_changes() if allow_refresh else (latest_changes() or get_latest_market_changes())
    daily = changes[MARKET_PRIMARY_INDEX]["daily"]
    dip = daily <= MARKET_DIP_THRESHOLD

    if dip:
        state = "dip"
    elif daily >= MARKET_GROWTH_THRESHOLD:
        state = "growth"
    else:
        state = "flat"
    detail = " | ".join(_format_index(name, changes[name]) for name in INDEX_TICKERS)

    return SubSignal("market_dip", daily, state, detail, passes=dip, table=_table(changes))
