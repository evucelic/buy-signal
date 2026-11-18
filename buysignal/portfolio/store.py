"""The owner's account as last read from Interactive Brokers, plus their own settings.

Positions and cash are replaced as a whole by each validated statement; trades, daily net asset
values and deposits accumulate, so history outlives IBKR's 365-day statement limit. The bot only
reads the account: nothing here can reach the broker. Settings (target mix, fee schedule) and the
quote cache live in the same SQLite file.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sqlite3

from buysignal import config
from buysignal.portfolio import instruments

def number(value, *, positive=True):
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError('Enter a valid number using a decimal point.') from None
    if not result.is_finite() or result < 0 or (positive and result == 0) or result > Decimal('1000000000000'):
        raise ValueError('Use a positive finite number.')
    if result.as_tuple().exponent < -12:
        raise ValueError('Use at most 12 decimal places.')
    return result


class PortfolioStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else config.DATA_DIR / 'portfolio.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS quotes (symbol TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS notifications (
                    exchange TEXT NOT NULL, session_date TEXT NOT NULL, sent_at TEXT NOT NULL,
                    PRIMARY KEY (exchange,session_date)
                );
                CREATE TABLE IF NOT EXISTS targets (symbol TEXT PRIMARY KEY, weight TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS fees (name TEXT PRIMARY KEY, minimum TEXT NOT NULL, percent TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS positions (symbol TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cash (currency TEXT PRIMARY KEY, amount TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS trades (trade_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS nav (day TEXT PRIMARY KEY, cash TEXT NOT NULL, total TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS deposits (day TEXT PRIMARY KEY, amount TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS account (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            ''')
        self._publish_instruments()

    @contextmanager
    def connection(self):
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            with db:
                yield db

    # --- the account, from IBKR -----------------------------------------------------------------

    def save_statement(self, statement, now: datetime) -> None:
        """Replace positions and cash with the statement's, add its history; all or nothing.

        A statement older than the one stored is refused, so a delayed or repeated download can
        never roll the holdings back.
        """
        stored = self.account().get('statement_to')
        if stored and statement.to_date < date.fromisoformat(stored):
            raise ValueError(f'Statement through {statement.to_date} is older than the stored {stored}')
        with self.connection() as db:
            db.execute('DELETE FROM positions')
            db.executemany('INSERT INTO positions VALUES (?,?)', [(p.symbol, json.dumps({
                'symbol': p.symbol, 'name': p.description, 'currency': p.currency, 'exchange': p.exchange,
                'isin': p.isin, 'quantity': str(p.quantity), 'mark_price': str(p.mark_price),
                'value': str(p.value), 'cost': str(p.cost), 'report_date': p.report_date.isoformat(),
            })) for p in statement.positions if p.quantity])
            db.execute('DELETE FROM cash')
            db.executemany('INSERT INTO cash VALUES (?,?)', [(c, str(a)) for c, a in statement.cash.items()])
            db.executemany('INSERT OR REPLACE INTO trades VALUES (?,?)', [(t.trade_id, json.dumps({
                'trade_id': t.trade_id, 'symbol': t.symbol, 'currency': t.currency, 'exchange': t.exchange,
                'trade_date': t.trade_date.isoformat(), 'quantity': str(t.quantity), 'price': str(t.price),
                'commission': str(t.commission), 'commission_currency': t.commission_currency,
            })) for t in statement.trades if t.trade_id])
            db.executemany('INSERT OR REPLACE INTO nav VALUES (?,?,?)',
                           [(d.isoformat(), str(c), str(t)) for d, c, t in statement.nav])
            # Deposits are replaced within the statement's period, where it is complete.
            db.execute('DELETE FROM deposits WHERE day BETWEEN ? AND ?', (statement.from_date.isoformat(), statement.to_date.isoformat()))
            db.executemany('INSERT OR REPLACE INTO deposits VALUES (?,?)', [(d.isoformat(), str(a)) for d, a in statement.deposits])
            db.executemany('INSERT OR REPLACE INTO account VALUES (?,?)', [
                ('base_currency', statement.base_currency), ('statement_to', statement.to_date.isoformat()),
                ('generated_at', statement.generated_at.isoformat()), ('synced_at', now.astimezone(timezone.utc).isoformat()),
            ])
        self._publish_instruments()

    def account(self) -> dict:
        """base_currency, statement_to, generated_at and synced_at of the stored statement; {} before the first."""
        with self.connection() as db:
            return {row['key']: row['value'] for row in db.execute('SELECT * FROM account')}

    def positions(self) -> dict:
        with self.connection() as db:
            rows = [json.loads(row['payload']) for row in db.execute('SELECT payload FROM positions ORDER BY symbol')]
        return {row['symbol']: row for row in rows}

    def holdings(self) -> dict:
        """{symbol: {quantity, cost, average, currency}} with Decimal amounts in the listing currency."""
        result = {}
        for symbol, row in self.positions().items():
            quantity, cost = Decimal(row['quantity']), Decimal(row['cost'])
            result[symbol] = {'quantity': quantity, 'cost': cost, 'average': cost / quantity if quantity else Decimal(0),
                              'currency': row['currency']}
        return result

    def cash(self) -> dict[str, Decimal]:
        with self.connection() as db:
            return {row['currency']: Decimal(row['amount']) for row in db.execute('SELECT * FROM cash ORDER BY currency')}

    def trades(self) -> list[dict]:
        """Every stored execution, oldest first, amounts as strings."""
        with self.connection() as db:
            rows = [json.loads(row['payload']) for row in db.execute('SELECT payload FROM trades')]
        return sorted(rows, key=lambda row: (row['trade_date'], row['trade_id']))

    def nav(self) -> list[tuple[date, float, float]]:
        """(day, cash, total) in the base currency, oldest first."""
        with self.connection() as db:
            return [(date.fromisoformat(row['day']), float(row['cash']), float(row['total']))
                    for row in db.execute('SELECT * FROM nav ORDER BY day')]

    def deposits(self) -> list[tuple[date, float]]:
        with self.connection() as db:
            return [(date.fromisoformat(row['day']), float(row['amount'])) for row in db.execute('SELECT * FROM deposits ORDER BY day')]

    def _publish_instruments(self) -> None:
        """Held listings become the instruments in use; with no statement yet, the defaults stay."""
        positions = self.positions()
        if not positions:
            instruments.set_current(None)
            return
        instruments.set_current(instruments.from_listing(p['symbol'], p['exchange'], p['currency'], p['name'])
                                for p in positions.values())

    # --- the owner's settings -------------------------------------------------------------------

    def targets(self):
        """The chosen mix as {symbol or CASH: share of everything in the account}; empty until /target sets one."""
        with self.connection() as db:
            return {row['symbol']: float(Decimal(row['weight']) / 100) for row in db.execute('SELECT * FROM targets')}

    def set_targets(self, weights):
        """Replace the mix. Weights are percentages of held listings or CASH and must total 100."""
        cleaned = {}
        for symbol, weight in weights.items():
            name = instruments.CASH if symbol.upper() == instruments.CASH else instruments.resolve(symbol).symbol
            if name in cleaned:
                raise ValueError('Name each listing once.')
            cleaned[name] = number(weight, positive=False)
        if sum(cleaned.values()) != 100:
            raise ValueError(f'Weights must total 100; these total {sum(cleaned.values()):f}.')
        with self.connection() as db:
            db.execute('DELETE FROM targets')
            db.executemany('INSERT INTO targets VALUES (?,?)', [(s, str(w)) for s, w in cleaned.items() if w > 0])
        return self.targets()

    def fees(self):
        """{listing symbol or 'FX': (minimum per order, share of the order value)} as set by /fees."""
        with self.connection() as db:
            return {row['name']: (float(row['minimum']), float(Decimal(row['percent']) / 100)) for row in db.execute('SELECT * FROM fees')}

    def set_fee(self, name, minimum, percent):
        name = 'FX' if name.upper() == 'FX' else instruments.resolve(name).symbol
        minimum, percent = number(minimum, positive=False), number(percent, positive=False)
        if percent > 5:
            raise ValueError('The percentage is per order, for example 0.05.')
        with self.connection() as db:
            db.execute('INSERT OR REPLACE INTO fees VALUES (?,?,?)', (name, str(minimum), str(percent)))
        return name

    # --- caches -----------------------------------------------------------------------------------

    def quote(self, symbol):
        with self.connection() as db:
            row = db.execute('SELECT payload FROM quotes WHERE symbol=?', (symbol,)).fetchone()
            return json.loads(row[0]) if row else None

    def save_quote(self, symbol, payload):
        with self.connection() as db:
            db.execute('INSERT OR REPLACE INTO quotes VALUES (?,?)', (symbol, json.dumps(payload, allow_nan=False)))

    def delivered(self, exchange, session_date):
        with self.connection() as db:
            return db.execute('SELECT 1 FROM notifications WHERE exchange=? AND session_date=?',
                              (exchange, session_date)).fetchone() is not None

    def mark_delivered(self, sessions, now):
        with self.connection() as db:
            db.executemany('INSERT OR IGNORE INTO notifications VALUES (?,?,?)',
                           [(s.exchange, str(s.session_date), now.isoformat()) for s in sessions])
