"""Shared pytest fixtures and setup.

Sets fake Telegram credentials before anything imports telegram_bot (its module-level
os.environ[...] reads would otherwise KeyError, or worse, silently pick up a real .env;
python-dotenv's load_dotenv() defaults to override=False, so pre-set fake values here win).
"""

import os

os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
os.environ["TELEGRAM_CHAT_ID"] = "12345"

import pytest

from signals.base import SubSignal
from signals.buy_signal import BuySignal


@pytest.fixture
def make_subsignal():
    def _make(name, state="none", detail="", passes=False, table=None, score=0.0, advisory=False):
        return SubSignal(name, score, state, detail, passes=passes, table=table, advisory=advisory)

    return _make


@pytest.fixture
def make_buy_signal():
    def _make(subsignals, state="none", missing_signals=None):
        required = [s for s in subsignals if not s.advisory]
        passing = sum(1 for s in required if s.passes)
        return BuySignal(
            score=passing / len(required) if required else 0.0,
            state=state,
            passing_count=passing,
            subsignals=subsignals,
            missing_signals=missing_signals or [],
        )

    return _make
