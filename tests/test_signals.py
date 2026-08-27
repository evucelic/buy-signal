"""Tests for signals/*.py: every state-machine branch, both passing and failing, per module."""

from unittest.mock import patch

import pandas as pd
import pytest

from signals import margin_signal, market_signal, rate_signal, sector_signal, vix_signal, yield_curve_signal
from config import MARKET_DIP_THRESHOLD, MARKET_GROWTH_THRESHOLD, YIELD_CURVE_BUCKET_DAYS
from signals.base import NONE, SOFT, STRONG, SubSignal
from signals.buy_signal import SIGNAL_LOCK, _alert_state, compute_signal

# --- vix_signal ---------------------------------------------------------------

_CACHED_VIX = 18.0
_LIVE_VIX = 31.0


def _vix(vix, change=None):
    with patch("signals.vix_signal.vix_change_pct", return_value=change):
        return vix_signal.score(vix=vix)


def _vix_sources(monkeypatch, cached=_CACHED_VIX, live=_LIVE_VIX):
    monkeypatch.setattr("signals.vix_signal.load_latest_cached_vix", lambda: cached)
    monkeypatch.setattr("signals.vix_signal.get_latest_vix", lambda: live)
    monkeypatch.setattr("signals.vix_signal.vix_change_pct", lambda: None)


def test_vix_strong_passes():
    s = _vix(30.0)
    assert s.state == STRONG
    assert s.passes is True


def test_vix_soft_boundary_passes():
    s = _vix(22.0)
    assert s.state == SOFT
    assert s.passes is True


def test_vix_calm_fails():
    s = _vix(15.0)
    assert s.state == NONE
    assert s.passes is False


def test_vix_optimistic_low_fails():
    s = _vix(5.0)
    assert s.state == NONE
    assert s.passes is False
    assert "optimistic" in s.detail


def test_vix_reports_the_live_level_when_refresh_is_allowed(monkeypatch):
    _vix_sources(monkeypatch)

    s = vix_signal.score(allow_refresh=True)

    assert s.score == _LIVE_VIX
    assert s.state == STRONG


def test_vix_reports_the_cached_level_when_refresh_is_disallowed(monkeypatch):
    _vix_sources(monkeypatch)

    s = vix_signal.score(allow_refresh=False)

    assert s.score == _CACHED_VIX
    assert s.state == NONE


def test_vix_reports_the_live_level_when_refresh_is_disallowed_but_the_cache_is_empty(monkeypatch):
    _vix_sources(monkeypatch, cached=None)

    s = vix_signal.score(allow_refresh=False)

    assert s.score == _LIVE_VIX


def test_vix_reports_an_explicitly_supplied_level_over_either_source(monkeypatch):
    _vix_sources(monkeypatch)

    s = vix_signal.score(vix=22.0)

    assert s.score == 22.0
    assert s.state == SOFT


def test_vix_change_suffix_present_vs_absent():
    assert "vs prior close" in _vix(15.0, change=0.05).detail
    assert "vs prior close" not in _vix(15.0, change=None).detail


def test_vix_just_below_strong_is_soft_not_strong():
    s = _vix(29.9)
    assert s.state == SOFT
    assert s.passes is True


def test_vix_just_below_soft_is_calm_not_soft():
    s = _vix(21.9)
    assert s.state == NONE
    assert s.passes is False


def test_vix_optimistic_boundary_exact():
    assert "optimistic" in _vix(10.0).detail


def test_vix_just_above_optimistic_is_plain_calm():
    s = _vix(10.1)
    assert "optimistic" not in s.detail
    assert "calm" in s.detail


# --- rate_signal ---------------------------------------------------------------


def _fedwatch(*, ease, no_change, hike):
    return pd.DataFrame(
        [
            {
                "meeting_date": meeting,
                "horizon": horizon,
                "prob_ease": ease,
                "prob_no_change": no_change,
                "prob_hike": hike,
            }
            for horizon, meeting in [
                ("nearest", pd.Timestamp("2026-08-01")),
                ("six_month", pd.Timestamp("2027-01-01")),
                ("one_year", pd.Timestamp("2027-07-01")),
            ]
        ]
    )


def _rate(expectations):
    with patch("signals.rate_signal.latest_fedwatch", return_value=expectations):
        return rate_signal.score()


def test_rate_hiking_fails():
    s = _rate(_fedwatch(ease=0.0, no_change=0.3, hike=0.7))
    assert s.state == "hiking"
    assert s.passes is False


def test_rate_cutting_passes():
    s = _rate(_fedwatch(ease=0.7, no_change=0.3, hike=0.0))
    assert s.state == "cutting"
    assert s.passes is True


def test_rate_no_change_passes():
    s = _rate(_fedwatch(ease=0.2, no_change=0.6, hike=0.2))
    assert s.state == "no_change"
    assert s.passes is True


def test_rate_falls_back_to_the_last_meeting_when_the_cache_has_no_horizon_column():
    without_horizons = pd.DataFrame(
        [
            {"meeting_date": pd.Timestamp("2026-08-01"), "prob_ease": 0.1, "prob_no_change": 0.2, "prob_hike": 0.7},
            {"meeting_date": pd.Timestamp("2027-07-01"), "prob_ease": 0.0, "prob_no_change": 0.9, "prob_hike": 0.1},
        ]
    )

    assert _rate(without_horizons).state == "no_change"


def test_rate_tie_between_the_two_leading_probabilities_is_flat():
    s = _rate(_fedwatch(ease=0.0, no_change=0.5, hike=0.5))
    assert s.state == "flat"
    assert s.passes is True


def test_rate_three_way_tie_at_zero_is_flat():
    assert _rate(_fedwatch(ease=0.0, no_change=0.0, hike=0.0)).state == "flat"


def test_rate_carries_its_horizons_in_the_detail_string_instead_of_a_table():
    s = _rate(_fedwatch(ease=0.0, no_change=0.3, hike=0.7))
    assert s.table is None
    assert "nearest" in s.detail
    assert "hike 70.0%" in s.detail


# --- margin_signal ---------------------------------------------------------------


def _margin_history(balances):
    return pd.DataFrame(
        {
            "month": pd.date_range("2026-01-01", periods=len(balances), freq="MS"),
            "debit_balances": balances,
        }
    )


def _margin(balances, freshness=("fresh", "ok")):
    with patch("signals.margin_signal.margin_history", return_value=_margin_history(balances)), patch(
        "signals.margin_signal.data_freshness", return_value=freshness
    ):
        return margin_signal.score()


def test_margin_deleveraging_passes():
    s = _margin([1000, 900, 800])
    assert s.state == "deleveraging"
    assert s.passes is True


def test_margin_leveraging_fails():
    s = _margin([800, 900, 1000])
    assert s.state == "leveraging"
    assert s.passes is False


def test_margin_single_month_cannot_show_a_decrease():
    s = _margin([1000])
    assert s.passes is False
    assert s.score == 0.0


def test_margin_zero_change_is_not_deleveraging():
    s = _margin([1000, 1000])
    assert s.passes is False
    assert s.score == 0.0


def test_margin_a_decrease_followed_by_an_increase_is_not_deleveraging(monkeypatch):
    monkeypatch.setattr("signals.margin_signal.MARGIN_DELEVERAGE_MONTHS", 2)

    assert _margin([1000, 900, 950]).passes is False


def test_margin_detail_omits_the_freshness_flag_when_the_data_is_current():
    s = _margin([1000, 900], freshness=("fresh", "latest month 2026-07 is current"))
    assert "|" not in s.detail
    assert "2026-07" not in s.detail


def test_margin_detail_flags_refresh_due_soon_without_the_raw_message():
    s = _margin(
        [1000, 900],
        freshness=("refresh_due_soon", "latest month 2026-07 is 2 months old and the release checkpoint has passed"),
    )
    assert "refresh due soon" in s.detail
    assert "2026-07" not in s.detail


def test_margin_detail_flags_stale_without_the_raw_message():
    s = _margin([1000, 900], freshness=("stale", "way overdue"))
    assert "stale" in s.detail
    assert "way overdue" not in s.detail


# --- market_signal ---------------------------------------------------------------

_CACHED_DAILY = -0.01
_LIVE_DAILY = 0.01


def _market_changes(spy_daily):
    return {
        "SPY": {"daily": spy_daily, "weekly": 0.01, "monthly": 0.02},
        "NASDAQ": {"daily": 0.01, "weekly": 0.02, "monthly": 0.03},
        "DOW": {"daily": 0.005, "weekly": 0.01, "monthly": 0.01},
    }


def _market(spy_daily):
    with patch("signals.market_signal.get_latest_market_changes", return_value=_market_changes(spy_daily)):
        return market_signal.score()


def _market_sources(monkeypatch, cached=_CACHED_DAILY, live=_LIVE_DAILY):
    monkeypatch.setattr(
        "signals.market_signal.latest_changes",
        lambda: None if cached is None else _market_changes(cached),
    )
    monkeypatch.setattr(
        "signals.market_signal.get_latest_market_changes", lambda: _market_changes(live)
    )


def test_market_dip_passes():
    s = _market(-0.01)
    assert s.state == "dip"
    assert s.passes is True


def test_market_growth_fails():
    s = _market(0.01)
    assert s.state == "growth"
    assert s.passes is False


def test_market_flat_fails():
    s = _market(0.0)
    assert s.state == "flat"
    assert s.passes is False


def test_market_reports_the_live_changes_when_refresh_is_allowed(monkeypatch):
    _market_sources(monkeypatch)

    s = market_signal.score(allow_refresh=True)

    assert s.score == _LIVE_DAILY
    assert s.state == "growth"


def test_market_reports_the_cached_changes_when_refresh_is_disallowed(monkeypatch):
    _market_sources(monkeypatch)

    s = market_signal.score(allow_refresh=False)

    assert s.score == _CACHED_DAILY
    assert s.state == "dip"


def test_market_reports_the_live_changes_when_refresh_is_disallowed_but_the_cache_is_empty(monkeypatch):
    _market_sources(monkeypatch, cached=None)

    s = market_signal.score(allow_refresh=False)

    assert s.score == _LIVE_DAILY


def test_market_dip_at_the_exact_threshold_passes():
    s = _market(MARKET_DIP_THRESHOLD)
    assert s.state == "dip"
    assert s.passes is True


def test_market_growth_at_the_exact_threshold_fails():
    s = _market(MARKET_GROWTH_THRESHOLD)
    assert s.state == "growth"
    assert s.passes is False


@pytest.mark.parametrize(
    "daily",
    [
        MARKET_DIP_THRESHOLD + 0.0001,
        0.0,
        MARKET_GROWTH_THRESHOLD - 0.0001,
    ],
)
def test_market_inside_the_band_between_the_thresholds_is_flat(daily):
    assert _market(daily).state == "flat"


def test_market_thresholds_are_asymmetric():
    assert _market(-MARKET_DIP_THRESHOLD).state == "flat"


def test_market_table_row_order():
    table = _market(-0.01).table
    assert table.index("SPY") < table.index("NASDAQ") < table.index("DOW")


# --- sector_signal ---------------------------------------------------------------


def _sector_df(growths):
    return pd.DataFrame(
        {
            "industry": [f"Industry {i}" for i in range(len(growths))],
            "market_cap": [1e12 * (i + 1) for i in range(len(growths))],
            "earnings_growth_estimate_1y": growths,
        }
    )


def test_sector_passes_at_quorum():
    with patch("signals.sector_signal.sector_performance", return_value=_sector_df([0.1, 0.1, 0.1, -0.1, -0.1])):
        s = sector_signal.score()
        assert s.passes is True
        assert s.state == "growing"


def test_sector_fails_below_quorum():
    with patch("signals.sector_signal.sector_performance", return_value=_sector_df([0.1, 0.1, -0.1, -0.1, -0.1])):
        s = sector_signal.score()
        assert s.passes is False
        assert s.state == "flat"


def test_sector_extreme_none_growing():
    with patch("signals.sector_signal.sector_performance", return_value=_sector_df([-0.1] * 5)):
        s = sector_signal.score()
        assert s.passes is False
        assert s.score == 0.0


def test_sector_extreme_all_growing():
    with patch("signals.sector_signal.sector_performance", return_value=_sector_df([0.1] * 5)):
        s = sector_signal.score()
        assert s.passes is True
        assert s.score == 1.0


@pytest.mark.parametrize(
    "value,expected",
    [
        (2.5e12, "2.50T"),
        (5e9, "5.00B"),
        (3e6, "3.00M"),
        (999, "999"),
        (1e12, "1.00T"),  # exact trillion boundary, inclusive
        (1e9, "1.00B"),  # exact billion boundary, inclusive
        (1e6, "1.00M"),  # exact million boundary, inclusive
        (999_999_999_999, "1000.00B"),  # just under the trillion boundary -> stays in B
        (999_999_999, "1000.00M"),  # just under the billion boundary -> stays in M
        (999_999, "999,999"),  # just under the million boundary -> plain number
        (-2.5e12, "-2.50T"),  # negative market cap: sign preserved, magnitude drives the unit
    ],
)
def test_human_cap_units(value, expected):
    assert sector_signal._human_cap(value) == expected


def test_format_industry_trend_icon_positive():
    row = {"industry": "X", "market_cap": 1e12, "earnings_growth_estimate_1y": 0.1}
    assert "📈" in sector_signal._format_industry(row)


def test_format_industry_trend_icon_negative():
    row = {"industry": "X", "market_cap": 1e12, "earnings_growth_estimate_1y": -0.1}
    assert "📉" in sector_signal._format_industry(row)


def test_format_industry_trend_icon_zero_is_not_growth():
    row = {"industry": "X", "market_cap": 1e12, "earnings_growth_estimate_1y": 0.0}
    assert "📉" in sector_signal._format_industry(row)


# --- yield_curve_signal ---------------------------------------------------------------


def _spread_df(spreads, policy=None, start="2026-01-01"):
    """Daily history holding one constant value per bucket; the last one is still filling."""
    begin = pd.Timestamp(start)
    rows = []
    for index, value in enumerate(spreads):
        for day in range(YIELD_CURVE_BUCKET_DAYS):
            offset = index * YIELD_CURVE_BUCKET_DAYS + day
            row = {"date": begin + pd.Timedelta(days=offset), "spread": value}
            if policy is not None:
                row["policy_spread"] = policy[index]
            rows.append(row)
    return pd.DataFrame(rows)


def test_spread_df_builder_fills_one_bucket_per_value():
    history = _spread_df([0.5, 0.8])

    assert len(history) == 2 * YIELD_CURVE_BUCKET_DAYS
    assert history["date"].iloc[0] == pd.Timestamp("2026-01-01")
    assert history["date"].iloc[-1] == pd.Timestamp("2026-02-11")


def test_spread_df_builder_holds_each_value_constant_across_its_own_bucket():
    history = _spread_df([0.5, 0.8])

    assert set(history["spread"].iloc[:YIELD_CURVE_BUCKET_DAYS]) == {0.5}
    assert set(history["spread"].iloc[YIELD_CURVE_BUCKET_DAYS:]) == {0.8}


def test_spread_df_builder_omits_the_policy_column_when_not_asked_for():
    assert "policy_spread" not in _spread_df([0.5, 0.8]).columns


def _curve(spreads, policy=None):
    with patch(
        "signals.yield_curve_signal.yield_curve_history", return_value=_spread_df(spreads, policy)
    ):
        return yield_curve_signal.score()


def test_yield_curve_steep_at_exact_threshold():
    s = _curve([1.0, 0.5])
    assert s.state == "steep"
    assert s.passes is True


def test_yield_curve_just_below_steep_is_flat():
    s = _curve([0.99, 0.5])
    assert s.state == "flat"
    assert s.passes is True


def test_yield_curve_exactly_zero_is_flat_not_inverted():
    s = _curve([0.0, 0.5])
    assert s.state == "flat"


def test_yield_curve_negative_bucket_is_inverted():
    s = _curve([-0.2, 0.5])
    assert s.state == "inverted"
    assert s.passes is False


def test_yield_curve_two_deep_buckets_is_deep_inversion():
    s = _curve([-1.0, -1.0, -1.2])
    assert s.state == "deep_inversion"
    assert s.passes is False


def test_yield_curve_one_deep_bucket_is_only_inverted():
    s = _curve([-0.3, -1.5, -1.5])
    assert s.state == "inverted"


def test_yield_curve_filling_bucket_excluded_from_classification():
    s = _curve([0.5, -2.0])
    assert s.state == "flat"


def test_yield_curve_is_advisory():
    s = _curve([0.5, 0.5])
    assert s.advisory is True
    assert s.name == "yield_curve"


def test_yield_curve_table_marks_the_filling_bucket():
    s = _curve([0.5, 0.8])
    assert "2026-02-11*" in s.table
    assert "2026-01-21 " in s.table


def test_yield_curve_detail_has_latest_spread_and_bucket_avg():
    s = _curve([0.5, 0.86])
    assert "+0.86pp" in s.detail
    assert "last full 3wk avg +0.50pp" in s.detail


def test_yield_curve_single_filling_bucket_defaults_to_flat():
    assert _curve([-2.0]).state == "flat"


def test_policy_spread_fires_after_two_complete_negative_buckets():
    s = _curve([0.5, 0.5, 0.5], policy=[-0.2, -0.41, -0.5])
    assert s.footer is not None
    assert "Cuts priced in since 2026-01-01" in s.footer
    assert "2y-FFR" in s.table
    assert "-0.41pp" in s.table


def test_policy_spread_silent_below_the_confirmation_threshold():
    s = _curve([0.5, 0.5, 0.5], policy=[0.3, -0.41, -0.5])
    assert s.footer is None
    assert "no cuts priced in" in s.detail


def test_policy_spread_ignores_the_filling_bucket():
    s = _curve([0.5, 0.5, 0.5, 0.5], policy=[0.3, 0.3, -0.41, -0.5])
    assert s.footer is None


def test_policy_spread_gap_breaks_the_run_instead_of_collapsing():
    history = _spread_df([0.5, 0.5, 0.5, 0.5, 0.5], policy=[-0.4, -0.5, 0.0, 0.0, 0.0])
    stale = history["date"] >= pd.Timestamp("2026-01-01") + pd.Timedelta(days=42)
    history.loc[stale, "policy_spread"] = float("nan")

    with patch("signals.yield_curve_signal.yield_curve_history", return_value=history):
        s = yield_curve_signal.score()

    assert s.footer is None
    assert "missing for the latest 3wk period" in s.detail


def test_policy_spread_silent_when_positive():
    s = _curve([0.5, 0.5, 0.5], policy=[0.3, 0.25, 0.2])
    assert s.footer is None
    assert "no cuts priced in" in s.detail


def test_policy_spread_omitted_when_cache_predates_the_column():
    s = _curve([0.5, 0.5])
    assert s.footer is None
    assert "2y-FFR" not in s.table
    assert "2y-FFR" not in s.detail


def test_policy_spread_does_not_change_the_curve_state():
    assert _curve([0.5, 0.5, 0.5], policy=[-2.0, -2.0, -2.0]).state == "flat"


# --- buy_signal ---------------------------------------------------------------


def test_alert_state_strong(make_subsignal):
    subs = [make_subsignal("vix", passes=True)]
    state, _ = _alert_state(subs, [])
    assert state == "strong"


def test_alert_state_soft(make_subsignal):
    subs = [make_subsignal("vix", passes=True)]
    state, _ = _alert_state(subs, ["sector"])
    assert state == "soft"


def test_alert_state_none_on_any_failure(make_subsignal):
    subs = [make_subsignal("vix", passes=True), make_subsignal("sector", passes=False)]
    state, _ = _alert_state(subs, [])
    assert state == "none"


def test_alert_state_none_when_empty():
    state, _ = _alert_state([], [])
    assert state == "none"


_SIGNAL_MODULES = {
    "vix_signal": "vix",
    "rate_signal": "fed_rate",
    "margin_signal": "margin_debt",
    "market_signal": "market_dip",
    "sector_signal": "sector",
    "yield_curve_signal": "yield_curve",
}


def _sub(name, passes=True, state="ok", advisory=False):
    return SubSignal(name, 0.0, state, "", passes=passes, advisory=advisory)


def _scoring(outcome):
    if callable(outcome):
        return outcome

    def score(*args, **kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return score


def _compute(monkeypatch, allow_refresh=True, **overrides):
    """Compute a signal with every sub-signal passing, except the ones this test names.

    Each override is a SubSignal to return, an Exception to raise, or a score() replacement.
    """
    outcomes = {module: _sub(name) for module, name in _SIGNAL_MODULES.items()}
    outcomes["yield_curve_signal"] = _sub("yield_curve", advisory=True)
    outcomes.update(overrides)

    for module, outcome in outcomes.items():
        monkeypatch.setattr(f"signals.buy_signal.{module}.score", _scoring(outcome))
    return compute_signal(allow_refresh=allow_refresh)


def test_compute_signal_is_strong_when_every_required_signal_passes(monkeypatch):
    result = _compute(monkeypatch)

    assert result.state == "strong"
    assert result.passing_count == 5
    assert result.required_count == 5


def test_compute_signal_counts_a_failing_required_signal_out_of_the_checklist(monkeypatch):
    result = _compute(monkeypatch, vix_signal=_sub("vix", passes=False))

    assert result.state == "none"
    assert result.passing_count == 4


def test_compute_signal_stays_strong_when_the_advisory_signal_fails(monkeypatch):
    failing_curve = _sub("yield_curve", passes=False, state="inverted", advisory=True)

    result = _compute(monkeypatch, yield_curve_signal=failing_curve)

    assert result.state == "strong"
    assert result.passing_count == 5
    assert result.required_count == 5
    assert result.score == 1.0


def test_compute_signal_still_shows_a_failing_advisory_signal(monkeypatch):
    failing_curve = _sub("yield_curve", passes=False, state="inverted", advisory=True)

    result = _compute(monkeypatch, yield_curve_signal=failing_curve)

    assert [s.name for s in result.subsignals] == list(_SIGNAL_MODULES.values())


def test_compute_signal_stays_strong_when_the_advisory_signal_is_missing(monkeypatch):
    result = _compute(monkeypatch, yield_curve_signal=RuntimeError("no cache"))

    assert result.state == "strong"


def test_compute_signal_reports_a_missing_advisory_signal(monkeypatch):
    result = _compute(monkeypatch, yield_curve_signal=RuntimeError("no cache"))

    assert result.missing_signals == ["yield_curve"]


def test_compute_signal_downgrades_to_soft_when_a_required_signal_is_missing(monkeypatch):
    result = _compute(monkeypatch, vix_signal=RuntimeError("boom"))

    assert result.state == "soft"
    assert result.missing_signals == ["vix"]
    assert len(result.subsignals) == 5


def test_compute_signal_reports_every_signal_missing_when_all_of_them_fail(monkeypatch):
    boom = {module: RuntimeError("boom") for module in _SIGNAL_MODULES}

    result = _compute(monkeypatch, **boom)

    assert result.subsignals == []
    assert result.state == "none"
    assert sorted(result.missing_signals) == sorted(_SIGNAL_MODULES.values())


def test_compute_signal_passes_allow_refresh_through_to_the_refetching_signals(monkeypatch):
    def vix_score(vix=None, allow_refresh=True):
        return _sub("vix", passes=allow_refresh)

    def market_score(allow_refresh=True):
        return _sub("market_dip", passes=allow_refresh)

    result = _compute(
        monkeypatch, allow_refresh=False, vix_signal=vix_score, market_signal=market_score
    )

    assert result.passing_count == 3


def test_compute_signal_holds_the_signal_lock_while_scoring(monkeypatch):
    acquired_during_scoring = []

    def check_lock(*args, **kwargs):
        got_it = SIGNAL_LOCK.acquire(blocking=False)
        acquired_during_scoring.append(got_it)
        if got_it:
            SIGNAL_LOCK.release()
        return _sub("vix")

    _compute(monkeypatch, vix_signal=check_lock)

    assert acquired_during_scoring == [False]
