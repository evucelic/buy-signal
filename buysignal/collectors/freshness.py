"""Cache age for the "data as of" table. Refresh *timing* lives in schedule/journal.py: an
mtime only records the last successful write, so it can't tell "never attempted" apart from
"attempted hourly and failing".
"""

from datetime import datetime, timezone
from pathlib import Path


def last_modified(filepath: Path | str) -> datetime | None:
    """filepath's mtime as an aware UTC datetime, or None if it doesn't exist yet."""
    filepath = Path(filepath)
    if not filepath.exists():
        return None
    return datetime.fromtimestamp(filepath.stat().st_mtime, tz=timezone.utc)
