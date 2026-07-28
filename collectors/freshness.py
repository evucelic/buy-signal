"""Shared once-a-day refresh gate for the macro collectors (fed_rate, sectors, margin_debt)."""

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


def refreshed_today(filepath: Path | str, today: pd.Timestamp | None = None) -> bool:
    """Whether filepath's mtime falls on today's date (local time)."""
    filepath = Path(filepath)
    if not filepath.exists():
        return False
    today = pd.Timestamp(today) if today is not None else pd.Timestamp.now()
    return datetime.fromtimestamp(filepath.stat().st_mtime).date() == today.date()


def last_modified(filepath: Path | str) -> datetime | None:
    """filepath's mtime as an aware UTC datetime, or None if it doesn't exist yet."""
    filepath = Path(filepath)
    if not filepath.exists():
        return None
    return datetime.fromtimestamp(filepath.stat().st_mtime, tz=timezone.utc)
