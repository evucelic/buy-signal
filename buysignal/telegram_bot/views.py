"""Compact numeric overview and on-demand panel captions; no collection or IO."""
from __future__ import annotations

from datetime import datetime
import os
from zoneinfo import ZoneInfo

import pandas as pd

from buysignal.signals.base import SubSignal, format_table
from buysignal.signals.buy_signal import BuySignal
from buysignal.telegram_bot.render import ALERT_LABELS, SIGNAL_META, esc, fmt

SECTIONS = (("market_dip", "Indexes"), ("vix", "VIX"), ("fed_rate", "FedWatch"),
            ("margin_debt", "Margin"), ("sector", "Industry EPS"), ("credit", "Credit"), ("yield_curve", "Yield curve"))
INDEXES = ("SPY", "NASDAQ", "DOW", "EURO50")
_SHORT_LABELS = {"fed_rate": "Fed", "sector": "EPS", "yield_curve": "Curve"}
_BUTTON_LABELS = {"sector": "EPS"}
_INDEX_BUTTON_LABELS = {"NASDAQ": "Nasdaq", "DOW": "Dow", "EURO50": "Euro50"}
VIEWS = {name for name, _ in SECTIONS} | {"market_info", "eps_info", "fed_changes", "close"} | {"index_" + name for name in INDEXES}


def stamp(value, *, short=False):
    if value is None or pd.isna(value):
        return "—"
    value = pd.Timestamp(value)
    if value.tzinfo is None:
        return value.strftime("%Y-%m-%d")
    value = value.tz_convert(ZoneInfo(os.environ.get("TELEGRAM_TIMEZONE", "Europe/Zagreb")))
    return value.strftime("%m-%d %H:%M" if short else "%Y-%m-%d %H:%M %Z")


def percent(value):
    return fmt(value, "+.1%")


def _vix(signal):
    m = signal.metrics
    high = m.get("recent_peak")
    basis = "20d high" if m.get('peak_basis') == 'intraday' else '20d closing high'
    suffix = f" · AT {basis.upper()}" if m.get("at_high") else f" · {percent(m.get('from_peak'))} vs {basis}"
    return f"{signal.score:.1f} · {percent(m.get('change'))} vs close" + (suffix + f" ({high:.1f})" if high else "")


def _fed(signal):
    frame = signal.history
    if frame is None or frame.empty:
        return "probabilities unavailable"
    labels = {"nearest": "Next", "six_month": "6m", "one_year": "1y"}
    pieces = []
    for row in frame.itertuples():
        probabilities = [("Ease", row.prob_ease), ("Hold", row.prob_no_change), ("Hike", row.prob_hike)]
        label, value = max(probabilities, key=lambda item: item[1])
        pieces.append(f"{labels.get(row.horizon, row.horizon)} {label} {value:.0%}")
    first = frame.iloc[0]
    lo, hi = first.get("reference_lower_bps"), first.get("reference_upper_bps")
    reference = f"{lo / 100:.2f}–{hi / 100:.2f}% · " if pd.notna(lo) and pd.notna(hi) else ""
    return reference + " / ".join(pieces)


def numeric_line(signal):
    m = signal.metrics
    if signal.name == "market_dip":
        return f"{signal.score:+.1%} from 63-session high · 5d {percent(m.get('weekly'))}"
    if signal.name == "vix":
        return _vix(signal)
    if signal.name == "fed_rate":
        return _fed(signal)
    if signal.name == "margin_debt":
        return f"{fmt(m.get('latest'), ',.0f')} USD m · {signal.score:+.1%} m/m" + (" · ⚠ stale" if m.get('freshness') == 'stale' else '')
    if signal.name == "sector":
        return f"{m.get('growing', '—')}/{m.get('sample_size', '—')} positive FY+1 EPS growth"
    if signal.name == "credit":
        changes = m.get("changes_bp", {})
        change = changes.get(21, changes.get("21"))
        return f"{signal.score*100:.0f} bp · {fmt(change, '+.0f')} bp / 21 sessions"
    if signal.name == "yield_curve":
        suffix = ""
        if signal.history is not None and "policy_spread" in signal.history:
            value = signal.history.policy_spread.iloc[-1]
            if pd.notna(value):
                suffix = f" · 2y–FFR {value:+.2f} pp"
        return f"10y–3m {signal.score:+.2f} pp" + suffix
    return signal.state.replace('_', ' ')


def overview(result: BuySignal, caches, now: datetime):
    by_name = {s.name: s for s in result.subsignals}
    icon, label = ALERT_LABELS.get(result.state, ("", result.state))
    lines = [f"{icon} <b>{esc(label)}</b> · {result.passing_count}/{result.required_count}", f"Sent {stamp(now)}", ""]
    for key, _ in SECTIONS:
        signal = by_name.get(key)
        mark = "⚠️" if signal and signal.metrics.get("stale") else "·" if signal and signal.advisory else "✅" if signal and signal.passes else "▫️" if signal else "❔"
        value = numeric_line(signal) if signal else "unknown"
        lines.append(f"{mark} <b>{SIGNAL_META[key][1]}</b>: {esc(value)}")
    market = by_name.get("market_dip")
    if market:
        m = market.metrics
        if m.get("stale"):
            lines.append(f"⚠️ Prices cached through {esc(market.observed_at or 'unknown')}; expected {esc(m.get('expected_session') or 'latest session')}.")
        elif m.get("refresh_failed"):
            lines.append("⚠️ Price refresh failed; showing cached observations.")
        if market.table:
            lines.extend(["", "<pre>" + esc(market.table) + "</pre>"])
        dates = m.get("index_changes", {})
        if dates and len({v['date'] for v in dates.values()}) > 1:
            lines.append("Index dates: " + esc(" · ".join(f"{k} {v['date']}" for k, v in dates.items())))
    if result.missing_signals:
        lines.append("⚠️ Missing: " + esc(", ".join(result.missing_signals)))
    cache_map = dict(caches)
    rows = []
    for key, label in SECTIONS:
        signal = by_name.get(key)
        observation = signal.observed_at if signal else None
        refreshed = signal.metrics.get('retrieved_at') if signal and key == 'market_dip' else None
        rows.append([_SHORT_LABELS.get(key, label), "FY+1" if key == 'sector' and signal else stamp(observation, short=True), stamp(refreshed or cache_map.get(key), short=True)])
    lines.extend(["", "<b>Data / last successful refresh</b>", "<pre>" + esc(format_table(["", "Data", "Refreshed"], rows)) + "</pre>",
                  "Refresh times: " + esc(pd.Timestamp(now).tz_convert(ZoneInfo(os.environ.get('TELEGRAM_TIMEZONE', 'Europe/Zagreb'))).strftime('%Z')),
                  "FINRA: monthly, published with a lag. EPS: snapshot of forecasts."])
    return "\n".join(lines)


def keyboard(token, view=None, available_indexes=INDEXES, sections=None):
    def button(label, key):
        return {"text": label, "callback_data": f"r:{token}:{key}"}
    choices = [(key, _BUTTON_LABELS.get(key, label)) for key, label in SECTIONS if sections is None or key in sections]
    if view and len(choices) == 1:
        choices = []
    rows = [[button(label, key) for key, label in choices[i:i+3]] for i in range(0, len(choices), 3)]
    if view and (view in ("market_dip", "market_info") or view.startswith("index_")):
        rows.insert(0, [button(_INDEX_BUTTON_LABELS.get(name, name), "index_" + name) for name in available_indexes])
        rows.insert(1, [button("Price & drawdown" if view == "market_info" else "Trend & intensity", "market_dip" if view == "market_info" else "market_info")])
    if view in ("sector", "eps_info"):
        rows.insert(0, [button("Estimate details" if view == "sector" else "EPS chart", "eps_info" if view == "sector" else "sector")])
    if view in ("fed_rate", "fed_changes"):
        rows.insert(0, [button("Probability changes" if view == "fed_rate" else "Probabilities", "fed_changes" if view == "fed_rate" else "fed_rate")])
    if view:
        rows.append([button("Close panel", "close")])
    return {"inline_keyboard": rows}


def panel_caption(signal: SubSignal, view: str, sent_at: str):
    title = dict(SECTIONS).get(signal.name, signal.name)
    lines = [f"<b>{esc(title)}</b> · {esc(numeric_line(signal))}"]
    if view == "market_info":
        m = signal.metrics
        lines = ["<b>SPY · trend &amp; move size</b>"]
        if m.get('ma200') and m.get('price'):
            lines.append(f"Price {m['price']/m['ma200']-1:+.1%} vs its 200-session average ({m['ma200']:,.2f}).")
            lines.append(f"That average changed {percent(m.get('ma_slope'))} over 20 sessions.")
        else:
            lines.append('200-session trend: unavailable.')
        if m.get('intensity') is not None:
            lines.append(f"Five-session move: {percent(m.get('weekly'))} · {m['intensity']:+.2f} volatility units.")
            lines.append('Move size is scaled by daily volatility from the 20 sessions before the move. Negative = decline; larger magnitude = larger move relative to that baseline.')
        else:
            lines.append('Volatility-scaled move: unavailable.')
        lines.append('Context only; neither is an entry condition or a probability.')
    elif view == "eps_info":
        lines = ["<b>EPS estimates · FY+1</b>", "Yahoo Finance analyst consensus, sampled from each industry's largest constituents; weighted by constituent market weight.",
                 "Analyst names are not provided by this feed. Counts below are analysts per covered company."]
        if signal.history is not None:
            for _, row in signal.history.iterrows():
                counts = f"{int(row['covered_companies'])} of {int(row['sampled_companies'])}" if pd.notna(row.get('covered_companies')) and pd.notna(row.get('sampled_companies')) else 'n/a'
                analysts = f"{int(row['analysts_min'])}–{int(row['analysts_max'])}" if pd.notna(row.get('analysts_min')) and pd.notna(row.get('analysts_max')) else 'not supplied'
                cap = row.get('market_cap')
                cap = f"${cap/1e12:.2f}T" if pd.notna(cap) else 'n/a'
                lines.append(f"<b>{esc(str(row.industry))}</b> · {cap}\n{counts} sampled companies have estimates · covered sampled weight {fmt(row.get('estimate_coverage'), '.0%')} · analysts {analysts}")
    elif view == "fed_changes":
        lines = ["<b>FedWatch probability changes</b>", "Same meeting and target range · Δ in percentage points (pp)"]
        if signal.history is not None:
            for row in signal.history.itertuples():
                delta = getattr(row, 'delta_hike_pp', None)
                if pd.notna(delta):
                    lines.append(f"{pd.Timestamp(row.meeting_date):%Y-%m-%d}: Ease {row.delta_ease_pp:+.1f} · Hold {row.delta_no_change_pp:+.1f} · Hike {delta:+.1f} pp\nCompared with {stamp(row.comparison_since)}")
                else:
                    lines.append(f"{pd.Timestamp(row.meeting_date):%Y-%m-%d}: no comparable prior observation")
    elif signal.name == "market_dip":
        name = view.removeprefix('index_') if view.startswith('index_') else 'SPY'
        lines = [f"<b>{esc(name)}</b> · daily adjusted prices"]
        if name == 'SPY':
            lines.append(f"Drawdown {signal.score:+.1%} · 63-session closing high {fmt(signal.metrics.get('peak'), ',.2f')}")
        change = signal.metrics.get('index_changes', {}).get(name)
        if change:
            lines.append('1 / 5 / 21 sessions: ' + ' / '.join(percent(v) for v in change['changes']))
            lines.append('Index observation: ' + change['date'])
    elif signal.name == 'margin_debt':
        lines.append('FINRA · USD million · monthly · published with a lag')
    elif signal.name == 'credit':
        changes = signal.metrics.get('changes_bp', {})
        lines.append('HY OAS · basis points · daily close')
        lines.append('5 sessions ' + fmt(changes.get(5, changes.get('5')), '+.0f') + ' bp · positive = widening')
    elif signal.name == 'fed_rate' and signal.history is not None and not signal.history.empty:
        # The chart contains the probabilities; avoid repeating them in its caption.
        lines = ['<b>CME FedWatch</b> · target-rate probabilities']
        row = signal.history.iloc[0]
        lo, hi = row.get('reference_lower_bps'), row.get('reference_upper_bps')
        if pd.notna(lo) and pd.notna(hi):
            lines.append(f'Current target {lo/100:.2f}–{hi/100:.2f}%')
    elif signal.name == 'sector':
        lines.append('FY+1 analyst EPS growth · market-weighted constituent sample')
    if signal.name == 'sector':
        retrieved = signal.history.fetched_at.iloc[-1] if signal.history is not None and 'fetched_at' in signal.history else None
        lines.append('Snapshot retrieved ' + stamp(retrieved))
    else:
        lines.append('Data ' + stamp(signal.observed_at))
    lines.append('Report ' + stamp(sent_at))
    return '\n'.join(lines)


def caption_pages(text, limit=950):
    """Keep each industry and its coverage together; repeat timestamps on every page."""
    from buysignal.telegram_bot.html_chunks import split_html
    if len(text.encode('utf-16-le')) // 2 <= limit:
        return [text]
    body, first_time, report_time = text.rsplit('\n', 2)
    footer = '\n' + first_time + '\n' + report_time
    title = body.split('\n', 1)[0]
    blocks = body.split('\n<b>')
    blocks = [blocks[0]] + ['<b>' + block for block in blocks[1:]]
    pages, current = [], ''
    for block in blocks:
        candidate = (current + '\n' if current else '') + block
        if len((candidate + footer).encode('utf-16-le')) // 2 > limit and current:
            pages.append(current + footer)
            current = title + '\n' + block
        else:
            current = candidate
    if current:
        pages.append(current + footer)
    return [chunk for page in pages for chunk in split_html(page, limit=limit)]
