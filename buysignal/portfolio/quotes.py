"""Lightweight quotes for the confirmed exchange listings, cached separately from signals."""
from datetime import datetime, timezone
import logging
import math
import threading

import pandas as pd
import yfinance as yf

from buysignal.config import LIVE_CACHE_TTL_SEC, YAHOO_TIMEOUT_SEC
from buysignal.portfolio.instruments import INSTRUMENTS

log = logging.getLogger(__name__)
_LOCK = threading.Lock()


def _fetch(instrument, now):
    ticker = yf.Ticker(instrument.yahoo)
    last_error = None
    for period, interval in (('5d', '5m'), ('1mo', '1d')):
        try:
            frame = ticker.history(period=period, interval=interval, auto_adjust=False,
                                   prepost=False, timeout=YAHOO_TIMEOUT_SEC)
            if frame is None or frame.empty or 'Close' not in frame:
                raise ValueError('No price observations')
            closes = pd.to_numeric(frame.Close, errors='coerce').dropna()
            if closes.empty or not closes.map(math.isfinite).all() or (closes <= 0).any():
                raise ValueError('Invalid price observations')
            index = pd.to_datetime(closes.index)
            zone = 'Europe/London' if instrument.exchange == 'LSE' else 'Europe/Berlin'
            if index.tz is None:
                index = index.tz_localize(zone)
            closes.index = index.tz_convert('UTC')
            closes = closes[~closes.index.duplicated(keep='last')].sort_index()
            observed = closes.index[-1]
            if observed > now + pd.Timedelta(minutes=5):
                raise ValueError('Future price observation')
            metadata = ticker.history_metadata or {}
            currency = metadata.get('currency')
            if currency and currency != instrument.currency:
                raise ValueError(f'Listing currency {currency} differs from {instrument.currency}')
            local_dates = closes.index.tz_convert(zone).date
            prior = closes.loc[local_dates < local_dates[-1]]
            latest = float(closes.iloc[-1])
            change = latest / float(prior.iloc[-1]) - 1 if not prior.empty else None
            return {'symbol': instrument.symbol, 'currency': instrument.currency, 'price': latest,
                    'change': change, 'observed_at': observed.isoformat(), 'interval': interval,
                    'retrieved_at': now.isoformat(), 'last_attempt': now.isoformat(), 'refresh_failed': False}
        except Exception as exc:
            last_error = exc
    raise ValueError(f'{type(last_error).__name__}: {last_error}')


def get(store, instrument, *, now=None, refresh=True):
    now = pd.Timestamp(now or datetime.now(timezone.utc))
    now = now.tz_localize('UTC') if now.tzinfo is None else now.tz_convert('UTC')
    with _LOCK:
        cached = store.quote(instrument.symbol)
        if cached:
            age = (now - pd.Timestamp(cached['retrieved_at'])).total_seconds() if cached.get('retrieved_at') else None
            attempt_age = (now - pd.Timestamp(cached['last_attempt'])).total_seconds()
            if not refresh or (age is not None and 0 <= age < LIVE_CACHE_TTL_SEC) or 0 <= attempt_age < 60:
                return cached
        elif not refresh:
            return {'symbol': instrument.symbol, 'currency': instrument.currency, 'price': None}
        try:
            result = _fetch(instrument, now)
        except Exception as exc:
            log.warning('Personal quote failed for %s (%s)', instrument.yahoo, type(exc).__name__)
            result = dict(cached or {'symbol': instrument.symbol, 'currency': instrument.currency, 'price': None})
            result.update(last_attempt=now.isoformat(), refresh_failed=True)
        store.save_quote(instrument.symbol, result)
        return result


def all_quotes(store, *, now=None, refresh=True):
    result = []
    for instrument in INSTRUMENTS:
        try:
            result.append(get(store, instrument, now=now, refresh=refresh))
        except Exception as exc:
            log.warning('Personal quote cache unavailable for %s (%s)', instrument.symbol, type(exc).__name__)
            result.append({'symbol': instrument.symbol, 'currency': instrument.currency, 'price': None})
    return result
