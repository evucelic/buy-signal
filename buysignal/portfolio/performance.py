"""Return arithmetic on series handed in: money view, time-weighted return, benchmarks and risk."""
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
    """Cumulative time-weighted return, each day's deposits taken out at that day's close."""
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
    """Value had the starting value and every deposit bought the benchmark that day."""
    aligned = prices.sort_index().reindex(index.union(prices.index)).ffill().reindex(index)
    flow = flows(deposits, index)
    flow.iloc[0] += start_value
    units = (flow / aligned).replace([np.inf, -np.inf], np.nan).fillna(0.).cumsum()
    return (units * aligned).where(aligned.notna())


def same_deposits_in_mix(closes: dict[str, pd.Series], weights: dict[str, float], deposits, index: pd.DatetimeIndex,
                         start_value: float = 0.) -> pd.Series | None:
    """Value had every deposit been split by the target weights that day; no rebalancing, cash kept as cash.

    None when a weighted listing has no prices.
    """
    total = sum(weights.values())
    if total <= 0:
        return None
    value = pd.Series(0., index=index)
    for symbol, weight in weights.items():
        share = weight / total
        if symbol == "CASH":
            flow = flows(deposits, index)
            flow.iloc[0] += start_value
            value = value + (flow * share).cumsum()
            continue
        prices = closes.get(symbol)
        if prices is None or prices.dropna().empty:
            return None
        part = same_deposits_in(prices, [(day, amount * share) for day, amount in deposits], index, start_value * share)
        value = value + part
    return value.dropna()


def money_weighted(flows_in: list[tuple], final_value: float, final_day) -> float | None:
    """The investor's own return: the rate that grows every flow into the final value.

    Per year from one year on, else over the period.
    """
    flows_in = [(pd.Timestamp(day), float(amount)) for day, amount in flows_in if amount]
    end = pd.Timestamp(final_day)
    if not flows_in or final_value <= 0:
        return None
    first = min(day for day, _ in flows_in)
    days = (end - first).days
    if days <= 0:
        return None

    def surplus(rate):
        return sum(amount * (1 + rate) ** ((end - day).days / 365.25) for day, amount in flows_in) - final_value
    low, high = -.99, 10.
    if surplus(low) * surplus(high) > 0:
        return None
    for _ in range(100):
        middle = (low + high) / 2
        if surplus(low) * surplus(middle) <= 0:
            high = middle
        else:
            low = middle
    yearly = (low + high) / 2
    return yearly if days >= 365 else (1 + yearly) ** (days / 365.25) - 1


def drawdown(cumulative: pd.Series) -> pd.Series:
    wealth = 1 + cumulative
    return wealth / wealth.cummax() - 1


def volatility(cumulative: pd.Series) -> float | None:
    """Yearly standard deviation of daily returns; None with fewer than 40 days."""
    daily = (1 + cumulative).pct_change().dropna()
    return float(daily.std() * math.sqrt(TRADING_DAYS)) if len(daily) >= 40 else None


def window(series: pd.Series, start) -> pd.Series:
    return series[series.index >= start]


def rebase(cumulative: pd.Series) -> pd.Series:
    """A cumulative return restarted at zero on the series' first day (for a chosen range)."""
    if cumulative.empty:
        return cumulative
    return (1 + cumulative) / (1 + cumulative.iloc[0]) - 1


def max_drawdown(cumulative: pd.Series) -> float | None:
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
    """Each holding's share of the money and of the portfolio's variance, over the last year in euros.

    Risk share = weight x covariance with the whole / the whole's variance; effective bets = 1 / sum(share^2).
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
