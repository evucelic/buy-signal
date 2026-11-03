"""On-demand chart navigation from a frozen report, without network collection."""
from __future__ import annotations

from dataclasses import replace
import threading

from buysignal import config
from buysignal.analysis import signal_charts
from buysignal.telegram_bot import transport, views

_NAVIGATION_LOCK = threading.Lock()


def handle_callback(query, store):
    raw = query.get('data', '').split(':')
    if len(raw) != 3 or raw[0] != 'r':
        transport.answer_callback(query['id'], 'Unknown button. Use /signal.')
        return
    _, token, requested = raw
    base, _, page = requested.partition('~')
    if base not in views.VIEWS or (page and (not page.isdigit() or int(page) > 10)):
        transport.answer_callback(query['id'], 'Unknown view.')
        return
    with _NAVIGATION_LOCK:
        try:
            snapshot = store.load(token)
            signals = store.signals(snapshot)
        except (OSError, ValueError, KeyError, TypeError):
            transport.answer_callback(query['id'], 'This report has expired. Use /signal for a new overview.')
            return
        panel = snapshot.get('panel')
        if base == 'close':
            transport.answer_callback(query['id'])
            # Never delete the overview or an arbitrary callback message.
            if panel and panel['message_id'] == query.get('message', {}).get('message_id'):
                if transport.close_panel(panel['message_id']):
                    store.set_panel(token, None)
            return
        name = ('market_dip' if base.startswith('index_') or base == 'market_info' else 'sector' if base == 'eps_info'
                else 'fed_rate' if base == 'fed_changes' else base)
        signal = signals.get(name)
        if signal is None:
            transport.answer_callback(query['id'], 'No data for this section in this report.')
            return
        index = base.removeprefix('index_') if base.startswith('index_') else 'SPY'
        available = [k for k in views.INDEXES if name != 'market_dip' or (signal.history is not None and k in signal.history and signal.history[k].notna().any())]
        if name == 'market_dip':
            if index not in available:
                transport.answer_callback(query['id'], 'This index is unavailable in this report.')
                return
            signal = replace(signal, metrics={**signal.metrics, 'chart_index': index})
        # Answer once, after cheap validation but before waiting for the shared
        # browser/plot work lock. Later failures use a silent operational notice.
        transport.answer_callback(query['id'])
        caption = views.panel_caption(signal, base, snapshot['sent_at'])
        chunks = views.caption_pages(caption)
        current = min(int(page or 0), len(chunks)-1)
        caption = chunks[current]
        markup = views.keyboard(token, base, available, sections=set(signals))
        if len(chunks) > 1:
            markup['inline_keyboard'].insert(0, [{
                'text': f"{'● ' if i == current else ''}Details {i+1}/{len(chunks)}",
                'callback_data': f'r:{token}:{base}~{i}',
            } for i in range(len(chunks))])
        chart_key = name + ':' + (index if name == 'market_dip' else '')
        same_chart = panel and panel.get('photo') and panel.get('chart_key') == chart_key
        try:
            png = signal_charts.render_signal_chart(signal) if config.TELEGRAM_CHARTS and not same_chart else None
        except Exception:
            transport.notice('Chart could not render. Try another section.')
            return
        if png is None and panel and panel.get('photo') and not same_chart:
            transport.notice('No chart is available for this section in this report.')
            return
        new_panel = transport.show_panel(caption, markup, png, panel)
        if new_panel:
            new_panel['chart_key'] = chart_key if new_panel.get('photo') else None
            store.set_panel(token, new_panel)
        else:
            transport.notice('Panel delivery failed. Please try again.')
