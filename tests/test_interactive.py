"""Frozen report navigation, bounded storage, Telegram editing and source semantics."""
from datetime import datetime, timezone
from unittest.mock import Mock, patch
import re
import json

import pandas as pd
import pytest

from buysignal.telegram_bot import app, interactive, transport, views, prepared
from buysignal.telegram_bot.snapshots import SnapshotStore
from buysignal.signals.base import SubSignal
from buysignal.signals.buy_signal import BuySignal, assess
from buysignal.signals import vix_signal
from scripts.preview_entry_report import sample_assessment, NOW


@pytest.fixture
def saved(tmp_path):
    store = SnapshotStore(tmp_path / 'reports')
    result = sample_assessment()
    return store, store.save(result, NOW), result


@pytest.fixture
def wire(monkeypatch):
    shown, notices = [], []
    def show(text, keyboard, png, existing=None):
        shown.append((text, keyboard, png, existing))
        return {'message_id': 99, 'photo': png is not None or bool(existing and existing.get('photo'))}
    monkeypatch.setattr(transport, 'show_panel', show)
    monkeypatch.setattr(transport, 'answer_callback', lambda ident, text=None: notices.append(text))
    monkeypatch.setattr(transport, 'notice', notices.append)
    return shown, notices


def query(token, view, message_id=12):
    return {'id': 'query-id', 'data': f'r:{token}:{view}', 'message': {'message_id': message_id, 'chat': {'id': 12345}}}


def test_buttons_reuse_one_panel_and_never_recompute(saved, wire, monkeypatch):
    store, token, _ = saved
    monkeypatch.setattr(app, 'compute_signal', lambda **kw: pytest.fail('Buttons must not collect'))
    interactive.handle_callback(query(token, 'vix'), store)
    interactive.handle_callback(query(token, 'fed_rate'), store)
    shown, _ = wire
    assert len(shown) == 2
    assert shown[0][3] is None
    assert shown[1][3]['message_id'] == 99
    assert store.load(token)['panel']['message_id'] == 99


def test_snapshot_survives_restart_and_keeps_original_values(saved, wire):
    store, token, result = saved
    result.subsignals[0].score = 999
    reloaded = SnapshotStore(store.directory)
    interactive.handle_callback(query(token, 'vix'), reloaded)
    assert '25.0' in wire[0][0][0] and '999' not in wire[0][0][0]
    assert '2026-11-02' in wire[0][0][0]
    assert 'signals' not in reloaded.load(token)
    assert reloaded.chart_bytes(token, 'market_dip:SPY') == store.chart_bytes(token, 'market_dip:SPY')


def test_legacy_report_prepares_once_without_downloading(tmp_path, wire, monkeypatch):
    store = SnapshotStore(tmp_path)
    result = sample_assessment()
    signals = []
    for signal in result.subsignals:
        values = {k: v for k, v in vars(signal).items() if k != 'history'}
        frame = signal.history
        values['history'] = None if frame is None else frame.tail(460).to_json(orient='split', date_format='iso')
        values['datetime_index'] = frame is not None and isinstance(frame.index, pd.DatetimeIndex)
        signals.append(values)
    import time
    token = 'abcdef123456'
    store._write(token, {'created': time.time(), 'sent_at': NOW.isoformat(), 'signals': signals, 'panel': None})
    monkeypatch.setattr(app, 'compute_signal', lambda **kw: pytest.fail('Legacy navigation must not collect'))
    interactive.handle_callback(query(token, 'vix'), store)
    monkeypatch.setattr(store, 'signals', lambda *a: pytest.fail('Legacy history must only be decoded once'))
    interactive.handle_callback(query(token, 'fed_rate'), store)
    assert len(wire[0]) == 2


def test_switching_index_uses_selected_series_and_buttons(saved, wire):
    store, token, _ = saved
    interactive.handle_callback(query(token, 'index_NASDAQ'), store)
    caption, markup, png, _ = wire[0][0]
    assert '<b>Nasdaq</b>' in caption
    assert png == store.chart_bytes(token, 'market_dip:NASDAQ')
    labels = [b['text'] for row in markup['inline_keyboard'] for b in row]
    assert 'SPY' in labels and 'Dow' in labels and 'Euro50' in labels


def test_unavailable_index_does_not_replace_existing_chart(saved, wire):
    store, token, result = saved
    market = next(s for s in result.subsignals if s.name == 'market_dip')
    market.history = market.history.drop(columns='EURO50')
    token = store.save(result, NOW)
    interactive.handle_callback(query(token, 'index_EURO50'), store)
    assert not wire[0]
    assert 'unavailable' in wire[1][0]


def labels(markup):
    return [b['text'] for row in markup['inline_keyboard'] for b in row]


def test_estimate_coverage_sits_in_the_earnings_caption_without_a_sub_tab(saved, wire):
    store, token, _ = saved
    interactive.handle_callback(query(token, 'sector'), store)
    caption, markup, png, _ = wire[0][-1]
    assert '4 of 5 industries growing' in caption
    assert 'Industry          Growth  Firms\nSemiconductors      +61%    8/8\n' in caption
    assert 'Drug Manufactur…     -5%    8/8' in caption and 'Analysts per firm 12–40' in caption
    assert png == store.chart_bytes(token, 'sector:')
    assert not any('detail' in text.lower() or 'estimate' in text.lower() for text in labels(markup))
    assert len(caption.encode('utf-16-le')) // 2 <= 950


def test_thin_estimate_coverage_is_named_rather_than_averaged_away():
    result = sample_assessment()
    signal = next(s for s in result.subsignals if s.name == 'sector')
    signal.history.loc[4, ['estimate_coverage', 'covered_companies']] = [.3, 3]
    caption = views.panel_caption(signal, 'sector')
    assert 'Too few estimates: Drug Manufacturers - General (3 of 8 firms)' in caption


def test_fed_changes_are_drawn_on_the_probability_chart(saved, wire, monkeypatch):
    from matplotlib.figure import Figure
    from matplotlib.text import Text
    from buysignal.analysis import signal_charts
    store, token, result = saved
    interactive.handle_callback(query(token, 'fed_rate'), store)
    caption, markup, _, _ = wire[0][-1]
    assert 'higher rates expected by Apr 2027' in caption and 'Rate now 3.75–4.00%' in caption
    assert not any('change' in text.lower() for text in labels(markup))
    drawn = []
    original = Figure.savefig
    monkeypatch.setattr(Figure, 'savefig', lambda figure, *a, **k: drawn.extend(t.get_text() for t in figure.findobj(Text)) or original(figure, *a, **k))
    signal = next(s for s in result.subsignals if s.name == 'fed_rate')
    assert signal_charts._render_signal_chart(signal)
    # Same meeting, same target range: the move since the prior reading sits under each share.
    assert '▼ 4' in drawn and '▲ 4' in drawn and '▲▼ points since 1 Nov' in drawn
    assert {'Lower', 'Same', 'Higher'} <= set(drawn) and 'By 28 Apr 2027' in drawn


def test_trend_is_part_of_each_index_panel_and_move_size_is_gone(saved, wire):
    store, token, _ = saved
    for view, title in (('market_dip', 'S&amp;P 500 (SPY)'), ('index_DOW', 'Dow Jones')):
        interactive.handle_callback(query(token, view), store)
        caption, markup, _, _ = wire[0][-1]
        assert title in caption
        assert '200-day avg' in caption and 'Price vs avg' in caption and "Week's move" not in caption
        assert not any('trend' in text.lower() or 'intensity' in text.lower() for text in labels(markup))
    assert '· in a dip' in wire[0][0][0] and '· in a dip' not in wire[0][1][0]  # the dip is judged on SPY only
    assert 'Dip line' not in wire[0][0][0]  # thresholds are drawn on the chart, not listed


def test_range_buttons_switch_the_chart_and_keep_the_selected_index(saved, wire):
    store, token, _ = saved
    interactive.handle_callback(query(token, 'index_NASDAQ'), store)
    _, markup, default_png, _ = wire[0][-1]
    assert '● 1Y' in labels(markup) and '● Nasdaq' in labels(markup) and '3M' in labels(markup)
    target = next(b['callback_data'] for row in markup['inline_keyboard'] for b in row if b['text'] == '3M')
    assert target.endswith(':index_NASDAQ@3m')
    interactive.handle_callback(query(token, 'index_NASDAQ@3m'), store)
    caption, markup, png, _ = wire[0][-1]
    assert '<b>Nasdaq</b>' in caption
    assert png == store.chart_bytes(token, 'market_dip:NASDAQ@3m') and png != default_png
    assert '● 3M' in labels(markup) and '1Y' in labels(markup)
    # Changing index keeps the chosen range; the default range needs no suffix.
    dow = next(b['callback_data'] for row in markup['inline_keyboard'] for b in row if b['text'] == 'Dow')
    assert dow.endswith(':index_DOW@3m')
    assert all(len(b['callback_data'].encode()) <= 64 for row in markup['inline_keyboard'] for b in row)


def test_ranges_follow_the_available_history():
    from buysignal.analysis.signal_charts import available_ranges
    dates = pd.bdate_range(end='2026-11-02', periods=3000)
    long = SubSignal('vix', 20, 'none', '', history=pd.DataFrame({'date': dates, 'vix': 20.}))
    assert available_ranges(long) == ('3m', '1y', '5y', 'max')
    short = SubSignal('vix', 20, 'none', '', history=long.history.tail(300))
    assert available_ranges(short) == ('3m', '1y', 'max')  # no "5Y" button that would show 14 months
    tiny = SubSignal('vix', 20, 'none', '', history=long.history.tail(40))
    assert available_ranges(tiny) == ('1y',)
    monthly = SubSignal('margin_debt', 0, 'leveraging', '', history=pd.DataFrame({'month': pd.date_range('1997-01-01', periods=357, freq='MS'), 'debit_balances': 1.}))
    assert available_ranges(monthly) == ('1y', '5y', 'max')
    assert available_ranges(SubSignal('fed_rate', 0, 'flat', '', history=pd.DataFrame({'a': [1]}))) == ()


def test_buttons_from_reports_sent_before_the_redesign_still_open(tmp_path, wire):
    import time
    store = SnapshotStore(tmp_path)
    token = 'abcdef123456'
    store._write(token, {'created': time.time(), 'sent_at': NOW.isoformat(), 'panel': None})
    store._write_views(token, {'created': time.time(), 'panel': None, 'file_ids': {}, 'views': {
        'fed_changes': {'caption': 'Old probability changes', 'keyboard': {'inline_keyboard': []}, 'chart_key': None}}})
    interactive.handle_callback(query(token, 'fed_changes'), store)
    assert wire[0][-1][0] == 'Old probability changes'


def test_background_ranges_are_announced_until_drawn_and_never_plotted_on_click(tmp_path, wire, monkeypatch):
    import threading
    from buysignal.telegram_bot import snapshots
    release, drawn = threading.Event(), []
    def render(signal, range_key='1y'):
        if range_key != '1y' or signal.metrics.get('chart_index', 'SPY') != 'SPY':
            assert release.wait(5)
        drawn.append((signal.name, signal.metrics.get('chart_index'), range_key))
        return b'png:' + signal.name.encode() + range_key.encode()
    monkeypatch.setattr(prepared.signal_charts, 'render_signal_chart', render)
    store = SnapshotStore(tmp_path, background=True)
    token = store.save(sample_assessment(), NOW)
    # Only each section's default chart is drawn before the overview can be sent.
    assert sorted(name for name, _, _ in drawn) == sorted(['market_dip', 'vix', 'fed_rate', 'margin_debt', 'sector', 'credit', 'yield_curve'])
    interactive.handle_callback(query(token, 'vix'), store)
    assert len(wire[0]) == 1
    interactive.handle_callback(query(token, 'vix@3m'), store)
    assert len(wire[0]) == 1 and 'Tap again' in wire[1][-1]
    release.set()
    store.drain()
    assert not store.load_views(token)['pending']
    monkeypatch.setattr(prepared.signal_charts, 'render_signal_chart', lambda *a: pytest.fail('Buttons must not plot'))
    interactive.handle_callback(query(token, 'vix@3m'), store)
    assert wire[0][-1][2] == b'png:vix3m'
    # A chart still marked pending long after the report (e.g. a restart) falls back to its caption.
    manifest = store.load_views(token)
    manifest['pending'] = {'credit:@3m': manifest['created'] - snapshots.PENDING_GRACE_SEC - 1}
    (store._asset_path(token, 'credit:@3m')).unlink()
    store._write_views(token, manifest)
    interactive.handle_callback(query(token, 'credit@3m'), store)
    assert '<b>Credit spread</b>' in wire[0][-1][0] and wire[0][-1][2] is None


def test_close_only_deletes_the_panel_not_the_overview(saved, wire, monkeypatch):
    store, token, _ = saved
    store.set_panel(token, {'message_id': 99, 'photo': True})
    closed = []
    monkeypatch.setattr(transport, 'close_panel', lambda ident: closed.append(ident) or True)
    interactive.handle_callback(query(token, 'close', message_id=12), store)
    assert not closed
    interactive.handle_callback(query(token, 'close', message_id=99), store)
    assert closed == [99] and store.load(token)['panel'] is None


@pytest.mark.parametrize('payload', ['r:../secret:vix', 'r:000000000000:vix', 'r:abc:unknown', 'oops', 'r:000000000000:vix~999'])
def test_invalid_and_expired_callbacks_are_non_destructive(tmp_path, wire, payload):
    store = SnapshotStore(tmp_path)
    q = query('000000000000', 'vix')
    q['data'] = payload
    interactive.handle_callback(q, store)
    assert not wire[0] and wire[1]


def test_storage_retains_bounded_history_and_expires_snapshots(tmp_path, monkeypatch):
    from buysignal.telegram_bot import snapshots
    monkeypatch.setattr(snapshots, 'MAX_SNAPSHOTS', 3)
    store = SnapshotStore(tmp_path)
    result = sample_assessment()
    tokens = [store.save(result, NOW) for _ in range(5)]
    assert len(list(tmp_path.glob('*.json'))) == 3
    with pytest.raises(OSError):
        store.load(tokens[0])
    with patch.object(snapshots.time, 'time', return_value=store.load(tokens[-1])['created'] + snapshots.MAX_AGE_SEC + 1):
        with pytest.raises(ValueError, match='expired'):
            store.load(tokens[-1])


def response(payload, status=200):
    result = Mock(status_code=status)
    result.json.return_value = payload
    return result


@pytest.fixture
def telegram(monkeypatch):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'test-token')
    monkeypatch.setenv('TELEGRAM_CHAT_ID', '12345')
    monkeypatch.setattr(transport.time, 'sleep', lambda _: None)
    monkeypatch.setattr(transport, '_last_delivery_at', 0)


def test_panel_media_and_caption_edits_use_correct_api_payloads(telegram, monkeypatch):
    posts = []
    def post(url, **kwargs):
        posts.append((url.split('/')[-1], kwargs))
        return response({'ok': True, 'result': {'message_id': 99}})
    monkeypatch.setattr(transport.requests, 'post', post)
    markup = views.keyboard('abcdef123456')
    panel = transport.show_panel('VIX', markup, b'png')
    assert panel == {'message_id': 99, 'photo': True}
    transport.show_panel('Fed', markup, b'new-png', panel)
    transport.show_panel('Details', markup, None, panel)
    assert [p[0] for p in posts] == ['sendPhoto', 'editMessageMedia', 'editMessageCaption']
    assert posts[0][1]['data']['disable_notification'] is True
    media = json.loads(posts[1][1]['data']['media'])
    assert media['media'] == 'attach://chart' and media['caption'] == 'Fed'
    assert 'disable_notification' not in posts[1][1]['data']
    assert posts[2][1]['json']['message_id'] == 99
    assert json.loads(posts[0][1]['data']['reply_markup']) == markup


def test_deleted_panel_is_recreated_only_on_explicit_not_found(telegram, monkeypatch):
    replies = iter([response({'ok': False, 'description': 'Bad Request: message to edit not found'}, 400), response({'ok': True, 'result': {'message_id': 101}})])
    posted = []
    def post(url, **kwargs):
        posted.append(url.split('/')[-1])
        return next(replies)
    monkeypatch.setattr(transport.requests, 'post', post)
    panel = transport.show_panel('New chart', {}, b'png', {'message_id': 99, 'photo': True})
    assert panel['message_id'] == 101
    assert posted == ['editMessageMedia', 'sendPhoto']


def test_poll_authorizes_callbacks_and_acknowledges_before_render(telegram, monkeypatch):
    events = []
    good = query('abcdef123456', 'vix')
    bad = query('abcdef123456', 'vix'); bad['message']['chat']['id'] = 9
    updates = response({'ok': True, 'result': [{'update_id': 1, 'callback_query': bad}, {'update_id': 2, 'callback_query': good}]})
    getter = Mock(side_effect=[updates, KeyboardInterrupt])
    monkeypatch.setattr(transport.requests, 'get', getter)
    monkeypatch.setattr(transport, 'answer_callback', lambda ident: events.append('ack'))
    def callback(q):
        transport.answer_callback(q['id'])
        events.append('render')
    with pytest.raises(KeyboardInterrupt):
        transport.poll_forever(lambda t: pytest.fail('Unexpected command'), callback)
    assert events == ['ack', 'render']
    assert json.loads(getter.call_args_list[0].kwargs['params']['allowed_updates']) == ['message', 'callback_query']


def test_overview_has_attached_buttons_in_one_send(telegram, tmp_path, monkeypatch):
    monkeypatch.setattr(app, '_snapshot_store', SnapshotStore(tmp_path))
    posts = []
    monkeypatch.setattr(transport.requests, 'post', lambda url, **kw: posts.append((url, kw)) or response({'ok': True}))
    report = app._signal_report(sample_assessment())
    assert transport.send_report(report)
    assert len(posts) == 1 and posts[0][0].endswith('/sendMessage')
    buttons = posts[0][1]['json']['reply_markup']['inline_keyboard']
    assert [len(row) for row in buttons] == [4, 3]
    # The five conditions in checklist order, then context.
    assert [b['text'] for row in buttons for b in row] == ['VIX', 'Fed', 'Margin', 'Index', 'EPS', 'Credit', 'Curve']
    assert all(len(b['callback_data'].encode()) <= 64 for row in buttons for b in row)


def test_vix_high_uses_intraday_highs_and_flags_new_high():
    dates = pd.bdate_range(end='2026-11-03', periods=25)
    history = pd.DataFrame({'date': dates, 'vix': [20.] * 24 + [29.], 'high': [30.] * 24 + [29.]})
    with patch.object(vix_signal, 'daily_history', return_value=history), patch.object(vix_signal, 'vix_change_pct', return_value=.1):
        signal = vix_signal.score(vix=29.)
    assert signal.metrics['recent_peak'] == 30
    assert signal.metrics['from_peak'] == pytest.approx(29/30-1)
    assert re.search(r'20-day high +30.0\n', views.panel_caption(signal, 'vix'))
    history.loc[history.index[-1], ['vix','high']] = 31.
    with patch.object(vix_signal, 'daily_history', return_value=history), patch.object(vix_signal, 'vix_change_pct', return_value=.2):
        signal = vix_signal.score(vix=31.)
    assert re.search(r'20-day high +31.0\n', views.panel_caption(signal, 'vix'))
    assert '<b>VIX</b> · high fear' in views.panel_caption(signal, 'vix')


def test_absolute_times_use_zagreb_daylight_saving_rules(monkeypatch):
    monkeypatch.setenv('TELEGRAM_TIMEZONE','Europe/Zagreb')
    assert views.stamp('2026-11-03T02:00:00Z') == '2026-11-03 03:00 CET'
    assert views.stamp('2026-07-03T02:00:00Z') == '2026-07-03 04:00 CEST'


def test_command_panel_delivery_binds_buttons_to_its_message(telegram, tmp_path, monkeypatch):
    store = SnapshotStore(tmp_path)
    monkeypatch.setattr(app, '_snapshot_store', store)
    monkeypatch.setattr(app.signal_charts, 'render_signal_chart', lambda s, r=None: b'png')
    monkeypatch.setattr(transport.requests, 'post', lambda *a, **kw: response({'ok':True,'result':{'message_id':123}}))
    signal = next(s for s in sample_assessment().subsignals if s.name == 'fed_rate')
    report = app._single_signal(lambda: signal)
    assert transport.send_report(report)
    token = report.messages[0].keyboard['inline_keyboard'][0][0]['callback_data'].split(':')[1]
    panel = store.load(token)['panel']
    assert panel == {'message_id':123,'photo':True,'chart_key':'fed_rate:'}


def test_callback_uses_prepared_view_without_rendering_or_history_parsing(saved, wire, monkeypatch):
    store, token, _ = saved
    def fail(*args):
        raise AssertionError('Buttons must not render or parse histories')
    monkeypatch.setattr(prepared.signal_charts, 'render_signal_chart', fail)
    monkeypatch.setattr(store, 'signals', fail)
    monkeypatch.setattr(store, 'load', fail)
    interactive.handle_callback(query(token, 'vix'), store)
    assert len(wire[0]) == 1 and wire[1] == [None]
