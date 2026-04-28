# Smart Money Tracker — Technical Architecture

---

## Full System Workflow

```
╔══════════════════════════════════════════════════════════════════════════════════════╗
║                           SMART MONEY TRACKER — SYSTEM FLOW                         ║
╚══════════════════════════════════════════════════════════════════════════════════════╝

  YOUR LAPTOP (Indian IP — required for NSE access)
  ┌─────────────────────────────────────────────────────────┐
  │  python run.py all  OR  python run.py full              │
  │                                                         │
  │  ┌───────────────┐  ┌──────────┐  ┌──────────────────┐ │
  │  │ NSE India API │  │Screener  │  │  NSE Shareholding│ │
  │  │ (5 endpoints) │  │  .in     │  │     Pattern API  │ │
  │  └──────┬────────┘  └────┬─────┘  └────────┬─────────┘ │
  │         │                │                  │           │
  │  ┌──────▼────────────────▼──────────────────▼─────────┐ │
  │  │              5 Scrapers                             │ │
  │  │  insider_trading  │  sast_regulation29              │ │
  │  │  bulk_block_deals │  fii_dii  │  mf_portfolios      │ │
  │  └──────────────────────────────┬──────────────────────┘ │
  └─────────────────────────────────┼───────────────────────┘
                                    │ HTTPS writes
                                    ▼
  ┌─────────────────────────────────────────────────────────┐
  │                    TURSO (Hosted DB)                    │
  │                                                         │
  │  insider_trades      sast_disclosures                   │
  │  bulk_block_deals    fii_dii_activity                   │
  │  shareholding_patterns  stock_fundamentals              │
  │  signal_clusters     promoter_streaks                   │
  │             consolidated_signals (VIEW)                 │
  └──────────────────────────┬──────────────────────────────┘
              ┌──────────────┴───────────────┐
              │                              │
              ▼                              ▼
  ┌───────────────────────┐      ┌───────────────────────────┐
  │  RENDER — Worker      │      │  RENDER — Dashboard       │
  │  smart-money-worker   │      │  smart-money-tracker      │
  │                       │      │                           │
  │  POST /recompute      │      │  GET  /                   │
  │  GET  /health         │      │  GET  /api/signals        │
  │  GET  /ready          │      │  GET  /api/dashboard-     │
  │                       │      │       summary             │
  │  Runs:                │      │  GET  /api/clusters       │
  │  → streak detection   │      │  GET  /api/stock-         │
  │  → cluster scoring    │      │       intelligence        │
  │  → fundamentals       │◄─────│  POST /api/run-analysis   │
  │    enrichment         │      │  ... (all read endpoints) │
  └───────────────────────┘      └───────────────┬───────────┘
                                                  │ HTML/JSON
                                                  ▼
                                       ┌─────────────────────┐
                                       │      BROWSER        │
                                       │                     │
                                       │  Dashboard Tab      │
                                       │  Signals Tab        │
                                       │  Stock Intel Tab    │
                                       │  Stock Deep Dive    │
                                       └─────────────────────┘
```

---

## Steps — In Order, What Runs When

### Step 1 — Fetch Raw Data (Your Laptop)
```
python run.py all
```
Runs 5 scrapers in order. Each scraper hits its NSE/Screener endpoint, parses the response, and inserts into Turso.

| Order | Scraper | Source | Table Written |
|-------|---------|--------|---------------|
| 1 | bulk_block_deals | NSE snapshot + historical API | bulk_block_deals |
| 2 | fii_dii | NSE fiidiiTradeReact API | fii_dii_activity |
| 3 | insider_trading | NSE corporates-pit API | insider_trades |
| 4 | sast_regulation29 | NSE corporate-sast-reg29 API | sast_disclosures |
| 5 | mf_portfolios | NSE shareholding master API | shareholding_patterns |

All inserts use `INSERT OR IGNORE` — duplicates are silently skipped via UNIQUE constraints.

### Step 2 — Compute Signals (Render Worker or Locally)
```
python run.py full   (local)
POST /recompute      (via worker on Render)
```

| Order | Step | What Happens | Table Written |
|-------|------|-------------|---------------|
| 1 | Streak Detection | Counts distinct insiders buying within 90 days, assigns strength tier | promoter_streaks |
| 2 | Cluster Scoring | Scores each symbol by counting signals across all sources, applies multipliers | signal_clusters |
| 3 | Fundamentals Enrichment | Scrapes Screener.in for balance sheet, P&L, cash flow, computes quality score | stock_fundamentals |

### Step 3 — Serve Dashboard (Render, always running)
The dashboard reads from Turso on every page load. No caching. Always shows latest data.

---

## Services

### Service 1 — Dashboard (`api.py`)
- Deployed on Render at `https://insidersignal.onrender.com`
- Serves the frontend HTML file and all read-only API endpoints
- Never writes to the database
- Proxies Run Analysis button clicks to the worker

### Service 2 — Worker (`worker/main.py`)
- Deployed on Render at `https://smart-money-worker.onrender.com`
- Runs computation: streaks, clusters, fundamentals
- Protected by `X-Worker-Secret` header — only the dashboard can call it
- Has `/health` and `/ready` endpoints for Render's health checks

### Local Machine
- Runs the 5 NSE scrapers (must be Indian IP — NSE geo-blocks cloud servers)
- `python run.py all` for just scrapers
- `python run.py full` for scrapers + computation
- `python run.py full --fresh` to wipe all data and start clean

### Database — Turso
- Hosted libSQL (SQLite-compatible) at `aws-ap-south-1.turso.io`
- Both Render services connect via HTTPS pipeline API
- 8 tables + 1 view
- Free tier: 9 GB storage, 500 databases

---

## Database Schema

### Table 1 — `insider_trades`
Stores every insider/promoter trade disclosed to NSE under PIT Regulation 7.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| symbol | TEXT | NSE stock symbol e.g. RELIANCE |
| company_name | TEXT | Full company name |
| insider_name | TEXT | Name of the person who traded |
| person_category | TEXT | Promoters / Promoter Group / Director / Key Managerial Personnel |
| transaction_type | TEXT | Buy or Sell |
| quantity | INTEGER | Number of shares traded |
| value | REAL | Total value in ₹ |
| holding_before_pct | REAL | % holding before the trade |
| holding_after_pct | REAL | % holding after the trade |
| trade_from_date | TEXT | Trade period start date (ISO YYYY-MM-DD) |
| trade_to_date | TEXT | Trade period end date |
| disclosure_date | TEXT | Date filed with the exchange |
| mode_of_acquisition | TEXT | Market Purchase / Open Market / Off Market / ESOP etc. |
| source | TEXT | Always "NSE" |
| created_at | TIMESTAMP | When this row was inserted |

**UNIQUE constraint:** (symbol, insider_name, trade_from_date, quantity)

---

### Table 2 — `sast_disclosures`
Stores disclosures under SAST Regulation 29 — triggered when any entity holds or crosses 5% of a company.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| symbol | TEXT | NSE stock symbol |
| company_name | TEXT | Target company |
| acquirer_name | TEXT | Who is acquiring or disposing |
| shares_transacted | INTEGER | Shares acquired or sold |
| pct_transacted | REAL | % of total shares in this transaction |
| holding_before_pct | REAL | Holding % before this transaction |
| holding_after_pct | REAL | Holding % after this transaction |
| transaction_type | TEXT | Acquisition or Disposal |
| disclosure_date | TEXT | Filing date (ISO YYYY-MM-DD) |
| source | TEXT | Always "NSE" |
| created_at | TIMESTAMP | When this row was inserted |

**UNIQUE constraint:** (symbol, acquirer_name, disclosure_date, shares_transacted)

---

### Table 3 — `bulk_block_deals`
Stores bulk deals (>0.5% of equity in one day by one client) and block deals (≥₹5 Cr or 5L shares in special window).

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| deal_date | TEXT | Date of the deal (ISO YYYY-MM-DD) |
| symbol | TEXT | NSE stock symbol |
| company_name | TEXT | Company name |
| client_name | TEXT | Who bought or sold (the most valuable field) |
| buy_sell | TEXT | BUY or SELL |
| quantity | INTEGER | Shares traded |
| price | REAL | Weighted average trade price |
| value | REAL | quantity × price |
| deal_type | TEXT | BULK or BLOCK |
| source | TEXT | Always "NSE" |
| created_at | TIMESTAMP | When this row was inserted |

**UNIQUE constraint:** (deal_date, symbol, client_name, quantity)

---

### Table 4 — `fii_dii_activity`
Daily buy/sell totals for Foreign Institutional Investors and Domestic Institutional Investors.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| date | TEXT | Trading date (ISO YYYY-MM-DD) |
| category | TEXT | FII/FPI or DII |
| buy_value_cr | REAL | Total buy value in ₹ Crores |
| sell_value_cr | REAL | Total sell value in ₹ Crores |
| net_value_cr | REAL | buy − sell (positive = net buying, negative = net selling) |
| source | TEXT | Always "NSE" |
| created_at | TIMESTAMP | When this row was inserted |

**UNIQUE constraint:** (date, category)

---

### Table 5 — `shareholding_patterns`
Quarterly shareholding breakdown for tracked stocks.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| symbol | TEXT | NSE stock symbol |
| company_name | TEXT | Company name |
| quarter | TEXT | e.g. December2025 |
| promoter_pct | REAL | Promoter + Promoter Group holding % |
| fii_pct | REAL | FII/FPI holding % |
| dii_pct | REAL | DII holding % |
| mf_pct | REAL | Mutual fund holding % |
| public_pct | REAL | Public / retail holding % |
| total_shares | INTEGER | Total issued shares |
| source | TEXT | Always "NSE" |
| created_at | TIMESTAMP | When this row was inserted |

**UNIQUE constraint:** (symbol, quarter)

---

### Table 6 — `signal_clusters`
Pre-computed conviction scores per stock. One row per symbol per computation run.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| symbol | TEXT | NSE stock symbol |
| company_name | TEXT | Company name |
| cluster_score | REAL | Weighted conviction score 0–100 |
| cluster_tier | TEXT | ELITE / HIGH / MEDIUM |
| source_count | INTEGER | How many distinct signal sources fired |
| sources_hit | TEXT | CSV e.g. "INSIDER_BUY,SAST,BLOCK_DEAL" |
| insider_buy_count | INTEGER | Number of insider buy events in window |
| sast_count | INTEGER | Number of SAST acquisitions in window |
| bulk_block_count | INTEGER | Number of bulk/block BUY deals in window |
| mf_accumulation | INTEGER | 1 if MF holding increased ≥1% QoQ, else 0 |
| total_transaction_value | REAL | Sum of insider + deal values in ₹ |
| first_signal_date | TEXT | Earliest signal in the window |
| last_signal_date | TEXT | Most recent signal in the window |
| window_days | INTEGER | Rolling window used (default 30) |
| computed_at | TIMESTAMP | When this row was computed |

**UNIQUE constraint:** (symbol, last_signal_date)

---

### Table 7 — `promoter_streaks`
Detected patterns of multiple insiders buying at the same company within 90 days.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| symbol | TEXT | NSE stock symbol |
| company_name | TEXT | Company name |
| distinct_insiders | INTEGER | How many different people bought |
| insider_names | TEXT | CSV of their names |
| total_value | REAL | Combined value of all their purchases in ₹ |
| window_start_date | TEXT | Start of the 90-day window |
| window_end_date | TEXT | End of the 90-day window |
| streak_strength | TEXT | WEAK / MODERATE / STRONG / ELITE |
| computed_at | TIMESTAMP | When this row was computed |

**UNIQUE constraint:** (symbol, window_end_date)

---

### Table 8 — `stock_fundamentals`
Fundamental metrics scraped from Screener.in. One row per stock, refreshed every 7 days.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| symbol | TEXT UNIQUE | NSE stock symbol |
| company_name | TEXT | Company name |
| sector | TEXT | Broad sector e.g. Technology |
| industry | TEXT | Specific industry e.g. IT Services |
| market_cap_cr | REAL | Market capitalisation in ₹ Crores |
| current_price | REAL | Last traded price |
| roce_current | REAL | Return on Capital Employed (latest year) % |
| roce_3yr_avg | REAL | ROCE averaged over last 3 years |
| roce_5yr_avg | REAL | ROCE averaged over last 5 years |
| roe_current | REAL | Return on Equity (latest year) % |
| roe_5yr_avg | REAL | ROE averaged over last 5 years |
| debt_to_equity | REAL | Borrowings ÷ (Equity + Reserves) |
| interest_coverage | REAL | Operating Profit ÷ Interest Expense |
| sales_cagr_5yr | REAL | Revenue compound annual growth rate over 5 years % |
| profit_cagr_5yr | REAL | Net profit CAGR over 5 years % |
| sales_growth_stddev | REAL | Standard deviation of YoY revenue growth (consistency measure) |
| fcf_5yr_cumulative | REAL | Sum of (Operating CF + Investing CF) over 5 years in ₹ Cr |
| profit_5yr_cumulative | REAL | Sum of net profit over 5 years in ₹ Cr |
| fcf_conversion | REAL | FCF cumulative ÷ Profit cumulative (how much profit converts to cash) |
| pe_current | REAL | Current Price ÷ Earnings Per Share |
| pe_5yr_median | REAL | Median PE over last 5 years (requires Screener login — usually null) |
| pe_vs_median | REAL | Current PE ÷ 5yr median PE (valuation vs history) |
| peg_ratio | REAL | PE ÷ Profit CAGR (growth-adjusted valuation) |
| promoter_holding_pct | REAL | % shares held by promoters |
| promoter_pledge_pct | REAL | % of promoter holding that is pledged |
| quality_score | REAL | Computed score 0–100 |
| quality_tier | TEXT | EXCELLENT / GOOD / AVERAGE / POOR / AVOID |
| red_flags | TEXT | JSON array of warning strings |
| fetched_at | TIMESTAMP | When Screener was last scraped |
| source | TEXT | Always "Screener.in" |

---

### View — `consolidated_signals`
A SQL VIEW that unions insider_trades, sast_disclosures, and bulk_block_deals into a single feed.

**Output columns:** signal_type, symbol, company_name, entity_name, entity_type, action, value_inr, quantity, signal_date, signal_strength, created_at

**Signal strength logic:**

| Source | Condition | Strength |
|--------|-----------|----------|
| Insider Buy | Promoters + Buy + value > ₹1 Crore | HIGH |
| Insider Buy | Promoters + Buy | MEDIUM |
| Insider Buy | Any category + Buy | LOW |
| Insider Sell | Any | INFO |
| SAST | after > before AND after ≥ 10% | HIGH |
| SAST | after > before | MEDIUM |
| SAST | Disposal or no change | INFO |
| Bulk/Block | BUY + value > ₹5 Crore | HIGH |
| Bulk/Block | BUY | MEDIUM |
| Bulk/Block | SELL | INFO |

---

## Data Sources

### Source 1 — Insider Trading (NSE PIT Regulation 7)
- **What it is:** When promoters, directors, CFOs, CTOs or their relatives buy/sell ≥ ₹10 lakh in their own company's stock in a quarter, they must file with the exchange within 2 trading days
- **API:** `GET /api/corporates-pit?index=equities&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY`
- **Referer required:** `https://www.nseindia.com/companies-listing/corporate-filings-insider-trading`
- **Default window:** Last 30 days
- **Key signal:** Promoter buying >₹1 Crore is the strongest bullish signal in this dataset

### Source 2 — SAST Regulation 29
- **What it is:** Any entity crossing 5% holding must disclose within 2 working days. Any entity already above 5% must disclose every 2% change
- **API:** `GET /api/corporate-sast-reg29?from_date=DD-MM-YYYY&to_date=DD-MM-YYYY`
- **Referer required:** `https://www.nseindia.com/companies-listing/corporate-filings-regulation-29`
- **Default window:** Last 30 days
- **Key signal:** An entity crossing 10%+ holding is a HIGH signal — usually a strategic investor or large fund building a position

### Source 3 — Bulk and Block Deals
- **What it is:** Bulk deal = one client trades >0.5% of total equity in a day. Block deal = single trade of ≥5 lakh shares or ≥₹5 Crore, executed in the 8:45–9:00 AM special window
- **APIs:**
  - Today: `GET /api/snapshot-capital-market-largedeal` → both BLOCK_DEALS_DATA and BULK_DEALS_DATA arrays
  - Historical: `GET /api/historical/bulk-deals?from=DD-MM-YYYY&to=DD-MM-YYYY`
- **Referer required:** `https://www.nseindia.com/report-detail/display-bulk-and-block-deals`
- **Key signal:** A known mutual fund or FII name in the client field buying a large stake is highly significant

### Source 4 — FII/DII Daily Activity
- **What it is:** Aggregate daily buy/sell for all Foreign Institutional Investors and Domestic Institutional Investors across exchanges
- **API:** `GET /api/fiidiiTradeReact` (no date params, returns recent history)
- **Referer required:** `https://www.nseindia.com/reports/fii-dii`
- **Values:** In ₹ Crores
- **Key signal:** FII net buying > ₹5000 Crore over 5 days = strong bullish; net selling > ₹5000 Crore = bearish

### Source 5 — Shareholding Patterns (MF Portfolios)
- **What it is:** Quarterly exchange-mandated disclosure of who holds what % of a company. Includes MF aggregate holding
- **API:** `GET /api/corporate-share-holdings-master?index=equities&symbol={SYMBOL}`
- **Referer required:** `https://www.nseindia.com/companies-listing/corporate-filings-shareholding-pattern`
- **Key signal:** MF holding increasing >1% QoQ while promoter holding is stable = institutional accumulation signal

---

## Scoring and Calculations

### Cluster Score Formula

Every stock with recent signals (last 30 days) gets scored:

**Base Score = sum of weighted events:**

| Event Type | Weight | Notes |
|------------|--------|-------|
| Insider buy by Promoter/Promoter Group | 30 per occurrence | Highest weight |
| Insider buy by Director/KMP | 15 per occurrence | Still meaningful |
| SAST acquisition (holding increased) | 25 per occurrence | Big acquirer building position |
| Block deal BUY | 20 per occurrence | Large institutional buy in special window |
| Bulk deal BUY | 15 per occurrence | Large daily volume by single client |
| MF accumulation (QoQ ≥ +1%) | 20 once per symbol | Mutual fund quietly building |

**Multipliers (stacked, applied to base score):**
- 3 or more distinct sources fired → multiply by **1.3×**
- Symbol has MODERATE/STRONG/ELITE promoter streak → multiply by **1.25×**

**Final Score = min(100, base × multipliers)**

**Tier Assignment:**
- ELITE: score ≥ 70
- HIGH: 50 ≤ score < 70
- MEDIUM: 30 ≤ score < 50
- Filtered out if score < 30

---

### Promoter Streak Formula

Looks at insider_trades over the last 90 days. Only counts buys where `mode_of_acquisition` is "Market Purchase", "Open Market", or "On Market" (filters out ESOPs, bonus shares, rights issues, gifts).

**Streak Strength Tiers:**
- WEAK: 2 distinct insiders buying
- MODERATE: 3 distinct insiders buying
- STRONG: 4 distinct insiders buying
- ELITE: 5+ distinct insiders buying, OR 3+ insiders + total value ≥ ₹10 Crore

---

### Fundamentals Quality Score Formula

Scraped from Screener.in. Total possible: **100 points**.

**Component 1 — ROCE 5-year average (max 30 points):**
- ≥ 20% → 30 points (excellent capital efficiency)
- ≥ 15% → 22 points
- ≥ 10% → 12 points
- < 10% → 0 points + red flag added

**Component 2 — Debt-to-Equity (max 20 points):**
- Calculated as: Borrowings ÷ (Equity Capital + Reserves)
- < 0.3 → 20 points (essentially debt-free)
- < 0.7 → 14 points
- < 1.2 → 7 points
- ≥ 1.2 → 0 points + red flag added

**Component 3 — FCF Conversion (max 20 points):**
- Calculated as: 5-year cumulative FCF ÷ 5-year cumulative Net Profit
- FCF = Operating Cash Flow + Investing Cash Flow (proxy)
- > 0.8 → 20 points (most profit converts to real cash)
- > 0.5 → 12 points
- > 0.3 → 5 points
- ≤ 0.3 → 0 points + red flag added

**Component 4 — Sales Growth Consistency (max 15 points):**
- Sales CAGR ≥ 12% → 10 points
- Else → 0 points
- Standard deviation of YoY growth < 10% → +5 bonus points (consistent, not lumpy)

**Component 5 — Valuation vs History (max 15 points):**
- PE vs 5-year median (requires Screener login, usually null):
- < 0.8× median → 15 points (trading cheap vs history)
- < 1.1× median → 10 points
- < 1.5× median → 5 points
- ≥ 1.5× → 0 points + red flag added

**Additional flags (don't subtract points, but add to red_flags list):**
- Interest Coverage < 2.5× → flag
- Promoter Pledge > 25% → flag

**Hard AVOID overrides (force tier to AVOID regardless of score):**
- Debt-to-Equity > 2.5
- Promoter pledge > 50%
- Interest Coverage < 1.5×

**Quality Tiers:**
- EXCELLENT: score ≥ 75
- GOOD: score ≥ 55
- AVERAGE: score ≥ 35
- POOR: score ≥ 20
- AVOID: score < 20 OR any hard override triggered

---

## API Endpoints

### Dashboard Endpoints (always-on, read-only)

| Endpoint | Method | Params | Returns |
|----------|--------|--------|---------|
| `/` | GET | — | Dashboard HTML |
| `/api/dashboard-summary` | GET | — | Today's KPIs + top 10 signals |
| `/api/signals` | GET | days, strength, symbol, limit | Consolidated signals feed |
| `/api/insider-trades` | GET | days, type, category, symbol, limit | Insider trade rows |
| `/api/sast` | GET | days, type, symbol, limit | SAST disclosure rows |
| `/api/deals` | GET | days, type, action, symbol, limit | Bulk/block deal rows |
| `/api/fii-dii` | GET | days | FII/DII activity + 5-day rolling net |
| `/api/shareholding/{symbol}` | GET | — | All quarters for a symbol |
| `/api/stock-signals/{symbol}` | GET | days | All signal types for one stock |
| `/api/clusters` | GET | tier, days | Signal clusters + fundamentals join |
| `/api/promoter-streaks` | GET | min_insiders, days | Promoter streaks + fundamentals join |
| `/api/fundamentals/{symbol}` | GET | — | Full fundamentals row for one stock |
| `/api/stock-intelligence` | GET | days, quality_tier, min_cluster_score | One row per stock — all signals + fundamentals |
| `/api/stock-news/{symbol}` | GET | — | Google News RSS + AI analysis |
| `/api/run-analysis` | POST | — | Triggers worker /recompute (proxied) |

### Worker Endpoints (computation service)

| Endpoint | Method | Auth | Returns |
|----------|--------|------|---------|
| `/health` | GET | None | `{status: "ok"}` |
| `/ready` | GET | None | `{status: "ready", db: "ok"}` or 503 |
| `/recompute` | POST | X-Worker-Secret header | `{status, steps, errors, total_elapsed_ms}` |

---

## Dashboard Pages and What They Show

### Tab 1 — Dashboard
The landing page. Shows:
- **4 KPI cards at top:** Insider Buys (last 2 days), Insider Buy Value (₹), Bulk/Block Deals count, FII/FPI Net latest (₹ Cr), DII Net latest (₹ Cr)
- **Top Signals table:** Latest HIGH and MEDIUM signals from the consolidated_signals view, with search, date filter, signal type filter, and symbol search
- **FII/DII chart:** Visual of recent FII and DII net values

Data source: `/api/dashboard-summary`, `/api/signals`, `/api/fii-dii`

### Tab 2 — Signals (Kanban Board)
A dark-themed Kanban board with 3 columns: STRONG (ELITE clusters), MODERATE (HIGH clusters), WEAK (MEDIUM clusters). Each card shows the stock ticker, company name, cluster score, signal pills (which sources fired), and fundamental metrics.

Clicking a card opens a light-themed gauge modal with animated horizontal bars for: Score, ROCE, P/E, D/E, Interest Coverage, FCF Conversion, Promoter Holding, Pledge %.

Data source: `/api/clusters`

### Tab 3 — Stock Intelligence
A table view with one row per signalled stock. Columns include: symbol, company, sector, latest signal date, cluster score, cluster tier, sources hit, promoter streak strength, quality score, quality tier, ROCE, D/E, Interest Coverage, FCF, P/E, PE vs median, Promoter %, Pledge %, Market Cap.

Clicking a row opens the same gauge modal.

Data source: `/api/stock-intelligence`

### Tab 4 — Stock Deep Dive
Search for any individual stock. Shows a full breakdown of all signals for that stock (insider trades, SAST disclosures, deals, shareholding history), news articles grouped by month from Google News RSS, and an AI-generated analysis of the news in context of the signals.

Data source: `/api/stock-signals/{symbol}`, `/api/stock-news/{symbol}`

---

## File Structure

```
InsiderSignal/
│
├── api.py                        # Dashboard FastAPI app — all read endpoints
├── config.py                     # All constants, URLs, thresholds, weights
├── db.py                         # Turso/SQLite abstraction, schema, helpers
├── run.py                        # CLI: run.py all / run.py full / run.py full --fresh
│
├── scrapers/
│   ├── nse_session.py            # Shared NSE HTTP session with cookie management
│   ├── insider_trading.py        # Source 1: PIT Regulation 7 insider trades
│   ├── sast_regulation29.py      # Source 2: SAST big acquirer disclosures
│   ├── bulk_block_deals.py       # Source 3: Bulk and block deals
│   ├── fii_dii.py                # Source 4: FII/DII daily activity
│   ├── mf_portfolios.py          # Source 5: NSE shareholding patterns
│   └── screener_fundamentals.py  # Screener.in scraper + quality score computation
│
├── smart_money/
│   └── cluster_detector.py       # Streak detection + cluster scoring algorithms
│
├── worker/
│   ├── main.py                   # Worker FastAPI app: /recompute /health /ready
│   ├── tasks.py                  # Worker task logic: streaks, clusters, fundamentals
│   ├── logging_config.py         # Structured JSON logging via structlog
│   ├── requirements.txt          # Worker-only dependencies
│   └── Dockerfile                # Container definition for worker
│
├── services/
│   └── ai_analysis.py            # Groq LLM integration for stock news analysis
│
├── dashboard/
│   └── index.html                # Single-file frontend (all HTML + CSS + JS)
│
├── Dockerfile                    # Container definition for dashboard
├── render.yaml                   # Render deployment config (2 services)
├── requirements.txt              # Dashboard dependencies
└── .env                          # Local environment variables (not in git)
```

---

## Configuration Constants

| Constant | Value | Used For |
|----------|-------|----------|
| `DEFAULT_BACKFILL_DAYS` | 30 | Default window for insider + SAST scrapers |
| `COOKIE_TTL_SECONDS` | 300 (5 min) | How often to refresh NSE session cookies |
| `REQUEST_RATE_LIMIT_SECONDS` | 1.0 | Minimum gap between NSE requests |
| `REQUEST_TIMEOUT_SECONDS` | 15 | NSE request timeout |
| `SCREENER_RATE_LIMIT_SECONDS` | 1.5 | Minimum gap between Screener.in requests |
| `SCREENER_TIMEOUT_SECONDS` | 20 | Screener request timeout |
| `CLUSTER_WINDOW_DAYS` | 30 | Rolling window for cluster scoring |
| `CLUSTER_MIN_SCORE` | 30 | Minimum score to store a cluster |
| `CLUSTER_MEDIUM_THRESHOLD` | 30 | Tier cutoff |
| `CLUSTER_HIGH_THRESHOLD` | 50 | Tier cutoff |
| `CLUSTER_ELITE_THRESHOLD` | 70 | Tier cutoff |
| `STREAK_WINDOW_DAYS` | 90 | Rolling window for streak detection |
| `STREAK_MIN_INSIDERS` | 2 | Minimum distinct buyers to record a streak |
| `STREAK_ELITE_VALUE_THRESHOLD` | ₹10 Crore | Value threshold for ELITE streak with 3 insiders |
| `FUNDAMENTALS_STALE_DAYS` | 7 | Refresh Screener data if older than this |
| `FUNDAMENTALS_LOOKBACK_DAYS` | 90 | Find symbols with signals in last 90 days for fundamentals |
| `INSIDER_HIGH_VALUE_INR` | ₹1 Crore | Threshold for HIGH insider signal |
| `BLOCK_DEAL_HIGH_VALUE_INR` | ₹5 Crore | Threshold for HIGH block deal signal |
| `FII_ROLLING_SIGNAL_CR` | ₹5,000 Crore | 5-day rolling threshold for FII signal |

---

## Environment Variables

| Variable | Service | Description |
|----------|---------|-------------|
| `TURSO_DATABASE_URL` | Both | `libsql://...turso.io` — hosted DB URL |
| `TURSO_AUTH_TOKEN` | Both | JWT token for Turso authentication |
| `GROQ_API_KEY` | Dashboard | API key for Groq LLM (AI stock analysis) |
| `WORKER_URL` | Dashboard | URL of worker service on Render |
| `WORKER_SECRET` | Both | Shared secret for service-to-service auth |
| `SENTRY_DSN` | Worker | Optional Sentry DSN for error tracking |

---

## NSE Session Handling

NSE India blocks direct API calls without a valid browser session. Every request requires:

1. First hit the NSE homepage to receive session cookies (`nseappid`, `nsit`, `bm_sv`)
2. Include those cookies on all subsequent API calls
3. Set the correct `Referer` header matching the page that would normally trigger each API
4. Keep at least 1 second between requests to avoid IP bans
5. Refresh cookies every 5 minutes

The `NSESession` singleton in `scrapers/nse_session.py` handles all of this automatically and is shared across all 5 scrapers.

---

## Turso Wire Format Notes

Turso's HTTPS pipeline API has specific requirements for parameter types:
- **Integer values** must be sent as quoted strings: `{"type": "integer", "value": "42"}`
- **Float values** must be sent as bare JSON numbers: `{"type": "float", "value": 3.14}`
- **Text** as strings: `{"type": "text", "value": "hello"}`
- **Null** with no value field: `{"type": "null"}`
- **Read responses** include type metadata — all values must be converted from their string representation using the type field, or they arrive as Python strings regardless of DB column type
