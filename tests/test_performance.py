"""Returns with and without deposits, the benchmark comparison and where the risk sits."""
import numpy as np
import pandas as pd
import pytest

from buysignal.portfolio import performance

DAYS = pd.bdate_range("2026-01-05", periods=5)


def test_a_deposit_is_not_counted_as_a_gain():
    value = pd.Series([1000., 1010., 2010., 2030.1, 2030.1], index=DAYS)  # +1%, a 1,000 deposit, +1%, flat

    twr = performance.time_weighted(value, [(DAYS[2].date(), 1000)])

    assert twr.iloc[-1] == pytest.approx(1.01 * 1.01 - 1)


def test_a_deposit_on_a_day_without_a_value_counts_on_the_next_one():
    flows = performance.flows([(pd.Timestamp("2026-01-04").date(), 500)], DAYS)  # a Sunday

    assert flows.tolist() == [500, 0, 0, 0, 0]


def test_the_benchmark_is_bought_with_the_same_deposits_on_the_same_days():
    prices = pd.Series([100., 100., 50., 50., 100.], index=DAYS)

    value = performance.same_deposits_in(prices, [(DAYS[0].date(), 1000), (DAYS[2].date(), 1000)], DAYS)

    assert value.iloc[-1] == pytest.approx(10 * 100 + 20 * 100)  # 10 units at 100, then 20 at 50


def test_returns_restart_at_zero_for_a_chosen_range():
    cumulative = pd.Series([0., .10, .21], index=DAYS[:3])

    assert performance.rebase(cumulative.iloc[1:]).tolist() == pytest.approx([0, .1])


def test_the_worst_fall_is_measured_from_the_previous_high():
    assert performance.max_drawdown(pd.Series([0., .2, -.04, .1], index=DAYS[:4])) == pytest.approx(.96 / 1.2 - 1)


def test_no_yearly_rate_under_a_year():
    assert performance.annualised(pd.Series([0., .05], index=DAYS[:2])) is None


def test_risk_shares_add_up_and_sit_with_the_volatile_holding():
    rng = np.random.default_rng(1)
    days = pd.bdate_range("2025-01-01", periods=300)
    calm = pd.Series(100 * np.cumprod(1 + rng.normal(0, .003, 300)), index=days)
    wild = pd.Series(100 * np.cumprod(1 + rng.normal(0, .02, 300)), index=days)

    result = performance.risk({"CALM": calm, "WILD": wild}, {"CALM": 5000, "WILD": 5000})

    assert sum(result["risk"].values()) == pytest.approx(1)
    assert result["risk"]["WILD"] > .9 and result["weights"]["WILD"] == pytest.approx(.5)
    assert 1 < result["effective_bets"] < 1.2


def test_too_little_shared_history_measures_no_risk():
    short = pd.Series(np.linspace(100, 110, 20), index=pd.bdate_range("2026-01-01", periods=20))

    assert performance.risk({"A": short}, {"A": 100}) is None


def test_dollar_closes_are_converted_at_each_days_rate():
    closes = pd.Series([110., 110.], index=DAYS[:2])
    eurusd = pd.Series([1.10, 1.00], index=DAYS[:2])

    assert performance.in_euros(closes, "USD", eurusd).tolist() == pytest.approx([100, 110])
