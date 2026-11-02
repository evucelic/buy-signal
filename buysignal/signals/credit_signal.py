"""Credit risk appetite: extra yield demanded for lending to riskier US companies."""

from __future__ import annotations

import pandas as pd

from buysignal.collectors.credit import credit_history
from buysignal.config import (
    CREDIT_CHANGE_SESSIONS, CREDIT_ELEVATED_SPREAD, CREDIT_CHANGE_NOISE_BP,
    CREDIT_MAX_OBSERVATION_AGE_DAYS,
)
from buysignal.signals.base import SubSignal, format_table


def score(asof: pd.Timestamp | None = None) -> SubSignal:
    history = credit_history()
    today = asof if asof is not None else pd.Timestamp.now(tz="America/New_York").tz_localize(None)
    if today.tzinfo is not None:
        today = today.tz_convert("America/New_York").tz_localize(None)
    latest = history.iloc[-1]
    age = (today.normalize() - latest.date.normalize()).days
    if age < 0 or age > CREDIT_MAX_OBSERVATION_AGE_DAYS:
        raise ValueError(f"Credit observation {latest.date.date()} is outside the freshness budget")
    spread = float(latest.spread)
    changes = {}
    for n in CREDIT_CHANGE_SESSIONS:
        changes[n] = (spread - float(history.spread.iloc[-1-n])) * 100 if len(history) > n else None
    monthly = changes[CREDIT_CHANGE_SESSIONS[-1]]
    state = "unknown" if monthly is None else (
        "widening" if monthly >= CREDIT_CHANGE_NOISE_BP else
        "narrowing" if monthly <= -CREDIT_CHANGE_NOISE_BP else "stable"
    )
    level = "elevated" if spread >= CREDIT_ELEVATED_SPREAD else "below the elevated threshold"
    detail = (
        f"High-yield option-adjusted spread {spread:.2f} percentage points ({spread*100:.0f} bp); {level}"
        f" | Elevated ≥{CREDIT_ELEVATED_SPREAD:.1f}pp; direction band ±{CREDIT_CHANGE_NOISE_BP:.0f}bp / 21 observations"
    )
    rows = [[f"{n} obs", f"{v:+.0f} bp" if v is not None else "n/a"] for n, v in changes.items()]
    return SubSignal(
        "credit", spread, state, detail, advisory=True,
        table=format_table(["Change over", "Spread change"], rows),
        observed_at=str(latest.date.date()),
        metrics={"spread": spread, "changes_bp": changes, "elevated": spread >= CREDIT_ELEVATED_SPREAD},
        history=history.copy(),
        footer="ICE BofA / FRED · HY OAS · Daily close",
    )
