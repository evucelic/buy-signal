"""Build an illustrative local report and its graphs without Telegram or network access.

Run: .venv/bin/python -m scripts.preview_entry_report --output /tmp/buysignal-report-preview
All observations are synthetic and visibly labelled as such in the HTML preview.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from buysignal.analysis.signal_charts import render_signal_chart
from buysignal.collectors.fed_rate import _add_comparisons
from buysignal.signals import market_signal, trend_signal, rate_signal, margin_signal, sector_signal, credit_signal, yield_curve_signal
from buysignal.signals.base import SubSignal
from buysignal.signals.buy_signal import BuySignal
from buysignal.telegram_bot.render import signal_report
from buysignal.telegram_bot import views

NOW = datetime(2026, 11, 2, 19, 0, tzinfo=timezone.utc)


def sample_assessment() -> BuySignal:
    dates = pd.bdate_range(end="2026-11-02", periods=300)
    index = np.arange(300)
    prices = np.linspace(65, 100, 300) + 1.25 * np.sin(index * 1.1)
    prices[-6:] = [100, 98.8, 96.5, 94, 92.8, 93.4]
    frame = pd.DataFrame({"SPY": prices, "NASDAQ": prices*1.3, "DOW": prices*.9, "EURO50": prices*40}, index=dates)
    with patch.object(market_signal, "get_entry_history", return_value=frame):
        market = market_signal.score()
    trend = trend_signal.from_market(market)
    vix_history = pd.DataFrame({"date": dates, "vix": 19 + 3*np.sin(index/14)})
    vix_history.loc[vix_history.index[-6:], "vix"] = [21, 24, 28, 31, 27, 25]
    vix_history["high"] = vix_history.vix
    vix = SubSignal(
        "vix", 25, "soft", "VIX 25.0: elevated fear; below the recent high of 31.0",
        passes=True, observed_at="2026-11-02", history=vix_history,
        metrics={"vix": 25., "change": 25/27-1, "recent_peak": 31., "from_peak": 25/31-1, "at_high": False, "peak_basis": "intraday"},
        footer="Illustrative observations",
    )
    fed = pd.DataFrame([
        {"horizon": horizon, "meeting_date": pd.Timestamp(date), "prob_ease": ease, "prob_no_change": hold,
         "prob_hike": hike, "reference_lower_bps": 375, "reference_upper_bps": 400,
         "fetched_at": "2026-11-02T19:00:00Z", "source_asof": "2026-11-02T18:50:00Z"}
        for horizon, date, ease, hold, hike in (("nearest", "2026-12-09", .05, .8, .15), ("six_month", "2027-04-28", .01, .2, .79), ("one_year", "2027-10-27", .01, .1, .89))
    ])
    previous = fed.copy()
    previous["fetched_at"] = "2026-11-01T19:00:00Z"
    previous["source_asof"] = "2026-11-01T18:50:00Z"
    previous["prob_hike"] += .04
    previous["prob_no_change"] -= .04
    fed = _add_comparisons(fed, previous)
    with patch.object(rate_signal, "latest_fedwatch", return_value=fed):
        rate = rate_signal.score(asof=pd.Timestamp("2026-11-02"))
    margin = pd.DataFrame({"month": pd.date_range(end="2026-09-01", periods=30, freq="MS"), "debit_balances": np.linspace(900000, 1400000, 30)})
    margin.loc[29, "debit_balances"] = margin.debit_balances.iloc[-2] * .98
    with patch.object(margin_signal, "margin_history", return_value=margin), patch.object(margin_signal, "data_freshness", return_value=("fresh", "")):
        leverage = margin_signal.score()
    sectors = pd.DataFrame({"industry": ["Semiconductors", "Software - Infrastructure", "Internet Content & Information", "Consumer Electronics", "Drug Manufacturers - General"],
                            "market_cap": [10e12, 6e12, 5e12, 4e12, 3e12], "earnings_growth_estimate_1y": [.61, .24, .15, .08, -.05]})
    sectors["estimate_coverage"] = 1.
    sectors["covered_companies"] = 8
    sectors["sampled_companies"] = 8
    sectors["covered_weight"] = .8
    sectors["analysts_min"] = 12
    sectors["analysts_max"] = 40
    sectors["fetched_at"] = NOW.isoformat()
    with patch.object(sector_signal, "sector_performance", return_value=sectors):
        earnings = sector_signal.score()
    credit = pd.DataFrame({"date": dates, "spread": 3.5 + .15*np.sin(index/15)})
    credit.loc[credit.index[-22:], "spread"] = np.linspace(3.4, 4.1, 22)
    with patch.object(credit_signal, "credit_history", return_value=credit):
        credit_reading = credit_signal.score(asof=pd.Timestamp("2026-11-02"))
    curve = pd.DataFrame({"date": dates, "spread": np.linspace(-.7, .8, 300), "policy_spread": np.linspace(-.3, .6, 300)})
    with patch.object(yield_curve_signal, "yield_curve_history", return_value=curve):
        yield_reading = yield_curve_signal.score()
    signals = [vix, rate, leverage, market, trend, earnings, credit_reading, yield_reading]
    required = [s for s in signals if not s.advisory]
    passing = sum(s.passes for s in required)
    return BuySignal(passing/len(required), "strong" if passing == len(required) else "none", passing, signals)


def build_preview(output: Path) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    assessment = sample_assessment()
    report = signal_report(assessment, [(s.name, NOW) for s in assessment.subsignals], NOW)
    signals = {s.name: s for s in assessment.subsignals}
    cards = []
    for message in report.messages:
        image = ""
        if message.key in signals:
            png = render_signal_chart(signals[message.key])
            if png is not None:
                filename = f"{message.key}.png"
                (output / filename).write_bytes(png)
                image = f'<img src="{filename}" alt="{message.key} chart">'
        cards.append('<article><div class="text">' + message.text.replace("\n", "<br>\n") + '</div>' + image + '</article>')
    page = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>BuySignal report preview — illustrative data</title><style>
body{margin:0;background:#eaf0f6;color:#17293e;font:16px/1.6 system-ui,sans-serif}main{max-width:780px;margin:40px auto;padding:0 16px}
h1{font-size:28px;line-height:1.2}.notice{padding:16px;background:#fff5d9;border-radius:12px;margin:24px 0}
article{background:#fff;border:1px solid #dbe3ed;border-radius:18px;margin:20px 0;box-shadow:0 3px 12px #18334b08;overflow:hidden}.text{padding:24px}
pre{font-size:13px;line-height:1.7;background:#f4f7fb;padding:14px;border-radius:9px;overflow-x:auto;white-space:pre}pre br{display:none}
img{display:block;width:100%;border-top:1px solid #e3e9f0}b{font-weight:650}
</style><main><h1>Entry report · Telegram preview</h1><div class="notice"><b>Illustrative preview — all values are synthetic.</b><br>This demonstrates the message layout and charts, not today's market or a validated investment recommendation.</div>'''
    page += "\n".join(cards) + "</main></html>"
    (output / "index.html").write_text(page)
    (output / "telegram_messages.txt").write_text("\n\n".join(m.text for m in report.messages))
    return output / "index.html"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("/tmp/buysignal-report-preview"))
    args = parser.parse_args()
    print(build_preview(args.output))
