"""Strict purchase syntax: never guess execution prices, currencies, quantities or dates."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
import os
import re
from zoneinfo import ZoneInfo

from buysignal.portfolio.instruments import resolve
from buysignal.portfolio.journal import number
from buysignal.telegram_bot.render import esc

USAGE = 'Use: bought 10 VUAA @ 151.25 USD on 2026-11-05\nOptional: fees 1.00. Without a date, today is used in your configured timezone.'
_PATTERN = re.compile(r'^(?:/buy|bought|buy)\s+(\d+(?:\.\d+)?)\s+([A-Z0-9.]+)\s*@\s*(\d+(?:\.\d+)?)(?:\s+(USD|EUR|GBP))?(?:\s+(?:on\s+)?(\d{4}-\d{2}-\d{2}|today|yesterday))?(?:\s+fees\s+(\d+(?:\.\d+)?))?$', re.I)


@dataclass(frozen=True)
class Purchase:
    symbol: str
    quantity: Decimal
    price: Decimal
    currency: str
    day: date
    fees: Decimal


def parse(text, now=None):
    match = _PATTERN.fullmatch(text.strip())
    if not match:
        raise ValueError(USAGE)
    quantity, symbol, price, currency, day, fees = match.groups()
    instrument = resolve(symbol)
    if currency and currency.upper() != instrument.currency:
        raise ValueError(f'{instrument.symbol} on {instrument.exchange} trades in {instrument.currency}.')
    zone = ZoneInfo(os.environ.get('TELEGRAM_TIMEZONE', 'Europe/Zagreb'))
    today = (now or datetime.now(zone)).astimezone(zone).date()
    if day is None or day.lower() == 'today':
        parsed_day = today
    elif day.lower() == 'yesterday':
        parsed_day = today - timedelta(days=1)
    else:
        parsed_day = date.fromisoformat(day)
    if parsed_day > today:
        raise ValueError('A recorded purchase cannot have a future date.')
    return Purchase(instrument.symbol, number(quantity), number(price), instrument.currency, parsed_day, number(fees or '0', positive=False))


def receipt(row):
    total = Decimal(row['quantity']) * Decimal(row['price']) + Decimal(row['fees'])
    status = 'Voided purchase' if row['voided_at'] else 'Purchase recorded'
    return (f'<b>{status} #{row["id"]}</b>\n'
            f'{esc(row["symbol"])} · {row["quantity"]} @ {row["price"]} {row["currency"]}\n'
            f'{row["trade_date"]} · fees {row["fees"]} · total {total:,.2f} {row["currency"]}\n'
            f'/voidtrade {row["id"]} to correct this record')


def handle(text, store, *, now=None, source_key=None):
    lower = text.lower()
    if lower.startswith(('/buy ', 'bought ', 'buy ')) or lower == '/buy':
        try:
            item = parse(text, now)
            return receipt(store.record(item.symbol, item.quantity, item.price, item.day,
                                       fees=item.fees, currency=item.currency, source_key=source_key))
        except ValueError as exc:
            return esc(str(exc))
    if lower.startswith('/voidtrade'):
        match = re.fullmatch(r'/voidtrade\s+(\d+)', lower)
        if not match:
            return 'Use /voidtrade PURCHASE_ID. See /trades for IDs.'
        try:
            row = store.void(int(match.group(1)))
            return f'Purchase #{row["id"]} voided. Holdings recalculated.'
        except ValueError as exc:
            return esc(str(exc))
    if lower == '/trades':
        rows = store.purchases()
        if not rows:
            return 'No purchases recorded.\n' + USAGE
        return '<b>Purchase journal · latest 30</b>\n' + '\n'.join(
            f'#{r["id"]} {r["trade_date"]} {esc(r["symbol"])} · {r["quantity"]} @ {r["price"]} {r["currency"]}'
            + (' · VOID' if r['voided_at'] else '') for r in rows)
    if lower == '/holdings':
        from buysignal.portfolio.presentation import holdings_block
        return holdings_block(store.holdings())
    return None
