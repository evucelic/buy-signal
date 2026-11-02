"""Score indicators #4 & #5: are the market's leading industries showing earnings growth."""

import pandas as pd

from buysignal.collectors.sectors import sector_performance
from buysignal.config import SECTOR_GROWTH_QUORUM, SECTOR_TOP_N, SECTOR_MIN_ESTIMATE_COVERAGE
from buysignal.signals.base import SubSignal

_CAP_UNITS = ((1e12, "T"), (1e9, "B"), (1e6, "M"))


def _human_cap(n: float) -> str:
    for threshold, suffix in _CAP_UNITS:
        if abs(n) >= threshold:
            return f"{n / threshold:.2f}{suffix}"
    return f"{n:,.0f}"


def _format_industry(row) -> str:
    qualified = pd.notna(row.get("estimate_coverage")) and row.get("estimate_coverage", 0) >= SECTOR_MIN_ESTIMATE_COVERAGE
    trend = ("📈" if row["earnings_growth_estimate_1y"] > 0 else "📉") if qualified else "❔"
    line = (
        f"{trend} {row['industry']}: cap {_human_cap(row['market_cap'])}, "
        f"next-yr EPS growth (est.) {row['earnings_growth_estimate_1y']:+.1%}"
    )
    if "estimate_coverage" in row:
        line += f"; coverage {row['estimate_coverage']:.0%}, {int(row['covered_companies'])}/{int(row['sampled_companies'])} companies"
        line += f"; industry weight {row['covered_weight']:.0%}"
        if pd.notna(row.get("analysts_min")):
            line += f"; analysts {int(row['analysts_min'])}–{int(row['analysts_max'])}/company"
    return line


def score() -> SubSignal:
    """Pass if at least SECTOR_GROWTH_QUORUM of the top SECTOR_TOP_N industries (by market cap) have positive forecast EPS growth."""
    df = sector_performance()
    qualified = df["earnings_growth_estimate_1y"].notna()
    if "estimate_coverage" in df:
        qualified &= df["estimate_coverage"] >= SECTOR_MIN_ESTIMATE_COVERAGE
    else:
        qualified[:] = False
    growing = int(((df["earnings_growth_estimate_1y"] > 0) & qualified).sum())
    unknown = len(df) - int(qualified.sum())
    if growing < SECTOR_GROWTH_QUORUM <= growing + unknown:
        raise ValueError("Industry earnings quorum indeterminate: insufficient estimate coverage")
    passes = growing >= SECTOR_GROWTH_QUORUM

    state = "growing" if passes else "flat"
    detail = f"{growing}/{SECTOR_TOP_N} top industries with positive forecast EPS growth | " + " | ".join(
        _format_industry(row) for _, row in df.iterrows()
    )

    return SubSignal(
        "sector", growing / SECTOR_TOP_N, state, detail, passes=passes, history=df.copy(),
        metrics={"growing": growing, "sample_size": len(df), "qualified": int(qualified.sum()), "unknown": unknown},
        footer=f"FY+1 · Estimate coverage ≥{SECTOR_MIN_ESTIMATE_COVERAGE:.0%} of sampled weight · Qualified {int(qualified.sum())}/{len(df)}",
    )
