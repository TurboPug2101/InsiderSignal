"""
Multi-source signal clustering and promoter streak detection.

This module scans the signal tables (insider_trades, sast_disclosures,
bulk_block_deals, shareholding_patterns) and computes:

1. Cluster scores — a weighted score for each stock that reflects how many
   distinct signal sources have fired within a rolling window.
2. Promoter streaks — how many distinct insiders at a company have been
   buying stock (genuine market purchases) within a 90-day window.

Both results are upserted into signal_clusters and promoter_streaks tables.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from config import (
    CLUSTER_WEIGHTS,
    CLUSTER_MIN_SCORE,
    CLUSTER_MEDIUM_THRESHOLD,
    CLUSTER_HIGH_THRESHOLD,
    CLUSTER_ELITE_THRESHOLD,
    STREAK_WINDOW_DAYS,
    STREAK_MIN_INSIDERS,
    STREAK_ELITE_VALUE_THRESHOLD,
    VALID_BUY_MODES,
)
from db import query, db_conn, init_db

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _window_cutoff(window_days: int) -> str:
    """Return ISO date string for (today - window_days)."""
    cutoff = datetime.today() - timedelta(days=window_days)
    return cutoff.strftime("%Y-%m-%d")


def _today_str() -> str:
    return datetime.today().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Feature 2: Promoter Streak Detection
# ---------------------------------------------------------------------------

def detect_promoter_streaks(window_days: int = 90) -> List[Dict]:
    """
    Scan insider_trades for all symbols with multiple genuine buy-side insiders.

    Counts distinct insiders (by insider_name) with:
      - transaction_type = 'Buy'
      - person_category IN ('Promoters','Promoter Group','Director','Key Managerial Personnel')
      - mode_of_acquisition IN VALID_BUY_MODES OR mode_of_acquisition IS NULL/empty
        (older data may not have this field populated)

    Strength tiers:
      - 2 insiders → WEAK
      - 3 insiders → MODERATE
      - 4 insiders → STRONG
      - 5+ insiders OR (3+ AND total_value > STREAK_ELITE_VALUE_THRESHOLD) → ELITE

    Returns a list of dicts for symbols with distinct_insiders >= STREAK_MIN_INSIDERS (2).
    """
    cutoff = _window_cutoff(window_days)
    today = _today_str()

    valid_modes_placeholders = ",".join("?" * len(VALID_BUY_MODES))
    valid_modes_list = list(VALID_BUY_MODES)

    rows = query(
        f"""
        SELECT
            symbol,
            MAX(company_name) AS company_name,
            COUNT(DISTINCT insider_name) AS distinct_insiders,
            GROUP_CONCAT(DISTINCT insider_name) AS insider_names,
            SUM(COALESCE(value, 0)) AS total_value,
            MIN(trade_from_date) AS window_start_date,
            MAX(trade_from_date) AS window_end_date
        FROM insider_trades
        WHERE
            UPPER(transaction_type) = 'BUY'
            AND person_category IN (
                'Promoters', 'Promoter Group', 'Director', 'Key Managerial Personnel'
            )
            AND (
                mode_of_acquisition IN ({valid_modes_placeholders})
                OR mode_of_acquisition IS NULL
                OR TRIM(mode_of_acquisition) = ''
            )
            AND date(COALESCE(disclosure_date, trade_from_date)) >= date(?)
        GROUP BY symbol
        HAVING COUNT(DISTINCT insider_name) >= ?
        ORDER BY distinct_insiders DESC
        """,
        tuple(valid_modes_list) + (cutoff, STREAK_MIN_INSIDERS),
    )

    results: List[Dict] = []
    for r in rows:
        n = r["distinct_insiders"]
        val = r["total_value"] or 0.0

        if n >= 5 or (n >= 3 and val >= STREAK_ELITE_VALUE_THRESHOLD):
            strength = "ELITE"
        elif n >= 4:
            strength = "STRONG"
        elif n >= 3:
            strength = "MODERATE"
        else:
            strength = "WEAK"

        results.append({
            "symbol": r["symbol"],
            "company_name": r["company_name"],
            "distinct_insiders": n,
            "insider_names": r["insider_names"],
            "total_value": val,
            "window_start_date": r["window_start_date"] or cutoff,
            "window_end_date": r["window_end_date"] or today,
            "streak_strength": strength,
        })

    logger.info("Detected %d promoter streaks in last %d days", len(results), window_days)
    return results


def refresh_streak_table(window_days: int = 90) -> int:
    """
    Upsert detected promoter streaks into the promoter_streaks table.

    Returns the count of rows written.
    """
    init_db()
    streaks = detect_promoter_streaks(window_days=window_days)
    if not streaks:
        logger.info("No streaks to upsert.")
        return 0

    with db_conn() as conn:
        count = 0
        for s in streaks:
            conn.execute(
                """
                INSERT OR REPLACE INTO promoter_streaks
                    (symbol, company_name, distinct_insiders, insider_names,
                     total_value, window_start_date, window_end_date, streak_strength)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    s["symbol"],
                    s["company_name"],
                    s["distinct_insiders"],
                    s["insider_names"],
                    s["total_value"],
                    s["window_start_date"],
                    s["window_end_date"],
                    s["streak_strength"],
                ),
            )
            count += 1

    logger.info("Upserted %d streaks.", count)
    return count


# ---------------------------------------------------------------------------
# Feature 1: Multi-Source Signal Clustering
# ---------------------------------------------------------------------------

def get_symbols_with_recent_signals(window_days: int = 30) -> List[str]:
    """
    Return distinct symbols that appear in any of the three main signal tables
    (insider_trades, sast_disclosures, bulk_block_deals) within the last
    window_days days.
    """
    cutoff = _window_cutoff(window_days)

    rows = query(
        f"""
        SELECT DISTINCT symbol FROM (
            SELECT symbol FROM insider_trades
            WHERE date(COALESCE(disclosure_date, trade_from_date)) >= date(?)
            UNION
            SELECT symbol FROM sast_disclosures
            WHERE date(disclosure_date) >= date(?)
            UNION
            SELECT symbol FROM bulk_block_deals
            WHERE date(deal_date) >= date(?)
        )
        ORDER BY symbol
        """,
        (cutoff, cutoff, cutoff),
    )
    return [r["symbol"] for r in rows]


def _has_streak_multiplier(symbol: str) -> bool:
    """
    Return True if the symbol has a streak with strength MODERATE, STRONG, or ELITE
    in the promoter_streaks table.
    """
    rows = query(
        """
        SELECT streak_strength FROM promoter_streaks
        WHERE symbol = ?
        ORDER BY computed_at DESC
        LIMIT 1
        """,
        (symbol,),
    )
    if not rows:
        return False
    return rows[0]["streak_strength"] in ("MODERATE", "STRONG", "ELITE")


def compute_cluster_score(symbol: str, window_days: int = 30) -> Optional[Dict]:
    """
    Compute a weighted cluster score for a single symbol over the last window_days.

    Scoring weights per occurrence:
      - Insider buy by Promoter/Promoter Group: 30
      - Insider buy by Director/KMP: 15
      - SAST acquisition (holding increased): 25
      - Block deal BUY: 20
      - Bulk deal BUY: 15
      - MF accumulation (QoQ mf_pct increase >= 1%): 20 (applied once)

    Multipliers on total base score:
      - 3+ distinct sources: x1.3
      - Symbol has MODERATE+ promoter streak: x1.25

    Score capped at 100.

    Returns a dict with all cluster fields, or None if score < CLUSTER_MIN_SCORE.
    """
    cutoff = _window_cutoff(window_days)

    # --- Insider buys ---
    insider_rows = query(
        """
        SELECT
            person_category,
            COUNT(*) AS cnt,
            SUM(COALESCE(value, 0)) AS total_val,
            MAX(company_name) AS company_name,
            MIN(COALESCE(disclosure_date, trade_from_date)) AS first_date,
            MAX(COALESCE(disclosure_date, trade_from_date)) AS last_date
        FROM insider_trades
        WHERE symbol = ?
          AND UPPER(transaction_type) = 'BUY'
          AND date(COALESCE(disclosure_date, trade_from_date)) >= date(?)
        GROUP BY person_category
        """,
        (symbol, cutoff),
    )

    insider_buy_count = 0
    insider_score = 0.0
    insider_value = 0.0
    insider_first: Optional[str] = None
    insider_last: Optional[str] = None
    company_name = ""

    for row in insider_rows:
        cat = (row["person_category"] or "").strip()
        cnt = row["cnt"] or 0
        insider_buy_count += cnt
        insider_value += row["total_val"] or 0.0
        if row["company_name"]:
            company_name = row["company_name"]
        if row["first_date"]:
            if insider_first is None or row["first_date"] < insider_first:
                insider_first = row["first_date"]
        if row["last_date"]:
            if insider_last is None or row["last_date"] > insider_last:
                insider_last = row["last_date"]

        if cat in ("Promoters", "Promoter Group"):
            insider_score += CLUSTER_WEIGHTS["INSIDER_PROMOTER"] * cnt
        else:
            insider_score += CLUSTER_WEIGHTS["INSIDER_KMP"] * cnt

    # --- SAST acquisitions ---
    sast_rows = query(
        """
        SELECT COUNT(*) AS cnt,
               MAX(company_name) AS company_name,
               MIN(disclosure_date) AS first_date,
               MAX(disclosure_date) AS last_date
        FROM sast_disclosures
        WHERE symbol = ?
          AND UPPER(transaction_type) IN ('ACQUISITION', 'ACQUIRING')
          AND (holding_after_pct IS NULL OR holding_after_pct >= COALESCE(holding_before_pct, 0))
          AND date(disclosure_date) >= date(?)
        """,
        (symbol, cutoff),
    )

    sast_count = 0
    sast_score = 0.0
    sast_first: Optional[str] = None
    sast_last: Optional[str] = None

    if sast_rows:
        r = sast_rows[0]
        sast_count = r["cnt"] or 0
        sast_score = CLUSTER_WEIGHTS["SAST_ACQUISITION"] * sast_count
        if r["company_name"] and not company_name:
            company_name = r["company_name"]
        sast_first = r["first_date"]
        sast_last = r["last_date"]

    # --- Bulk/Block deals BUY ---
    deal_rows = query(
        """
        SELECT
            deal_type,
            COUNT(*) AS cnt,
            SUM(COALESCE(value, 0)) AS total_val,
            MAX(company_name) AS company_name,
            MIN(deal_date) AS first_date,
            MAX(deal_date) AS last_date
        FROM bulk_block_deals
        WHERE symbol = ?
          AND UPPER(buy_sell) = 'BUY'
          AND date(deal_date) >= date(?)
        GROUP BY deal_type
        """,
        (symbol, cutoff),
    )

    bulk_block_count = 0
    deal_score = 0.0
    deal_value = 0.0
    deal_first: Optional[str] = None
    deal_last: Optional[str] = None

    for row in deal_rows:
        cnt = row["cnt"] or 0
        bulk_block_count += cnt
        deal_value += row["total_val"] or 0.0
        if row["company_name"] and not company_name:
            company_name = row["company_name"]
        if row["first_date"]:
            if deal_first is None or row["first_date"] < deal_first:
                deal_first = row["first_date"]
        if row["last_date"]:
            if deal_last is None or row["last_date"] > deal_last:
                deal_last = row["last_date"]

        dt = (row["deal_type"] or "").upper()
        if dt == "BLOCK":
            deal_score += CLUSTER_WEIGHTS["BLOCK_DEAL_BUY"] * cnt
        else:
            deal_score += CLUSTER_WEIGHTS["BULK_DEAL_BUY"] * cnt

    # --- MF accumulation (shareholding_patterns QoQ delta) ---
    mf_accumulation = 0
    mf_score = 0.0
    sp_rows = query(
        """
        SELECT mf_pct, quarter FROM shareholding_patterns
        WHERE symbol = ?
        ORDER BY quarter DESC
        LIMIT 2
        """,
        (symbol,),
    )
    if len(sp_rows) == 2:
        latest_mf = sp_rows[0]["mf_pct"]
        prev_mf = sp_rows[1]["mf_pct"]
        if latest_mf is not None and prev_mf is not None:
            if (latest_mf - prev_mf) >= 1.0:
                mf_accumulation = 1
                mf_score = CLUSTER_WEIGHTS["MF_ACCUMULATION"]

    # --- Base score ---
    base_score = insider_score + sast_score + deal_score + mf_score

    if base_score == 0:
        return None

    # --- Distinct sources ---
    sources_hit: List[str] = []
    if insider_buy_count > 0:
        sources_hit.append("INSIDER_BUY")
    if sast_count > 0:
        sources_hit.append("SAST")
    if bulk_block_count > 0:
        sources_hit.append("BLOCK_DEAL" if any(
            (r.get("deal_type") or "").upper() == "BLOCK" for r in deal_rows
        ) else "BULK_DEAL")
    if mf_accumulation:
        sources_hit.append("MF_ACCUMULATION")

    distinct_source_count = len(sources_hit)

    # --- Multipliers ---
    multiplier = 1.0
    if distinct_source_count >= 3:
        multiplier *= 1.3
    if _has_streak_multiplier(symbol):
        multiplier *= 1.25

    final_score = min(100.0, round(base_score * multiplier, 2))

    if final_score < CLUSTER_MIN_SCORE:
        return None

    # --- Tier ---
    if final_score >= CLUSTER_ELITE_THRESHOLD:
        tier = "ELITE"
    elif final_score >= CLUSTER_HIGH_THRESHOLD:
        tier = "HIGH"
    else:
        tier = "MEDIUM"

    # --- Aggregate dates ---
    all_firsts = [d for d in [insider_first, sast_first, deal_first] if d]
    all_lasts  = [d for d in [insider_last, sast_last, deal_last] if d]
    first_signal_date = min(all_firsts) if all_firsts else None
    last_signal_date  = max(all_lasts)  if all_lasts  else _today_str()

    total_value = insider_value + deal_value

    return {
        "symbol": symbol,
        "company_name": company_name or None,
        "cluster_score": final_score,
        "cluster_tier": tier,
        "source_count": distinct_source_count,
        "sources_hit": ",".join(sources_hit),
        "insider_buy_count": insider_buy_count,
        "sast_count": sast_count,
        "bulk_block_count": bulk_block_count,
        "mf_accumulation": mf_accumulation,
        "total_transaction_value": total_value if total_value else None,
        "first_signal_date": first_signal_date,
        "last_signal_date": last_signal_date,
        "window_days": window_days,
    }


def refresh_cluster_table(window_days: int = 30) -> int:
    """
    Recompute clusters for all recently-signalled symbols and upsert
    results into signal_clusters.

    Returns the number of clusters stored (score >= CLUSTER_MIN_SCORE).
    """
    init_db()
    symbols = get_symbols_with_recent_signals(window_days=window_days)
    logger.info("Computing clusters for %d symbols (window=%d days)", len(symbols), window_days)

    stored = 0
    with db_conn() as conn:
        for sym in symbols:
            try:
                result = compute_cluster_score(sym, window_days=window_days)
                if result is None:
                    continue
                conn.execute(
                    """
                    INSERT OR REPLACE INTO signal_clusters
                        (symbol, company_name, cluster_score, cluster_tier,
                         source_count, sources_hit, insider_buy_count,
                         sast_count, bulk_block_count, mf_accumulation,
                         total_transaction_value, first_signal_date,
                         last_signal_date, window_days)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        result["symbol"],
                        result["company_name"],
                        result["cluster_score"],
                        result["cluster_tier"],
                        result["source_count"],
                        result["sources_hit"],
                        result["insider_buy_count"],
                        result["sast_count"],
                        result["bulk_block_count"],
                        result["mf_accumulation"],
                        result["total_transaction_value"],
                        result["first_signal_date"],
                        result["last_signal_date"],
                        result["window_days"],
                    ),
                )
                stored += 1
                logger.debug("Cluster %s: score=%.1f tier=%s", sym, result["cluster_score"], result["cluster_tier"])
            except Exception as e:
                logger.error("Error computing cluster for %s: %s", sym, e)

    logger.info("Stored %d clusters.", stored)
    return stored


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Compute signal clusters and promoter streaks")
    parser.add_argument("--window-days", type=int, default=30)
    args = parser.parse_args()

    print("=== Promoter Streaks ===")
    n_streaks = refresh_streak_table(window_days=STREAK_WINDOW_DAYS)
    print(f"Saved {n_streaks} streaks")

    print("\n=== Signal Clusters ===")
    n_clusters = refresh_cluster_table(window_days=args.window_days)
    print(f"Saved {n_clusters} clusters")
