"""Prepared navigation does no analytical work and recovers Telegram media failures."""
import threading
from unittest.mock import Mock, patch

import pytest
import requests

from buysignal.telegram_bot import interactive, transport, prepared
from buysignal.telegram_bot.snapshots import SnapshotStore
from scripts.preview_entry_report import sample_assessment, NOW


def response(result=None,status=200,description=''):
    value=Mock(status_code=status)
    value.json.return_value={'ok':status<400,'result':result or {},'description':description}
    if status>=400:
        value.raise_for_status.side_effect=requests.HTTPError(f'HTTP {status}')
    return value


@pytest.fixture
def report(tmp_path,monkeypatch):
    monkeypatch.setattr(prepared.signal_charts,'render_signal_chart',lambda s:b'png:'+s.name.encode()+s.metrics.get('chart_index','').encode())
    store=SnapshotStore(tmp_path)
    token=store.save(sample_assessment(),NOW)
    return store,token


@pytest.fixture
def wire(monkeypatch):
    monkeypatch.setenv('TELEGRAM_CHAT_ID','12345')
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN','test-token')
    monkeypatch.setattr(transport.time,'sleep',lambda seconds:None)
    monkeypatch.setattr(transport,'_last_delivery_at',0)
    monkeypatch.setattr(transport,'answer_callback',lambda *a:None)


def query(token,view):
    return {'id':'q','data':f'r:{token}:{view}','message':{'message_id':10,'chat':{'id':12345}}}


def test_charts_upload_once_then_reuse_telegram_ids_across_restart(report,wire,monkeypatch):
    store,token=report; posted=[]
    def post(url,**kw):
        posted.append((url,kw))
        if kw.get('files'):
            content=next(iter(kw['files'].values()))[1].decode()
            file_id='file:'+content
        else:
            file_id=kw.get('json',{}).get('media',{}).get('media','file:existing')
        return response({'message_id':99,'photo':[{'file_id':file_id}]})
    monkeypatch.setattr(transport.requests,'post',post)
    interactive.handle_callback(query(token,'vix'),store)
    interactive.handle_callback(query(token,'fed_rate'),store)
    restarted=SnapshotStore(store.directory)
    with patch.object(prepared.signal_charts,'render_signal_chart',side_effect=AssertionError('No render on click')), patch.object(restarted,'signals',side_effect=AssertionError('No pandas on click')):
        interactive.handle_callback(query(token,'vix'),restarted)
    assert len(posted)==3 and sum(bool(kw.get('files')) for _,kw in posted)==2
    assert posted[-1][1]['json']['media']['media'].startswith('file:png:vix')
    assert restarted.load_views(token)['panel']['message_id']==99


def test_expired_telegram_file_id_is_reuploaded_once(report,wire,monkeypatch):
    store,token=report
    store.remember_media(token,'vix:','expired')
    store.set_panel(token,{'message_id':99,'photo':True,'chart_key':'fed_rate:'})
    replies=iter([response(status=400,description='Bad Request: wrong file identifier/HTTP URL specified'),response({'message_id':99,'photo':[{'file_id':'repaired'}]})])
    posted=[]
    def post(url,**kw):posted.append(kw); return next(replies)
    monkeypatch.setattr(transport.requests,'post',post)
    interactive.handle_callback(query(token,'vix'),store)
    assert posted[0]['json']['media']['media']=='expired'
    assert posted[1]['files']['chart'][1].startswith(b'png:vix')
    assert store.load_views(token)['file_ids']['vix:']=='repaired'


def test_transient_edit_timeout_retries_idempotently_without_extra_message(wire,monkeypatch):
    replies=iter([requests.Timeout('timeout'),response({'message_id':99})]); methods=[]
    def post(url,**kwargs):
        methods.append(url.rsplit('/',1)[-1]); value=next(replies)
        if isinstance(value,Exception):raise value
        return value
    monkeypatch.setattr(transport.requests,'post',post)
    assert transport.show_panel('Caption',{},'file:cached',{'message_id':99,'photo':True})
    assert methods==['editMessageMedia','editMessageMedia']


def test_api_rejection_logs_telegram_description_without_token(wire,monkeypatch,caplog):
    monkeypatch.setattr(transport.requests,'post',lambda *a,**kw:response(status=400,description='Bad Request: cannot parse media test-token'))
    assert transport.show_panel('Caption',{},b'png',{'message_id':99,'photo':True}) is None
    assert 'cannot parse media [redacted]' in caplog.text
    assert 'test-token' not in caplog.text


def test_missing_prepared_image_never_puts_new_caption_under_old_chart(report,wire,monkeypatch):
    store,token=report
    store.set_panel(token,{'message_id':99,'photo':True,'chart_key':'vix:'})
    manifest=store.load_views(token); manifest['views']['fed_rate']['chart_key']=None
    store._write_views(token,manifest)
    calls=[]
    monkeypatch.setattr(transport.requests,'post',lambda url,**kw:calls.append((url.rsplit('/',1)[-1],kw)) or response({'message_id':100}))
    interactive.handle_callback(query(token,'fed_rate'),store)
    assert [m for m,_ in calls]==['sendMessage','deleteMessage']
    assert not store.load_views(token)['panel']['photo']


def test_refresh_worker_does_not_block_callback_polling(wire,monkeypatch):
    entered,release,clicked=threading.Event(),threading.Event(),threading.Event()
    first={'ok':True,'result':[{'update_id':1,'message':{'chat':{'id':12345},'text':'/refresh'}}]}
    second={'ok':True,'result':[{'update_id':2,'callback_query':query('abcdef123456','vix')}]}
    reads=iter([first,second])
    def get(*a,**kw):
        try: data=next(reads)
        except StopIteration:
            assert clicked.wait(1); release.set(); raise KeyboardInterrupt()
        if data is second: assert entered.wait(1)
        result=Mock(); result.json.return_value=data; return result
    def handler(text):entered.set(); assert release.wait(2)
    monkeypatch.setattr(transport.requests,'get',get)
    with pytest.raises(KeyboardInterrupt):
        transport.poll_forever(handler,lambda q:clicked.set())
    assert clicked.is_set()


def test_repeated_button_does_not_edit_again(report,wire,monkeypatch):
    store,token=report; calls=[]
    monkeypatch.setattr(transport.requests,'post',lambda *a,**kw:calls.append(kw) or response({'message_id':99,'photo':[{'file_id':'vix-file'}]}))
    interactive.handle_callback(query(token,'vix'),store)
    interactive.handle_callback(query(token,'vix'),store)
    assert len(calls)==1


def test_uncertain_edit_failure_forces_media_restore_on_next_click(report,wire,monkeypatch):
    store,token=report
    store.set_panel(token,{'message_id':99,'photo':True,'chart_key':'vix:','view':'vix'})
    store.remember_media(token,'vix:','vix-file')
    monkeypatch.setattr(transport,'notice',lambda *a:None)
    monkeypatch.setattr(transport,'show_panel',lambda *a:None)
    interactive.handle_callback(query(token,'fed_rate'),store)
    assert store.load_views(token)['panel']['chart_key'] is None
    calls=[]
    def show(text,keyboard,media,existing):
        calls.append(media)
        return {'message_id':99,'photo':True}
    monkeypatch.setattr(transport,'show_panel',show)
    interactive.handle_callback(query(token,'vix'),store)
    assert calls==['vix-file']
