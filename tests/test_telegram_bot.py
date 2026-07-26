"""Tests for telegram_bot.py (formatting, notification edge-triggering, command dispatch)
and bot.py's one testable function.
"""

from datetime import datetime, timezone

import pytest

import bot
import telegram_bot as tb


@pytest.fixture(autouse=True)
def reset_state():
    """telegram_bot's module-level _state is mutable global state — reset it every test."""
    tb._state = tb._State(start_time=datetime.now(timezone.utc))
    yield


# --- _is_alerting / _trigger_reasons ---------------------------------------------------------


def test_is_alerting_vix_only(make_subsignal, make_buy_signal):
    result = make_buy_signal([make_subsignal("vix", "soft", "x", passes=True)])
    assert tb._is_alerting(result) is True


def test_is_alerting_dip_only(make_subsignal, make_buy_signal):
    result = make_buy_signal([make_subsignal("market_dip", "dip", "x", passes=True)])
    assert tb._is_alerting(result) is True


def test_is_alerting_checklist_soft(make_subsignal, make_buy_signal):
    result = make_buy_signal([make_subsignal("sector", "growing", "x", passes=True)], state="soft")
    assert tb._is_alerting(result) is True


def test_is_alerting_false_when_nothing_triggers(make_subsignal, make_buy_signal):
    result = make_buy_signal([make_subsignal("vix", "none", "x", passes=False)])
    assert tb._is_alerting(result) is False


def test_trigger_reasons_combines_all(make_subsignal, make_buy_signal):
    result = make_buy_signal(
        [make_subsignal("vix", "soft", "x", passes=True), make_subsignal("market_dip", "dip", "y", passes=True)],
        state="strong",
    )
    reasons = tb._trigger_reasons(result)
    assert "VIX elevated" in reasons
    assert "SPY dip" in reasons
    assert "checklist strong" in reasons


def test_trigger_reasons_empty_when_nothing_triggers(make_subsignal, make_buy_signal):
    result = make_buy_signal([make_subsignal("vix", "none", "x", passes=False)])
    assert tb._trigger_reasons(result) == ""


# --- formatting ---------------------------------------------------------------


def test_bullets_splits_and_escapes():
    assert tb._bullets("a & b | c < d") == "• a &amp; b\n• c &lt; d"


def test_format_subsignal_uses_table_when_present(make_subsignal):
    s = make_subsignal("market_dip", "flat", "raw detail", table="col1 col2\nval1 val2")
    assert "<pre>col1 col2\nval1 val2</pre>" in tb._format_subsignal(s)


def test_format_subsignal_uses_bullets_when_no_table(make_subsignal):
    s = make_subsignal("vix", "none", "calm")
    assert "•" in tb._format_subsignal(s)


def test_format_signal_escapes_real_ampersand_case(make_subsignal, make_buy_signal):
    # the real production case: sector detail strings can contain a literal '&'
    s = make_subsignal("sector", "growing", "Aerospace & Defense: +1%", passes=True)
    result = make_buy_signal([s], state="strong")
    assert "Aerospace &amp; Defense" in tb._format_signal(result)


def test_format_signal_includes_missing_line(make_buy_signal):
    result = make_buy_signal([], missing_signals=["sector", "vix"])
    assert "missing: sector, vix" in tb._format_signal(result)


def test_format_status_never_ticked():
    assert "never" in tb._format_status()


def test_format_status_ok_after_tick(make_buy_signal):
    tb._state.last_tick_at = datetime.now(timezone.utc)
    tb._state.last_ok = True
    tb._state.last_result = make_buy_signal([])
    assert "(ok)" in tb._format_status()


def test_format_status_failed_after_tick():
    tb._state.last_tick_at = datetime.now(timezone.utc)
    tb._state.last_ok = False
    tb._state.last_error = "boom"
    assert "FAILED (boom)" in tb._format_status()


# --- handle_tick: alert edge-triggering ---------------------------------------------------------


def test_handle_tick_alert_edge_triggering(monkeypatch, make_subsignal, make_buy_signal):
    sent = []
    monkeypatch.setattr(tb, "_send", lambda text: sent.append(text))
    monkeypatch.setattr(tb.runner, "market_session", lambda: tb.runner.REGULAR)

    not_alerting = make_buy_signal([make_subsignal("vix", "none", "x", passes=False)])
    alerting = make_buy_signal([make_subsignal("vix", "soft", "x", passes=True)])

    tb.handle_tick(not_alerting, None)
    assert len(sent) == 0

    tb.handle_tick(alerting, None)
    assert len(sent) == 1 and "Signal active" in sent[0]

    tb.handle_tick(alerting, None)
    assert len(sent) == 1  # no repeat while sustained

    tb.handle_tick(not_alerting, None)
    assert len(sent) == 2 and "cleared" in sent[1].lower()


# --- handle_tick: daily-report edge-triggering ---------------------------------------------------------


def test_handle_tick_daily_report_fires_once_at_close(monkeypatch, make_subsignal, make_buy_signal):
    sent = []
    monkeypatch.setattr(tb, "_send", lambda text: sent.append(text))
    sessions = iter([tb.runner.REGULAR, tb.runner.REGULAR, tb.runner.AFTER_HOURS, tb.runner.AFTER_HOURS])
    monkeypatch.setattr(tb.runner, "market_session", lambda: next(sessions))

    result = make_buy_signal([make_subsignal("vix", "none", "x", passes=False)])
    for _ in range(4):
        tb.handle_tick(result, None)

    daily_reports = [s for s in sent if "End of day" in s]
    assert len(daily_reports) == 1


def test_handle_tick_daily_report_no_false_fire_on_fresh_restart(monkeypatch, make_subsignal, make_buy_signal):
    sent = []
    monkeypatch.setattr(tb, "_send", lambda text: sent.append(text))
    monkeypatch.setattr(tb.runner, "market_session", lambda: tb.runner.AFTER_HOURS)

    result = make_buy_signal([make_subsignal("vix", "none", "x", passes=False)])
    tb.handle_tick(result, None)

    assert not any("End of day" in s for s in sent)


def test_handle_tick_records_last_tick_and_error(make_buy_signal):
    tb.handle_tick(None, RuntimeError("boom"))
    assert tb._state.last_ok is False
    assert tb._state.last_error == "boom"
    assert tb._state.last_tick_at is not None


# --- _handle_message dispatch ---------------------------------------------------------


def test_handle_message_help_and_start_identical():
    assert tb._handle_message("/help") == tb._handle_message("/start")
    assert "Commands:" in tb._handle_message("/help")


def test_handle_message_unrecognized():
    assert tb._handle_message("gibberish") == "Unrecognized command, try /help."


def test_handle_message_empty_string():
    assert tb._handle_message("") == "Unrecognized command, try /help."


def test_handle_message_strips_surrounding_whitespace():
    assert tb._handle_message("   /help   ") == tb._handle_message("/help")


def test_handle_message_status():
    assert "uptime" in tb._handle_message("/status")


def test_handle_message_signal(monkeypatch, make_buy_signal):
    fake = make_buy_signal([])
    monkeypatch.setattr(tb, "compute_signal", lambda allow_refresh: fake)
    monkeypatch.setattr(tb, "_market_open", lambda: True)
    assert "No buy signal" in tb._handle_message("/signal")


def test_handle_message_vix_respects_market_open_flag(monkeypatch, make_subsignal):
    calls = []

    def fake_score(allow_refresh):
        calls.append(allow_refresh)
        return make_subsignal("vix", "none", "x")

    monkeypatch.setattr(tb.vix_signal, "score", fake_score)
    monkeypatch.setattr(tb, "_market_open", lambda: False)
    tb._handle_message("/vix")
    assert calls == [False]


def test_handle_message_dip_respects_market_open_flag(monkeypatch, make_subsignal):
    calls = []

    def fake_score(allow_refresh):
        calls.append(allow_refresh)
        return make_subsignal("market_dip", "flat", "x")

    monkeypatch.setattr(tb.market_signal, "score", fake_score)
    monkeypatch.setattr(tb, "_market_open", lambda: True)
    tb._handle_message("/dip")
    assert calls == [True]


def test_handle_message_fedrate(monkeypatch, make_subsignal):
    monkeypatch.setattr(tb.rate_signal, "score", lambda: make_subsignal("fed_rate", "flat", "x", passes=True))
    assert "Fed Rate" in tb._handle_message("/fedrate")


def test_handle_message_margin(monkeypatch, make_subsignal):
    monkeypatch.setattr(
        tb.margin_signal, "score", lambda: make_subsignal("margin_debt", "deleveraging", "x", passes=True)
    )
    assert "Margin Debt" in tb._handle_message("/margin")


def test_handle_message_sector(monkeypatch, make_subsignal):
    monkeypatch.setattr(tb.sector_signal, "score", lambda: make_subsignal("sector", "growing", "x", passes=True))
    assert "Leading Industries" in tb._handle_message("/sector")


def test_format_single_handles_exception():
    def boom():
        raise RuntimeError("bad")

    assert "failed to compute" in tb._format_single(boom)


# --- _market_open ---------------------------------------------------------


def test_market_open_true_when_not_closed(monkeypatch):
    monkeypatch.setattr(tb.runner, "market_session", lambda: tb.runner.REGULAR)
    assert tb._market_open() is True


def test_market_open_false_when_closed(monkeypatch):
    monkeypatch.setattr(tb.runner, "market_session", lambda: tb.runner.CLOSED)
    assert tb._market_open() is False


# --- notifications / send ---------------------------------------------------------


def test_notify_started(monkeypatch):
    sent = []
    monkeypatch.setattr(tb, "_send", lambda t: sent.append(t))
    tb.notify_started()
    assert sent == ["🟢 Runner started"]


def test_notify_stopped(monkeypatch):
    sent = []
    monkeypatch.setattr(tb, "_send", lambda t: sent.append(t))
    tb.notify_stopped()
    assert sent == ["🔴 Runner stopped"]


def test_send_swallows_request_exception(monkeypatch, capsys):
    def boom(*a, **k):
        raise tb.requests.exceptions.RequestException("fail")

    monkeypatch.setattr(tb.requests, "post", boom)
    tb._send("hi")  # must not raise
    assert "Telegram send failed" in capsys.readouterr().out


# --- bot.py ---------------------------------------------------------


def test_handle_shutdown_signal_raises_keyboard_interrupt():
    with pytest.raises(KeyboardInterrupt):
        bot._handle_shutdown_signal(None, None)
