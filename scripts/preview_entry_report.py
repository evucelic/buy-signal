"""Build an illustrative local report and its graphs without Telegram or network access.

Run: .venv/bin/python -m scripts.preview_entry_report --output /tmp/buysignal-report-preview
All observations are synthetic and visibly labelled as such in the HTML preview.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from buysignal.analysis.context import describe
from buysignal.collectors.fed_rate import _add_comparisons
from buysignal.signals import market_signal, rate_signal, margin_signal, sector_signal, credit_signal, yield_curve_signal
from buysignal.signals.base import SubSignal
from buysignal.signals.buy_signal import BuySignal, assess
from buysignal.telegram_bot.render import signal_report
from buysignal.telegram_bot import prepared, views

NOW = datetime(2026, 11, 2, 19, 0, tzinfo=timezone.utc)


def sample_assessment() -> BuySignal:
    # Ten years of history behind the charts; the last 300 sessions are the ones the readings describe.
    long_dates = pd.bdate_range(end="2026-11-02", periods=2600)
    steps = np.arange(2600)
    index = np.arange(300)
    prices = np.r_[np.linspace(22, 65, 2300) * (1 + .12 * np.sin(np.arange(2300) / 90)), np.linspace(65, 100, 300) + 1.25 * np.sin(index * 1.1)]
    prices[-6:] = [100, 98.8, 96.5, 94, 92.8, 93.4]
    frame = pd.DataFrame({"SPY": prices, "NASDAQ": prices*1.3, "DOW": prices*.9, "EURO50": prices*40}, index=long_dates)
    with patch.object(market_signal, "get_entry_history", return_value=frame):
        market = market_signal.score()
    vix_history = pd.DataFrame({"date": long_dates, "vix": 19 + 3*np.sin(steps/14) + 9*np.clip(np.sin(steps/120), .7, 1) - 6.3})
    vix_history.loc[vix_history.index[-6:], "vix"] = [21, 24, 28, 31, 27, 25]
    vix_history["high"] = vix_history.vix
    vix = SubSignal(
        "vix", 25, "soft", "VIX 25.0: elevated fear; below the recent high of 31.0",
        passes=True, observed_at="2026-11-02", history=vix_history,
        metrics={"vix": 25., "change": 25/27-1, "recent_peak": 31., "from_peak": 25/31-1, "at_high": False, "peak_basis": "intraday",
                 **describe(vix_history.vix, 25., vix_history.date)},
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
    margin = pd.DataFrame({"month": pd.date_range(end="2026-09-01", periods=120, freq="MS"),
                           "debit_balances": np.linspace(500000, 1400000, 120) * (1 + .05 * np.sin(np.arange(120) / 2.5))})
    margin.loc[119, "debit_balances"] = margin.debit_balances.iloc[-2] * .98
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
    credit = pd.DataFrame({"date": long_dates, "spread": 2.0 + .35*np.sin(steps/160) + .12*np.sin(steps/23)})
    credit.loc[credit.index[-22:], "spread"] = np.linspace(1.85, 2.12, 22)
    with patch.object(credit_signal, "credit_history", return_value=credit):
        credit_reading = credit_signal.score(asof=pd.Timestamp("2026-11-02"))
    curve = pd.DataFrame({"date": long_dates, "spread": np.r_[1.2 * np.sin(np.arange(2300) / 250), np.linspace(-.7, .8, 300)],
                          "policy_spread": np.r_[.6 * np.sin(np.arange(2300) / 180), np.linspace(-.3, .6, 300)]})
    with patch.object(yield_curve_signal, "yield_curve_history", return_value=curve):
        yield_reading = yield_curve_signal.score()
    return assess([vix, rate, leverage, market, earnings, credit_reading, yield_reading])


FLEX_FIXTURE = Path(__file__).resolve().parent.parent / 'tests' / 'fixtures' / 'flex_statement.xml'


def sample_cape() -> pd.DataFrame:
    """Synthetic monthly CAPE since 1881."""
    months = pd.date_range('1881-01-01', '2026-10-01', freq='MS')
    step = np.arange(len(months))
    cape = 16 + 7 * np.sin(step / 70.) + 4 * np.sin(step / 23.) + np.clip(step - 1500, 0, None) * .08
    excess = 1 / cape - (.02 + .015 * np.sin(step / 40.))
    stocks = .02 + 1.1 * excess + .03 * np.sin(step / 11.)
    frame = pd.DataFrame({'date': months, 'cape': cape, 'excess_yield': excess, 'stocks_10y': stocks, 'bonds_10y': .02 + 0 * step})
    frame.loc[frame.date > '2016-10-01', ['stocks_10y', 'bonds_10y']] = np.nan
    return frame


def sample_personal(directory: Path):
    """The /portfolio tabs and a /whattobuy reply from the synthetic IBKR statement used by the tests."""
    import numpy as np
    from buysignal.broker import flex
    from buysignal.portfolio import panels, plan, presentation
    from buysignal.portfolio.store import PortfolioStore
    store = PortfolioStore(directory / 'portfolio.sqlite3')
    store.save_statement(flex.parse(FLEX_FIXTURE.read_bytes()), NOW)
    store.set_targets({'VUAA': 55, 'VWCG': 20, 'AVWS': 20, 'CASH': 5})
    index = pd.bdate_range('2025-01-01', NOW.date())
    wave = np.sin(np.arange(len(index)) / 9.)
    closes = {'VUAA': pd.Series(np.linspace(100, 121, len(index)) + 3 * wave, index=index),
              'VWCG': pd.Series(np.linspace(38, 44, len(index)) + wave, index=index),
              'AVWS': pd.Series(np.linspace(23, 27, len(index)) + .6 * wave, index=index)}
    quotes = [{'symbol': s, 'price': float(c.iloc[-1]), 'observed_at': NOW.isoformat()} for s, c in closes.items()]
    positions = store.positions()
    fx = pd.Series(1.085 + .02 * np.sin(np.arange(len(index)) / 40.), index=index)
    benchmarks = {'S&P 500': pd.Series(np.linspace(560, 690, len(index)) + 14 * np.sin(np.arange(len(index)) / 13.), index=index),
                  'All-World': pd.Series(np.linspace(118, 136, len(index)) + 2.5 * np.sin(np.arange(len(index)) / 15.), index=index)}
    account = panels.Account(positions=positions, holdings=store.holdings(), cash=store.cash(), nav=store.nav(),
                             deposits=store.deposits(), trades=store.trades(), meta=store.account(), targets=store.targets(),
                             prices=plan.prices(quotes, positions), quotes=quotes, eurusd=1.085, closes=closes,
                             eurusd_history=fx, benchmarks=benchmarks, history_complete=store.history_complete(),
                             cape=sample_cape(), sleeves=[('VUAA', 22.1), ('VWCG', 14.6), ('AVWS', 11.9)],
                             real_yield=(.018, '2026-10-30'), sources=['MSCI Oct 31, 2026', 'TIPS 2026-10-30'], today=NOW.date())
    tabs = panels.draw(panels.build(account))
    orders = plan.purchase_plan(store.cash(), store.holdings(), plan.prices(quotes, positions), 1.085, store.targets())
    return tabs, presentation.orders_message(orders, source='IBKR cash 2026-11-17')


def build_preview(output: Path) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    assessment = sample_assessment()
    report = signal_report(assessment, [(s.name, NOW) for s in assessment.subsignals], NOW)
    # The same plan the bot prepares, so the preview cannot drift from what Telegram shows.
    planned, jobs = prepared.plan(assessment, 'preview', NOW.isoformat())
    files = {}
    for job in jobs:
        png = prepared.render(job)
        if png:
            files[job.key] = job.key.replace(':', '_').replace('@', '-') + '.png'
            (output / files[job.key]).write_bytes(png)
    content = {view: {'caption': item['caption'], 'image': files.get(item['chart_key']), 'keyboard': item['keyboard']}
               for view, item in planned.items()}
    from buysignal.portfolio import panels
    tabs, orders = sample_personal(output)
    for view, (caption, png) in tabs.items():
        image = None
        if png:
            image = view.replace('@', '-') + '.png'
            (output / image).write_bytes(png)
        content[view] = {'caption': caption, 'image': image, 'keyboard': panels.keyboard('preview', view)}
    payload = json.dumps({'views': content, 'keyboard': views.keyboard('preview'),
                          'portfolio': panels.keyboard('preview', panels.FIRST)}).replace('<', r'\u003c')
    page = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BuySignal · interactive preview</title><style>
body{margin:0;background:#eaf0f6;color:#17293e;font:16px/1.4 system-ui,sans-serif}*{box-sizing:border-box}main{max-width:440px;margin:24px auto;padding:0 10px}
h1{font-size:21px}.notice{color:#617085;font-size:12px;margin-bottom:18px}
article{background:#fff;border:1px solid #dbe3ed;border-radius:14px;margin:16px 0;overflow:hidden;box-shadow:0 3px 12px #18334b08}.text{padding:12px;white-space:pre-wrap}
pre{font:13px/1.5 monospace;white-space:pre;overflow-x:auto;margin:8px 0}img{display:block;width:100%}.buttons{padding:0 8px 8px}.row{display:flex;gap:4px;margin-top:5px}button{flex:1;cursor:pointer;border:0;border-radius:6px;background:#edf4fc;color:#2864dc;min-width:0;white-space:nowrap;padding:10px 4px;font:600 14px system-ui}button:hover,button:focus{background:#dceaff}b{font-weight:650}
</style><main><h1>Entry checklist</h1><div class="notice">Interactive local preview · synthetic data · nothing sent to Telegram.</div><article><div class="text">''' + report.messages[0].text + '''</div><div id="overview" class="buttons"></div></article><article id="panel" hidden><img id="chart" alt="Selected signal chart"><div id="caption" class="text"></div><div id="navigation" class="buttons"></div></article><article><div class="text"><b>/portfolio</b></div><div id="portfolio" class="buttons"></div></article><article><div class="text">''' + orders + '''</div></article></main><script id="data" type="application/json">''' + payload + '''</script><script>
const data=JSON.parse(document.getElementById('data').textContent);
function buttons(target,keyboard){target.replaceChildren();for(const row of keyboard.inline_keyboard){const r=document.createElement('div');r.className='row';for(const b of row){const el=document.createElement('button');el.textContent=b.text;el.onclick=()=>openView(b.callback_data.split(':')[2]);r.append(el)}target.append(r)}}
function openView(key){const panel=document.getElementById('panel');if(key==='close'){panel.hidden=true;return}const view=data.views[key];if(!view)return;document.getElementById('caption').innerHTML=view.caption;const img=document.getElementById('chart');img.hidden=!view.image;if(view.image)img.src=view.image;buttons(document.getElementById('navigation'),view.keyboard);panel.hidden=false;panel.scrollIntoView({behavior:'smooth',block:'start'})}
buttons(document.getElementById('overview'),data.keyboard);buttons(document.getElementById('portfolio'),data.portfolio);
</script></html>'''
    (output / 'index.html').write_text(page)
    (output / 'telegram_messages.txt').write_text(report.messages[0].text)
    return output / 'index.html'


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("/tmp/buysignal-report-preview"))
    args = parser.parse_args()
    print(build_preview(args.output))
