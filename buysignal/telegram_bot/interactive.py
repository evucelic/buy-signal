"""Fast navigation over prepared report views; no scoring, collection or plotting."""
import threading

from buysignal.telegram_bot import transport, views

_NAVIGATION_LOCK = threading.Lock()


def handle_callback(query, store):
    raw = (query.get('data') or '').split(':')
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
            manifest = store.load_views(token)
        except (OSError, ValueError, KeyError, TypeError):
            transport.answer_callback(query['id'], 'This report has expired. Use /signal for a new overview.')
            return
        panel = manifest.get('panel')
        if base == 'close':
            transport.answer_callback(query['id'])
            if panel and panel['message_id'] == query.get('message', {}).get('message_id'):
                if transport.close_panel(panel['message_id']):
                    store.set_panel(token, None)
            return
        item = manifest['views'].get(requested)
        if item is None:
            transport.answer_callback(query['id'], 'This section is unavailable in this report.')
            return
        transport.answer_callback(query['id'])
        if panel and panel.get('view') == requested and (panel.get('photo') or not item['chart_key']):
            return
        chart_key = item['chart_key']
        same_chart = panel and panel.get('photo') and chart_key and panel.get('chart_key') == chart_key
        media = None
        cached_id = manifest['file_ids'].get(chart_key)
        if chart_key and not same_chart:
            try:
                media = cached_id or store.chart_bytes(token, chart_key)
            except OSError:
                # The figures remain readable even if an asset was removed externally.
                media = None
        target_panel = panel
        if panel and panel.get('photo') and media is None and not same_chart:
            target_panel = {**panel, 'replace_with_text': True}
        new_panel = transport.show_panel(item['caption'], item['keyboard'], media, target_panel)
        if new_panel and new_panel.get('invalid_media') and cached_id:
            # Telegram file IDs are bot-specific and can expire. Repair just this upload.
            try:
                media = store.chart_bytes(token, chart_key)
                new_panel = transport.show_panel(item['caption'], item['keyboard'], media, panel)
            except OSError:
                new_panel = None
        if new_panel and new_panel.get('message_id'):
            file_id = new_panel.pop('file_id', None)
            store.remember_media(token, chart_key, file_id)
            new_panel.update(chart_key=chart_key if new_panel.get('photo') else None, view=requested)
            store.set_panel(token, new_panel)
        else:
            if panel:
                # A timed-out edit may have reached Telegram. Never assume which
                # image is visible; the next click must replace media explicitly.
                store.set_panel(token, {**panel, 'chart_key': None, 'view': None})
            transport.notice('Panel delivery failed. Please try again.')
