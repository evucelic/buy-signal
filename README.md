# BuySignal

Telegram bot for judging entry points for long-term ETF purchases: a daily five-condition checklist, and a read-only view of the owner's Interactive Brokers account. It never places orders.

## Checklist

Headline: conditions passing, e.g. `3 of 5`. Thresholds as set on 2026-11-05.

| Condition | Passes when |
|---|---|
| Fear | VIX ≥ 22 (≥ 30 shown as high fear) |
| Fed | "Higher" leads or ties at none of the nearest, 6-month and 1-year FedWatch meetings |
| Margin | FINRA margin debt fell over the last `MARGIN_DELEVERAGE_MONTHS` (1) month |
| Dip | SPY ≥ 5% below its highest adjusted close of the prior 63 sessions |
| EPS | ≥ 3 of the 5 largest sampled industries have positive forecast next-year EPS growth |

Missing or stale data fails and is flagged (`4 of 5 incomplete`, `[?]`). Credit spread and yield curve are context only. Each NYSE session appends one line to `data/checklistlog.jsonl`; never rewritten or backfilled.

Reports: pre-close (16:30 Zagreb) and end of day. Pushes when VIX reaches 22 or 30, SPY enters a dip, both clear, or the count changes.

## Commands

| Command | |
|---|---|
| `/signal` | checklist with chart panels (3M / 1Y / 5Y / All) |
| `/portfolio` | account tabs, below |
| `/whattobuy [amount]` | whole-share orders for the cash at IBKR, fees included |
| `/target VUAA 55 VWCG 20 AVWS 20 CASH 5` | target mix |
| `/fees` | planner fees; `/fees fixed`, `/fees VUAA 1.70 0.05`, `/fees FX 0 0.03` |
| `/vix` `/fedrate` `/margin` `/dip` `/sector` `/credit` `/curve` | one reading |
| `/refresh`, `/status` | fresh data; health |

## Portfolio tabs

Value and Returns have 3M / 1Y / All buttons and open on All.

| Tab | Shows |
|---|---|
| Value | value vs paid in; the same deposits in each benchmark; MWR |
| Returns | TWR and MWR of you and each benchmark; deepest fall, volatility |
| Mix | now vs target, gap in euros, next buy |
| Risk | share of money vs share of variance, correlation, effective bets |
| Trades | each purchase vs its month's average close; last 12 listed |
| Cash | deposits, uninvested cash, median days to invest |
| Tax | tax on a full sale today and on coming dates; gain per purchase |
| Valuation | Shiller CAPE, excess CAPE yield and the next 10 years after similar readings |

- **TWR** removes deposits and weighs every day equally (judges the investments); **MWR** is the yearly rate that grows each deposit into today's value (judges the money). Both are per year from 365 days.
- **Benchmarks**: S&P 500 (`SXR8.DE`), FTSE All-World (`VWCE.DE`) and the target mix, each receiving your deposits on the day IBKR booked them.
- **Tax**: Croatian rules (NN 152/24): 12% on gains, FIFO, lots held over two years exempt, losses offset within the year.
- History starts the day before the first deposit.

## Interactive Brokers (read only)

Flex Web Service: the token can only download a report, and can be locked to one IP.

**1. Flex query.** Client Portal → Performance & Reports → Flex Queries → Activity Flex Query:

| Section | Options | Fields |
|---|---|---|
| Account Information | | Currency only |
| Cash Report | Currency Breakout; exclude segments and MTD/YTD | Currency, Level of Detail, Ending Cash |
| Cash Transactions | Deposits & Withdrawals; Detail | all except Description |
| Net Asset Value (NAV) in Base | exclude long/short breakout | all |
| Open Positions | Summary | all |
| Trades | Execution | all |

XML, Last 365 Calendar Days, date `yyyyMMdd`, time `HHmmss`, separator `;`, every Yes/No option No (Breakout by Day must stay No). Note the Query ID.

**2. Token.** Flex Web Service Configuration → Generate New Token, locked to the server's public IP, longest expiry. A new token cancels the old one.

**3. Server `.env`** (no quotes):

```
IBKR_FLEX_TOKEN=...
IBKR_FLEX_QUERY_ID=...
IBKR_FLEX_TOKEN_EXPIRES=2027-11-18
```

Check without storing anything:

```
docker compose run --rm --no-deps bot python -m scripts.check_ibkr [--period 2024-11-18 2025-11-17]
```

`/status` shows the statement date, token days left and Flex errors (1012 expired, 1013 wrong IP, 1014 wrong query ID, 1015 wrong token).

The statement is read each weekday, and on `/portfolio` when behind. History older than 365 days is read a year at a time (`ibkr_history`) until the account's start. Deposits are dated by IBKR's `reportDate`.

## Setup

```sh
cp .env.example .env            # Telegram token and chat ID
python -m buysignal             # or: docker compose up -d
.venv/bin/pytest -q
```

`data/` holds caches, `portfolio.sqlite3` and the checklist log; it is excluded from Git and the image.
