"""pushes.decide(): which message a reading calls for, and what the owner knows once it is delivered."""
from datetime import date

from buysignal.signals.base import SubSignal
from buysignal.signals.buy_signal import assess
from buysignal.telegram_bot.pushes import Seen, decide

TODAY = date(2026, 11, 16)


def _read(vix=15., dip=False, passing=0):
    """A complete checklist: VIX at `vix`, the dip as given, then `passing` of Fed, margin, EPS."""
    subs = [SubSignal("vix", vix, "x", "", passes=vix >= 22), SubSignal("market_dip", -.06 if dip else 0., "x", "", passes=dip)]
    subs += [SubSignal(name, 0., "x", "", passes=i < passing) for i, name in enumerate(("fed_rate", "margin_debt", "sector"))]
    return assess(subs)


def test_a_quiet_reading_sets_the_baseline_without_a_message():
    heading, after = decide(Seen(), _read(passing=1), TODAY)
    assert heading == "" and after == Seen(count=1)


def test_a_watch_trigger_and_a_count_change_share_one_heading():
    heading, after = decide(Seen(count=1), _read(vix=25., passing=1), TODAY)
    assert heading == "🔔 <b>Watch trigger active</b> (VIX elevated)\n🔢 <b>Checklist 1 → 2 of 5</b>\n\n"
    assert after == Seen(alerting=True, count=2)


def test_high_fear_is_its_own_push_once_a_day():
    seen = Seen(alerting=True, count=1)
    heading, after = decide(seen, _read(vix=31.), TODAY)
    assert heading.startswith("🔴 <b>High fear</b>") and after.high_fear_date == TODAY
    calmer = decide(after, _read(vix=25.), TODAY)[1]
    assert decide(calmer, _read(vix=31.), TODAY)[0] == ""  # flapping around 30 the same day


def test_unknown_evidence_neither_clears_a_watch_nor_counts_as_a_change():
    seen = Seen(alerting=True, high_fear=True, count=3)
    heading, after = decide(seen, assess([], ["vix", "market_dip"]), TODAY)
    assert heading == "" and after == seen
    stale = assess([SubSignal("market_dip", -.07, "flat", "", metrics={"stale": True})])
    assert decide(Seen(alerting=True), stale, TODAY) == ("", Seen(alerting=True))


def test_a_watch_clears_on_known_calm_evidence():
    heading, after = decide(Seen(alerting=True, count=0), _read(), TODAY)
    assert heading == "<b>Watch trigger cleared (VIX/SPY)</b>\n\n" and not after.alerting
