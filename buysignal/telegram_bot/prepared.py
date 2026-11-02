"""Prepare immutable captions, keyboards and unique charts once per observation report."""
from dataclasses import replace
import logging

from buysignal import config
from buysignal.analysis import signal_charts
from buysignal.telegram_bot import views

log = logging.getLogger(__name__)


def build(result, token, sent_at):
    signals = {s.name: s for s in result.subsignals}
    captions, charts = {}, {}
    available = []
    market = signals.get('market_dip')
    if market is not None and market.history is not None:
        available = [k for k in views.INDEXES if k in market.history and market.history[k].notna().any()]
    for view in sorted(views.VIEWS - {'close'}):
        name = ('market_dip' if view.startswith('index_') or view == 'market_info' else
                'sector' if view == 'eps_info' else 'fed_rate' if view == 'fed_changes' else view)
        signal = signals.get(name)
        if signal is None:
            continue
        index = view.removeprefix('index_') if view.startswith('index_') else 'SPY'
        if name == 'market_dip':
            if index not in available:
                continue
            observed = str(signal.history[index].dropna().index[-1].date())
            signal = replace(signal, observed_at=observed, metrics={**signal.metrics, 'chart_index': index})
        chart_key = name + ':' + (index if name == 'market_dip' else '')
        if chart_key not in charts:
            try:
                chart_signal = replace(signal, history=None if signal.history is None else signal.history.tail(500))
                charts[chart_key] = signal_charts.render_signal_chart(chart_signal) if config.TELEGRAM_CHARTS else None
            except Exception as exc:
                charts[chart_key] = None
                log.warning('Chart preparation failed for %s (%s)', chart_key, type(exc).__name__)
        pages = views.caption_pages(views.panel_caption(signal, view, sent_at))
        for i, text in enumerate(pages):
            markup = views.keyboard(token, view, available, sections=set(signals))
            if len(pages) > 1:
                markup['inline_keyboard'].insert(0, [{
                    'text': f"{'● ' if j == i else ''}Details {j+1}/{len(pages)}",
                    'callback_data': f'r:{token}:{view}~{j}',
                } for j in range(len(pages))])
            item = {'caption': text, 'keyboard': markup,
                    'chart_key': chart_key if charts[chart_key] is not None else None}
            captions[f'{view}~{i}'] = item
            if i == 0:
                captions[view] = item
    return captions, {key: png for key, png in charts.items() if png is not None}
