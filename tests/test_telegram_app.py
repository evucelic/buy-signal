"""Tests for telegram_bot/app.py: what the bot decides, remembers and replies.

The rendering it delegates to is covered in test_telegram_render.py; what's checked here is
the decision - whether an alert edge was crossed, whether the daily report is due, which
command ran, and what reached the transport.
"""

from datetime import date, datetime, timezone

import pytest

from buysignal.schedule.journal import RefreshJournal
from buysignal.telegram_bot import app
from buysignal.telegram_bot.messages import Report

def _text(reply):
    return "\n".join(m.text for m in reply.messages) if isinstance(reply, Report) else reply



@pytest.fixture(autouse=True)
def reset_state(monkeypatch, tmp_path):
    """Reset the module-level state, disable the daily report by default, and keep the refresh
    journal on tmp_path so no test reads or writes the real one.
    """
    app._state = app._State(start_time=datetime.now(timezone.utc))
    monkeypatch.setattr(app, "_snapshot_store", app.SnapshotStore(tmp_path / "reports"))
    monkeypatch.setattr(app.config, "DAILY_REPORT_HOUR_CT", -1)
    journal = RefreshJournal(tmp_path / "buysignal.schedule.journal.json")
    monkeypatch.setattr(app.runner, "macro_journal", lambda: journal)
    monkeypatch.setattr(app.runner, "refresh_macro", lambda **kwargs: [])
    yield journal


@pytest.fixture
def sent(monkeypatch):
    """Everything the app hands to the transport, in order, all of it delivered."""
    messages = []
    monkeypatch.setattr(app.transport, "send", lambda text, **kwargs: messages.append(text) or True)
    return messages


@pytest.fixture
def undeliverable(monkeypatch):
    """A transport that swallows everything, the way it does when Telegram is unreachable."""
    attempts = []
    monkeypatch.setattr(app.transport, "send", lambda text, **kwargs: attempts.append(text) and False)
    return attempts


def _ct(hour, day=24):
    return datetime(2026, 7, day, hour, 0, tzinfo=app.market_hours.CT)


def _never_computes():
    raise AssertionError("compute_signal() must not run for this case")


# --- is_alerting / trigger_reasons ---------------------------------------------------------


def test_is_alerting_vix_only(make_subsignal, make_buy_signal):
    result = make_buy_signal([make_subsignal("vix", "soft", "x", passes=True)])
    assert app.is_alerting(result) is True


def test_is_alerting_dip_only(make_subsignal, make_buy_signal):
    result = make_buy_signal([make_subsignal("market_dip", "dip", "x", passes=True)])
    assert app.is_alerting(result) is True


def test_is_alerting_checklist_soft(make_subsignal, make_buy_signal):
    result = make_buy_signal([make_subsignal("vix", "none", "x")], state="soft")
    assert app.is_alerting(result) is True


def test_is_alerting_false_when_nothing_triggers(make_subsignal, make_buy_signal):
    result = make_buy_signal([make_subsignal("vix", "none", "x")], state="none")
    assert app.is_alerting(result) is False


def test_trigger_reasons_combines_all(make_subsignal, make_buy_signal):
    result = make_buy_signal(
        [
            make_subsignal("vix", "soft", "x", passes=True),
            make_subsignal("market_dip", "dip", "x", passes=True),
        ],
        state="soft",
    )
    assert app.trigger_reasons(result) == "VIX elevated, SPY dip, checklist soft"


def test_trigger_reasons_empty_when_nothing_triggers(make_subsignal, make_buy_signal):
    result = make_buy_signal([make_subsignal("vix", "none", "x")], state="none")
    assert app.trigger_reasons(result) == ""


# --- handle_tick: alert edge-triggering ---------------------------------------------------------


def test_handle_tick_alert_edge_triggering(monkeypatch, sent, make_subsignal, make_buy_signal):
    monkeypatch.setattr(app.market_hours, "market_session", lambda: app.market_hours.REGULAR)

    not_alerting = make_buy_signal([make_subsignal("vix", "none", "x", passes=False)])
    alerting = make_buy_signal([make_subsignal("vix", "soft", "x", passes=True)])

    app.handle_tick(not_alerting, None)
    assert len(sent) == 0

    app.handle_tick(alerting, None)
    assert len(sent) == 1
    assert "Watch trigger active" in sent[0]

    app.handle_tick(alerting, None)
    assert len(sent) == 1  # no repeat while sustained

    app.handle_tick(not_alerting, None)
    assert len(sent) == 2
    assert "cleared" in sent[-1].lower()


def test_handle_tick_records_last_tick_and_error():
    app.handle_tick(None, RuntimeError("boom"))
    assert app._state.last_ok is False
    assert app._state.last_error == "boom"
    assert app._state.last_tick_at is not None


# --- daily report: when it is due, and what it shows ---------------------------------------


def test_report_is_due_at_the_report_hour_when_none_was_sent_today():
    assert app.report_due(_ct(20), last_report_date=None, report_hour=20) is True


def test_report_is_not_due_an_hour_before_the_report_hour():
    assert app.report_due(_ct(19), last_report_date=None, report_hour=20) is False


def test_report_can_retry_or_start_late_after_the_report_hour():
    assert app.report_due(_ct(21), last_report_date=None, report_hour=20) is True


def test_report_is_not_due_again_on_a_date_already_reported():
    assert app.report_due(_ct(20), last_report_date=date(2026, 7, 24), report_hour=20) is False


def test_report_is_due_again_the_next_day():
    assert app.report_due(_ct(20, day=25), last_report_date=date(2026, 7, 24), report_hour=20) is True


def test_report_shows_this_ticks_result_when_the_tick_produced_one(make_buy_signal):
    ticked = make_buy_signal([])
    cached = make_buy_signal([])

    chosen = app.report_result(ticked, None, cached, compute=_never_computes)

    assert chosen is ticked


def test_report_computes_fresh_when_an_idle_tick_produced_nothing(make_buy_signal):
    fresh = make_buy_signal([])
    cached = make_buy_signal([])

    chosen = app.report_result(None, None, cached, compute=lambda: fresh)

    assert chosen is fresh


def test_report_falls_back_to_the_cache_when_the_tick_failed(make_buy_signal):
    cached = make_buy_signal([])

    chosen = app.report_result(None, RuntimeError("boom"), cached, compute=_never_computes)

    assert chosen is cached


def test_report_has_nothing_to_show_when_the_tick_failed_with_no_cache():
    assert app.report_result(None, RuntimeError("boom"), None, compute=_never_computes) is None


# --- handle_tick: daily-report wiring ---------------------------------------------------------


def test_handle_tick_sends_the_daily_report_once_per_day(monkeypatch, sent, make_subsignal, make_buy_signal):
    monkeypatch.setattr(app.config, "DAILY_REPORT_HOUR_CT", 20)
    result = make_buy_signal([make_subsignal("vix", "none", "x", passes=False)])
    monkeypatch.setattr(app, "compute_signal", lambda: result)

    app.handle_tick(result, None, now=_ct(20))
    app.handle_tick(result, None, now=_ct(20))

    assert len([s for s in sent if "End of day" in s]) == 1


def test_handle_tick_force_refreshes_and_recomputes_before_the_daily_report(
    monkeypatch, sent, make_subsignal, make_buy_signal
):
    """The tick's result was computed before any refresh, so the report must not reuse it."""
    monkeypatch.setattr(app.config, "DAILY_REPORT_HOUR_CT", 20)
    calls = []
    monkeypatch.setattr(app.runner, "refresh_macro", lambda **kwargs: calls.append(("refresh", kwargs["force"])) or [])
    fresh = make_buy_signal([make_subsignal("vix", "none", "fresh-detail", passes=False)])
    monkeypatch.setattr(app, "compute_signal", lambda: calls.append(("compute",)) or fresh)
    stale = make_buy_signal([make_subsignal("vix", "none", "stale-detail", passes=False)])

    app.handle_tick(stale, None, now=_ct(20))

    assert calls == [("refresh", True), ("compute",)]
    report = next(s for s in sent if "End of day" in s)
    assert app._state.last_result is fresh
    assert len(sent) == 1
    assert "stale-detail" not in "\n".join(sent)


def test_handle_tick_sends_the_daily_reports_refresh_failures_in_the_same_tick(
    monkeypatch, sent, reset_state, make_subsignal, make_buy_signal
):
    monkeypatch.setattr(app.config, "DAILY_REPORT_HOUR_CT", 20)
    now = _ct(20)
    monkeypatch.setattr(
        app.runner,
        "refresh_macro",
        lambda **kwargs: reset_state.record("fed_rate", ok=False, error="tab crashed", now=kwargs["now"]) or [],
    )
    result = make_buy_signal([make_subsignal("vix", "none", "x", passes=False)])
    monkeypatch.setattr(app, "compute_signal", lambda: result)

    app.handle_tick(result, None, now=now)

    assert any("tab crashed" in s for s in sent)


def test_handle_tick_sends_no_daily_report_outside_the_report_hour(monkeypatch, sent, make_subsignal, make_buy_signal):
    monkeypatch.setattr(app.config, "DAILY_REPORT_HOUR_CT", 20)
    result = make_buy_signal([make_subsignal("vix", "none", "x", passes=False)])

    app.handle_tick(result, None, now=_ct(19))

    assert not any("End of day" in s for s in sent)


def test_handle_tick_sends_no_daily_report_when_the_tick_failed_with_no_cache(monkeypatch, sent):
    monkeypatch.setattr(app.config, "DAILY_REPORT_HOUR_CT", 20)

    app.handle_tick(None, RuntimeError("boom"), now=_ct(20))

    assert not any("End of day" in s for s in sent)


def test_handle_tick_flags_the_error_when_the_daily_report_falls_back_to_the_cache(
    monkeypatch, sent, make_subsignal, make_buy_signal
):
    monkeypatch.setattr(app.config, "DAILY_REPORT_HOUR_CT", 20)
    app._state.last_result = make_buy_signal([make_subsignal("vix", "none", "x", passes=False)])

    app.handle_tick(None, RuntimeError("boom"), now=_ct(20))

    report = next(s for s in sent if "End of day" in s)
    assert "latest tick failed" in report
    assert "boom" in report


def test_handle_tick_daily_report_carries_no_error_flag_after_an_idle_tick(
    monkeypatch, sent, make_subsignal, make_buy_signal
):
    monkeypatch.setattr(app.config, "DAILY_REPORT_HOUR_CT", 20)
    fresh = make_buy_signal([make_subsignal("vix", "none", "x", passes=False)])
    monkeypatch.setattr(app, "compute_signal", lambda: fresh)

    app.handle_tick(None, None, now=_ct(20))

    report = next(s for s in sent if "End of day" in s)
    assert "latest tick failed" not in report


# --- handle_tick: refresh-failure pushes ---------------------------------------------------


def test_handle_tick_pushes_a_refresh_failure_once_a_day(sent, reset_state):
    now = datetime.now(timezone.utc)
    reset_state.record("fed_rate", ok=False, error="SessionNotCreated", now=now.astimezone(app.market_hours.ET))

    app.handle_tick(None, None, now)
    app.handle_tick(None, None, now)

    assert len(sent) == 1
    assert "Refresh failed" in sent[0]
    assert "fed_rate" in sent[0]
    assert "SessionNotCreated" in sent[0]


def test_handle_tick_keeps_a_failure_pending_when_the_push_could_not_be_delivered(
    undeliverable, reset_state
):
    """Telegram being down is exactly when collectors tend to be failing too: an attempted
    push that never arrived must not count as the day's notification.
    """
    now = datetime.now(timezone.utc)
    reset_state.record("fed_rate", ok=False, error="boom", now=now.astimezone(app.market_hours.ET))

    app.handle_tick(None, None, now)

    assert len(undeliverable) == 1
    assert reset_state.unreported_failures(now.astimezone(app.market_hours.ET)) == [("fed_rate", "boom")]


def test_handle_tick_pushes_nothing_when_every_collector_succeeded(sent, reset_state):
    now = datetime.now(timezone.utc)
    reset_state.record("fed_rate", ok=True, error=None, now=now.astimezone(app.market_hours.ET))

    app.handle_tick(None, None, now)

    assert sent == []


def test_handle_tick_refresh_failures_do_not_corrupt_the_daily_report(
    monkeypatch, sent, reset_state, make_buy_signal
):
    """The failure loop must not rebind handle_tick's own `error`: a healthy tick that happens
    to carry a refresh failure would then report itself as a failed tick.
    """
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(app.config, "DAILY_REPORT_HOUR_CT", now.astimezone(app.market_hours.CT).hour)
    reset_state.record("fed_rate", ok=False, error="boom", now=now.astimezone(app.market_hours.ET))
    monkeypatch.setattr(app, "compute_signal", lambda: make_buy_signal([]))

    app.handle_tick(None, None, now)

    report = next(text for text in sent if "End of day" in text)
    assert "latest tick failed" not in report


# --- handle_message dispatch ---------------------------------------------------------


def test_handle_message_help_and_start_identical():
    assert app.handle_message("/help") == app.handle_message("/start")
    assert "Commands:" in app.handle_message("/help")


def test_handle_message_unrecognized():
    assert app.handle_message("gibberish") == "Unrecognized command, try /help."


def test_handle_message_empty_string():
    assert app.handle_message("") == "Unrecognized command, try /help."


def test_handle_message_strips_surrounding_whitespace():
    assert app.handle_message("   /help   ") == app.handle_message("/help")


def test_handle_message_status():
    assert "uptime" in app.handle_message("/status")


def test_handle_message_status_reads_the_journal(reset_state):
    reset_state.record("yield_curve", ok=False, error="HTTPError: 503", now=datetime.now(app.market_hours.ET))

    assert "HTTPError: 503" in app.handle_message("/status")


def test_handle_message_signal(monkeypatch, make_buy_signal):
    fake = make_buy_signal([])
    monkeypatch.setattr(app, "compute_signal", lambda allow_refresh: fake)
    monkeypatch.setattr(app, "market_open", lambda: True)
    assert "No buy signal" in _text(app.handle_message("/signal"))


def test_handle_message_vix_respects_market_open_flag(monkeypatch, make_subsignal):
    calls = []

    def fake_score(allow_refresh):
        calls.append(allow_refresh)
        return make_subsignal("vix", "none", "x")

    monkeypatch.setattr(app.vix_signal, "score", fake_score)
    monkeypatch.setattr(app, "market_open", lambda: False)
    app.handle_message("/vix")
    assert calls == [False]


def test_handle_message_dip_respects_market_open_flag(monkeypatch, make_subsignal):
    calls = []

    def fake_score(allow_refresh):
        calls.append(allow_refresh)
        return make_subsignal("market_dip", "flat", "x")

    monkeypatch.setattr(app.market_signal, "score", fake_score)
    monkeypatch.setattr(app, "market_open", lambda: True)
    app.handle_message("/dip")
    assert calls == [True]


def test_handle_message_fedrate(monkeypatch, make_subsignal):
    # _SIGNAL_COMMANDS binds these score functions at import time, so patching
    # app.rate_signal.score wouldn't reach the already-bound dict entry -- patch the entry itself.
    monkeypatch.setitem(app._SIGNAL_COMMANDS, "/fedrate", lambda: make_subsignal("fed_rate", "flat", "x", passes=True))
    assert "FedWatch" in _text(app.handle_message("/fedrate"))


def test_handle_message_margin(monkeypatch, make_subsignal):
    monkeypatch.setitem(
        app._SIGNAL_COMMANDS, "/margin", lambda: make_subsignal("margin_debt", "deleveraging", "x", passes=True)
    )
    assert "Margin" in _text(app.handle_message("/margin"))


def test_handle_message_sector(monkeypatch, make_subsignal):
    monkeypatch.setitem(app._SIGNAL_COMMANDS, "/sector", lambda: make_subsignal("sector", "growing", "x", passes=True))
    assert "Industry EPS" in _text(app.handle_message("/sector"))


def test_handle_message_curve(monkeypatch, make_subsignal):
    monkeypatch.setitem(
        app._SIGNAL_COMMANDS, "/curve", lambda: make_subsignal("yield_curve", "flat", "x", advisory=True)
    )
    assert "Yield curve" in _text(app.handle_message("/curve"))


def test_single_signal_handles_exception():
    def boom():
        raise ValueError("no data")

    reply = app._single_signal(boom)

    assert "failed to compute" in reply
    assert "ValueError" in reply


# --- /whattobuy ---------------------------------------------------------


@pytest.fixture
def whattobuy_env(monkeypatch, make_opportunity):
    """Offline /whattobuy: collectors fresh, analyzer canned, photo captured."""
    monkeypatch.setattr(app.runner, "refresh_macro", lambda **kwargs: [])
    monkeypatch.setattr(app.opportunity, "analyze", make_opportunity)
    photos = []
    monkeypatch.setattr(app.transport, "send_photo", lambda png, caption: photos.append(png))
    monkeypatch.setattr(app.chart, "render_chart", lambda opp: b"\x89PNG-fake")
    return photos


def test_handle_message_whattobuy_sends_chart_and_facts(whattobuy_env):
    reply = app.handle_message("/whattobuy")
    assert whattobuy_env == [b"\x89PNG-fake"]
    assert "What to buy" in reply
    assert "20.0" in reply
    assert "15.0" in reply
    assert "Small-cap band: candidate" in reply
    assert "FedWatch: 1/3 easing (ease → no change → no change, rate support: yes, need ≥1)" in reply
    assert "z-scores" in reply  # notes render as bullets


def test_handle_message_whattobuy_includes_buy_signal_context(whattobuy_env, make_subsignal, make_buy_signal):
    with app._lock:
        app._state.last_result = make_buy_signal([make_subsignal("vix", "none", "x")], state="none")
    reply = app.handle_message("/whattobuy")
    assert "Buy signal (context): No buy signal" in reply


def test_handle_message_whattobuy_chart_failure_degrades_to_text(whattobuy_env, monkeypatch):
    def boom(opp):
        raise RuntimeError("no display")

    monkeypatch.setattr(app.chart, "render_chart", boom)
    reply = app.handle_message("/whattobuy")
    assert whattobuy_env == []  # no photo sent
    assert "What to buy" in reply  # text still delivered
    assert "chart rendering failed" in reply


def test_handle_message_whattobuy_surfaces_refresh_errors(whattobuy_env, monkeypatch):
    monkeypatch.setattr(
        app.runner, "refresh_macro", lambda **kwargs: [("valuations", "ValueError: FY1 not found")]
    )
    reply = app.handle_message("/whattobuy")
    assert "Refresh failed" in reply
    assert "FY1 not found" in reply


def test_handle_message_whattobuy_refreshes_only_valuations(whattobuy_env, monkeypatch):
    """Everything else on the /whattobuy path reads a cache; none of it is worth a scrape here."""
    calls = []
    monkeypatch.setattr(app.runner, "refresh_macro", lambda **kwargs: calls.append(kwargs) or [])

    app.handle_message("/whattobuy")

    assert calls == [{"only": ("valuations",)}]


def test_whattobuy_says_so_when_valuations_is_failing_but_was_not_retried(whattobuy_env, reset_state):
    """Inside the retry backoff nothing is attempted and nothing fails, so without this the
    reply would render a stale snapshot with no warning at all.
    """
    reset_state.record("valuations", ok=False, error="ValueError: FY1 not found", now=datetime.now(app.market_hours.ET))

    reply = app.handle_message("/whattobuy")

    assert "valuations refresh is failing" in reply
    assert "FY1 not found" in reply


def test_whattobuy_does_not_double_report_a_failure_it_just_surfaced(whattobuy_env, monkeypatch, reset_state):
    monkeypatch.setattr(
        app.runner, "refresh_macro", lambda **kw: [("valuations", "ValueError: FY1 not found")]
    )
    reset_state.record("valuations", ok=False, error="ValueError: FY1 not found", now=datetime.now(app.market_hours.ET))

    reply = app.handle_message("/whattobuy")

    assert reply.count("FY1 not found") == 1


# --- /refresh ---------------------------------------------------------


def test_handle_message_refresh_forces_macro_and_returns_fresh_signal(monkeypatch, make_buy_signal):
    calls = []
    monkeypatch.setattr(app.runner, "refresh_macro", lambda force=False, **kw: calls.append(force) or [])
    fake = make_buy_signal([])
    monkeypatch.setattr(app, "compute_signal", lambda allow_refresh: fake)
    reply = _text(app.handle_message("/refresh"))
    assert calls == [True]
    assert "No buy signal" in reply


def test_handle_message_refresh_surfaces_collector_errors(monkeypatch, make_buy_signal):
    monkeypatch.setattr(
        app.runner, "refresh_macro", lambda force=False, **kw: [("fed_rate", "ConnectionError: boom")]
    )
    fake = make_buy_signal([])
    monkeypatch.setattr(app, "compute_signal", lambda allow_refresh: fake)
    reply = _text(app.handle_message("/refresh"))
    assert "Refresh failed" in reply
    assert "fed_rate" in reply
    assert "ConnectionError: boom" in reply


def test_handle_message_refresh_leaves_the_failure_for_the_daily_push(
    monkeypatch, sent, reset_state, make_buy_signal
):
    """/refresh lists the failures in its reply, but the poll loop's send can fail like any
    other, so the journal keeps them pending: a duplicate push beats a silent failure.
    """
    monkeypatch.setattr(app, "compute_signal", lambda allow_refresh: make_buy_signal([]))

    def failing_refresh(force=False, **kw):
        reset_state.record("fed_rate", ok=False, error="boom", now=datetime.now(app.market_hours.ET))
        return [("fed_rate", "boom")]

    monkeypatch.setattr(app.runner, "refresh_macro", failing_refresh)

    reply = _text(app.handle_message("/refresh"))
    app.handle_tick(None, None, datetime.now(timezone.utc))

    assert "Refresh failed" in reply
    assert [text for text in sent if "fed_rate" in text]


# --- market_open ---------------------------------------------------------


def test_market_open_true_when_not_closed(monkeypatch):
    monkeypatch.setattr(app.market_hours, "market_session", lambda: app.market_hours.REGULAR)
    assert app.market_open() is True


def test_market_open_false_when_closed(monkeypatch):
    monkeypatch.setattr(app.market_hours, "market_session", lambda: app.market_hours.CLOSED)
    assert app.market_open() is False


def test_start_hands_the_command_handler_to_the_transport(monkeypatch):
    started = []
    monkeypatch.setattr(app, "_start_personal", lambda: None)
    monkeypatch.setattr(app.transport, "start", lambda handler, callback: started.append((handler, callback)))

    app.start()

    assert started == [(app.handle_message, app.handle_callback)]


def test_daily_report_is_not_marked_complete_after_delivery_failure(monkeypatch, make_buy_signal):
    monkeypatch.setattr(app.config, "DAILY_REPORT_HOUR_CT", 20)
    monkeypatch.setattr(app, "compute_signal", lambda: make_buy_signal([]))
    attempted = []
    monkeypatch.setattr(app.transport, "send_report", lambda report: attempted.append(report) and False)
    app.handle_tick(None, None, now=_ct(20))
    assert app._state.last_report_date is None
    monkeypatch.setattr(app.transport, "send_report", lambda report: attempted.append(report) or True)
    app.handle_tick(None, None, now=_ct(21))
    assert app._state.last_report_date == _ct(21).date()
    assert len(attempted) == 2


def test_watch_activation_retries_when_report_delivery_failed(monkeypatch, make_buy_signal, make_subsignal):
    result = make_buy_signal([make_subsignal("market_dip", "dip", passes=True)])
    monkeypatch.setattr(app.transport, "send_report", lambda report: False)
    app.handle_tick(result, None)
    assert app._state.alerting is False
    monkeypatch.setattr(app.transport, "send_report", lambda report: True)
    app.handle_tick(result, None)
    assert app._state.alerting is True


def test_overview_never_renders_charts(monkeypatch, make_buy_signal, make_subsignal):
    signal = make_subsignal("market_dip", "dip", detail="Verbose facts", passes=True)
    def fail(_):
        raise AssertionError("Overview must not eagerly render a chart")
    monkeypatch.setattr(app.signal_charts, "render_signal_chart", fail)
    report = app._signal_report(make_buy_signal([signal]))
    assert len(report.messages) == 1
    assert report.messages[0].photo is None
    assert report.messages[0].keyboard
    assert "Verbose facts" not in report.messages[0].text
