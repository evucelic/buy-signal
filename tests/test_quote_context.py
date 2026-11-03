"""Optional index coverage and VIX high-history cache migration."""

import json
from unittest.mock import patch

import numpy as np
import pandas as pd

from buysignal.collectors import entry, vix


def test_optional_european_calendar_does_not_invalidate_us_observations(tmp_path):
    dates = pd.bdate_range(end='2026-11-03', periods=250)
    frame = pd.DataFrame({name: np.linspace(80, 100, 250) for name in ('SPY','^IXIC','^DJI','^STOXX50E')}, index=dates)
    frame.loc[dates[-4], ['SPY','^IXIC','^DJI']] = np.nan
    raw = pd.concat({'Close': frame}, axis=1)
    with patch.object(entry.yf, 'download', return_value=raw), patch.object(entry.time, 'sleep'):
        assert entry.refresh_entry_history(tmp_path / 'prices.csv')
    saved = entry.entry_history(tmp_path / 'prices.csv')
    assert 'EURO50' in saved
    assert pd.isna(saved.loc[dates[-4], 'SPY'])

def test_invalid_optional_prices_do_not_disable_us_data():
    dates = pd.bdate_range(end='2026-11-03', periods=250)
    frame = pd.DataFrame({name: np.linspace(80, 100, 250) for name in ('SPY','NASDAQ','DOW','EURO50')}, index=dates)
    frame.loc[dates[-1], 'EURO50'] = -1
    result = entry._normalize(frame)
    assert 'EURO50' not in result and 'SPY' in result

def test_downloaded_vix_highs_survive_cache_merge(tmp_path):
    path = tmp_path / 'vix.csv'
    index = pd.date_range('2026-11-02 15:00', periods=3, freq='h', tz='UTC')
    raw = pd.DataFrame({'Close':[20.,21.,22.], 'High':[22.,25.,24.]}, index=index)
    with patch.object(vix.yf, 'download', return_value=raw), patch.object(vix.time, 'sleep'):
        assert vix.refresh_vix_cache(path)
    daily = vix.daily_history(path)
    assert daily.vix.iloc[-1] == 22.
    assert daily.high.iloc[-1] == 25.

def test_existing_cache_acquires_european_history_once_without_repeated_full_downloads(tmp_path):
    path = tmp_path / 'prices.csv'
    dates = pd.bdate_range(end='2026-11-03', periods=250)
    old = pd.DataFrame({name: np.linspace(80,100,250) for name in ('SPY','NASDAQ','DOW')}, index=dates)
    old.to_csv(path)
    path.with_suffix('.meta.json').write_text(json.dumps({'full_refresh_date': pd.Timestamp.now(tz='America/New_York').date().isoformat(), 'bootstrap_scope': entry.MARKET_BOOTSTRAP_PERIOD}))
    new = old.rename(columns={'NASDAQ':'^IXIC','DOW':'^DJI'}).assign(**{'^STOXX50E': np.linspace(4000,5000,250)})
    raw = pd.concat({'Close':new}, axis=1)
    with patch.object(entry.yf, 'download', return_value=raw) as download, patch.object(entry.time, 'sleep'):
        assert entry.refresh_entry_history(path)
        assert entry.refresh_entry_history(path)
    assert [c.kwargs['period'] for c in download.call_args_list] == [entry.MARKET_DAILY_PERIOD, '5d']
    assert len(entry.entry_history(path).EURO50.dropna()) == 250
