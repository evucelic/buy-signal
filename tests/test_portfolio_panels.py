"""The /portfolio deck built from a synthetic IBKR statement: every tab readable on a phone."""
from dataclasses import replace
from datetime import date, datetime, timezone
from html import unescape
from html.parser import HTMLParser
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from buysignal.broker import flex
from buysignal.portfolio import instruments, panels, plan
from buysignal.portfolio.store import PortfolioStore
from scripts.preview_entry_report import sample_cape

STATEMENT = flex.parse((Path(__file__).parent / "fixtures" / "flex_statement.xml").read_bytes())


def make_account(store):
    days = pd.bdate_range("2025-06-01", "2026-11-17")
    wave = np.sin(np.arange(len(days)) / 9.)
    closes = {"VUAA": pd.Series(110 + 8 * wave + np.arange(len(days)) * .03, index=days),
              "VWCG": pd.Series(41 + wave, index=days), "AVWS": pd.Series(25 + .7 * np.cos(np.arange(len(days)) / 5.), index=days)}
    quotes = [{"symbol": s, "price": float(c.iloc[-1])} for s, c in closes.items()]
    return panels.Account(
        positions=store.positions(), holdings=store.holdings(), cash=store.cash(), nav=store.nav(), deposits=store.deposits(),
        trades=store.trades(), meta=store.account(), targets=store.targets(), prices=plan.prices(quotes, store.positions()),
        quotes=quotes, eurusd=1.10, closes=closes, eurusd_history=pd.Series(1.10, index=days),
        benchmarks={"S&P 500": pd.Series(np.linspace(500, 600, len(days)), index=days),
                    "All-World": pd.Series(np.linspace(100, 115, len(days)), index=days)},
        history_complete=store.history_complete(), cape=sample_cape(), sleeves=[("VUAA", 21.)], real_yield=(.018, "2026-11-16"),
        today=date(2026, 11, 18))


@pytest.fixture(scope="module")
def deck(tmp_path_factory):
    """Built once: drawing every chart takes a couple of seconds."""
    store = PortfolioStore(tmp_path_factory.mktemp("deck") / "portfolio.sqlite3")
    store.save_statement(STATEMENT, datetime(2026, 11, 18, 7, tzinfo=timezone.utc))
    store.set_targets({"VUAA": 55, "VWCG": 20, "AVWS": 20, "CASH": 5})
    yield panels.draw(panels.build(make_account(store)))
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
            assert max(map(len, unescape(table.split("</pre>")[0]).splitlines())) <= 33, view


def test_every_tab_has_a_chart(deck):
    assert all(png and png.startswith(b"\x89PNG") for _, png in deck.values())


def test_value_and_returns_follow_the_range_button(deck):
    assert deck["pf_value"][0] != deck["pf_value@1y"][0] != deck["pf_value@3m"][0]
    assert "since Nov 2025" in deck["pf_returns"][0] and "3 months" in deck["pf_returns@3m"][0]


def test_value_compares_the_same_deposits_in_the_sp500_the_target_mix_and_the_world(deck):
    caption = unescape(deck["pf_value"][0])

    assert all(name in caption for name in ("S&P 500", "Target mix", "All-World"))


def money(caption: str, label: str) -> float:
    text = re.sub(r"<[^>]+>", "", unescape(caption))
    line = next(line for line in text.splitlines() if line.startswith(label))
    return float(re.sub(r"[^\d.−-]", "", line.split()[-1]).replace("−", "-"))


def test_money_held_before_the_oldest_day_read_is_shown_so_the_value_adds_up(tmp_path):
    store = PortfolioStore(tmp_path / "portfolio.sqlite3")
    store.save_statement(replace(STATEMENT, nav=STATEMENT.nav[1:]), datetime(2026, 11, 18, 7, tzinfo=timezone.utc))
    caption = panels.build(make_account(store))["pf_value"][0]

    held, deposits, gain, now = (money(caption, label) for label in ("Held", "Deposits", "Market gain", "Now"))

    assert held > 0 and held + deposits + gain == pytest.approx(now, abs=2)
    # €299 gained on about €4,900 of average capital over the year: every deposit counted once.
    assert 5 < float(re.search(r"MWR ([+−-][\d.]+)%", caption).group(1).replace("−", "-")) < 7
    instruments.set_current(None)


def test_holdings_and_cash_that_miss_ibkrs_total_are_flagged(tmp_path):
    store = PortfolioStore(tmp_path / "portfolio.sqlite3")
    store.save_statement(STATEMENT, datetime(2026, 11, 18, 7, tzinfo=timezone.utc))
    account = make_account(store)
    assert panels.reconcile(account) is None

    account.nav[-1] = (account.nav[-1][0], account.nav[-1][1], account.nav[-1][2] + 400)

    assert "differ from IBKR's total" in panels.reconcile(account)
    instruments.set_current(None)


def test_buttons_keep_the_chosen_range_when_switching_tabs():
    keyboard = panels.keyboard("abcdef123456", "pf_returns@3m")

    data = [button["callback_data"] for row in keyboard["inline_keyboard"] for button in row]
    assert "r:abcdef123456:pf_value@3m" in data and "r:abcdef123456:pf_mix" in data
    assert all(len(item.encode()) <= 64 for item in data)  # Telegram's callback limit
    assert [len(row) for row in keyboard["inline_keyboard"]] == [3, 3, 3, 3]


def test_days_before_the_account_was_funded_do_not_stretch_the_history(tmp_path):
    store = PortfolioStore(tmp_path / "portfolio.sqlite3")
    store.save_statement(STATEMENT, datetime(2026, 11, 18, 7, tzinfo=timezone.utc))
    unfunded = tuple((day.date(), 0., 0.) for day in pd.bdate_range("2025-08-01", "2025-11-17"))
    account = make_account(store)
    padded = replace(account, nav=list(unfunded) + account.nav)

    for view in ("pf_value", "pf_returns"):
        assert panels.build(padded)[view][0] == panels.build(account)[view][0], view
    instruments.set_current(None)


def table_row(caption: str, label: str) -> list[str]:
    text = re.sub(r"<[^>]+>", "", unescape(caption))
    return next(line for line in text.splitlines() if line.startswith(label + " ")).split()[1:]


def percent(cell: str) -> float:
    return float(cell.replace("−", "-").rstrip("%"))



def test_returns_show_the_money_weighted_figure_of_the_value_tab_beside_the_time_weighted_one(deck):
    value, returns = deck["pf_value"][0], deck["pf_returns"][0]
    assert "TWR" in returns and "MWR" in returns

    you, sp = table_row(returns, "You"), table_row(returns, "S&P")
    assert f"MWR {you[1]}" in re.sub(r"<[^>]+>", "", unescape(value))
    assert 0 < float(re.search(r"Avg euro in\s+([\d.]+) yr", value).group(1)) < 1
    # Same deposits: ahead of the S&P 500 in euros exactly when ahead in money-weighted return.
    assert (percent(you[1]) > percent(sp[1])) == (money(value, "S&P 500") > 0)


def test_trades_list_the_last_twelve_purchases_within_a_caption(tmp_path):
    store = PortfolioStore(tmp_path / "portfolio.sqlite3")
    buy = STATEMENT.trades[0]
    days = pd.bdate_range("2025-06-02", periods=20, freq="15B")
    many = tuple(replace(buy, trade_id=str(i), trade_date=day.date()) for i, day in enumerate(days))
    store.save_statement(replace(STATEMENT, trades=many), datetime(2026, 11, 18, 7, tzinfo=timezone.utc))
    caption = panels.build(make_account(store))["pf_trades"][0]

    listed = re.findall(r"^\s?\d{1,2} \w{3} \d{2} ", re.sub(r"<[^>]+>", "", unescape(caption)), re.M)
    assert len(listed) == 12 and listed[-1].strip() == panels._day(days[-1]).strip()
    assert len(caption) <= 1024
    instruments.set_current(None)
