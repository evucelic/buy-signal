# BuySignal

A Telegram bot that helps judge an entry point for long-term ETF purchases. Each day it reports a five-condition checklist with its data dates, and plans whole-share purchases against a target mix, fees included. It never places orders and does not estimate a probability of profit.

## The checklist

The headline is how many of five conditions pass, e.g. `3 of 5`.

| Condition | Passes when |
|---|---|
| Fear | VIX at least 22; 30 or more is shown as high fear |
| Fed | "Higher" leads or ties at none of the nearest, six-month and one-year CME FedWatch meetings |
| Margin | FINRA margin debt fell over the last `MARGIN_DELEVERAGE_MONTHS` (1) month |
| Dip | SPY at least 5% below its highest adjusted close of the prior 63 sessions |
| EPS | At least 3 of the 5 largest sampled industries have positive forecast next-year earnings growth |

A missing or stale condition cannot pass and stays in the denominator: the headline then reads `4 of 5 incomplete`, the row shows `[?]`, and the report names it. Credit (Baa corporate yield minus the 10-year Treasury, in basis points) and the yield curve (10-year minus 3-month) are context and never count. The thresholds are the rules as set on 2026-11-05; none was optimised on outcomes.

Every NYSE session, the end-of-day report appends one line to `data/checklistlog.jsonl`: each condition's value, pass/fail/incomplete and data date, the count, the context readings, the SPY adjusted close and the ETF closes. Lines are never rewritten and nothing is backfilled; Fed and earnings expectations have no synchronised history, so the log only starts now.

## Reports

- **Pre-close** (16:30 Europe/Zagreb, an hour before London and Xetra close) and **end of day**: the checklist, context, index moves, and a collapsed "Data as of" block with the date of every row.
- **Pushes:** when VIX reaches 22 or SPY enters a dip, when VIX reaches 30 (at most once a day), when both clear, and when the count changes.
- Buttons open one chart panel per reading with 3M / 1Y / 5Y / All ranges.

## Commands

| Command | |
|---|---|
| `/signal` | the checklist, with chart buttons |
| `/cash 500` | whole-share orders for that many euros, fees included |
| `/whattobuy` | your mix, where new cash goes, forward P/E and what each market earns over the real yield |
| `/portfolio` | ETF prices and recorded positions |
| `/buy 10 VUAA @ 151.25` | record a purchase (`on 2026-11-05`, `fees 1.00`, a currency are optional); `bought …` also works |
| `/target VUAA 60 VWCG 20 AVWS 20` | set the target mix |
| `/fees` | broker fees; starts from Interactive Brokers' published tiered rates (read 2026-11-14); `/fees fixed`, `/fees VUAA 1.70 0.05` |
| `/entries` | each purchase price against that listing's average close in the same month |
| `/trades`, `/holdings`, `/voidtrade ID` | purchase history, positions, correct a record |
| `/vix` `/fedrate` `/margin` `/dip` `/sector` `/credit` `/curve` | one reading's panel |
| `/refresh`, `/status` | fresh data; bot and collector health |

Listings: VUAA (London, USD), VWCG and AVWS (Xetra, EUR).

## Setup

```sh
cp .env.example .env            # Telegram token and chat ID
python -m buysignal             # scheduler and Telegram polling
docker compose up -d            # or in Docker; data/ is a mounted volume
.venv/bin/pytest -q             # offline tests
.venv/bin/python -m scripts.preview_entry_report   # synthetic preview in /tmp/buysignal-report-preview
```

Caches, the purchase journal (`data/portfolio.sqlite3`) and the checklist log live in `data/`, which is excluded from Git and the image.
