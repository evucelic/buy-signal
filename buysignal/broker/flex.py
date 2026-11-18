"""Interactive Brokers Flex Web Service client: read-only statements of positions, trades and cash.

Errors carry IBKR's code and message only; the token never reaches a log line or an exception.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import logging
import time
import xml.etree.ElementTree as ET

from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

BASE_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
USER_AGENT = "Python/3.11"  # IBKR requires a User-Agent naming the technology and version
VERSION = "3"
TIMEOUT_SEC = 30
POLL_SEC = (2, 3, 5, 8, 13, 20)  # waits while IBKR generates the statement, about a minute in all
# Codes meaning "not ready yet, ask again": generation in progress, server busy, too many requests.
_RETRY = {"1001", "1004", "1005", "1006", "1007", "1008", "1009", "1018", "1019", "1021"}
# Codes the owner has to act on, in words that say what to do (IBKR Flex Web Service v3 error codes).
_ACTIONS = {
    "1011": "the Flex Web Service is not active: enable it under Reporting > Flex Queries",
    "1012": "the token has expired: generate a new one and update IBKR_FLEX_TOKEN",
    "1013": "this server's IP address is not the one the token is locked to",
    "1014": "the query ID is not valid: check IBKR_FLEX_QUERY_ID",
    "1015": "the token is not valid: check IBKR_FLEX_TOKEN",
    "1016": "the account is not valid for this query",
}


class FlexError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"IBKR Flex {code}: {message}")
        self.code = code


@dataclass(frozen=True)
class Position:
    symbol: str
    description: str
    currency: str
    exchange: str
    isin: str
    conid: str
    quantity: Decimal
    mark_price: Decimal
    value: Decimal
    cost: Decimal          # cost basis in the listing currency, commissions included
    report_date: date


@dataclass(frozen=True)
class Trade:
    trade_id: str
    symbol: str
    currency: str
    exchange: str
    trade_date: date
    quantity: Decimal      # signed: negative for a sale
    price: Decimal
    commission: Decimal    # what IBKR charged, positive, in commission_currency
    commission_currency: str


@dataclass(frozen=True)
class Statement:
    base_currency: str
    from_date: date
    to_date: date
    generated_at: datetime
    positions: tuple[Position, ...]
    trades: tuple[Trade, ...]
    cash: dict[str, Decimal]                                        # ending cash per currency
    nav: tuple[tuple[date, Decimal, Decimal], ...] = ()             # (day, cash, total) in base
    deposits: tuple[tuple[date, Decimal], ...] = field(default=())  # net deposits in base, by day


def _day(value: str) -> date:
    """Flex dates follow the query's chosen format; accept the ones Client Portal offers."""
    text = value.split(";")[0].split(",")[0].split(" ")[0].strip()
    for pattern in ("%Y%m%d", "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    raise ValueError(f"Unrecognised Flex date {value!r}")


def _number(value: str | None) -> Decimal:
    try:
        return Decimal((value or "0").replace(",", ""))
    except InvalidOperation:
        raise ValueError(f"Unrecognised Flex number {value!r}") from None


def _generated(value: str) -> datetime:
    day = _day(value)
    rest = value.replace(";", " ").replace(",", " ").split()
    clock = rest[1] if len(rest) > 1 else "000000"
    clock = clock.replace(":", "")[:6].ljust(6, "0")
    return datetime(day.year, day.month, day.day, int(clock[:2]), int(clock[2:4]), int(clock[4:6]))


def _finest(items, levels: tuple[str, ...]):
    """Rows at the finest level of detail present, so a query with two levels never counts twice."""
    rows = list(items)
    present = {(row.get("levelOfDetail") or "").upper() for row in rows}
    chosen = next((level for level in levels if level in present), None)
    return [row for row in rows if (row.get("levelOfDetail") or "").upper() in (chosen, "")]


def parse(payload: bytes) -> Statement:
    """One account's Activity statement.

    A missing section raises: an absent <OpenPositions> must not replace stored holdings with nothing.
    """
    root = ET.fromstring(payload)
    if root.tag == "FlexStatementResponse":
        raise FlexError(root.findtext("ErrorCode") or "?", root.findtext("ErrorMessage") or "no statement")
    statements = root.findall("./FlexStatements/FlexStatement")
    if len(statements) != 1:
        raise ValueError(f"Expected one account statement, got {len(statements)}")
    node = statements[0]
    for section in ("OpenPositions", "CashReport", "Trades"):
        if node.find(section) is None:
            raise ValueError(f"The Flex query has no {section} section")
    info = node.find("AccountInformation")
    base = (info.get("currency") if info is not None else None) or "EUR"

    positions = []
    for item in node.iterfind("./OpenPositions/OpenPosition"):
        if item.get("levelOfDetail", "SUMMARY") != "SUMMARY" or item.get("assetCategory", "STK") != "STK":
            continue
        positions.append(Position(
            symbol=item.get("symbol", ""), description=item.get("description", ""), currency=item.get("currency", ""),
            exchange=item.get("listingExchange", ""), isin=item.get("isin", ""), conid=item.get("conid", ""),
            quantity=_number(item.get("position")), mark_price=_number(item.get("markPrice")),
            value=_number(item.get("positionValue")), cost=_number(item.get("costBasisMoney")),
            report_date=_day(item.get("reportDate") or node.get("toDate")),
        ))
    trades = []
    for item in _finest(node.iterfind("./Trades/Trade"), ("EXECUTION", "ORDER")):
        if item.get("assetCategory", "STK") != "STK":
            continue
        trades.append(Trade(
            trade_id=item.get("tradeID") or item.get("ibExecID") or "", symbol=item.get("symbol", ""),
            currency=item.get("currency", ""), exchange=item.get("listingExchange", ""),
            trade_date=_day(item.get("tradeDate") or item.get("dateTime")),
            quantity=_number(item.get("quantity")), price=_number(item.get("tradePrice")),
            commission=abs(_number(item.get("ibCommission"))),
            commission_currency=item.get("ibCommissionCurrency") or item.get("currency", ""),
        ))
    cash, summary = {}, None
    for item in node.iterfind("./CashReport/CashReportCurrency"):
        currency = item.get("currency", "")
        if currency == "BASE_SUMMARY" or item.get("levelOfDetail", "").lower() == "basecurrency":
            summary = _number(item.get("endingCash"))
        elif currency:
            cash[currency] = _number(item.get("endingCash"))
    if not cash and summary is not None:
        cash = {base: summary}  # no currency breakout: the whole balance, in the base currency
    nav = tuple(sorted(
        (_day(item.get("reportDate")), _number(item.get("cash")), _number(item.get("total")))
        for item in node.iterfind("./EquitySummaryInBase/EquitySummaryByReportDateInBase") if item.get("reportDate")
    ))
    deposits = {}
    for item in _finest(node.iterfind("./CashTransactions/CashTransaction"), ("DETAIL", "SUMMARY")):
        if item.get("type") == "Deposits/Withdrawals":
            day = _day(item.get("dateTime") or item.get("reportDate"))
            amount = _number(item.get("amount")) * _number(item.get("fxRateToBase") or "1")
            deposits[day] = deposits.get(day, Decimal(0)) + amount
    return Statement(
        base_currency=base, from_date=_day(node.get("fromDate")), to_date=_day(node.get("toDate")),
        generated_at=_generated(node.get("whenGenerated") or node.get("toDate")),
        positions=tuple(positions), trades=tuple(trades), cash=cash, nav=nav,
        deposits=tuple(sorted(deposits.items())),
    )


def _get(session, url: str, params: dict) -> bytes:
    try:
        response = session.get(url, params=params, timeout=TIMEOUT_SEC, headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
    except requests.RequestException as exc:
        # The request URL carries the token; keep only the kind of failure.
        status = getattr(getattr(exc, "response", None), "status_code", None)
        raise FlexError("http", type(exc).__name__ + (f" {status}" if status else "")) from None
    return response.content


def _response(payload: bytes) -> ET.Element | None:
    """The FlexStatementResponse envelope, or None when the payload is the statement; raises on failure."""
    root = ET.fromstring(payload)
    if root.tag != "FlexStatementResponse":
        return None
    if root.findtext("Status") == "Success":
        return root
    code = root.findtext("ErrorCode") or "?"
    raise FlexError(code, _ACTIONS.get(code, root.findtext("ErrorMessage") or "request failed"))


def _statement_url(envelope: ET.Element) -> str:
    """IBKR's GetStatement URL, accepted only over https on an IBKR host."""
    url = (envelope.findtext("Url") or envelope.findtext("url") or "").strip()
    parsed = urlparse(url)
    trusted = parsed.scheme == "https" and (parsed.hostname or "").endswith(".interactivebrokers.com")
    return url if trusted and parsed.path.endswith("/GetStatement") else f"{BASE_URL}/GetStatement"


def fetch(token: str, query_id: str, *, session=None, sleep=time.sleep) -> bytes:
    """The statement XML for `query_id`, waiting while IBKR generates it.

    Two requests at least a few seconds apart, then a slowing poll: well inside IBKR's limit of
    one request a second and ten a minute per token.
    """
    session = session or requests.Session()
    envelope = _response(_get(session, f"{BASE_URL}/SendRequest", {"t": token, "q": query_id, "v": VERSION}))
    reference = envelope.findtext("ReferenceCode") if envelope is not None else None
    if not reference:
        raise FlexError("?", "SendRequest returned no reference code")
    url = _statement_url(envelope)
    last = None
    for wait in POLL_SEC:
        sleep(wait)
        payload = _get(session, url, {"t": token, "q": reference, "v": VERSION})
        try:
            if _response(payload) is None:
                return payload
        except FlexError as exc:
            if exc.code not in _RETRY:
                raise
            last = exc
    raise last or FlexError("timeout", "statement not ready")
