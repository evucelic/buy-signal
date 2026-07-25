"""Rank industries by market cap and their forward-earnings outlook (#4/#5); cache to CSV.

Industry market cap = sector.overview['market_cap'] * the industry's weight within
that sector — ranks all ~145 industries from ~11 sector-level requests instead of
one request per industry.
"""

from pathlib import Path

import pandas as pd
import yfinance as yf
from yfinance.const import SECTOR_INDUSTY_MAPPING_LC

from collectors.freshness import refreshed_today
from config import SECTOR_TOP_CONSTITUENTS, SECTOR_TOP_N, SECTORS_CSV


def _industry_market_caps() -> pd.DataFrame:
    """Market cap for every industry, ranked descending."""
    rows = []
    for sector_key in SECTOR_INDUSTY_MAPPING_LC:
        sector = yf.Sector(sector_key)
        sector_cap = sector.overview["market_cap"]
        for industry_key, row in sector.industries.iterrows():
            rows.append({
                "industry": row["name"],
                "key": industry_key,
                "market_cap": sector_cap * row["market weight"],
            })
    return pd.DataFrame(rows).sort_values("market_cap", ascending=False).reset_index(drop=True)


def _industry_earnings_growth(industry_key: str) -> float:
    """Market-weighted average next-fiscal-year EPS growth estimate across an industry's top constituents.

    `Ticker.info["earningsGrowth"]` is trailing (last reported quarter vs a year ago) despite the
    name; `Ticker.earnings_estimate`'s "+1y" row is the actual forward analyst consensus.
    """
    top = yf.Industry(industry_key).top_companies
    if top is None:
        return float("nan")
    top = top.head(SECTOR_TOP_CONSTITUENTS)

    weights, growths = [], []
    for symbol, weight in top["market weight"].items():
        estimate = yf.Ticker(symbol).earnings_estimate
        if "growth" not in estimate.columns or "+1y" not in estimate.index:
            continue
        growth = estimate.loc["+1y", "growth"]
        if pd.isna(growth):
            continue
        weights.append(weight)
        growths.append(growth)

    return sum(w * g for w, g in zip(weights, growths)) / sum(weights) if weights else float("nan")


def update_sector_data(filepath: Path = SECTORS_CSV) -> None:
    """Refresh the cached top-N industries (by market cap) and their earnings growth."""
    filepath = Path(filepath)

    try:
        ranked = _industry_market_caps().head(SECTOR_TOP_N).copy()
        ranked["earnings_growth_estimate_1y"] = ranked["key"].apply(_industry_earnings_growth)
    except Exception as exc:
        print(f"Sector data fetch failed ({type(exc).__name__}: {exc}); cache unchanged.")
        return

    filepath.parent.mkdir(parents=True, exist_ok=True)
    ranked.to_csv(filepath, index=False)

    growing = int((ranked["earnings_growth_estimate_1y"] > 0).sum())
    print(f"Sector cache updated: top {len(ranked)} industries, {growing} with positive earnings growth")


def sector_performance(filepath: Path = SECTORS_CSV) -> pd.DataFrame:
    """Return the cached top-N industries with their market cap and earnings growth."""
    return pd.read_csv(filepath)


def should_refresh(filepath: Path = SECTORS_CSV) -> bool:
    """Skip if already refreshed today — industry rankings/earnings estimates move slowly."""
    return not refreshed_today(filepath)


if __name__ == "__main__":
    update_sector_data()
    print(sector_performance().to_string(index=False))
