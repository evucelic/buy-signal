"""Score indicators #4 & #5: are the market's leading industries showing earnings growth."""

from collectors.sectors import sector_performance
from config import SECTOR_GROWTH_QUORUM, SECTOR_TOP_N
from signals.base import SubSignal

_CAP_UNITS = ((1e12, "T"), (1e9, "B"), (1e6, "M"))


def _human_cap(n: float) -> str:
    for threshold, suffix in _CAP_UNITS:
        if abs(n) >= threshold:
            return f"{n / threshold:.2f}{suffix}"
    return f"{n:,.0f}"


def _format_industry(row) -> str:
    trend = "📈" if row["earnings_growth_estimate_1y"] > 0 else "📉"
    return (
        f"{trend} {row['industry']}: cap {_human_cap(row['market_cap'])}, "
        f"next-yr EPS growth (est.) {row['earnings_growth_estimate_1y']:+.1%}"
    )


def score() -> SubSignal:
    """Pass if at least SECTOR_GROWTH_QUORUM of the top SECTOR_TOP_N industries (by market cap) have a rising EPS estimate."""
    df = sector_performance()
    growing = int((df["earnings_growth_estimate_1y"] > 0).sum())
    passes = growing >= SECTOR_GROWTH_QUORUM

    state = "growing" if passes else "flat"
    detail = f"{growing}/{SECTOR_TOP_N} top industries with rising EPS estimates | " + " | ".join(
        _format_industry(row) for _, row in df.iterrows()
    )

    return SubSignal("sector", growing / SECTOR_TOP_N, state, detail, passes=passes)
