"""Source 5: Mutual fund / shareholding pattern scraper via NSE shareholding API."""

import logging
from config import (
    NSE_SHAREHOLDING_MASTER_URL, NSE_SHAREHOLDING_URL,
    REFERER_SHAREHOLDING, WATCHLIST_SYMBOLS,
)
from scrapers.nse_session import nse
from db import init_db, insert_many, db_conn

logger = logging.getLogger(__name__)


def _safe_float(v) -> float:
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return 0.0


def _safe_int(v) -> int:
    try:
        return int(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return 0


def _parse_shareholding(symbol: str, rec: dict) -> dict:
    """
    Extract key shareholding percentages from an NSE shareholding record.
    The actual field names vary — we probe common patterns.
    """
    def pick(*keys):
        for k in keys:
            v = rec.get(k)
            if v is not None:
                return _safe_float(v)
        return 0.0

    promoter = pick("promoterAndPromoterGroupShareholding", "promoter",
                    "totPromoterHolding", "promoterHolding", "promoter_pct")
    fii = pick("foreignInstitutionalInvestors", "fii", "fiiHolding",
               "foreignPortfolioInvestors", "fpi", "fii_pct")
    dii = pick("dii", "domesticInstitutionalInvestors", "diiHolding", "dii_pct")
    mf = pick("mutualFunds", "mf", "mfHolding", "mutualFundHolding", "mf_pct")
    public = pick("public", "publicShareholding", "publicHolding", "public_pct")

    quarter = (
        rec.get("quarter")
        or rec.get("quarterEnding")
        or rec.get("period")
        or "Unknown"
    )
    company = (rec.get("companyName") or rec.get("company") or "").strip()
    total = _safe_int(rec.get("totalShares") or rec.get("paidUpCapital") or 0)

    return {
        "symbol": symbol,
        "company_name": company,
        "quarter": str(quarter).strip(),
        "promoter_pct": promoter,
        "fii_pct": fii,
        "dii_pct": dii,
        "mf_pct": mf,
        "public_pct": public,
        "total_shares": total,
        "source": "NSE",
    }


def fetch_symbol(symbol: str) -> list[dict]:
    """Fetch shareholding pattern for a single symbol."""
    # First get available quarters
    master_url = NSE_SHAREHOLDING_MASTER_URL.format(symbol=symbol)
    try:
        master = nse.get(master_url, referer=REFERER_SHAREHOLDING)
    except Exception as e:
        logger.warning("Could not fetch shareholding master for %s: %s", symbol, e)
        return []

    # master may be a list of quarter strings or a dict with quarters key
    quarters = []
    if isinstance(master, list):
        quarters = master
    elif isinstance(master, dict):
        quarters = (
            master.get("quarters")
            or master.get("data")
            or master.get("quarterList")
            or []
        )

    if not quarters:
        logger.debug("No quarters available for %s", symbol)
        return []

    # Use master data directly — the detail endpoint is currently unavailable.
    # master records have: date ("31-DEC-2025"), pr_and_prgrp, public_val, name
    rows = []
    for first in quarters[:1]:  # only most recent quarter
        if not isinstance(first, dict):
            continue
        raw_date = first.get("date") or first.get("quarterEnding") or ""
        try:
            from datetime import datetime as _dt
            dt = _dt.strptime(raw_date, "%d-%b-%Y")
            quarter_str = dt.strftime("%B%Y")  # "December2025"
        except Exception:
            quarter_str = raw_date

        rows.append({
            "symbol": symbol,
            "company_name": first.get("name", "").strip(),
            "quarter": quarter_str,
            "promoter_pct": _safe_float(first.get("pr_and_prgrp", 0)),
            "fii_pct": 0.0,   # not available in master; detail API is down
            "dii_pct": 0.0,
            "mf_pct": 0.0,
            "public_pct": _safe_float(first.get("public_val", 0)),
            "total_shares": 0,
            "source": "NSE",
        })

    return rows


def run(symbols: list = None):
    """Fetch shareholding patterns for watchlist symbols and save to DB."""
    init_db()
    symbols = symbols or WATCHLIST_SYMBOLS
    total_inserted = 0

    for symbol in symbols:
        rows = fetch_symbol(symbol)
        if rows:
            with db_conn() as conn:
                inserted = insert_many("shareholding_patterns", rows, conn)
            total_inserted += inserted
            logger.info("%s: inserted %d record(s)", symbol, inserted)
        else:
            logger.debug("%s: no data", symbol)

    logger.info("Shareholding patterns: %d new records total.", total_inserted)
    return total_inserted


if __name__ == "__main__":
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Fetch MF/shareholding patterns")
    parser.add_argument("--symbols", nargs="+", default=None,
                        help="Symbols to fetch (default: watchlist)")
    parser.add_argument("--limit", type=int, default=5,
                        help="Max symbols to fetch in one run (default: 5)")
    args = parser.parse_args()

    symbols = (args.symbols or WATCHLIST_SYMBOLS)[: args.limit]
    print(f"Fetching shareholding for: {symbols}")

    results = []
    for sym in symbols:
        rows = fetch_symbol(sym)
        results.extend(rows)

    print(f"\n--- Shareholding Patterns ({len(results)} records) ---")
    for r in results:
        print(
            f"  {r['symbol']:12} | Q: {r['quarter']:15} | "
            f"Promoter: {r['promoter_pct']:5.1f}% | "
            f"FII: {r['fii_pct']:5.1f}% | "
            f"DII: {r['dii_pct']:5.1f}% | "
            f"MF: {r['mf_pct']:5.1f}% | "
            f"Public: {r['public_pct']:5.1f}%"
        )

    n = run(symbols=symbols)
    print(f"\nSaved {n} new records to DB.")
