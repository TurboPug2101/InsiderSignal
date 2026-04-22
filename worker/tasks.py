"""
Worker task implementations.
Each function runs one computation step and returns a result dict.
All business logic lives here; main.py only handles HTTP concerns.
"""

import time
import structlog

log = structlog.get_logger()


def run_streaks() -> dict:
    start = time.time()
    from smart_money.cluster_detector import refresh_streak_table
    count = refresh_streak_table()
    elapsed_ms = int((time.time() - start) * 1000)
    log.info("streaks_done", count=count, elapsed_ms=elapsed_ms)
    return {"step": "streaks", "name": "Promoter Streak Detection", "count": count, "elapsed_ms": elapsed_ms}


def run_clusters() -> dict:
    start = time.time()
    from smart_money.cluster_detector import refresh_cluster_table
    count = refresh_cluster_table()
    elapsed_ms = int((time.time() - start) * 1000)
    log.info("clusters_done", count=count, elapsed_ms=elapsed_ms)
    return {"step": "clusters", "name": "Signal Cluster Computation", "count": count, "elapsed_ms": elapsed_ms}


def run_fundamentals() -> dict:
    start = time.time()
    from scrapers.screener_fundamentals import refresh_fundamentals, get_symbols_needing_fundamentals
    syms = get_symbols_needing_fundamentals()
    log.info("fundamentals_starting", symbols=len(syms))
    count = refresh_fundamentals(symbols=syms)
    elapsed_ms = int((time.time() - start) * 1000)
    log.info("fundamentals_done", count=count, elapsed_ms=elapsed_ms)
    return {"step": "fundamentals", "name": "Fundamentals Enrichment", "count": count, "elapsed_ms": elapsed_ms}


def run_recompute() -> dict:
    """Run the full computation pipeline: streaks → clusters → fundamentals."""
    overall_start = time.time()
    log.info("recompute_started")

    results = []
    errors = []

    for fn, label in [
        (run_streaks,       "streaks"),
        (run_clusters,      "clusters"),
        (run_fundamentals,  "fundamentals"),
    ]:
        try:
            result = fn()
            results.append(result)
        except Exception as e:
            log.error("step_failed", step=label, error=str(e))
            errors.append({"step": label, "error": str(e)})

    total_elapsed_ms = int((time.time() - overall_start) * 1000)
    log.info("recompute_complete", total_elapsed_ms=total_elapsed_ms, errors=len(errors))

    return {
        "status": "done" if not errors else "partial",
        "steps": results,
        "errors": errors,
        "total_elapsed_ms": total_elapsed_ms,
    }
