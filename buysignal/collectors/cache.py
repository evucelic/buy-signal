"""Atomic CSV publication shared by the new observation caches."""

from pathlib import Path
import tempfile

import pandas as pd


def write_csv(frame: pd.DataFrame, filepath: Path | str, *, index: bool = False) -> None:
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, suffix=".csv", delete=False) as handle:
            temporary = Path(handle.name)
            frame.to_csv(handle, index=index)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
