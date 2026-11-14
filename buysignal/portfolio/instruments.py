"""The user's confirmed listings; trading currencies are distinct from fund currencies."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Instrument:
    symbol: str
    yahoo: str
    exchange: str
    currency: str
    us_hours: bool = False  # most of the holdings trade in New York, so quotes are tightest while it is open


INSTRUMENTS = (
    Instrument('VUAA', 'VUAA.L', 'LSE', 'USD', True),
    Instrument('VWCG', 'VWCG.DE', 'XETR', 'EUR'),
    Instrument('AVWS', 'AVWS.DE', 'XETR', 'EUR', True),
)


def resolve(symbol):
    symbol = symbol.upper()
    matches = [i for i in INSTRUMENTS if symbol in (i.symbol, i.yahoo)]
    if len(matches) != 1:
        raise ValueError('Choose VUAA (London/USD), VWCG (IBIS2/EUR), or AVWS (IBIS2/EUR).')
    return matches[0]
