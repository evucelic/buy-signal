"""Shared pytest fixtures."""

import pytest

from buysignal.signals.base import SubSignal
from dataclasses import replace

from buysignal.signals.buy_signal import assess


@pytest.fixture(autouse=True)
def no_broker(monkeypatch):
    """The bot loads a local .env at import; a real IBKR token there must not reach any test."""
    for name in ("IBKR_FLEX_TOKEN", "IBKR_FLEX_QUERY_ID", "IBKR_FLEX_TOKEN_EXPIRES"):
        monkeypatch.delenv(name, raising=False)


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
