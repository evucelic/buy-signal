"""Shiller's CAPE workbook: months read from his number format, and today against similar months."""
import numpy as np
import pandas as pd
import pytest

from buysignal.collectors import cape


def sheet(months, capes, excess=None, stocks=None, bonds=None):
    rows = np.full((len(months), 22), np.nan, dtype=object)
    rows[:, 0], rows[:, 12] = months, capes
    rows[:, 16] = excess if excess is not None else .03
    rows[:, 19] = stocks if stocks is not None else np.nan
    rows[:, 20] = bonds if bonds is not None else np.nan
    return pd.DataFrame(rows)


def test_october_written_as_a_tenth_is_read_as_october():
    frame = cape.table(sheet([1881.01, 1881.09, 1881.1, 1881.11], [18., 18.5, 19., 19.2]))

    assert [d.month for d in frame.date] == [1, 9, 10, 11]


def test_a_note_row_and_blank_months_are_left_out():
    raw = sheet([1881.01, 1881.02, 1881.03], [18., 18.5, 19.])
    raw.loc[3] = ["Nov price is an average", *[np.nan] * 21]

    assert len(cape.table(raw)) == 3


def test_implausible_values_are_refused():
    with pytest.raises(ValueError, match="implausible"):
        cape.table(sheet([1881.01, 1881.02], [18., 900.]))


def test_today_is_compared_with_months_of_a_similar_excess_yield():
    months = pd.date_range("1881-01-01", periods=600, freq="MS")
    excess = np.where(np.arange(600) % 2, .01, .05)
    frame = pd.DataFrame({"date": months, "cape": 20., "excess_yield": excess,
                          "stocks_10y": np.where(excess < .03, .02, .08), "bonds_10y": .01})
    frame.loc[599, ["excess_yield", "stocks_10y", "bonds_10y"]] = [.011, np.nan, np.nan]

    result = cape.summary(frame)

    assert result["outcome"]["months"] == 299 and result["outcome"]["stocks"][1] == pytest.approx(.02)
    assert result["outcome"]["over_bonds"][1] == pytest.approx(.01)
