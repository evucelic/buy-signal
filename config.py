"""Thresholds, tickers, data-source IDs, and file paths for all indicators."""

from pathlib import Path

# --- Paths -------------------------------------------------------------------
ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"

VIX_CSV = DATA_DIR / "vixdata.csv"
FEDWATCH_CSV = DATA_DIR / "fedwatch.csv"
MARGIN_DEBT_CSV = DATA_DIR / "margindebt.csv"
SECTORS_CSV = DATA_DIR / "sectors.csv"
MARKET_CSV = DATA_DIR / "market.csv"

# --- Tickers -----------------------------------------------------------------
VIX_TICKER = "^VIX"
VIX_INTERVAL = "1h"
VIX_LOOKBACK_DAYS = 729   # Yahoo caps 1h intraday history at ~730 days

# --- VIX thresholds (#1): higher VIX = more fear = potential buying dip -------
VIX_STRONG = 30.0      # strong "fear" signal
VIX_SOFT = 22.0        # soft signal band
VIX_OPTIMISTIC = 10.0  # very low / complacent

# --- Fed rate (#2): CME FedWatch ---------------------------------------------
FEDWATCH_URL = "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html"
FEDWATCH_HORIZON_MONTHS = 12   # weigh meetings within ~1y of the first meeting
FED_EASE_BPS = -25.0           # tilt <= this -> easing (+1)
FED_HIKE_BPS = 12.5            # tilt >= this -> hiking (-1); between = flat (0)
# Current target rate = midpoint of FRED's target-range bounds (no API key).
FRED_TARGET_RANGE_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DFEDTARU,DFEDTARL"

# --- Fetch hardening (anti bot-detection / rate-limiting) --------------------
FETCH_JITTER_SEC = (2.0, 12.0)  # random pause before an external fetch, so it isn't periodic
SCRAPE_RETRIES = 3
SCRAPE_BACKOFF_SEC = 5.0        # base for exponential backoff between scrape retries
# Recent desktop-Chrome UAs only — must match the real browser or detection worsens.
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
# finra.org sits behind Cloudflare Turnstile; a local Docker service solves it and
# hands back clearance cookies (collectors/margin_debt.py, runner.py starts it on demand).
CF_BYPASS_CONTAINER = "cfbypass"
CF_BYPASS_IMAGE = "ghcr.io/sarperavci/cloudflarebypassforscraping:latest"
CF_BYPASS_PORT = 8000
CF_BYPASS_URL = f"http://localhost:{CF_BYPASS_PORT}"

# --- Market index % change (#6): daily/weekly/monthly, dip watch -------------
INDEX_TICKERS = {"SPY": "SPY", "NASDAQ": "^IXIC", "DOW": "^DJI"}
MARKET_PRIMARY_INDEX = "SPY"
MARKET_INTERVAL = "1h"              # same cadence as VIX, for live intraday daily-change tracking
MARKET_LOOKBACK_DAYS = 90           # bar history fetched (buffer over monthly lookback)
MARKET_WEEKLY_LOOKBACK_DAYS = 5     # trading days
MARKET_MONTHLY_LOOKBACK_DAYS = 21   # trading days
MARKET_DIP_THRESHOLD = -0.005       # -0.5% or worse daily change on MARKET_PRIMARY_INDEX counts as a dip

# --- Leading industries (#4/#5): top industries by market cap, earnings growth ---
SECTOR_TOP_N = 5              # how many top industries (by market cap, across all sectors) to consider
SECTOR_GROWTH_QUORUM = 3      # at least this many of the top N must have positive forward earnings growth
SECTOR_TOP_CONSTITUENTS = 8   # top-weighted companies per industry sampled for earnings growth

# --- Runner cadence -----------------------------------------------------------
TICK_INTERVAL_SEC = 3600  # 1h between ticks in continuous mode (runner.py --loop); matches VIX/market's cadence

# --- Signal combination: weights for the weighted-average score --------------
SIGNAL_WEIGHTS = {
    "vix": 1.0,
    "fed_rate": 1.0,
    "margin_debt": 1.0,
    "sector": 1.0,
    "market_dip": 1.0,
}
