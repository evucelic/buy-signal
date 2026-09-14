"""Shared pytest fixtures.

Nothing here injects Telegram credentials any more: telegram_bot/render.py is pure and
telegram_bot/transport.py reads the environment at send time, so only the transport tests
that actually send need a token, and they set their own.
"""

import pytest

from buysignal.signals.base import SubSignal
from buysignal.signals.buy_signal import BuySignal


@pytest.fixture
def make_subsignal():
    def _make(
        name, state="none", detail="", passes=False, table=None, score=0.0, advisory=False, footer=None
    ):
        return SubSignal(
            name, score, state, detail, passes=passes, table=table, advisory=advisory, footer=footer
        )

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


@pytest.fixture
def make_opportunity():
    """A canned /whattobuy analysis: small caps at a 25% forward-P/E discount, cuts priced in."""

    def _make():
        from buysignal import analyzer as az

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

    return _make
