"""Reproduce delayed close bars and partial multi-index responses without losing prices."""
import json
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from buysignal.collectors import entry
from buysignal.signals import market_signal
from buysignal.signals.buy_signal import _alert_state
from buysignal.signals.base import SubSignal
from buysignal.telegram_bot import render
from buysignal.signals.buy_signal import BuySignal

NOW = pd.Timestamp('2026-11-05 02:02Z')  # Nov 4, 20:02 Chicago; user's end-of-day case


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(entry, '_utc_now', lambda: NOW)
    monkeypatch.setattr(entry.time, 'sleep', lambda seconds: None)


def history(end='2026-11-03'):
    dates = pd.bdate_range(end=end, periods=250)
    return pd.DataFrame({name: np.r_[np.full(249,100.),93.] for name in ('SPY','NASDAQ','DOW')},index=dates)


def save(path, frame):
    frame.to_csv(path)
    path.with_suffix('.meta.json').write_text(json.dumps({
        'full_refresh_date':'2026-11-04', 'bootstrap_scope':entry.MARKET_BOOTSTRAP_PERIOD,
        'optional_indexes':list(entry.OPTIONAL_INDEX_TICKERS), 'last_success':'2026-11-04T12:00:00Z',
    }))


def provider(frame):
    return pd.concat({'Close':frame.rename(columns={'NASDAQ':'^IXIC','DOW':'^DJI'})},axis=1)


def test_missing_close_is_repaired_with_a_single_symbol_request(tmp_path):
    path=tmp_path/'daily.csv'
    old=history(); save(path,old)
    fresh=pd.concat([old.tail(5),pd.DataFrame({'SPY':[94.],'NASDAQ':[95.],'DOW':[96.]},index=pd.to_datetime(['2026-11-04']))])
    batch=fresh.copy(); batch.loc[pd.Timestamp('2026-11-04'),'SPY']=np.nan
    def download(tickers, **kwargs):
        return provider(fresh[['SPY']] if tickers==['SPY'] else batch)
    with patch.object(entry.yf,'download',side_effect=download) as request:
        result=entry.get_entry_history(True,path,now=NOW)
    assert not result.attrs['stale']
    assert result.SPY.dropna().index[-1] == pd.Timestamp('2026-11-04')
    assert [c.args[0] for c in request.call_args_list] == [['SPY','^IXIC','^DJI','^STOXX50E'],['SPY']]


def test_nasdaq_outage_does_not_hide_fresh_spy_or_erase_nasdaq_cache(tmp_path):
    path=tmp_path/'daily.csv'; old=history(); save(path,old)
    fresh=pd.concat([old.tail(5),pd.DataFrame({'SPY':[94.],'NASDAQ':[np.nan],'DOW':[96.]},index=pd.to_datetime(['2026-11-04']))])
    fresh.NASDAQ=np.nan
    def download(tickers, **kwargs):
        return pd.DataFrame() if tickers==['^IXIC'] else provider(fresh)
    with patch.object(entry.yf,'download',side_effect=download):
        result=entry.get_entry_history(True,path,now=NOW)
    with patch.object(market_signal,'get_entry_history',return_value=result):
        signal=market_signal.score()
    assert signal.passes and not signal.metrics['stale']
    assert signal.metrics['index_changes']['NASDAQ']['date']=='2026-11-03'
    assert result.NASDAQ.dropna().iloc[-1] == 93.
    assert 'NASDAQ' in signal.table


def test_total_provider_failure_keeps_cached_numbers_and_does_not_pass_stale_gate(tmp_path):
    path=tmp_path/'daily.csv'; old=history(); save(path,old)
    original=path.read_bytes(); modified=path.stat().st_mtime_ns
    with patch.object(entry.yf,'download',side_effect=TimeoutError('provider unavailable')):
        result=entry.get_entry_history(True,path,now=NOW)
    assert path.read_bytes()==original and path.stat().st_mtime_ns==modified
    assert result.attrs['stale'] and result.attrs['refresh_failed']
    assert result.attrs['retrieved_at']=='2026-11-04T12:00:00Z'
    with patch.object(market_signal,'get_entry_history',return_value=result):
        signal=market_signal.score()
    assert signal.score==pytest.approx(-.07) and not signal.passes
    assert _alert_state([signal],[])[0]=='incomplete'
    text=render.signal_report(BuySignal(0,'incomplete',0,[signal]),[],NOW.to_pydatetime()).messages[0].text
    assert '-7.0%' in text and 'Prices cached through 2026-11-03' in text
    assert 'SPY Drawdown</b>: unknown' not in text and 'NASDAQ' in text


def test_retry_cooldown_prevents_multiple_full_attempts_during_outage(tmp_path):
    path=tmp_path/'daily.csv'; save(path,history())
    with patch.object(entry.yf,'download',side_effect=TimeoutError('offline')) as request:
        entry.get_entry_history(True,path,now=NOW)
        first=request.call_count
        entry.get_entry_history(True,path,now=NOW+pd.Timedelta(seconds=10))
        assert request.call_count==first
        entry.get_entry_history(True,path,now=NOW+pd.Timedelta(seconds=61))
        assert request.call_count>first


def test_single_bad_peer_value_does_not_reject_valid_spy():
    frame=history('2026-11-04'); frame.loc[frame.index[-1],'NASDAQ']=float('inf')
    result=entry._normalize(frame)
    assert result.SPY.iloc[-1]==93. and pd.isna(result.NASDAQ.iloc[-1])


def test_unknown_prices_do_not_clear_an_existing_watch(monkeypatch):
    from buysignal.telegram_bot import app
    monkeypatch.setattr(app,'_state',app._State(start_time=NOW.to_pydatetime(),alerting=True))
    monkeypatch.setattr(app.config,'DAILY_REPORT_HOUR_CT',-1)
    monkeypatch.setattr(app.runner,'macro_journal',lambda:type('Journal',(),{'unreported_failures':lambda self,now:[]})())
    report=patch.object(app.transport,'send_report')
    with report as sent:
        stale=SubSignal('market_dip',-.07,'flat','',metrics={'stale':True})
        app.handle_tick(BuySignal(0,'incomplete',0,[stale]),None,NOW.to_pydatetime())
    sent.assert_not_called()
    assert app._state.alerting
