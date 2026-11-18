# Research and benchmarks

Studies and measurements run from 2026-11-12 to 2026-11-14, kept as written. They were done while the bot showed a fear and macro read instead of the checklist; some readings they mention are not in the bot: jobs, the excess bond premium, recession bets, weekly claims, the real dollar and the US stock share.

## Evidence

A local study replayed fixed entry policies over real SPY history with identical cash and dates, T-bill interest on idle cash, decisions on a close executed at the next close, and a 63-session deadline.

Run on 2026-11-12 over 1993-01-29 to 2026-11-12 (399 monthly cash arrivals), shares acquired relative to investing immediately:

| Policy | Mean | 90% interval | Forced at deadline | Better when it waited |
|---|---|---|---|---|
| Three equal monthly purchases | -0.56% | -0.94% to -0.12% | n/a | n/a |
| Wait for SPY dip rule | -0.54% | -1.09% to +0.03% | 35% | 50% |
| Wait for VIX ≥ 22 | -0.75% | -1.31% to -0.20% | 38% | 42% |
| Wait for dip and VIX | -1.07% | -1.67% to -0.43% | 49% | 41% |
| Wait for dip or VIX | -0.24% | -0.73% to +0.24% | 25% | 52% |
| Wait for dip, VIX and falling margin debt | -1.49% | -2.26% to -0.67% | 69% | 35% |

The sign is the same for arrivals before and from 2010, and for 21- and 126-session deadlines ("dip or VIX" with a 126-session deadline is the only variant at zero). The median deepest fall in the 63 sessions after buying was -2.9% for immediate investment and -2.9% to -3.5% for the waiting policies. One year after the dip rule switched on (56 episodes) SPY's median change was +15.2%, against +14.4% for any day.

What this does and does not establish: on this history, a condition being unmet was not a reason to hold new cash back, and a condition being met did not mark a better-than-usual entry. It covers one asset over a period that mostly rose, uses closes only, and its 63-session windows overlap. The Fed and earnings conditions cannot be replayed because no synchronized history of expectations exists yet, so those two readings remain untested. This result is why the checklist no longer gates anything: the same readings are now context beside the fear and macro read.

### Where the stakes are

Measured locally on 2026-11-14 from daily closes (SPY from 1993, VGK from 2005, IJS from 2000 as long stand-ins; the three owned listings over their own shorter histories):

| Question | Size |
|---|---|
| A purchase on a random day against that month's average close | standard deviation 1.7% to 2.6% |
| Buying at the month's lowest close, known only afterwards | 2.4% to 3.5% below the average, median |
| Best minus worst calendar-year return of US large, Europe, US small value, 2006 to 2025 | median 15.1%, range 5.6% to 31.6% |
| Fixed 1.25 fee per trade on 300 of monthly cash: three purchases, or one | 1.25% or 0.42% of the cash |

Telling a real timing edge of 0.5% per purchase from luck takes about 60 monthly purchases (five years); 0.25% takes about 240. This is why the `/portfolio` Entries tab reports the chance range beside the mean.

Sending each month's cash to the ETF furthest below a 60/20/20 mix, against splitting it 60/20/20, over the same 2005 to 2026 stand-ins: the mix stayed within 1.0% of target on average (largest miss 3.9%) instead of drifting 5.9% on average (to 72/12/16 by the end), with a third of the trades. It ended with 3.0% less money before fees, because it kept buying the two that lagged the US over this period. It is a way to hold a chosen mix cheaply, and on this history it cost return; nothing here says which mix to choose.

### Forward readings

Four readings are shown as context and change no read. Each was checked locally on 2026-11-14 against what followed; `collectors/outlook.py` holds the definitions.

| Reading | Where | Check on history |
|---|---|---|
| Recession next year, bets (Kalshi, settled on two straight quarters of falling GDP) | overview | No recession has been settled yet, so no test. Fed staff (FEDS 2026-010) found the same exchange's rate and inflation prices at least as accurate as futures and surveys. |
| Weekly claims: 26-week average insured unemployment rate over its low of the prior 52 weeks | overview | Reached 0.20 in all seven recessions since 1971, before the monthly jobs reading in five, by one to three months. It marks a recession that has begun; the S&P 500 was already 1% to 25% below its high. |
| Dollar vs euro, real: dollars per euro after both price levels, against its own average | `/whattobuy` | 1971 to 2026 with the D-mark before 1999: when the dollar was dearest the euro then rose 4.5% a year over five years (in 83% of months), when cheapest it fell 2.5% a year. Nine separate five-year spans. |
| US stock share: shares over shares plus debt held by investors (Fed Z.1) | `/whattobuy` | 1946 to 2016 starts: correlation with the next ten years' return -0.83; fitted only on decades already finished it beat the running average (error 3.5% a year against 5.6%), and missed the decade from 2015 by nine points. The latest value is outside all history. |

Rank is the share of the reading's own history below today's value. Tried and left out: the options-implied premium (VIX squared; a valid floor on average, no more informative than VIX itself), the value spread on book-to-price breakpoints (no relation to the next five years of small value, 1945 to 2021), VIX minus realised volatility, and the vacancy-based recession rule.

### Fear with the macro picture sound or deteriorating

A second local study used the live definitions above on the same SPY history (run 2026-11-13, 1993-01-29 to 2026-11-12, jobs known from the 10th of the following month, credit from the previous session).

What followed every day in each state (medians; these are the figures the overview quotes):

| State | Share of days | Calendar years | After 3 months | After 6 months | After 1 year | Up after 6 months | Deepest fall within 6 months |
|---|---|---|---|---|---|---|---|
| Any day | 100% | 34 | +3.7% | +6.9% | +14.4% | 76% | -3.8% |
| Calm, macro sound | 63% | 30 | +3.5% | +7.0% | +14.6% | 83% | -3.0% |
| Calm, macro deteriorating | 9% | 17 | -0.8% | -1.2% | +9.1% | 47% | -10.3% |
| Fear, macro sound | 9% | 24 | +4.9% | +10.0% | +14.6% | 75% | -6.0% |
| Fear, macro deteriorating | 19% | 16 | +5.3% | +8.3% | +16.0% | 68% | -5.6% |

Waiting for fear with a sound macro picture, instead of buying new cash on arrival, ended with fewer shares at every deadline tried (21, 42, 63 sessions: -0.44%, -0.72%, -0.90%; the 63-session 90% interval is -2.00% to +0.32%). At the first day of each of 46 fear episodes, the 37 that began with macro sound had a median +8.6% after six months and the 9 that began with macro deteriorating -1.1%; those nine fall in a handful of periods (2000, 2002, 2003, 2008, 2010, 2012).

What this does and does not establish: fear days were followed by somewhat higher six-month changes than ordinary days whatever the macro state, and the weakest state on record was a calm market with a deteriorating macro picture. Neither supports holding new cash back to wait for a state. Days in a state are heavily overlapping, so the number of calendar years is the honest sample size; one asset, closes only, a period that mostly rose.

Routine Telegram reports use clinical values and source metadata. Method explanations remain in this document rather than being repeated in each report.

The container figures above were measured before the panel redesign and have not been repeated in the production image. A local, uncapped run of the redesigned report on real histories (SPY from 1993, VIX from 1990, 29 charts at 150 DPI) spent 2.0 s on charts before the overview could be sent, had every range ready 10.6 s after the request, wrote about 2.7 MiB of PNGs and peaked at about 203 MiB RSS. That is a workstation measurement, not the image under its limits and not the AWS host.

The isolated production-image source check also passed at 1 GiB / 0.75 CPU, including the CME browser path: 1,255 adjusted daily price sessions, 784 credit observations, 357 monthly FINRA observations and all three FedWatch horizons. Total cgroup memory peak was 516,354,048 bytes (about 492 MiB) during that run. This is a measured container workload; it is not a measurement of the AWS host or a guarantee about future page sizes.

The maximum-history bootstrap was also checked directly: 8,498 SPY sessions from 1993-01-29, 14,053 Nasdaq sessions from 1971-02-05 and 8,771 Dow sessions from 1992-01-02, all through 2026-11-02. The combined CSV was 689,440 bytes. Subsequent intraday downloads remain five-day updates; historical coverage does not increase the chart-cache budget or displayed signal lookback.

## Small-instance deployment

The default Compose service is the bot only. FINRA's official XLSX is requested directly; the old browser-clearance service is available only through the `finra-browser` profile:

```sh
docker compose --profile finra-browser up -d cfbypass
```

A FINRA challenge still falls back to that service when it is running. If the sidecar is left over from an older deployment, profile changes alone should not be assumed to have stopped it; check running containers before reclaiming its memory. Source access must be checked from the actual AWS public IP.

Defaults are a 1 GiB bot memory limit, 0.75 CPU, single-thread numerical libraries and sequential Yahoo downloads. Chromium and charts share a process-wide work lock; macro refresh passes are serialized. Chart imports are lazy, PNGs have an 8 MiB LRU budget, and 150 DPI is the default (900 px wide; set `TELEGRAM_CHART_DPI=120` to return to the previous size). Unchanged source snapshots reuse the existing PNG. `TELEGRAM_CHARTS=0` disables graph rendering without changing the data messages.

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
