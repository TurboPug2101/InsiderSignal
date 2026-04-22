# Claude Code Prompt — Extend Signal Tracker with Clustering, Streaks & Fundamentals

## Context

Read `CLAUDE.md` in the project root before writing any code. It describes the existing project: a Python CLI + FastAPI + SQLite dashboard that scrapes 5 Indian regulatory data sources (insider trading, SAST Reg 29, bulk/block deals, FII/DII, MF portfolios/shareholding).

You are now **extending** this project with three new features. Preserve all existing files, tables, scrapers, endpoints, and dashboard behavior. Only add or ALTER — never drop or rewrite what's already working.

---

## Features to Implement

### Feature 1 — Multi-Source Signal Clustering (the primary feature)

**Why:** The alpha isn't any single signal — it's convergence. When a promoter buys, an FII increases holding, and a block deal hits the same stock within 30 days, that's conviction. We flag these clusters and score them.

### Feature 2 — Promoter Buying Streak Detection

**Why:** Three distinct insiders at the same company buying within 90 days is a far stronger signal than any single insider buy. Detect and flag these streaks.

### Feature 3 — Fundamentals Enrichment from Screener.in

**Why:** A cluster on a debt-laden, low-ROCE company is a trap. A cluster on a high-ROCE, clean-balance-sheet company is a setup. We attach a minimal fundamentals layer to every signalled stock — ROCE, Debt/Equity, Interest Coverage, FCF conversion, Valuation — and compute a quality score.

---

## New Files to Create

```
project/
├── smart_money/
│   ├── __init__.py
│   └── cluster_detector.py        # Clustering + promoter streak logic
├── scrapers/
│   └── screener_fundamentals.py   # Scraper for Screener.in
└── tests/
    ├── __init__.py
    ├── test_cluster_detector.py
    └── test_fundamentals.py
```

## Files to Extend (add-only, do NOT rewrite)

- `db.py` — add 3 new tables
- `api.py` — add 4 new endpoints
- `scheduler.py` — add 2 new jobs
- `config.py` — add new constants
- `dashboard/index.html` — add new "Stock Intelligence" tab/section
- `requirements.txt` — add new deps

---

## Feature 1 — Multi-Source Signal Clustering

### Logic

For each stock symbol that appears in any signal source within the last 30 days:
1. Count distinct signal sources that fired
2. Weight each signal by type + size
3. Compute a cluster score
4. Store clusters above a minimum threshold

### New DB Table

```sql
CREATE TABLE IF NOT EXISTS signal_clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    company_name TEXT,
    cluster_score REAL NOT NULL,
    cluster_tier TEXT NOT NULL,          -- ELITE, HIGH, MEDIUM
    source_count INTEGER NOT NULL,       -- count of distinct sources hit
    sources_hit TEXT NOT NULL,           -- comma-sep: "INSIDER_BUY,BLOCK_DEAL,SAST"
    insider_buy_count INTEGER DEFAULT 0,
    sast_count INTEGER DEFAULT 0,
    bulk_block_count INTEGER DEFAULT 0,
    mf_accumulation BOOLEAN DEFAULT 0,   -- from shareholding pattern QoQ delta
    total_transaction_value REAL,        -- sum of ₹ across all signals in window
    first_signal_date TEXT,
    last_signal_date TEXT,
    window_days INTEGER DEFAULT 30,
    computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(symbol, last_signal_date)
);
```

### Scoring Formula

Base weights per source (per occurrence within window):

| Source | Weight |
|--------|--------|
| Insider buy — Promoter | 30 |
| Insider buy — Director / KMP | 15 |
| SAST acquisition (holding increased) | 25 |
| Block deal — BUY | 20 |
| Bulk deal — BUY | 15 |
| MF accumulation (QoQ mf_pct increase ≥ 1%) | 20 |


Multipliers applied to the summed base:

| Condition | Multiplier |
|-----------|-----------|
| 3+ distinct sources hit | × 1.3 |
| Promoter streak detected for this symbol (see Feature 2, ≥ MODERATE) | × 1.25 |

Final score capped at 100.

### Tier Mapping

- Score ≥ 70 → **ELITE**
- Score ≥ 50 → **HIGH**
- Score ≥ 30 → **MEDIUM**
- Below 30 → don't store

### Function Signatures

```python
# smart_money/cluster_detector.py

def compute_cluster_score(symbol: str, window_days: int = 30) -> dict:
    """
    Compute cluster score for a single symbol.
    Returns: {
        symbol, company_name, cluster_score, cluster_tier,
        source_count, sources_hit (list), insider_buy_count,
        sast_count, bulk_block_count, mf_accumulation,
        total_transaction_value, first_signal_date, last_signal_date
    }
    Returns None if score < CLUSTER_MIN_SCORE.
    """

def get_symbols_with_recent_signals(window_days: int = 30) -> list[str]:
    """Distinct symbols appearing in any signal table in the window."""

def refresh_cluster_table(window_days: int = 30) -> int:
    """
    Recompute clusters for all signalled symbols.
    Upsert into signal_clusters. Returns number of clusters stored.
    """
```

## Feature 2 — Promoter Buying Streak Detection

### Logic

For each symbol, count distinct insiders (by `insider_name`) with `transaction_type = 'Buy'` AND `person_category IN ('Promoters', 'Promoter Group', 'Director', 'Key Managerial Personnel')` in rolling 90-day window.

Also apply the noise filter: exclude `mode_of_acquisition` values that are not genuine market buys (`ESOP`, `Gift`, `Inter-se Transfer`, `Off Market`, `Rights Issue`, `Bonus`). Only count `Market Purchase` / `Open Market` / similar.

### New DB Table

```sql
CREATE TABLE IF NOT EXISTS promoter_streaks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    company_name TEXT,
    distinct_insiders INTEGER NOT NULL,
    insider_names TEXT,                  -- comma-separated
    total_value REAL,                    -- sum of market-purchase buys in window
    window_start_date TEXT,
    window_end_date TEXT,
    streak_strength TEXT NOT NULL,       -- WEAK, MODERATE, STRONG, ELITE
    computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(symbol, window_end_date)
);
```

### Strength Tiers

- 2 insiders → **WEAK**
- 3 insiders → **MODERATE**
- 4 insiders → **STRONG**
- 5+ insiders, OR (3+ insiders AND total_value > ₹10 Cr) → **ELITE**

### Function Signatures

```python
# smart_money/cluster_detector.py (same file)

VALID_BUY_MODES = {"Market Purchase", "Open Market", "On Market"}  # extend as needed

def detect_promoter_streaks(window_days: int = 90) -> list[dict]:
    """Scan insider_trades, return streaks with distinct_insiders >= 2."""

def refresh_streak_table(window_days: int = 90) -> int:
    """Upsert streaks into promoter_streaks. Returns count."""
```


## Feature 3 — Fundamentals from Screener.in

### Source

`https://www.screener.in/company/{SYMBOL}/consolidated/` (use `/consolidated/` — cleaner data; fall back to standalone for companies that don't have consolidated). Public pages, no auth required.

### Fetch Strategy

1. GET the HTML page with a standard browser User-Agent.
2. Parse with BeautifulSoup (lxml parser).
3. Screener exposes most data in labeled HTML sections:
   - **Top ratios bar** (Market Cap, Current Price, PE, Book Value, Dividend Yield, ROCE, ROE)
   - **Quarterly / Annual tables** (sales, profit, OPM)
   - **Balance sheet section** (borrowings, equity)
   - **Cash flows section** (operating CF, capex → FCF)
   - **Ratios section** (ROCE historical, ROE historical)
   - **Shareholding pattern** (promoter holding, pledge)

4. Respect rate limit: **1.5 seconds** between requests.
5. On parse failure for any single field, store NULL — never crash the whole fetch. Log which fields failed.

### Metrics to Extract / Compute

| Metric | Source | DB Column | Type |
|--------|--------|-----------|------|
| Sector | Top section | `sector` | TEXT |
| Industry | Top section | `industry` | TEXT |
| Market cap (₹ Cr) | Top ratios | `market_cap_cr` | REAL |
| Current price | Top ratios | `current_price` | REAL |
| Current ROCE | Top ratios | `roce_current` | REAL |
| 3-yr avg ROCE | Annual ratios table | `roce_3yr_avg` | REAL |
| 5-yr avg ROCE | Annual ratios table | `roce_5yr_avg` | REAL |
| Current ROE | Top ratios | `roe_current` | REAL |
| 5-yr avg ROE | Annual ratios table | `roe_5yr_avg` | REAL |
| Debt-to-Equity | Balance sheet: Borrowings / (Share Capital + Reserves) | `debt_to_equity` | REAL |
| Interest Coverage | (Operating Profit) / Interest | `interest_coverage` | REAL |
| 5-yr sales CAGR | Growth section | `sales_cagr_5yr` | REAL |
| 5-yr profit CAGR | Growth section | `profit_cagr_5yr` | REAL |
| Sales growth std dev (5yr YoY) | Compute from annual sales array | `sales_growth_stddev` | REAL |
| 5-yr cumulative FCF | Sum of (Cash from Operations - Capex) over last 5 years | `fcf_5yr_cumulative` | REAL |
| 5-yr cumulative Net Profit | Sum of net profit over last 5 years | `profit_5yr_cumulative` | REAL |
| FCF conversion | `fcf_5yr_cumulative / profit_5yr_cumulative` | `fcf_conversion` | REAL |
| Current PE | Top ratios | `pe_current` | REAL |
| 5-yr median PE | Screener's "Median PE" field if available, else NULL | `pe_5yr_median` | REAL |
| PE vs median ratio | `pe_current / pe_5yr_median` | `pe_vs_median` | REAL |
| PEG ratio | `pe_current / profit_cagr_5yr` (NULL if CAGR ≤ 0) | `peg_ratio` | REAL |
| Promoter holding % | Shareholding pattern (most recent quarter) | `promoter_holding_pct` | REAL |
| Promoter pledge % | Shareholding pattern | `promoter_pledge_pct` | REAL |

**Note:** If 5-yr median PE parsing is too brittle, fall back to NULL for that one field. Don't block the rest of the fetch on it.

### New DB Table

```sql
CREATE TABLE IF NOT EXISTS stock_fundamentals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL UNIQUE,
    company_name TEXT,
    sector TEXT,
    industry TEXT,

    market_cap_cr REAL,
    current_price REAL,

    -- Quality
    roce_current REAL,
    roce_3yr_avg REAL,
    roce_5yr_avg REAL,
    roe_current REAL,
    roe_5yr_avg REAL,

    -- Leverage
    debt_to_equity REAL,
    interest_coverage REAL,

    -- Growth
    sales_cagr_5yr REAL,
    profit_cagr_5yr REAL,
    sales_growth_stddev REAL,

    -- Cash quality
    fcf_5yr_cumulative REAL,
    profit_5yr_cumulative REAL,
    fcf_conversion REAL,

    -- Valuation
    pe_current REAL,
    pe_5yr_median REAL,
    pe_vs_median REAL,
    peg_ratio REAL,

    -- Ownership
    promoter_holding_pct REAL,
    promoter_pledge_pct REAL,

    -- Computed
    quality_score REAL,
    quality_tier TEXT,                   -- EXCELLENT, GOOD, AVERAGE, POOR, AVOID
    red_flags TEXT,                      -- JSON array of strings

    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    source TEXT DEFAULT 'Screener.in'
);
```

### Quality Scoring

```python
def compute_quality_score(f: dict) -> tuple[float, str, list[str]]:
    """
    Return (score 0-100, tier, red_flags list).
    Missing (None) metrics contribute 0 and are noted but don't crash.
    """
    score = 0.0
    red_flags = []

    # ROCE (max 30)
    roce = f.get('roce_5yr_avg')
    if roce is not None:
        if roce >= 20: score += 30
        elif roce >= 15: score += 22
        elif roce >= 10: score += 12
        else:
            red_flags.append(f"Low 5yr ROCE: {roce:.1f}%")

    # Debt-to-Equity (max 20)
    de = f.get('debt_to_equity')
    if de is not None:
        if de < 0.3: score += 20
        elif de < 0.7: score += 14
        elif de < 1.2: score += 7
        else:
            red_flags.append(f"High D/E: {de:.2f}")

    # Interest coverage (flag only, also a hard AVOID trigger below)
    ic = f.get('interest_coverage')
    if ic is not None and ic < 2.5:
        red_flags.append(f"Weak interest coverage: {ic:.1f}x")

    # FCF conversion (max 20)
    fcf_conv = f.get('fcf_conversion')
    if fcf_conv is not None:
        if fcf_conv > 0.8: score += 20
        elif fcf_conv > 0.5: score += 12
        elif fcf_conv > 0.3: score += 5
        else:
            red_flags.append(f"Poor FCF conversion: {fcf_conv:.1%}")

    # Growth consistency (max 15)
    sales_cagr = f.get('sales_cagr_5yr')
    stddev = f.get('sales_growth_stddev')
    if sales_cagr is not None and sales_cagr >= 12:
        score += 10
        if stddev is not None and stddev < 10:
            score += 5

    # Valuation (max 15)
    pe_ratio = f.get('pe_vs_median')
    if pe_ratio is not None:
        if pe_ratio < 0.8: score += 15
        elif pe_ratio < 1.1: score += 10
        elif pe_ratio < 1.5: score += 5
        else:
            red_flags.append(f"PE {pe_ratio:.1f}x its 5yr median")

    # Pledge flag (doesn't subtract, but forces AVOID if severe)
    pledge = f.get('promoter_pledge_pct')
    if pledge is not None and pledge > 25:
        red_flags.append(f"Promoter pledge {pledge:.1f}%")

    # Tier mapping
    if score >= 75: tier = "EXCELLENT"
    elif score >= 55: tier = "GOOD"
    elif score >= 35: tier = "AVERAGE"
    elif score >= 20: tier = "POOR"
    else: tier = "AVOID"

    # Hard overrides to AVOID
    if de is not None and de > 2.5:
        tier = "AVOID"
    if pledge is not None and pledge > 50:
        tier = "AVOID"
    if ic is not None and ic < 1.5:
        tier = "AVOID"

    return round(score, 1), tier, red_flags
```

### Function Signatures

```python
# scrapers/screener_fundamentals.py

def fetch_fundamentals(symbol: str) -> dict | None:
    """Fetch + parse all metrics for one symbol. Returns None on hard failure."""

def refresh_fundamentals(symbols: list[str] = None, force: bool = False) -> int:
    """
    Refresh fundamentals. If symbols is None, refresh all symbols appearing
    in any signal table in last FUNDAMENTALS_LOOKBACK_DAYS. Skip symbols
    refreshed in last FUNDAMENTALS_STALE_DAYS unless force=True.
    Returns count refreshed.
    """

def get_symbols_needing_fundamentals() -> list[str]:
    """
    Distinct symbols in insider_trades, sast_disclosures, or bulk_block_deals
    in last 90 days that either (a) have no row in stock_fundamentals, or
    (b) have fetched_at older than FUNDAMENTALS_STALE_DAYS.
    """
```

### When to Run
I need a button on the UI to trigger all this, once it starts , I need a minimal UI to see the progress, steps being performed and time taken per task.

## New API Endpoints

Add to `api.py`:

```
GET /api/clusters?tier=ELITE,HIGH&days=30
    → Returns signal_clusters filtered, LEFT JOIN stock_fundamentals for quality context

GET /api/promoter-streaks?min_insiders=3&days=90
    → Returns promoter_streaks filtered, LEFT JOIN stock_fundamentals

GET /api/fundamentals/{symbol}
    → Returns full stock_fundamentals row for one symbol, or 404

GET /api/stock-intelligence?days=30&quality_tier=EXCELLENT,GOOD&min_cluster_score=30
    → The main new endpoint powering the new dashboard tab
    → One row per signalled stock with:
      {
        symbol, company_name, sector,
        latest_signal_date, latest_signal_type,
        cluster_score, cluster_tier, sources_hit, source_count,
        promoter_streak_strength, distinct_insiders,
        quality_score, quality_tier, red_flags,
        roce_5yr_avg, debt_to_equity, interest_coverage,
        fcf_conversion, pe_current, pe_vs_median,
        promoter_holding_pct, promoter_pledge_pct
      }
    → Default sort: cluster_score DESC, then quality_score DESC
```

---

## Dashboard — New "Stock Intelligence" Tab

Extend `dashboard/index.html`. Add a new tab alongside the existing signal feed. **Do not modify the existing signal feed layout.**

### Tab Structure

```
┌──────────────────────────────────────────────────────────────────┐
│  [Signal Feed]  [Stock Intelligence]  ← new tab                  │
├──────────────────────────────────────────────────────────────────┤
│  FILTERS:                                                         │
│  [Quality Tier ▼] [Min Cluster Score: 30] [Sector ▼] [Search]   │
│  ☐ Has promoter streak   ☐ ELITE clusters only                   │
├──────────────────────────────────────────────────────────────────┤
│  Symbol | Company    | Cluster | Sources | Quality   | ROCE 5y  │
│  -------|------------|---------|---------|-----------|----------│
│  HDFC   | HDFC Bank  | 82 🔴   | I,B,S   | GOOD 62   | 16.8%    │
│  TCS    | TCS Ltd    | 71 🔴   | I,B     | EXCELLENT | 45.2%    │
│  INFY   | Infosys    | 58 🟠   | I,S     | EXCELLENT | 32.1%    │
│                                                                  │
│  More columns: D/E | Int Cov | FCF Conv | PE (vs med) | Prom % |│
│                Pledge % | Streak | Red Flags                    │
└──────────────────────────────────────────────────────────────────┘
```

### Full Column List

| Column | Notes |
|--------|-------|
| Symbol | Stock ticker, click → drill-down modal |
| Company | Short name, truncate at 30 chars |
| Latest Signal | Type + "N days ago" |
| Cluster Score | 0-100, colored badge (ELITE=red, HIGH=orange, MEDIUM=yellow) |
| Sources | Compact badges: I (insider), S (SAST), B (block), Bk (bulk), M (MF) |
| Quality | Tier badge: EXCELLENT=dark green, GOOD=green, AVERAGE=yellow, POOR=orange, AVOID=red |
| ROCE 5y | % |
| D/E | 2 decimals; red text if > 1.5 |
| Int Cov | `Nx`; red if < 2.5 |
| FCF Conv | %; red if < 30% |
| PE | `24.3 (1.2x)` where 1.2x = PE ÷ 5yr median; orange if > 1.5x |
| Prom % | Current quarter promoter holding |
| Pledge % | Red bold if > 25% |
| Streak | Badge with tier if present (MODERATE, STRONG, ELITE) |
| Red Flags | Count pill; hover/click → list popup |

### Drill-Down Modal (click symbol)

On row click, open a modal showing:

1. **Header**: Symbol, company, sector, market cap, cluster tier, quality tier
2. **Signal timeline**: all signals for this stock in last 90 days, chronological, one line each
3. **Fundamentals panel**: all metrics from stock_fundamentals in a clean 2-column layout
4. **Red flags list**: full list with context
5. **Shareholding pattern mini-chart**: promoter/FII/DII/MF % across last 4 quarters (line chart, lightweight Chart.js)

### Styling

- Match the existing dashboard's color palette and typography — do NOT introduce a new design system
- Use vanilla HTML/CSS + minimal JS if the existing dashboard is plain HTML
- Tables sortable by clicking column headers
- Initial load: fetch `/api/stock-intelligence?days=30`, render table
- Filters trigger re-fetch with updated query params

---


Also trigger `refresh_fundamentals([new_symbols])` inline at the end of each scraper run for any symbols not already in `stock_fundamentals`.

---

## Config Additions

Add to `config.py`:

```python
# Screener.in
SCREENER_BASE_URL = "https://www.screener.in"
SCREENER_COMPANY_URL = "https://www.screener.in/company/{symbol}/consolidated/"
SCREENER_FALLBACK_URL = "https://www.screener.in/company/{symbol}/"
SCREENER_RATE_LIMIT_SECONDS = 1.5
SCREENER_TIMEOUT_SECONDS = 20

# Clustering
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

# Promoter streaks
STREAK_WINDOW_DAYS = 90
STREAK_MIN_INSIDERS = 2
STREAK_ELITE_VALUE_THRESHOLD = 100_000_000  # ₹10 Cr

VALID_BUY_MODES = {"Market Purchase", "Open Market", "On Market"}

# Fundamentals
FUNDAMENTALS_STALE_DAYS = 7
FUNDAMENTALS_LOOKBACK_DAYS = 90  # enrich symbols with any signal in last N days
```

---

## requirements.txt Additions

Append:
```
beautifulsoup4>=4.12.0
lxml>=4.9.0
pytest>=7.4.0
```

(`requests`, `sqlite3`, `fastapi`, `schedule`, `python-dateutil` already present per CLAUDE.md.)

---

## Testing

### `tests/test_cluster_detector.py`

Set up an in-memory SQLite fixture. Insert synthetic rows:

1. **Single-source stock** (only 1 insider buy) → no cluster stored (score < 30)
2. **Two-source stock** (promoter buy + block deal buy, ₹20 Cr total) → MEDIUM cluster
3. **Three-source stock** (promoter buy + SAST + block deal) → HIGH or ELITE (1.3x multiplier)
4. **High-value stock** (promoter buy + block deal, ₹80 Cr) → 1.2x multiplier applied
5. **Stock with streak** → cluster gets 1.25x multiplier when streak is MODERATE+

Assert: correct score, correct tier, correct sources_hit list, correct multipliers applied.

### `tests/test_fundamentals.py`

Test `compute_quality_score()` with mock dicts:

1. **Excellent**: ROCE 25%, D/E 0.2, FCF conv 0.9, sales CAGR 15% low variance, PE 0.9x median → EXCELLENT, score 75+
2. **Good**: ROCE 17%, D/E 0.6, FCF conv 0.6, moderate growth → GOOD
3. **Average**: ROCE 11%, D/E 0.9, FCF conv 0.4 → AVERAGE
4. **Poor**: ROCE 8%, D/E 1.4, FCF conv 0.2 → POOR
5. **Avoid override — high D/E**: D/E 3.0 → AVOID regardless of other metrics
6. **Avoid override — high pledge**: pledge 55% → AVOID
7. **Avoid override — weak coverage**: interest_coverage 1.2 → AVOID
8. **Missing data**: most fields None → score 0, tier AVOID, no crash
9. **Red flags accumulate correctly**: verify red_flags list contents

Run with `pytest tests/` — all must pass.

---

## Implementation Order

Do this sequentially. Stop and verify after each step before moving on.

1. **DB schema** — extend `db.py` with the 3 new tables. Run a quick check that schema creation succeeds and existing tables are untouched.
2. **Cluster detector logic** — implement `smart_money/cluster_detector.py`. Write tests. Run `pytest tests/test_cluster_detector.py`.
3. **Promoter streak logic** — add to same file. Extend tests. Ensure cluster scorer correctly applies the streak multiplier.
4. **Screener scraper** — implement `scrapers/screener_fundamentals.py`. Smoke test: `python -m scrapers.screener_fundamentals --symbol RELIANCE`. Verify all fields parse or explicitly NULL. Print the parsed dict.
5. **Quality scorer tests** — `pytest tests/test_fundamentals.py`.
6. **Backfill fundamentals** — run `refresh_fundamentals()` across all historically-signalled symbols. Respect rate limits. Log progress.
7. **API endpoints** — add the 4 new endpoints to `api.py`. Test each with `curl` or the FastAPI auto-docs.
8. **Dashboard tab** — extend `dashboard/index.html`. Add the Stock Intelligence tab with table + filters + drill-down modal.
9. **Scheduler wiring** — add the new jobs. Verify they'd fire at the right times (log a dry-run).
10. **End-to-end smoke test** — run all scrapers + cluster refresh + fundamentals refresh + open dashboard. Confirm at least one cluster appears in the UI with fundamentals context.

---

## Non-Negotiable Rules

1. **Do NOT modify existing scraper logic.** Only add enrichment hooks at the *end* of each scraper run (to queue new symbols for fundamentals fetch).
2. **Do NOT drop or recreate existing tables.** Use `CREATE TABLE IF NOT EXISTS` for new tables only.
3. **Do NOT break existing API endpoints or dashboard sections.** Add new only.
4. **Handle Screener.in parse failures gracefully.** Any single field that fails to parse → store NULL + log. Never crash the whole fetch.
5. **Rate limit Screener at 1.5s** between requests. Running the full backfill is fine as long as it's throttled.
6. **Never store 0 or empty string where NULL is correct.** NULL is semantically different from 0 in the scoring logic.
7. **All new functions need docstrings** explaining purpose, inputs, outputs, and edge cases.
8. **Log, don't crash.** Every scraper and job must catch top-level exceptions, log them clearly, and continue.

---

## Acceptance Criteria

When complete, I must be able to:

1. Run `python run.py --all` and have it scrape → streaks → clusters → fundamentals without errors
2. Hit `GET /api/stock-intelligence?days=30` and receive enriched rows
3. Open the dashboard, click the new "Stock Intelligence" tab, and see a sortable filtered table of signalled stocks with quality overlay
4. Click a stock row and see the drill-down modal with full signal history and fundamentals
5. See at least one MEDIUM+ cluster and one streak in the data (assuming enough historical signals exist)
6. Run `pytest tests/` and see all tests pass

Start with Step 1 (DB schema). After it's in place and verified and continue doing next steps without asking questions