"""Compact clinical prices and holdings for the user's exchange window."""
from datetime import datetime, timezone
from decimal import Decimal

from buysignal.portfolio.instruments import INSTRUMENTS
from buysignal.telegram_bot.views import stamp, percent
from buysignal.telegram_bot.render import esc
from buysignal.signals.base import format_table


def quote_block(quotes, *, title='Your ETFs'):
    by_symbol = {q['symbol']: q for q in quotes}
    rows, dates, cached, daily = [], {}, [], []
    for instrument in INSTRUMENTS:
        quote = by_symbol.get(instrument.symbol, {})
        price = quote.get('price')
        label = instrument.symbol
        if quote.get('refresh_failed') and price is not None:
            label += '*'
            cached.append(instrument.symbol)
        observed = stamp(quote.get('observed_at'))
        if observed != '—':
            dates[instrument.symbol] = observed[:10]
        if quote.get('interval') == '1d':
            bar = 'daily'
            daily.append(instrument.symbol)
        else:
            bar = observed[11:16] if len(observed) > 10 else '—'
        rows.append([label, f'{price:,.2f} {instrument.currency}' if price is not None else '—',
                     percent(quote.get('change')) if price is not None else '—', bar])
    lines = [f'<b>{esc(title)}</b>', '<pre>' + esc(format_table(['ETF', 'Price', 'Day', 'Bar'], rows)) + '</pre>']
    if dates:
        unique = set(dates.values())
        observed = next((stamp(q.get('observed_at')) for q in quotes if q.get('observed_at')), '')
        zone = observed.rsplit(' ', 1)[-1] if len(observed) > 10 else ''
        source = next(iter(unique)) if len(unique) == 1 else ' · '.join(f'{symbol} {day}' for symbol, day in dates.items())
        cadence = 'Daily' if daily and len(daily) == len(dates) else '5m / daily' if daily else '5m'
        lines.append(f'{cadence} bars · {esc(source)} {esc(zone)}'.rstrip())
    if daily:
        lines.append('Daily fallback: ' + ', '.join(daily))
    if cached:
        lines.append('* Cached: ' + ', '.join(cached))
    return '\n'.join(lines)


def holdings_block(holdings, quotes=()):
    if not holdings:
        return 'No purchases recorded yet. Use /buy or send “bought 10 VUAA @ 151.25”.'
    by_symbol = {q['symbol']: q for q in quotes}
    lines, totals = ['<b>Recorded positions</b>'], {}
    for instrument in INSTRUMENTS:
        item = holdings.get(instrument.symbol)
        if not item:
            continue
        lines.append(f'<b>{instrument.symbol}</b> · {item["quantity"]:f} shares · avg {item["average"]:,.4f} {instrument.currency}')
        detail = f'Cost {item["cost"]:,.2f} {instrument.currency}'
        quote = by_symbol.get(instrument.symbol)
        if quote and quote.get('price') is not None:
            value = Decimal(str(quote['price'])) * item['quantity']
            detail += f' · value {value:,.2f} {instrument.currency}'
        lines.append(detail)
        totals[instrument.currency] = totals.get(instrument.currency, Decimal(0)) + item['cost']
    lines.append('Invested, fees included: ' + ' / '.join(f'{value:,.2f} {currency}' for currency, value in totals.items()))
    return '\n'.join(lines)


def portfolio_message(quotes, holdings, now=None):
    now = now or datetime.now(timezone.utc)
    return quote_block(quotes) + f'\nSent {stamp(now)}\n\n' + holdings_block(holdings, quotes)


def preclose_heading(sessions):
    closes = sorted(set(stamp(s.closes) for s in sessions))
    names = ' / '.join('London' if s.exchange == 'LSE' else 'IBIS2/Xetra' for s in sessions)
    return '<b>⏳ Exchange pre-close update</b>\n' + esc(names + ' · regular close ' + ' / '.join(closes)) + '\n\n'
