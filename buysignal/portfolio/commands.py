"""The owner's settings by command: the target mix and the broker's fee schedule."""
import re

from buysignal.portfolio import instruments, plan
from buysignal.telegram_bot.render import esc

TARGET_USAGE = 'Use: /target VUAA 60 VWCG 20 AVWS 15 CASH 5 (percent of the account, total 100)'


def _mix(targets) -> str:
    return ' · '.join(f'{"Cash" if s == instruments.CASH else s} {w:.0%}' for s, w in targets.items()) if targets else 'not set'


def target(text, store):
    argument = text[len('/target'):]
    pairs = re.findall(r'([A-Za-z0-9.]+)\s+(\d+(?:\.\d+)?)', argument)
    if not pairs or re.sub(r'([A-Za-z0-9.]+)\s+(\d+(?:\.\d+)?)|\s|%', '', argument):
        held = ' '.join(i.symbol for i in instruments.all())
        return f'🎯 <b>Target mix</b>: {esc(_mix(store.targets()))}\nHeld: {esc(held)}\n{esc(TARGET_USAGE)}'
    try:
        current = store.set_targets(dict(pairs)) if len({s.upper() for s, _ in pairs}) == len(pairs) else None
    except ValueError as exc:
        return '⚠️ ' + esc(str(exc))
    if current is None:
        return '⚠️ Name each listing once.'
    return f'🎯 <b>Target mix set</b>: {esc(_mix(current))}\n/portfolio → Mix shows the gaps · /whattobuy the orders'


def fees(text, store):
    argument = text[len('/fees'):].strip()
    if argument.lower() in plan.IBKR:
        for instrument in instruments.all():
            minimum, rate = plan.IBKR[argument.lower()].get(instrument.currency, plan.IBKR[argument.lower()][plan.REPORT_CURRENCY])
            store.set_fee(instrument.symbol, minimum, rate * 100)
    elif argument:
        match = re.fullmatch(r'([A-Za-z0-9.]+)\s+(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)%?', argument)
        if not match:
            return ('Use: /fees tiered or /fees fixed (Interactive Brokers\' published rates)\n'
                    'Your own: /fees VUAA 1.70 0.05 (minimum per order, then percent of the order)\nCurrency conversion: /fees FX 0 0.03')
        try:
            store.set_fee(*match.groups())
        except ValueError as exc:
            return '⚠️ ' + esc(str(exc))
    chosen = store.fees()
    # What IBKR really took on the latest purchase of each listing, beside the schedule used to plan.
    paid = {}
    for trade in store.trades():
        if float(trade['quantity']) > 0 and trade.get('commission_currency', trade['currency']) == trade['currency']:
            paid[trade['symbol']] = float(trade['commission'])
    rows = []
    for instrument in instruments.all():
        minimum, rate = chosen.get(instrument.symbol) or plan.DEFAULT_FEES.get(instrument.currency, plan.DEFAULT_FEES[plan.REPORT_CURRENCY])
        rows.append(f'{instrument.symbol:<5}{minimum:>6.2f} {instrument.currency}{rate:>8.3%}'
                    + (f'{paid[instrument.symbol]:>6.2f}' if instrument.symbol in paid else ''))
    minimum, rate = chosen.get('FX') or plan.DEFAULT_FX
    rows.append(f'{"FX":<5}{minimum:>6.2f} EUR{rate:>8.3%}')
    unset = [name for name in [*(i.symbol for i in instruments.all()), 'FX'] if name not in chosen]
    note = '\nIBKR tiered, as published: ' + ', '.join(unset) if unset else ''
    return ('🧾 <b>Fees per order</b>\n<pre>' + esc('\n'.join(['      Minimum   Of order  Paid', *rows])) + '</pre>\n'
            + '<i>Paid: commission on your latest IBKR purchase.</i>' + esc(note))


def handle(text, store):
    lower = text.lower()
    if lower.startswith('/target'):
        return target(text, store)
    if lower.startswith('/fees'):
        return fees(text, store)
    return None
