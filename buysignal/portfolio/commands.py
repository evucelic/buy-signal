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
    if lower.startswith('/target'):
        pairs = re.findall(r'([A-Za-z0-9.]+)\s+(\d+(?:\.\d+)?)', text[len('/target'):])
        if not pairs or re.sub(r'([A-Za-z0-9.]+)\s+(\d+(?:\.\d+)?)|\s|%', '', text[len('/target'):]):
            current = store.targets()
            mix = ' '.join(f'{symbol} {weight:.0%}' for symbol, weight in current.items()) if current else 'not set'
            return f'Mix: {esc(mix)}\nUse: /target VUAA 60 VWCG 20 AVWS 20'
        try:
            if len({symbol.upper() for symbol, _ in pairs}) != len(pairs):
                raise ValueError('Name each ETF once.')
            current = store.set_targets(dict(pairs))
        except ValueError as exc:
            return esc(str(exc))
        return 'Mix set: ' + esc(' '.join(f'{symbol} {weight:.0%}' for symbol, weight in current.items()))
    if lower.startswith('/fees'):
        from buysignal.portfolio import plan
        from buysignal.portfolio.instruments import INSTRUMENTS
        argument = text[len('/fees'):].strip()
        if argument.lower() in plan.IBKR:
            for instrument in INSTRUMENTS:
                minimum, rate = plan.IBKR[argument.lower()][instrument.currency]
                store.set_fee(instrument.symbol, minimum, rate * 100)
        elif argument:
            match = re.fullmatch(r'([A-Za-z0-9.]+)\s+(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)%?', argument)
            if not match:
                return ('Use: /fees tiered or /fees fixed (Interactive Brokers\' published rates)\n'
                        'Your own: /fees VUAA 1.70 0.05 (minimum per order, then percent of the order)\nCurrency conversion: /fees FX 0 0.03')
            try:
                store.set_fee(*match.groups())
            except ValueError as exc:
                return esc(str(exc))
        chosen = store.fees()
        # The fee on the latest recorded purchase of each ETF: what the broker really took.
        paid = {}
        for purchase in reversed(store.purchases(limit=1000)):
            if not purchase['voided_at']:
                paid[purchase['symbol']] = float(purchase['fees'])
        rows = []
        for instrument in INSTRUMENTS:
            minimum, rate = chosen.get(instrument.symbol) or plan.DEFAULT_FEES[instrument.currency]
            rows.append(f'{instrument.symbol:<5}{minimum:>6.2f} {instrument.currency}{rate:>8.3%}'
                        + (f'{paid[instrument.symbol]:>6.2f}' if instrument.symbol in paid else ''))
        minimum, rate = chosen.get('FX') or plan.DEFAULT_FX
        rows.append(f'{"FX":<5}{minimum:>6.2f} EUR{rate:>8.3%}')
        unset = [name for name in [*(i.symbol for i in INSTRUMENTS), 'FX'] if name not in chosen]
        note = '\nInteractive Brokers tiered, as published: ' + ', '.join(unset) if unset else ''
        return '<b>Fees per order</b>\n<pre>' + esc('\n'.join(['      Minimum   Of order  Paid', *rows])) + '</pre>' + note
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
