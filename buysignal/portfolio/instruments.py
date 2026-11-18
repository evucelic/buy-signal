"""The owner's listings, read from the IBKR statement, with three defaults before the first one.

The trading currency is the listing's, not the fund's: VUAA is a dollar listing in London.
"""
from __future__ import annotations

from dataclasses import dataclass
import threading


@dataclass(frozen=True)
class Instrument:
    symbol: str
    yahoo: str
    exchange: str          # pandas_market_calendars name, e.g. LSE, XETR, XNYS
    currency: str
    us_hours: bool = False  # most of the holdings trade in New York, so quotes are tightest while it is open
    name: str = ""


# Before the first IBKR statement: the listings confirmed by the owner on 2026-11-14.
DEFAULTS = (
    Instrument('VUAA', 'VUAA.L', 'LSE', 'USD', True, 'Vanguard S&P 500 (Acc)'),
    Instrument('VWCG', 'VWCG.DE', 'XETR', 'EUR', False, 'Vanguard FTSE Developed Europe (Acc)'),
    Instrument('AVWS', 'AVWS.DE', 'XETR', 'EUR', True, 'Avantis Global Small Cap Value (Acc)'),
)
_KNOWN = {i.symbol: i for i in DEFAULTS}

# IBKR listing exchange -> (market calendar, Yahoo suffix, display name).
EXCHANGES = {
    'LSE': ('LSE', '.L', 'London'), 'LSEETF': ('LSE', '.L', 'London'),
    'IBIS': ('XETR', '.DE', 'Xetra'), 'IBIS2': ('XETR', '.DE', 'Xetra'), 'XETRA': ('XETR', '.DE', 'Xetra'),
    'AEB': ('XAMS', '.AS', 'Amsterdam'), 'SBF': ('XPAR', '.PA', 'Paris'), 'EBS': ('XSWX', '.SW', 'Zurich'),
    'BVME': ('XMIL', '.MI', 'Milan'), 'BVME.ETF': ('XMIL', '.MI', 'Milan'),
    'NYSE': ('XNYS', '', 'New York'), 'NASDAQ': ('XNYS', '', 'New York'), 'ARCA': ('XNYS', '', 'New York'),
    'BATS': ('XNYS', '', 'New York'), 'AMEX': ('XNYS', '', 'New York'),
}
CALENDAR_NAMES = {calendar: name for calendar, _, name in EXCHANGES.values()}
CASH = 'CASH'  # a target weight for cash kept uninvested

_lock = threading.Lock()
_current: tuple[Instrument, ...] | None = None


def from_listing(symbol: str, exchange: str, currency: str, name: str = '') -> Instrument:
    """An instrument from IBKR's symbol and listing exchange; an unknown exchange keeps no calendar."""
    known = _KNOWN.get(symbol)
    if known and known.currency == currency:
        return known
    calendar, suffix, _ = EXCHANGES.get(exchange.upper(), ('', '', exchange))
    return Instrument(symbol, symbol.replace(' ', '-') + suffix, calendar, currency, False, name)


def all() -> tuple[Instrument, ...]:  # noqa: A001 - reads as instruments.all()
    with _lock:
        return _current if _current is not None else DEFAULTS


def set_current(items) -> None:
    """Replace the listings in use; None restores the defaults."""
    global _current
    with _lock:
        _current = None if items is None else tuple(dict((i.symbol, i) for i in items).values())


def resolve(symbol: str) -> Instrument:
    symbol = symbol.upper()
    matches = [i for i in all() if symbol in (i.symbol, i.yahoo.upper())]
    if len(matches) != 1:
        raise ValueError('Not a listing you hold: ' + ', '.join(i.symbol for i in all()))
    return matches[0]


def calendar_name(calendar: str) -> str:
    return CALENDAR_NAMES.get(calendar, calendar)
