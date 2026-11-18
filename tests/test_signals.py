"""Tests for signals/*.py: every state-machine branch, both passing and failing, per module."""

from unittest.mock import patch

import pandas as pd
import pytest

from buysignal.signals import margin_signal, market_signal, rate_signal, sector_signal, vix_signal, yield_curve_signal
from buysignal.config import MARKET_DIP_THRESHOLD, YIELD_CURVE_BUCKET_DAYS
from buysignal.signals.base import NONE, SOFT, STRONG, SubSignal
from buysignal.signals.buy_signal import SIGNAL_LOCK, assess, compute_signal

# vix_signal

_CACHED_VIX = 18.0
_LIVE_VIX = 31.0


def _vix(vix, change=None):
    with patch("buysignal.signals.vix_signal.vix_change_pct", return_value=change):
        return vix_signal.score(vix=vix)


def _vix_sources(monkeypatch, cached=_CACHED_VIX, live=_LIVE_VIX):
    monkeypatch.setattr("buysignal.signals.vix_signal._vix_session_is_current", lambda *args: True)
    monkeypatch.setattr("buysignal.signals.vix_signal.load_latest_cached_vix", lambda: cached)
    monkeypatch.setattr("buysignal.signals.vix_signal.get_latest_vix", lambda: live)
    monkeypatch.setattr("buysignal.signals.vix_signal.vix_change_pct", lambda: None)


@pytest.mark.parametrize("level, state, passes", [
    (30.0, STRONG, True), (29.9, SOFT, True), (22.0, SOFT, True), (21.9, NONE, False), (15.0, NONE, False), (5.0, NONE, False),
])
def test_vix_passes_from_22_and_is_strong_from_30(level, state, passes):
    s = _vix(level)

    assert (s.state, s.passes) == (state, passes)


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


# rate_signal


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
    with patch("buysignal.signals.rate_signal.latest_fedwatch", return_value=expectations):
        return rate_signal.score(asof=pd.Timestamp("2026-07-01"))


@pytest.mark.parametrize("ease, no_change, hike, state, passes", [
    (0.0, 0.3, 0.7, "hiking", False),
    (0.0, 0.5, 0.5, "hiking", False),   # a tie with "higher" counts as higher
    (0.7, 0.3, 0.0, "cutting", True),
    (0.2, 0.6, 0.2, "no_change", True),
])
def test_rate_passes_unless_higher_leads_or_ties(ease, no_change, hike, state, passes):
    s = _rate(_fedwatch(ease=ease, no_change=no_change, hike=hike))

    assert (s.state, s.passes) == (state, passes)


def test_rate_missing_horizons_requires_refresh():
    without_horizons = pd.DataFrame(
        [
            {"meeting_date": pd.Timestamp("2026-08-01"), "prob_ease": 0.1, "prob_no_change": 0.2, "prob_hike": 0.7},
            {"meeting_date": pd.Timestamp("2027-07-01"), "prob_ease": 0.0, "prob_no_change": 0.9, "prob_hike": 0.1},
        ]
    )

    with pytest.raises(ValueError, match="horizons missing"):
        _rate(without_horizons)


def test_rate_zero_distribution_is_rejected():
    with pytest.raises(ValueError, match="distribution"):
        _rate(_fedwatch(ease=0.0, no_change=0.0, hike=0.0))


# margin_signal


def _margin_history(balances):
    return pd.DataFrame(
        {
            "month": pd.date_range("2026-01-01", periods=len(balances), freq="MS"),
            "debit_balances": balances,
        }
    )


def _margin(balances, freshness=("fresh", "ok")):
    with patch("buysignal.signals.margin_signal.margin_history", return_value=_margin_history(balances)), patch(
        "buysignal.signals.margin_signal.data_freshness", return_value=freshness
    ):
        return margin_signal.score()


@pytest.mark.parametrize("balances, passes", [
    ([1000, 900, 800], True),
    ([800, 900, 1000], False),
    ([1000, 1000], False),   # unchanged is not deleveraging
    ([1000], False),         # one month shows no change at all
])
def test_margin_passes_when_debt_fell_over_the_last_month(balances, passes):
    assert _margin(balances).passes is passes


def test_margin_a_decrease_followed_by_an_increase_is_not_deleveraging(monkeypatch):
    monkeypatch.setattr("buysignal.signals.margin_signal.MARGIN_DELEVERAGE_MONTHS", 2)

    assert _margin([1000, 900, 950]).passes is False


# market_signal

def _market(drawdown):
    values = [100.] * 219 + [100 * (1 + drawdown)]
    history = pd.DataFrame({name: values for name in ("SPY", "NASDAQ", "DOW")},
                           index=pd.bdate_range("2025-01-01", periods=len(values)))
    with patch("buysignal.signals.market_signal.get_entry_history", return_value=history):
        return market_signal.score()


@pytest.mark.parametrize("drawdown, passes", [
    (-.07, True), (MARKET_DIP_THRESHOLD, True), (MARKET_DIP_THRESHOLD + .0001, False), (-.01, False),
])
def test_a_dip_is_5_percent_or_more_below_the_3_month_high(drawdown, passes):
    assert _market(drawdown).passes is passes


def test_market_score_preserves_allow_refresh_flag():
    history = pd.DataFrame({name: [100.] * 220 for name in ("SPY", "NASDAQ", "DOW")},
                           index=pd.bdate_range("2025-01-01", periods=220))
    with patch("buysignal.signals.market_signal.get_entry_history", return_value=history) as get:
        market_signal.score(allow_refresh=False)
    get.assert_called_once_with(allow_refresh=False)


def test_market_index_changes_keep_the_index_order():
    assert list(_market(-.07).metrics["index_changes"])[:3] == ["SPY", "NASDAQ", "DOW"]


# sector_signal


def _sector_df(growths):
    return pd.DataFrame(
        {
            "industry": [f"Industry {i}" for i in range(len(growths))],
            "market_cap": [1e12 * (i + 1) for i in range(len(growths))],
            "earnings_growth_estimate_1y": growths,
            "estimate_coverage": [1.] * len(growths),
            "covered_companies": [8] * len(growths),
            "sampled_companies": [8] * len(growths),
            "covered_weight": [.8] * len(growths),
        }
    )


def test_sector_passes_at_quorum():
    with patch("buysignal.signals.sector_signal.sector_performance", return_value=_sector_df([0.1, 0.1, 0.1, -0.1, -0.1])):
        s = sector_signal.score()
        assert s.passes is True
        assert s.state == "growing"


def test_sector_fails_below_quorum():
    with patch("buysignal.signals.sector_signal.sector_performance", return_value=_sector_df([0.1, 0.1, -0.1, -0.1, -0.1])):
        s = sector_signal.score()
        assert s.passes is False
        assert s.state == "flat"


def test_sector_extreme_none_growing():
    with patch("buysignal.signals.sector_signal.sector_performance", return_value=_sector_df([-0.1] * 5)):
        s = sector_signal.score()
        assert s.passes is False
        assert s.score == 0.0


def test_sector_extreme_all_growing():
    with patch("buysignal.signals.sector_signal.sector_performance", return_value=_sector_df([0.1] * 5)):
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
    row = {"industry": "X", "market_cap": 1e12, "estimate_coverage": 1., "covered_companies": 8, "sampled_companies": 8, "covered_weight": .8, "earnings_growth_estimate_1y": 0.1}
    assert "📈" in sector_signal._format_industry(row)


def test_format_industry_trend_icon_negative():
    row = {"industry": "X", "market_cap": 1e12, "estimate_coverage": 1., "covered_companies": 8, "sampled_companies": 8, "covered_weight": .8, "earnings_growth_estimate_1y": -0.1}
    assert "📉" in sector_signal._format_industry(row)


def test_format_industry_trend_icon_zero_is_not_growth():
    row = {"industry": "X", "market_cap": 1e12, "estimate_coverage": 1., "covered_companies": 8, "sampled_companies": 8, "covered_weight": .8, "earnings_growth_estimate_1y": 0.0}
    assert "📉" in sector_signal._format_industry(row)


# yield_curve_signal


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


def _curve(spreads, policy=None):
    with patch(
        "buysignal.signals.yield_curve_signal.yield_curve_history", return_value=_spread_df(spreads, policy)
    ):
        return yield_curve_signal.score()


@pytest.mark.parametrize("buckets, state", [
    ([1.0, 0.5], "steep"),
    ([0.99, 0.5], "flat"),
    ([0.0, 0.5], "flat"),                    # zero is not inverted
    ([-0.2, 0.5], "inverted"),
    ([-1.0, -1.0, -1.2], "deep_inversion"),  # two complete deep buckets
    ([-0.3, -1.5, -1.5], "inverted"),        # one deep complete bucket is not enough
    ([0.5, -2.0], "flat"),                   # the bucket still filling is not judged
    ([-2.0], "flat"),
])
def test_yield_curve_state_comes_from_complete_three_week_buckets(buckets, state):
    assert _curve(buckets).state == state


def test_yield_curve_is_advisory():
    s = _curve([0.5, 0.5])
    assert s.advisory is True
    assert s.name == "yield_curve"


def test_yield_curve_averages_mark_the_filling_bucket():
    buckets = _curve([0.5, 0.8]).metrics["buckets"]
    assert [(b["thru"], b["partial"]) for b in buckets] == [("2026-01-21", False), ("2026-02-11", True)]
    assert [b["spread"] for b in buckets] == pytest.approx([0.5, 0.8])


def test_policy_spread_fires_after_two_complete_negative_buckets():
    s = _curve([0.5, 0.5, 0.5], policy=[-0.2, -0.41, -0.5])
    assert s.metrics["cuts_priced"] and s.metrics["cuts_since"] == "2026-01-01"
    assert [b["policy_spread"] for b in s.metrics["buckets"]] == pytest.approx([-0.2, -0.41, -0.5])


def test_policy_spread_silent_below_the_confirmation_threshold():
    s = _curve([0.5, 0.5, 0.5], policy=[0.3, -0.41, -0.5])
    assert s.metrics["cuts_priced"] is False and s.metrics["cuts_since"] is None


def test_policy_spread_ignores_the_filling_bucket():
    s = _curve([0.5, 0.5, 0.5, 0.5], policy=[0.3, 0.3, -0.41, -0.5])
    assert s.metrics["cuts_priced"] is False


def test_policy_spread_gap_breaks_the_run_instead_of_collapsing():
    history = _spread_df([0.5, 0.5, 0.5, 0.5, 0.5], policy=[-0.4, -0.5, 0.0, 0.0, 0.0])
    stale = history["date"] >= pd.Timestamp("2026-01-01") + pd.Timedelta(days=42)
    history.loc[stale, "policy_spread"] = float("nan")

    with patch("buysignal.signals.yield_curve_signal.yield_curve_history", return_value=history):
        s = yield_curve_signal.score()

    assert s.metrics["cuts_priced"] is None


def test_policy_spread_silent_when_positive():
    s = _curve([0.5, 0.5, 0.5], policy=[0.3, 0.25, 0.2])
    assert s.metrics["cuts_priced"] is False


def test_policy_spread_omitted_when_cache_predates_the_column():
    s = _curve([0.5, 0.5])
    assert s.metrics["cuts_priced"] is None
    assert all(b["policy_spread"] is None for b in s.metrics["buckets"])


def test_policy_spread_does_not_change_the_curve_state():
    assert _curve([0.5, 0.5, 0.5], policy=[-2.0, -2.0, -2.0]).state == "flat"


# buy_signal


REQUIRED = ("vix", "fed_rate", "margin_debt", "market_dip", "sector")


def _checklist(passing=REQUIRED, missing=(), stale=(), context=()):
    """The five conditions: those in `passing` pass, `missing` are left out, `stale` are marked stale."""
    subs = [SubSignal(name, 0.0, "x", "", passes=name in passing, metrics={"stale": True} if name in stale else {})
            for name in REQUIRED if name not in missing]
    return assess([*subs, *context])


@pytest.mark.parametrize("passing,count,state", [
    (REQUIRED, 5, "strong"),
    (("vix", "margin_debt", "sector"), 3, "none"),
    ((), 0, "none"),
])
def test_checklist_counts_the_conditions_that_pass(passing, count, state):
    result = _checklist(passing)
    assert (result.passing_count, result.required_count, result.state) == (count, 5, state)
    assert result.label == f"{count} of 5"


def test_a_missing_condition_stays_in_the_denominator():
    result = _checklist(missing=("fed_rate",))
    assert (result.passing_count, result.required_count, result.state) == (4, 5, "incomplete")
    assert result.label == "4 of 5 incomplete"
    assert result.status("fed_rate") == "incomplete"


def test_a_stale_condition_cannot_pass_and_marks_the_checklist_incomplete():
    result = _checklist(stale=("margin_debt",))
    assert (result.passing_count, result.state) == (4, "incomplete")
    assert result.status("margin_debt") == "incomplete"
    assert result.status("vix") == "pass"


def test_context_readings_never_change_the_count():
    context = [SubSignal(name, 0.0, "x", "", passes=False, advisory=True) for name in ("credit", "yield_curve")]
    assert _checklist(context=context).label == "5 of 5"
    assert _checklist().label == "5 of 5"


def test_checklist_with_no_evidence_is_incomplete():
    result = assess([])
    assert (result.passing_count, result.required_count, result.state) == (0, 5, "incomplete")


def test_required_signals_are_not_advisory_and_context_ones_are():
    assert _margin([100, 90]).advisory is False
    assert _market(MARKET_DIP_THRESHOLD - .01).advisory is False
    assert _curve([1.5, 1.5]).advisory is True


def test_a_margin_month_past_its_release_window_is_stale():
    assert _margin([100, 90], freshness=("stale", "late")).metrics["stale"] is True
    assert _margin([100, 90]).metrics["stale"] is False


_SIGNAL_MODULES = {
    "vix_signal": "vix",
    "rate_signal": "fed_rate",
    "margin_signal": "margin_debt",
    "market_signal": "market_dip",
    "sector_signal": "sector",
    "credit_signal": "credit",
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
    """Compute the checklist with every condition passing, except what this test names.

    Each override is a SubSignal to return, an Exception to raise, or a score() replacement.
    """
    outcomes = {module: _sub(name, advisory=name not in REQUIRED) for module, name in _SIGNAL_MODULES.items()}
    outcomes.update(overrides)

    for module, outcome in outcomes.items():
        monkeypatch.setattr(f"buysignal.signals.buy_signal.{module}.score", _scoring(outcome))
    return compute_signal(allow_refresh=allow_refresh)


def test_compute_signal_counts_all_five_when_every_condition_passes(monkeypatch):
    result = _compute(monkeypatch)

    assert (result.state, result.label) == ("strong", "5 of 5")
    assert result.missing_signals == []


def test_compute_signal_counts_a_failing_condition_out(monkeypatch):
    result = _compute(monkeypatch, rate_signal=_sub("fed_rate", passes=False))

    assert (result.state, result.label) == ("none", "4 of 5")


def test_compute_signal_ignores_a_failing_context_signal(monkeypatch):
    failing_curve = _sub("yield_curve", passes=False, state="inverted", advisory=True)

    result = _compute(monkeypatch, yield_curve_signal=failing_curve, credit_signal=_sub("credit", passes=False, advisory=True))

    assert result.label == "5 of 5"


def test_compute_signal_shows_every_reading_in_checklist_order(monkeypatch):
    result = _compute(monkeypatch)

    assert [s.name for s in result.subsignals] == [
        "vix", "fed_rate", "margin_debt", "market_dip", "sector", "credit", "yield_curve"]


def test_compute_signal_keeps_its_count_when_a_context_signal_is_missing(monkeypatch):
    result = _compute(monkeypatch, yield_curve_signal=RuntimeError("no cache"), credit_signal=RuntimeError("fred down"))

    assert (result.state, result.label) == ("strong", "5 of 5")
    assert result.missing_signals == ["credit", "yield_curve"]


@pytest.mark.parametrize("module,name", [(module, name) for module, name in _SIGNAL_MODULES.items() if name in REQUIRED])
def test_compute_signal_is_incomplete_when_a_condition_is_missing(monkeypatch, module, name):
    result = _compute(monkeypatch, **{module: RuntimeError("boom")})

    assert (result.state, result.label) == ("incomplete", "4 of 5 incomplete")
    assert result.missing_signals == [name]
    assert len(result.subsignals) == 6


def test_compute_signal_reports_every_signal_missing_when_all_of_them_fail(monkeypatch):
    boom = {module: RuntimeError("boom") for module in _SIGNAL_MODULES}

    result = _compute(monkeypatch, **boom)

    assert result.subsignals == []
    assert result.state == "incomplete"
    assert sorted(result.missing_signals) == sorted(_SIGNAL_MODULES.values())


def test_compute_signal_passes_allow_refresh_through_to_the_refetching_signals(monkeypatch):
    def vix_score(vix=None, allow_refresh=True):
        return _sub("vix", passes=allow_refresh, state=SOFT if allow_refresh else NONE)

    def market_score(allow_refresh=True):
        return _sub("market_dip", passes=allow_refresh)

    result = _compute(
        monkeypatch, allow_refresh=False, vix_signal=vix_score, market_signal=market_score
    )

    by_name = {signal.name: signal for signal in result.subsignals}
    assert not by_name["vix"].passes and not by_name["market_dip"].passes


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


def test_rate_pause_with_later_hikes_fails():
    df = _fedwatch(ease=0.0, no_change=0.906, hike=0.094)
    df.loc[df.horizon != "nearest", ["prob_no_change", "prob_hike"]] = [0.02, 0.98]
    signal = _rate(df)
    assert not signal.passes
    assert signal.state == "hiking"


def test_rate_past_meeting_is_missing_not_supportive():
    df = _fedwatch(ease=0.2, no_change=0.7, hike=0.1)
    df.loc[df.horizon == "nearest", "meeting_date"] = pd.Timestamp("2026-06-30")
    with pytest.raises(ValueError, match="past meeting"):
        _rate(df)
