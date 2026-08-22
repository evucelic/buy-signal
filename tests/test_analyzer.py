"""Tests for analyzer.py: discount bands, rate regime, z-scores, matrix verdicts, chart."""

from unittest.mock import patch

import pandas as pd
import pytest

import analyzer


# --- discount bands ---------------------------------------------------------------


def test_discount_band_below_little_threshold():
    assert analyzer._discount_band(0.09) == "little"


def test_discount_band_exactly_at_mild_boundary():
    assert analyzer._discount_band(0.10) == "mild"


def test_discount_band_exactly_at_candidate_boundary():
    assert analyzer._discount_band(0.20) == "candidate"


def test_discount_band_exactly_at_upper_candidate_boundary_still_candidate():
    assert analyzer._discount_band(0.30) == "candidate"


def test_discount_band_above_candidate_is_investigate():
    assert analyzer._discount_band(0.31) == "investigate"


def test_discount_band_negative_discount_is_little():
    assert analyzer._discount_band(-0.05) == "little"  # small caps more expensive than SPX


# --- rate regime (FedWatch expectations) ---------------------------------------------------------------


def _fedwatch_df(nearest, six_month, one_year):
    """Each arg is an (ease, no_change, hike) probability triple."""
    rows = []
    for i, (horizon, probs) in enumerate([("nearest", nearest), ("six_month", six_month), ("one_year", one_year)]):
        ease, no_change, hike = probs
        rows.append(
            {
                "horizon": horizon,
                "meeting_date": pd.Timestamp("2026-09-16") + pd.Timedelta(days=i * 120),
                "prob_ease": ease,
                "prob_no_change": no_change,
                "prob_hike": hike,
            }
        )
    return pd.DataFrame(rows)


def _rate_view_with(nearest, six_month=(0.2, 0.7, 0.1), one_year=(0.2, 0.7, 0.1)):
    # latest_fedwatch is imported lazily inside _rate_view, so patch it at its home module.
    with patch("collectors.fed_rate.latest_fedwatch", return_value=_fedwatch_df(nearest, six_month, one_year)):
        return analyzer._rate_view()


def test_rate_view_counts_consecutive_easing_horizons_from_nearest():
    view = _rate_view_with((0.8, 0.1, 0.1), (0.6, 0.3, 0.1), (0.2, 0.7, 0.1))
    assert view.consecutive_easing == 2
    assert [h.state for h in view.horizons] == ["ease", "ease", "no_change"]


def test_rate_view_gap_in_easing_resets_count():
    view = _rate_view_with((0.2, 0.7, 0.1), (0.8, 0.1, 0.1), (0.8, 0.1, 0.1))  # nearest holds
    assert view.consecutive_easing == 0
    assert view.rate_support is False


def test_rate_view_support_threshold_one_horizon(monkeypatch):
    monkeypatch.setattr(analyzer, "SMALL_CAP_EASING_OBS", 1)
    assert _rate_view_with((0.8, 0.1, 0.1)).rate_support is True
    assert _rate_view_with((0.1, 0.8, 0.1)).rate_support is False


def test_rate_view_support_threshold_two_horizons(monkeypatch):
    monkeypatch.setattr(analyzer, "SMALL_CAP_EASING_OBS", 2)
    assert _rate_view_with((0.8, 0.1, 0.1), (0.2, 0.7, 0.1)).rate_support is False
    assert _rate_view_with((0.8, 0.1, 0.1), (0.6, 0.3, 0.1)).rate_support is True


def test_rate_view_hike_dominant_state():
    view = _rate_view_with((0.1, 0.2, 0.7))
    assert view.horizons[0].state == "hike"


def test_rate_view_tie_is_mixed():
    view = _rate_view_with((0.4, 0.4, 0.2))
    assert view.horizons[0].state == "mixed"


def test_rate_view_missing_cache_degrades_to_empty():
    with patch("collectors.fed_rate.latest_fedwatch", side_effect=FileNotFoundError):
        view = analyzer._rate_view()
    assert view.horizons == []
    assert view.consecutive_easing == 0
    assert view.rate_support is False


def test_rate_view_empty_fedwatch_frame_degrades_not_raises():
    # A present-but-empty cache must not raise out of analyze() (it would kill the bot's poll thread).
    empty = pd.DataFrame(columns=["horizon", "meeting_date", "prob_ease", "prob_no_change", "prob_hike"])
    with patch("collectors.fed_rate.latest_fedwatch", return_value=empty):
        view = analyzer._rate_view()
    assert view.horizons == []
    assert view.rate_support is False


# --- z-scores ---------------------------------------------------------------


def test_cheapness_z_insufficient_history_is_none(monkeypatch):
    monkeypatch.setattr(analyzer, "VALUATION_Z_MIN_OBS", 3)
    assert analyzer._cheapness_z(pd.Series([20.0, 21.0]), 20.0) is None


def test_cheapness_z_at_exact_min_history(monkeypatch):
    monkeypatch.setattr(analyzer, "VALUATION_Z_MIN_OBS", 3)
    series = pd.Series([10.0, 20.0, 30.0])  # mean 20, std 10
    assert analyzer._cheapness_z(series, 10.0) == pytest.approx(1.0)  # cheaper -> positive


def test_cheapness_z_expensive_is_negative(monkeypatch):
    monkeypatch.setattr(analyzer, "VALUATION_Z_MIN_OBS", 3)
    series = pd.Series([10.0, 20.0, 30.0])
    assert analyzer._cheapness_z(series, 30.0) == pytest.approx(-1.0)


def test_cheapness_z_zero_std_is_none(monkeypatch):
    monkeypatch.setattr(analyzer, "VALUATION_Z_MIN_OBS", 3)
    assert analyzer._cheapness_z(pd.Series([20.0, 20.0, 20.0]), 20.0) is None


# --- verdict matrix ---------------------------------------------------------------


def _rate(support):
    return analyzer.RateView(rate_support=support)


def test_verdict_insufficient_data():
    assert "insufficient" in analyzer._verdict(None, _rate(False))


def test_verdict_no_discount_no_support_defaults_to_sp500():
    verdict = analyzer._verdict("little", _rate(False))
    assert "S&P 500 default" in verdict and "watch" not in verdict


def test_verdict_no_discount_with_support_watches_small_caps():
    verdict = analyzer._verdict("mild", _rate(True))
    assert "S&P 500 default" in verdict and "watch" in verdict


def test_verdict_candidate_without_support_keeps_sp500_default():
    verdict = analyzer._verdict("candidate", _rate(False))
    assert "no rate support" in verdict and "S&P 500 default" in verdict


def test_verdict_candidate_with_support_prefers_small_caps():
    verdict = analyzer._verdict("candidate", _rate(True))
    assert verdict.startswith("small caps") and "conditions both met" in verdict


def test_verdict_investigate_with_support_warns_about_stress():
    verdict = analyzer._verdict("investigate", _rate(True))
    assert "investigate" in verdict


# --- analyze() ---------------------------------------------------------------


def _valuations_df(rows):
    return pd.DataFrame(rows, columns=["date", "segment", "fwd_pe", "trailing_pe", "asof"])


def _one_day_history(sp500=20.0, world_small=15.0, europe=16.0):
    day = pd.Timestamp("2026-08-22")
    return _valuations_df(
        [
            [day, "sp500", sp500, 25.0, "July 31, 2026"],
            [day, "world_small", world_small, 19.0, "July 31, 2026"],
            [day, "europe", europe, 18.0, "July 31, 2026"],
        ]
    )


def _analyze_with(history):
    with patch("analyzer.valuations_history", return_value=history), patch(
        "collectors.fed_rate.latest_fedwatch", return_value=_fedwatch_df((0.8, 0.1, 0.1), (0.2, 0.7, 0.1), (0.2, 0.7, 0.1))
    ):
        return analyzer.analyze()


def test_analyze_computes_ratios_and_discounts():
    opp = _analyze_with(_one_day_history(sp500=20.0, world_small=15.0))
    small = next(s for s in opp.segments if s.name == "world_small")
    assert small.ratio_vs_spx == pytest.approx(0.75)
    assert small.discount_vs_spx == pytest.approx(0.25)
    assert opp.small_cap_band == "candidate"


def test_analyze_missing_valuations_cache_degrades_with_note():
    with patch("analyzer.valuations_history", side_effect=FileNotFoundError), patch(
        "collectors.fed_rate.latest_fedwatch", side_effect=FileNotFoundError
    ):
        opp = analyzer.analyze()
    assert all(s.fwd_pe is None for s in opp.segments)
    assert opp.small_cap_band is None
    assert "insufficient" in opp.verdict
    assert any("no valuations cache" in n for n in opp.notes)
    assert any("FedWatch expectations unavailable" in n for n in opp.notes)


def test_analyze_notes_insufficient_z_history():
    opp = _analyze_with(_one_day_history())
    assert opp.history_obs == 1
    assert any("z-scores" in n and "monthly" in n for n in opp.notes)
    assert all(s.fwd_z is None for s in opp.segments)


def test_analyze_z_scores_appear_with_enough_history(monkeypatch):
    monkeypatch.setattr(analyzer, "VALUATION_Z_MIN_OBS", 3)
    rows = []
    for i, (spx, small) in enumerate([(20.0, 16.0), (21.0, 15.0), (22.0, 14.0)]):
        day = pd.Timestamp("2026-06-20") + pd.Timedelta(days=i * 30)
        asof = f"Month {i}, 2026"  # distinct MSCI as-of -> distinct monthly observation
        rows.append([day, "sp500", spx, None, asof])
        rows.append([day, "world_small", small, None, asof])
    opp = _analyze_with(_valuations_df(rows))
    spx = next(s for s in opp.segments if s.name == "sp500")
    small = next(s for s in opp.segments if s.name == "world_small")
    assert spx.fwd_z == pytest.approx(-1.0)   # 22 vs mean 21, std 1 -> more expensive than norm
    assert small.fwd_z == pytest.approx(1.0)  # 14 vs mean 15, std 1 -> cheaper than norm
    assert small.rel_z is not None and small.rel_z > 0  # ratio compressed -> relatively cheap
    assert not any("z-scores" in n for n in opp.notes)


def test_analyze_repeated_daily_snapshots_of_same_month_count_once(monkeypatch):
    # MSCI only changes values monthly; 3 daily snapshots of one as-of must not fake 3 observations.
    monkeypatch.setattr(analyzer, "VALUATION_Z_MIN_OBS", 3)
    rows = []
    for i in range(3):
        day = pd.Timestamp("2026-08-20") + pd.Timedelta(days=i)
        rows.append([day, "sp500", 20.0, None, "July 31, 2026"])
    opp = _analyze_with(_valuations_df(rows))
    assert opp.history_obs == 1
    assert next(s for s in opp.segments if s.name == "sp500").fwd_z is None


# --- chart ---------------------------------------------------------------


def test_render_chart_bars_only_produces_png():
    opp = _analyze_with(_one_day_history())
    png = analyzer.render_chart(opp, history=_one_day_history())
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_chart_with_history_panel_produces_png():
    rows = []
    for i in range(6):  # past _HISTORY_PANEL_MIN_OBS -> second panel renders
        day = pd.Timestamp("2026-08-10") + pd.Timedelta(days=i)
        for segment, pe in (("sp500", 20.0 + i * 0.1), ("world_small", 15.0), ("europe", 16.0)):
            rows.append([day, segment, pe, None, None])
    history = _valuations_df(rows)
    opp = _analyze_with(history)
    png = analyzer.render_chart(opp, history=history)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
