"""Credit context keeps its own observation clock and never gates the entry checklist."""

from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from buysignal.collectors import credit
from buysignal.signals import credit_signal


def observations(values):
    return pd.DataFrame({"date": pd.bdate_range(end="2026-10-30", periods=len(values)), "spread": values})


@pytest.mark.parametrize("last, state", [(4.5, "widening"), (3.5, "narrowing"), (4.01, "stable")])
def test_credit_direction_and_units(last, state):
    frame = observations([4.] * 25 + [last])
    with patch.object(credit_signal, "credit_history", return_value=frame):
        signal = credit_signal.score(asof=pd.Timestamp("2026-11-02"))
    assert signal.state == state
    assert signal.advisory
    assert signal.metrics["changes_bp"][21] == pytest.approx((last - 4) * 100)
    assert signal.observed_at == "2026-10-30"


def test_credit_insufficient_change_history_is_unknown():
    with patch.object(credit_signal, "credit_history", return_value=observations([4.])):
        assert credit_signal.score(asof=pd.Timestamp("2026-11-02")).state == "unknown"


@pytest.mark.parametrize("asof", ["2026-11-10", "2026-10-29"])
def test_stale_and_future_credit_observations_are_rejected(asof):
    with patch.object(credit_signal, "credit_history", return_value=observations([4.] * 30)):
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
