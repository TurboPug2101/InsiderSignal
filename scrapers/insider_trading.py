"""Source 1: Insider/Promoter trading disclosures (PIT Regulation 7)."""

import logging
from datetime import datetime, timedelta
from config import (
    NSE_INSIDER_TRADING_URL, REFERER_INSIDER,
    NSE_DATE_FORMAT, DEFAULT_BACKFILL_DAYS,
)
from scrapers.nse_session import nse
from db import init_db, insert_many, db_conn, to_iso_date

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


def _parse_record(rec: dict) -> dict:
    """Normalise one insider trade record from NSE response.

    Actual NSE fields (verified live):
      symbol, company, acqName, personCategory, tdpTransactionType,
      secAcq (shares count), secVal (value), befAcqSharesPer,
      afterAcqSharesPer, acqfromDt, acqtoDt, date (disclosure),
      acqMode (mode of acquisition), buyValue, sellValue, buyQuantity, sellquantity
    """
    tx_type = (rec.get("tdpTransactionType") or "").strip()

    # Quantity: secAcq holds share count; fallback to buy/sell quantity
    if _safe_int(rec.get("secAcq", 0)) > 0:
        qty = _safe_int(rec.get("secAcq", 0))
    elif tx_type.upper() == "BUY":
        qty = _safe_int(rec.get("buyQuantity", 0))
    else:
        qty = _safe_int(rec.get("sellquantity", 0))

    # Value: secVal holds transaction value in ₹
    value = _safe_float(rec.get("secVal") or rec.get("buyValue") or rec.get("sellValue") or 0)

    # Disclosure date may include time — take just the date part
    raw_date = (rec.get("date") or rec.get("intimDt") or "").strip()
    disclosure_date = raw_date.split(" ")[0] if raw_date else ""

    return {
        "symbol": (rec.get("symbol") or "").strip(),
        "company_name": (rec.get("company") or "").strip(),
        "insider_name": (rec.get("acqName") or rec.get("tkdAcqm") or "").strip(),
        "person_category": (rec.get("personCategory") or "").strip(),
        "transaction_type": tx_type,
        "quantity": qty,
        "value": value,
        "holding_before_pct": _safe_float(rec.get("befAcqSharesPer", 0)),
        "holding_after_pct": _safe_float(rec.get("afterAcqSharesPer") or rec.get("aftAcqSharesPer") or 0),
        "trade_from_date": to_iso_date((rec.get("acqfromDt") or "").strip()),
        "trade_to_date": to_iso_date((rec.get("acqtoDt") or "").strip()),
        "disclosure_date": to_iso_date(disclosure_date),
        "mode_of_acquisition": (rec.get("acqMode") or "").strip(),
        "source": "NSE",
    }


def fetch(from_date: str, to_date: str) -> list[dict]:
    """
    Fetch insider trades for the given date range.
    from_date / to_date: DD-MM-YYYY
    """
    url = NSE_INSIDER_TRADING_URL.format(from_date=from_date, to_date=to_date)
    logger.info("Fetching insider trades %s → %s", from_date, to_date)
    try:
        data = nse.get(url, referer=REFERER_INSIDER)
    except Exception as e:
        logger.error("Failed to fetch insider trades: %s", e)
        return []

    records = data.get("data", []) if isinstance(data, dict) else data
    if not records:
        logger.info("No insider trade records returned for this period.")
        return []

    rows = [_parse_record(r) for r in records]
    logger.info("Fetched %d insider trade records", len(rows))
    return rows


def run(backfill_days: int = DEFAULT_BACKFILL_DAYS):
    """Main entry: fetch insider trades for last N days and save to DB."""
    init_db()
    today = datetime.today()
    from_dt = today - timedelta(days=backfill_days)

    rows = fetch(
        from_date=from_dt.strftime(NSE_DATE_FORMAT),
        to_date=today.strftime(NSE_DATE_FORMAT),
    )

    if not rows:
        logger.warning("No insider trade records to insert.")
        return 0

    with db_conn() as conn:
        inserted = insert_many("insider_trades", rows, conn)

    logger.info("Inserted %d new insider trade records.", inserted)
    return inserted


if __name__ == "__main__":
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Fetch insider trading disclosures")
    parser.add_argument("--backfill-days", type=int, default=DEFAULT_BACKFILL_DAYS)
    args = parser.parse_args()

    today = datetime.today()
    from_dt = today - timedelta(days=args.backfill_days)
    records = fetch(
        from_date=from_dt.strftime(NSE_DATE_FORMAT),
        to_date=today.strftime(NSE_DATE_FORMAT),
    )

    buys = [r for r in records if r["transaction_type"].upper() == "BUY"]
    print(f"\n--- Insider Trades: {len(records)} total, {len(buys)} BUYs ---")
    for r in buys[:15]:
        print(
            f"  {r['disclosure_date']:12} | {r['symbol']:12} | "
            f"{r['person_category']:30} | {r['insider_name'][:30]:30} | "
            f"{r['transaction_type']:4} | qty={r['quantity']:>10,} | "
            f"₹{r['value']:>15,.0f}"
        )
    if len(buys) > 15:
        print(f"  ... and {len(buys) - 15} more BUYs")

    n = run(backfill_days=args.backfill_days)
    print(f"\nSaved {n} new records to DB.")
