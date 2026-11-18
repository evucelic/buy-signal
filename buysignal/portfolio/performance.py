"""How the account has done: with and without its deposits, against a benchmark, and where its risk sits.

Pure arithmetic on series handed in; nothing here downloads. Two views of return answer two
questions. The money view (value against what was paid in) says what the owner has. The
time-weighted return strips deposits out, chaining each day's return, so it measures the
investments alone and compares fairly with a benchmark. The benchmark is also bought with the
same deposits on the same days, which answers "what if every euro had gone into it instead".
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

TRADING_DAYS = 252
MIN_RISK_SESSIONS = 60   # fewer common sessions than this say little about volatility or correlation


def flows(deposits, index: pd.DatetimeIndex) -> pd.Series:
    """Net deposits per day of `index`; a deposit on a day without a value lands on the next one."""
    series = pd.Series({pd.Timestamp(day): float(amount) for day, amount in deposits}, dtype=float)
    if series.empty or len(index) == 0:
        return pd.Series(0., index=index)
    positions = index.searchsorted(series.index)
    landed = pd.Series(series.to_numpy(), index=positions).groupby(level=0).sum()
    result = pd.Series(0., index=index)
    for position, amount in landed.items():
        if position < len(index):
            result.iloc[position] += amount
    return result


def time_weighted(nav: pd.Series, deposits) -> pd.Series:
    """Cumulative time-weighted return from the first value, deposits taken out on the day they arrive.

    A day's return is (value − that day's deposits) / previous value − 1, the usual convention when
    flows arrive at the end of a day. Days that start from nothing are skipped.
    """
    nav = nav.dropna()
    flow = flows(deposits, nav.index)
    previous = nav.shift(1)
    daily = ((nav - flow) / previous - 1).where(previous > 0).fillna(0.)
    return (1 + daily).cumprod() - 1


def benchmark_return(prices: pd.Series, index: pd.DatetimeIndex) -> pd.Series:
    """The benchmark's own cumulative return over `index`, from its first day."""
    aligned = prices.sort_index().reindex(index.union(prices.index)).ffill().reindex(index).dropna()
    return aligned / aligned.iloc[0] - 1 if len(aligned) else aligned


def same_deposits_in(prices: pd.Series, deposits, index: pd.DatetimeIndex, start_value: float = 0.) -> pd.Series:
    """What the account would be worth had its starting value and every deposit bought the benchmark that day."""
    aligned = prices.sort_index().reindex(index.union(prices.index)).ffill().reindex(index)
    flow = flows(deposits, index)
    flow.iloc[0] += start_value
    units = (flow / aligned).replace([np.inf, -np.inf], np.nan).fillna(0.).cumsum()
    return (units * aligned).where(aligned.notna())


def window(series: pd.Series, start) -> pd.Series:
    return series[series.index >= start]


def rebase(cumulative: pd.Series) -> pd.Series:
    """A cumulative return restarted at zero on the series' first day (for a chosen range)."""
    if cumulative.empty:
        return cumulative
    return (1 + cumulative) / (1 + cumulative.iloc[0]) - 1


def max_drawdown(cumulative: pd.Series) -> float | None:
    """The deepest fall from a previous high of a cumulative return series."""
    if len(cumulative) < 2:
        return None
    wealth = 1 + cumulative
    return float((wealth / wealth.cummax() - 1).min())


def annualised(cumulative: pd.Series) -> float | None:
    """Yearly rate of a cumulative return; None under a year, where it would only magnify noise."""
    if len(cumulative) < 2:
        return None
    days = (cumulative.index[-1] - cumulative.index[0]).days
    if days < 365:
        return None
    return float((1 + cumulative.iloc[-1]) ** (365.25 / days) - 1)


def risk(closes_eur: dict[str, pd.Series], values: dict[str, float]) -> dict | None:
    """Each holding's share of the portfolio's volatility beside its share of the money.

    Daily returns in euros over the last year of common sessions. A holding's risk share is its
    weight times its covariance with the whole, over the whole's variance; the shares add to 1.
    Effective bets is 1 / Σ share², which is the number of equal, independent holdings that
    would spread risk as evenly.
    """
    symbols = [s for s in values if s in closes_eur and values[s] > 0]
    if len(symbols) < 1:
        return None
    frame = pd.DataFrame({s: closes_eur[s] for s in symbols}).dropna().tail(TRADING_DAYS + 1)
    returns = frame.pct_change().dropna()
    if len(returns) < MIN_RISK_SESSIONS:
        return None
    total = sum(values[s] for s in symbols)
    weights = np.array([values[s] / total for s in symbols])
    cov = returns.cov().to_numpy() * TRADING_DAYS
    variance = float(weights @ cov @ weights)
    if variance <= 0:
        return None
    share = weights * (cov @ weights) / variance
    vol = np.sqrt(np.diag(cov))
    return {
        "symbols": symbols, "weights": dict(zip(symbols, weights.tolist())), "risk": dict(zip(symbols, share.tolist())),
        "volatility": dict(zip(symbols, vol.tolist())), "portfolio_volatility": math.sqrt(variance),
        "correlation": returns.corr(), "effective_bets": float(1 / (share ** 2).sum()) if (share ** 2).sum() else None,
        "sessions": len(returns), "since": returns.index[0],
    }


def in_euros(closes: pd.Series, currency: str, eurusd: pd.Series | None) -> pd.Series | None:
    """A listing's closes in euros, dollars converted at each day's rate."""
    if currency == "EUR":
        return closes
    if currency == "USD" and eurusd is not None and len(eurusd):
        rate = eurusd.sort_index().reindex(closes.index.union(eurusd.index)).ffill().reindex(closes.index)
        return (closes / rate).dropna()
    return None
