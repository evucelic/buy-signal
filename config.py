"""Thresholds, tickers, data-source IDs, and file paths for all indicators."""

import os
from pathlib import Path

# --- Paths -------------------------------------------------------------------
ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"

VIX_CSV = DATA_DIR / "vixdata.csv"
FEDWATCH_CSV = DATA_DIR / "fedwatch.csv"
MARGIN_DEBT_CSV = DATA_DIR / "margindebt.csv"
SECTORS_CSV = DATA_DIR / "sectors.csv"
MARKET_CSV = DATA_DIR / "market.csv"
YIELD_CURVE_CSV = DATA_DIR / "yieldcurve.csv"
VALUATIONS_CSV = DATA_DIR / "valuations.csv"

# --- Tickers -----------------------------------------------------------------
VIX_TICKER = "^VIX"
VIX_INTERVAL = "1h"
VIX_LOOKBACK_DAYS = 729   # Yahoo caps 1h intraday history at ~730 days

# --- VIX thresholds (#1): higher VIX = more fear = potential buying dip -------
VIX_STRONG = 30.0      # strong "fear" signal
VIX_SOFT = 22.0        # soft signal band
VIX_OPTIMISTIC = 10.0  # very low / complacent
VIX_WINDOW_START_HOUR_CT = 2   # VIX starts moving ~2am Central
VIX_WINDOW_END_HOUR_CT = 20    # ...and is done moving by ~8pm Central

# --- Fed rate (#2): CME FedWatch ---------------------------------------------
FEDWATCH_URL = "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html"

# --- Fetch hardening (anti bot-detection / rate-limiting) --------------------
FETCH_JITTER_SEC = (2.0, 12.0)  # random pause before an external fetch, so it isn't periodic
SCRAPE_RETRIES = 3
SCRAPE_BACKOFF_SEC = 5.0        # base for exponential backoff between scrape retries
# CME FedWatch's page is JS-heavy and has been observed taking close to (and sometimes past)
# 60s to finish rendering in production; give it real headroom instead of timing out mid-load.
FEDWATCH_PAGE_LOAD_TIMEOUT_SEC = 120
# Recent desktop-Chrome UAs only: must match the real browser or detection worsens.
USER_AGENTS = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
)

# --- FINRA margin debt (#3) --------------------------------------------------
MARGIN_STATS_URL = "https://www.finra.org/rules-guidance/key-topics/margin-accounts/margin-statistics"
MARGIN_XLSX_URL = "https://www.finra.org/sites/default/files/2021-03/margin-statistics.xlsx"
MARGIN_SHEET_NAME = "Customer Margin Balances"
MARGIN_DELEVERAGE_MONTHS = 1    # min consecutive months of decrease = deleveraging
MARGIN_REFRESH_WINDOW_DAY = 21  # FINRA's stated "third week"; past this, expect a newer release
# finra.org sits behind Cloudflare Turnstile; the cfbypass compose service solves it and hands
# back clearance cookies (collectors/margin_debt.py). CF_BYPASS_HOST is the compose service
# name in production (docker-compose.yml sets it), localhost for local/bare-metal dev.
CF_BYPASS_HOST = os.environ.get("CF_BYPASS_HOST", "localhost")
CF_BYPASS_PORT = 8000
CF_BYPASS_URL = f"http://{CF_BYPASS_HOST}:{CF_BYPASS_PORT}"
# cfbypass solving the Cloudflare Turnstile challenge has been observed taking 90-120s+ in
# production, so give it real headroom instead of timing out mid-solve.
CF_BYPASS_TIMEOUT_SEC = 180

# --- Market index % change (#6): daily/weekly/monthly, dip watch -------------
INDEX_TICKERS = {"SPY": "SPY", "NASDAQ": "^IXIC", "DOW": "^DJI"}
MARKET_PRIMARY_INDEX = "SPY"
MARKET_INTERVAL = "1h"              # same cadence as VIX, for live intraday daily-change tracking
MARKET_LOOKBACK_DAYS = 90           # bar history fetched (buffer over monthly lookback)
MARKET_WEEKLY_LOOKBACK_DAYS = 5     # trading days
MARKET_MONTHLY_LOOKBACK_DAYS = 21   # trading days
MARKET_DIP_THRESHOLD = -0.005       # -0.5% or worse daily change on MARKET_PRIMARY_INDEX counts as a dip
MARKET_GROWTH_THRESHOLD = 0.0055    # +0.55% or better daily change counts as growth

# --- Leading industries (#4/#5): top industries by market cap, earnings growth ---
SECTOR_TOP_N = 5              # how many top industries (by market cap, across all sectors) to consider
SECTOR_GROWTH_QUORUM = 3      # at least this many of the top N must have positive forward earnings growth
SECTOR_TOP_CONSTITUENTS = 8   # top-weighted companies per industry sampled for earnings growth

# --- Yield curve (#7): 10y-3m Treasury spread, advisory-only recession context ---
FRED_GRAPH_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"  # no API key needed
YIELD_CURVE_SERIES = "T10Y3M"
YIELD_CURVE_STEEP = 1.0            # monthly avg spread (pp) at/above this = supportive regime
YIELD_CURVE_DEEP_INVERSION = -1.0  # two consecutive monthly avgs at/below this = deep inversion

# --- Equity opportunity analyzer (#8): which segment looks attractive ---------
# Segments compared on valuations. Trailing AND forward P/E both come from MSCI index
# pages (one provider, one methodology; true next-year consensus "P/E Fwd"), scraped with
# Selenium since the site sits behind a JS challenge that blocks plain requests. MSCI
# updates these monthly (factsheet cadence). Rate support is forward-looking: CME FedWatch
# expectations (fedwatch.csv), not historical Fed moves.
OPPORTUNITY_SEGMENTS = {
    "sp500": {
        "label": "MSCI USA",
        "msci_code": "984000",
    },
    "world_small": {
        "label": "World Small Cap",
        "msci_code": "106230",  # broad index: drives the small-cap decision matrix
    },
    "world_small_value": {
        "label": "World SC Value",
        "msci_code": "139549",  # MSCI World Small Cap Value Weighted, closest index to AVWS
    },
    "europe": {
        "label": "Europe",
        "msci_code": "990500",
    },
}
MSCI_INDEX_URL = "https://www.msci.com/indexes/index/{code}"
MSCI_PAGE_LOAD_TIMEOUT_SEC = 60
MSCI_RENDER_WAIT_SEC = 15            # JS challenge + SPA render time before reading the page
SMALL_CAP_DISCOUNT_BANDS = (0.10, 0.20, 0.30)  # fwd-P/E discount vs MSCI USA: little / mild / candidate / investigate
SMALL_CAP_EASING_OBS = 1             # FedWatch horizons (from nearest) expected to ease, for small-cap rate support
VALUATION_Z_MIN_OBS = 12             # unique monthly MSCI observations needed before z-scores are shown

# --- Runner cadence -----------------------------------------------------------
TICK_INTERVAL_SEC = 3600  # 1h between ticks in continuous mode (runner.py --loop); matches VIX/market's cadence
DAILY_REPORT_HOUR_CT = 20  # send the end-of-day Telegram report at 8pm Central
