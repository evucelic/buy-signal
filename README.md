# BuySignal

A Telegram bot that helps judge an entry point for long-term ETF purchases. Each day it reports a five-condition checklist with its data dates. It reads the owner's Interactive Brokers account (read only) to show how the portfolio is doing and which whole shares bring it back to a target mix, fees included. It never places orders and does not estimate a probability of profit.

## The checklist

The headline is how many of five conditions pass, e.g. `3 of 5`.

| Condition | Passes when |
|---|---|
| Fear | VIX at least 22; 30 or more is shown as high fear |
| Fed | "Higher" leads or ties at none of the nearest, six-month and one-year CME FedWatch meetings |
| Margin | FINRA margin debt fell over the last `MARGIN_DELEVERAGE_MONTHS` (1) month |
| Dip | SPY at least 5% below its highest adjusted close of the prior 63 sessions |
| EPS | At least 3 of the 5 largest sampled industries have positive forecast next-year earnings growth |

A missing or stale condition cannot pass and stays in the denominator: the headline then reads `4 of 5 incomplete`, the row shows `[?]`, and the report names it. Credit (Baa corporate yield minus the 10-year Treasury, in basis points) and the yield curve (10-year minus 3-month) are context and never count. The thresholds are the rules as set on 2026-11-05; none was optimised on outcomes. Research behind them is in [docs/research.md](docs/research.md).

Every NYSE session, the end-of-day report appends one line to `data/checklistlog.jsonl`: each condition's value, pass/fail/incomplete and data date, the count, the context readings, the SPY adjusted close and the ETF closes. Lines are never rewritten and nothing is backfilled; Fed and earnings expectations have no synchronised history, so the log only starts now.

## Reports

- **Pre-close** (16:30 Europe/Zagreb, an hour before London and Xetra close) and **end of day**: the checklist, context, index moves, and a collapsed "Data as of" block with the date of every row.
- **Pushes:** when VIX reaches 22 or SPY enters a dip, when VIX reaches 30 (at most once a day), when both clear, and when the count changes.
- Buttons open one chart panel per reading with 3M / 1Y / 5Y / All ranges.

## Commands

| Command | |
|---|---|
| `/signal` | the checklist, with chart buttons; each panel's numbers follow its 3M / 1Y / 5Y / All button |
| `/portfolio` | a deck of tabs, each a chart and its numbers (below) |
| `/whattobuy` | whole-share orders for the cash at IBKR, fees included; `/whattobuy 500` plans 500 € instead |
| `/target VUAA 55 VWCG 20 AVWS 20 CASH 5` | the target mix: held listings, and optionally cash kept uninvested |
| `/fees` | the fee schedule the planner uses; starts from IBKR's published tiered rates (read 2026-11-14); `/fees fixed`, `/fees VUAA 1.70 0.05`, `/fees FX 0 0.03` |
| `/vix` `/fedrate` `/margin` `/dip` `/sector` `/credit` `/curve` | one reading's panel |
| `/refresh`, `/status` | fresh data; bot, collector and IBKR health |

The `/portfolio` tabs:

| Tab | Chart | Numbers |
|---|---|---|
| Overview | account value against money paid in; on All, the same deposits put into the benchmark on the day they arrived | value, paid in, market gain, you against the benchmark |
| Returns | time-weighted return (deposits removed) against the benchmark | return, per year (after a year), worst fall |
| Mix | share of the account now against the target | holdings with gain on IBKR's cost basis, gap to target in euros |
| Risk | each holding's share of the money beside its share of the portfolio's swings | volatility, correlation, effective number of bets |
| Entries | each purchase against its month's average close | mean difference and the range chance alone gives |
| Fees | commissions paid, running and per order | total, share of money invested |
| Cash | cash balance over time | per currency, cash target, recent deposits |
| P/E | forward P/E by market | earnings yield over the real 10-year yield, each ETF's dip |

The benchmark is `BUYSIGNAL_BENCHMARK` (default `VWCE.DE`, Vanguard FTSE All-World, accumulating, in euros). Listings are whatever IBKR reports; before the first statement the defaults are VUAA (London, USD), VWCG and AVWS (Xetra, EUR).

## Interactive Brokers (read only)

The bot uses IBKR's Flex Web Service, which can only download reports: it cannot place orders, move money or change settings.

1. Client Portal → Performance & Reports → Flex Queries → create an **Activity Flex Query**, format XML, period **Last 365 Calendar Days**, with these sections:
   - Account Information (currency)
   - Cash Report (currency breakdown)
   - Net Asset Value (NAV) in Base (daily)
   - Open Positions (Summary)
   - Trades (Execution)
   - Cash Transactions (Deposits & Withdrawals)

   Keep the default date format. Note the Query ID.
2. Flex Queries → Flex Web Service Configuration → activate it, then generate a token. Set **Valid For IP Address** to the server's Elastic IP and choose an expiry.
3. In the server's `.env`: `IBKR_FLEX_TOKEN`, `IBKR_FLEX_QUERY_ID`, and `IBKR_FLEX_TOKEN_EXPIRES=YYYY-MM-DD` (the expiry chosen). `/status` and `/portfolio` warn 14 days before it.

The statement is read once a weekday after IBKR publishes the previous day, again every three hours until it does, and on `/portfolio` or `/whattobuy` when the stored one is behind. A statement is checked for every section before it replaces the stored holdings, and an older one is refused. Trades, daily values and deposits are kept beyond the 365-day window.

## Setup

```sh
cp .env.example .env            # Telegram token and chat ID
python -m buysignal             # scheduler and Telegram polling
docker compose up -d            # or in Docker; data/ is a mounted volume
.venv/bin/pytest -q             # offline tests
.venv/bin/python -m scripts.preview_entry_report   # synthetic preview in /tmp/buysignal-report-preview
```

Caches, the account read from IBKR (`data/portfolio.sqlite3`) and the checklist log live in `data/`, which is excluded from Git and the image. Deployment limits and resource benchmarks are in [docs/research.md](docs/research.md).
