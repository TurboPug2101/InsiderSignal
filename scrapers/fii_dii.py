"""Source 4: FII/DII daily trading activity scraper."""

import logging
from config import NSE_FII_DII_URL, REFERER_FII_DII
from scrapers.nse_session import nse
from db import init_db, insert_many, db_conn, to_iso_date

logger = logging.getLogger(__name__)


def _clean_value(v) -> float:
    """Convert '10,233.17' or '-3231.54' or None to float."""
    if v is None:
        return 0.0
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return 0.0


def _normalise_category(cat: str) -> str:
    """Standardise category labels to 'FII/FPI' or 'DII'."""
    cat = cat.strip().upper()
    if "FII" in cat or "FPI" in cat:
        return "FII/FPI"
    if "DII" in cat:
        return "DII"
    return cat


def _parse_record(rec: dict) -> dict:
    """Normalise a single FII/DII record."""
    buy = _clean_value(rec.get("buyValue") or rec.get("buy_value"))
    sell = _clean_value(rec.get("sellValue") or rec.get("sell_value"))
    net = _clean_value(rec.get("netValue") or rec.get("net_value"))
    # If net is not provided or zero, compute it
    if net == 0.0 and (buy != 0.0 or sell != 0.0):
        net = round(buy - sell, 2)

    return {
        "date": to_iso_date(rec.get("date", "").strip()),
        "category": _normalise_category(rec.get("category", "")),
        "buy_value_cr": buy,
        "sell_value_cr": sell,
        "net_value_cr": net,
        "source": "NSE",
    }


def fetch() -> list[dict]:
    """Fetch FII/DII data from NSE (returns today + recent history)."""
    logger.info("Fetching FII/DII data...")
    try:
        data = nse.get(NSE_FII_DII_URL, referer=REFERER_FII_DII)
    except Exception as e:
        logger.error("Failed to fetch FII/DII data: %s", e)
        return []

    rows = []
    # Handle both array format and dict format
    if isinstance(data, list):
        for rec in data:
            parsed = _parse_record(rec)
            if parsed["date"] and parsed["category"]:
                rows.append(parsed)
    elif isinstance(data, dict):
        # Might be {fpiData: {...}, diiData: {...}, date: "..."} format
        date = data.get("date", "")
        for key, cat in [("fpiData", "FII/FPI"), ("diiData", "DII")]:
            if key in data:
                rec = dict(data[key])
                rec.setdefault("date", date)
                rec.setdefault("category", cat)
                parsed = _parse_record(rec)
                if parsed["date"]:
                    rows.append(parsed)

    logger.info("Fetched %d FII/DII records", len(rows))
    return rows


def run():
    """Main entry: fetch FII/DII data and save to DB."""
    init_db()
    rows = fetch()

    if not rows:
        logger.warning("No FII/DII records to insert.")
        return 0

    with db_conn() as conn:
        inserted = insert_many("fii_dii_activity", rows, conn)

    logger.info("Inserted %d new FII/DII records.", inserted)
    return inserted


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    records = fetch()
    print(f"\n--- FII/DII Activity ({len(records)} records) ---")
    for r in records:
        net_str = f"+{r['net_value_cr']:,.2f}" if r['net_value_cr'] >= 0 else f"{r['net_value_cr']:,.2f}"
        print(
            f"  {r['date']:12} | {r['category']:7} | "
            f"Buy: ₹{r['buy_value_cr']:>10,.2f} Cr | "
            f"Sell: ₹{r['sell_value_cr']:>10,.2f} Cr | "
            f"Net: ₹{net_str} Cr"
        )

    n = run()
    print(f"\nSaved {n} new records to DB.")
