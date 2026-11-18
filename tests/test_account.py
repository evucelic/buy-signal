"""The account as stored from IBKR statements, the listings it defines, and when it is read again."""
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from buysignal.broker import flex, sync
from buysignal.portfolio import instruments
from buysignal.portfolio.store import PortfolioStore

STATEMENT = flex.parse((Path(__file__).parent / "fixtures" / "flex_statement.xml").read_bytes())
NOW = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    yield PortfolioStore(tmp_path / "portfolio.sqlite3")
    instruments.set_current(None)  # the listings in use are process-wide


def later(statement, **changes):
    return replace(statement, from_date=date(2026, 11, 18), to_date=date(2026, 11, 18), trades=(), nav=(), deposits=(), **changes)


def test_a_new_statement_replaces_holdings_and_cash(store):
    store.save_statement(STATEMENT, NOW)
    sold_avws = later(STATEMENT, positions=tuple(p for p in STATEMENT.positions if p.symbol != "AVWS"), cash={"EUR": Decimal(4000)})

    store.save_statement(sold_avws, NOW)

    assert set(store.holdings()) == {"VUAA", "VWCG"}
    assert store.cash() == {"EUR": Decimal(4000)}


def test_trades_and_daily_values_outlive_the_statement_that_brought_them(store):
    store.save_statement(STATEMENT, NOW)

    store.save_statement(later(STATEMENT), NOW)

    assert len(store.trades()) == len(STATEMENT.trades)
    assert len(store.nav()) == len(STATEMENT.nav)
    assert sum(amount for _, amount in store.deposits()) == 7600


def test_an_older_statement_never_rolls_the_holdings_back(store):
    store.save_statement(later(STATEMENT), NOW)

    with pytest.raises(ValueError, match="older"):
        store.save_statement(STATEMENT, NOW)
    assert store.account()["statement_to"] == "2026-11-18"


def test_holdings_carry_ibkr_cost_basis_in_the_listing_currency(store):
    store.save_statement(STATEMENT, NOW)

    vuaa = store.holdings()["VUAA"]

    assert (vuaa["quantity"], vuaa["cost"], vuaa["currency"]) == (Decimal(18), Decimal("2003.94"), "USD")


def test_held_listings_become_the_instruments_in_use(store):
    iwda = replace(STATEMENT.positions[0], symbol="IWDA", exchange="AEB", currency="EUR")
    store.save_statement(replace(STATEMENT, positions=(*STATEMENT.positions, iwda)), NOW)

    assert {i.symbol for i in instruments.all()} == {"VUAA", "VWCG", "AVWS", "IWDA"}
    assert instruments.resolve("IWDA").yahoo == "IWDA.AS" and instruments.resolve("IWDA").exchange == "XAMS"
    assert instruments.resolve("VUAA").yahoo == "VUAA.L"


def test_targets_name_held_listings_or_cash_and_total_100(store):
    store.save_statement(STATEMENT, NOW)

    assert store.set_targets({"VUAA": "55", "VWCG": "20", "AVWS": "20", "cash": "5"}) == {
        "VUAA": .55, "VWCG": .2, "AVWS": .2, "CASH": .05}
    with pytest.raises(ValueError, match="total 100"):
        store.set_targets({"VUAA": "60"})
    with pytest.raises(ValueError, match="Not a listing you hold"):
        store.set_targets({"SPY": "100"})


def test_a_sync_stores_the_downloaded_statement(store, monkeypatch):
    monkeypatch.setenv("IBKR_FLEX_TOKEN", "t" * 24)
    monkeypatch.setenv("IBKR_FLEX_QUERY_ID", "42")
    raw = (Path(__file__).parent / "fixtures" / "flex_statement.xml").read_bytes()

    assert sync.update(store, now=NOW, fetch=lambda token, query: raw) is None

    assert store.account()["statement_to"] == "2026-11-17"


def test_ibkr_is_read_again_until_the_statement_covers_the_last_weekday(store):
    monday_noon = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
    assert sync.demands_refresh(store, now=monday_noon)  # nothing stored yet

    store.save_statement(replace(STATEMENT, to_date=date(2026, 11, 12)), monday_noon)  # through Thursday
    assert not sync.demands_refresh(store, now=monday_noon)  # just read: wait before asking again
    assert sync.demands_refresh(store, now=datetime(2026, 11, 16, 16, tzinfo=timezone.utc))

    store.save_statement(replace(STATEMENT, to_date=date(2026, 11, 13)), monday_noon)  # through Friday
    assert not sync.demands_refresh(store, now=datetime(2026, 11, 16, 23, tzinfo=timezone.utc))


def test_the_sync_exists_only_once_ibkr_is_configured(monkeypatch):
    from buysignal.schedule import runner
    monkeypatch.delenv("IBKR_FLEX_TOKEN", raising=False)
    assert "ibkr" not in {c.name for c in runner._macro_collectors()}

    monkeypatch.setenv("IBKR_FLEX_TOKEN", "t" * 24)
    monkeypatch.setenv("IBKR_FLEX_QUERY_ID", "42")
    assert "ibkr" in {c.name for c in runner._macro_collectors()}


def test_token_expiry_is_counted_from_the_date_chosen_when_it_was_made(monkeypatch):
    monkeypatch.setenv("IBKR_FLEX_TOKEN_EXPIRES", "2026-12-01")
    assert sync.token_days_left(date(2026, 11, 18)) == 13

    monkeypatch.delenv("IBKR_FLEX_TOKEN_EXPIRES")
    assert sync.token_days_left(date(2026, 11, 18)) is None


def test_history_older_than_a_year_is_read_window_by_window_until_the_account_starts(store, monkeypatch):
    monkeypatch.setenv("IBKR_FLEX_TOKEN", "t" * 24)
    monkeypatch.setenv("IBKR_FLEX_QUERY_ID", "42")
    funded = replace(STATEMENT, nav=STATEMENT.nav[1:])  # the year opens with money already in the account
    store.save_statement(funded, NOW)
    old_buy = replace(STATEMENT.trades[0], trade_id="1", trade_date=date(2025, 3, 3))
    older = {date(2025, 11, 17): replace(STATEMENT, from_date=date(2024, 11, 18), to_date=date(2025, 11, 17), positions=(),
                                         cash={}, trades=(old_buy,), nav=((date(2025, 3, 3), Decimal(0), Decimal(900)),),
                                         deposits=((date(2025, 3, 3), Decimal(900)),)),
             date(2024, 11, 17): replace(STATEMENT, from_date=date(2023, 11, 19), to_date=date(2024, 11, 17), positions=(),
                                         cash={}, trades=(), nav=(), deposits=())}
    asked = []
    monkeypatch.setattr(sync.flex, "parse", lambda period: older[period[1]])

    while sync.history_due(store):
        sync.backfill(store, fetch=lambda token, query, period: asked.append(period) or period)

    assert asked == [(date(2024, 11, 18), date(2025, 11, 17)), (date(2023, 11, 19), date(2024, 11, 17))]
    assert store.trades()[0]["trade_date"] == "2025-03-03" and store.deposits()[0] == (date(2025, 3, 3), 900.)
    assert set(store.holdings()) == {"VUAA", "VWCG", "AVWS"}  # holdings come only from the latest statement


def test_a_statement_starting_from_nothing_needs_no_older_history(store):
    store.save_statement(STATEMENT, NOW)  # its first daily value is 0

    assert not sync.history_due(store)


def test_a_period_ibkr_did_not_honour_is_refused(store, monkeypatch):
    monkeypatch.setenv("IBKR_FLEX_TOKEN", "t" * 24)
    monkeypatch.setenv("IBKR_FLEX_QUERY_ID", "42")
    store.save_statement(replace(STATEMENT, nav=STATEMENT.nav[1:]), NOW)
    monkeypatch.setattr(sync.flex, "parse", lambda payload: STATEMENT)  # the default year again

    with pytest.raises(ValueError, match="instead of"):
        sync.backfill(store, fetch=lambda token, query, period: b"")
    assert sync.history_due(store)
