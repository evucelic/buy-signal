"""Exact manual purchase records, source isolation and the user's own venue deadlines."""
from datetime import datetime, timezone, date
from decimal import Decimal
from unittest.mock import patch, Mock

import pandas as pd
import pytest

from buysignal.portfolio import commands, quotes
from buysignal.portfolio.instruments import resolve
from buysignal.portfolio.journal import PortfolioStore
from buysignal.telegram_bot import app, transport

NOW = datetime(2026,11,5,15,30,tzinfo=timezone.utc)  # 16:30 Zagreb


@pytest.fixture
def store(tmp_path):
    return PortfolioStore(tmp_path/'portfolio.sqlite3')


def test_confirmed_listing_currencies_are_not_fund_base_currency():
    assert resolve('VUAA').yahoo=='VUAA.L' and resolve('VUAA').currency=='USD'
    assert resolve('VWCG').yahoo=='VWCG.DE' and resolve('VWCG').currency=='EUR'
    assert resolve('AVWS').yahoo=='AVWS.DE' and resolve('AVWS').currency=='EUR'
    with pytest.raises(ValueError):resolve('VUAA.DE')


def test_natural_purchase_defaults_to_zagreb_date_and_preserves_exact_values(store,monkeypatch):
    monkeypatch.setenv('TELEGRAM_TIMEZONE','Europe/Zagreb')
    at=datetime(2026,11,5,23,30,tzinfo=timezone.utc)
    text=commands.handle('bought 10.5 VUAA @ 151.25 fees 1.00',store,now=at,source_key='m1')
    assert '2026-11-06' in text and '1,589.125' not in text
    row=store.purchases()[0]
    assert row['quantity']=='10.5' and row['price']=='151.25' and row['fees']=='1.00'
    holding=store.holdings()['VUAA']
    assert holding['cost']==Decimal('1589.125')
    assert holding['currency']=='USD'


def test_explicit_backdated_purchase_and_fractional_units(store):
    result=commands.handle('/buy 0.1 AVWS @ 52.13 EUR on 2026-10-01 fees 0.01',store,now=NOW)
    assert '2026-10-01' in result and 'AVWS' in result
    assert store.holdings()['AVWS']['cost']==Decimal('5.223')


@pytest.mark.parametrize('text',[
    'bought 10 VUAA @ price', 'bought 10 VUAA @ 100 EUR', 'bought -1 VUAA @ 100',
    'bought 1 VUAA @ nan', 'bought 10 VUAA @ 100 on 2027-01-01',
    'bought 10 VUAA @ 100 on 2026-02-30', 'bought 1,000 VUAA @ 100',
    'bought 10 UNKNOWN @ 100',
])
def test_ambiguous_or_invalid_purchase_never_writes(text,store):
    commands.handle(text,store,now=NOW)
    assert not store.purchases()


def test_replayed_telegram_message_is_idempotent_but_separate_buys_are_allowed(store):
    for _ in range(2):commands.handle('bought 10 VUAA @ 100',store,now=NOW,source_key='telegram:1:42')
    assert store.holdings()['VUAA']['quantity']==10
    commands.handle('bought 10 VUAA @ 100',store,now=NOW,source_key='telegram:1:43')
    assert store.holdings()['VUAA']['quantity']==20


def test_currency_totals_stay_separate_and_voids_remain_in_audit_log(store):
    commands.handle('bought 10 VUAA @ 100 USD',store,now=NOW)
    commands.handle('bought 2 VWCG @ 50 EUR',store,now=NOW)
    text=commands.handle('/holdings',store)
    assert '1,000.00 USD' in text and '100.00 EUR' in text
    row=store.purchases()[0]
    commands.handle('/voidtrade '+str(row['id']),store)
    assert 'VWCG' not in store.holdings()
    assert 'VOID' in commands.handle('/trades',store)
    assert len(PortfolioStore(store.path).purchases())==2


def test_average_cost_includes_fees_without_rounding_each_purchase(store):
    commands.handle('bought 1 VUAA @ 10 fees 1',store,now=NOW)
    commands.handle('bought 3 VUAA @ 20 fees 2',store,now=NOW)
    assert store.holdings()['VUAA']['average']==Decimal('18.25')


def test_failed_personal_quote_preserves_previous_price_without_advancing_source_time(store,monkeypatch):
    item=resolve('VUAA')
    quote={'symbol':'VUAA','currency':'USD','price':150.,'change':.01,'interval':'5m',
           'observed_at':'2026-11-05T15:00Z','retrieved_at':'2026-11-05T15:01Z','last_attempt':'2026-11-05T15:01Z'}
    store.save_quote('VUAA',quote)
    monkeypatch.setattr(quotes,'_fetch',lambda *a: (_ for _ in ()).throw(TimeoutError()))
    result=quotes.get(store,item,now=NOW)
    assert result['price']==150 and result['refresh_failed']
    assert result['observed_at']==quote['observed_at'] and result['retrieved_at']==quote['retrieved_at']


def test_personal_quote_cache_prevents_repeat_calls(store,monkeypatch):
    calls=[]
    def fetch(item,now):
        calls.append(item.symbol)
        return {'symbol':item.symbol,'price':150.,'currency':item.currency,'retrieved_at':now.isoformat(),'last_attempt':now.isoformat()}
    monkeypatch.setattr(quotes,'_fetch',fetch)
    quotes.get(store,resolve('VUAA'),now=NOW)
    quotes.get(store,resolve('VUAA'),now=NOW)
    assert calls==['VUAA']


def test_provider_currency_mismatch_is_not_silently_converted():
    ticker=Mock(); ticker.history.return_value=pd.DataFrame({'Close':[10.,11.]},index=pd.date_range('2026-11-05T14:00Z',periods=2,freq='5min'))
    ticker.history_metadata={'currency':'GBP'}
    with patch.object(quotes.yf,'Ticker',return_value=ticker):
        with pytest.raises(ValueError,match='currency'):
            quotes._fetch(resolve('VUAA'),pd.Timestamp(NOW))


def test_app_accepts_purchase_with_telegram_idempotency_key(store,monkeypatch):
    monkeypatch.setattr(app,'_portfolio_store',store)
    monkeypatch.setattr(transport._message_context,'source_key','telegram:1:99',raising=False)
    app.handle_message('bought 10 VUAA @ 100')
    app.handle_message('bought 10 VUAA @ 100')
    assert store.holdings()['VUAA']['quantity']==10
    assert 'USD' in app.handle_message('/holdings')


def test_one_broken_quote_cache_does_not_block_other_listings(store,monkeypatch):
    def get(store,item,**kw):
        if item.symbol=='VWCG':raise ValueError('damaged cache')
        return {'symbol':item.symbol,'currency':item.currency,'price':100.}
    monkeypatch.setattr(quotes,'get',get)
    data=quotes.all_quotes(store,now=NOW)
    assert [q['symbol'] for q in data if q['price'] is not None]==['VUAA','AVWS']


def test_personal_quote_table_stays_narrow_and_marks_cached_source_dates():
    import re
    from buysignal.portfolio.presentation import quote_block
    quotes_data=[{'symbol':'VUAA','price':151.25,'currency':'USD','change':-.1,'observed_at':'2026-11-05T15:25Z','interval':'5m'},
                 {'symbol':'VWCG','price':61.05,'currency':'EUR','change':.01,'observed_at':'2026-11-04T15:25Z','interval':'5m','refresh_failed':True}]
    text=quote_block(quotes_data)
    table=re.search('<pre>(.*?)</pre>',text,re.S).group(1)
    assert max(map(len,table.splitlines()))<=33
    assert '* Cached: VWCG' in text and 'VWCG 2026-11-04' in text
    assert '151.25 USD' in text
