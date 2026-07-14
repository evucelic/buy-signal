"""Collect FINRA margin statistics (margin debt) and cache to CSV.

TODO: implement. Notes from idea.md:
  - FINRA publishes monthly margin statistics, ~1 month delayed.
  - No clean API; likely scrape/download the table from FINRA's site:
    https://www.finra.org/investors/learn-to-invest/advanced-investing/margin-statistics
  - We care about the trend (decreasing = deleveraging), not the absolute level.
"""

from config import MARGIN_DEBT_CSV


def update_margin_debt_data(filepath=MARGIN_DEBT_CSV):
    raise NotImplementedError(f"Download FINRA margin statistics to {filepath}")


def margin_history(filepath=MARGIN_DEBT_CSV):
    """Return monthly margin debt history (most recent last)."""
    raise NotImplementedError
