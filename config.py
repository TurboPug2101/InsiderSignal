"""All constants, URLs, headers, and configuration for the Smart Money Tracker."""

import os
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent / ".env")

# --- Groq AI ---
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = "openai/gpt-oss-20b"

# --- Paths ---
BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "smart_money.db"   # only used locally; Render uses Turso

# --- NSE Base ---
NSE_BASE_URL = "https://www.nseindia.com"

# --- NSE API Endpoints ---
NSE_INSIDER_TRADING_URL = (
    "https://www.nseindia.com/api/corporates-pit"
    "?index=equities&from_date={from_date}&to_date={to_date}"
)
NSE_SAST_URL = (
    "https://www.nseindia.com/api/corporate-sast-reg29"
    "?from_date={from_date}&to_date={to_date}"
)
NSE_BULK_BLOCK_URL = "https://www.nseindia.com/api/snapshot-capital-market-largedeal"
NSE_BULK_BLOCK_HISTORICAL_URL = (
    "https://www.nseindia.com/api/historical/bulk-deals"
    "?from={from_date}&to={to_date}"
)
NSE_BLOCK_DEAL_URL = "https://www.nseindia.com/api/block-deal"
NSE_FII_DII_URL = "https://www.nseindia.com/api/fiidiiTradeReact"
NSE_SHAREHOLDING_MASTER_URL = (
    "https://www.nseindia.com/api/corporate-share-holdings-master"
    "?index=equities&symbol={symbol}"
)
NSE_SHAREHOLDING_URL = (
    "https://www.nseindia.com/api/corporate-share-holdings"
    "?symbol={symbol}&industry=-&quarterEnding={quarter}"
)

# --- NSE Referers (must match the page that triggers each API call) ---
REFERER_INSIDER = "https://www.nseindia.com/companies-listing/corporate-filings-insider-trading"
REFERER_SAST = "https://www.nseindia.com/companies-listing/corporate-filings-regulation-29"
REFERER_DEALS = "https://www.nseindia.com/report-detail/display-bulk-and-block-deals"
REFERER_FII_DII = "https://www.nseindia.com/reports/fii-dii"
REFERER_SHAREHOLDING = "https://www.nseindia.com/companies-listing/corporate-filings-shareholding-pattern"

# --- BSE Fallback Endpoints ---
BSE_INSIDER_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/CorporateAction/w"
    "?strSearch=insidertrading&dtFromDate={from_date}&dtToDate={to_date}&strScrip=&strName="
)
BSE_BULK_BLOCK_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/DefaultData/w"
    "?Ession=blkdeal&Ession2=bulk&fmdt={from_date}&todt={to_date}"
)

# --- AMFI ---
AMFI_NAV_URL = "https://www.amfiindia.com/spages/NAVAll.txt"

# --- Date Formats ---
NSE_DATE_FORMAT = "%d-%m-%Y"       # DD-MM-YYYY (API params)
NSE_DISPLAY_FORMAT = "%d-%b-%Y"    # DD-Mon-YYYY (in response data)
BSE_DATE_FORMAT = "%d/%m/%Y"       # DD/MM/YYYY

# --- Scraper Settings ---
DEFAULT_BACKFILL_DAYS = 30
COOKIE_TTL_SECONDS = 300           # 5 minutes
REQUEST_RATE_LIMIT_SECONDS = 1.0   # min seconds between requests
REQUEST_TIMEOUT_SECONDS = 15

# --- Session Headers ---
NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

# --- Signal Thresholds ---
INSIDER_HIGH_VALUE_INR = 10_000_000    # ₹1 Crore
BLOCK_DEAL_HIGH_VALUE_INR = 50_000_000 # ₹5 Crore
FII_ROLLING_SIGNAL_CR = 5_000          # ₹5000 Crore over 5 days
FII_ROLLING_DAYS = 5

# --- Nifty 200 sample watchlist (subset for shareholding scraper) ---
# --- Screener.in ---
SCREENER_BASE_URL = "https://www.screener.in"
SCREENER_COMPANY_URL = "https://www.screener.in/company/{symbol}/consolidated/"
SCREENER_FALLBACK_URL = "https://www.screener.in/company/{symbol}/"
SCREENER_RATE_LIMIT_SECONDS = 1.5
SCREENER_TIMEOUT_SECONDS = 20

# --- Clustering ---
CLUSTER_WINDOW_DAYS = 30
CLUSTER_MIN_SCORE = 30
CLUSTER_MEDIUM_THRESHOLD = 30
CLUSTER_HIGH_THRESHOLD = 50
CLUSTER_ELITE_THRESHOLD = 70

CLUSTER_WEIGHTS = {
    "INSIDER_PROMOTER": 30,
    "INSIDER_KMP": 15,
    "SAST_ACQUISITION": 25,
    "BLOCK_DEAL_BUY": 20,
    "BULK_DEAL_BUY": 15,
    "MF_ACCUMULATION": 20,
}

# --- Promoter streaks ---
STREAK_WINDOW_DAYS = 90
STREAK_MIN_INSIDERS = 2
STREAK_ELITE_VALUE_THRESHOLD = 100_000_000  # ₹10 Cr

VALID_BUY_MODES = {"Market Purchase", "Open Market", "On Market"}

# --- Fundamentals ---
FUNDAMENTALS_STALE_DAYS = 7
FUNDAMENTALS_LOOKBACK_DAYS = 90

# --- Watchlist ---
WATCHLIST_SYMBOLS = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
    "HINDUNILVR", "SBIN", "BHARTIARTL", "ITC", "KOTAKBANK",
    "LT", "AXISBANK", "BAJFINANCE", "ASIANPAINT", "MARUTI",
    "TITAN", "SUNPHARMA", "WIPRO", "ONGC", "NTPC",
    "POWERGRID", "ULTRACEMCO", "NESTLEIND", "TATAMOTORS", "HCLTECH",
    "TECHM", "M&M", "JSWSTEEL", "BAJAJFINSV", "DRREDDY",
]
