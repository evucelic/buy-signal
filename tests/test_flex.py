"""Reading an Interactive Brokers Flex statement: what is parsed, refused and never leaked."""
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import requests

from buysignal.broker import flex

STATEMENT = (Path(__file__).parent / "fixtures" / "flex_statement.xml").read_bytes()
TOKEN = "123456789012345678901234"


def envelope(body: str) -> bytes:
    return f'<FlexStatementResponse timestamp="18 November, 2026 06:15 AM EST">{body}</FlexStatementResponse>'.encode()


ACCEPTED = envelope("<Status>Success</Status><ReferenceCode>555</ReferenceCode>"
                    "<Url>https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement</Url>")
IN_PROGRESS = envelope("<Status>Fail</Status><ErrorCode>1019</ErrorCode>"
                       "<ErrorMessage>Statement generation in progress. Please try again shortly.</ErrorMessage>")


class FakeIbkr:
    """Answers each GET with the next queued body (or raises it), and remembers what was asked."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def get(self, url, params, timeout, headers):
        self.requests.append((url, dict(params), headers))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        response = requests.Response()
        response.status_code, response._content = 200, answer
        return response


def test_a_statement_gives_holdings_cash_trades_and_daily_values():
    statement = flex.parse(STATEMENT)

    assert (statement.base_currency, statement.to_date) == ("EUR", date(2026, 11, 17))
    vuaa = next(p for p in statement.positions if p.symbol == "VUAA")
    assert (vuaa.quantity, vuaa.currency, vuaa.exchange, vuaa.cost) == (Decimal(18), "USD", "LSEETF", Decimal("2003.94"))
    assert statement.cash == {"EUR": Decimal("1650.10"), "USD": Decimal("67.75")}
    first = statement.trades[0]
    assert (first.symbol, first.trade_date, first.quantity, first.commission) == ("VUAA", date(2025, 11, 24), Decimal(10), Decimal("1.70"))
    assert statement.nav[-1][0] == date(2026, 11, 17)
    assert sum(amount for _, amount in statement.deposits) == Decimal(7600)


def test_a_query_missing_a_section_is_refused_rather_than_read_as_empty():
    without_trades = STATEMENT.split(b"<Trades>")[0] + STATEMENT.split(b"</Trades>")[1]

    with pytest.raises(ValueError, match="no Trades section"):
        flex.parse(without_trades)


def test_an_expired_token_says_what_to_do():
    expired = envelope("<Status>Fail</Status><ErrorCode>1012</ErrorCode><ErrorMessage>Token has expired.</ErrorMessage>")

    with pytest.raises(flex.FlexError, match="generate a new one"):
        flex.fetch(TOKEN, "42", session=FakeIbkr(expired), sleep=lambda _: None)


def test_fetch_waits_while_ibkr_generates_the_statement():
    ibkr, waits = FakeIbkr(ACCEPTED, IN_PROGRESS, STATEMENT), []

    assert flex.fetch(TOKEN, "42", session=ibkr, sleep=waits.append) == STATEMENT

    (send_url, send, headers), _, (_, get, _) = ibkr.requests
    assert send_url.endswith("/SendRequest") and send == {"t": TOKEN, "q": "42", "v": "3"}
    assert get == {"t": TOKEN, "q": "555", "v": "3"}
    assert headers["User-Agent"].startswith("Python/")  # IBKR refuses programmatic requests without one
    assert len(waits) == 2 and all(wait >= 1 for wait in waits)  # IBKR allows one request a second


def test_the_token_goes_only_to_interactive_brokers():
    redirected = ACCEPTED.replace(b"https://ndcdyn.interactivebrokers.com", b"https://collector.example.com")
    ibkr = FakeIbkr(redirected, STATEMENT)

    flex.fetch(TOKEN, "42", session=ibkr, sleep=lambda _: None)

    assert all(".interactivebrokers.com/" in url for url, _, _ in ibkr.requests)


def test_a_network_failure_never_carries_the_token():
    failure = requests.ConnectionError(f"Max retries exceeded with url: /SendRequest?t={TOKEN}&q=42")

    with pytest.raises(flex.FlexError) as raised:
        flex.fetch(TOKEN, "42", session=FakeIbkr(failure), sleep=lambda _: None)

    assert TOKEN not in str(raised.value) and "ConnectionError" in str(raised.value)


def test_without_a_currency_breakout_the_cash_is_the_base_currency_total():
    summary_only = STATEMENT.replace(b'<CashReportCurrency accountId="U0000000" currency="EUR" levelOfDetail="Currency" endingCash="1650.10" />', b"")
    summary_only = summary_only.replace(b'<CashReportCurrency accountId="U0000000" currency="USD" levelOfDetail="Currency" endingCash="67.75" />', b"")

    assert flex.parse(summary_only).cash == {"EUR": Decimal("1712.40")}


def test_a_trade_listed_both_per_order_and_per_execution_counts_once():
    order_rows = STATEMENT.replace(b'levelOfDetail="EXECUTION"', b'levelOfDetail="ORDER"')
    both = STATEMENT.replace(b"</Trades>", order_rows.split(b"<Trades>")[1].split(b"</Trades>")[0] + b"</Trades>")

    assert len(flex.parse(both).trades) == len(flex.parse(STATEMENT).trades)


def test_a_deposit_counts_on_the_day_ibkr_booked_it():
    booked_later = STATEMENT.replace(b'dateTime="20260515" reportDate="20260515"', b'dateTime="20260515" reportDate="20260518"')

    deposits = dict(flex.parse(booked_later).deposits)

    assert date(2026, 5, 18) in deposits and date(2026, 5, 15) not in deposits
