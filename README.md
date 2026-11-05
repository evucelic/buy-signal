# BuySignal

A Telegram entry-point aid for long-term equity investments. It reports a five-condition checklist and separate market context; it does not execute orders or estimate a probability of profit.

## Entry conditions

- **Fear:** VIX at least 22; 30 marks high fear.
- **Fed outlook:** no hike-leading or hike-tied horizon among the nearest, approximately six-month and approximately one-year CME FedWatch meetings.
- **Deleveraging:** the latest reported FINRA margin balance declined over the configured number of months.
- **Drawdown:** SPY at least 5% below the highest adjusted closing price in the prior 63 completed sessions. A rebound day does not clear the condition while the drawdown remains at least 5%.
- **Earnings:** at least three of the five largest sampled industries have positive forecast next-year earnings growth.

These thresholds describe the rules, not empirically optimized investment recommendations.

## Additional context

**Trend** compares SPY with its 200-session average and compares that average with its value 20 sessions earlier. The resulting uptrend, trend under pressure, downtrend or possible recovery is advisory.

**Selloff intensity** describes the five-session move in volatility units:

```
log(current price / price five sessions earlier)
--------------------------------------------------
prior daily log-return standard deviation × sqrt(5)
```

The standard deviation uses 20 returns ending before the five-session move. For example, with prior daily volatility of 1%, the reference size of a five-session move is about 2.24%. A 5% decline has an intensity around -2.3. This is a descriptive scale, not a normal-distribution probability or a prediction of a rebound. A flat baseline produces an unavailable reading rather than infinity. Intensity is not an additional entry gate.

**Credit** uses the daily ICE BofA US High Yield option-adjusted spread from FRED. It reports the spread and changes over five and 21 available observations. Positive changes mean widening. A 5-percentage-point spread is an explicit elevated reference; monthly moves smaller than 10 basis points in magnitude are labelled broadly stable. These are descriptive defaults. Credit and the yield curve are advisory and never add a sixth required condition.

**FedWatch changes** compare the same meeting date and current target range between successful refreshes. The report shows percentage-point probability changes and elapsed retrieval time. A new meeting, missing reference range, changed reference range, or unchanged/backwards source timestamp does not create a fabricated change. An append-only observation archive begins with the first new refresh; older caches remain readable but cannot supply missing metadata.

## Telegram reports

`/signal`, `/refresh`, watch-trigger changes and end-of-day pushes send **one numeric overview** with inline buttons. It includes drawdown, VIX and its changes, the Fed target/probability path, monthly margin debt, EPS growth count, credit spreads, yield-curve spreads and index returns. Source observations and last successful cache refreshes are distinct columns. The sent timestamp and refresh timezone are absolute; `TELEGRAM_TIMEZONE` defaults to `Europe/Zagreb` and handles daylight-saving changes.

Tap a section to open one silent chart panel. Further navigation edits that panel in place; **Close panel** removes it while keeping the overview. Index buttons switch between SPY, Nasdaq, Dow and optional EURO STOXX 50. The optional **Trend & intensity** button contains the long-term average and volatility-scaled move, with short definitions; these stay out of the main overview. Each index uses its own observations and trading sessions; differing observation dates are shown. A missing optional European feed does not invalidate US evidence. Daily bars can be in progress.

Captions, keyboards and unique charts are prepared once before the overview is sent. Button presses read the prepared views without scoring, reading price CSVs, reconstructing dataframes or plotting. An image is uploaded on its first visit; subsequent visits reuse Telegram file IDs. Text-only detail toggles update only the caption. Long collection commands run on one bounded background worker so polling can continue to handle buttons. FedWatch shows a probability chart and source time; **Probability changes** opens clearly labelled percentage-point changes with the comparison timestamp. **Estimate details** shows the Yahoo Finance analyst-consensus source, industry market caps, covered versus sampled company counts, sampled-weight coverage and analyst-count ranges. Analyst names are not supplied by this feed. Long detail captions have page buttons rather than spilling into more messages. FINRA charts use USD millions, every month is labelled, and the publication lag is explicit. Credit levels and changes use basis points consistently.

VIX reports its level, change versus the previous close, and distance from the 20-session high, marking a current high. Hourly bar highs come from the existing download. Legacy close-only caches are refreshed once to acquire highs; closing-high fallbacks are labelled explicitly.

Button data is saved in `data/reports/` (at most 32 snapshots, two MiB of JSON each, 14-day validity). Reports store prepared views and PNGs, without duplicating source histories. PNG storage is capped at 64 MiB globally and eight MiB per report; the manifest cache holds eight reports. Price histories used in preparation retain up to 500 rows. Legacy snapshots are prepared once when first opened. Old buttons keep their report's observations across bot restarts; expired buttons direct you to `/signal`. Navigation never refreshes sources or substitutes current observations into historical reports. The existing data-volume mount persists these snapshots. Only the configured Telegram chat can invoke navigation. No extra Telegram framework, plugin or service is required.

The main command menu contains the eight everyday tasks; detailed signal shortcuts remain in `/help` and the chart buttons. Individual signal commands open the same compact chart views. New messages remain paced and serialized; in-place edits use a shorter 150 ms minimum interval and respect Telegram `retry_after`. Transient edit failures retry safely, deleted panels are recreated, expired file IDs are re-uploaded from the saved PNG, and API rejection descriptions are logged with credentials redacted. After an uncertain edit failure, the next click restores the selected image explicitly. Graph failures leave the overview available.

Individual commands: `/vix`, `/dip`, `/trend`, `/fedrate`, `/margin`, `/sector`, `/credit`, `/curve`, `/whattobuy`, `/status`, `/help`.

Missing required evidence produces an **incomplete assessment** and keeps that condition in the denominator. Missing advisory evidence does not weaken otherwise complete checklist alignment. Observation dates are distinct from cache-write times; the latest daily price bar may still be in progress.

## Exchanges and purchase journal

The personal watchlist uses the confirmed listings: **VUAA.L / London / USD**, **VWCG.DE / IBIS2 (Xetra) / EUR**, and **AVWS.DE / IBIS2 (Xetra) / EUR**. Trading currencies come from those listings, separately from fund base currencies. Sources: [Vanguard VWCG listing table](https://fund-docs.vanguard.com/FTSE_Developed_Europe_UCITS_ETF_EUR_Accumulating_9681_EU_GERMAN_EN.pdf), [Avantis AVWS listings](https://www.avantisinvestors.com/ucitsetf/avantis-global-small-cap-value-ucits-etf/), [London VUAA listing](https://www.londonstockexchange.com/en/quote/etps/VUAA/vanguard-funds-plc).

An independent calendar clock sends an extra update **60 minutes before the regular close**. On normal days that is **16:30 Europe/Zagreb**, before London and Xetra close at 17:30 Zagreb time. Their coincident deadlines are combined into one message containing ETF prices/source times and the usual global overview/buttons. Exchange holidays, daylight-saving changes and London half-days use the exchange calendars. The regular Xetra close is the default; broker-dependent extended retail hours are a separate session. Sources: [LSE business days](https://www.londonstockexchange.com/equities-trading/business-days), [Xetra regular and extended hours](https://www.cashmarket.deutsche-boerse.com/cash-en/trading/trading-calendar-and-trading-hours).

A watch alert that already includes the pre-close context during that window covers the scheduled ping. Earlier alerts still leave the later pre-close snapshot due. Delivery is remembered across restarts; failures retry while the exchanges remain open. A late start can catch up before the close, and a report that finishes preparing after the close is skipped. Existing US/end-of-day reports and out-of-hours signals continue on their current schedule.

ETF quotes use five-minute Yahoo bars with individual source timestamps, a five-minute cache and daily-bar fallback. One listing's failure preserves its cached price or shows that listing as unavailable; the other ETFs and global report remain available. The extra clock does not poll market data throughout the day. `PRECLOSE_ENABLED=0` disables the extra ping; `PRECLOSE_MINUTES=60` controls the lead time.

Record purchases through Telegram, one per message:

```text
bought 10 VUAA @ 151.25 USD on 2026-11-05 fees 1.00
/buy 2 AVWS @ 25.50 EUR
bought 3 VWCG @ 60.75 yesterday
```

An omitted date means today in `TELEGRAM_TIMEZONE`; an omitted currency uses the confirmed listing's currency. Prices and quantities must be explicit; fees use the purchase currency. Use decimal points and ISO dates (`YYYY-MM-DD`). A repeated delivery of the same Telegram message cannot duplicate a purchase. No initial quantities or execution prices are assumed.

- `/portfolio`: watched ETF prices, recorded quantities, average cost and market values in each listing's currency.
- `/holdings`: recorded quantities and fee-inclusive average cost/invested amounts; currency totals stay separate.
- `/trades`: the latest 30 purchase records with IDs.
- `/voidtrade ID`: archive an incorrect purchase and recalculate the recorded position. The original record remains in the journal.

The purchase journal uses exact decimal arithmetic and SQLite transactions in `data/portfolio.sqlite3`. It is persisted by the existing data-volume mount and excluded from Git. These are manually recorded purchases; the journal starts empty until actual purchases are entered. To back it up while running, use SQLite's backup API; alternatively copy it with the bot stopped.

## Data and migration

Existing hourly price caches remain available. The new `data/marketdaily.csv` bootstraps all available adjusted daily index history and bootstraps on the first entry assessment. The latest five years are reconciled once per New York calendar day, retaining and rebasing the earlier archive. Intraday requests fetch five days and reconcile each index independently using completed overlap observations. Missing or delayed US bars get targeted single-symbol retries. Inconsistent adjustment anchors trigger a full reconciliation for that symbol only. Nasdaq, Dow or European outages retain their own last-known series and observation dates without invalidating fresh SPY evidence. A five-minute cache prevents duplicate live requests, and `/refresh` bypasses it. The NYSE calendar determines expected sessions; a prior session is valid before the next open and over holidays/weekends. A provider outage keeps valid last-known prices, tables and graphs visible, with an explicit cached-through date. Stale SPY observations cannot pass the entry gate, and keep the assessment incomplete without removing its numbers. Failed or unchanged stale responses do not advance the last successful price-refresh timestamp. A one-minute retry cooldown prevents repeated commands from hammering an unavailable price feed. Unknown price evidence does not falsely clear an existing watch.

`data/credit.csv` retains older local observations when the provider serves a shorter rolling window. FRED currently documents a three-year window for this ICE series; longer research history requires a permitted source. `data/fedwatch_history.csv` archives successful source observations separately from the replaceable current snapshot. A first comparable follow-up refresh is needed before probability changes can be shown.

Industry snapshots include covered/sample company counts, industry weights, covered/sample weight ratios and available analyst-count ranges. At least 60% of sampled constituent weight must have estimates for an industry to qualify; missing coverage leaves an indeterminate quorum incomplete. Industry observations are archived in `data/sectors_history.csv`.

The existing scheduler refreshes credit with other macro data. Failed refreshes preserve valid caches and remain visible in the refresh journal. No paid feed or Telegram plugin is required.

Sources: [CME methodology](https://www.cmegroup.com/articles/2023/understanding-the-cme-group-fedwatch-tool-methodology.html), [FRED credit series and usage notes](https://fred.stlouisfed.org/series/BAMLH0A0HYM2), [Telegram Bot API](https://core.telegram.org/bots/api).

## Run and verify

Use the existing `.env.example` for credentials. `python -m buysignal` starts the scheduler and Telegram polling; `python -m buysignal.schedule.runner` performs one tick. Docker Compose supplies Chromium and the optional FINRA browser fallback.

Run the offline regression suite:

```sh
.venv/bin/pytest -q
```

Create an offline, explicitly synthetic report preview without sending anything to Telegram:

```sh
MPLCONFIGDIR=/tmp/buysignal-mpl .venv/bin/python -m scripts.preview_entry_report
```

Open `/tmp/buysignal-report-preview/index.html`; the folder also contains the individual PNGs and Telegram HTML text. The preview uses synthetic data, not a backtest or current recommendation.


## Small-instance deployment

The default Compose service is the bot only. FINRA's official XLSX is requested directly; the old browser-clearance service is available only through the `finra-browser` profile:

```sh
docker compose --profile finra-browser up -d cfbypass
```

A FINRA challenge still falls back to that service when it is running. If the sidecar is left over from an older deployment, profile changes alone should not be assumed to have stopped it; check running containers before reclaiming its memory. Source access must be checked from the actual AWS public IP.

Defaults are a 1 GiB bot memory limit, 0.75 CPU, single-thread numerical libraries and sequential Yahoo downloads. Chromium and charts share a process-wide work lock; macro refresh passes are serialized. Chart imports are lazy, PNGs have an 8 MiB LRU budget, and 120 DPI is the default. Unchanged source snapshots reuse the existing PNG. `TELEGRAM_CHARTS=0` disables graph rendering without changing the data messages.

The Python dependency versions are pinned to the tested set. CI builds both amd64 and arm64 images; EC2 pulls its native architecture. Local credentials, caches, logs and untracked instruction files are excluded from the image build context. Routine chromedriver logging is non-verbose; set `CHROME_VERBOSE_LOG=1` for diagnostic sessions.

Offline resource check, with no network or credentials:

```sh
docker run --rm --memory=512m --cpus=0.75 --network=none \
  --entrypoint python ghcr.io/evucelic/buy-signal:latest -m scripts.benchmark_report
```

Navigation benchmark with all Telegram HTTP calls simulated (no messages are sent):

```sh
MPLCONFIGDIR=/tmp/buysignal-mpl .venv/bin/python -m scripts.benchmark_navigation
```

It reports one-time preparation, saved asset size, local callback time and first-visit versus repeat image uploads. Network latency and Telegram pacing are excluded from the local callback timing. A local production-image run with networking disabled and limits of 1 GiB / 0.75 CPU prepared ten charts in 8.0 seconds once, averaged 8.1 ms of local handling per click, and performed no repeated image uploads. The ten-view report occupied about 0.94 MB on disk; container peak memory was 243,638,272 bytes (about 232 MiB). This measures the offline report workload, not an AWS host or simultaneous live source collection.

Source access check from the deployment host, isolated from production caches and Telegram:

```sh
docker run --rm --init --memory=1g --cpus=0.75 --pids-limit=512 \
  --entrypoint python ghcr.io/evucelic/buy-signal:latest -m scripts.check_sources --browser
```

Add `--earnings` to include the constituent-estimate path. Checks print observation ranges, runtime and memory metrics. They do not start the scheduler or deliver messages.

A local amd64/Python 3.11 benchmark under a 512 MiB / 0.75 CPU cap rendered seven charts in 6.8 seconds cold and about 0.011 seconds for an unchanged cached report. Process peak RSS was approximately 164 MiB; these figures describe that offline workload, not the full Chromium workload or an EC2 instance measurement.

Routine Telegram reports use clinical values and source metadata. Method explanations remain in this document rather than being repeated in each report.

The isolated production-image source check also passed at 1 GiB / 0.75 CPU, including the CME browser path: 1,255 adjusted daily price sessions, 784 credit observations, 357 monthly FINRA observations and all three FedWatch horizons. Total cgroup memory peak was 516,354,048 bytes (about 492 MiB) during that run. This is a measured container workload; it is not a measurement of the AWS host or a guarantee about future page sizes.

The maximum-history bootstrap was also checked directly: 8,498 SPY sessions from 1993-01-29, 14,053 Nasdaq sessions from 1971-02-05 and 8,771 Dow sessions from 1992-01-02, all through 2026-11-02. The combined CSV was 689,440 bytes. Subsequent intraday downloads remain five-day updates; historical coverage does not increase the chart-cache budget or displayed signal lookback.
