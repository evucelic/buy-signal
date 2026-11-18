"""Shared pytest fixtures."""

import pytest

from buysignal.signals.base import SubSignal
from dataclasses import replace

from buysignal.signals.buy_signal import assess


@pytest.fixture(autouse=True)
def checklist_log_path(monkeypatch, tmp_path):
    """The checklist log is append-only real data: no test may write to the one in data/."""
    from buysignal.analysis import checklist_log

    from buysignal.telegram_bot import app

    path = tmp_path / "checklistlog.jsonl"
    monkeypatch.setattr(checklist_log, "CHECKLIST_LOG", path)
    # ETF closes would come from Yahoo through the real portfolio store; a test supplies its own.
    monkeypatch.setattr(app, "_etf_closes", lambda now: {})
    return path


@pytest.fixture
def make_subsignal():
    def _make(name, state="none", detail="", passes=False, score=0.0, advisory=False):
        return SubSignal(name, score, state, detail, passes=passes, advisory=advisory)

    return _make


@pytest.fixture
def make_buy_signal():
    def _make(subsignals, state=None, missing_signals=None):
        result = assess(subsignals, missing_signals or [])
        return result if state is None else replace(result, state=state)

    return _make


@pytest.fixture
def make_opportunity():
    """A canned valuation analysis (the /portfolio P/E tab): small caps at a 25% forward-P/E discount, cuts priced in."""

    def _make():
        from buysignal.analysis import opportunity as az

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
                              notes=["Own-history comparison needs 12 monthly readings; 1 so far"], history_obs=1)

    return _make
