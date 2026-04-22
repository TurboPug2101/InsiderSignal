"""Source 3: Bulk & Block deals scraper."""

import logging
from datetime import datetime, timedelta
from config import (
    NSE_BULK_BLOCK_URL, NSE_BULK_BLOCK_HISTORICAL_URL,
    REFERER_DEALS, NSE_DATE_FORMAT, DEFAULT_BACKFILL_DAYS,
)
from scrapers.nse_session import nse
from db import init_db, insert_many, db_conn, to_iso_date

logger = logging.getLogger(__name__)


def _parse_record(rec: dict, deal_type: str) -> dict:
    """Normalise a single bulk/block deal record.

    Actual NSE field names (verified live):
        date, symbol, name, clientName, buySell, qty, watp
    """
    try:
        qty = int(str(rec.get("qty", "0")).replace(",", "") or 0)
    except (ValueError, TypeError):
        qty = 0
    try:
        price = float(str(rec.get("watp", "0")).replace(",", "") or 0)
    except (ValueError, TypeError):
        price = 0.0

    return {
        "deal_date": to_iso_date(rec.get("date", "")),
        "symbol": rec.get("symbol", "").strip(),
        "company_name": rec.get("name", "").strip(),
        "client_name": rec.get("clientName", "").strip(),
        "buy_sell": rec.get("buySell", "").strip().upper(),
        "quantity": qty,
        "price": price,
        "value": round(qty * price, 2),
        "deal_type": deal_type,
        "source": "NSE",
    }


def fetch_today() -> list[dict]:
    """Fetch today's bulk and block deals from the snapshot endpoint."""
    logger.info("Fetching today's bulk/block deals...")
    try:
        data = nse.get(NSE_BULK_BLOCK_URL, referer=REFERER_DEALS)
    except Exception as e:
        logger.error("Failed to fetch bulk/block deals: %s", e)
        return []

    rows = []
    for rec in data.get("BLOCK_DEALS_DATA") or []:
        rows.append(_parse_record(rec, "BLOCK"))
    for rec in data.get("BULK_DEALS_DATA") or []:
        rows.append(_parse_record(rec, "BULK"))

    logger.info("Fetched %d deals (snapshot)", len(rows))
    return rows


def fetch_historical(from_date: str, to_date: str) -> list[dict]:
    """
    Fetch historical bulk deals via the historical endpoint.
    from_date / to_date: DD-MM-YYYY
    """
    url = NSE_BULK_BLOCK_HISTORICAL_URL.format(from_date=from_date, to_date=to_date)
    logger.info("Fetching historical bulk deals %s → %s", from_date, to_date)
    try:
        data = nse.get(url, referer=REFERER_DEALS)
    except Exception as e:
        logger.error("Failed to fetch historical bulk deals: %s", e)
        return []

    rows = []
    # Historical endpoint returns a flat list or dict with data key
    records = data if isinstance(data, list) else data.get("data", [])
    for rec in records:
        rows.append(_parse_record(rec, "BULK"))

    logger.info("Fetched %d historical bulk deals", len(rows))
    return rows


def run(backfill_days: int = 0):
    """Main entry: fetch deals and save to DB."""
    init_db()
    rows = fetch_today()

    if backfill_days > 0:
        today = datetime.today()
        from_dt = today - timedelta(days=backfill_days)
        rows += fetch_historical(
            from_dt.strftime(NSE_DATE_FORMAT),
            today.strftime(NSE_DATE_FORMAT),
        )

    if not rows:
        logger.warning("No bulk/block deal records to insert.")
        return 0

    with db_conn() as conn:
        inserted = insert_many("bulk_block_deals", rows, conn)

    logger.info("Inserted %d new bulk/block deal records.", inserted)
    return inserted


if __name__ == "__main__":
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Fetch bulk/block deals")
    parser.add_argument("--backfill-days", type=int, default=0,
                        help="Also fetch N days of historical data")
    args = parser.parse_args()

    records = fetch_today()
    print(f"\n--- Today's Deals ({len(records)}) ---")
    for r in records[:10]:
        print(
            f"  [{r['deal_type']}] {r['deal_date']} | {r['symbol']:12} | "
            f"{r['buy_sell']:4} | {r['client_name'][:40]:40} | "
            f"qty={r['quantity']:>12,} | ₹{r['value']:>15,.0f}"
        )
    if len(records) > 10:
        print(f"  ... and {len(records) - 10} more")

    n = run(backfill_days=args.backfill_days)
    print(f"\nSaved {n} new records to DB.")
