"""Source 2: SAST Regulation 29 — big acquirer disclosures (5%+ stakes)."""

import logging
from datetime import datetime, timedelta
from config import (
    NSE_SAST_URL, REFERER_SAST,
    NSE_DATE_FORMAT, DEFAULT_BACKFILL_DAYS,
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


def _parse_record(rec: dict) -> dict:
    """
    Normalise one SAST regulation 29 record.

    Handles both the old API field names and the new API (corporate-sast-reg29) field names:
      New fields: company (not companyName), acqSaleType, totAcqShare, totAftShare,
                  acquirerDate (date range string), noOfShareAcq / noOfShareSale
    """
    # Transaction type: new API uses acqSaleType ("Acquisition" / "Sale")
    tx_raw = (
        rec.get("acqSaleType")
        or rec.get("acqType")
        or rec.get("transactionType")
        or ""
    ).strip()
    # Normalise "Sale" → "Disposal"
    if tx_raw.lower() == "sale":
        tx_type = "Disposal"
    elif tx_raw:
        tx_type = tx_raw
    else:
        tx_type = ""

    # Shares transacted — new API: noOfShareAcq for acquisitions, noOfShareSale for sales
    shares = _safe_int(
        rec.get("noOfShareAcq")
        or rec.get("noOfShareSale")
        or rec.get("sharesTransacted")
        or rec.get("secAcq")
        or 0
    )

    # Percentage transacted — new API uses totAcqShare
    pct = _safe_float(
        rec.get("totAcqShare")
        or rec.get("percOfSharesAcq")
        or rec.get("pctTransacted")
        or rec.get("percShares")
        or 0
    )

    # Holding after — new API uses totAftShare
    after = _safe_float(
        rec.get("totAftShare")
        or rec.get("aftAcqSharesPer")
        or rec.get("afterAcqPer")
        or 0
    )

    # Holding before — new API doesn't have a direct field; compute as after - pct_acquired
    before_raw = rec.get("befAcqSharesPer") or rec.get("beforeAcqPer")
    if before_raw is not None:
        before = _safe_float(before_raw)
    else:
        # Approximate: holding before ≈ holding after − pct transacted (for acquisitions)
        computed = after - pct
        before = max(0.0, round(computed, 4)) if tx_type != "Disposal" else after + pct

    # Infer tx_type from direction if still unknown
    if not tx_type:
        tx_type = "Acquisition" if after >= before else "Disposal"

    # Acquirer name: try several fields
    acquirer = (
        rec.get("acquirerName")
        or rec.get("acqName")
        or rec.get("shareholderName")
        or ""
    ).strip()

    # Symbol / company — new API uses "company" (not "companyName")
    symbol = (rec.get("symbol") or rec.get("scrip") or "").strip()
    company = (rec.get("company") or rec.get("companyName") or "").strip()

    # Date — new API may use acquirerDate (e.g. "17-APR-2026 to 17-APR-2026") or date/timestamp
    date_raw = (
        rec.get("date")
        or rec.get("timestamp")
        or rec.get("intimDt")
        or rec.get("acquirerDate")
        or ""
    ).strip()
    # acquirerDate format: "17-APR-2026 to 17-APR-2026" — take the last (end) date
    if " to " in date_raw:
        date_raw = date_raw.split(" to ")[-1].strip()
    # Strip time portion if present
    date = date_raw.split(" ")[0]

    return {
        "symbol": symbol,
        "company_name": company,
        "acquirer_name": acquirer,
        "shares_transacted": shares if shares else None,
        "pct_transacted": pct if pct else None,
        "holding_before_pct": before if before else None,
        "holding_after_pct": after if after else None,
        "transaction_type": tx_type,
        "disclosure_date": date,
        "source": "NSE",
    }


def fetch(from_date: str, to_date: str) -> list[dict]:
    """
    Fetch SAST Reg29 disclosures for a date range.
    from_date / to_date: DD-MM-YYYY
    """
    url = NSE_SAST_URL.format(from_date=from_date, to_date=to_date)
    logger.info("Fetching SAST Reg29 %s → %s", from_date, to_date)
    try:
        data = nse.get(url, referer=REFERER_SAST)
    except Exception as e:
        logger.error("Failed to fetch SAST data: %s", e)
        return []

    # New API returns {"acqNameList": [...], "data": [...]}
    # Old API returned {"data": [...]}  — handle both
    if isinstance(data, dict):
        records = data.get("data", [])
    elif isinstance(data, list):
        records = data
    else:
        records = []
    if not records:
        logger.info("No SAST records returned for this period.")
        return []

    rows = [_parse_record(r) for r in records]
    logger.info("Fetched %d SAST records", len(rows))
    return rows


def run(backfill_days: int = DEFAULT_BACKFILL_DAYS):
    """Main entry: fetch SAST disclosures for last N days and save to DB."""
    init_db()
    today = datetime.today()
    from_dt = today - timedelta(days=backfill_days)

    rows = fetch(
        from_date=from_dt.strftime(NSE_DATE_FORMAT),
        to_date=today.strftime(NSE_DATE_FORMAT),
    )

    if not rows:
        logger.warning("No SAST records to insert.")
        return 0

    with db_conn() as conn:
        inserted = insert_many("sast_disclosures", rows, conn)

    logger.info("Inserted %d new SAST records.", inserted)
    return inserted


if __name__ == "__main__":
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Fetch SAST Regulation 29 disclosures")
    parser.add_argument("--backfill-days", type=int, default=DEFAULT_BACKFILL_DAYS)
    args = parser.parse_args()

    today = datetime.today()
    from_dt = today - timedelta(days=args.backfill_days)
    records = fetch(
        from_date=from_dt.strftime(NSE_DATE_FORMAT),
        to_date=today.strftime(NSE_DATE_FORMAT),
    )

    acquisitions = [r for r in records if "acq" in r["transaction_type"].lower()]
    print(f"\n--- SAST Reg29: {len(records)} total, {len(acquisitions)} acquisitions ---")
    for r in records[:15]:
        before = r["holding_before_pct"] or 0.0
        after = r["holding_after_pct"] or 0.0
        direction = "↑" if after >= before else "↓"
        print(
            f"  {r['disclosure_date']:12} | {r['symbol']:12} | "
            f"{r['acquirer_name'][:35]:35} | "
            f"{direction} {before:.2f}% → {after:.2f}% | "
            f"{r['transaction_type']}"
        )
    if len(records) > 15:
        print(f"  ... and {len(records) - 15} more")

    n = run(backfill_days=args.backfill_days)
    print(f"\nSaved {n} new records to DB.")
