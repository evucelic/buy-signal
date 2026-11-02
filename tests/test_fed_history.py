"""Comparable FedWatch observations: same date, same reference, honest elapsed time."""

from unittest.mock import patch

import pandas as pd
import pytest

from buysignal.collectors import fed_rate


def snapshot(hike=.4, reference=375, fetched="2026-11-02T13:00:00+00:00"):
    return pd.DataFrame([
        {"meeting_date": pd.Timestamp(date), "horizon": horizon,
         "prob_ease": 0., "prob_no_change": 1-hike, "prob_hike": hike,
         "reference_lower_bps": reference, "reference_upper_bps": reference+25,
         "fetched_at": fetched, "source_asof": fetched}
        for date, horizon in (("2026-12-09", "nearest"), ("2027-04-28", "six_month"), ("2027-10-27", "one_year"))
    ])


def test_reference_and_source_timestamp_parse_from_live_page_text():
    metadata = fed_rate._reference_metadata("Current target rate is 375-400\n* Data as of 2 Nov 2026 01:31:50 CT")
    assert metadata["reference_lower_bps"] == 375
    assert metadata["reference_upper_bps"] == 400
    assert pd.Timestamp(metadata["source_asof"]) == pd.Timestamp("2026-11-02T07:31:50Z")


def test_probability_changes_use_percentage_points():
    previous = snapshot(fetched="2026-11-02T01:00:00Z")
    new = fed_rate._add_comparisons(snapshot(hike=.49), previous)
    assert new.delta_hike_pp.tolist() == pytest.approx([9.] * 3)
    assert new.delta_no_change_pp.tolist() == pytest.approx([-9.] * 3)
    assert (pd.Timestamp(new.fetched_at.iloc[0]) - pd.Timestamp(new.comparison_since.iloc[0])).total_seconds() == 12 * 3600


def test_matching_is_by_meeting_not_the_horizon_label():
    previous = snapshot(fetched="2026-11-02T01:00:00Z")
    previous["horizon"] = ["six_month", "one_year", "nearest"]
    new = fed_rate._add_comparisons(snapshot(hike=.5), previous)
    assert new.delta_hike_pp.tolist() == pytest.approx([10.] * 3)


def test_reference_rate_change_resets_the_baseline():
    result = fed_rate._add_comparisons(snapshot(reference=400), snapshot(fetched="2026-11-02T01:00:00Z"))
    assert result.delta_hike_pp.isna().all()
    assert result.comparison_status.str.contains("range changed").all()


def test_new_meeting_does_not_inherit_another_meetings_change():
    new = snapshot()
    new.loc[0, "meeting_date"] = pd.Timestamp("2026-12-16")
    result = fed_rate._add_comparisons(new, snapshot(fetched="2026-11-02T01:00:00Z"))
    assert pd.isna(result.delta_hike_pp.iloc[0])
    assert "New meeting" in result.comparison_status.iloc[0]


def test_legacy_cache_without_reference_has_no_fabricated_change():
    previous = snapshot().drop(columns=["reference_lower_bps", "reference_upper_bps"])
    result = fed_rate._add_comparisons(snapshot(), previous)
    assert result.delta_hike_pp.isna().all()


def test_backwards_source_timestamp_cannot_look_like_repricing():
    previous = snapshot(fetched="2026-11-02T01:00:00Z")
    new = snapshot(hike=.5)
    new["source_asof"] = "2026-11-01T23:00:00Z"
    result = fed_rate._add_comparisons(new, previous)
    assert result.delta_hike_pp.isna().all()


def test_reference_change_during_scrape_is_retried():
    frame = snapshot()
    frame.loc[2, "reference_lower_bps"] = 400
    frame.loc[2, "reference_upper_bps"] = 425
    with pytest.raises(ValueError, match="during collection"):
        fed_rate._validate_snapshot(frame, pd.Timestamp("2026-11-02"))


def test_unchanged_source_timestamp_is_not_a_new_consensus_observation():
    old = snapshot(fetched="2026-11-02T01:00:00Z")
    new = snapshot(hike=.5)
    new["source_asof"] = old.source_asof
    result = fed_rate._add_comparisons(new, old)
    assert result.delta_hike_pp.isna().all()
    assert result.comparison_status.str.contains("unchanged").all()
