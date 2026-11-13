"""Credit is context beside the checklist: calm below the stress line of its own last ten years."""

from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from buysignal.collectors import credit
from buysignal.config import CREDIT_STRESS_MIN_OBS, CREDIT_STRESS_WINDOW
from buysignal.signals import credit_signal


def observations(values):
    return pd.DataFrame({"date": pd.bdate_range(end="2026-10-30", periods=len(values)), "spread": values})


def _score(values):
    with patch.object(credit_signal, "credit_history", return_value=observations(values)):
        return credit_signal.score(asof=pd.Timestamp("2026-11-02"))


def _decade(last):
    """Ten years spread evenly over 1.00-3.00, so the 80th percentile sits near 2.60."""
    return [*np.linspace(1., 3., CREDIT_STRESS_WINDOW - 1), last]


@pytest.mark.parametrize("last, state, passes", [(2.0, "calm", True), (2.59, "calm", True), (2.61, "stressed", False), (4.0, "stressed", False)])
def test_credit_is_stressed_at_or_above_the_80th_percentile_of_its_last_ten_years(last, state, passes):
    signal = _score(_decade(last))
    assert (signal.state, signal.passes) == (state, passes)
    assert signal.metrics["stress"] == pytest.approx(2.6, abs=.01)
    assert signal.advisory  # context beside the checklist, never one of its conditions
    assert signal.observed_at == "2026-10-30"


def test_credit_changes_are_reported_in_basis_points():
    signal = _score([*([2.] * (CREDIT_STRESS_MIN_OBS + 25)), 2.5])
    assert signal.metrics["changes_bp"][21] == pytest.approx(50)
    assert signal.metrics["changes_bp"][5] == pytest.approx(50)


def test_the_stress_line_ignores_observations_older_than_ten_years():
    old_crisis = [9.] * 3000  # a wide era that has left the ten-year window
    signal = _score([*old_crisis, *_decade(2.7)])
    assert signal.state == "stressed"
    assert signal.metrics["stress"] == pytest.approx(2.6, abs=.01)


def test_credit_with_too_little_history_is_missing_not_calm():
    with pytest.raises(ValueError, match="stress line needs"):
        _score([2.] * (CREDIT_STRESS_MIN_OBS - 1))


@pytest.mark.parametrize("asof", ["2026-11-10", "2026-10-29"])
def test_stale_and_future_credit_observations_are_rejected(asof):
    with patch.object(credit_signal, "credit_history", return_value=observations([2.] * CREDIT_STRESS_MIN_OBS)):
        with pytest.raises(ValueError, match="freshness"):
            credit_signal.score(asof=pd.Timestamp(asof))


def test_rolling_source_window_preserves_older_local_observations(tmp_path):
    path = tmp_path / "credit.csv"
    old = observations([3.] * 30)
    old.to_csv(path, index=False)
    new = old.tail(5).rename(columns={"spread": "value"}).copy()
    new.loc[new.index[-1], "value"] = 4.
    with patch.object(credit, "fetch_series_with_retries", return_value=(new, None)):
        assert credit.update_credit_data(path) is None
    loaded = credit.credit_history(path)
    assert len(loaded) == 30
    assert loaded.spread.iloc[-1] == 4.


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1.])
def test_invalid_credit_fetch_keeps_the_old_cache(tmp_path, value):
    path = tmp_path / "credit.csv"
    path.write_text("last good cache")
    new = observations([value]).rename(columns={"spread": "value"})
    with patch.object(credit, "fetch_series_with_retries", return_value=(new, None)):
        assert "Invalid credit" in credit.update_credit_data(path)
    assert path.read_text() == "last good cache"


def test_a_missing_credit_file_demands_a_refresh(tmp_path):
    path = tmp_path / "creditbaa.csv"
    assert credit.demands_refresh(path)
    observations([2.] * 5).to_csv(path, index=False)
    assert not credit.demands_refresh(path)
