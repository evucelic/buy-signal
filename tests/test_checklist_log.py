"""The checklist log: one line per session, never rewritten, readable after a torn write."""

import json
from datetime import datetime, timezone


from buysignal.analysis import checklist_log
from buysignal.signals.base import SubSignal
from buysignal.signals.buy_signal import assess

NOW = datetime(2026, 11, 16, 21, 5, tzinfo=timezone.utc)


def _result():
    return assess([
        SubSignal("vix", 25.0, "soft", "", passes=True, observed_at="2026-11-16"),
        SubSignal("fed_rate", .79, "hiking", "", passes=False, observed_at="2026-11-16T18:50:00Z",
                  metrics={"blocking_horizons": ["six_month"]}),
        SubSignal("margin_debt", -.02, "deleveraging", "", passes=True, observed_at="2026-10-01",
                  metrics={"freshness": "fresh"}),
        SubSignal("market_dip", -.031, "flat", "", passes=False, observed_at="2026-11-16", metrics={"price": 671.2}),
        SubSignal("credit", 2.12, "calm", "", passes=True, advisory=True, observed_at="2026-11-13"),
    ], ["sector"])


def test_a_row_records_each_condition_with_its_status_and_data_date():
    row = checklist_log.row(_result(), "2026-11-16", NOW, {"VUAA": (151.2, "2026-11-16", "USD")})
    assert (row["session"], row["count"], row["of"], row["incomplete"]) == ("2026-11-16", 2, 5, True)
    assert row["rules"] == "2026-11-05" and row["logged_at"] == "2026-11-16T21:05:00+00:00"
    assert row["conditions"]["vix"] == {"value": 25.0, "observed": "2026-11-16", "status": "pass"}
    assert row["conditions"]["fed_rate"]["blocking"] == ["six_month"] and row["conditions"]["fed_rate"]["status"] == "fail"
    assert row["conditions"]["sector"] == {"status": "incomplete"}
    assert row["context"] == {"credit": {"value": 2.12, "observed": "2026-11-13"}}
    assert row["spy"] == {"close": 671.2, "observed": "2026-11-16"}
    assert row["etfs"] == {"VUAA": {"close": 151.2, "observed": "2026-11-16", "currency": "USD"}}
    json.dumps(row, allow_nan=False)


def test_a_session_is_written_once_and_never_rewritten(tmp_path):
    path = tmp_path / "log.jsonl"
    first = checklist_log.row(_result(), "2026-11-16", NOW)
    assert checklist_log.append(first, path)
    later = dict(first, count=5)
    assert not checklist_log.append(later, path)
    assert checklist_log.append(checklist_log.row(_result(), "2026-11-17", NOW), path)
    rows = checklist_log.read(path)
    assert [row["session"] for row in rows] == ["2026-11-16", "2026-11-17"] and rows[0]["count"] == 2


def test_a_torn_last_line_is_skipped_and_the_next_session_starts_on_its_own_line(tmp_path):
    path = tmp_path / "log.jsonl"
    checklist_log.append(checklist_log.row(_result(), "2026-11-16", NOW), path)
    with path.open("a") as handle:
        handle.write('{"session":"2026-11-17","cou')
    assert [row["session"] for row in checklist_log.read(path)] == ["2026-11-16"]
    assert checklist_log.append(checklist_log.row(_result(), "2026-11-17", NOW), path)
    assert [row["session"] for row in checklist_log.read(path)] == ["2026-11-16", "2026-11-17"]


def test_a_missing_log_reads_as_empty(tmp_path):
    assert checklist_log.read(tmp_path / "absent.jsonl") == []
