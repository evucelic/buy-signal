"""Calendar-aware personal alerts, delivery deduplication and normal-bot coexistence."""
from datetime import datetime, timezone
from unittest.mock import Mock
import pytest
from buysignal.portfolio import sessions
from buysignal.portfolio.journal import PortfolioStore
from buysignal.portfolio.service import PrecloseService
from buysignal.telegram_bot import app, transport
from buysignal.telegram_bot.messages import Report, ReportMessage

NOW = datetime(2026,11,5,15,30,tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    return PortfolioStore(tmp_path/'portfolio.sqlite3')


def test_normal_day_is_one_hour_before_both_regular_closes(store):
    due=sessions.due(NOW,store)
    assert {s.exchange for s in due}=={'LSE','XETR'}
    assert all(s.closes==datetime(2026,11,5,16,30,tzinfo=timezone.utc) for s in due)
    assert not sessions.due(datetime(2026,11,5,15,29,tzinfo=timezone.utc),store)
    assert not sessions.due(datetime(2026,11,5,16,30,tzinfo=timezone.utc),store)

def test_early_close_and_distinct_holidays_follow_exchange_calendars(store):
    christmas=sessions.due(datetime(2026,12,24,11,30,tzinfo=timezone.utc),store)
    assert [s.exchange for s in christmas]==['LSE']
    assert christmas[0].closes.hour==12
    # London summer bank holiday: Xetra remains open.
    holiday=sessions.due(datetime(2026,8,31,14,30,tzinfo=timezone.utc),store)
    assert [s.exchange for s in holiday]==['XETR']
    assert not sessions.due(datetime(2026,11,7,15,30,tzinfo=timezone.utc),store)

def test_summer_time_and_us_dst_mismatch_do_not_shift_european_close(store):
    due=sessions.due(datetime(2026,3,20,15,30,tzinfo=timezone.utc),store)
    assert len(due)==2
    summer=sessions.due(datetime(2026,7,17,14,30,tzinfo=timezone.utc),store)
    assert len(summer)==2 and all(s.closes.hour==15 for s in summer)

def test_delivery_retries_and_survives_restart_without_duplicate_ping(store):
    attempts=[]
    service=PrecloseService(store,lambda group,now: attempts.append(group) and False)
    assert not service.check(NOW)
    assert len(sessions.due(NOW,store))==2
    service.deliver=lambda group,now: attempts.append(group) or True
    assert service.check(NOW)
    again=PrecloseService(PortfolioStore(store.path),lambda *a:pytest.fail('Duplicate pre-close'))
    assert not again.check(NOW)
    assert len(attempts)==2

def test_preclose_combines_personal_quotes_and_global_overview_once(store,monkeypatch):
    from buysignal.signals.buy_signal import BuySignal
    monkeypatch.setattr(app,'_portfolio_store',store)
    monkeypatch.setattr(app,'_utc_now',lambda:NOW)
    monkeypatch.setattr(app,'_signal_report',lambda result,heading='',prefix='': Report([ReportMessage(heading+prefix+'GLOBAL OVERVIEW')]))
    monkeypatch.setattr(app.personal_quotes,'all_quotes',lambda *a,**kw:[{'symbol':'VUAA','price':150.,'change':.01,'currency':'USD','interval':'5m','observed_at':'2026-11-05T15:25Z'}])
    sent=[]; monkeypatch.setattr(app.transport,'send_report',lambda report:sent.append(report) or True)
    service=PrecloseService(store,lambda group,now:app._deliver_report(BuySignal(0,'none',0,[]),now=now,preclose=group))
    assert service.check(NOW)
    assert not service.check(NOW)
    assert len(sent)==1
    text=sent[0].messages[0].text
    assert 'London' in text and 'IBIS2/Xetra' in text and '17:30 CET' in text
    assert 'VUAA' in text and 'GLOBAL OVERVIEW' in text

def test_regular_watch_during_preclose_window_covers_the_scheduled_ping(store,monkeypatch):
    from buysignal.signals.buy_signal import BuySignal
    monkeypatch.setattr(app,'_portfolio_store',store)
    monkeypatch.setattr(app,'_personal_service',Mock())
    monkeypatch.setattr(app,'_utc_now',lambda:NOW)
    monkeypatch.setattr(app,'_signal_report',lambda *a: Report([ReportMessage('combined')]))
    monkeypatch.setattr(app.personal_quotes,'all_quotes',lambda *a,**kw:[])
    monkeypatch.setattr(app.transport,'send_report',lambda report:True)
    assert app._deliver_report(BuySignal(0,'none',0,[]),'Watch trigger',now=NOW)
    assert not sessions.due(NOW,store)

def test_personal_service_startup_failure_does_not_stop_existing_bot(monkeypatch):
    started=[]; notices=[]
    monkeypatch.setattr(transport,'start',lambda *a:started.append(True))
    monkeypatch.setattr(transport,'notice',notices.append)
    monkeypatch.setattr(app,'_start_personal',lambda: (_ for _ in ()).throw(OSError('storage unavailable')))
    app.start()
    assert started==[True] and 'Existing market reports remain active' in notices[0]

def test_calendar_clock_sleeps_while_quiet_but_wakes_at_deadline(store):
    from datetime import timedelta
    assert sessions.next_delay(NOW-timedelta(seconds=30),store)==30
    assert sessions.next_delay(NOW-timedelta(hours=5),store)>60
    assert sessions.next_delay(NOW,store)==60  # retry a due but undelivered ping
    store.mark_delivered(sessions.due(NOW,store),NOW)
    assert sessions.next_delay(NOW,store)>60
