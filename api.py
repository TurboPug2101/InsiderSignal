"""FastAPI application — all endpoints for Smart Money Tracker."""

from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from typing import Optional, List
from collections import defaultdict
from datetime import datetime, timedelta
import asyncio
import feedparser
import json
import logging
import os
import time as time_module

from db import query, init_db, get_connection
from config import DB_PATH, GROQ_API_KEY

logger = logging.getLogger(__name__)

app = FastAPI(title="Smart Money Tracker", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup():
    init_db()


# ---------- Signals ----------

@app.get("/api/signals")
def get_signals(
    days: int = Query(7, ge=1, le=365),
    strength: Optional[str] = Query(None, description="Comma-separated: HIGH,MEDIUM,LOW,INFO"),
    symbol: Optional[str] = None,
    limit: int = Query(500, ge=1, le=2000),
):
    """Consolidated signals from all sources."""
    conditions = [f"date(signal_date) >= date('now', '-{days} days')"]
    params = []

    if strength:
        levels = [s.strip().upper() for s in strength.split(",")]
        placeholders = ",".join("?" * len(levels))
        conditions.append(f"signal_strength IN ({placeholders})")
        params.extend(levels)

    if symbol:
        conditions.append("symbol = ?")
        params.append(symbol.upper())

    where = " AND ".join(conditions)
    sql = f"""
        SELECT * FROM consolidated_signals
        WHERE {where}
        ORDER BY signal_date DESC
        LIMIT {limit}
    """
    return query(sql, tuple(params))


# ---------- Insider Trades ----------

@app.get("/api/insider-trades")
def get_insider_trades(
    days: int = Query(30, ge=1, le=365),
    type: Optional[str] = Query(None, description="Buy or Sell"),
    category: Optional[str] = Query(None, description="e.g. Promoters"),
    symbol: Optional[str] = None,
    limit: int = Query(500, ge=1, le=2000),
):
    conditions = [f"date(disclosure_date) >= date('now', '-{days} days')"]
    params = []

    if type:
        conditions.append("UPPER(transaction_type) = ?")
        params.append(type.upper())
    if category:
        conditions.append("person_category LIKE ?")
        params.append(f"%{category}%")
    if symbol:
        conditions.append("symbol = ?")
        params.append(symbol.upper())

    where = " AND ".join(conditions)
    sql = f"""
        SELECT * FROM insider_trades
        WHERE {where}
        ORDER BY disclosure_date DESC, value DESC
        LIMIT {limit}
    """
    return query(sql, tuple(params))


# ---------- SAST ----------

@app.get("/api/sast")
def get_sast(
    days: int = Query(30, ge=1, le=365),
    type: Optional[str] = Query(None, description="Acquisition or Disposal"),
    symbol: Optional[str] = None,
    limit: int = Query(500, ge=1, le=2000),
):
    conditions = [f"date(disclosure_date) >= date('now', '-{days} days')"]
    params = []

    if type:
        conditions.append("transaction_type LIKE ?")
        params.append(f"%{type}%")
    if symbol:
        conditions.append("symbol = ?")
        params.append(symbol.upper())

    where = " AND ".join(conditions)
    sql = f"""
        SELECT * FROM sast_disclosures
        WHERE {where}
        ORDER BY disclosure_date DESC
        LIMIT {limit}
    """
    return query(sql, tuple(params))


# ---------- Deals ----------

@app.get("/api/deals")
def get_deals(
    days: int = Query(7, ge=1, le=365),
    type: Optional[str] = Query(None, description="BULK, BLOCK, or both"),
    action: Optional[str] = Query(None, description="BUY or SELL"),
    symbol: Optional[str] = None,
    limit: int = Query(500, ge=1, le=2000),
):
    conditions = [f"date(deal_date) >= date('now', '-{days} days')"]
    params = []

    if type:
        types = [t.strip().upper() for t in type.split(",")]
        placeholders = ",".join("?" * len(types))
        conditions.append(f"deal_type IN ({placeholders})")
        params.extend(types)
    if action:
        conditions.append("UPPER(buy_sell) = ?")
        params.append(action.upper())
    if symbol:
        conditions.append("symbol = ?")
        params.append(symbol.upper())

    where = " AND ".join(conditions)
    sql = f"""
        SELECT * FROM bulk_block_deals
        WHERE {where}
        ORDER BY deal_date DESC, value DESC
        LIMIT {limit}
    """
    return query(sql, tuple(params))


# ---------- FII/DII ----------

@app.get("/api/fii-dii")
def get_fii_dii(days: int = Query(30, ge=1, le=365)):
    """FII/DII activity with 5-day rolling net calculation."""
    rows = query(
        f"""
        SELECT * FROM fii_dii_activity
        WHERE date(date) >= date('now', '-{days} days')
        ORDER BY date DESC, category
        """
    )

    by_cat: dict = defaultdict(list)
    for r in rows:
        by_cat[r["category"]].append(r)

    rolling: dict = {}
    for cat, records in by_cat.items():
        net_5d = sum(r["net_value_cr"] or 0 for r in records[:5])
        rolling[cat] = round(net_5d, 2)

    return {"data": rows, "rolling_5d_net": rolling}


# ---------- Shareholding ----------

@app.get("/api/shareholding/{symbol}")
def get_shareholding(symbol: str):
    return query(
        "SELECT * FROM shareholding_patterns WHERE symbol = ? ORDER BY quarter DESC",
        (symbol.upper(),),
    )


# ---------- Stock signals ----------

@app.get("/api/stock-signals/{symbol}")
def get_stock_signals(symbol: str, days: int = Query(90, ge=1, le=365)):
    sym = symbol.upper()
    return {
        "insider_trades": query(
            f"SELECT * FROM insider_trades WHERE symbol=? AND date(disclosure_date)>=date('now','-{days} days') ORDER BY disclosure_date DESC",
            (sym,),
        ),
        "sast": query(
            f"SELECT * FROM sast_disclosures WHERE symbol=? AND date(disclosure_date)>=date('now','-{days} days') ORDER BY disclosure_date DESC",
            (sym,),
        ),
        "deals": query(
            f"SELECT * FROM bulk_block_deals WHERE symbol=? AND date(deal_date)>=date('now','-{days} days') ORDER BY deal_date DESC",
            (sym,),
        ),
        "shareholding": query(
            "SELECT * FROM shareholding_patterns WHERE symbol=? ORDER BY quarter DESC",
            (sym,),
        ),
    }


# ---------- Dashboard summary ----------

@app.get("/api/dashboard-summary")
def get_dashboard_summary():
    conn = get_connection()
    try:
        def scalar(sql, params=()):
            row = conn.execute(sql, params).fetchone()
            return row[0] if row else 0

        today_insider_buys = scalar(
            "SELECT COUNT(*) FROM insider_trades WHERE UPPER(transaction_type)='BUY' AND date(disclosure_date)=date('now')"
        )
        today_insider_buy_value = scalar(
            "SELECT COALESCE(SUM(value),0) FROM insider_trades WHERE UPPER(transaction_type)='BUY' AND date(disclosure_date)=date('now')"
        )
        today_bulk_deals = scalar(
            "SELECT COUNT(*) FROM bulk_block_deals WHERE date(deal_date)=date('now')"
        )
        fii_row = conn.execute(
            "SELECT net_value_cr FROM fii_dii_activity WHERE category='FII/FPI' ORDER BY date DESC LIMIT 1"
        ).fetchone()
        dii_row = conn.execute(
            "SELECT net_value_cr FROM fii_dii_activity WHERE category='DII' ORDER BY date DESC LIMIT 1"
        ).fetchone()

        top_signals = [
            dict(r) for r in conn.execute(
                "SELECT * FROM consolidated_signals WHERE signal_strength IN ('HIGH','MEDIUM') LIMIT 10"
            ).fetchall()
        ]

        return {
            "today_insider_buys": today_insider_buys,
            "today_insider_buy_value": today_insider_buy_value,
            "today_bulk_deals": today_bulk_deals,
            "fii_net_latest": fii_row[0] if fii_row else None,
            "dii_net_latest": dii_row[0] if dii_row else None,
            "top_signals": top_signals,
        }
    finally:
        conn.close()


# ---------- Stock News + AI Analysis ----------

def _fetch_rss_articles(query_str: str) -> list:
    """Fetch and parse Google News RSS, return normalised article dicts."""
    url = f"https://news.google.com/rss/search?q={query_str}&hl=en-IN&gl=IN&ceid=IN:en"
    # feedparser with a browser User-Agent to avoid blocks
    feedparser.USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    d = feedparser.parse(url)
    articles = []
    cutoff = datetime.utcnow() - timedelta(days=180)

    for entry in d.entries:
        # Parse date
        pub = entry.get("published_parsed") or entry.get("updated_parsed")
        if pub:
            try:
                dt = datetime(*pub[:6])
            except Exception:
                dt = datetime.utcnow()
        else:
            dt = datetime.utcnow()

        if dt < cutoff:
            continue

        # Source name from <source> tag or domain
        source = ""
        if hasattr(entry, "source") and entry.source:
            source = entry.source.get("title", "")
        if not source:
            link = entry.get("link", "")
            try:
                from urllib.parse import urlparse
                source = urlparse(link).netloc.replace("www.", "")
            except Exception:
                source = "Unknown"

        articles.append({
            "title": entry.get("title", ""),
            "link": entry.get("link", ""),
            "source": source,
            "date": dt.strftime("%Y-%m-%d"),
            "datetime": dt,
        })

    # Sort newest first
    articles.sort(key=lambda x: x["datetime"], reverse=True)
    # Drop datetime (not JSON-serialisable)
    for a in articles:
        del a["datetime"]
    return articles


def _build_related_signals(sym: str) -> list:
    """Pull insider trades + deals for a symbol and format as signal list."""
    signals = []

    trades = query(
        "SELECT * FROM insider_trades WHERE symbol=? AND date(disclosure_date)>=date('now','-180 days') ORDER BY disclosure_date DESC LIMIT 20",
        (sym,),
    )
    for t in trades:
        action = t.get("transaction_type", "")
        detail = (
            f"{t.get('person_category','Insider')} {t.get('insider_name','')} "
            f"{action.lower()}  {int(t.get('quantity') or 0):,} shares"
        )
        signals.append({
            "type": "insider_trade",
            "detail": detail.strip(),
            "date": t.get("disclosure_date", ""),
            "value": t.get("value"),
        })

    deals = query(
        "SELECT * FROM bulk_block_deals WHERE symbol=? AND date(deal_date)>=date('now','-180 days') ORDER BY deal_date DESC LIMIT 20",
        (sym,),
    )
    for d in deals:
        detail = (
            f"{d.get('deal_type','')} deal — {d.get('client_name','')} "
            f"{d.get('buy_sell','').lower()}  {int(d.get('quantity') or 0):,} shares"
        )
        signals.append({
            "type": "bulk_block_deal",
            "detail": detail.strip(),
            "date": d.get("deal_date", ""),
            "value": d.get("value"),
        })

    sast = query(
        "SELECT * FROM sast_disclosures WHERE symbol=? AND date(disclosure_date)>=date('now','-180 days') ORDER BY disclosure_date DESC LIMIT 10",
        (sym,),
    )
    for s in sast:
        detail = (
            f"SAST Reg29 — {s.get('acquirer_name','')} "
            f"{s.get('transaction_type','').lower()}: "
            f"{s.get('holding_before_pct',0):.1f}% → {s.get('holding_after_pct',0):.1f}%"
        )
        signals.append({
            "type": "sast_disclosure",
            "detail": detail.strip(),
            "date": s.get("disclosure_date", ""),
            "value": None,
        })

    return signals


def _group_by_month(articles: list) -> dict:
    """Group articles into {month_label: [article, ...]} ordered newest first."""
    grouped: dict = {}
    for a in articles:
        try:
            dt = datetime.strptime(a["date"], "%Y-%m-%d")
            label = dt.strftime("%b %Y")
        except Exception:
            label = "Unknown"
        grouped.setdefault(label, []).append(a)
    return grouped


@app.get("/api/stock-news/{symbol}")
def get_stock_news(symbol: str):
    sym = symbol.upper()
    ai_available = bool(GROQ_API_KEY)

    # Get company name from DB for a richer search query
    company_rows = query(
        "SELECT company_name FROM insider_trades WHERE symbol=? AND company_name!='' LIMIT 1",
        (sym,),
    )
    company_name = company_rows[0]["company_name"] if company_rows else ""

    # Build RSS query — try symbol first, add company name if available
    rss_query = f"{sym}+stock+NSE+india"
    articles = _fetch_rss_articles(rss_query)

    # If sparse, retry with company name
    if len(articles) < 5 and company_name:
        company_query = "+".join(company_name.split()[:3]) + "+NSE+india"
        extra = _fetch_rss_articles(company_query)
        # Merge deduped by title
        seen = {a["title"] for a in articles}
        for a in extra:
            if a["title"] not in seen:
                articles.append(a)
                seen.add(a["title"])
        articles.sort(key=lambda x: x["date"], reverse=True)

    related_signals = _build_related_signals(sym)
    monthly_articles = _group_by_month(articles)

    response: dict = {
        "symbol": sym,
        "company_name": company_name,
        "total_articles": len(articles),
        "ai_available": ai_available,
        "monthly_articles": monthly_articles,
        "related_signals": related_signals,
    }

    if not ai_available:
        return response

    # Run AI analysis
    try:
        from services.ai_analysis import analyze_stock_news
        ai_result = analyze_stock_news(sym, articles, related_signals)
        response["ai_analysis"] = ai_result
    except Exception as e:
        logger.error("AI analysis failed for %s: %s", sym, e)
        response["ai_available"] = False
        response["ai_error"] = str(e)

    return response


# ---------- Signal Clusters ----------

@app.get("/api/clusters")
def get_clusters(
    tier: Optional[str] = Query(None, description="Comma-separated: ELITE,HIGH,MEDIUM"),
    days: int = Query(30, ge=1, le=365),
):
    """Return signal clusters, optionally filtered by tier. LEFT JOINs stock_fundamentals for quality context."""
    conditions = [f"date(sc.last_signal_date) >= date('now', '-{days} days')"]
    params: list = []

    if tier:
        tiers = [t.strip().upper() for t in tier.split(",")]
        placeholders = ",".join("?" * len(tiers))
        conditions.append(f"sc.cluster_tier IN ({placeholders})")
        params.extend(tiers)

    where = " AND ".join(conditions)
    sql = f"""
        SELECT
            sc.*,
            sf.quality_score, sf.quality_tier, sf.red_flags,
            sf.roce_5yr_avg, sf.debt_to_equity, sf.interest_coverage,
            sf.fcf_conversion, sf.pe_current, sf.pe_vs_median,
            sf.promoter_holding_pct, sf.promoter_pledge_pct,
            sf.sector, sf.market_cap_cr
        FROM signal_clusters sc
        LEFT JOIN stock_fundamentals sf ON sc.symbol = sf.symbol
        WHERE {where}
        ORDER BY sc.cluster_score DESC
        LIMIT 500
    """
    return query(sql, tuple(params))


# ---------- Promoter Streaks ----------

@app.get("/api/promoter-streaks")
def get_promoter_streaks(
    min_insiders: int = Query(2, ge=2),
    days: int = Query(90, ge=1, le=365),
):
    """Return promoter streaks filtered by minimum distinct insiders and date range. LEFT JOINs fundamentals."""
    sql = f"""
        SELECT
            ps.*,
            sf.quality_score, sf.quality_tier,
            sf.roce_5yr_avg, sf.debt_to_equity,
            sf.sector, sf.market_cap_cr
        FROM promoter_streaks ps
        LEFT JOIN stock_fundamentals sf ON ps.symbol = sf.symbol
        WHERE ps.distinct_insiders >= ?
          AND date(ps.window_end_date) >= date('now', '-{days} days')
        ORDER BY ps.distinct_insiders DESC, ps.total_value DESC
        LIMIT 500
    """
    return query(sql, (min_insiders,))


# ---------- Fundamentals ----------

@app.get("/api/fundamentals/{symbol}")
def get_fundamentals(symbol: str):
    """Return full stock_fundamentals row for a symbol, or 404."""
    rows = query(
        "SELECT * FROM stock_fundamentals WHERE symbol = ?",
        (symbol.upper(),),
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"No fundamentals found for {symbol.upper()}")
    return rows[0]


# ---------- Stock Intelligence ----------

@app.get("/api/stock-intelligence")
def get_stock_intelligence(
    days: int = Query(30, ge=1, le=365),
    quality_tier: Optional[str] = Query(None, description="Comma-separated: EXCELLENT,GOOD,AVERAGE,POOR,AVOID"),
    min_cluster_score: float = Query(0.0, ge=0),
):
    """
    One row per signalled stock combining cluster scores, promoter streaks, and fundamentals.
    Sorted by cluster_score DESC, quality_score DESC.
    """
    conditions = [f"date(sc.last_signal_date) >= date('now', '-{days} days')"]
    conditions.append(f"sc.cluster_score >= {min_cluster_score}")
    params: list = []

    if quality_tier:
        tiers = [t.strip().upper() for t in quality_tier.split(",")]
        placeholders = ",".join("?" * len(tiers))
        conditions.append(f"(sf.quality_tier IN ({placeholders}) OR sf.quality_tier IS NULL)")
        params.extend(tiers)

    where = " AND ".join(conditions)

    sql = f"""
        SELECT
            sc.symbol,
            COALESCE(sc.company_name, sf.company_name) AS company_name,
            sf.sector,
            sc.last_signal_date AS latest_signal_date,
            sc.sources_hit AS latest_signal_type,
            sc.cluster_score,
            sc.cluster_tier,
            sc.sources_hit,
            sc.source_count,
            ps.streak_strength AS promoter_streak_strength,
            ps.distinct_insiders,
            sf.quality_score,
            sf.quality_tier,
            sf.red_flags,
            sf.roce_5yr_avg,
            sf.debt_to_equity,
            sf.interest_coverage,
            sf.fcf_conversion,
            sf.pe_current,
            sf.pe_vs_median,
            sf.promoter_holding_pct,
            sf.promoter_pledge_pct,
            sf.market_cap_cr
        FROM signal_clusters sc
        LEFT JOIN stock_fundamentals sf ON sc.symbol = sf.symbol
        LEFT JOIN (
            SELECT symbol, streak_strength, distinct_insiders,
                   ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY computed_at DESC) AS rn
            FROM promoter_streaks
        ) ps ON sc.symbol = ps.symbol AND ps.rn = 1
        WHERE {where}
        ORDER BY sc.cluster_score DESC, sf.quality_score DESC
        LIMIT 500
    """
    return query(sql, tuple(params))


# ---------- Run Analysis (SSE streaming endpoint) ----------

def _run_step_sync(step_id: str) -> int:
    """Execute one pipeline step synchronously. Returns record count."""
    count = 0
    if step_id == "fii_dii":
        from scrapers.fii_dii import run as run_fii
        count = run_fii() or 0
    elif step_id == "bulk_block":
        from scrapers.bulk_block_deals import run as run_bulk
        count = run_bulk() or 0
    elif step_id == "insider":
        from scrapers.insider_trading import run as run_insider
        count = run_insider() or 0
    elif step_id == "sast":
        from scrapers.sast_regulation29 import run as run_sast
        count = run_sast() or 0
    elif step_id == "mf_portfolios":
        from scrapers.mf_portfolios import run as run_mf
        count = run_mf() or 0
    elif step_id == "streaks":
        from smart_money.cluster_detector import refresh_streak_table
        count = refresh_streak_table()
    elif step_id == "clusters":
        from smart_money.cluster_detector import refresh_cluster_table
        count = refresh_cluster_table()
    elif step_id == "fundamentals":
        from scrapers.screener_fundamentals import refresh_fundamentals, get_symbols_needing_fundamentals
        syms = get_symbols_needing_fundamentals()
        count = refresh_fundamentals(symbols=syms)
    return count


@app.post("/api/run-analysis")
async def run_analysis():
    """
    Run full analysis pipeline: scrape → streaks → clusters → fundamentals.
    Streams progress via Server-Sent Events so the browser sees each step live.

    Each scraper is blocking (network + DB), so we offload to a thread executor
    to keep the async event loop free to flush the stream between steps.
    """
    async def generate():
        steps = [
            ("fii_dii",       "FII/DII Activity"),
            ("bulk_block",    "Bulk/Block Deals"),
            ("insider",       "Insider Trading"),
            ("sast",          "SAST Regulation 29"),
            ("mf_portfolios", "MF Portfolios/Shareholding"),
            ("streaks",       "Promoter Streak Detection"),
            ("clusters",      "Signal Cluster Computation"),
            ("fundamentals",  "Fundamentals Enrichment"),
        ]

        loop = asyncio.get_event_loop()
        overall_start = time_module.time()

        for step_id, step_name in steps:
            step_start = time_module.time()
            # Send "running" event and yield control so uvicorn flushes it immediately
            yield f"data: {json.dumps({'step': step_id, 'name': step_name, 'status': 'running', 'elapsed_ms': 0})}\n\n"
            await asyncio.sleep(0)   # let the event loop flush the buffer

            try:
                # Run blocking scraper in a thread pool so the event loop stays free
                count = await loop.run_in_executor(None, _run_step_sync, step_id)
                elapsed = int((time_module.time() - step_start) * 1000)
                yield f"data: {json.dumps({'step': step_id, 'name': step_name, 'status': 'done', 'count': count, 'elapsed_ms': elapsed})}\n\n"
                await asyncio.sleep(0)
            except Exception as e:
                elapsed = int((time_module.time() - step_start) * 1000)
                yield f"data: {json.dumps({'step': step_id, 'name': step_name, 'status': 'error', 'error': str(e), 'elapsed_ms': elapsed})}\n\n"
                await asyncio.sleep(0)

        total = int((time_module.time() - overall_start) * 1000)
        yield f"data: {json.dumps({'step': 'complete', 'name': 'All Done', 'status': 'done', 'total_elapsed_ms': total})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------- Static dashboard ----------

@app.get("/")
def serve_dashboard():
    dashboard_path = os.path.join(os.path.dirname(__file__), "dashboard", "index.html")
    return FileResponse(dashboard_path)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)
