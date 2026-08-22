"""Tests for signals/*.py: every state-machine branch, both passing and failing, per module."""

from unittest.mock import patch

import pandas as pd
import pytest

from signals import margin_signal, market_signal, rate_signal, sector_signal, vix_signal, yield_curve_signal
from signals.base import NONE, SOFT, STRONG
from signals.buy_signal import SIGNAL_LOCK, _alert_state, compute_signal

# --- vix_signal ---------------------------------------------------------------


def _vix(vix, change=None):
    with patch("signals.vix_signal.vix_change_pct", return_value=change):
        return vix_signal.score(vix=vix)


def test_vix_strong_passes():
    s = _vix(30.0)
    assert s.state == STRONG and s.passes is True


def test_vix_soft_boundary_passes():
    s = _vix(22.0)
    assert s.state == SOFT and s.passes is True


def test_vix_calm_fails():
    s = _vix(15.0)
    assert s.state == NONE and s.passes is False


def test_vix_optimistic_low_fails():
    s = _vix(5.0)
    assert s.state == NONE and s.passes is False
    assert "optimistic" in s.detail


def test_vix_explicit_value_skips_fetch():
    with patch("signals.vix_signal.get_latest_vix") as mock_fetch, patch(
        "signals.vix_signal.vix_change_pct", return_value=None
    ):
        vix_signal.score(vix=20.0)
        mock_fetch.assert_not_called()


def test_vix_allow_refresh_true_fetches_live():
    with patch("signals.vix_signal.get_latest_vix", return_value=18.0) as mock_fetch, patch(
        "signals.vix_signal.vix_change_pct", return_value=None
    ):
        vix_signal.score(allow_refresh=True)
        mock_fetch.assert_called_once()


def test_vix_allow_refresh_false_uses_cache():
    with patch("signals.vix_signal.load_latest_cached_vix", return_value=18.0) as mock_cache, patch(
        "signals.vix_signal.get_latest_vix"
    ) as mock_fetch, patch("signals.vix_signal.vix_change_pct", return_value=None):
        vix_signal.score(allow_refresh=False)
        mock_cache.assert_called_once()
        mock_fetch.assert_not_called()


def test_vix_allow_refresh_false_falls_back_when_cache_empty():
    with patch("signals.vix_signal.load_latest_cached_vix", return_value=None), patch(
        "signals.vix_signal.get_latest_vix", return_value=18.0
    ) as mock_fetch, patch("signals.vix_signal.vix_change_pct", return_value=None):
        vix_signal.score(allow_refresh=False)
        mock_fetch.assert_called_once()


def test_vix_change_suffix_present_vs_absent():
    assert "vs prior close" in _vix(15.0, change=0.05).detail
    assert "vs prior close" not in _vix(15.0, change=None).detail


def test_vix_just_below_strong_is_soft_not_strong():
    s = _vix(29.9)
    assert s.state == SOFT and s.passes is True


def test_vix_just_below_soft_is_calm_not_soft():
    s = _vix(21.9)
    assert s.state == NONE and s.passes is False


def test_vix_optimistic_boundary_exact():
    s = _vix(10.0)  # exactly VIX_OPTIMISTIC, inclusive per `<=`
    assert "optimistic" in s.detail


def test_vix_just_above_optimistic_is_plain_calm():
    s = _vix(10.1)
    assert "optimistic" not in s.detail
    assert "calm" in s.detail


# --- rate_signal ---------------------------------------------------------------


def _meetings_df(ease, no_change, hike):
    rows = [
        {
            "meeting_date": d,
            "horizon": h,
            "prob_ease": ease,
            "prob_no_change": no_change,
            "prob_hike": hike,
        }
        for h, d in [
            ("nearest", pd.Timestamp("2026-08-01")),
            ("six_month", pd.Timestamp("2027-01-01")),
            ("one_year", pd.Timestamp("2027-07-01")),
        ]
    ]
    return pd.DataFrame(rows)


def test_rate_hiking_fails():
    with patch("signals.rate_signal.latest_fedwatch", return_value=_meetings_df(0.0, 0.3, 0.7)):
        s = rate_signal.score()
        assert s.state == "hiking" and s.passes is False


def test_rate_cutting_passes():
    with patch("signals.rate_signal.latest_fedwatch", return_value=_meetings_df(0.7, 0.3, 0.0)):
        s = rate_signal.score()
        assert s.state == "cutting" and s.passes is True


def test_rate_no_change_passes():
    with patch("signals.rate_signal.latest_fedwatch", return_value=_meetings_df(0.2, 0.6, 0.2)):
        s = rate_signal.score()
        assert s.state == "no_change" and s.passes is True


def test_rate_meeting_fallback_no_horizon_column():
    df = pd.DataFrame(
        [
            {"meeting_date": pd.Timestamp("2026-08-01"), "prob_ease": 0.1, "prob_no_change": 0.2, "prob_hike": 0.7},
            {"meeting_date": pd.Timestamp("2027-07-01"), "prob_ease": 0.0, "prob_no_change": 0.9, "prob_hike": 0.1},
        ]
    )
    with patch("signals.rate_signal.latest_fedwatch", return_value=df):
        s = rate_signal.score()
        # falls back to the last row (sorted by meeting_date) for every horizon
        assert s.state == "no_change"


def test_rate_exact_tie_at_top_is_flat():
    # hike == no_change, both dominant over ease -> neither strictly greater -> falls to flat
    with patch("signals.rate_signal.latest_fedwatch", return_value=_meetings_df(0.0, 0.5, 0.5)):
        s = rate_signal.score()
        assert s.state == "flat" and s.passes is True


def test_rate_all_zero_probabilities_is_flat():
    # a three-way tie at 0.0 -- no probability strictly dominates, so it falls all the way to flat
    with patch("signals.rate_signal.latest_fedwatch", return_value=_meetings_df(0.0, 0.0, 0.0)):
        s = rate_signal.score()
        assert s.state == "flat"


def test_rate_no_table_uses_detail_string():
    # fed_rate doesn't get a table (5 columns with a 10-char date field doesn't fit on mobile):
    # detail carries the same info as a bullet-friendly ' | '-joined string instead.
    with patch("signals.rate_signal.latest_fedwatch", return_value=_meetings_df(0.0, 0.3, 0.7)):
        s = rate_signal.score()
        assert s.table is None
        assert "nearest" in s.detail and "hike 70.0%" in s.detail


# --- margin_signal ---------------------------------------------------------------


def _margin_history(values):
    return pd.DataFrame(
        {
            "month": pd.date_range("2026-01-01", periods=len(values), freq="MS"),
            "debit_balances": values,
        }
    )


def test_margin_deleveraging_passes():
    with patch("signals.margin_signal.margin_history", return_value=_margin_history([1000, 900, 800])), patch(
        "signals.margin_signal.data_freshness", return_value=("fresh", "ok")
    ):
        s = margin_signal.score()
        assert s.state == "deleveraging" and s.passes is True


def test_margin_leveraging_fails():
    with patch("signals.margin_signal.margin_history", return_value=_margin_history([800, 900, 1000])), patch(
        "signals.margin_signal.data_freshness", return_value=("fresh", "ok")
    ):
        s = margin_signal.score()
        assert s.state == "leveraging" and s.passes is False


def test_margin_single_row_edge_case():
    with patch("signals.margin_signal.margin_history", return_value=_margin_history([1000])), patch(
        "signals.margin_signal.data_freshness", return_value=("fresh", "ok")
    ):
        s = margin_signal.score()
        assert s.passes is False
        assert s.score == 0.0


def test_margin_zero_change_is_not_deleveraging():
    # a flat month (diff == 0) doesn't count as "decreasing" -- strictly < 0 required
    with patch("signals.margin_signal.margin_history", return_value=_margin_history([1000, 1000])), patch(
        "signals.margin_signal.data_freshness", return_value=("fresh", "ok")
    ):
        s = margin_signal.score()
        assert s.passes is False
        assert s.score == 0.0


def test_margin_mixed_trend_over_longer_window_is_not_deleveraging(monkeypatch):
    # MARGIN_DELEVERAGE_MONTHS + 1 = 2 months by default, but this proves the "all must decrease"
    # rule holds even with a longer decrease-then-increase run
    monkeypatch.setattr("signals.margin_signal.MARGIN_DELEVERAGE_MONTHS", 2)
    with patch("signals.margin_signal.margin_history", return_value=_margin_history([1000, 900, 950])), patch(
        "signals.margin_signal.data_freshness", return_value=("fresh", "ok")
    ):
        s = margin_signal.score()
        assert s.passes is False


def test_margin_detail_omits_flag_when_fresh():
    # fresh is the routine case -- no extra bullet, and no raw date/month text either (that's
    # already visible in the "data as of" freshness table at the bottom of the Telegram reply).
    with patch("signals.margin_signal.margin_history", return_value=_margin_history([1000, 900])), patch(
        "signals.margin_signal.data_freshness", return_value=("fresh", "latest month 2026-07 is current")
    ):
        s = margin_signal.score()
        assert "|" not in s.detail
        assert "2026-07" not in s.detail


def test_margin_detail_flags_refresh_due_soon_tersely():
    with patch("signals.margin_signal.margin_history", return_value=_margin_history([1000, 900])), patch(
        "signals.margin_signal.data_freshness",
        return_value=("refresh_due_soon", "latest month 2026-07 is 2 months old and the release checkpoint has passed"),
    ):
        s = margin_signal.score()
        assert "refresh due soon" in s.detail
        assert "2026-07" not in s.detail  # terse flag only, no raw freshness message


def test_margin_detail_flags_stale():
    with patch("signals.margin_signal.margin_history", return_value=_margin_history([1000, 900])), patch(
        "signals.margin_signal.data_freshness", return_value=("stale", "way overdue")
    ):
        s = margin_signal.score()
        assert "stale" in s.detail
        assert "way overdue" not in s.detail


# --- market_signal ---------------------------------------------------------------


def _market_changes(spy_daily):
    return {
        "SPY": {"daily": spy_daily, "weekly": 0.01, "monthly": 0.02},
        "NASDAQ": {"daily": 0.01, "weekly": 0.02, "monthly": 0.03},
        "DOW": {"daily": 0.005, "weekly": 0.01, "monthly": 0.01},
    }


def test_market_dip_passes():
    with patch("signals.market_signal.get_latest_market_changes", return_value=_market_changes(-0.01)):
        s = market_signal.score()
        assert s.state == "dip" and s.passes is True


def test_market_growth_fails():
    with patch("signals.market_signal.get_latest_market_changes", return_value=_market_changes(0.01)):
        s = market_signal.score()
        assert s.state == "growth" and s.passes is False


def test_market_flat_fails():
    with patch("signals.market_signal.get_latest_market_changes", return_value=_market_changes(0.0)):
        s = market_signal.score()
        assert s.state == "flat" and s.passes is False


def test_market_allow_refresh_false_uses_cache():
    with patch("signals.market_signal.latest_changes", return_value=_market_changes(-0.01)) as mock_cache, patch(
        "signals.market_signal.get_latest_market_changes"
    ) as mock_fetch:
        market_signal.score(allow_refresh=False)
        mock_cache.assert_called_once()
        mock_fetch.assert_not_called()


def test_market_allow_refresh_false_falls_back_when_cache_empty():
    with patch("signals.market_signal.latest_changes", return_value=None), patch(
        "signals.market_signal.get_latest_market_changes", return_value=_market_changes(-0.01)
    ) as mock_fetch:
        market_signal.score(allow_refresh=False)
        mock_fetch.assert_called_once()


def test_market_dip_exact_threshold_boundary():
    # MARKET_DIP_THRESHOLD = -0.005, and the condition is `daily <= threshold` (inclusive)
    with patch("signals.market_signal.get_latest_market_changes", return_value=_market_changes(-0.005)):
        s = market_signal.score()
        assert s.state == "dip" and s.passes is True


def test_market_growth_exact_threshold_boundary():
    # MARKET_GROWTH_THRESHOLD = 0.0055, and the condition is `daily >= threshold` (inclusive)
    with patch("signals.market_signal.get_latest_market_changes", return_value=_market_changes(0.0055)):
        s = market_signal.score()
        assert s.state == "growth" and s.passes is False


def test_market_just_below_growth_threshold_is_flat():
    with patch("signals.market_signal.get_latest_market_changes", return_value=_market_changes(0.0054)):
        s = market_signal.score()
        assert s.state == "flat" and s.passes is False


def test_market_just_inside_flat_band_both_sides():
    with patch("signals.market_signal.get_latest_market_changes", return_value=_market_changes(-0.0049)):
        assert market_signal.score().state == "flat"
    with patch("signals.market_signal.get_latest_market_changes", return_value=_market_changes(0.0049)):
        assert market_signal.score().state == "flat"


def test_market_asymmetric_thresholds_are_independent():
    # the old symmetric behavior (growth threshold == abs(dip threshold)) no longer holds --
    # 0.005 used to be exactly the growth boundary, now it's still inside the flat band
    with patch("signals.market_signal.get_latest_market_changes", return_value=_market_changes(0.005)):
        assert market_signal.score().state == "flat"


def test_market_table_row_order():
    with patch("signals.market_signal.get_latest_market_changes", return_value=_market_changes(-0.01)):
        s = market_signal.score()
        assert s.table.index("SPY") < s.table.index("NASDAQ") < s.table.index("DOW")


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
        assert s.passes is True and s.state == "growing"


def test_sector_fails_below_quorum():
    with patch("signals.sector_signal.sector_performance", return_value=_sector_df([0.1, 0.1, -0.1, -0.1, -0.1])):
        s = sector_signal.score()
        assert s.passes is False and s.state == "flat"


def test_sector_extreme_none_growing():
    with patch("signals.sector_signal.sector_performance", return_value=_sector_df([-0.1] * 5)):
        s = sector_signal.score()
        assert s.passes is False and s.score == 0.0


def test_sector_extreme_all_growing():
    with patch("signals.sector_signal.sector_performance", return_value=_sector_df([0.1] * 5)):
        s = sector_signal.score()
        assert s.passes is True and s.score == 1.0


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


def _spread_df(months):
    """Daily spread history: three rows per month at a constant value, e.g. {"2026-07": 0.5}.
    The last month acts as the current (partial) month, excluded from classification.
    """
    rows = []
    for month, value in months.items():
        start = pd.Timestamp(f"{month}-01")
        rows.extend({"date": start + pd.Timedelta(days=day), "spread": value} for day in range(3))
    return pd.DataFrame(rows)


def _curve(months):
    with patch("signals.yield_curve_signal.yield_curve_history", return_value=_spread_df(months)):
        return yield_curve_signal.score()


def test_yield_curve_steep_at_exact_threshold():
    s = _curve({"2026-07": 1.0, "2026-08": 0.5})
    assert s.state == "steep" and s.passes is True


def test_yield_curve_just_below_steep_is_flat():
    s = _curve({"2026-07": 0.99, "2026-08": 0.5})
    assert s.state == "flat" and s.passes is True


def test_yield_curve_exactly_zero_is_flat_not_inverted():
    s = _curve({"2026-07": 0.0, "2026-08": 0.5})
    assert s.state == "flat"


def test_yield_curve_negative_month_is_inverted():
    s = _curve({"2026-07": -0.2, "2026-08": 0.5})
    assert s.state == "inverted" and s.passes is False


def test_yield_curve_two_deep_months_is_deep_inversion():
    s = _curve({"2026-06": -1.0, "2026-07": -1.0, "2026-08": -1.2})
    assert s.state == "deep_inversion" and s.passes is False


def test_yield_curve_one_deep_month_is_only_inverted():
    s = _curve({"2026-06": -0.3, "2026-07": -1.5, "2026-08": -1.5})
    assert s.state == "inverted"


def test_yield_curve_partial_current_month_excluded_from_classification():
    # Current month is deeply negative, but the last complete month is positive -> still flat.
    s = _curve({"2026-07": 0.5, "2026-08": -2.0})
    assert s.state == "flat"


def test_yield_curve_is_advisory():
    s = _curve({"2026-07": 0.5, "2026-08": 0.5})
    assert s.advisory is True
    assert s.name == "yield_curve"


def test_yield_curve_table_marks_current_partial_month():
    s = _curve({"2026-07": 0.5, "2026-08": 0.8})
    assert "2026-08*" in s.table
    assert "2026-07 " in s.table  # complete month, no asterisk


def test_yield_curve_detail_has_latest_spread_and_month_avg():
    s = _curve({"2026-07": 0.5, "2026-08": 0.86})
    assert "+0.86pp" in s.detail
    assert "last full month avg +0.50pp" in s.detail


def test_yield_curve_single_partial_month_defaults_to_flat():
    # No complete month yet (fresh series edge case): no classification basis, stay neutral.
    s = _curve({"2026-08": -2.0})
    assert s.state == "flat"


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


def test_compute_signal_advisory_failure_does_not_break_strong(make_subsignal):
    ok = lambda *a, **k: make_subsignal("x", passes=True)
    inverted_curve = lambda *a, **k: make_subsignal("yield_curve", "inverted", passes=False, advisory=True)
    with patch("signals.buy_signal.vix_signal.score", side_effect=ok), patch(
        "signals.buy_signal.rate_signal.score", side_effect=ok
    ), patch("signals.buy_signal.margin_signal.score", side_effect=ok), patch(
        "signals.buy_signal.market_signal.score", side_effect=ok
    ), patch(
        "signals.buy_signal.sector_signal.score", side_effect=ok
    ), patch(
        "signals.buy_signal.yield_curve_signal.score", side_effect=inverted_curve
    ):
        result = compute_signal()
    assert result.state == "strong"
    assert result.passing_count == 5
    assert result.required_count == 5
    assert len(result.subsignals) == 6  # advisory still shown
    assert result.score == 1.0


def test_compute_signal_missing_advisory_does_not_downgrade_to_soft(make_subsignal):
    ok = lambda *a, **k: make_subsignal("x", passes=True)
    with patch("signals.buy_signal.vix_signal.score", side_effect=ok), patch(
        "signals.buy_signal.rate_signal.score", side_effect=ok
    ), patch("signals.buy_signal.margin_signal.score", side_effect=ok), patch(
        "signals.buy_signal.market_signal.score", side_effect=ok
    ), patch(
        "signals.buy_signal.sector_signal.score", side_effect=ok
    ), patch(
        "signals.buy_signal.yield_curve_signal.score", side_effect=RuntimeError("no cache")
    ):
        result = compute_signal()
    assert result.state == "strong"  # a missing required signal would make this "soft"
    assert result.missing_signals == ["yield_curve"]  # still visible in the missing line


def test_compute_signal_catches_one_failing_signal(make_subsignal):
    ok = lambda *a, **k: make_subsignal("x", passes=True)
    with patch("signals.buy_signal.vix_signal.score", side_effect=RuntimeError("boom")), patch(
        "signals.buy_signal.rate_signal.score", side_effect=ok
    ), patch("signals.buy_signal.margin_signal.score", side_effect=ok), patch(
        "signals.buy_signal.market_signal.score", side_effect=ok
    ), patch(
        "signals.buy_signal.sector_signal.score", side_effect=ok
    ), patch(
        "signals.buy_signal.yield_curve_signal.score", side_effect=ok
    ):
        result = compute_signal()
    assert result.missing_signals == ["vix"]
    assert len(result.subsignals) == 5


def test_compute_signal_threads_allow_refresh(make_subsignal):
    ok = lambda *a, **k: make_subsignal("x", passes=True)
    with patch("signals.buy_signal.vix_signal.score") as mock_vix, patch(
        "signals.buy_signal.market_signal.score"
    ) as mock_market, patch("signals.buy_signal.rate_signal.score", side_effect=ok), patch(
        "signals.buy_signal.margin_signal.score", side_effect=ok
    ), patch(
        "signals.buy_signal.sector_signal.score", side_effect=ok
    ), patch(
        "signals.buy_signal.yield_curve_signal.score", side_effect=ok
    ):
        mock_vix.return_value = make_subsignal("vix", passes=True)
        mock_market.return_value = make_subsignal("market_dip", passes=True)
        compute_signal(allow_refresh=False)
    mock_vix.assert_called_once_with(None, allow_refresh=False)
    mock_market.assert_called_once_with(allow_refresh=False)


def test_compute_signal_all_six_signals_fail():
    with patch("signals.buy_signal.vix_signal.score", side_effect=RuntimeError("boom")), patch(
        "signals.buy_signal.rate_signal.score", side_effect=RuntimeError("boom")
    ), patch("signals.buy_signal.margin_signal.score", side_effect=RuntimeError("boom")), patch(
        "signals.buy_signal.market_signal.score", side_effect=RuntimeError("boom")
    ), patch(
        "signals.buy_signal.sector_signal.score", side_effect=RuntimeError("boom")
    ), patch(
        "signals.buy_signal.yield_curve_signal.score", side_effect=RuntimeError("boom")
    ):
        result = compute_signal()
    assert result.subsignals == []
    assert sorted(result.missing_signals) == ["fed_rate", "margin_debt", "market_dip", "sector", "vix", "yield_curve"]
    assert result.state == "none"


def test_signal_lock_held_during_compute(make_subsignal):
    lock_state = []

    def check_lock(*a, **k):
        lock_state.append(SIGNAL_LOCK.acquire(blocking=False))
        if lock_state[-1]:
            SIGNAL_LOCK.release()
        return make_subsignal("vix", passes=True)

    ok = lambda *a, **k: make_subsignal("x", passes=True)
    with patch("signals.buy_signal.vix_signal.score", side_effect=check_lock), patch(
        "signals.buy_signal.rate_signal.score", side_effect=ok
    ), patch("signals.buy_signal.margin_signal.score", side_effect=ok), patch(
        "signals.buy_signal.market_signal.score", side_effect=ok
    ), patch(
        "signals.buy_signal.sector_signal.score", side_effect=ok
    ), patch(
        "signals.buy_signal.yield_curve_signal.score", side_effect=ok
    ):
        compute_signal()
    assert lock_state == [False]  # re-acquiring non-blocking failed -> lock was already held
