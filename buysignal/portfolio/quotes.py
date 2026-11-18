"""Quotes and daily closes for the listings, benchmarks, EUR/USD and the TIPS yield, cached in the store."""
from datetime import datetime, timezone
import logging
import math
import threading

import pandas as pd
import yfinance as yf

from buysignal.config import LIVE_CACHE_TTL_SEC, YAHOO_TIMEOUT_SEC
from buysignal.portfolio import instruments

log = logging.getLogger(__name__)
# Where a listing's bars are stamped without a zone.
_ZONES = {'LSE': 'Europe/London', 'XETR': 'Europe/Berlin', 'XAMS': 'Europe/Amsterdam', 'XPAR': 'Europe/Paris',
          'XSWX': 'Europe/Zurich', 'XMIL': 'Europe/Rome', 'XNYS': 'America/New_York'}
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
            zone = _ZONES.get(instrument.exchange, 'UTC')
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
    for instrument in instruments.all():
        try:
            result.append(get(store, instrument, now=now, refresh=refresh))
        except Exception as exc:
            log.warning('Personal quote cache unavailable for %s (%s)', instrument.symbol, type(exc).__name__)
            result.append({'symbol': instrument.symbol, 'currency': instrument.currency, 'price': None})
    return result


_DIP_TTL_SEC = 3600  # daily closes: an hour-old reading is as current as the data


def _fetch_dip(instrument, now):
    """This listing's distance below its own 3-month high, in its trading currency."""
    from buysignal.signals.market_signal import analyze_series
    frame = yf.Ticker(instrument.yahoo).history(period='1y', interval='1d', auto_adjust=True, timeout=YAHOO_TIMEOUT_SEC)
    if frame is None or frame.empty or 'Close' not in frame:
        raise ValueError('No daily price history')
    closes = pd.to_numeric(frame.Close, errors='coerce').dropna()
    index = pd.to_datetime(closes.index)
    closes.index = pd.DatetimeIndex(index.tz_localize(None) if index.tz is not None else index).normalize()
    closes = closes[~closes.index.duplicated(keep='last')].sort_index()
    if closes.index[-1] > now.tz_localize(None).normalize() + pd.Timedelta(days=1):
        raise ValueError('Future price observation')
    facts = analyze_series(closes, instrument.symbol)
    return {'symbol': instrument.symbol, 'drawdown': facts['drawdown'], 'monthly': facts['monthly'],
            'observed_at': facts['observed_at'], 'peak_date': facts['peak_date'], 'retrieved_at': now.isoformat()}


def dips(store, *, now=None, refresh=True):
    """{symbol: dip facts} for the listings whose history is available; a failure omits only that one."""
    now = pd.Timestamp(now or datetime.now(timezone.utc))
    now = now.tz_localize('UTC') if now.tzinfo is None else now.tz_convert('UTC')
    result = {}
    for instrument in instruments.all():
        key = instrument.symbol + ':dip'
        cached = None
        try:
            with _LOCK:
                cached = store.quote(key)
                age = (now - pd.Timestamp(cached['retrieved_at'])).total_seconds() if cached else None
                if cached and (not refresh or 0 <= age < _DIP_TTL_SEC):
                    result[instrument.symbol] = cached
                    continue
                if not refresh:
                    continue
                fresh = _fetch_dip(instrument, now)
                store.save_quote(key, fresh)
                result[instrument.symbol] = fresh
        except Exception as exc:
            log.warning('Dip history failed for %s (%s)', instrument.yahoo, type(exc).__name__)
            if cached:
                result[instrument.symbol] = {**cached, 'refresh_failed': True}
    return result


_DAILY_TTL_SEC = 12 * 3600
_DAILY_PERIOD = '5y'


def _fetch_closes(yahoo, period):
    frame = yf.Ticker(yahoo).history(period=period, interval='1d', auto_adjust=False, timeout=YAHOO_TIMEOUT_SEC)
    if frame is None or frame.empty or 'Close' not in frame:
        raise ValueError('No daily price history')
    closes = pd.to_numeric(frame.Close, errors='coerce').dropna()
    if closes.empty or (closes <= 0).any():
        raise ValueError('Invalid price observations')
    index = pd.to_datetime(closes.index)
    closes.index = pd.DatetimeIndex(index.tz_localize(None) if index.tz is not None else index).normalize()
    return closes[~closes.index.duplicated(keep='last')].sort_index()


def _cached_series(store, key, yahoo, period, ttl, now, refresh):
    """(series, stale): unadjusted daily closes kept in the quote cache; the last good copy outlives a failed fetch."""
    with _LOCK:
        cached = store.quote(key)
        age = (now - pd.Timestamp(cached['retrieved_at'])).total_seconds() if cached else None
        if cached and (not refresh or 0 <= age < ttl):
            return _series(cached), False
        if not refresh:
            return None, False
        try:
            closes = _fetch_closes(yahoo, period)
            if closes.index[-1] > now.tz_localize(None).normalize() + pd.Timedelta(days=1):
                raise ValueError('Future price observation')
            payload = {'retrieved_at': now.isoformat(),
                       'closes': {day.strftime('%Y-%m-%d'): float(value) for day, value in closes.items()}}
            store.save_quote(key, payload)
            return closes, False
        except Exception as exc:
            log.warning('Daily history failed for %s (%s)', yahoo, type(exc).__name__)
            return (_series(cached), True) if cached else (None, True)


def _series(payload):
    series = pd.Series(payload['closes'], dtype=float)
    series.index = pd.to_datetime(series.index)
    return series.sort_index()


def _utc(now):
    now = pd.Timestamp(now or datetime.now(timezone.utc))
    return now.tz_localize('UTC') if now.tzinfo is None else now.tz_convert('UTC')


def daily_closes(store, *, now=None, refresh=True):
    """({symbol: closes in the trading currency}, [symbols served from an old copy])."""
    now, result, stale = _utc(now), {}, []
    for instrument in instruments.all():
        series, old = _cached_series(store, instrument.symbol + ':daily', instrument.yahoo, _DAILY_PERIOD, _DAILY_TTL_SEC, now, refresh)
        if series is not None:
            result[instrument.symbol] = series
        if old:
            stale.append(instrument.symbol)
    return result, stale


def session_closes(store, *, now=None):
    """{symbol: (close, date, currency)} from a download made now, for the checklist log.

    A cached bar may have been taken mid-session, so a failed download leaves the listing out.
    """
    now, result = _utc(now), {}
    for instrument in instruments.all():
        series, old = _cached_series(store, instrument.symbol + ':daily', instrument.yahoo, _DAILY_PERIOD, 0, now, True)
        if series is not None and not old:
            result[instrument.symbol] = (float(series.iloc[-1]), series.index[-1].date().isoformat(), instrument.currency)
    return result


def eurusd(store, *, now=None, refresh=True):
    """(dollars per euro, observation date, stale) from the latest daily close; (None, None, True) if never fetched."""
    series, old = _cached_series(store, 'EURUSD:fx', 'EURUSD=X', '5d', _DIP_TTL_SEC, _utc(now), refresh)
    if series is None:
        return None, None, True
    return float(series.iloc[-1]), series.index[-1].date().isoformat(), old


REAL_YIELD_SERIES = 'DFII10'  # 10-year US Treasury inflation-protected yield, daily, from FRED


def real_yield(store, *, now=None, refresh=True):
    """(yield as a fraction, observation date, stale): what a 10-year inflation-protected Treasury pays."""
    from buysignal.collectors.fred import fetch_series
    now = _utc(now)
    with _LOCK:
        cached = store.quote(REAL_YIELD_SERIES)
        age = (now - pd.Timestamp(cached['retrieved_at'])).total_seconds() if cached else None
        if cached and (not refresh or 0 <= age < _DAILY_TTL_SEC):
            return cached['value'], cached['date'], False
        if not refresh:
            return None, None, True
        try:
            latest = fetch_series(REAL_YIELD_SERIES).iloc[-1]
            if not -.05 < latest.value / 100 < .15:
                raise ValueError('Implausible real yield')
            payload = {'retrieved_at': now.isoformat(), 'value': float(latest.value) / 100, 'date': latest.date.date().isoformat()}
            store.save_quote(REAL_YIELD_SERIES, payload)
            return payload['value'], payload['date'], False
        except Exception as exc:
            log.warning('Real yield failed (%s)', type(exc).__name__)
            return (cached['value'], cached['date'], True) if cached else (None, None, True)


def eurusd_history(store, *, now=None, refresh=True):
    """(daily dollars per euro over five years, stale) for converting dollar closes day by day."""
    return _cached_series(store, 'EURUSD:daily', 'EURUSD=X', _DAILY_PERIOD, _DAILY_TTL_SEC, _utc(now), refresh)


# Accumulating UCITS ETFs quoted in euros on Xetra, so their closes are total returns for a euro investor.
BENCHMARKS = {'S&P 500': 'SXR8.DE', 'All-World': 'VWCE.DE'}


def benchmarks(store, *, now=None, refresh=True):
    """({name: daily closes in euros}, [names served from an old copy or missing])."""
    result, stale = {}, []
    for name, yahoo in BENCHMARKS.items():
        series, old = _cached_series(store, yahoo + ':bench', yahoo, _DAILY_PERIOD, _DAILY_TTL_SEC, _utc(now), refresh)
        if series is not None:
            result[name] = series
        if old or series is None:
            stale.append(name)
    return result, stale
