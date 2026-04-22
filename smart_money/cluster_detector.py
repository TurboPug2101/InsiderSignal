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
    Recompute clusters for all recently-signalled symbols and upsert results
    into signal_clusters.

    Bulk-fetches all signal data in a small number of queries (not per-symbol),
    computes scores in Python, then batch-inserts — minimises HTTP round-trips
    to Turso (or any remote DB).

    Returns the number of clusters stored (score >= CLUSTER_MIN_SCORE).
    """
    init_db()
    cutoff = _window_cutoff(window_days)
    today = _today_str()
    logger.info("Computing clusters (window=%d days, cutoff=%s)", window_days, cutoff)

    # --- Bulk fetch all signal data in 4 queries ---
    valid_modes_list = list(VALID_BUY_MODES)
    valid_modes_placeholders = ",".join("?" * len(valid_modes_list))

    insider_rows = query(
        f"""
        SELECT symbol, person_category, transaction_type,
               COALESCE(value, 0) AS value,
               COALESCE(disclosure_date, trade_from_date) AS sig_date,
               company_name
        FROM insider_trades
        WHERE UPPER(transaction_type) = 'BUY'
          AND (
              mode_of_acquisition IN ({valid_modes_placeholders})
              OR mode_of_acquisition IS NULL
              OR TRIM(mode_of_acquisition) = ''
          )
          AND date(COALESCE(disclosure_date, trade_from_date)) >= date(?)
        """,
        tuple(valid_modes_list) + (cutoff,),
    )

    sast_rows = query(
        """
        SELECT symbol, holding_before_pct, holding_after_pct,
               disclosure_date AS sig_date, company_name
        FROM sast_disclosures
        WHERE date(disclosure_date) >= date(?)
        """,
        (cutoff,),
    )

    deal_rows = query(
        """
        SELECT symbol, deal_type, COALESCE(value, 0) AS value,
               deal_date AS sig_date, company_name
        FROM bulk_block_deals
        WHERE UPPER(buy_sell) = 'BUY'
          AND date(deal_date) >= date(?)
        """,
        (cutoff,),
    )

    mf_rows = query(
        """
        SELECT symbol, mf_pct, quarter
        FROM shareholding_patterns
        ORDER BY symbol, quarter DESC
        """,
    )

    streak_rows = query(
        """
        SELECT symbol, streak_strength FROM promoter_streaks
        ORDER BY symbol, computed_at DESC
        """,
    )

    # --- Index streak multiplier by symbol (latest row wins) ---
    streak_map: Dict[str, str] = {}
    for r in streak_rows:
        sym = r["symbol"]
        if sym not in streak_map:
            streak_map[sym] = r["streak_strength"]

    # --- Index MF accumulation by symbol ---
    mf_by_sym: Dict[str, list] = {}
    for r in mf_rows:
        mf_by_sym.setdefault(r["symbol"], []).append(r)

    mf_accum_syms: set = set()
    for sym, records in mf_by_sym.items():
        records.sort(key=lambda x: x["quarter"], reverse=True)
        if len(records) >= 2:
            latest = records[0]["mf_pct"]
            prev = records[1]["mf_pct"]
            if latest is not None and prev is not None:
                if (float(latest) - float(prev)) >= 1.0:
                    mf_accum_syms.add(sym)

    # --- Aggregate per symbol ---
    from collections import defaultdict

    # company_name lookup (first non-null found)
    company_names: Dict[str, str] = {}
    for r in insider_rows + sast_rows + deal_rows:
        sym = r["symbol"]
        if sym not in company_names and r.get("company_name"):
            company_names[sym] = r["company_name"]

    # insider aggregation
    insider_by_sym: Dict[str, Dict] = defaultdict(lambda: {
        "buy_count": 0, "promoter_score": 0.0, "kmp_score": 0.0,
        "value": 0.0, "dates": [],
    })
    for r in insider_rows:
        sym = r["symbol"]
        d = insider_by_sym[sym]
        d["buy_count"] += 1
        d["value"] += float(r["value"] or 0)
        if r["sig_date"]:
            d["dates"].append(r["sig_date"])
        cat = (r["person_category"] or "").upper()
        if "PROMOTER" in cat:
            d["promoter_score"] += CLUSTER_WEIGHTS["INSIDER_PROMOTER"]
        else:
            d["kmp_score"] += CLUSTER_WEIGHTS["INSIDER_KMP"]

    # sast aggregation
    sast_by_sym: Dict[str, Dict] = defaultdict(lambda: {
        "count": 0, "score": 0.0, "dates": [],
    })
    for r in sast_rows:
        sym = r["symbol"]
        d = sast_by_sym[sym]
        before = float(r["holding_before_pct"] or 0)
        after = float(r["holding_after_pct"] or 0)
        if after > before:
            d["count"] += 1
            d["score"] += CLUSTER_WEIGHTS["SAST_ACQUISITION"]
            if r["sig_date"]:
                d["dates"].append(r["sig_date"])

    # deal aggregation
    deal_by_sym: Dict[str, Dict] = defaultdict(lambda: {
        "count": 0, "score": 0.0, "value": 0.0, "dates": [], "has_block": False,
    })
    for r in deal_rows:
        sym = r["symbol"]
        d = deal_by_sym[sym]
        d["count"] += 1
        d["value"] += float(r["value"] or 0)
        if r["sig_date"]:
            d["dates"].append(r["sig_date"])
        dt = (r["deal_type"] or "").upper()
        if dt == "BLOCK":
            d["score"] += CLUSTER_WEIGHTS["BLOCK_DEAL_BUY"]
            d["has_block"] = True
        else:
            d["score"] += CLUSTER_WEIGHTS["BULK_DEAL_BUY"]

    # --- Compute cluster score per symbol ---
    all_symbols = (
        set(insider_by_sym.keys())
        | set(sast_by_sym.keys())
        | set(deal_by_sym.keys())
        | mf_accum_syms
    )

    results = []
    for sym in sorted(all_symbols):
        ins = insider_by_sym[sym]
        sas = sast_by_sym[sym]
        dea = deal_by_sym[sym]

        mf_accumulation = 1 if sym in mf_accum_syms else 0
        mf_score = CLUSTER_WEIGHTS["MF_ACCUMULATION"] if mf_accumulation else 0.0

        base_score = ins["promoter_score"] + ins["kmp_score"] + sas["score"] + dea["score"] + mf_score
        if base_score == 0:
            continue

        sources_hit = []
        if ins["buy_count"] > 0:
            sources_hit.append("INSIDER_BUY")
        if sas["count"] > 0:
            sources_hit.append("SAST")
        if dea["count"] > 0:
            sources_hit.append("BLOCK_DEAL" if dea["has_block"] else "BULK_DEAL")
        if mf_accumulation:
            sources_hit.append("MF_ACCUM")

        distinct_source_count = len(sources_hit)
        multiplier = 1.0
        if distinct_source_count >= 3:
            multiplier *= 1.3
        if streak_map.get(sym) in ("MODERATE", "STRONG", "ELITE"):
            multiplier *= 1.25

        final_score = min(round(base_score * multiplier, 2), 100.0)

        if final_score < CLUSTER_MIN_SCORE:
            continue

        if final_score >= CLUSTER_ELITE_THRESHOLD:
            tier = "ELITE"
        elif final_score >= CLUSTER_HIGH_THRESHOLD:
            tier = "HIGH"
        else:
            tier = "MEDIUM"

        all_dates = ins["dates"] + sas["dates"] + dea["dates"]
        first_signal_date = min(all_dates) if all_dates else None
        last_signal_date = max(all_dates) if all_dates else today
        total_value = ins["value"] + dea["value"]

        results.append((
            sym,
            company_names.get(sym),
            final_score,
            tier,
            distinct_source_count,
            ",".join(sources_hit),
            ins["buy_count"],
            sas["count"],
            dea["count"],
            mf_accumulation,
            total_value if total_value else None,
            first_signal_date,
            last_signal_date,
            window_days,
        ))

    if not results:
        logger.info("No clusters above threshold.")
        return 0

    # --- Batch insert all results ---
    with db_conn() as conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO signal_clusters
                (symbol, company_name, cluster_score, cluster_tier,
                 source_count, sources_hit, insider_buy_count,
                 sast_count, bulk_block_count, mf_accumulation,
                 total_transaction_value, first_signal_date,
                 last_signal_date, window_days)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            results,
        )

    logger.info("Stored %d clusters.", len(results))
    return len(results)


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
