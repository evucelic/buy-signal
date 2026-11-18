"""Pre-close prices for the owner's listings and the /whattobuy orders."""
from buysignal.portfolio import instruments
from buysignal.telegram_bot.views import stamp, percent, _lines
from buysignal.telegram_bot.render import esc
from buysignal.signals.base import format_table


def quote_block(quotes, *, title='💹 Your listings'):
    by_symbol = {q['symbol']: q for q in quotes}
    rows, dates, cached, daily = [], {}, [], []
    for instrument in instruments.all():
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


def preclose_heading(sessions):
    closes = sorted(set(stamp(s.closes) for s in sessions))
    names = ' / '.join(dict.fromkeys(instruments.calendar_name(s.exchange) for s in sessions))
    return '<b>⏳ Pre-close update</b>\n' + esc(names + ' · regular close ' + ' / '.join(closes)) + '\n\n'


def orders_message(plan, *, source: str, overlaps=None, notes=()):
    """Whole-share orders for the cash: shares, cost and fees, the mix after, and the alternatives."""
    cash = plan['cash']
    title = f'🛒 <b>What to buy</b> · {esc(source)} €{cash:,.0f}'
    lines = [title]
    if plan['reserve']:
        lines.append(f'Cash target keeps €{plan["reserve"]:,.0f} · €{plan["spendable"]:,.0f} to invest')
    chosen = plan['chosen']
    if chosen is None:
        lines.append('Not enough for one share of any listing in the mix after fees.')
        lines += ['⚠️ ' + esc(note) for note in notes]
        return '\n'.join(lines)
    rows = [('', 'Qty', 'Price', 'EUR', 'Fee')]
    for order in chosen['orders']:
        rows.append((order['symbol'], str(order['quantity']), f'{order["price"]:,.2f}', f'{order["value"]:,.0f}',
                     f'{order["commission"] + order["conversion"]:.2f}'))
    totals = [('Spent', f'{chosen["spent"]:,.2f}'), ('Fees', f'{chosen["fees"]:.2f}', f'{chosen["fee_share"]:.2%}'),
              ('Left', f'{chosen["left"]:,.2f}')]
    converted = sum(order['conversion'] for order in chosen['orders'])
    if converted:
        totals.insert(2, ('of which FX', f'{converted:.2f}'))
    before = plan['off_mix_now']
    totals.append(('Off target', (f'{before:.1%} → ' if before is not None else '') + f'{chosen["off_mix"]:.1%}'))
    compare = [('Orders', 'Fees', 'Off')]
    compare += [(('▶ ' if option is chosen else '  ') + str(len(option['orders'])), f'{option["fee_share"]:.2%}', f'{option["off_mix"]:.1%}')
                for option in plan['options']]
    count = len(chosen['orders'])
    lines.append(f'<b>{count} order{"s" if count > 1 else ""}</b> · prices in each listing\'s currency')
    lines.append('<pre>' + esc('\n'.join([*_lines(rows), '', *_lines(totals), '', *_lines(compare)])) + '</pre>')
    lines.append('<i>Off target: the largest gap between a listing\'s share and its target. '
                 'Fewer orders cost less; ▶ is the closest mix with fees under '
                 f'{plan["max_fee_share"]:.1%}.</i>')
    overlaps = {symbol: window for symbol, window in (overlaps or {}).items() if window is not False}
    shut = [symbol for symbol, window in overlaps.items() if window is None]
    if shut:
        lines.append('⚠️ New York is closed today; wider quotes: ' + esc(', '.join(shut)))
    for window in sorted({window for window in overlaps.values() if window}):
        names = ', '.join(symbol for symbol, item in overlaps.items() if item == window)
        lines.append(f'🗽 New York open {stamp(window[0])[11:16]}–{stamp(window[1])[11:]} · {esc(names)}')
    lines += ['⚠️ ' + esc(note) for note in notes]
    return '\n'.join(lines)
