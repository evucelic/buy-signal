"""Tests for telegram_bot.py (formatting, notification edge-triggering, command dispatch)
and bot.py's one testable function.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

import bot
import telegram_bot as tb


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    """Reset telegram_bot's module-level _state, and disable the daily report by default."""
    tb._state = tb._State(start_time=datetime.now(timezone.utc))
    monkeypatch.setattr(tb.config, "DAILY_REPORT_HOUR_CT", -1)
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


def test_format_subsignal_renders_footer_under_the_table(make_subsignal):
    s = make_subsignal("yield_curve", "flat", "d", table="col1\nval1", footer="🔻 Cuts priced in")
    assert tb._format_subsignal(s).endswith("</pre>\n🔻 Cuts priced in")


def test_format_subsignal_escapes_the_footer(make_subsignal):
    s = make_subsignal("yield_curve", "flat", "d", table="col1", footer="2y < FFR & falling")
    assert "2y &lt; FFR &amp; falling" in tb._format_subsignal(s)


def test_format_subsignal_omits_footer_when_absent(make_subsignal):
    s = make_subsignal("yield_curve", "flat", "d", table="col1\nval1")
    assert tb._format_subsignal(s).endswith("</pre>")


def test_format_signal_escapes_real_ampersand_case(make_subsignal, make_buy_signal):
    # the real production case: sector detail strings can contain a literal '&'
    s = make_subsignal("sector", "growing", "Aerospace & Defense: +1%", passes=True)
    result = make_buy_signal([s], state="strong")
    assert "Aerospace &amp; Defense" in tb._format_signal(result)


def test_format_signal_includes_missing_line(make_buy_signal):
    result = make_buy_signal([], missing_signals=["sector", "vix"])
    assert "missing: sector, vix" in tb._format_signal(result)


def test_format_signal_includes_freshness_table(make_buy_signal):
    result = make_buy_signal([])
    assert "Data as of" in tb._format_signal(result)


def test_format_subsignal_uses_no_change_label(make_subsignal):
    s = make_subsignal("fed_rate", "no_change", "x", passes=True)
    assert "No change" in tb._format_subsignal(s)


def test_format_subsignal_advisory_uses_info_mark_not_pass_fail(make_subsignal):
    s = make_subsignal("yield_curve", "inverted", "x", passes=False, advisory=True)
    formatted = tb._format_subsignal(s)
    assert formatted.startswith("ℹ️")
    assert "✅" not in formatted
    assert "❌" not in formatted
    assert "Inverted" in formatted


def test_format_signal_header_count_excludes_advisory(make_subsignal, make_buy_signal):
    subs = [
        make_subsignal("vix", "strong", "x", passes=True),
        make_subsignal("yield_curve", "inverted", "x", passes=False, advisory=True),
    ]
    result = make_buy_signal(subs, state="strong")
    assert "(1/1 conditions met)" in tb._format_signal(result)


def test_humanize_age_no_cache():
    assert tb._humanize_age(None) == "no cache"


def test_humanize_age_just_now():
    assert tb._humanize_age(datetime.now(timezone.utc)) == "just now"


def test_humanize_age_minutes():
    assert tb._humanize_age(datetime.now(timezone.utc) - timedelta(minutes=5)) == "5m ago"


def test_humanize_age_hours():
    assert tb._humanize_age(datetime.now(timezone.utc) - timedelta(hours=3)) == "3h ago"


def test_humanize_age_days():
    assert tb._humanize_age(datetime.now(timezone.utc) - timedelta(days=2)) == "2d ago"


def test_format_freshness_lists_all_signals(monkeypatch):
    monkeypatch.setattr(tb.freshness, "last_modified", lambda path: None)
    table = tb._format_freshness()
    for name in tb._FRESHNESS_FILES:
        assert name in table


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
    assert len(sent) == 1
    assert "Signal active" in sent[0]

    tb.handle_tick(alerting, None)
    assert len(sent) == 1  # no repeat while sustained

    tb.handle_tick(not_alerting, None)
    assert len(sent) == 2
    assert "cleared" in sent[1].lower()


# --- daily report: when it is due, and what it shows ---------------------------------------


def _ct(hour, day=24):
    return datetime(2026, 7, day, hour, 0, tzinfo=tb.runner.CT)


def test_report_is_due_at_the_report_hour_when_none_was_sent_today():
    assert tb._report_due(_ct(20), last_report_date=None, report_hour=20) is True


def test_report_is_not_due_an_hour_before_the_report_hour():
    assert tb._report_due(_ct(19), last_report_date=None, report_hour=20) is False


def test_report_is_not_due_an_hour_after_the_report_hour():
    assert tb._report_due(_ct(21), last_report_date=None, report_hour=20) is False


def test_report_is_not_due_again_on_a_date_already_reported():
    assert tb._report_due(_ct(20), last_report_date=date(2026, 7, 24), report_hour=20) is False


def test_report_is_due_again_the_next_day():
    assert tb._report_due(_ct(20, day=25), last_report_date=date(2026, 7, 24), report_hour=20) is True


def test_report_shows_this_ticks_result_when_the_tick_produced_one(make_buy_signal):
    ticked = make_buy_signal([])
    cached = make_buy_signal([])

    chosen = tb._report_result(ticked, None, cached, compute=_never_computes)

    assert chosen is ticked


def test_report_computes_fresh_when_an_idle_tick_produced_nothing(make_buy_signal):
    fresh = make_buy_signal([])
    cached = make_buy_signal([])

    chosen = tb._report_result(None, None, cached, compute=lambda: fresh)

    assert chosen is fresh


def test_report_falls_back_to_the_cache_when_the_tick_failed(make_buy_signal):
    cached = make_buy_signal([])

    chosen = tb._report_result(None, RuntimeError("boom"), cached, compute=_never_computes)

    assert chosen is cached


def test_report_has_nothing_to_show_when_the_tick_failed_with_no_cache():
    assert tb._report_result(None, RuntimeError("boom"), None, compute=_never_computes) is None


def _never_computes():
    raise AssertionError("compute_signal() must not run for this case")


# --- handle_tick: daily-report wiring ---------------------------------------------------------


def test_handle_tick_sends_the_daily_report_once_per_day(monkeypatch, make_subsignal, make_buy_signal):
    sent = []
    monkeypatch.setattr(tb, "_send", lambda text: sent.append(text))
    monkeypatch.setattr(tb.config, "DAILY_REPORT_HOUR_CT", 20)
    result = make_buy_signal([make_subsignal("vix", "none", "x", passes=False)])

    tb.handle_tick(result, None, now=_ct(20))
    tb.handle_tick(result, None, now=_ct(20))

    assert len([s for s in sent if "End of day" in s]) == 1


def test_handle_tick_sends_no_daily_report_outside_the_report_hour(monkeypatch, make_subsignal, make_buy_signal):
    sent = []
    monkeypatch.setattr(tb, "_send", lambda text: sent.append(text))
    monkeypatch.setattr(tb.config, "DAILY_REPORT_HOUR_CT", 20)
    result = make_buy_signal([make_subsignal("vix", "none", "x", passes=False)])

    tb.handle_tick(result, None, now=_ct(19))

    assert not any("End of day" in s for s in sent)


def test_handle_tick_sends_no_daily_report_when_the_tick_failed_with_no_cache(monkeypatch):
    sent = []
    monkeypatch.setattr(tb, "_send", lambda text: sent.append(text))
    monkeypatch.setattr(tb.config, "DAILY_REPORT_HOUR_CT", 20)

    tb.handle_tick(None, RuntimeError("boom"), now=_ct(20))

    assert not any("End of day" in s for s in sent)


def test_handle_tick_flags_the_error_when_the_daily_report_falls_back_to_the_cache(
    monkeypatch, make_subsignal, make_buy_signal
):
    sent = []
    monkeypatch.setattr(tb, "_send", lambda text: sent.append(text))
    monkeypatch.setattr(tb.config, "DAILY_REPORT_HOUR_CT", 20)
    tb._state.last_result = make_buy_signal([make_subsignal("vix", "none", "x", passes=False)])

    tb.handle_tick(None, RuntimeError("boom"), now=_ct(20))

    report = next(s for s in sent if "End of day" in s)
    assert "latest tick failed" in report
    assert "boom" in report


def test_handle_tick_daily_report_carries_no_error_flag_after_an_idle_tick(
    monkeypatch, make_subsignal, make_buy_signal
):
    sent = []
    monkeypatch.setattr(tb, "_send", lambda text: sent.append(text))
    monkeypatch.setattr(tb.config, "DAILY_REPORT_HOUR_CT", 20)
    fresh = make_buy_signal([make_subsignal("vix", "none", "x", passes=False)])
    monkeypatch.setattr(tb, "compute_signal", lambda: fresh)

    tb.handle_tick(None, None, now=_ct(20))

    report = next(s for s in sent if "End of day" in s)
    assert "latest tick failed" not in report


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
    # _SIGNAL_COMMANDS binds these score functions at import time, so patching
    # tb.rate_signal.score wouldn't reach the already-bound dict entry -- patch the entry itself.
    monkeypatch.setitem(tb._SIGNAL_COMMANDS, "/fedrate", lambda: make_subsignal("fed_rate", "flat", "x", passes=True))
    assert "Fed Rate" in tb._handle_message("/fedrate")


def test_handle_message_margin(monkeypatch, make_subsignal):
    monkeypatch.setitem(
        tb._SIGNAL_COMMANDS, "/margin", lambda: make_subsignal("margin_debt", "deleveraging", "x", passes=True)
    )
    assert "Margin Debt" in tb._handle_message("/margin")


def test_handle_message_sector(monkeypatch, make_subsignal):
    monkeypatch.setitem(tb._SIGNAL_COMMANDS, "/sector", lambda: make_subsignal("sector", "growing", "x", passes=True))
    assert "Leading Industries" in tb._handle_message("/sector")


def test_handle_message_curve(monkeypatch, make_subsignal):
    monkeypatch.setitem(
        tb._SIGNAL_COMMANDS, "/curve", lambda: make_subsignal("yield_curve", "flat", "x", advisory=True)
    )
    assert "Yield Curve" in tb._handle_message("/curve")


def _fake_opportunity():
    import analyzer as az

    segments = [
        az.SegmentView("sp500", "MSCI USA", fwd_pe=20.0, asof="July 31, 2026", trailing_pe=25.0,
                       ratio_vs_spx=1.0, discount_vs_spx=0.0),
        az.SegmentView("world_small", "World Small Cap", fwd_pe=15.0, trailing_pe=19.0,
                       ratio_vs_spx=0.75, discount_vs_spx=0.25),
        az.SegmentView("europe", "Europe", fwd_pe=16.0, trailing_pe=18.0,
                       ratio_vs_spx=0.80, discount_vs_spx=0.20),
    ]
    rate = az.RateView(
        horizons=[
            az.HorizonView("nearest", "2026-09-16", 0.78, 0.18, 0.04, "ease"),
            az.HorizonView("6m", "2027-01-27", 0.30, 0.60, 0.10, "no_change"),
            az.HorizonView("1y", "2027-07-28", 0.25, 0.65, 0.10, "no_change"),
        ],
        consecutive_easing=1,
        rate_support=True,
    )
    return az.Opportunity(segments=segments, rate=rate, small_cap_band="candidate",
                          verdict="small caps: valuation + rate conditions both met (candidate)",
                          notes=["z-scores need 12 monthly MSCI observations, have 1"], history_obs=1)


@pytest.fixture
def whattobuy_env(monkeypatch):
    """Offline /whattobuy: collectors fresh, analyzer canned, photo captured."""
    from collectors import valuations

    monkeypatch.setattr(valuations, "should_refresh", lambda: False)
    monkeypatch.setattr(tb.analyzer, "analyze", lambda: _fake_opportunity())
    photos = []
    monkeypatch.setattr(tb, "_send_photo", lambda png, caption: photos.append(png))
    monkeypatch.setattr(tb.analyzer, "render_chart", lambda opp: b"\x89PNG-fake")
    return photos


def test_handle_message_whattobuy_sends_chart_and_facts(whattobuy_env):
    reply = tb._handle_message("/whattobuy")
    assert whattobuy_env == [b"\x89PNG-fake"]
    assert "What to buy" in reply
    assert "20.0" in reply
    assert "15.0" in reply
    assert "Small-cap band: candidate" in reply
    assert "FedWatch: 1/3 easing (ease → no change → no change, rate support: yes, need ≥1)" in reply
    assert "z-scores" in reply  # notes render as bullets


def test_handle_message_whattobuy_includes_buy_signal_context(whattobuy_env, make_subsignal, make_buy_signal):
    with tb._lock:
        tb._state.last_result = make_buy_signal([make_subsignal("vix", "none", "x")], state="none")
    reply = tb._handle_message("/whattobuy")
    assert "Buy signal (context): No buy signal" in reply


def test_handle_message_whattobuy_chart_failure_degrades_to_text(whattobuy_env, monkeypatch):
    def boom(opp):
        raise RuntimeError("no display")

    monkeypatch.setattr(tb.analyzer, "render_chart", boom)
    reply = tb._handle_message("/whattobuy")
    assert whattobuy_env == []  # no photo sent
    assert "What to buy" in reply  # text still delivered
    assert "chart rendering failed" in reply


def test_handle_message_whattobuy_surfaces_refresh_errors(whattobuy_env, monkeypatch):
    from collectors import valuations

    monkeypatch.setattr(valuations, "should_refresh", lambda: True)
    monkeypatch.setattr(valuations, "update_valuations_data", lambda: "ValueError: FY1 not found")
    reply = tb._handle_message("/whattobuy")
    assert "Refresh failed" in reply
    assert "FY1 not found" in reply


def test_handle_message_refresh_forces_macro_and_returns_fresh_signal(monkeypatch, make_buy_signal):
    calls = []
    monkeypatch.setattr(tb.runner, "refresh_macro", lambda force=False: calls.append(force) or [])
    fake = make_buy_signal([])
    monkeypatch.setattr(tb, "compute_signal", lambda allow_refresh: fake)
    reply = tb._handle_message("/refresh")
    assert calls == [True]
    assert "No buy signal" in reply


def test_handle_message_refresh_surfaces_collector_errors(monkeypatch, make_buy_signal):
    monkeypatch.setattr(
        tb.runner, "refresh_macro", lambda force=False: [("fed_rate", "ConnectionError: boom")]
    )
    fake = make_buy_signal([])
    monkeypatch.setattr(tb, "compute_signal", lambda allow_refresh: fake)
    reply = tb._handle_message("/refresh")
    assert "Refresh failed" in reply
    assert "fed_rate" in reply
    assert "ConnectionError: boom" in reply


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


# --- the rendered message, in full ---------------------------------------------------------


_GOLDEN_TABLE = (
    "Thru         10y-3m   2y-FFR \n"
    "-----------  -------  -------\n"
    "2026-08-14   +0.79pp  +0.59pp\n"
    "2026-08-27*  +0.82pp  +0.57pp"
)

_GOLDEN_MESSAGE = (
    "🟢 <b>STRONG BUY SIGNAL</b> (1/1 conditions met)\n"
    "\n"
    "✅ 🌡️ <b>VIX</b>: 🔴 High fear\n"
    "• VIX 31.2 &gt;= 30.0, high fear\n"
    "• +4.1% vs prior close\n"
    "\n"
    "ℹ️ 〽️ <b>Yield Curve</b>: ➡️ Flat/normal\n"
    "<pre>Thru         10y-3m   2y-FFR \n"
    "-----------  -------  -------\n"
    "2026-08-14   +0.79pp  +0.59pp\n"
    "2026-08-27*  +0.82pp  +0.57pp</pre>\n"
    "🔻 Cuts priced in since 2026-08-14. First cut historically 4-20mo out.\n"
    "\n"
    "⚠️ missing: sector\n"
    "\n"
    "🕓 <b>Data as of</b>\n"
    "<pre>signal       updated \n"
    "-----------  --------\n"
    "vix          no cache\n"
    "yield_curve  no cache</pre>"
)


def test_format_signal_renders_the_whole_message(monkeypatch, make_subsignal, make_buy_signal):
    monkeypatch.setattr(
        tb,
        "_FRESHNESS_FILES",
        {"vix": "/nonexistent/vix.csv", "yield_curve": "/nonexistent/yieldcurve.csv"},
    )
    vix = make_subsignal(
        "vix",
        state="strong",
        detail="VIX 31.2 >= 30.0, high fear | +4.1% vs prior close",
        passes=True,
        score=31.2,
    )
    curve = make_subsignal(
        "yield_curve",
        state="flat",
        detail="10y-3m spread +0.82pp (2026-08-27) | last full 3wk avg +0.79pp",
        passes=True,
        table=_GOLDEN_TABLE,
        advisory=True,
        footer="🔻 Cuts priced in since 2026-08-14. First cut historically 4-20mo out.",
        score=0.82,
    )
    result = make_buy_signal([vix, curve], state="strong", missing_signals=["sector"])

    assert tb._format_signal(result) == _GOLDEN_MESSAGE
