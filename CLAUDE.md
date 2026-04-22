# Indian Stock Market Investment Signal Tracker — CLAUDE.md

## Project Overview

Build a Python CLI + web dashboard that scrapes **5 official Indian regulatory data sources** daily and consolidates them into a unified "smart money signals" view. The goal: surface insider buys, big-investor accumulation, bulk/block deals, FII/DII flows, and mutual fund portfolio changes — all from **official SEBI-mandated disclosures** — before mainstream media reports them.

**Stack:** Python 3.11+, SQLite, FastAPI (API), React or plain HTML dashboard.

---

## Architecture

```
project/
├── CLAUDE.md                  # This file
├── config.py                  # All constants, URLs, headers, DB path
├── db.py                      # SQLite schema + insert/query helpers
├── scrapers/
│   ├── __init__.py
│   ├── nse_session.py         # Shared NSE session with cookie handling
│   ├── insider_trading.py     # Source 1: Insider/Promoter trades
│   ├── sast_regulation29.py   # Source 2: 5%+ acquirer disclosures
│   ├── bulk_block_deals.py    # Source 3: Bulk & Block deals
│   ├── fii_dii.py             # Source 4: FII/DII daily activity
│   └── mf_portfolios.py       # Source 5: Mutual fund portfolios
├── scheduler.py               # Cron-like daily runner
├── api.py                     # FastAPI endpoints
├── dashboard/                 # Frontend (single HTML or React)
│   └── index.html
├── requirements.txt
└── run.py                     # Entry point
```

---

## CRITICAL: NSE Website Session Handling

NSE India blocks direct API calls. You MUST first visit a page to get cookies, then use those cookies for API calls. Every scraper must use the shared session from `nse_session.py`.

### `scrapers/nse_session.py` — Implement EXACTLY This Pattern

```python
import requests
import time

class NSESession:
    """
    Singleton session handler for NSE India.
    NSE requires:
    1. First hit the main page to get cookies (nseappid, nsit, bm_sv etc.)
    2. Then use those cookies + proper headers for API calls
    3. Rate limit: minimum 1 second between requests
    4. Refresh cookies every 5 minutes (they expire)
    """

    BASE_URL = "https://www.nseindia.com"
    
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(self.HEADERS)
        self.last_request_time = 0
        self.cookie_time = 0

    def _refresh_cookies(self):
        """Visit NSE homepage to get fresh cookies."""
        now = time.time()
        if now - self.cookie_time < 300:  # Cookies valid for 5 min
            return
        self.session.get(self.BASE_URL, timeout=10)
        self.cookie_time = time.time()

    def get(self, url: str) -> dict:
        """Make a rate-limited, cookie-authenticated GET request."""
        self._refresh_cookies()
        # Rate limit: 1 request per second minimum
        elapsed = time.time() - self.last_request_time
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)
        
        # Set Referer to the page that would normally trigger this API call
        self.session.headers["Referer"] = self.BASE_URL
        response = self.session.get(url, timeout=15)
        self.last_request_time = time.time()
        response.raise_for_status()
        return response.json()

# Module-level singleton
nse = NSESession()
```

**IMPORTANT:** If you get 401/403 errors, the cookies expired. Call `_refresh_cookies(force=True)`. If persistent 403, add a 3-second delay and retry once.

---

## Source 1: Insider Trading Disclosures (PIT Regulation 7)

### What This Is
When promoters, directors, CEOs, CTOs, CFOs, or their relatives buy/sell shares of their own company worth ≥₹10 lakh in a quarter, they MUST file a disclosure with the stock exchange within 2 trading days. This is the single most valuable signal — insiders buying is historically bullish.

### NSE API Endpoint
```
GET https://www.nseindia.com/api/corporates-pit?index=equities&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY
```

**Parameters:**
- `index`: always `equities`
- `from_date`: DD-MM-YYYY format (e.g., `01-04-2026`)
- `to_date`: DD-MM-YYYY format (max 3-month range per call)

**Referer to set:** `https://www.nseindia.com/companies-listing/corporate-filings-insider-trading`

### Expected JSON Response Structure
```json
{
  "data": [
    {
      "symbol": "RELIANCE",
      "company": "Reliance Industries Limited",
      "anession": "Regulation 7 (2) Connected Person",
      "acqName": "Mukesh Dhirubhai Ambani",
      "personCategory": "Promoters",
      "acqfromDt": "15-Mar-2026",
      "acqtoDt": "15-Mar-2026",
      "befAcqSharesNo": "452345678",
      "befAcqSharesPer": "50.31",
      "secAcq": "Market Purchase",
      "secType": "Shares",
      "tdpTransactionType": "Buy",
      "shareNo": "100000",
      "acqValue": "25000000",
      "aftAcqSharesNo": "452445678",
      "aftAcqSharesPer": "50.32",
      "date": "17-Mar-2026",
      "intimDt": "17-Mar-2026",
      "tkdAcquirerName": "Mukesh Dhirubhai Ambani"
    }
  ]
}
```

### Key Fields to Extract & Store
| Field | DB Column | Type | Description |
|-------|-----------|------|-------------|
| `symbol` | `symbol` | TEXT | NSE stock symbol |
| `company` | `company_name` | TEXT | Full company name |
| `acqName` | `insider_name` | TEXT | Name of the insider |
| `personCategory` | `person_category` | TEXT | "Promoters", "Promoter Group", "Director", "Key Managerial Personnel" |
| `tdpTransactionType` | `transaction_type` | TEXT | "Buy" or "Sell" |
| `shareNo` | `quantity` | INTEGER | Number of shares |
| `acqValue` | `value` | REAL | Total transaction value in ₹ |
| `befAcqSharesPer` | `holding_before_pct` | REAL | % holding before transaction |
| `aftAcqSharesPer` | `holding_after_pct` | REAL | % holding after transaction |
| `acqfromDt` | `trade_from_date` | TEXT | Trade period start (DD-Mon-YYYY) |
| `acqtoDt` | `trade_to_date` | TEXT | Trade period end |
| `date` | `disclosure_date` | TEXT | Date disclosed to exchange |
| `secAcq` | `mode_of_acquisition` | TEXT | "Market Purchase", "Off Market", etc. |

### Scraping Logic
```
1. Use nse.get() with the endpoint above
2. Default date range: last 30 days (from_date = today - 30, to_date = today)
3. Parse response["data"] — it's a list of dicts
4. For each record, INSERT OR IGNORE into insider_trades table (unique key: symbol + insider_name + trade_from_date + quantity)
5. IMPORTANT: Filter for tdpTransactionType == "Buy" for bullish signals. Sells are informational but less predictive.
```

### BSE Fallback (if NSE fails)
```
GET https://api.bseindia.com/BseIndiaAPI/api/CorporateAction/w?strSearch=insidertrading&dtFromDate=DD/MM/YYYY&dtToDate=DD/MM/YYYY&strScrip=&strName=
```
BSE does NOT require cookie dance. Direct GET works. Response format differs — map fields accordingly.

---

## Source 2: SAST Regulation 29 — Big Acquirer Disclosures

### What This Is
When ANY entity (individual, fund, company) acquires 5%+ of a company's shares, they must disclose within 2 working days. If they already hold 5%+ and their holding changes by 2%+, another disclosure is required. This catches: activist investors building positions, PE funds accumulating, corporate raiders, strategic investors.

### NSE API Endpoint
```
GET https://www.nseindia.com/api/corporates-sast?index=equities&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY&fo_flag=Y&reg_type=reg29
```

**Parameters:**
- `index`: `equities`
- `from_date` / `to_date`: DD-MM-YYYY format
- `reg_type`: `reg29` for acquisition/disposal disclosures

**Referer:** `https://www.nseindia.com/companies-listing/corporate-filings-regulation-29`

### Expected JSON Response Structure
```json
{
  "data": [
    {
      "symbol": "TATAMOTORS",
      "companyName": "Tata Motors Limited",
      "acquirerName": "XYZ Capital Partners",
      "noOfShareAcq": "5000000",
      "percOfSharesAcq": "1.5",
      "befAcqSharesNo": "10000000",
      "befAcqSharesPer": "3.0",
      "aftAcqSharesNo": "15000000",
      "aftAcqSharesPer": "4.5",
      "acqType": "Acquisition",
      "date": "15-Mar-2026",
      "timestamp": "17-Mar-2026 14:30:00"
    }
  ]
}
```

### Key Fields to Extract & Store
| Field | DB Column | Type | Description |
|-------|-----------|------|-------------|
| `symbol` | `symbol` | TEXT | NSE stock symbol |
| `companyName` | `company_name` | TEXT | Target company |
| `acquirerName` | `acquirer_name` | TEXT | Who is acquiring/disposing |
| `noOfShareAcq` | `shares_transacted` | INTEGER | Shares acquired or disposed |
| `percOfSharesAcq` | `pct_transacted` | REAL | % of total shares transacted |
| `befAcqSharesPer` | `holding_before_pct` | REAL | Holding % before |
| `aftAcqSharesPer` | `holding_after_pct` | REAL | Holding % after |
| `acqType` | `transaction_type` | TEXT | "Acquisition" or "Disposal" |
| `date` | `disclosure_date` | TEXT | Filing date |

### Scraping Logic
```
1. Use nse.get() with endpoint above
2. Default: last 30 days
3. Parse response["data"]
4. INSERT OR IGNORE into sast_disclosures (unique: symbol + acquirer_name + date + shares_transacted)
5. SIGNAL: If holding_after_pct > holding_before_pct AND holding_after_pct crosses 5%, 10%, 15%, 20%, 25% thresholds — flag as HIGH SIGNAL
```

---

## Source 3: Bulk Deals & Block Deals

### What This Is
- **Bulk Deal**: Total quantity traded in a stock by a single client exceeds 0.5% of total equity shares in a trading day. Disclosed same day after market hours.
- **Block Deal**: Single trade of minimum 500,000 shares or ₹5 crore value. Executed in a special window (8:45-9:00 AM).

### NSE API Endpoints

**Live/Today's Deals:**
```
GET https://www.nseindia.com/api/snapshot-capital-market-largedeal
```
Returns today's bulk and block deals. No date parameters needed.

**Referer:** `https://www.nseindia.com/report-detail/display-bulk-and-block-deals`

**Historical Deals (CSV download):**
```
GET https://www.nseindia.com/api/historical/bulk-deals?from=DD-MM-YYYY&to=DD-MM-YYYY&csv=true
```
Also available as JSON without `&csv=true`.

For **block deals** specifically:
```
GET https://www.nseindia.com/api/block-deal
```

### Expected JSON Response Structure (snapshot endpoint)
```json
{
  "BLOCK_DEALS_DATA": [
    {
      "BD_DT_DATE": "16-Apr-2026",
      "BD_SYMBOL": "HDFCBANK",
      "BD_SCRIP_NAME": "HDFC Bank Limited",
      "BD_CLIENT_NAME": "GOLDMAN SACHS INVESTMENTS MAURITIUS",
      "BD_BUY_SELL": "BUY",
      "BD_QTY_TRD": "2500000",
      "BD_TP_WATP": "1645.50",
      "BD_REMARKS": ""
    }
  ],
  "BULK_DEALS_DATA": [
    {
      "BD_DT_DATE": "16-Apr-2026",
      "BD_SYMBOL": "IRFC",
      "BD_SCRIP_NAME": "Indian Railway Finance Corporation Limited",
      "BD_CLIENT_NAME": "SBI MUTUAL FUND - SBI LARGE AND MIDCAP FUND",
      "BD_BUY_SELL": "BUY",
      "BD_QTY_TRD": "15000000",
      "BD_TP_WATP": "155.20",
      "BD_REMARKS": ""
    }
  ]
}
```

### Key Fields to Extract & Store
| Field | DB Column | Type | Description |
|-------|-----------|------|-------------|
| `BD_DT_DATE` | `deal_date` | TEXT | Date of deal |
| `BD_SYMBOL` | `symbol` | TEXT | NSE symbol |
| `BD_SCRIP_NAME` | `company_name` | TEXT | Company name |
| `BD_CLIENT_NAME` | `client_name` | TEXT | WHO bought/sold (this is the gold) |
| `BD_BUY_SELL` | `buy_sell` | TEXT | "BUY" or "SELL" |
| `BD_QTY_TRD` | `quantity` | INTEGER | Shares traded |
| `BD_TP_WATP` | `price` | REAL | Weighted avg trade price |
| — (computed) | `deal_type` | TEXT | "BULK" or "BLOCK" |
| — (computed) | `value` | REAL | quantity × price |

### Scraping Logic
```
1. Use nse.get("https://www.nseindia.com/api/snapshot-capital-market-largedeal")
2. Parse both BLOCK_DEALS_DATA and BULK_DEALS_DATA arrays
3. For each record, compute value = quantity * price
4. INSERT OR IGNORE into bulk_block_deals (unique: deal_date + symbol + client_name + quantity)
5. For historical backfill, use the historical endpoint with date range (max 3 months per call)
```

### BSE Equivalent
```
GET https://api.bseindia.com/BseIndiaAPI/api/DefaultData/w?Ession=blkdeal&Ession2=bulk&fmdt=DD/MM/YYYY&todt=DD/MM/YYYY
```
No cookie required.

---

## Source 4: FII/DII Daily Trading Activity

### What This Is
Aggregate daily buy/sell figures for Foreign Institutional Investors (FII/FPI) and Domestic Institutional Investors (DII) across all exchanges. Published same evening. This is the broadest market-level signal — persistent FII buying = bullish for markets, persistent selling = bearish.

### NSE API Endpoint
```
GET https://www.nseindia.com/api/fiidiiTradeReact
```
No date parameters — returns today's data + recent history.

**Referer:** `https://www.nseindia.com/reports/fii-dii`

### Expected JSON Response Structure
```json
{
  "date": "16-Apr-2026",
  "fpiData": {
    "buyValue": "12345.67",
    "sellValue": "9876.54",
    "netValue": "2469.13",
    "category": "FPI / FII"
  },
  "diiData": {
    "buyValue": "8765.43",
    "sellValue": "7654.32",
    "netValue": "1111.11",
    "category": "DII"
  }
}
```

**Note:** The exact response structure may be an array of objects or a nested object. The API returns values in ₹ Crores. Handle both structures:

**Alternative structure (array format):**
```json
[
  {
    "category": "DII *",
    "date": "16-Apr-2026",
    "buyValue": "14156.17",
    "sellValue": "10,233.17",
    "netValue": "3923.00"
  },
  {
    "category": "FII/FPI *",
    "date": "16-Apr-2026",
    "buyValue": "10245.38",
    "sellValue": "13476.92",
    "netValue": "-3231.54"
  }
]
```

### Key Fields to Extract & Store
| Field | DB Column | Type | Description |
|-------|-----------|------|-------------|
| `date` | `date` | TEXT | Trading date |
| `category` | `category` | TEXT | "FII/FPI" or "DII" |
| `buyValue` | `buy_value_cr` | REAL | Total buy in ₹ Crores |
| `sellValue` | `sell_value_cr` | REAL | Total sell in ₹ Crores |
| `netValue` | `net_value_cr` | REAL | Net = Buy - Sell (positive = net buying) |

### NSDL Detailed FPI Data (Optional Enhancement)
For sector-wise FII/FPI breakdown:
```
https://www.fpi.nsdl.co.in/web/Reports/Latest.aspx
```
This provides FPI investment broken down by: Equity, Debt, Hybrid. Requires HTML scraping (not JSON). Implement only if time permits.

### Scraping Logic
```
1. Use nse.get("https://www.nseindia.com/api/fiidiiTradeReact")
2. Parse the response — handle both array and object formats
3. Clean values: remove commas, convert to float
4. INSERT OR IGNORE into fii_dii_activity (unique: date + category)
5. SIGNAL: Track 5-day rolling net. If FII net buying > ₹5000 Cr over 5 days = strong bullish. If net selling > ₹5000 Cr = bearish.
```

---

## Source 5: Mutual Fund Portfolio Disclosures

### What This Is
Every Indian mutual fund must disclose its full portfolio monthly (published on AMFI website ~15 days after month-end). This tells you exactly which stocks the biggest domestic funds (SBI MF, HDFC MF, ICICI Pru MF, etc.) are buying or selling.

### Data Source: AMFI India Website
```
https://www.amfiindia.com/online-center/portfolio-disclosure
```

This is NOT a JSON API — it's a website with dropdowns. The workflow:
1. Select disclosure type: "Monthly" or "Half Yearly"
2. Select AMC (fund house)
3. Select month/year
4. Download the Excel/PDF file

### Scraping Strategy — Use AMC Direct Downloads Instead

Each AMC publishes portfolio PDFs/XLS on their own website. More reliable than AMFI:

| AMC | Portfolio URL Pattern |
|-----|---------------------|
| SBI MF | `https://www.sbimf.com/disclosure` → Portfolio section |
| HDFC MF | `https://www.hdfcfund.com/statutory-disclosure/monthly-portfolio` |
| ICICI Pru MF | `https://www.icicipruamc.com/statutory-disclosure/portfolio-disclosure` |
| Kotak MF | `https://www.kotakmf.com/Information/portfolio-disclosure` |
| Nippon MF | `https://mf.nipponindiaim.com/investor-service/downloads/portfolio-disclosure` |
| Axis MF | `https://www.axismf.com/statutory-disclosures` |

### Alternative: Trendlyne/Screener Aggregated Data (Simpler)
For a simpler approach, scrape pre-aggregated MF holding data from:
```
https://trendlyne.com/equity/share-holding/{STOCK_ID}/{SYMBOL}/latest/{company-slug}/
```
This gives shareholding breakdown including MF holding % for any stock.

### Practical Implementation (Recommended Approach)
Since parsing 40+ AMC portfolios from Excel/PDF is complex, use this two-step approach:

**Step A — Track Top MF Scheme NAVs via AMFI API (detects if fund is growing/shrinking):**
```
GET https://www.amfiindia.com/spages/NAVAll.txt
```
This is a plain-text file (not JSON) with ALL mutual fund NAVs updated daily. Format:
```
Scheme Code;ISIN Div Payout/ISIN Growth;ISIN Div Reinvestment;Scheme Name;Net Asset Value;Date
119551;INF209K01YY0;INF209K01YZ7;SBI Large & Midcap Fund - Direct Plan - Growth;456.7890;16-Apr-2026
```

**Step B — For individual stock MF holding changes, scrape NSE shareholding pattern:**
```
GET https://www.nseindia.com/api/corporate-share-holdings-master?index=equities&symbol=RELIANCE
```
Then for detailed pattern:
```
GET https://www.nseindia.com/api/corporate-share-holdings?symbol=RELIANCE&industry=-&quarterEnding=March2026
```

This gives the quarterly shareholding pattern including:
- Mutual fund holding % change
- Insurance company holding % change
- FII/FPI holding % change
- Promoter holding % change

### Key Fields to Store (from shareholding pattern)
| Field | DB Column | Type | Description |
|-------|-----------|------|-------------|
| symbol | `symbol` | TEXT | NSE symbol |
| quarter | `quarter` | TEXT | e.g., "March2026" |
| promoter_pct | `promoter_pct` | REAL | Promoter holding % |
| fii_pct | `fii_pct` | REAL | FII/FPI holding % |
| dii_pct | `dii_pct` | REAL | DII holding % |
| mf_pct | `mf_pct` | REAL | Mutual fund holding % |
| public_pct | `public_pct` | REAL | Public holding % |

### Scraping Logic
```
1. Maintain a watchlist of ~200 stocks (Nifty 200 or custom)
2. For each symbol, call the shareholding API quarterly
3. INSERT OR IGNORE into shareholding_patterns (unique: symbol + quarter)
4. SIGNAL: Compare current quarter vs previous quarter. If MF holding increased by >1% AND promoter holding stable = bullish. If promoter decreased = investigate why.
```

---

## Database Schema

Use SQLite. Create all tables in `db.py`.

```sql
-- Source 1: Insider Trading
CREATE TABLE IF NOT EXISTS insider_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    company_name TEXT,
    insider_name TEXT NOT NULL,
    person_category TEXT,           -- Promoter, Director, KMP, etc.
    transaction_type TEXT NOT NULL,  -- Buy or Sell
    quantity INTEGER,
    value REAL,                     -- in ₹
    holding_before_pct REAL,
    holding_after_pct REAL,
    trade_from_date TEXT,
    trade_to_date TEXT,
    disclosure_date TEXT,
    mode_of_acquisition TEXT,
    source TEXT DEFAULT 'NSE',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(symbol, insider_name, trade_from_date, quantity)
);

-- Source 2: SAST Regulation 29
CREATE TABLE IF NOT EXISTS sast_disclosures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    company_name TEXT,
    acquirer_name TEXT NOT NULL,
    shares_transacted INTEGER,
    pct_transacted REAL,
    holding_before_pct REAL,
    holding_after_pct REAL,
    transaction_type TEXT,          -- Acquisition or Disposal
    disclosure_date TEXT,
    source TEXT DEFAULT 'NSE',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(symbol, acquirer_name, disclosure_date, shares_transacted)
);

-- Source 3: Bulk and Block Deals
CREATE TABLE IF NOT EXISTS bulk_block_deals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    company_name TEXT,
    client_name TEXT NOT NULL,
    buy_sell TEXT NOT NULL,          -- BUY or SELL
    quantity INTEGER,
    price REAL,
    value REAL,                     -- quantity * price
    deal_type TEXT NOT NULL,        -- BULK or BLOCK
    source TEXT DEFAULT 'NSE',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(deal_date, symbol, client_name, quantity)
);

-- Source 4: FII/DII Activity
CREATE TABLE IF NOT EXISTS fii_dii_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    category TEXT NOT NULL,         -- 'FII/FPI' or 'DII'
    buy_value_cr REAL,              -- in ₹ Crores
    sell_value_cr REAL,
    net_value_cr REAL,
    source TEXT DEFAULT 'NSE',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(date, category)
);

-- Source 5: Shareholding Patterns (quarterly, includes MF data)
CREATE TABLE IF NOT EXISTS shareholding_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    company_name TEXT,
    quarter TEXT NOT NULL,          -- e.g., 'March2026'
    promoter_pct REAL,
    fii_pct REAL,
    dii_pct REAL,
    mf_pct REAL,
    public_pct REAL,
    total_shares INTEGER,
    source TEXT DEFAULT 'NSE',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(symbol, quarter)
);

-- Consolidated Signals View (create as a VIEW)
CREATE VIEW IF NOT EXISTS consolidated_signals AS
SELECT 
    'INSIDER_BUY' as signal_type,
    symbol,
    company_name,
    insider_name as entity_name,
    person_category as entity_type,
    transaction_type as action,
    value as value_inr,
    quantity,
    disclosure_date as signal_date,
    CASE 
        WHEN person_category = 'Promoters' AND transaction_type = 'Buy' AND value > 10000000 THEN 'HIGH'
        WHEN person_category = 'Promoters' AND transaction_type = 'Buy' THEN 'MEDIUM'
        WHEN transaction_type = 'Buy' THEN 'LOW'
        ELSE 'INFO'
    END as signal_strength,
    created_at
FROM insider_trades

UNION ALL

SELECT
    'SAST_ACCUMULATION' as signal_type,
    symbol,
    company_name,
    acquirer_name as entity_name,
    'Acquirer' as entity_type,
    transaction_type as action,
    NULL as value_inr,
    shares_transacted as quantity,
    disclosure_date as signal_date,
    CASE
        WHEN holding_after_pct > holding_before_pct AND holding_after_pct >= 10 THEN 'HIGH'
        WHEN holding_after_pct > holding_before_pct THEN 'MEDIUM'
        ELSE 'INFO'
    END as signal_strength,
    created_at
FROM sast_disclosures

UNION ALL

SELECT
    'BULK_BLOCK_DEAL' as signal_type,
    symbol,
    company_name,
    client_name as entity_name,
    deal_type as entity_type,
    buy_sell as action,
    value as value_inr,
    quantity,
    deal_date as signal_date,
    CASE
        WHEN buy_sell = 'BUY' AND value > 50000000 THEN 'HIGH'
        WHEN buy_sell = 'BUY' THEN 'MEDIUM'
        ELSE 'INFO'
    END as signal_strength,
    created_at
FROM bulk_block_deals

ORDER BY signal_date DESC;
```

---

## API Endpoints (FastAPI)

Implement in `api.py`:

```
GET /api/signals?days=7&strength=HIGH,MEDIUM&symbol=RELIANCE
    → Returns consolidated_signals view with filters
    → Default: last 7 days, all strengths, all symbols

GET /api/insider-trades?days=30&type=Buy&category=Promoters
    → Returns insider_trades filtered

GET /api/sast?days=30&type=Acquisition
    → Returns sast_disclosures filtered

GET /api/deals?days=7&type=BULK,BLOCK&action=BUY
    → Returns bulk_block_deals filtered

GET /api/fii-dii?days=30
    → Returns fii_dii_activity with rolling net calculation

GET /api/shareholding/{symbol}
    → Returns all quarters for a symbol, sorted by quarter

GET /api/dashboard-summary
    → Returns aggregated summary:
    {
        "today_insider_buys": 5,
        "today_insider_buy_value": 125000000,
        "today_bulk_deals": 12,
        "fii_net_today": -3231.54,
        "dii_net_today": 3923.00,
        "top_signals": [...first 10 from consolidated_signals...]
    }

GET /api/stock-signals/{symbol}
    → Returns ALL signals for a specific stock across all 5 sources
```

---

## Dashboard — Consolidated View

Build a single-page dashboard (HTML + vanilla JS or React). Show:

### Layout
```
┌─────────────────────────────────────────────────────────┐
│  HEADER: "Smart Money Tracker — Indian Markets"          │
│  Last updated: 16 Apr 2026, 7:30 PM IST                │
├─────────────────┬───────────────────────────────────────┤
│ FII/DII Panel   │  Signal Feed (real-time scrolling)     │
│ FII Net: -3231  │  🔴 HDFC: Promoter bought 50L shares  │
│ DII Net: +3923  │  🟢 TCS: Block deal - GS bought 25L   │
│ 5-day FII: -8k  │  🟡 INFY: Reg29 - 5% crossed by XYZ  │
│ 5-day DII: +12k │  🔴 RELIANCE: Bulk sell by ABC Fund   │
├─────────────────┴───────────────────────────────────────┤
│  FILTERS: [Date Range] [Signal Type ▼] [Symbol Search]  │
├─────────────────────────────────────────────────────────┤
│  Consolidated Signal Table                               │
│  Date | Symbol | Signal | Entity | Action | Value | Str  │
│  ──────────────────────────────────────────────────────  │
│  16 Apr | HDFC  | Insider| Deepak P| Buy | ₹50Cr | HIGH │
│  16 Apr | TCS   | Block  | GS     | Buy | ₹411Cr| HIGH │
│  15 Apr | INFY  | SAST   | Vanguard| Acq | 5.2% | MED  │
└─────────────────────────────────────────────────────────┘
```

### Signal Strength Color Coding
- **HIGH** (🔴 Red badge): Promoter buys >₹1Cr, SAST crossing 10%+, Block deals >₹50Cr
- **MEDIUM** (🟡 Yellow badge): Any insider buy, SAST 5%+ crossing, Bulk buy >₹10Cr  
- **LOW** (🟢 Green badge): KMP buys, small bulk deals
- **INFO** (⚪ Gray): Sells, disposals (still useful context)

---

## Scheduler / Runner

In `scheduler.py`, implement a simple scheduler:

```python
# Run order matters — FII/DII and bulk/block data available same evening
# Insider and SAST data available T+2 days
SCHEDULE = {
    "18:30": ["fii_dii", "bulk_block_deals"],      # Run at 6:30 PM IST daily
    "20:00": ["insider_trading", "sast_regulation29"], # Run at 8 PM daily  
    "06:00": ["mf_portfolios"],                     # Run at 6 AM (quarterly/monthly)
}
```

For first run / backfill, each scraper should accept a `--backfill-days N` argument to fetch N days of historical data.

---

## Error Handling Rules

1. **NSE 401/403**: Refresh cookies and retry once after 3s delay. If still fails, log and skip.
2. **NSE 429 (rate limit)**: Wait 30 seconds, then retry. Max 3 retries.
3. **Empty response**: Log warning, do NOT treat as error. Some days have no insider trades.
4. **Malformed JSON**: Log the raw response (first 500 chars), skip record.
5. **Duplicate records**: Use INSERT OR IGNORE — SQLite handles this via UNIQUE constraints.
6. **Network timeout**: 15 second timeout. Retry once after 5s.

---

## requirements.txt
```
requests>=2.31.0
fastapi>=0.104.0
uvicorn>=0.24.0
sqlite3  # built-in, no install needed
schedule>=1.2.0
python-dateutil>=2.8.2
```

---

## Implementation Order (for minimal token usage)

Implement in this exact order. Each step is independently testable:

1. **`config.py`** — All URLs, headers, DB path, date formats (10 lines)
2. **`db.py`** — Schema creation + generic insert/query helpers (50 lines)
3. **`scrapers/nse_session.py`** — Cookie-managed session (30 lines)
4. **`scrapers/bulk_block_deals.py`** — Simplest scraper, test session works (40 lines)
5. **`scrapers/fii_dii.py`** — Second simplest (30 lines)
6. **`scrapers/insider_trading.py`** — Most valuable signal (50 lines)
7. **`scrapers/sast_regulation29.py`** — Similar pattern to insider (40 lines)
8. **`scrapers/mf_portfolios.py`** — Shareholding pattern approach (60 lines)
9. **`run.py`** — CLI to run individual or all scrapers (20 lines)
10. **`api.py`** — FastAPI with all endpoints (80 lines)
11. **`dashboard/index.html`** — Single-file dashboard (150 lines)

**Total: ~560 lines of code.** Each file is small and focused.

---

## Testing Each Scraper

After implementing each scraper, test it standalone:
```bash
python -m scrapers.bulk_block_deals   # Should fetch today's deals
python -m scrapers.fii_dii            # Should fetch today's FII/DII
python -m scrapers.insider_trading    # Should fetch last 30 days
python -m scrapers.sast_regulation29  # Should fetch last 30 days
python -m scrapers.mf_portfolios     # Should fetch latest quarter
```

Each scraper module should have a `if __name__ == "__main__"` block that runs it standalone and prints results.

---

## IMPORTANT NOTES

1. **This is NOT insider trading.** All data comes from legally mandated public disclosures. You are simply consuming public regulatory filings faster than media reports them.
2. **NSE APIs are unofficial.** They can change without notice. If an endpoint breaks, inspect the NSE website Network tab in Chrome DevTools to find the updated URL.
3. **Rate limiting is critical.** NSE will IP-ban aggressive scrapers. Keep minimum 1-second gaps between requests. For bulk operations, add 2-3 second delays.
4. **Data is for educational/informational purposes only.** Not financial advice. Build your own judgment around these signals.

<!-- code-review-graph MCP tools -->
## MCP Tools: code-review-graph

**IMPORTANT: This project has a knowledge graph. ALWAYS use the
code-review-graph MCP tools BEFORE using Grep/Glob/Read to explore
the codebase.** The graph is faster, cheaper (fewer tokens), and gives
you structural context (callers, dependents, test coverage) that file
scanning cannot.

### When to use graph tools FIRST

- **Exploring code**: `semantic_search_nodes` or `query_graph` instead of Grep
- **Understanding impact**: `get_impact_radius` instead of manually tracing imports
- **Code review**: `detect_changes` + `get_review_context` instead of reading entire files
- **Finding relationships**: `query_graph` with callers_of/callees_of/imports_of/tests_for
- **Architecture questions**: `get_architecture_overview` + `list_communities`

Fall back to Grep/Glob/Read **only** when the graph doesn't cover what you need.

### Key Tools

| Tool | Use when |
|------|----------|
| `detect_changes` | Reviewing code changes — gives risk-scored analysis |
| `get_review_context` | Need source snippets for review — token-efficient |
| `get_impact_radius` | Understanding blast radius of a change |
| `get_affected_flows` | Finding which execution paths are impacted |
| `query_graph` | Tracing callers, callees, imports, tests, dependencies |
| `semantic_search_nodes` | Finding functions/classes by name or keyword |
| `get_architecture_overview` | Understanding high-level codebase structure |
| `refactor_tool` | Planning renames, finding dead code |

### Workflow

1. The graph auto-updates on file changes (via hooks).
2. Use `detect_changes` for code review.
3. Use `get_affected_flows` to understand impact.
4. Use `query_graph` pattern="tests_for" to check coverage.
