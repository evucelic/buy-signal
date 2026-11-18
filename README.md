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

A missing or stale condition cannot pass and stays in the denominator: the headline then reads `4 of 5 incomplete`, the row shows `[?]`, and the report names it. Credit (Baa corporate yield minus the 10-year Treasury, in basis points) and the yield curve (10-year minus 3-month) are context and never count. The thresholds are the rules as set on 2026-11-05; none was optimised on outcomes.

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

The `/portfolio` tabs (Value and Returns follow the 3M / 1Y / All buttons):

| Tab | Chart | Numbers |
|---|---|---|
| Value | account value against money paid in, and the same deposits put into each benchmark on the day IBKR booked them | start or held value, deposits, market gain, money-weighted return, you against each benchmark |
| Returns | time-weighted return (deposits removed) of you and the benchmarks; below it, the fall from the previous high | return (per year beyond 400 days), worst fall, yearly volatility |
| Mix | two rings: the account now outside, the target inside | value, share now, target, gap in euros, where new cash goes |
| Risk | each holding's share of the money beside its share of the swings | volatility, correlation, effective number of bets |
| Trades | each purchase against its month's average close | timing against chance, commissions |
| Cash | deposits over time, running total and per month; cash left uninvested | totals, last 12 months, median days to invest, cash per currency, recent deposits |
| Tax | tax a full sale would cost on each coming date at today's prices; gain per purchase | holdings, cost, gain taxed and tax-free, tax, what you keep |
| Valuation | Shiller's CAPE since 1881; every month's excess CAPE yield against the next ten years' real return | CAPE, earnings yield, real bond yield, excess yield, what followed similar months, each ETF's forward earnings yield over the 10-year TIPS |

Benchmarks: the S&P 500 (`SXR8.DE`, iShares Core S&P 500 Acc) and the FTSE All-World (`VWCE.DE`, Vanguard All-World Acc), both accumulating and quoted in euros on Xetra, and the target mix: each deposit split by the `/target` weights into the held listings, cash kept as cash, no rebalancing. Every benchmark starts with the range's opening value and receives each later deposit on the day IBKR booked it, so it answers "what if every euro had gone there instead". The target mix is the policy benchmark of Brinson, Hood and Beebower (1986); the All-World index is the usual yardstick for a global equity investor. A sector index such as the Nasdaq-100 is left out: it does not match the strategy.

Tax follows the Zakon o porezu na dohodak (NN 152/24): 12% on capital gains from financial assets, fund units included (čl. 70 st. 4); lots matched first in, first out (čl. 70 st. 5); nothing due on a lot sold more than two years after its purchase (čl. 67 st. 8); losses offset gains within the calendar year. Costs include the commission and use IBKR's exchange rate on the trade date. Shares bought before the oldest statement read are counted as taxed until the history reaches them. Sale commissions are left out.

Valuation uses Robert Shiller's `ie_data.xls` from shillerdata.com (monthly, read at most weekly). The excess CAPE yield is 1/CAPE minus the real 10-year Treasury yield (GS10 less the past ten years' inflation). "What followed" takes every month whose excess yield was within one point of today's and reports the 10th, 50th and 90th percentile of the next ten years' real annual return; overlapping windows make the month count larger than the number of independent decades, which is shown too.

Listings are whatever IBKR reports; before the first statement the defaults are VUAA (London, USD), VWCG and AVWS (Xetra, EUR).

## Interactive Brokers (read only)

The bot uses IBKR's Flex Web Service. Its token can only download reports built in Client Portal: it cannot place orders, move money or change settings, and it can be locked to one IP address. What a leak could expose is the report itself, so the query carries only what the bot reads.

### 1. The Flex query

Client Portal → Performance & Reports → Flex Queries → create an **Activity Flex Query**.

| Section | Options | Fields |
|---|---|---|
| Account Information | | **Currency only** |
| Cash Report | Currency Breakout; Exclude segments and MTD/YTD breakout | Currency, Level of Detail, Ending Cash |
| Cash Transactions | Deposits & Withdrawals; Detail | all except Description |
| Net Asset Value (NAV) in Base | Exclude long and short breakout | all (needs Report Date, Cash, Total) |
| Open Positions | Summary | all |
| Trades | Execution | all |

Delivery and general configuration: Format **XML**, Period **Last 365 Calendar Days**, Date Format yyyyMMdd, Time Format HHmmss, separator `;`, Profit and Loss **Default**, and **No** for every Yes/No option. "Breakout by Day" must stay No: it splits the report into one statement per day.

Leave out:
- Every other field of Account Information: name, address, email and account details. The bot needs only the base currency.
- Description in Cash Transactions: for a deposit it can name the sending bank.
- Any other section. Account ID and Alias can stay; IBKR prints the account number in the report header anyway.

Save the query and note its **Query ID**.

### 2. The token

Flex Queries → Flex Web Service Configuration → activate it → Generate New Token:
- **Valid For IP Address:** the server's public Elastic IP (not the private 172.31.x.x address). Check that the server sends from it: `curl -s https://checkip.amazonaws.com` on the server must print the same address, or IBKR answers with error 1013.
- **Should Expire After:** the longest offered. Note the date.

Generating a token cancels the previous one. Copy it straight into the server's `.env`, nowhere else.

### 3. The server's `.env`

No quotes and no spaces around `=` (some Docker Compose versions keep quote marks as part of the value):

```
IBKR_FLEX_TOKEN=123456789012345678901234
IBKR_FLEX_QUERY_ID=1234567
IBKR_FLEX_TOKEN_EXPIRES=2027-11-18
```

To check the query and token, run on the server (it downloads one statement and prints counts, symbols and dates, no balances, and stores nothing):

```
docker compose run --rm --no-deps bot python -m scripts.check_ibkr
```

`docker compose up -d` (or the next deploy) picks it up. `/status` then shows the statement date and the days left on the token, and warns from 14 days before expiry. If the read fails, `/status` lists the error beside `ibkr`: 1012 an expired token, 1013 a request from another IP, 1014 a wrong query ID, 1015 a wrong token.

### How it is read

The statement is read once a weekday after IBKR publishes the previous day, again every three hours until it does, and on `/portfolio` or `/whattobuy` when the stored one is behind. A deposit that cleared today therefore shows from the next day; `/whattobuy 500` plans for it meanwhile. A statement is checked for every section before it replaces the stored holdings, and an older one is refused. Trades, daily values and deposits are kept beyond the 365-day window. A statement covers at most 365 days, so older history is read separately (`ibkr_history` in `/status`): each run asks for the 365 days before the oldest stored day, using the Flex Web Service's `fd`/`td` dates, until it reaches the day the account held nothing. Deposits count on the day IBKR booked them (`reportDate`), which is when the account's value includes them. Each tab's footer gives the statement's last day, when it was read, and "adds up ✓" when holdings plus cash match IBKR's own total for that day.

## Setup

```sh
cp .env.example .env            # Telegram token and chat ID
python -m buysignal             # scheduler and Telegram polling
docker compose up -d            # or in Docker; data/ is a mounted volume
.venv/bin/pytest -q             # offline tests
.venv/bin/python -m scripts.preview_entry_report   # synthetic preview in /tmp/buysignal-report-preview
```

Caches, the account read from IBKR (`data/portfolio.sqlite3`) and the checklist log live in `data/`, which is excluded from Git and the image.
