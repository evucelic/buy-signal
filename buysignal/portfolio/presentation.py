"""Compact clinical prices and holdings for the user's exchange window."""
from datetime import datetime, timezone
from decimal import Decimal

import pandas as pd

from buysignal.portfolio.instruments import INSTRUMENTS
from buysignal.telegram_bot.views import stamp, percent, _lines
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
            if item['cost'] > 0:
                # Against fee-inclusive cost, in the listing's own currency.
                detail += f' · {float(value / item["cost"] - 1):+.1%}'
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


def allocation_table(plan):
    """Value and share of each ETF in euros; with a mix set, its target and the gap to it."""
    mixed = plan['destination'] is not None
    rows = [('', 'EUR', 'Now', 'Mix', 'Gap') if mixed else ('', 'EUR', 'Now')]
    for row in plan['rows']:
        cells = (row['symbol'], f'{row["value"]:,.0f}', f'{row["weight"]:.0%}')
        rows.append(cells + (f'{row["target"]:.0%}', f'{row["gap"]:+,.0f}') if mixed else cells)
    rows.append(('Total', f'{plan["total"]:,.0f}'))
    return '<pre>' + esc('\n'.join(_lines(rows))) + '</pre>'


def entries_message(card, stale=()):
    """Each purchase against its month's average close, then the totals and the range chance gives."""
    if not card['rows']:
        return 'No purchases recorded yet. Use /buy or send “bought 10 VUAA @ 151.25”.'
    summary = card['summary']
    lines = ['<b>Your entries</b> · ' + (summary['verdict'] if summary else 'no finished month yet')]
    rows = [('Month', 'ETF', 'Paid', 'Mo avg', 'Diff')]
    running = False
    for row in card['rows'][-12:]:
        day = pd.Timestamp(row['date'])
        mark = '' if row['complete'] or row['diff'] is None else '*'
        running = running or bool(mark)
        rows.append((f'{day:%b %y}', row['symbol'], f'{row["price"]:,.2f}',
                     '—' if row['average'] is None else f'{row["average"]:,.2f}',
                     '—' if row['diff'] is None else f'{row["diff"]:+.1%}{mark}'))
    widths = [max(len(row[i]) for row in rows) for i in range(5)]
    table = [' '.join(cell.ljust(w) if i < 2 else cell.rjust(w) for i, (cell, w) in enumerate(zip(row, widths))).rstrip() for row in rows]
    if running:
        table.append('* month still running')
    totals = [('Buys', str(card['count']))]
    if summary:
        totals += [('Judged', str(summary['count'])), ('Mean diff', f'{summary["mean"]:+.2%}'),
                   ('Chance range', f'±{summary["chance"]:.2%}'),
                   ('Below avg', f'{summary["below"]} of {summary["count"]}')]
    if card['fee_share'] is not None:
        totals.append(('Fees', f'{card["fee_share"]:.2%}'))
    lines.append('<pre>' + esc('\n'.join([*table, '', *_lines(totals)])) + '</pre>')
    if stale:
        lines.append('⚠️ Prices from an older copy: ' + esc(', '.join(stale)))
    return '\n'.join(lines)


def cash_message(plan, overlaps=None):
    """Orders for the cash in hand: shares, cost and fees, then the same for fewer or more orders."""
    cash = plan['cash']
    chosen = plan['chosen']
    if chosen is None:
        return f'<b>Buy with {cash:,.0f} EUR</b>\nNot enough for one share of any ETF in the mix after fees.'
    by_symbol = {i.symbol: i for i in INSTRUMENTS}
    rows = [('', 'Qty', 'Price', 'EUR', 'Fee')]
    for order in chosen['orders']:
        instrument = by_symbol[order['symbol']]
        price = order['price_eur'] * plan['eurusd'] if instrument.currency == 'USD' else order['price_eur']
        rows.append((order['symbol'], str(order['quantity']), f'{price:,.2f}', f'{order["value"]:,.2f}',
                     f'{order["commission"] + order["conversion"]:.2f}'))
    totals = [('Spent', f'{chosen["spent"]:,.2f}'), ('Fees', f'{chosen["fees"]:.2f}', f'{chosen["fee_share"]:.2%}'),
              ('Left', f'{chosen["left"]:,.2f}')]
    converted = sum(order['conversion'] for order in chosen['orders'])
    if converted:
        totals.insert(2, ('of which FX', f'{converted:.2f}'))
    if plan['off_mix_now'] is not None:
        totals.append(('Off mix', f'{plan["off_mix_now"]:.1%} → {chosen["off_mix"]:.1%}'))
    else:
        totals.append(('Off mix', f'{chosen["off_mix"]:.1%}'))
    compare = [(' Orders', 'Fees', 'Off mix')]
    compare += [(('>' if option is chosen else ' ') + str(len(option['orders'])), f'{option["fee_share"]:.2%}', f'{option["off_mix"]:.1%}')
                for option in plan['options']]
    count = len(chosen['orders'])
    lines = [f'<b>Buy with {cash:,.0f} EUR</b> · {count} order{"s" if count > 1 else ""}',
             '<pre>' + esc('\n'.join([*_lines(rows), '', *_lines(totals), '', *_lines(compare)])) + '</pre>']
    overlaps = {symbol: window for symbol, window in (overlaps or {}).items() if window is not False}
    shut = [symbol for symbol, window in overlaps.items() if window is None]
    if shut:
        lines.append('⚠️ New York is closed today; wider quotes: ' + esc(', '.join(shut)))
    for window in sorted({window for window in overlaps.values() if window}):
        names = ', '.join(symbol for symbol, item in overlaps.items() if item == window)
        lines.append(f'New York open {stamp(window[0])[11:16]}–{stamp(window[1])[11:]} · {esc(names)}')
    return '\n'.join(lines)
