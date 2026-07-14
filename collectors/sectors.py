"""Collect sector ETF performance + earnings data via yfinance, cache to CSV.

TODO: implement. Used by indicators #4 (clear leading sector) and
#5 (that sector's earnings trajectory).
  - Relative strength: pull SECTOR_ETFS closes, compare trailing returns.
  - Earnings trajectory: yfinance Ticker(...).get_earnings() / income stmt,
    or a fundamentals API if yfinance coverage is too thin.
"""

from config import SECTOR_ETFS, SECTORS_CSV


def update_sector_data(filepath=SECTORS_CSV):
    raise NotImplementedError(f"Fetch {len(SECTOR_ETFS)} sector ETFs to {filepath}")


def sector_performance(filepath=SECTORS_CSV):
    """Return trailing returns per sector (to find the leader)."""
    raise NotImplementedError
