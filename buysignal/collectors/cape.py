"""Robert Shiller's monthly CAPE and excess CAPE yield since 1881, from ie_data.xls at shillerdata.com.

Excess CAPE yield: 1/CAPE minus the real 10-year Treasury yield (GS10 less the past ten years' inflation).
"""
from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
import logging
from pathlib import Path
import re

import pandas as pd
import requests

from buysignal.collectors.cache import write_csv
from buysignal.config import CAPE_CSV, SHILLER_DATA_URL

log = logging.getLogger(__name__)

TIMEOUT_SEC = 30
MAX_AGE_DAYS = 7  # Shiller updates monthly; a week-old copy is as current as the source
_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}
_WORKBOOK = re.compile(r'href="((?:https?:)?//[^"]+/ie_data\.xls[^"]*)"')
# Columns of the "Data" sheet (0-based): date, CAPE, excess CAPE yield, next-10-year real returns.
_COLUMNS = {0: "month", 12: "cape", 16: "excess_yield", 19: "stocks_10y", 20: "bonds_10y"}
COLUMNS = ["date", "cape", "excess_yield", "stocks_10y", "bonds_10y"]


def _month(value: float) -> pd.Timestamp:
    """Shiller writes October 2026 as 2026.1 (2026.10 read as a number)."""
    year = int(value)
    return pd.Timestamp(year, int(round((value - year) * 100)), 1)


def parse(content: bytes) -> pd.DataFrame:
    return table(pd.read_excel(BytesIO(content), sheet_name="Data", header=None, skiprows=8, engine="xlrd"))


def table(sheet: pd.DataFrame) -> pd.DataFrame:
    frame = sheet[list(_COLUMNS)].rename(columns=_COLUMNS).apply(pd.to_numeric, errors="coerce")
    frame = frame[frame.month.notna() & frame.cape.notna()]
    frame.insert(0, "date", frame.pop("month").map(_month))
    if frame.empty or frame.date.duplicated().any() or not frame.date.is_monotonic_increasing:
        raise ValueError("CAPE months are missing, repeated or out of order")
    if not frame.cape.between(3, 100).all() or frame.date.iloc[0].year > 1900:
        raise ValueError("CAPE values or the history's start are implausible")
    return frame[COLUMNS].reset_index(drop=True)


def history(filepath: Path | str = CAPE_CSV) -> pd.DataFrame:
    frame = pd.read_csv(filepath, parse_dates=["date"])
    if list(frame.columns) != COLUMNS or frame.empty:
        raise ValueError("CAPE history is empty or malformed")
    return frame


def update_cape_data(filepath: Path | str = CAPE_CSV, *, session=None) -> str | None:
    path = Path(filepath)
    if path.exists():
        age = datetime.now(timezone.utc).timestamp() - path.stat().st_mtime
        if age < MAX_AGE_DAYS * 86400:
            return None
    session = session or requests.Session()
    try:
        page = session.get(SHILLER_DATA_URL, headers=_HEADERS, timeout=TIMEOUT_SEC)
        page.raise_for_status()
        link = _WORKBOOK.search(page.text)
        if not link:
            return "Shiller data page has no ie_data.xls link"
        url = link.group(1)
        workbook = session.get(("https:" + url) if url.startswith("//") else url, headers=_HEADERS, timeout=TIMEOUT_SEC)
        workbook.raise_for_status()
        new = parse(workbook.content)
        try:
            old = history(path)
            if new.date.iloc[-1] < old.date.iloc[-1]:
                return f"Shiller data ends {new.date.iloc[-1]:%b %Y}, before the stored {old.date.iloc[-1]:%b %Y}"
        except (OSError, ValueError):
            pass
        write_csv(new, path)
    except requests.RequestException as exc:
        return f"Shiller data download failed ({type(exc).__name__})"
    except (ValueError, KeyError) as exc:
        return f"Invalid Shiller data: {exc}"
    return None


def demands_refresh(filepath: Path | str = CAPE_CSV) -> bool:
    return not Path(filepath).exists()


def summary(frame: pd.DataFrame, band: float = .01) -> dict:
    """Today's CAPE and excess yield against history, and the next ten years' real returns after months within `band` of today's yield.

    Overlapping windows inflate the month count, so the number of distinct decades is reported too.
    """
    latest = frame.dropna(subset=["cape", "excess_yield"]).iloc[-1]
    cape, excess = float(latest.cape), float(latest.excess_yield)
    earnings = 1 / cape
    done = frame.dropna(subset=["excess_yield", "stocks_10y", "bonds_10y"])
    near = done[(done.excess_yield - excess).abs() <= band]
    outcome = None
    if len(near) >= 24:
        stocks, over = near.stocks_10y, near.stocks_10y - near.bonds_10y
        outcome = {"months": len(near), "decades": len({year // 10 for year in near.date.dt.year}),
                   "stocks": stocks.quantile([.1, .5, .9]).tolist(), "over_bonds": over.quantile([.1, .5, .9]).tolist()}
    return {
        "date": latest.date, "cape": cape, "earnings_yield": earnings, "real_bond": earnings - excess, "excess_yield": excess,
        "cape_rank": float((frame.cape < cape).mean()), "cape_median": float(frame.cape.median()),
        "excess_median": float(frame.excess_yield.median()), "excess_rank": float((frame.excess_yield.dropna() < excess).mean()),
        "since": frame.date.iloc[0], "band": band, "outcome": outcome,
        "correlation": float(done.excess_yield.corr(done.stocks_10y - done.bonds_10y)) if len(done) > 24 else None,
    }
