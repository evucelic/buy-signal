"""Tests for telegram_bot/app.py: what the bot decides, remembers and replies.

The rendering it delegates to is covered in test_telegram_render.py; what's checked here is
the decision - whether an alert edge was crossed, whether the daily report is due, which
command ran, and what reached the transport.
"""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from buysignal.portfolio.store import PortfolioStore
from buysignal.schedule.journal import RefreshJournal
from buysignal.telegram_bot import app, pushes
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


def _fear(state, score):
    from buysignal.signals.base import SubSignal
    from buysignal.signals.buy_signal import assess
    return assess([SubSignal("vix", score, state, "", passes=state != "none")])


def test_high_fear_gets_its_own_push_once_a_day(monkeypatch, sent):
    monkeypatch.setattr(app.market_hours, "market_session", lambda: app.market_hours.REGULAR)
    morning = datetime(2026, 3, 9, 15, 0, tzinfo=timezone.utc)
    elevated, high, calm = _fear("soft", 25.), _fear("strong", 32.), _fear("none", 15.)

    app.handle_tick(elevated, None, morning)
    assert len(sent) == 1 and "Watch trigger active</b> (VIX elevated)" in sent[0]
    app.handle_tick(high, None, morning + timedelta(hours=1))
    assert len(sent) == 2 and sent[1].startswith("🔴 <b>High fear</b> (VIX at or above 30)")
    app.handle_tick(high, None, morning + timedelta(hours=2))
    app.handle_tick(elevated, None, morning + timedelta(hours=3))
    app.handle_tick(high, None, morning + timedelta(hours=4))
    assert len(sent) == 2  # sustained, or flapping around the line the same day: nothing more
    app.handle_tick(elevated, None, morning + timedelta(days=1))
    app.handle_tick(high, None, morning + timedelta(days=1, hours=1))
    assert len(sent) == 3 and "High fear" in sent[2]
    app.handle_tick(calm, None, morning + timedelta(days=1, hours=2))
    assert len(sent) == 4 and "cleared" in sent[3].lower()


def test_a_jump_straight_to_high_fear_sends_one_push_that_says_so(monkeypatch, sent):
    monkeypatch.setattr(app.market_hours, "market_session", lambda: app.market_hours.REGULAR)
    now = datetime(2026, 3, 9, 15, 0, tzinfo=timezone.utc)
    app.handle_tick(_fear("none", 15.), None, now)
    app.handle_tick(_fear("strong", 34.), None, now + timedelta(hours=1))
    app.handle_tick(_fear("strong", 36.), None, now + timedelta(hours=2))
    assert len(sent) == 1 and "Watch trigger active</b> (VIX high fear)" in sent[0]


def test_an_unknown_vix_neither_sets_nor_clears_high_fear(monkeypatch, sent):
    from buysignal.signals.buy_signal import assess
    monkeypatch.setattr(app.market_hours, "market_session", lambda: app.market_hours.REGULAR)
    now = datetime(2026, 3, 9, 15, 0, tzinfo=timezone.utc)
    app.handle_tick(_fear("strong", 34.), None, now)
    app.handle_tick(assess([]), None, now + timedelta(hours=1))
    assert app._state.seen.high_fear is True


def _count(passing, vix_passes=False):
    """A complete checklist: VIX passes only when asked, then `passing` of Fed, margin, EPS, dip."""
    from buysignal.signals.base import SubSignal
    from buysignal.signals.buy_signal import assess
    names = ("fed_rate", "margin_debt", "sector", "market_dip")
    vix = SubSignal("vix", 25. if vix_passes else 15., "soft" if vix_passes else "none", "", passes=vix_passes)
    return assess([vix, *(SubSignal(name, 0., "x", "", passes=i < passing) for i, name in enumerate(names))])


def test_a_change_in_the_count_sends_one_push(monkeypatch, sent):
    now = datetime(2026, 3, 9, 15, 0, tzinfo=timezone.utc)
    app.handle_tick(_count(1), None, now)
    assert sent == []  # the first complete read after a start is the baseline
    app.handle_tick(_count(2), None, now + timedelta(hours=1))
    assert len(sent) == 1 and sent[0].startswith("🔢 <b>Checklist 1 → 2 of 5</b>") and "Entry checklist · 2 of 5" in sent[0]
    app.handle_tick(_count(2), None, now + timedelta(hours=2))
    from buysignal.signals.buy_signal import assess
    app.handle_tick(assess([]), None, now + timedelta(hours=3))  # an incomplete read is not a change
    assert len(sent) == 1
    app.handle_tick(_count(1), None, now + timedelta(hours=4))
    assert len(sent) == 2 and "Checklist 2 → 1 of 5" in sent[1]


def test_a_count_change_that_comes_with_a_watch_trigger_is_one_message(monkeypatch, sent):
    now = datetime(2026, 3, 9, 15, 0, tzinfo=timezone.utc)
    app.handle_tick(_count(1), None, now)
    app.handle_tick(_count(1, vix_passes=True), None, now + timedelta(hours=1))
    assert len(sent) == 1
    assert "Watch trigger active" in sent[0] and "Checklist 1 → 2 of 5" in sent[0]


def test_an_undelivered_count_change_is_sent_on_the_next_tick(monkeypatch):
    now = datetime(2026, 3, 9, 15, 0, tzinfo=timezone.utc)
    attempts = []
    monkeypatch.setattr(app.transport, "send", lambda text, **kwargs: attempts.append(text) and False)
    app.handle_tick(_count(1), None, now)
    app.handle_tick(_count(2), None, now + timedelta(hours=1))
    delivered = []
    monkeypatch.setattr(app.transport, "send", lambda text, **kwargs: delivered.append(text) or True)
    app.handle_tick(_count(2), None, now + timedelta(hours=2))
    assert len(attempts) == 1 and len(delivered) == 1 and "Checklist 1 → 2 of 5" in delivered[0]


def test_the_end_of_day_report_carries_a_count_change_and_logs_the_session(monkeypatch, sent, checklist_log_path):
    from buysignal.analysis import checklist_log
    monkeypatch.setattr(app.config, "DAILY_REPORT_HOUR_CT", 20)
    monkeypatch.setattr(app, "_etf_closes", lambda now: {"VWCG": (60.5, "2026-07-24", "EUR")})
    app.handle_tick(_count(1), None, _ct(10))
    result = _count(3)
    monkeypatch.setattr(app, "compute_signal", lambda: result)
    app.handle_tick(result, None, now=_ct(20))
    app.handle_tick(result, None, now=_ct(21))
    assert len(sent) == 1 and sent[0].startswith("📅 <b>End of day</b>\n\n🔢 <b>Checklist 1 → 3 of 5</b>")
    rows = checklist_log.read(checklist_log_path)
    assert len(rows) == 1 and rows[0]["session"] == "2026-07-24" and rows[0]["count"] == 3
    assert rows[0]["etfs"] == {"VWCG": {"close": 60.5, "observed": "2026-07-24", "currency": "EUR"}}
    app.handle_tick(result, None, now=_ct(20, day=25))  # Saturday: the report goes out, no session is logged
    assert len(sent) == 2 and len(checklist_log.read(checklist_log_path)) == 1


def test_handle_tick_records_last_tick_and_error():
    app.handle_tick(None, RuntimeError("boom"))
    assert app._state.last_ok is False
    assert app._state.last_error == "boom"
    assert app._state.last_tick_at is not None


# --- daily report: when it is due, and what it shows ---------------------------------------


@pytest.mark.parametrize("now, last_report, due", [
    (_ct(20), None, True),                        # the report hour, nothing sent today
    (_ct(19), None, False),                       # an hour early
    (_ct(21), None, True),                        # late or retried after the hour
    (_ct(20), date(2026, 7, 24), False),          # already sent today
    (_ct(20, day=25), date(2026, 7, 24), True),   # the next day
])
def test_the_daily_report_is_due_once_per_day_from_the_report_hour(now, last_report, due):
    assert app.report_due(now, last_report_date=last_report, report_hour=20) is due


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
    assert all(command in app.handle_message("/help") for command in ("/signal", "/portfolio", "/whattobuy", "/target"))


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
    assert "Entry checklist · 0 of 5 incomplete" in _text(app.handle_message("/signal"))


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


@pytest.mark.parametrize("command, name, title", [
    ("/fedrate", "fed_rate", "Fed outlook"), ("/margin", "margin_debt", "Margin"),
    ("/sector", "sector", "Earnings outlook"), ("/curve", "yield_curve", "Yield curve"),
])
def test_each_reading_has_its_own_command(monkeypatch, make_subsignal, command, name, title):
    # The command table binds score functions at import, so the entry itself is replaced.
    monkeypatch.setitem(app._SIGNAL_COMMANDS, command, lambda: make_subsignal(name, "flat", "x", passes=True))

    assert title in _text(app.handle_message(command))


def test_single_signal_handles_exception():
    def boom():
        raise ValueError("no data")

    reply = app._single_signal(boom)

    assert "failed to compute" in reply
    assert "ValueError" in reply


# --- /portfolio, /whattobuy, /target ---------------------------------------------------------------


FLEX = (Path(__file__).parent / "fixtures" / "flex_statement.xml").read_bytes()


@pytest.fixture
def account(monkeypatch, tmp_path, make_opportunity):
    """An offline account read from the synthetic IBKR statement, with prices and valuations canned."""
    from buysignal.broker import flex
    from buysignal.portfolio import instruments
    store = PortfolioStore(tmp_path / "portfolio.sqlite3")
    store.save_statement(flex.parse(FLEX), datetime(2026, 11, 18, 7, tzinfo=timezone.utc))
    monkeypatch.setattr(app, "_portfolio_store", store)
    monkeypatch.setenv("IBKR_FLEX_TOKEN", "t" * 24)
    monkeypatch.setenv("IBKR_FLEX_QUERY_ID", "42")
    monkeypatch.setattr(app.broker_sync, "demands_refresh", lambda store: False)
    prices = [{"symbol": "VUAA", "price": 121.35}, {"symbol": "VWCG", "price": 44.10}, {"symbol": "AVWS", "price": 27.02}]
    monkeypatch.setattr(app.personal_quotes, "all_quotes", lambda store: prices)
    monkeypatch.setattr(app.personal_quotes, "eurusd", lambda store: (1.10, "2026-11-17", False))
    monkeypatch.setattr(app.personal_quotes, "eurusd_history", lambda store: (None, False))
    monkeypatch.setattr(app.personal_quotes, "benchmark", lambda store: (None, False))
    monkeypatch.setattr(app.personal_quotes, "daily_closes", lambda store: ({}, []))
    monkeypatch.setattr(app.personal_quotes, "dips", lambda store: {})
    monkeypatch.setattr(app.personal_quotes, "real_yield", lambda store: (None, None, False))
    monkeypatch.setattr(app.opportunity, "analyze", make_opportunity)
    monkeypatch.setattr(app.chart, "render_chart", lambda opp, dips=None: b"\x89PNG-fake")
    yield store
    instruments.set_current(None)


def test_portfolio_opens_on_the_overview_with_a_button_for_every_tab(account):
    reply = app.handle_message("/portfolio")

    message = reply.messages[0]
    assert message.panel and message.text.startswith("💼 <b>Portfolio</b>")
    labels = {button["text"].lstrip("● ") for row in message.keyboard["inline_keyboard"] for button in row}
    assert {"Overview", "Returns", "Mix", "Risk", "Entries", "Fees", "Cash", "P/E", "1Y"} <= labels


def test_portfolio_without_ibkr_explains_how_to_connect(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "_portfolio_store", PortfolioStore(tmp_path / "empty.sqlite3"))
    monkeypatch.delenv("IBKR_FLEX_TOKEN", raising=False)

    assert "IBKR not connected" in app.handle_message("/portfolio")


def test_portfolio_reads_ibkr_first_when_the_statement_is_behind(account, monkeypatch):
    calls = []
    monkeypatch.setattr(app.broker_sync, "demands_refresh", lambda store: True)
    monkeypatch.setattr(app.runner, "refresh_macro", lambda **kwargs: calls.append(kwargs) or [])

    app.handle_message("/portfolio")

    assert {"force": True, "only": ("ibkr",)} in calls


def test_a_failed_ibkr_read_still_shows_the_stored_statement_with_a_warning(account, monkeypatch):
    monkeypatch.setattr(app.broker_sync, "demands_refresh", lambda store: True)
    monkeypatch.setattr(app.runner, "refresh_macro",
                        lambda **kwargs: [("ibkr", "IBKR Flex 1012: the token has expired")] if kwargs.get("only") == ("ibkr",) else [])

    text = app.handle_message("/portfolio").messages[0].text

    assert "IBKR read failed" in text and "through 2026-11-17" in text


def test_whattobuy_spends_the_cash_at_ibkr(account):
    app.handle_message("/target VUAA 55 VWCG 20 AVWS 20 CASH 5")

    reply = app.handle_message("/whattobuy")

    assert reply.startswith("🛒 <b>What to buy</b> · IBKR cash 2026-11-17")
    assert "Cash target keeps" in reply and "VUAA" in reply.split("<pre>")[1]


def test_whattobuy_with_an_amount_plans_that_many_euros_without_the_cash_target(account):
    app.handle_message("/target VUAA 55 VWCG 20 AVWS 20 CASH 5")

    reply = app.handle_message("/whattobuy 500")

    assert "for €500" in reply and "Cash target keeps" not in reply


def test_whattobuy_asks_for_a_target_first(account):
    assert "Set a target mix first" in app.handle_message("/whattobuy")


@pytest.mark.parametrize("retired", ["/buy 10 VUAA @ 100", "bought 10 VUAA @ 100", "/cash 500", "/entries", "/trades", "/holdings"])
def test_hand_entered_purchase_commands_are_gone(retired):
    assert app.handle_message(retired) == "Unrecognized command, try /help."


# --- /refresh ---------------------------------------------------------


def test_handle_message_refresh_forces_macro_and_returns_fresh_signal(monkeypatch, make_buy_signal):
    calls = []
    monkeypatch.setattr(app.runner, "refresh_macro", lambda force=False, **kw: calls.append(force) or [])
    fake = make_buy_signal([])
    monkeypatch.setattr(app, "compute_signal", lambda allow_refresh: fake)
    reply = _text(app.handle_message("/refresh"))
    assert calls == [True]
    assert "Entry checklist · 0 of 5 incomplete" in reply


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
    assert app._state.seen.alerting is False
    monkeypatch.setattr(app.transport, "send_report", lambda report: True)
    app.handle_tick(result, None)
    assert app._state.seen.alerting is True


def test_overview_never_renders_charts(monkeypatch, make_buy_signal, make_subsignal):
    signal = make_subsignal("market_dip", "dip", detail="Verbose facts", passes=True)
    def fail(_):
        raise AssertionError("Overview must not eagerly render a chart")
    monkeypatch.setattr("buysignal.analysis.signal_charts.render_signal_chart", fail)
    report = app._signal_report(make_buy_signal([signal]))
    assert len(report.messages) == 1
    assert report.messages[0].photo is None
    assert report.messages[0].keyboard
    assert "Verbose facts" not in report.messages[0].text


def test_a_late_end_of_day_report_logs_under_its_own_session(monkeypatch, sent, checklist_log_path, make_subsignal, make_buy_signal):
    # 23:00 CT on a Friday is already Saturday in New York; the row still belongs to Friday.
    from buysignal.analysis import checklist_log
    monkeypatch.setattr(app.config, "DAILY_REPORT_HOUR_CT", 20)
    result = make_buy_signal([make_subsignal("vix", "none", "x")])
    monkeypatch.setattr(app, "compute_signal", lambda: result)
    app.handle_tick(result, None, now=_ct(23))
    assert [row["session"] for row in checklist_log.read(checklist_log_path)] == ["2026-07-24"]
