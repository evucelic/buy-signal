"""The /portfolio deck built from a synthetic IBKR statement: every tab readable on a phone."""
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from buysignal.broker import flex
from buysignal.portfolio import instruments, panels, plan
from buysignal.portfolio.store import PortfolioStore

STATEMENT = flex.parse((Path(__file__).parent / "fixtures" / "flex_statement.xml").read_bytes())


@pytest.fixture(scope="module")
def deck(tmp_path_factory):
    """Built once: drawing every chart takes a couple of seconds."""
    store = PortfolioStore(tmp_path_factory.mktemp("deck") / "portfolio.sqlite3")
    store.save_statement(STATEMENT, datetime(2026, 11, 18, 7, tzinfo=timezone.utc))
    store.set_targets({"VUAA": 55, "VWCG": 20, "AVWS": 20, "CASH": 5})
    days = pd.bdate_range("2025-06-01", "2026-11-17")
    wave = np.sin(np.arange(len(days)) / 9.)
    closes = {"VUAA": pd.Series(110 + 8 * wave + np.arange(len(days)) * .03, index=days),
              "VWCG": pd.Series(41 + wave, index=days), "AVWS": pd.Series(25 + .7 * np.cos(np.arange(len(days)) / 5.), index=days)}
    quotes = [{"symbol": s, "price": float(c.iloc[-1])} for s, c in closes.items()]
    account = panels.Account(
        positions=store.positions(), holdings=store.holdings(), cash=store.cash(), nav=store.nav(), deposits=store.deposits(),
        trades=store.trades(), meta=store.account(), targets=store.targets(), prices=plan.prices(quotes, store.positions()),
        quotes=quotes, eurusd=1.10, closes=closes, eurusd_history=pd.Series(1.10, index=days),
        benchmark=pd.Series(np.linspace(100, 115, len(days)), index=days))
    yield panels.build(account)
    instruments.set_current(None)


def balanced(html: str) -> bool:
    stack = []

    class Check(HTMLParser):
        def handle_starttag(self, tag, attrs):
            stack.append(tag)

        def handle_endtag(self, tag):
            assert stack.pop() == tag
    Check().feed(html)
    return not stack


def test_every_tab_fits_a_photo_caption_with_tables_a_phone_does_not_wrap(deck):
    assert {view.partition("@")[0] for view in deck} == {name for name, _ in panels.TABS}
    for view, (caption, _) in deck.items():
        assert len(caption) <= 1024 and balanced(caption), view
        for table in caption.split("<pre>")[1:]:
            assert max(map(len, table.split("</pre>")[0].splitlines())) <= 33, view


def test_every_tab_with_data_has_a_chart(deck):
    assert all(png and png.startswith(b"\x89PNG") for view, (_, png) in deck.items() if view != "pf_pe")


def test_overview_and_returns_follow_the_range_button(deck):
    assert deck["pf_overview"][0] != deck["pf_overview@3m"][0]
    assert "since Nov 2025" in deck["pf_returns@max"][0] and "3 months" in deck["pf_returns@3m"][0]


def test_the_whole_history_compares_with_the_same_deposits_in_the_benchmark(deck):
    caption = deck["pf_overview@max"][0]

    assert "Same in VWCE" in caption and "You vs VWCE" in caption


def test_returns_put_the_account_beside_the_benchmark_without_deposits(deck):
    caption = deck["pf_returns"][0]

    assert "deposits removed" in caption and ("ahead of VWCE" in caption or "behind VWCE" in caption)


def test_buttons_keep_the_chosen_range_when_switching_tabs():
    keyboard = panels.keyboard("abcdef123456", "pf_returns@3m")

    data = [button["callback_data"] for row in keyboard["inline_keyboard"] for button in row]
    assert "r:abcdef123456:pf_overview@3m" in data and "r:abcdef123456:pf_mix" in data
    assert all(len(item.encode()) <= 64 for item in data)  # Telegram's callback limit
