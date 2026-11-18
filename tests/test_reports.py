"""Readable report sections, bounded Telegram HTML and graphs from exact snapshots."""

from dataclasses import replace
from datetime import datetime, timezone
from html.parser import HTMLParser
from io import BytesIO
from unittest.mock import patch

import html
import numpy as np
import pandas as pd
import pytest
from PIL import Image

from buysignal.analysis.signal_charts import render_signal_chart
from buysignal.signals.base import SubSignal
from buysignal.signals.buy_signal import assess
from buysignal.signals import market_signal
from buysignal.telegram_bot import render
from buysignal.telegram_bot.html_chunks import split_html

NOW = datetime(2026, 11, 2, 19, 0, tzinfo=timezone.utc)


class Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.data = []
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)

    def handle_endtag(self, tag):
        assert self.tags.pop() == tag

    def handle_data(self, data):
        self.data.append(data)


def plain(text):
    parser = Text()
    parser.feed(text)
    assert not parser.tags
    return "".join(parser.data)


def test_html_chunks_preserve_entities_unicode_and_balanced_tables():
    text = "<b>Header &amp; context</b>\n<pre>" + "SPY &lt; threshold 🧭\n" * 500 + "</pre>"
    parts = split_html(text, limit=500)
    assert len(parts) > 1
    assert all(len(part.encode("utf-16-le")) // 2 <= 500 for part in parts)
    assert "".join(plain(part) for part in parts) == plain(text)


def test_report_is_one_numeric_overview_with_absolute_times():
    from scripts.preview_entry_report import sample_assessment
    result = sample_assessment()
    report = render.signal_report(result, [(s.name, NOW) for s in result.subsignals], NOW)
    assert len(report.messages) == 1
    text = report.messages[0].text
    assert text.startswith(
        "📊 <b>Entry checklist · 4 of 5</b>\n●●●●○\n\n"
        "✅ <b>Fear</b> VIX 25.0\n"
        "❌ <b>Fed</b> higher leads 79% by Apr 27\n"
        "✅ <b>Margin</b> -2.0% in Sep\n"
        "✅ <b>Dip</b> SPY -7.1% from 3-month high\n"
        "✅ <b>EPS</b> 4 of 5 industries growing\n"
    )
    assert "🟢 <b>Credit</b> 212 bp · stress line 228\n🟢 <b>Curve</b> +0.80 pp · flat\n" in text
    sheet = html.unescape(text.split("<pre>")[1].split("</pre>")[0])
    assert max(len(line) for line in sheet.splitlines()) <= 32  # a phone shows the table without wrapping
    assert "AFTER" not in sheet and "Jobs" not in sheet and "Recession" not in sheet
    assert "Report 2026-11-02 20:00 CET" in text
    assert "last refresh" in text and "ago" not in text
    assert "Nasdaq" in text and "Dow" in text
    assert len(text.encode("utf-16-le")) // 2 < 4096
    plain(text)


def test_every_data_as_of_row_carries_a_date():
    from scripts.preview_entry_report import sample_assessment
    result = sample_assessment()
    text = render.signal_report(result, [(s.name, NOW) for s in result.subsignals], NOW).messages[0].text
    block = text.split("<b>Data as of</b>")[1].split("</blockquote>")[0].strip().splitlines()[1:]
    assert [line.split()[0] for line in block] == ["Prices", "VIX", "Fed", "Margin", "Earnings", "Credit", "Curve", "Report"]
    assert all("—" not in line for line in block)


def test_missing_required_data_stays_in_the_count_and_is_named():
    result = assess([], ["fed_rate"])
    text = render.signal_report(result, [], NOW).messages[0].text
    assert text.startswith("📊 <b>Entry checklist · 0 of 5 incomplete</b>\n○○○○○\n\n❔ <b>Fear</b> no data\n❔ <b>Fed</b> no data\n")
    assert "⚠️ No current data: Fear, Fed, Margin, Dip, EPS" in text
    assert "❔ <b>Credit</b> no data" in text


def test_checklist_boxes_show_pass_fail_and_stale():
    vix = SubSignal("vix", 34., "strong", "", passes=True)
    fed = SubSignal("fed_rate", .2, "flat", "", passes=True)
    stale = SubSignal("market_dip", -.07, "flat", "", passes=False, metrics={"stale": True, "index_changes": {}})
    margin = SubSignal("margin_debt", .01, "leveraging", "", passes=False, observed_at="2026-09-01")
    text = html.unescape(render.signal_report(assess([vix, fed, stale, margin]), [], NOW).messages[0].text)
    assert "Entry checklist · 2 of 5 incomplete" in text
    assert "✅ <b>Fear</b> VIX 34.0 · high fear" in text
    assert "❌ <b>Margin</b> +1.0% in Sep" in text
    assert "❔ <b>Dip</b> SPY -7.0% from 3-month high" in text
    assert "No current data: Dip, EPS" in text


def test_verbose_source_descriptions_do_not_leak_into_overview():
    signal = SubSignal("sector", 1, "growing", "A & B " * 2000, passes=True)
    report = render.signal_report(assess([signal]), [], NOW)
    assert len(report.messages) == 1
    assert 'A &amp; B' not in report.messages[0].text
    plain(report.messages[0].text)


def market():
    frame = pd.DataFrame({name: np.r_[np.linspace(70, 100, 245), [100, 98, 96, 94, 93]] for name in ("SPY", "NASDAQ", "DOW")},
                         index=pd.bdate_range(end="2026-11-02", periods=250))
    with patch.object(market_signal, "get_entry_history", return_value=frame):
        return market_signal.score()


@pytest.mark.parametrize("kind", ["market_dip", "vix", "fed_rate", "credit", "margin_debt", "sector", "yield_curve"])
def test_every_supported_chart_is_a_mobile_sized_png(kind):
    dates = pd.bdate_range(end="2026-11-02", periods=252)
    if kind == "market_dip":
        signal = market()
    elif kind == "vix":
        signal = SubSignal(kind, 25, "soft", "", history=pd.DataFrame({"date": dates, "vix": np.linspace(18, 25, 252)}))
    elif kind == "fed_rate":
        signal = SubSignal(kind, .8, "hiking", "", history=pd.DataFrame({
            "horizon": ["nearest", "six_month", "one_year"], "meeting_date": ["2026-12-09", "2027-04-28", "2027-10-27"],
            "prob_ease": [.1, .05, .01], "prob_no_change": [.5, .2, .1], "prob_hike": [.4, .75, .89],
            "delta_hike_pp": [2., -3., 0.],
        }))
    elif kind == "credit":
        signal = SubSignal(kind, 4, "widening", "", history=pd.DataFrame({"date": dates, "spread": np.linspace(3, 4, 252)}))
    elif kind == "margin_debt":
        signal = SubSignal(kind, 0, "deleveraging", "", history=pd.DataFrame({"month": pd.date_range(end="2026-10-01", periods=30, freq="MS"), "debit_balances": np.linspace(1e6, 1.4e6, 30)}))
    elif kind == "sector":
        signal = SubSignal(kind, 1, "growing", "", history=pd.DataFrame({"industry": ["Semiconductors", "Software", "Consumer Electronics"], "earnings_growth_estimate_1y": [.6, .2, -.05]}))
    else:
        signal = SubSignal(kind, 1, "flat", "", history=pd.DataFrame({"date": dates, "spread": np.linspace(-1, 1, 252), "policy_spread": np.linspace(.2, -.5, 252)}))
    png = render_signal_chart(signal)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    image = Image.open(BytesIO(png))
    # Near-square: Telegram shows it at full bubble width, with caption and buttons on one screen.
    assert image.width >= 700 and .85 <= image.height / image.width <= 1.2
    assert len(png) < 10_000_000


def test_absent_history_does_not_produce_a_fabricated_chart():
    assert render_signal_chart(SubSignal("vix", 25, "soft", "")) is None


def test_routine_report_is_clinical_without_repeated_coaching():
    from scripts.preview_entry_report import sample_assessment
    report = render.signal_report(sample_assessment(), [], NOW)
    text = '\n'.join(part.text for part in report.messages).lower()
    for phrase in ('probability of profit', 'safe bottom', 'room to consider', 'chance of a rebound', 'investigate before acting'):
        assert phrase not in text
    assert 'data as of' in text
    assert 'report 2026-11-02' in text


def test_valuation_report_keeps_facts_without_allocation_advice(make_opportunity):
    opportunity = make_opportunity()
    opportunity.verdict = 'S&P 500 default; investigate before acting'
    text = render.opportunity_message(opportunity)
    assert '20.0' in text and '15.0' in text
    assert 'Verdict:' not in text
    assert 'investigate before acting' not in text


def test_telegram_overview_tables_fit_a_small_phone_at_normal_monospace_size():
    from scripts.preview_entry_report import sample_assessment
    import re
    result = sample_assessment()
    text = render.signal_report(result, [(s.name, NOW) for s in result.subsignals], NOW).messages[0].text
    tables = re.findall(r'<pre>(.*?)</pre>', text, flags=re.S)
    assert len(tables) == 1
    assert all(len(line) <= 33 for table in tables for line in html.unescape(table).splitlines())
    assert 'intensity' not in text and 'volatility' not in text


def test_telegram_detail_pages_use_supported_html_and_fit_photo_caption_limit():
    from scripts.preview_entry_report import sample_assessment
    from buysignal.telegram_bot import prepared
    planned, _ = prepared.plan(sample_assessment(), 'abcdef123456', NOW.isoformat())
    assert {'market_dip', 'index_NASDAQ', 'vix', 'fed_rate', 'margin_debt', 'sector', 'credit', 'yield_curve'} <= set(planned)
    for item in planned.values():
        plain(item['caption'])  # balanced HTML, including ampersands and non-ASCII characters
        assert len(item['caption'].encode('utf-16-le')) // 2 <= 950 < 1024
        # Every number sits in one aligned table that a phone shows without wrapping.
        tables = item['caption'].split('<pre>')[1:]
        assert len(tables) == 1
        assert max(len(line) for line in html.unescape(tables[0].split('</pre>')[0]).splitlines()) <= 32


@pytest.fixture
def nothing_clipped(monkeypatch):
    """Every chart saved during the test must keep each heading, label and tick inside the canvas."""
    from matplotlib.figure import Figure
    from matplotlib.text import Text
    original, saved = Figure.savefig, []

    def checked_save(figure, *args, **kwargs):
        figure.canvas.draw()
        renderer = figure.canvas.get_renderer()
        # Matplotlib keeps label objects for ticks beyond the axis limits without drawing them.
        undrawn = set()
        for ax in figure.axes:
            for axis in (ax.xaxis, ax.yaxis):
                low, high = sorted(axis.get_view_interval())
                for tick in [*axis.get_major_ticks(), *axis.get_minor_ticks()]:
                    if not low <= tick.get_loc() <= high:
                        undrawn.update((id(tick.label1), id(tick.label2)))
        for text in figure.findobj(Text):
            if id(text) in undrawn or not text.get_visible() or not text.get_text().strip():
                continue
            bounds = text.get_window_extent(renderer)
            assert bounds.x0 >= -1 and bounds.x1 <= figure.bbox.width + 1, text.get_text()
            assert bounds.y0 >= -1 and bounds.y1 <= figure.bbox.height + 1, text.get_text()
        saved.append(1)
        return original(figure, *args, **kwargs)
    monkeypatch.setattr(Figure, 'savefig', checked_save)
    return saved


def test_signal_charts_keep_every_label_inside_the_image(nothing_clipped):
    from buysignal.analysis import signal_charts
    from scripts.preview_entry_report import sample_assessment
    for signal in sample_assessment().subsignals:
        if signal.history is None:
            continue
        # SPY draws the dip line, an index without it the plain layout; the other indexes repeat these.
        for index in (('SPY', 'EURO50') if signal.name == 'market_dip' else (None,)):
            shown = signal if index is None else replace(signal, metrics={**signal.metrics, 'chart_index': index})
            for range_key in signal_charts.available_ranges(shown, index) or (signal_charts.DEFAULT_RANGE,):
                assert signal_charts._render_signal_chart(shown, range_key)
    assert len(nothing_clipped) >= 15


def test_portfolio_charts_keep_every_label_inside_the_image(nothing_clipped, tmp_path):
    from scripts.preview_entry_report import sample_personal
    tabs, _ = sample_personal(tmp_path)
    assert sum(png is not None for _, png in tabs.values()) == len(nothing_clipped) >= 9


def test_panel_numbers_follow_the_range_button():
    from scripts.preview_entry_report import sample_assessment
    from buysignal.telegram_bot import prepared
    planned, _ = prepared.plan(sample_assessment(), 'abcdef123456', NOW.isoformat())
    year, everything = planned['credit']['caption'], planned['credit@max']['caption']
    assert year != everything
    assert '📏 Past year' in year and '1 yr ago' in year
    assert '📏 Since 2016' in everything and 'Nov 2016' in everything
    assert 'Stress line' in year and 'wider only 1 day in 5' in year  # the line is explained once, briefly
    assert planned['vix@3m']['caption'] != planned['vix@5y']['caption']
