"""Rank industries by market cap and their forward-earnings outlook (#4/#5); cache to CSV.

Industry market cap = sector.overview['market_cap'] * the industry's weight within
that sector; ranks all ~145 industries from ~11 sector-level requests instead of
one request per industry.
"""

import logging
import time
import math
from pathlib import Path

import pandas as pd
import yfinance as yf
from yfinance.const import SECTOR_INDUSTY_MAPPING_LC

from buysignal.config import SECTOR_TOP_CONSTITUENTS, SECTOR_TOP_N, SECTORS_CSV, SECTOR_MIN_ESTIMATE_COVERAGE, SECTOR_MAX_CACHE_AGE_HOURS
from buysignal.collectors.cache import write_csv

log = logging.getLogger(__name__)


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


def _industry_earnings_data(industry_key: str) -> dict:
    top = yf.Industry(industry_key).top_companies
    result = {"earnings_growth_estimate_1y": float("nan"), "sampled_companies": 0,
              "covered_companies": 0, "sampled_weight": 0., "covered_weight": 0.,
              "estimate_coverage": 0., "analysts_min": float("nan"), "analysts_max": float("nan")}
    if top is None or top.empty:
        return result
    top = top.loc[~top.index.duplicated()].head(SECTOR_TOP_CONSTITUENTS)
    weights, growths, counts = [], [], []
    sampled_weight = 0.
    for symbol, raw_weight in top["market weight"].items():
        weight = float(raw_weight)
        if not math.isfinite(weight) or weight <= 0:
            continue
        sampled_weight += weight
        try:
            estimate = yf.Ticker(symbol).earnings_estimate
            if "growth" not in estimate.columns or "+1y" not in estimate.index:
                continue
            growth = float(estimate.loc["+1y", "growth"])
            if not math.isfinite(growth):
                continue
            analyst_count = estimate.loc["+1y"].get("numberOfAnalysts")
            if pd.notna(analyst_count) and float(analyst_count) <= 0:
                continue
            weights.append(weight)
            growths.append(growth)
            if pd.notna(analyst_count):
                counts.append(int(analyst_count))
        except Exception as exc:
            log.warning("EPS estimate missing for %s (%s)", symbol, type(exc).__name__)
    covered_weight = sum(weights)
    result.update(sampled_companies=len(top), covered_companies=len(weights),
                  sampled_weight=sampled_weight, covered_weight=covered_weight,
                  estimate_coverage=covered_weight/sampled_weight if sampled_weight else 0.)
    if covered_weight:
        result["earnings_growth_estimate_1y"] = sum(w*g for w,g in zip(weights, growths))/covered_weight
    if counts:
        result.update(analysts_min=min(counts), analysts_max=max(counts))
    return result


def _industry_earnings_growth(industry_key: str) -> float:
    """Compatibility helper; current snapshots also retain constituent coverage."""
    return _industry_earnings_data(industry_key)["earnings_growth_estimate_1y"]


def update_sector_data(filepath: Path = SECTORS_CSV) -> str | None:
    """Refresh the cached top-N industries (by market cap) and their earnings growth.

    Returns an error message, or None on success.
    """
    filepath = Path(filepath)

    try:
        ranked = _industry_market_caps().head(SECTOR_TOP_N).copy()
        if len(ranked) != SECTOR_TOP_N or ranked["key"].duplicated().any():
            raise ValueError("Industry ranking is incomplete or duplicated")
        if not ranked["market_cap"].map(lambda v: math.isfinite(float(v)) and float(v) > 0).all():
            raise ValueError("Invalid industry market caps")
        estimates = pd.DataFrame([_industry_earnings_data(key) for key in ranked["key"]], index=ranked.index)
        ranked = pd.concat([ranked, estimates], axis=1)
        valid = (ranked.estimate_coverage >= SECTOR_MIN_ESTIMATE_COVERAGE) & ranked.earnings_growth_estimate_1y.notna()
        if int(valid.sum()) < 3:
            raise ValueError("Fewer than three industries meet the estimate-coverage minimum")
        ranked["fetched_at"] = pd.Timestamp.now(tz="UTC").isoformat()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        log.error("Sector data fetch failed (%s); cache unchanged.", error)
        return error

    filepath.parent.mkdir(parents=True, exist_ok=True)
    history_path = filepath.with_name(f"{filepath.stem}_history.csv")
    try:
        history = pd.read_csv(history_path)
    except (OSError, ValueError):
        history = pd.DataFrame()
    archived = pd.concat([history, ranked], ignore_index=True) if not history.empty else ranked
    write_csv(archived, history_path)
    write_csv(ranked, filepath)

    growing = int((ranked["earnings_growth_estimate_1y"] > 0).sum())
    log.info("Sector cache updated: top %d industries, %d with positive earnings growth", len(ranked), growing)
    return None


def sector_performance(filepath: Path = SECTORS_CSV) -> pd.DataFrame:
    """Return the cached top-N industries with their market cap and earnings growth."""
    filepath = Path(filepath)
    if time.time() - filepath.stat().st_mtime > SECTOR_MAX_CACHE_AGE_HOURS * 3600:
        raise ValueError("Industry estimate cache exceeds 96h")
    return pd.read_csv(filepath)


def demands_refresh(filepath: Path | str = SECTORS_CSV) -> bool:
    try:
        return "estimate_coverage" not in sector_performance(filepath).columns
    except (OSError, ValueError):
        return True



if __name__ == "__main__":
    from buysignal import logs

    logs.configure()
    update_sector_data()
    print(sector_performance().to_string(index=False))
