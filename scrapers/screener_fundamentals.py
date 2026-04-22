"""
Screener.in fundamentals scraper.

Fetches company fundamentals from https://www.screener.in/company/{SYMBOL}/consolidated/
(falls back to standalone page if consolidated is not available).

Parses:
  - Top ratios bar (market cap, PE, ROCE, ROE, current price)
  - P&L table (sales, net profit, operating profit, interest, depreciation)
  - Balance sheet (borrowings, equity, reserves)
  - Cash flow (operating CF, investing CF → FCF)
  - Shareholding (promoter %, pledge %)

Computes quality score and stores results in stock_fundamentals table.

Rate limit: 1.5 seconds between requests (SCREENER_RATE_LIMIT_SECONDS).
"""

import json
import logging
import math
import re
import time
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

from config import (
    SCREENER_COMPANY_URL,
    SCREENER_FALLBACK_URL,
    SCREENER_RATE_LIMIT_SECONDS,
    SCREENER_TIMEOUT_SECONDS,
    FUNDAMENTALS_STALE_DAYS,
    FUNDAMENTALS_LOOKBACK_DAYS,
)
from db import db_conn, init_db, query

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_last_request_time = 0.0


def _rate_limited_get(url: str) -> Optional[requests.Response]:
    """GET with rate limiting and browser headers. Returns None on failure."""
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < SCREENER_RATE_LIMIT_SECONDS:
        time.sleep(SCREENER_RATE_LIMIT_SECONDS - elapsed)

    try:
        session = requests.Session()
        session.headers.update(_HEADERS)
        resp = session.get(url, timeout=SCREENER_TIMEOUT_SECONDS, allow_redirects=True)
        _last_request_time = time.time()
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp
    except requests.RequestException as e:
        logger.error("HTTP error fetching %s: %s", url, e)
        _last_request_time = time.time()
        return None


def _safe_float(val: Optional[str]) -> Optional[float]:
    """Parse a string like '1,234.56' or '12.3%' to float, or None."""
    if val is None:
        return None
    v = str(val).strip().replace(",", "").replace("%", "").replace("₹", "").strip()
    if v in ("", "-", "—", "N/A", "n.a.", "NA"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _parse_top_ratios(soup: BeautifulSoup) -> Dict:
    """
    Extract metrics from the top ratios bar.
    Returns dict with keys: market_cap_cr, current_price, pe_current, roce_current, roe_current.

    Market Cap uses a nested <span class="number"> inside the .value span,
    so we pull from that child element to avoid the 'Cr.' suffix corrupting the parse.
    All other ratios use the plain .value text.
    """
    result: Dict = {}
    try:
        items = soup.select("#top-ratios li")
        for li in items:
            name_el = li.select_one(".name")
            val_el = li.select_one(".value")
            if not name_el or not val_el:
                continue
            name = name_el.get_text(strip=True).lower()

            if "market cap" in name:
                # Value is split: ₹ <span class="number">9,44,502</span> Cr.
                # Pull just the number span to avoid the "Cr." suffix
                num_el = val_el.select_one(".number")
                val_raw = num_el.get_text(strip=True) if num_el else val_el.get_text(strip=True)
                result["market_cap_cr"] = _safe_float(val_raw)
            elif "current price" in name:
                num_el = val_el.select_one(".number")
                val_raw = num_el.get_text(strip=True) if num_el else val_el.get_text(strip=True)
                result["current_price"] = _safe_float(val_raw)
            elif "stock p/e" in name or ("p/e" in name and "book" not in name):
                result["pe_current"] = _safe_float(val_el.get_text(strip=True))
            elif "roce" in name:
                result["roce_current"] = _safe_float(val_el.get_text(strip=True))
            elif "roe" in name:
                result["roe_current"] = _safe_float(val_el.get_text(strip=True))
    except Exception as e:
        logger.warning("Error parsing top ratios: %s", e)
    return result


def _parse_annual_table(soup: BeautifulSoup, section_id: str) -> Tuple[List[str], Dict[str, List]]:
    """
    Parse an annual data table from a section (e.g. #profit-loss, #balance-sheet, #cash-flow).

    Returns (headers, data_dict) where:
      - headers is a list of year strings e.g. ['Mar 2025', 'Mar 2024', ...]
      - data_dict maps row_label (lowercase, stripped) → list of values aligned with headers
    """
    headers: List[str] = []
    data: Dict[str, List] = {}
    try:
        section = soup.select_one(section_id)
        if not section:
            return headers, data
        table = section.select_one("table")
        if not table:
            return headers, data

        rows = table.select("tr")
        if not rows:
            return headers, data

        # Header row
        header_row = rows[0]
        ths = header_row.select("th")
        if not ths:
            tds = header_row.select("td")
            ths = tds

        for th in ths[1:]:
            txt = th.get_text(strip=True)
            headers.append(txt)

        # Data rows
        for row in rows[1:]:
            cols = row.select("td")
            if not cols:
                continue
            label = cols[0].get_text(strip=True).lower().strip()
            if not label:
                continue
            vals = []
            for td in cols[1:len(headers) + 1]:
                vals.append(_safe_float(td.get_text(strip=True)))
            # Pad with None if short
            while len(vals) < len(headers):
                vals.append(None)
            data[label] = vals
    except Exception as e:
        logger.warning("Error parsing table %s: %s", section_id, e)
    return headers, data


def _filter_annual_headers(headers: List[str]) -> List[int]:
    """
    Return indices of annual "Mar YYYY" columns, excluding TTM/Sep/Quarterly.
    Prefer the 5 most recent annual columns.
    """
    indices = []
    for i, h in enumerate(headers):
        if re.match(r"^Mar\s+\d{4}$", h.strip(), re.IGNORECASE):
            indices.append(i)
    # Most recent first
    return list(reversed(indices))[:5]


def _get_col_values(data: Dict[str, List], label_keywords: List[str], col_indices: List[int]) -> List[Optional[float]]:
    """
    Find a row by matching any label_keyword (case-insensitive substring),
    and return values at the given column indices.
    """
    for key, vals in data.items():
        if any(kw.lower() in key for kw in label_keywords):
            return [vals[i] if i < len(vals) else None for i in col_indices]
    return [None] * len(col_indices)


def _cagr(start: Optional[float], end: Optional[float], years: int) -> Optional[float]:
    """Compute CAGR between start and end over 'years' periods. Returns % or None."""
    if start is None or end is None or years <= 0:
        return None
    if start <= 0:
        return None
    try:
        return round(((end / start) ** (1.0 / years) - 1) * 100, 2)
    except Exception:
        return None


def _safe_div(a: Optional[float], b: Optional[float]) -> Optional[float]:
    """Safe division, returns None on zero/None."""
    if a is None or b is None or b == 0:
        return None
    return round(a / b, 4)


def _stddev(values: List[Optional[float]]) -> Optional[float]:
    """Population std dev of a list, ignoring Nones. Returns None if < 2 valid values."""
    valid = [v for v in values if v is not None]
    if len(valid) < 2:
        return None
    mean = sum(valid) / len(valid)
    variance = sum((x - mean) ** 2 for x in valid) / len(valid)
    return round(math.sqrt(variance), 2)


def _parse_shareholding_section(soup: BeautifulSoup) -> Dict:
    """
    Parse the #shareholding section for promoter holding % and pledge %.
    Returns dict with promoter_holding_pct and promoter_pledge_pct (both Optional[float]).
    """
    result: Dict = {"promoter_holding_pct": None, "promoter_pledge_pct": None}
    try:
        section = soup.select_one("#shareholding")
        if not section:
            return result
        table = section.select_one("table")
        if not table:
            return result

        rows = table.select("tr")
        # Header row has quarter dates — we want the most recent (first data column)
        for row in rows:
            tds = row.select("td")
            if not tds:
                tds = row.select("th")
            if not tds:
                continue
            label = tds[0].get_text(strip=True).lower()

            # Get the first value column (most recent quarter)
            val = _safe_float(tds[1].get_text(strip=True)) if len(tds) > 1 else None

            if "promoter" in label and "pledge" not in label and "promoter" == label.split("&")[0].strip():
                if result["promoter_holding_pct"] is None:
                    result["promoter_holding_pct"] = val
            elif "pledg" in label:
                if result["promoter_pledge_pct"] is None:
                    result["promoter_pledge_pct"] = val

        # Alternative: look for promoters row more broadly
        if result["promoter_holding_pct"] is None:
            for row in rows:
                tds = row.select("td")
                if not tds:
                    continue
                label = tds[0].get_text(strip=True).lower().strip()
                if label.startswith("promoter") and "pledge" not in label:
                    val = _safe_float(tds[1].get_text(strip=True)) if len(tds) > 1 else None
                    result["promoter_holding_pct"] = val
                    break

    except Exception as e:
        logger.warning("Error parsing shareholding: %s", e)
    return result


def _extract_sector_industry(soup: BeautifulSoup) -> Tuple[Optional[str], Optional[str]]:
    """
    Extract sector and industry from the Screener.in page.

    Screener places them in a <p class="sub"> immediately after the "Peer comparison" h2.
    The links have title="Broad Sector", title="Sector", and title="Industry" attributes.

    Fallback: look for any <a> whose href matches /market/INXX/ patterns.
    """
    sector = None
    industry = None
    try:
        # Primary: find the "Peer comparison" section's subtitle paragraph
        for h2 in soup.find_all("h2"):
            if "peer comparison" in h2.get_text(strip=True).lower():
                sub_p = h2.find_next_sibling("p")
                if sub_p:
                    for a in sub_p.find_all("a", title=True):
                        title_attr = a.get("title", "").lower()
                        text = a.get_text(strip=True)
                        if not text:
                            continue
                        if title_attr == "broad sector" and sector is None:
                            sector = text
                        elif title_attr == "sector" and sector is None:
                            # "Sector" is more specific; prefer "Broad Sector" if already set
                            sector = text
                        elif title_attr in ("industry", "sub-industry") and industry is None:
                            industry = text
                break

        # Fallback: scan all links with /market/ hrefs
        if not sector or not industry:
            for a in soup.find_all("a", href=re.compile(r"/market/IN\d+/")):
                title_attr = a.get("title", "").lower()
                text = a.get_text(strip=True)
                if not text:
                    continue
                if not sector and title_attr in ("broad sector", "sector"):
                    sector = text
                elif not industry and title_attr in ("industry", "sub-industry"):
                    industry = text
    except Exception as e:
        logger.debug("Could not extract sector/industry: %s", e)
    return sector, industry


def fetch_fundamentals(symbol: str) -> Optional[Dict]:
    """
    Fetch and parse all fundamental metrics for a single stock symbol
    from Screener.in.

    Tries consolidated page first; falls back to standalone page.
    On parse failure for any individual field, stores None and logs — never crashes.

    Returns a dict of fundamental fields, or None on hard network/parse failure.
    """
    url_consolidated = SCREENER_COMPANY_URL.format(symbol=symbol.upper())
    url_fallback = SCREENER_FALLBACK_URL.format(symbol=symbol.upper())

    resp = _rate_limited_get(url_consolidated)
    if resp is None:
        logger.info("Consolidated page not found for %s, trying standalone.", symbol)
        resp = _rate_limited_get(url_fallback)
    if resp is None:
        logger.warning("Could not fetch Screener.in page for %s", symbol)
        return None

    try:
        soup = BeautifulSoup(resp.text, "lxml")
    except Exception as e:
        logger.error("HTML parse error for %s: %s", symbol, e)
        return None

    # Check for 404 / company not found
    title = soup.title.string if soup.title else ""
    if "not found" in title.lower() or "404" in title:
        logger.warning("Symbol %s not found on Screener.in", symbol)
        return None

    f: Dict = {"symbol": symbol.upper()}

    # --- Company name ---
    try:
        name_el = soup.select_one("h1.h2, h1, .company-name")
        if name_el:
            f["company_name"] = name_el.get_text(strip=True)
    except Exception:
        f["company_name"] = None

    # --- Sector / Industry ---
    try:
        sector, industry = _extract_sector_industry(soup)
        f["sector"] = sector
        f["industry"] = industry
    except Exception as e:
        logger.debug("sector/industry extraction failed: %s", e)
        f["sector"] = None
        f["industry"] = None

    # --- Top ratios ---
    try:
        top = _parse_top_ratios(soup)
        f.update(top)
    except Exception as e:
        logger.warning("Top ratios parse failed for %s: %s", symbol, e)

    # Ensure required keys exist
    for k in ["market_cap_cr", "current_price", "pe_current", "roce_current", "roe_current"]:
        f.setdefault(k, None)

    # --- P&L Table ---
    pl_headers, pl_data = _parse_annual_table(soup, "#profit-loss")
    ann_idx = _filter_annual_headers(pl_headers)  # up to 5 most recent annual

    def pl_col(keywords: List[str]) -> List[Optional[float]]:
        return _get_col_values(pl_data, keywords, ann_idx)

    sales_vals = pl_col(["sales", "revenue"])
    profit_vals = pl_col(["net profit", "profit after tax", "pat"])
    op_profit_vals = pl_col(["operating profit", "ebitda", "ebita"])
    interest_vals = pl_col(["interest"])
    depreciation_vals = pl_col(["depreciation"])

    # Sales CAGR 5yr
    try:
        if len(ann_idx) >= 2:
            latest_sales = sales_vals[0]
            oldest_sales = sales_vals[len(ann_idx) - 1]
            yrs = len(ann_idx) - 1
            f["sales_cagr_5yr"] = _cagr(oldest_sales, latest_sales, yrs)
        else:
            f["sales_cagr_5yr"] = None
    except Exception as e:
        logger.debug("sales_cagr error for %s: %s", symbol, e)
        f["sales_cagr_5yr"] = None

    # Profit CAGR 5yr
    try:
        if len(ann_idx) >= 2:
            latest_profit = profit_vals[0]
            oldest_profit = profit_vals[len(ann_idx) - 1]
            yrs = len(ann_idx) - 1
            f["profit_cagr_5yr"] = _cagr(oldest_profit, latest_profit, yrs)
        else:
            f["profit_cagr_5yr"] = None
    except Exception as e:
        logger.debug("profit_cagr error for %s: %s", symbol, e)
        f["profit_cagr_5yr"] = None

    # Sales growth stddev (YoY % changes)
    try:
        yoy_growths = []
        for i in range(len(ann_idx) - 1):
            s_cur = sales_vals[i]
            s_prev = sales_vals[i + 1]
            if s_cur is not None and s_prev is not None and s_prev != 0:
                yoy_growths.append((s_cur - s_prev) / abs(s_prev) * 100)
        f["sales_growth_stddev"] = _stddev(yoy_growths) if yoy_growths else None
    except Exception as e:
        logger.debug("sales_growth_stddev error for %s: %s", symbol, e)
        f["sales_growth_stddev"] = None

    # Profit cumulative 5yr
    try:
        profit_sum = sum(v for v in profit_vals if v is not None)
        f["profit_5yr_cumulative"] = profit_sum if profit_sum != 0 else None
    except Exception:
        f["profit_5yr_cumulative"] = None

    # Interest coverage = Operating Profit / Interest (use latest annual year)
    try:
        op_latest = op_profit_vals[0] if op_profit_vals else None
        int_latest = interest_vals[0] if interest_vals else None
        f["interest_coverage"] = _safe_div(op_latest, int_latest)
    except Exception:
        f["interest_coverage"] = None

    # --- Balance Sheet ---
    bs_headers, bs_data = _parse_annual_table(soup, "#balance-sheet")
    bs_idx = _filter_annual_headers(bs_headers)

    def bs_col(keywords: List[str]) -> List[Optional[float]]:
        return _get_col_values(bs_data, keywords, bs_idx)

    borrowings_vals = bs_col(["borrowings", "total debt", "debt"])
    equity_vals = bs_col(["equity capital", "share capital", "equity"])
    reserves_vals = bs_col(["reserves", "reserve"])

    # Debt-to-equity = Borrowings / (Equity + Reserves)
    try:
        borr = borrowings_vals[0] if borrowings_vals else None
        eq = equity_vals[0] if equity_vals else None
        res = reserves_vals[0] if reserves_vals else None
        if borr is not None and eq is not None and res is not None:
            net_worth = (eq or 0) + (res or 0)
            f["debt_to_equity"] = _safe_div(borr, net_worth)
        elif borr is not None and eq is not None:
            f["debt_to_equity"] = _safe_div(borr, eq)
        else:
            f["debt_to_equity"] = None
    except Exception as e:
        logger.debug("D/E error for %s: %s", symbol, e)
        f["debt_to_equity"] = None

    # --- Cash Flow ---
    cf_headers, cf_data = _parse_annual_table(soup, "#cash-flow")
    cf_idx = _filter_annual_headers(cf_headers)

    def cf_col(keywords: List[str]) -> List[Optional[float]]:
        return _get_col_values(cf_data, keywords, cf_idx)

    op_cf_vals = cf_col(["cash from operating", "operating activity", "net cash from operations"])
    inv_cf_vals = cf_col(["cash from investing", "investing activity"])

    # FCF = Operating CF - abs(Investing CF) for each year; sum over 5 years
    try:
        fcf_total = 0.0
        fcf_counted = 0
        n_cf = min(len(cf_idx), 5)
        for i in range(n_cf):
            op = op_cf_vals[i] if i < len(op_cf_vals) else None
            inv = inv_cf_vals[i] if i < len(inv_cf_vals) else None
            if op is not None and inv is not None:
                # Capex is embedded in investing; FCF = operating - abs(capex)
                # investing CF is usually negative (outflow), so FCF = op + inv (adding a negative)
                fcf_total += op + inv
                fcf_counted += 1
            elif op is not None:
                fcf_total += op
                fcf_counted += 1
        f["fcf_5yr_cumulative"] = round(fcf_total, 2) if fcf_counted > 0 else None
    except Exception as e:
        logger.debug("FCF error for %s: %s", symbol, e)
        f["fcf_5yr_cumulative"] = None

    # FCF conversion = fcf_5yr / profit_5yr
    f["fcf_conversion"] = _safe_div(f.get("fcf_5yr_cumulative"), f.get("profit_5yr_cumulative"))

    # --- ROCE/ROE historical (from ratios section) ---
    try:
        r_headers, r_data = _parse_annual_table(soup, "#ratios")
        r_idx = _filter_annual_headers(r_headers)

        def r_col(keywords: List[str]) -> List[Optional[float]]:
            return _get_col_values(r_data, keywords, r_idx)

        roce_hist = r_col(["roce", "return on capital"])
        roe_hist = r_col(["roe", "return on equity"])

        # 3-yr and 5-yr averages from ratios section
        roce_valid = [v for v in roce_hist if v is not None]
        roe_valid = [v for v in roe_hist if v is not None]

        f["roce_3yr_avg"] = round(sum(roce_valid[:3]) / len(roce_valid[:3]), 2) if len(roce_valid) >= 3 else (
            round(sum(roce_valid) / len(roce_valid), 2) if roce_valid else None
        )
        f["roce_5yr_avg"] = round(sum(roce_valid[:5]) / len(roce_valid[:5]), 2) if roce_valid else None
        f["roe_5yr_avg"] = round(sum(roe_valid[:5]) / len(roe_valid[:5]), 2) if roe_valid else None

        # Use ratios section ROCE current if top-ratios didn't give it
        if f.get("roce_current") is None and roce_valid:
            f["roce_current"] = roce_valid[0]
        if f.get("roe_current") is None and roe_valid:
            f["roe_current"] = roe_valid[0]
    except Exception as e:
        logger.debug("ROCE/ROE history error for %s: %s", symbol, e)
        f.setdefault("roce_3yr_avg", None)
        f.setdefault("roce_5yr_avg", None)
        f.setdefault("roe_5yr_avg", None)

    # --- PE 5yr median (look for "Median PE" in ratios section or top-ratios) ---
    try:
        pe_median = None
        for li in soup.select("#top-ratios li"):
            name_el = li.select_one(".name")
            val_el = li.select_one(".value")
            if name_el and val_el:
                name_txt = name_el.get_text(strip=True).lower()
                if "median" in name_txt and "pe" in name_txt:
                    pe_median = _safe_float(val_el.get_text(strip=True))
                    break
        f["pe_5yr_median"] = pe_median
        pe_curr = f.get("pe_current")
        f["pe_vs_median"] = _safe_div(pe_curr, pe_median) if pe_curr and pe_median else None
    except Exception as e:
        logger.debug("PE median error for %s: %s", symbol, e)
        f.setdefault("pe_5yr_median", None)
        f.setdefault("pe_vs_median", None)

    # PEG ratio
    try:
        pc = f.get("profit_cagr_5yr")
        pe = f.get("pe_current")
        if pe is not None and pc is not None and pc > 0:
            f["peg_ratio"] = round(pe / pc, 3)
        else:
            f["peg_ratio"] = None
    except Exception:
        f["peg_ratio"] = None

    # --- Shareholding ---
    try:
        sh = _parse_shareholding_section(soup)
        f.update(sh)
    except Exception as e:
        logger.warning("Shareholding parse error for %s: %s", symbol, e)
        f.setdefault("promoter_holding_pct", None)
        f.setdefault("promoter_pledge_pct", None)

    # --- Quality score ---
    try:
        score, tier, red_flags = compute_quality_score(f)
        f["quality_score"] = score
        f["quality_tier"] = tier
        f["red_flags"] = json.dumps(red_flags)
    except Exception as e:
        logger.error("Quality score computation failed for %s: %s", symbol, e)
        f["quality_score"] = None
        f["quality_tier"] = "AVOID"
        f["red_flags"] = json.dumps([])

    f["source"] = "Screener.in"
    return f


def compute_quality_score(f: Dict) -> Tuple[float, str, List[str]]:
    """
    Compute a quality score (0-100), tier string, and list of red flags for a stock.

    Scoring breakdown:
      - ROCE 5yr avg (max 30): ≥20%=30, ≥15%=22, ≥10%=12, else 0 + flag
      - Debt-to-Equity (max 20): <0.3=20, <0.7=14, <1.2=7, else 0 + flag
      - Interest coverage: no score, but flag if < 2.5x
      - FCF conversion (max 20): >0.8=20, >0.5=12, >0.3=5, else 0 + flag
      - Growth consistency (max 15): sales_cagr ≥12% = 10; +5 if stddev < 10
      - Valuation PE vs median (max 15): <0.8=15, <1.1=10, <1.5=5, else 0 + flag

    Hard AVOID overrides (regardless of score):
      - D/E > 2.5
      - Pledge > 50%
      - Interest coverage < 1.5

    Pledge > 25% adds a flag but doesn't override unless > 50%.

    Missing (None) values contribute 0 to score — they do NOT crash the function.

    Returns: (score: float, tier: str, red_flags: List[str])
    """
    score = 0.0
    red_flags: List[str] = []

    # ROCE (max 30)
    roce = f.get("roce_5yr_avg")
    if roce is not None:
        if roce >= 20:
            score += 30
        elif roce >= 15:
            score += 22
        elif roce >= 10:
            score += 12
        else:
            red_flags.append(f"Low 5yr ROCE: {roce:.1f}%")

    # Debt-to-Equity (max 20)
    de = f.get("debt_to_equity")
    if de is not None:
        if de < 0.3:
            score += 20
        elif de < 0.7:
            score += 14
        elif de < 1.2:
            score += 7
        else:
            red_flags.append(f"High D/E: {de:.2f}")

    # Interest coverage (flag only, also a hard AVOID trigger below)
    ic = f.get("interest_coverage")
    if ic is not None and ic < 2.5:
        red_flags.append(f"Weak interest coverage: {ic:.1f}x")

    # FCF conversion (max 20)
    fcf_conv = f.get("fcf_conversion")
    if fcf_conv is not None:
        if fcf_conv > 0.8:
            score += 20
        elif fcf_conv > 0.5:
            score += 12
        elif fcf_conv > 0.3:
            score += 5
        else:
            red_flags.append(f"Poor FCF conversion: {fcf_conv:.1%}")

    # Growth consistency (max 15)
    sales_cagr = f.get("sales_cagr_5yr")
    stddev = f.get("sales_growth_stddev")
    if sales_cagr is not None and sales_cagr >= 12:
        score += 10
        if stddev is not None and stddev < 10:
            score += 5

    # Valuation (max 15)
    pe_ratio = f.get("pe_vs_median")
    if pe_ratio is not None:
        if pe_ratio < 0.8:
            score += 15
        elif pe_ratio < 1.1:
            score += 10
        elif pe_ratio < 1.5:
            score += 5
        else:
            red_flags.append(f"PE {pe_ratio:.1f}x its 5yr median")

    # Pledge flag
    pledge = f.get("promoter_pledge_pct")
    if pledge is not None and pledge > 25:
        red_flags.append(f"Promoter pledge {pledge:.1f}%")

    # Tier mapping
    if score >= 75:
        tier = "EXCELLENT"
    elif score >= 55:
        tier = "GOOD"
    elif score >= 35:
        tier = "AVERAGE"
    elif score >= 20:
        tier = "POOR"
    else:
        tier = "AVOID"

    # Hard overrides to AVOID
    if de is not None and de > 2.5:
        tier = "AVOID"
        if f"High D/E: {de:.2f}" not in red_flags:
            red_flags.append(f"High D/E: {de:.2f}")
    if pledge is not None and pledge > 50:
        tier = "AVOID"
    if ic is not None and ic < 1.5:
        tier = "AVOID"

    return round(score, 1), tier, red_flags


def get_symbols_needing_fundamentals() -> List[str]:
    """
    Return distinct symbols from the signal tables (insider_trades, sast_disclosures,
    bulk_block_deals) with activity in the last FUNDAMENTALS_LOOKBACK_DAYS days
    that either:
      (a) have no row in stock_fundamentals, OR
      (b) have fetched_at older than FUNDAMENTALS_STALE_DAYS days ago.
    """
    rows = query(
        f"""
        SELECT DISTINCT s.symbol
        FROM (
            SELECT symbol FROM insider_trades
            WHERE date(COALESCE(disclosure_date, trade_from_date)) >= date('now', '-{FUNDAMENTALS_LOOKBACK_DAYS} days')
            UNION
            SELECT symbol FROM sast_disclosures
            WHERE date(disclosure_date) >= date('now', '-{FUNDAMENTALS_LOOKBACK_DAYS} days')
            UNION
            SELECT symbol FROM bulk_block_deals
            WHERE date(deal_date) >= date('now', '-{FUNDAMENTALS_LOOKBACK_DAYS} days')
        ) s
        LEFT JOIN stock_fundamentals f ON s.symbol = f.symbol
        WHERE
            f.symbol IS NULL
            OR date(f.fetched_at) <= date('now', '-{FUNDAMENTALS_STALE_DAYS} days')
        ORDER BY s.symbol
        """
    )
    return [r["symbol"] for r in rows]


def refresh_fundamentals(symbols: Optional[List[str]] = None, force: bool = False) -> int:
    """
    Refresh fundamentals for the given symbols list.

    If symbols is None, automatically determines which symbols need refreshing
    using get_symbols_needing_fundamentals().

    Skips symbols that were refreshed within FUNDAMENTALS_STALE_DAYS unless
    force=True.

    Rate-limits requests to Screener.in at SCREENER_RATE_LIMIT_SECONDS.
    Returns the count of symbols successfully refreshed.
    """
    init_db()

    if symbols is None:
        symbols = get_symbols_needing_fundamentals()

    if not symbols:
        logger.info("No symbols need fundamentals refresh.")
        return 0

    if not force:
        # Filter out recently-refreshed symbols (unless force)
        fresh_rows = query(
            f"""
            SELECT symbol FROM stock_fundamentals
            WHERE date(fetched_at) > date('now', '-{FUNDAMENTALS_STALE_DAYS} days')
            """
        )
        fresh_set = {r["symbol"] for r in fresh_rows}
        symbols = [s for s in symbols if s not in fresh_set]

    if not symbols:
        logger.info("All symbols are fresh. Nothing to refresh.")
        return 0

    logger.info("Refreshing fundamentals for %d symbols.", len(symbols))
    refreshed = 0

    for sym in symbols:
        try:
            f = fetch_fundamentals(sym)
            if f is None:
                logger.warning("Could not fetch fundamentals for %s", sym)
                continue

            with db_conn() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO stock_fundamentals
                        (symbol, company_name, sector, industry,
                         market_cap_cr, current_price,
                         roce_current, roce_3yr_avg, roce_5yr_avg,
                         roe_current, roe_5yr_avg,
                         debt_to_equity, interest_coverage,
                         sales_cagr_5yr, profit_cagr_5yr, sales_growth_stddev,
                         fcf_5yr_cumulative, profit_5yr_cumulative, fcf_conversion,
                         pe_current, pe_5yr_median, pe_vs_median, peg_ratio,
                         promoter_holding_pct, promoter_pledge_pct,
                         quality_score, quality_tier, red_flags,
                         fetched_at, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
                    """,
                    (
                        f.get("symbol"),
                        f.get("company_name"),
                        f.get("sector"),
                        f.get("industry"),
                        f.get("market_cap_cr"),
                        f.get("current_price"),
                        f.get("roce_current"),
                        f.get("roce_3yr_avg"),
                        f.get("roce_5yr_avg"),
                        f.get("roe_current"),
                        f.get("roe_5yr_avg"),
                        f.get("debt_to_equity"),
                        f.get("interest_coverage"),
                        f.get("sales_cagr_5yr"),
                        f.get("profit_cagr_5yr"),
                        f.get("sales_growth_stddev"),
                        f.get("fcf_5yr_cumulative"),
                        f.get("profit_5yr_cumulative"),
                        f.get("fcf_conversion"),
                        f.get("pe_current"),
                        f.get("pe_5yr_median"),
                        f.get("pe_vs_median"),
                        f.get("peg_ratio"),
                        f.get("promoter_holding_pct"),
                        f.get("promoter_pledge_pct"),
                        f.get("quality_score"),
                        f.get("quality_tier"),
                        f.get("red_flags"),
                        f.get("source", "Screener.in"),
                    ),
                )
            refreshed += 1
            logger.info("Refreshed fundamentals for %s (tier=%s score=%s)", sym, f.get("quality_tier"), f.get("quality_score"))
        except Exception as e:
            logger.error("Failed refreshing fundamentals for %s: %s", sym, e)

    logger.info("Fundamentals refresh complete: %d/%d symbols updated.", refreshed, len(symbols))
    return refreshed


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Fetch Screener.in fundamentals")
    parser.add_argument("--symbol", type=str, help="Single symbol to fetch")
    parser.add_argument("--refresh-all", action="store_true", help="Refresh all signalled symbols")
    parser.add_argument("--force", action="store_true", help="Force refresh even if fresh")
    args = parser.parse_args()

    if args.symbol:
        import json as _json
        result = fetch_fundamentals(args.symbol.upper())
        if result:
            display = {k: v for k, v in result.items() if k not in ["red_flags"]}
            print(_json.dumps(display, indent=2, default=str))
            flags = _json.loads(result.get("red_flags") or "[]")
            if flags:
                print("\nRed flags:")
                for f in flags:
                    print(f"  - {f}")
        else:
            print(f"No data found for {args.symbol}")
    elif args.refresh_all:
        n = refresh_fundamentals(force=args.force)
        print(f"Refreshed {n} symbols")
    else:
        syms = get_symbols_needing_fundamentals()
        print(f"Symbols needing refresh: {len(syms)}")
        for s in syms[:20]:
            print(f"  {s}")
