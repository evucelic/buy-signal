"""Durable exact-decimal purchase journal and pre-close delivery deduplication."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sqlite3

from buysignal import config
from buysignal.portfolio.instruments import resolve


def number(value, *, positive=True):
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError('Enter a valid number using a decimal point.') from None
    if not result.is_finite() or result < 0 or (positive and result == 0) or result > Decimal('1000000000000'):
        raise ValueError('Quantity and price must be positive finite numbers; fees cannot be negative.')
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
                CREATE TABLE IF NOT EXISTS purchases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL, quantity TEXT NOT NULL, price TEXT NOT NULL,
                    fees TEXT NOT NULL, currency TEXT NOT NULL, trade_date TEXT NOT NULL,
                    recorded_at TEXT NOT NULL, source_key TEXT UNIQUE, voided_at TEXT
                );
                CREATE TABLE IF NOT EXISTS quotes (symbol TEXT PRIMARY KEY, payload TEXT NOT NULL);
            ''')

    @contextmanager
    def connection(self):
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            with db:
                yield db

    def record(self, symbol, quantity, price, trade_date, *, fees='0', currency=None, source_key=None):
        instrument = resolve(symbol)
        quantity, price, fees = number(quantity), number(price), number(fees, positive=False)
        if currency and currency.upper() != instrument.currency:
            raise ValueError(f'{instrument.symbol} on {instrument.exchange} is recorded in {instrument.currency}.')
        with self.connection() as db:
            # The same Telegram message can be redelivered after a restart or outage.
            db.execute('''INSERT INTO purchases
                (symbol,quantity,price,fees,currency,trade_date,recorded_at,source_key)
                VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(source_key) DO NOTHING''', (instrument.symbol, str(quantity), str(price), str(fees),
                instrument.currency, trade_date.isoformat(), datetime.now(timezone.utc).isoformat(), source_key))
            if source_key is not None:
                row = db.execute('SELECT * FROM purchases WHERE source_key=?', (source_key,)).fetchone()
            else:
                row = db.execute('SELECT * FROM purchases WHERE id=last_insert_rowid()').fetchone()
            return dict(row)

    def purchases(self, limit=30):
        with self.connection() as db:
            return [dict(row) for row in db.execute('SELECT * FROM purchases ORDER BY trade_date DESC,id DESC LIMIT ?', (limit,))]

    def holdings(self):
        with self.connection() as db:
            rows = db.execute('SELECT * FROM purchases WHERE voided_at IS NULL ORDER BY trade_date,id').fetchall()
        result = {}
        for row in rows:
            item = result.setdefault(row['symbol'], {'quantity': Decimal(0), 'cost': Decimal(0), 'currency': row['currency']})
            item['quantity'] += Decimal(row['quantity'])
            item['cost'] += Decimal(row['quantity']) * Decimal(row['price']) + Decimal(row['fees'])
        for item in result.values():
            item['average'] = item['cost'] / item['quantity']
        return result

    def void(self, trade_id):
        with self.connection() as db:
            row = db.execute('SELECT * FROM purchases WHERE id=?', (trade_id,)).fetchone()
            if row is None:
                raise ValueError('Purchase ID not found. See /trades.')
            db.execute('UPDATE purchases SET voided_at=COALESCE(voided_at,?) WHERE id=?',
                       (datetime.now(timezone.utc).isoformat(), trade_id))
        return dict(row)

    def quote(self, symbol):
        with self.connection() as db:
            row = db.execute('SELECT payload FROM quotes WHERE symbol=?', (symbol,)).fetchone()
            return json.loads(row[0]) if row else None

    def save_quote(self, symbol, payload):
        with self.connection() as db:
            db.execute('INSERT OR REPLACE INTO quotes VALUES (?,?)', (symbol, json.dumps(payload, allow_nan=False)))
