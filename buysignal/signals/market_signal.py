"""SPY drawdown (the dip) with trend context."""

from __future__ import annotations

import numpy as np
import pandas as pd

from buysignal.analysis.context import share_at_or_below
from buysignal.collectors.entry import get_entry_history
from buysignal.config import (
    INDEX_TICKERS, OPTIONAL_INDEX_TICKERS, MARKET_DIP_THRESHOLD, MARKET_DRAWDOWN_SESSIONS,
    MARKET_TREND_SESSIONS, MARKET_TREND_SLOPE_SESSIONS,
)
from buysignal.signals.base import SubSignal


def drawdown_series(closes: pd.Series) -> pd.Series:
    """Distance below the highest close of the prior sessions, zero when at a new high."""
    peak = closes.shift(1).rolling(MARKET_DRAWDOWN_SESSIONS).max()
    return (closes / peak - 1).clip(upper=0).dropna()


def analyze_series(closes: pd.Series, name: str = "SPY") -> dict:
    """Drawdown and trend facts for one index, from its own sessions only."""
    closes = closes.dropna()
    needed = MARKET_DRAWDOWN_SESSIONS + 1
    if len(closes) < needed:
        raise ValueError(f"{name} drawdown needs {needed} sessions; have {len(closes)}")
    if (closes <= 0).any() or not np.isfinite(closes).all():
        raise ValueError(f"{name} prices must be positive and finite")
    latest = float(closes.iloc[-1])
    prior = closes.iloc[-1 - MARKET_DRAWDOWN_SESSIONS:-1]
    peak = float(prior.max())
    # Prefer the most recent occurrence when closing highs tie.
    peak_date = prior.loc[prior == peak].index[-1]
    facts = {
        "price": latest, "peak": peak, "peak_date": str(peak_date.date()),
        "drawdown": min(0.0, latest / peak - 1),
        "observed_at": str(closes.index[-1].date()),
        "daily": latest / float(closes.iloc[-2]) - 1,
        "weekly": latest / float(closes.iloc[-6]) - 1,
        "monthly": latest / float(closes.iloc[-22]) - 1,
        "ma200": None, "ma_slope": None, "trend": "unknown",
    }
    ma = closes.rolling(MARKET_TREND_SESSIONS).mean()
    slope_back = MARKET_TREND_SLOPE_SESSIONS
    if len(closes) >= MARKET_TREND_SESSIONS + slope_back:
        current, previous = float(ma.iloc[-1]), float(ma.iloc[-1-slope_back])
        facts["ma200"], facts["ma_slope"] = current, current / previous - 1
        above, rising = latest >= current, current >= previous
        facts["trend"] = (
            "uptrend" if above and rising else "under_pressure" if rising
            else "recovering" if above else "downtrend"
        )
    # How unusual this distance below the high has been in this index's own past.
    past = drawdown_series(closes)
    facts["history_since"] = str(past.index[0].date()) if len(past) else None
    facts["drawdown_share"] = share_at_or_below(past, facts["drawdown"]) if facts["drawdown"] < 0 else None
    facts["rule_share"] = share_at_or_below(past, MARKET_DIP_THRESHOLD)
    return facts


def analyze_history(history: pd.DataFrame) -> dict:
    """Compute facts using only the rows in this observation snapshot."""
    return analyze_series(history["SPY"], "SPY")


def score(allow_refresh: bool = True) -> SubSignal:
    history = get_entry_history(allow_refresh=allow_refresh)
    facts = analyze_history(history)
    facts["refresh_failed"] = bool(history.attrs.get("refresh_failed"))
    facts["stale"] = bool(history.attrs.get("stale"))
    facts["expected_session"] = history.attrs.get("expected_session")
    facts["retrieved_at"] = history.attrs.get("retrieved_at")
    facts["index_changes"] = {}
    facts["index_facts"] = {}
    for name in (*INDEX_TICKERS, *OPTIONAL_INDEX_TICKERS):
        if name not in history:
            continue
        closes = history[name].dropna()
        if len(closes) >= 22:
            facts["index_changes"][name] = {
                "changes": [float(closes.iloc[-1] / closes.iloc[-1-n] - 1) for n in (1, 5, 21)],
                "date": str(closes.index[-1].date()),
            }
        try:
            # Each index is described from its own sessions; the dip line is defined for SPY only.
            facts["index_facts"][name] = analyze_series(closes, name)
        except ValueError:
            continue
    drawdown = facts["drawdown"]
    condition_met = drawdown <= MARKET_DIP_THRESHOLD + 1e-12
    passes = condition_met and not facts["stale"]
    detail = (
        f"SPY ${facts['price']:,.2f}: drawdown {drawdown:+.1%}"
        f" | Prior {MARKET_DRAWDOWN_SESSIONS}-session closing high ${facts['peak']:,.2f} ({facts['peak_date']})"
        f" | Rule: DD63 ≤ {MARKET_DIP_THRESHOLD:.0%}; "
        f"{'met in cached observation' if condition_met and facts['stale'] else 'met' if passes else 'not met'}"
        f" | SPY daily {facts['daily']:+.1%}; five sessions {facts['weekly']:+.1%}; 21 sessions {facts['monthly']:+.1%}"
    )
    dates = {str(history[name].dropna().index[-1].date()) for name in INDEX_TICKERS if history[name].notna().any()}
    if len(dates) > 1:
        detail += " | Index observation dates differ: " + ", ".join(
            f"{name} {history[name].dropna().index[-1].date()}" for name in INDEX_TICKERS if history[name].notna().any()
        )
    if history.attrs.get("refresh_failed"):
        detail += " | Latest refresh failed; showing the last validated prices"
    if facts["stale"]:
        detail += f" | Cached through {facts['observed_at']}; expected {facts['expected_session']}"
    return SubSignal(
        "market_dip", drawdown, "dip" if passes else "flat", detail, passes=passes,
        observed_at=facts["observed_at"], metrics=facts, history=history.copy(),
    )
