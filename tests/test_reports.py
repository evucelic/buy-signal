"""Readable report sections, bounded Telegram HTML and graphs from exact snapshots."""

from datetime import datetime, timezone
from html.parser import HTMLParser
from io import BytesIO
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
from PIL import Image

from buysignal.analysis.signal_charts import render_signal_chart
from buysignal.signals.base import SubSignal
from buysignal.signals.buy_signal import BuySignal
from buysignal.signals import market_signal, trend_signal
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


def test_report_groups_context_with_drawdown_and_keeps_advisories_out_of_count():
    market = SubSignal("market_dip", -.07, "dip", "Seven percent below high", passes=True)
    trend = SubSignal("trend", 0, "uptrend", "Rising trend", advisory=True)
    credit = SubSignal("credit", 4, "widening", "Spreads widening", advisory=True)
    result = BuySignal(1, "strong", 1, [market, trend, credit])
    report = render.signal_report(result, [], NOW)
    assert len(report.messages) == 9
    assert "1/1 entry conditions" in report.messages[0].text
    assert report.messages[1].key == "market_dip"
    assert "Seven percent" in report.messages[1].text and "Rising trend" in report.messages[1].text
    assert all(len(message.text.encode("utf-16-le")) // 2 < 4096 for message in report.messages)
    for message in report.messages:
        plain(message.text)


def test_missing_required_data_remains_unknown_in_summary():
    result = BuySignal(0, "incomplete", 0, [], missing_signals=["fed_rate"])
    report = render.signal_report(result, [], NOW)
    assert "Incomplete entry assessment" in report.messages[0].text
    assert "❔ Fed Rate: unknown" in report.messages[0].text
    assert "0/1 entry conditions" in report.messages[0].text


def test_detailed_report_keeps_explanations_as_well_as_the_table():
    signal = SubSignal("credit", 4, "stable", "What this measures & why", table="Spread  Change\n4.0     +2bp", advisory=True,
                       observed_at="2026-10-30", footer="Publication lag matters")
    report = render.signal_report(BuySignal(0, "none", 0, [signal]), [], NOW)
    part = next(p for p in report.messages if p.key == "credit")
    assert "What this measures &amp; why" in part.text
    assert "<pre>" in part.text and "Publication lag matters" in part.text
    assert "Source observation: 2026-10-30" in part.text


def test_long_industry_names_are_split_into_numbered_valid_messages():
    signal = SubSignal("sector", 1, "growing", " | ".join(["A & B " * 200] * 10), passes=True)
    report = render.signal_report(BuySignal(1, "strong", 1, [signal]), [], NOW)
    assert len(report.messages) > 9
    for index, part in enumerate(report.messages, 1):
        assert f"{index}/{len(report.messages)}" in part.text
        assert len(part.text.encode("utf-16-le")) // 2 < 4096
        plain(part.text)


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
    assert image.width >= 1000 and image.height >= 800
    assert len(png) < 10_000_000


def test_absent_history_does_not_produce_a_fabricated_chart():
    assert render_signal_chart(SubSignal("vix", 25, "soft", "")) is None


def test_routine_report_is_clinical_without_repeated_coaching():
    from scripts.preview_entry_report import sample_assessment
    report = render.signal_report(sample_assessment(), [], NOW)
    text = '\n'.join(part.text for part in report.messages).lower()
    for phrase in ('probability of profit', 'safe bottom', 'room to consider', 'chance of a rebound', 'investigate before acting'):
        assert phrase not in text
    assert 'coverage' in text
    assert 'source observation' in text


def test_valuation_report_keeps_facts_without_allocation_advice(make_opportunity):
    opportunity = make_opportunity()
    opportunity.verdict = 'S&P 500 default; investigate before acting'
    text = render.opportunity_message(opportunity, 1)
    assert '20.0' in text and '15.0' in text
    assert 'Verdict:' not in text
    assert 'investigate before acting' not in text
