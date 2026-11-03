"""SPY drawdown gate with volatility-scaled selloff and trend context."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from buysignal.collectors.entry import get_entry_history
from buysignal.config import (
    INDEX_TICKERS, OPTIONAL_INDEX_TICKERS, MARKET_DIP_THRESHOLD, MARKET_DRAWDOWN_SESSIONS,
    MARKET_VOLATILITY_SESSIONS, MARKET_SELLOFF_SESSIONS,
    MARKET_TREND_SESSIONS, MARKET_TREND_SLOPE_SESSIONS,
)
from buysignal.signals.base import SubSignal, format_table


def analyze_history(history: pd.DataFrame) -> dict:
    """Compute facts using only the rows in this observation snapshot."""
    spy = history["SPY"].dropna()
    needed = MARKET_DRAWDOWN_SESSIONS + 1
    if len(spy) < needed:
        raise ValueError(f"SPY drawdown needs {needed} sessions; have {len(spy)}")
    if (spy <= 0).any() or not np.isfinite(spy).all():
        raise ValueError("SPY prices must be positive and finite")
    latest = float(spy.iloc[-1])
    prior = spy.iloc[-1 - MARKET_DRAWDOWN_SESSIONS:-1]
    peak = float(prior.max())
    # Prefer the most recent occurrence when closing highs tie.
    peak_date = prior.loc[prior == peak].index[-1]
    facts = {
        "price": latest, "peak": peak, "peak_date": str(peak_date.date()),
        "drawdown": min(0.0, latest / peak - 1),
        "observed_at": str(spy.index[-1].date()),
        "daily": latest / float(spy.iloc[-2]) - 1,
        "weekly": latest / float(spy.iloc[-6]) - 1,
        "monthly": latest / float(spy.iloc[-22]) - 1,
        "intensity": None, "daily_volatility": None,
        "ma200": None, "ma_slope": None, "trend": "unknown",
    }
    # Log returns are additive. The 20-return baseline ends at the START of
    # the five-session move, so the shock cannot inflate its own denominator.
    returns = np.log(spy).diff()
    n, window = MARKET_SELLOFF_SESSIONS, MARKET_VOLATILITY_SESSIONS
    baseline = returns.iloc[-n-window:-n]
    if len(baseline.dropna()) == window:
        volatility = float(baseline.std(ddof=1))
        if volatility > 1e-10:
            facts["daily_volatility"] = volatility
            facts["intensity"] = float(math.log(latest / float(spy.iloc[-1-n])) / (volatility * math.sqrt(n)))
    ma = spy.rolling(MARKET_TREND_SESSIONS).mean()
    slope_back = MARKET_TREND_SLOPE_SESSIONS
    if len(spy) >= MARKET_TREND_SESSIONS + slope_back:
        current, previous = float(ma.iloc[-1]), float(ma.iloc[-1-slope_back])
        facts["ma200"], facts["ma_slope"] = current, current / previous - 1
        above, rising = latest >= current, current >= previous
        facts["trend"] = (
            "uptrend" if above and rising else "under_pressure" if rising
            else "recovering" if above else "downtrend"
        )
    return facts


def _changes_table(history: pd.DataFrame) -> str:
    rows = []
    for name in (*INDEX_TICKERS, *OPTIONAL_INDEX_TICKERS):
        if name not in history:
            continue
        closes = history[name].dropna()
        if len(closes) < 22:
            rows.append([name, "n/a", "n/a", "n/a"])
            continue
        latest = float(closes.iloc[-1])
        rows.append([name, *[f"{latest / float(closes.iloc[-1-n]) - 1:+.1%}" for n in (1, 5, 21)]])
    return format_table(["Index", "Daily", "5 sess", "21 sess"], rows)


def score(allow_refresh: bool = True) -> SubSignal:
    history = get_entry_history(allow_refresh=allow_refresh)
    facts = analyze_history(history)
    facts["refresh_failed"] = bool(history.attrs.get("refresh_failed"))
    facts["index_changes"] = {}
    for name in (*INDEX_TICKERS, *OPTIONAL_INDEX_TICKERS):
        if name not in history:
            continue
        closes = history[name].dropna()
        if len(closes) >= 22:
            facts["index_changes"][name] = {
                "changes": [float(closes.iloc[-1] / closes.iloc[-1-n] - 1) for n in (1, 5, 21)],
                "date": str(closes.index[-1].date()),
            }
    drawdown = facts["drawdown"]
    passes = drawdown <= MARKET_DIP_THRESHOLD + 1e-12
    detail = (
        f"SPY ${facts['price']:,.2f}: drawdown {drawdown:+.1%}"
        f" | Prior {MARKET_DRAWDOWN_SESSIONS}-session closing high ${facts['peak']:,.2f} ({facts['peak_date']})"
        f" | Rule: DD63 ≤ {MARKET_DIP_THRESHOLD:.0%}; "
        f"{'met' if passes else 'not met'}"
        f" | SPY daily {facts['daily']:+.1%}; five sessions {facts['weekly']:+.1%}; 21 sessions {facts['monthly']:+.1%}"
    )
    if facts["intensity"] is None:
        detail += " | Five-session selloff intensity unavailable: baseline volatility is zero or history is insufficient"
    else:
        detail += (
            f" | Five-session move {facts['intensity']:+.2f} volatility units; "
            f"prior 20-session daily volatility {facts['daily_volatility']:.2%}"
        )
    dates = {str(history[name].dropna().index[-1].date()) for name in INDEX_TICKERS if history[name].notna().any()}
    if len(dates) > 1:
        detail += " | Index observation dates differ: " + ", ".join(
            f"{name} {history[name].dropna().index[-1].date()}" for name in INDEX_TICKERS if history[name].notna().any()
        )
    if history.attrs.get("refresh_failed"):
        detail += " | Latest refresh failed; using a cached observation for the expected session"
    return SubSignal(
        "market_dip", drawdown, "dip" if passes else "flat", detail, passes=passes,
        table=_changes_table(history), observed_at=facts["observed_at"], metrics=facts,
        history=history.copy(),
        footer="Adjusted daily close · Latest session may be partial",
    )
