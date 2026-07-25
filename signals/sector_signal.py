"""Score indicators #4 & #5: are the market's leading industries showing earnings growth."""

from collectors.sectors import sector_performance
from config import SECTOR_GROWTH_QUORUM, SECTOR_TOP_N
from signals.base import SubSignal


def _format_industry(row) -> str:
    return f"{row['industry']} cap {row['market_cap']:,.0f} growth {row['earnings_growth_estimate_1y']:+.1%}"


def score() -> SubSignal:
    """Pass if at least SECTOR_GROWTH_QUORUM of the top SECTOR_TOP_N industries (by market cap) are growing earnings."""
    df = sector_performance()
    growing = int((df["earnings_growth_estimate_1y"] > 0).sum())
    passes = growing >= SECTOR_GROWTH_QUORUM

    state = "growing" if passes else "flat"
    detail = f"{growing}/{SECTOR_TOP_N} top industries growing | " + " | ".join(
        _format_industry(row) for _, row in df.iterrows()
    )

    return SubSignal("sector", growing / SECTOR_TOP_N, state, detail, passes=passes)
