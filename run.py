"""CLI entry point — run individual scrapers or all of them."""

import argparse
import logging
import sys


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    # Suppress urllib3 SSL warning
    logging.getLogger("urllib3.connectionpool").setLevel(logging.WARNING)


SCRAPERS = {
    "bulk_block": "scrapers.bulk_block_deals",
    "fii_dii": "scrapers.fii_dii",
    "insider": "scrapers.insider_trading",
    "sast": "scrapers.sast_regulation29",
    "mf": "scrapers.mf_portfolios",
}


def run_scraper(name: str, backfill_days: int = 0):
    """Dynamically import and run a scraper by short name."""
    module_path = SCRAPERS[name]
    import importlib
    mod = importlib.import_module(module_path)

    if name == "bulk_block":
        return mod.run(backfill_days=backfill_days)
    elif name == "fii_dii":
        return mod.run()
    elif name == "insider":
        return mod.run(backfill_days=backfill_days if backfill_days else 30)
    elif name == "sast":
        return mod.run(backfill_days=backfill_days if backfill_days else 30)
    elif name == "mf":
        return mod.run()
    return 0


def run_full_pipeline(backfill_days: int = 0):
    """
    Full pipeline: scrape all sources → streaks → clusters → fundamentals.
    Optionally truncates all tables first if --fresh is passed.
    """
    import importlib
    logger = logging.getLogger("run")

    # Step 1: Scrapers
    total = 0
    for name in SCRAPERS.keys():
        logger.info("=== [1/4] Scraper: %s ===", name)
        try:
            n = run_scraper(name, backfill_days=backfill_days)
            logger.info("  → %s: %d new records", name, n or 0)
            total += n or 0
        except Exception as e:
            logger.error("  → %s FAILED: %s", name, e)

    logger.info("=== Scrapers done. %d total records ===", total)

    # Step 2: Promoter streaks
    logger.info("=== [2/4] Promoter streak detection ===")
    try:
        from smart_money.cluster_detector import refresh_streak_table
        n = refresh_streak_table()
        logger.info("  → %d streaks written", n)
    except Exception as e:
        logger.error("  → Streaks FAILED: %s", e)

    # Step 3: Signal clusters
    logger.info("=== [3/4] Signal cluster computation ===")
    try:
        from smart_money.cluster_detector import refresh_cluster_table
        n = refresh_cluster_table()
        logger.info("  → %d clusters written", n)
    except Exception as e:
        logger.error("  → Clusters FAILED: %s", e)

    # Step 4: Fundamentals
    logger.info("=== [4/4] Fundamentals enrichment (Screener.in) ===")
    try:
        from scrapers.screener_fundamentals import refresh_fundamentals, get_symbols_needing_fundamentals
        syms = get_symbols_needing_fundamentals()
        logger.info("  → %d symbols need fundamentals", len(syms))
        n = refresh_fundamentals(symbols=syms)
        logger.info("  → %d symbols updated", n)
    except Exception as e:
        logger.error("  → Fundamentals FAILED: %s", e)

    logger.info("=== Full pipeline complete ===")


def main():
    parser = argparse.ArgumentParser(
        description="Smart Money Tracker — Indian Market Signals",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run.py full                         # Full pipeline (scrape + streaks + clusters + fundamentals)
  python run.py full --fresh                 # Truncate all tables first, then run full pipeline
  python run.py all                          # Run scrapers only
  python run.py insider fii_dii             # Run specific scrapers
  python run.py all --backfill-days 90      # Backfill 90 days
        """,
    )
    parser.add_argument(
        "scrapers",
        nargs="+",
        choices=list(SCRAPERS.keys()) + ["all", "full"],
        help="Which scraper(s) to run, 'all' for all scrapers, 'full' for complete pipeline",
    )
    parser.add_argument(
        "--backfill-days",
        type=int,
        default=0,
        help="Fetch N days of historical data (default: scraper default)",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Truncate all tables before running (use with 'full')",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)
    logger = logging.getLogger("run")

    if "full" in args.scrapers:
        if args.fresh:
            logger.info("=== Truncating all tables ===")
            from db import db_conn
            tables = [
                "insider_trades", "sast_disclosures", "bulk_block_deals",
                "fii_dii_activity", "shareholding_patterns", "signal_clusters",
                "promoter_streaks", "stock_fundamentals",
            ]
            with db_conn() as conn:
                for t in tables:
                    conn.execute(f"DELETE FROM {t}")
                    logger.info("  Cleared %s", t)
        run_full_pipeline(backfill_days=args.backfill_days)
        return

    targets = list(SCRAPERS.keys()) if "all" in args.scrapers else args.scrapers

    total = 0
    for name in targets:
        logger.info("=== Running scraper: %s ===", name)
        try:
            n = run_scraper(name, backfill_days=args.backfill_days)
            logger.info("  → %s: %d new records", name, n or 0)
            total += n or 0
        except Exception as e:
            logger.error("  → %s FAILED: %s", name, e)

    logger.info("=== Done. Total new records: %d ===", total)


if __name__ == "__main__":
    main()
