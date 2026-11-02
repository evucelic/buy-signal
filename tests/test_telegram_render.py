"""Tests for telegram_bot/render.py: values in, Telegram HTML out.

Every input is a literal - no clock, no cache files, no journal, no credentials - which is the
whole point of the module: rendering is the part of the bot you can check by reading it.
"""

from datetime import datetime, timedelta, timezone

from buysignal.schedule.journal import CollectorStatus
from buysignal.telegram_bot import render

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)

NO_CACHES: list[tuple[str, datetime | None]] = []


def _signal(result):
    return render.signal_message(result, NO_CACHES, NOW)


def _status(**overrides):
    fields = {
        "uptime": timedelta(hours=3),
        "last_tick_at": None,
        "ok": True,
        "error": None,
        "alert_state": None,
    }
    return render.RunnerStatus(**{**fields, **overrides})


# --- sub-signal blocks ---------------------------------------------------------------


def test_bullets_splits_and_escapes():
    assert render._bullets("a & b | c < d") == "• a &amp; b\n• c &lt; d"


def test_subsignal_block_uses_table_when_present(make_subsignal):
    s = make_subsignal("market_dip", "flat", "raw detail", table="col1 col2\nval1 val2")
    assert "<pre>col1 col2\nval1 val2</pre>" in render.subsignal_block(s)


def test_subsignal_block_uses_bullets_when_no_table(make_subsignal):
    s = make_subsignal("vix", "none", "calm")
    assert "•" in render.subsignal_block(s)


def test_subsignal_block_renders_footer_under_the_table(make_subsignal):
    s = make_subsignal("yield_curve", "flat", "d", table="col1\nval1", footer="🔻 Cuts priced in")
    assert render.subsignal_block(s).endswith("</pre>\n🔻 Cuts priced in")


def test_subsignal_block_escapes_the_footer(make_subsignal):
    s = make_subsignal("yield_curve", "flat", "d", table="col1", footer="2y < FFR & falling")
    assert "2y &lt; FFR &amp; falling" in render.subsignal_block(s)


def test_subsignal_block_omits_footer_when_absent(make_subsignal):
    s = make_subsignal("yield_curve", "flat", "d", table="col1\nval1")
    assert render.subsignal_block(s).endswith("</pre>")


def test_subsignal_block_uses_no_change_label(make_subsignal):
    s = make_subsignal("fed_rate", "no_change", "x", passes=True)
    assert "No change" in render.subsignal_block(s)


def test_subsignal_block_advisory_uses_info_mark_not_pass_fail(make_subsignal):
    s = make_subsignal("yield_curve", "inverted", "x", passes=False, advisory=True)
    formatted = render.subsignal_block(s)
    assert formatted.startswith("ℹ️")
    assert "✅" not in formatted
    assert "❌" not in formatted
    assert "Inverted" in formatted


# --- the signal message ---------------------------------------------------------------


def test_signal_message_escapes_real_ampersand_case(make_subsignal, make_buy_signal):
    # the real production case: sector detail strings can contain a literal '&'
    s = make_subsignal("sector", "growing", "Aerospace & Defense: +1%", passes=True)
    result = make_buy_signal([s], state="strong")
    assert "Aerospace &amp; Defense" in _signal(result)


def test_signal_message_includes_missing_line(make_buy_signal):
    result = make_buy_signal([], missing_signals=["sector", "vix"])
    assert "missing: sector, vix" in _signal(result)


def test_signal_message_includes_the_freshness_table(make_buy_signal):
    result = make_buy_signal([])
    message = render.signal_message(result, [("vix", NOW - timedelta(hours=2))], NOW)
    assert "Data as of" in message
    assert "vix" in message
    assert "2h ago" in message


def test_signal_message_header_count_excludes_advisory(make_subsignal, make_buy_signal):
    subs = [
        make_subsignal("vix", "strong", "x", passes=True),
        make_subsignal("yield_curve", "inverted", "x", passes=False, advisory=True),
    ]
    result = make_buy_signal(subs, state="strong")
    assert "(1/1 conditions met)" in _signal(result)


# --- cache ages ---------------------------------------------------------------


def test_humanize_age_no_cache():
    assert render.humanize_age(None, NOW) == "no cache"


def test_humanize_age_just_now():
    assert render.humanize_age(NOW, NOW) == "just now"


def test_humanize_age_minutes():
    assert render.humanize_age(NOW - timedelta(minutes=5), NOW) == "5m ago"


def test_humanize_age_hours():
    assert render.humanize_age(NOW - timedelta(hours=3), NOW) == "3h ago"


def test_humanize_age_days():
    assert render.humanize_age(NOW - timedelta(days=2), NOW) == "2d ago"


def test_freshness_table_lists_every_cache_it_is_given():
    table = render.freshness_table([("vix", None), ("sector", NOW)], NOW)
    assert "vix" in table
    assert "no cache" in table
    assert "sector" in table


# --- /status ---------------------------------------------------------------


def test_status_message_never_ticked():
    assert "never" in render.status_message(_status(), [], NOW)


def test_status_message_ok_after_tick():
    status = _status(last_tick_at=NOW, ok=True, alert_state="none")
    assert "(ok)" in render.status_message(status, [], NOW)


def test_status_message_failed_after_tick():
    status = _status(last_tick_at=NOW, ok=False, error="boom")
    assert "FAILED (boom)" in render.status_message(status, [], NOW)


def test_status_message_names_the_alert_state():
    status = _status(alert_state="strong")
    assert "Entry conditions aligned" in render.status_message(status, [], NOW)


def test_status_message_lists_each_collectors_last_attempt():
    statuses = [
        CollectorStatus("yield_curve", NOW - timedelta(hours=2), True, None),
        CollectorStatus("margin_debt", NOW, False, "cfbypass unreachable"),
    ]

    message = render.status_message(_status(), statuses, NOW)

    assert "collector" in message
    assert "yield_curve" in message
    assert "2h ago" in message
    assert "cfbypass unreachable" in message


def test_status_message_says_so_when_nothing_has_been_attempted():
    assert "no refresh attempted yet" in render.status_message(_status(), [], NOW)


def test_status_message_truncates_a_runaway_error():
    statuses = [CollectorStatus("fed_rate", NOW, False, "x" * 500)]

    assert "x" * 200 not in render.status_message(_status(), statuses, NOW)


# --- stale-collector notes ---------------------------------------------------------------


def test_stale_collector_notes_warns_when_a_collector_is_failing():
    statuses = [CollectorStatus("valuations", NOW, False, "ValueError: FY1 not found")]

    notes = render.stale_collector_notes("valuations", [], statuses)

    assert len(notes) == 1
    assert "FY1 not found" in notes[0]


def test_stale_collector_notes_stays_quiet_when_the_failure_was_already_reported():
    statuses = [CollectorStatus("valuations", NOW, False, "boom")]

    assert render.stale_collector_notes("valuations", [("valuations", "boom")], statuses) == []


def test_stale_collector_notes_stays_quiet_when_the_collector_is_healthy():
    statuses = [CollectorStatus("valuations", NOW, True, None)]

    assert render.stale_collector_notes("valuations", [], statuses) == []


def test_stale_collector_notes_stays_quiet_when_the_collector_is_unknown():
    assert render.stale_collector_notes("valuations", [], []) == []


def test_refresh_failures_lists_each_collector_and_escapes_it():
    block = render.refresh_failures([("fed_rate", "HTTPError: <503>")])

    assert "Refresh failed" in block
    assert "fed_rate" in block
    assert "&lt;503&gt;" in block


# --- /whattobuy ---------------------------------------------------------------


def test_opportunity_message_is_rendered_from_the_opportunity_alone(make_opportunity):
    message = render.opportunity_message(make_opportunity(), easing_obs_needed=1)

    assert "What to buy" in message
    assert "20.0" in message
    assert "Small-cap band: candidate" in message
    assert "FedWatch: 1/3 easing (ease → no change → no change, rate support: yes, need ≥1)" in message


def test_opportunity_message_names_the_buy_signal_context(make_opportunity):
    opp = make_opportunity()
    opp.buy_signal_state = "none"

    assert "Buy signal (context): No buy signal" in render.opportunity_message(opp, 1)


# --- the whole message, byte for byte ---------------------------------------------------------


_GOLDEN_TABLE = (
    "Thru         10y-3m   2y-FFR \n"
    "-----------  -------  -------\n"
    "2026-08-14   +0.79pp  +0.59pp\n"
    "2026-08-27*  +0.82pp  +0.57pp"
)

_GOLDEN_MESSAGE = (
    "🟢 <b>Entry conditions aligned</b> (1/2 conditions met)\n"
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


def test_signal_message_renders_the_whole_message(make_subsignal, make_buy_signal):
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

    message = render.signal_message(result, [("vix", None), ("yield_curve", None)], NOW)

    assert message == _GOLDEN_MESSAGE
