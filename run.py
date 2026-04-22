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


def main():
    parser = argparse.ArgumentParser(
        description="Smart Money Tracker — Indian Market Signals",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run.py all                          # Run all scrapers
  python run.py insider fii_dii             # Run specific scrapers
  python run.py all --backfill-days 90      # Backfill 90 days
  python run.py bulk_block --backfill-days 7
        """,
    )
    parser.add_argument(
        "scrapers",
        nargs="+",
        choices=list(SCRAPERS.keys()) + ["all"],
        help="Which scraper(s) to run",
    )
    parser.add_argument(
        "--backfill-days",
        type=int,
        default=0,
        help="Fetch N days of historical data (default: scraper default)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)
    logger = logging.getLogger("run")

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
