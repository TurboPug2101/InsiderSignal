# Challenges, Bugs & Decisions — Smart Money Tracker

A candid record of everything non-trivial that happened while building this project: where things broke, the choices made under uncertainty, and the reasoning behind each decision.

---

## 1. NSE API Requires a Cookie Dance (Architecture Decision)

**The problem:** NSE India blocks all direct API calls. A fresh `requests.get()` to any `/api/` endpoint returns a 401 or an empty body. NSE requires a browser-like session: you must first hit the homepage (or the relevant page) to receive cookies (`nseappid`, `nsit`, `bm_sv`), and only then will the API endpoints respond with data.

**The choice:** Build a singleton `NSESession` class that holds a `requests.Session`, auto-refreshes cookies every 5 minutes, and enforces a 1-second rate limit between requests.

**Why singleton:** All 5 scrapers share the same session object. If each scraper created its own session, they'd each trigger a separate cookie refresh, multiplying homepage hits and risking IP blocks. One shared session means one set of cookies used across everything.

**Trade-off accepted:** The singleton is a module-level global. This is fine for a CLI/cron tool but would need rethinking for a multi-process production server (where each worker would have its own session — acceptable here since we only run one process).

**Cookie TTL of 5 minutes:** Chosen conservatively. NSE cookies in practice seem to last longer, but 5 minutes balances freshness against unnecessary homepage hits.

---

## 2. SAST Regulation 29 API — Endpoint Silently Changed (Bug)

**What happened:** The endpoint documented everywhere (including in CLAUDE.md) was:
```
/api/corporates-sast?index=equities&from_date=...&to_date=...&fo_flag=Y&reg_type=reg29
```
This returned 404 for every URL variant tried — with or without `fo_flag`, with or without `index`, with different referers. The NSE page itself loaded fine (200), but the API behind it had been moved.

**How it was found:** Inspected the HTML of the Reg29 page and found `data-page="corporate-sast-reg29"` on a tab container element. NSE uses this attribute to map page tabs to their API endpoints. The new URL was:
```
/api/corporate-sast-reg29?from_date=...&to_date=...
```
Note: `corporates` (plural) became `corporate` (singular), and `sast` + `reg29` are now joined with a hyphen as a single endpoint name.

**Field names also changed:** The new API response has different field names than what CLAUDE.md documented:
- `companyName` → `company`
- `acqType` → `acqSaleType` (values also changed: "Sale" instead of "Disposal")
- `percOfSharesAcq` → `totAcqShare`
- `aftAcqSharesPer` → `totAftShare`
- `befAcqSharesPer` — removed entirely; holding before must be approximated as `totAftShare - totAcqShare`
- `acquirerDate` is now a range string like `"17-APR-2026 to 17-APR-2026"` — split on ` to ` to get the end date

**Lesson:** NSE APIs are unofficial and undocumented. They change without notice. The right long-term approach is to always inspect `data-page` attributes on the live page to derive the current API endpoint name rather than hardcoding URLs.

---

## 3. Screener.in — Verifying Public Access Before Building (Process Decision)

**The uncertainty:** Screener.in requires login for some data. The risk was: build a full scraper, then discover critical metrics (ROCE history, D/E, cash flows) are behind a login wall and return placeholder/truncated data for logged-out users.

**What was done:** A standalone verification script was written and run first, checking three symbols (RELIANCE, TCS, INFY) for every data section before writing any production code. Result:
- No login wall indicators on any symbol
- 10-12 years of annual data in P&L, Balance Sheet, and Cash Flow sections
- All key metrics (borrowings, equity, operating profit, cash from operations) present

**The value:** Took 10 minutes upfront, saved potentially hours of building a scraper against data that might not exist publicly.

---

## 4. Market Cap Parsing — Nested HTML Structure (Bug)

**What happened:** The `market_cap_cr` field parsed as `null` for all symbols even though the top ratios bar visibly showed it.

**Root cause:** The Market Cap value in Screener.in HTML is split across nested elements:
```html
<span class="nowrap value">
  ₹
  <span class="number">9,44,502</span>
  Cr.
</span>
```
The `_safe_float()` function stripped `₹` and commas, but not `Cr.` suffix. So `get_text(strip=True)` on the outer `.value` span returned `₹9,44,502Cr.`, and after stripping `₹` the result was `9,44,502Cr.` which `float()` rejected.

**Fix:** For market cap (and current price), target the inner `<span class="number">` specifically, avoiding the surrounding currency symbols and suffix text entirely.

**Why not just strip "Cr." globally:** Other fields like "High / Low" (`₹1,612/1,285`) would be mangled. The fix was surgical — only use `.number` for fields that have that structure.

---

## 5. Sector/Industry Extraction — Wrong HTML Location (Bug)

**What happened:** `sector` and `industry` both returned `null`. The original code looked for links containing `/screen/industry/` or `/screen/sector/` in their `href`. No such links exist on Screener.in.

**Where the data actually lives:** Inside a `<p class="sub">` paragraph immediately after the `<h2>Peer comparison</h2>` heading. The links use a `/market/IN08/IN0801/IN080101/` URL pattern and have `title="Broad Sector"`, `title="Sector"`, `title="Industry"` attributes.

**Fix:** Find the "Peer comparison" h2, walk to its next sibling `<p>`, then read `<a title="Broad Sector">` for sector and `<a title="Industry">` for industry.

**Learning:** Screener.in's URL patterns for sector/industry classification use their internal market hierarchy IDs (e.g., `IN08`), not human-readable slugs. Can't guess these from the field names — need to inspect the live HTML structure.

---

## 6. Historical ROCE/ROE — No Dedicated Section (Design Decision)

**The expectation:** CLAUDE.md specified extracting historical ROCE/ROE from the `#ratios` section. In reality, the `#ratios` section on Screener.in only contains working capital metrics: Debtor Days, Inventory Days, Days Payable, Cash Conversion Cycle, Working Capital Days. No ROCE or ROE rows.

**The choice:** Compute ROCE and ROE historically from raw P&L and Balance Sheet data:
- ROCE = Operating Profit / (Equity Capital + Reserves + Borrowings) per year
- ROE = Net Profit / (Equity Capital + Reserves) per year
- Average the last 3 and 5 annual values

**Alternative considered:** Parse Screener's "Key Metrics" section if it exists, or use the "Ratios" page (separate URL). Rejected because it would require an extra HTTP request per symbol and the section structure varies by company type.

**What's still null:** `roe_5yr_avg` sometimes returns null when net profit rows can't be matched reliably across the table (different label wording between companies). `pe_5yr_median` is null for all — Screener only shows this to logged-in users.

---

## 7. Two `switchTab` Function Declarations — Silent Infinite Recursion (Bug)

**What happened:** Clicking any nav tab (Signals, Stock Intelligence, Stock Deep Dive) did nothing. No error visible in normal use.

**Root cause:** The dashboard had two `function switchTab` declarations. The second was added at the bottom of the `<script>` block to lazily load Chart.js when the intelligence tab opened, using this pattern:
```javascript
const _origSwitchTab = switchTab;
function switchTab(name, el) {
  _origSwitchTab(name, el);
  // ... Chart.js loading
}
```
This looks safe but breaks due to JavaScript hoisting. `function` declarations are hoisted to the top of their scope before any code executes. Both declarations get hoisted; the second overwrites the first. By the time `const _origSwitchTab = switchTab` runs, `switchTab` already refers to the second (overwriting) declaration. So `_origSwitchTab` and `switchTab` point to the same function — calling `_origSwitchTab(name, el)` inside `switchTab` recursively calls itself until the stack overflows. The browser silently swallows the "Maximum call stack size exceeded" error; the user sees nothing happen.

**Fix:** Merge the Chart.js lazy-load logic directly into the single `switchTab` function. Remove the second declaration entirely.

**Why this pattern is tempting but wrong:** The "save original, wrap it" pattern works for reassigned function expressions (`let fn = ...; fn = wrap(fn)`) but not for `function` declarations due to hoisting. The correct workaround is either a named expression or a simple `if` branch inside the original function.

---

## 8. SSE Streaming Blocking the Event Loop (Bug)

**What happened:** Clicking "Run Analysis" opened the progress modal but showed no progress — all 8 steps appeared done simultaneously at the very end (or the connection timed out).

**Root cause:** All scrapers are synchronous blocking code: they make HTTP requests (taking 1-5 seconds each) and do SQLite writes. The SSE endpoint used `async def generate()` with these blocking calls directly inside. In Python's asyncio model, any synchronous blocking call inside an async function blocks the entire event loop. Uvicorn couldn't flush the response buffer to the browser while a scraper was running, so the browser received no data until all steps completed.

**Fix:** Move each blocking scraper call into `loop.run_in_executor(None, ...)`, which runs it in a thread pool. The event loop stays free between steps and can flush the stream. Add `await asyncio.sleep(0)` after each `yield` to explicitly yield control back to the event loop so uvicorn sends the buffered bytes.

**Why not rewrite scrapers as async:** The scrapers use `requests` (synchronous) and `sqlite3` (synchronous). Rewriting all of them as async (using `httpx` + `aiosqlite`) would be a large refactor with no other benefit for this project. `run_in_executor` gives true non-blocking behavior without changing the scraper internals.

**Trade-off:** Thread pool execution means multiple scrapers could theoretically run concurrently if triggered simultaneously. Since each scraper has its own state and they write to separate DB tables, this is safe. The sequential `await` in `generate()` ensures they run one at a time anyway.

---

## 9. MF Portfolios — Full AMC Scraping vs Shareholding Pattern Approach (Design Decision)

**The full approach (rejected):** Parse 40+ AMC portfolio disclosures from Excel/PDF files published monthly. Each AMC has its own URL, file format, and update schedule.

**Problems with this approach:**
- 40+ AMCs, each requiring a separate scraper with custom Excel/PDF parsing
- Files are published 15 days after month-end, so data is always stale
- PDF parsing is brittle (font encoding, table detection varies by AMC)
- Rate limiting and download management across 40 different domains

**The chosen approach:** Use NSE's shareholding pattern API (`/api/corporate-share-holdings`) per symbol. This gives quarterly breakdowns of promoter/FII/DII/MF holding percentages already aggregated by the exchange from all regulatory filings.

**Pros of shareholding approach:**
- Single clean JSON API
- Data is exchange-verified and SEBI-compliant
- Covers all funds in aggregate, not just the ones we remember to add
- Gives QoQ deltas directly (compare current vs previous quarter)

**Cons:**
- Quarterly granularity only (monthly AMC data is more frequent)
- Can't see which specific fund increased — only the aggregate MF %
- Requires a watchlist (~200 symbols) and one API call per symbol

**Verdict:** The shareholding pattern approach gives 80% of the signal value (is aggregate MF holding going up or down?) with 5% of the implementation complexity. The full AMC approach would give individual fund attribution but at a maintenance cost that's not justified for this tool.

---

## 10. Signal Clustering — Per-Occurrence vs Per-Source Weighting (Design Decision)

**The question:** Should multiple insider buys from the same company in the window each contribute weight, or should insider buying count only once (as a binary "source present" flag)?

**Per-occurrence chosen:** Each individual buy event adds weight. Three promoters buying in the same 30-day window adds 3 × 30 = 90 base points, not 30.

**Reasoning:** Multiple distinct individuals making independent buy decisions in a short window is a qualitatively stronger signal than one person buying once. The weight accumulation captures conviction level, not just source presence.

**Side effect:** Stocks with many small insider purchases score as ELITE even if they're a single-source cluster. For example, DIAMINESQ with 8 distinct insiders buying = score 240 → capped at 100 → ELITE. This is intentional and correct — 8 promoters buying is genuinely elite conviction.

**Cap at 100:** Without a cap, scores become meaningless. The cap means "maximum conviction" rather than a raw sum. The tier thresholds (30/50/70) are the real discriminators; the cap prevents outliers from distorting comparisons.

---

## 11. Promoter Streak — Filtering Noise from Non-Market Buys (Design Decision)

**The problem:** Not all insider "buys" in the regulatory data represent genuine market conviction. Many are mechanical non-discretionary events:
- ESOPs (received as compensation, not chosen)
- Bonus shares (automatic corporate action)
- Rights issue (priced below market, almost always exercised)
- Gift / inter-se transfer (moving shares between family members, no cash changes hands)
- Off-market transfers (pre-arranged, price often arbitrary)

Including these would make streaks meaningless — a rights issue where 5 directors exercise entitlements would show as a 5-insider ELITE streak.

**The filter:** Only count acquisitions where `mode_of_acquisition` is in `{"Market Purchase", "Open Market", "On Market"}`. These represent deliberate open-market buying at prevailing prices — real skin-in-the-game signals.

**Edge case handled:** Old data in the DB may have NULL or empty `mode_of_acquisition` (field wasn't always populated). The filter allows NULL/empty through rather than excluding it — better to include ambiguous old records than to silently ignore all historical buys.

---

## 12. Quality Scoring — Threshold Values (Design Decision)

**The scoring thresholds (ROCE ≥ 20% = full marks, D/E < 0.3 = full marks, etc.) are somewhat arbitrary.** They represent reasonable rules of thumb for Indian mid-to-large cap equities, calibrated against known high-quality companies:

- TCS (ROCE ~63%, D/E ~0.1) → scores GOOD (70), not EXCELLENT — because PE vs median is null (no 5yr median data from Screener without login), suppressing the valuation component
- INFY (ROCE ~37%, D/E 0, FCF conv ~95%) → EXCELLENT (85)
- RELIANCE (ROCE ~9%, D/E 0.44, FCF conv negative) → POOR (24)

**Deliberate omission from score (but included as flags):** Interest coverage below 2.5x, pledge above 25%. These are warning signals but not automatically disqualifying — a company restructuring debt might have low coverage temporarily. They flag for investigation without overriding the quantitative score.

**Hard AVOID overrides:** D/E > 2.5, pledge > 50%, interest coverage < 1.5x. These are genuinely dangerous levels where the scoring formula's partial-credit approach is misleading. A D/E of 3.0 with great ROCE doesn't mean the company is investable.

---

## 13. Testing Strategy — Mocking DB vs In-Memory SQLite (Design Decision)

**The choice:** Mock the `query()` and `db_conn()` functions using `unittest.mock.patch` rather than spinning up a real in-memory SQLite DB for each test.

**Why mocking was chosen:**
- The cluster scoring and streak detection functions call `query()` with specific SQL — mocking lets tests control exactly what data the function "sees" without worrying about schema setup, date arithmetic in SQLite, or test isolation
- Faster (no DB overhead)
- The `compute_quality_score()` function is a pure function with no DB calls at all — trivially testable with dict inputs

**When an in-memory DB would be better:** If we were testing the SQL queries themselves (e.g., verifying that the date window filter actually works in SQLite). The current tests verify the scoring logic given pre-fetched data, not the data fetching itself. That's a reasonable scope for unit tests — integration tests against a real DB would be a separate layer.

---

## 14. SSE vs WebSocket vs Polling for Progress UI (Design Decision)

**Three options considered:**

| Approach | Complexity | Browser support | Reconnect | Bidirectional |
|----------|-----------|-----------------|-----------|---------------|
| Polling (setInterval) | Simplest | Universal | N/A | N/A |
| SSE (EventSource) | Simple | Universal | Auto | Server → client only |
| WebSocket | Complex | Universal | Manual | Bidirectional |

**Polling rejected:** Would require a separate "job status" table in the DB, a job queue, and state management. Over-engineered for a single-user tool triggered by a button.

**WebSocket rejected:** Bidirectional communication isn't needed — the server only needs to push progress to the browser, not receive messages mid-stream. WebSocket adds setup complexity (connection lifecycle, ping/pong) for no benefit here.

**SSE chosen** via `StreamingResponse` with `text/event-stream`: The server pushes data to the browser using a simple HTTP response that stays open. No WebSocket handshake, no polling overhead, auto-reconnect built into the browser's `EventSource` API. One HTTP POST opens the stream; the server yields events as each step completes. Simple, fits the use case exactly.

**Implementation note:** The initial implementation used `EventSource` (GET-only). Switched to `fetch()` + `ReadableStream` reader because the analysis trigger needs to be a POST (idempotent GET semantics don't apply here). `fetch()` gives the same streaming capability without the GET constraint.

---

## 15. SQLite vs PostgreSQL (Architecture Decision)

**SQLite chosen** despite the project running a FastAPI server.

**Why SQLite is fine here:**
- Single-user tool (one person running analysis, viewing dashboard)
- Write pattern is append-only, batched, infrequent (daily runs)
- Read pattern is small aggregations over <100k rows
- No concurrent writes from multiple processes
- Zero infrastructure: no separate DB server, no connection pooling, no migrations tooling needed
- The DB file is trivially backed up or moved

**When to reconsider:** If multiple users query the dashboard simultaneously, or if the analysis pipeline is parallelized across multiple worker processes. SQLite's write lock would become a bottleneck. For this project's scope, it's the right call.

---

## 16. Single HTML File vs React for Dashboard (Design Decision)

**Single HTML file chosen.**

**Why not React:**
- No build step (no npm, no webpack, no node_modules)
- The dashboard is served by FastAPI as a static file — a single `FileResponse`. With React, you'd need either a separate dev server or a build pipeline integrated into the Python project
- The data is fetched via REST API calls and rendered as HTML strings — this is entirely manageable in vanilla JS for a table-based dashboard
- React's value (component reuse, state management) pays off at scale; for 4 tabs with tables and one modal, it's overhead

**Cost paid:** The HTML file grew to ~1,400 lines. Vanilla JS for table sorting, filter state, and modal management is more verbose than React hooks. The code is less modular. Acceptable for a tool used by one person.

---

## 17. FCF Calculation — Operating CF + Investing CF vs Capex Extraction (Design Decision)

**The spec said:** `FCF = Cash from Operations - Capex (last 5 years cumulative)`.

**The problem:** Screener.in's cash flow table has three rows — Operating, Investing, Financing — not separate Capex and non-capex investing. The Investing CF line mixes capex (machinery, buildings) with acquisitions, investments in securities, and proceeds from asset sales.

**Options:**
1. Use `Operating CF + Investing CF` as a proxy for FCF (adds a negative investing number to operating)
2. Try to find a "Purchase of fixed assets" or "Capital expenditure" sub-row in the investing section
3. Treat investing CF as pure capex

**Option 1 chosen:** `FCF = Operating CF + Investing CF`. For most industrial/manufacturing companies this is a reasonable proxy. For companies with large M&A activity or investment portfolio churn (banks, holding companies), it overstates capex and understates FCF. For the purposes of a quality filter (FCF conversion > 0.8 is good), the directional signal is still valid even if the absolute number is imprecise.

**Why not option 2:** Screener.in doesn't consistently expose capex as a named sub-row across all companies. The P&L/Balance Sheet tables have clean consistent labels; cash flow sub-items vary significantly by industry and company structure. Building a brittle parser for a sub-row that may not exist would produce more nulls, not more accuracy.

---

## 18. `pe_5yr_median` Always Null (Known Limitation)

**What it is:** Screener.in shows "Median PE" on stock pages but only for logged-in users. For public (logged-out) access, the field either isn't rendered or is inside a component that requires the user's watchlist data.

**Impact:** `pe_vs_median` (PE ÷ 5yr median) is always null. This means the valuation component of the quality score (max 15 points) contributes nothing for any stock. Scores are systematically 15 points lower than they would be with full data.

**Decision:** Accept the null rather than implement login/session management for Screener.in. Adding cookie-based Screener authentication would require storing credentials, managing session expiry, and handling login flows — significant complexity for one metric. The other 85 points of the quality score are computed correctly and still discriminate well between companies.

---

## 19. Rate Limiting — Different Limits for NSE vs Screener.in

**NSE:** 1 second minimum between requests. NSE will IP-block aggressive scrapers. The shared `NSESession` singleton enforces this globally.

**Screener.in:** 1.5 seconds between requests. Screener is more tolerant than NSE but rate-limits aggressively above ~40 requests/minute. The 1.5s limit keeps us under ~40/min with headroom.

**Why not longer delays:** The fundamentals backfill (potentially 200+ symbols) takes 200 × 1.5s = 5 minutes at minimum. Longer delays compound into impractical run times. 1.5s was the shortest delay that didn't trigger 429s in testing.

**Fundamentals stale TTL (7 days):** Fundamental data (balance sheet, ROCE, PE) changes quarterly at most. A 7-day cache means the scraper only refetches a symbol's Screener page once per week, keeping total Screener requests minimal on subsequent runs.
