"""SQLite / Turso schema creation and insert/query helpers.

Locally: uses a plain SQLite file (sqlite3 stdlib).
On Render: uses Turso (hosted libSQL) when TURSO_DATABASE_URL + TURSO_AUTH_TOKEN are set.
The rest of the codebase sees the same interface either way.
"""

import os
import re
import sqlite3
import logging
from contextlib import contextmanager
from dateutil import parser as dateparser
from config import DB_PATH

# --- Backend selection ---
TURSO_URL   = os.environ.get("TURSO_DATABASE_URL", "")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "")
USE_TURSO   = bool(TURSO_URL and TURSO_TOKEN)


# ---- Turso HTTP API shim (no compiled packages needed) ----
# Turso exposes a plain HTTPS endpoint — we talk to it with requests.

class _TursoRow(dict):
    """Dict-based row so callers can use row['col'] just like sqlite3.Row."""


class _TursoCursor:
    def __init__(self, cols, rows, rowcount=-1):
        self._cols = cols
        self._rows = rows
        self.rowcount = rowcount
        self._idx = 0

    def fetchall(self):
        return [_TursoRow(zip(self._cols, r)) for r in self._rows]

    def fetchone(self):
        if self._idx < len(self._rows):
            row = _TursoRow(zip(self._cols, self._rows[self._idx]))
            self._idx += 1
            return row
        return None


class _TursoConn:
    """Talks to Turso via its HTTPS pipeline API — pure Python, no Rust."""

    def __init__(self, url: str, token: str):
        import requests as _req
        # Convert libsql:// → https://
        self._http_url = url.replace("libsql://", "https://") + "/v2/pipeline"
        self._token = token
        self._req = _req
        self._pending: list = []   # buffered statements for commit

    def _send(self, statements: list) -> list:
        """POST a pipeline of statements, return list of result objects."""
        payload = {"requests": [
            {"type": "execute", "stmt": {"sql": sql, "args": [
                {"type": "text", "value": str(a)} if isinstance(a, str)
                else {"type": "integer", "value": str(int(a))} if isinstance(a, int)
                else {"type": "float", "value": str(float(a))} if isinstance(a, float)
                else {"type": "null"} if a is None
                else {"type": "text", "value": str(a)}
                for a in args
            ]}}
            for sql, args in statements
        ] + [{"type": "close"}]}
        r = self._req.post(
            self._http_url,
            json=payload,
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json().get("results", [])

    def execute(self, sql: str, params=()):
        results = self._send([(sql, list(params))])
        res = results[0] if results else {}
        if res.get("type") == "error":
            raise Exception(res.get("error", {}).get("message", "Turso error"))
        inner = res.get("response", {}).get("result", {})
        cols = [c["name"] for c in inner.get("cols", [])]
        rows = [[v.get("value") for v in r] for r in inner.get("rows", [])]
        affected = inner.get("affected_row_count", -1)
        return _TursoCursor(cols, rows, rowcount=affected)

    def executemany(self, sql: str, data):
        data = list(data)
        if not data:
            return _TursoCursor([], [], rowcount=0)
        results = self._send([(sql, list(row)) for row in data])
        affected = sum(
            r.get("response", {}).get("result", {}).get("affected_row_count", 0)
            for r in results if r.get("type") != "error"
        )
        return _TursoCursor([], [], rowcount=affected)

    def executescript(self, script: str):
        clean = re.sub(r"--[^\n]*", "", script)
        stmts = [s.strip() for s in clean.split(";") if s.strip()]
        for stmt in stmts:
            try:
                self._send([(stmt, [])])
            except Exception as e:
                logger.warning("executescript warning: %s | %.80s", e, stmt)

    def commit(self):
        pass   # each _send is auto-committed by Turso

    def rollback(self):
        pass   # no transaction buffering in HTTP mode

    def close(self):
        pass


def to_iso_date(raw: str) -> str:
    """
    Convert any recognisable date string to ISO YYYY-MM-DD.
    Returns the original string if parsing fails (safer than crashing).
    """
    if not raw:
        return raw
    raw = str(raw).strip().split(" ")[0]  # drop time portion
    # Already ISO
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        return raw
    try:
        return dateparser.parse(raw, dayfirst=True).strftime("%Y-%m-%d")
    except Exception:
        return raw

logger = logging.getLogger(__name__)


def get_connection():
    if USE_TURSO:
        return _TursoConn(TURSO_URL, TURSO_TOKEN)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def db_conn():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create all tables and views if they don't exist."""
    with db_conn() as conn:
        conn.executescript("""
-- Source 1: Insider Trading
CREATE TABLE IF NOT EXISTS insider_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    company_name TEXT,
    insider_name TEXT NOT NULL,
    person_category TEXT,
    transaction_type TEXT NOT NULL,
    quantity INTEGER,
    value REAL,
    holding_before_pct REAL,
    holding_after_pct REAL,
    trade_from_date TEXT,
    trade_to_date TEXT,
    disclosure_date TEXT,
    mode_of_acquisition TEXT,
    source TEXT DEFAULT 'NSE',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(symbol, insider_name, trade_from_date, quantity)
);

-- Source 2: SAST Regulation 29
CREATE TABLE IF NOT EXISTS sast_disclosures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    company_name TEXT,
    acquirer_name TEXT NOT NULL,
    shares_transacted INTEGER,
    pct_transacted REAL,
    holding_before_pct REAL,
    holding_after_pct REAL,
    transaction_type TEXT,
    disclosure_date TEXT,
    source TEXT DEFAULT 'NSE',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(symbol, acquirer_name, disclosure_date, shares_transacted)
);

-- Source 3: Bulk and Block Deals
CREATE TABLE IF NOT EXISTS bulk_block_deals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    company_name TEXT,
    client_name TEXT NOT NULL,
    buy_sell TEXT NOT NULL,
    quantity INTEGER,
    price REAL,
    value REAL,
    deal_type TEXT NOT NULL,
    source TEXT DEFAULT 'NSE',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(deal_date, symbol, client_name, quantity)
);

-- Source 4: FII/DII Activity
CREATE TABLE IF NOT EXISTS fii_dii_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    category TEXT NOT NULL,
    buy_value_cr REAL,
    sell_value_cr REAL,
    net_value_cr REAL,
    source TEXT DEFAULT 'NSE',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(date, category)
);

-- Source 5: Shareholding Patterns
CREATE TABLE IF NOT EXISTS shareholding_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    company_name TEXT,
    quarter TEXT NOT NULL,
    promoter_pct REAL,
    fii_pct REAL,
    dii_pct REAL,
    mf_pct REAL,
    public_pct REAL,
    total_shares INTEGER,
    source TEXT DEFAULT 'NSE',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(symbol, quarter)
);

-- Feature: Signal Clusters
CREATE TABLE IF NOT EXISTS signal_clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    company_name TEXT,
    cluster_score REAL NOT NULL,
    cluster_tier TEXT NOT NULL,
    source_count INTEGER NOT NULL,
    sources_hit TEXT NOT NULL,
    insider_buy_count INTEGER DEFAULT 0,
    sast_count INTEGER DEFAULT 0,
    bulk_block_count INTEGER DEFAULT 0,
    mf_accumulation INTEGER DEFAULT 0,
    total_transaction_value REAL,
    first_signal_date TEXT,
    last_signal_date TEXT,
    window_days INTEGER DEFAULT 30,
    computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(symbol, last_signal_date)
);

-- Feature: Promoter Streaks
CREATE TABLE IF NOT EXISTS promoter_streaks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    company_name TEXT,
    distinct_insiders INTEGER NOT NULL,
    insider_names TEXT,
    total_value REAL,
    window_start_date TEXT,
    window_end_date TEXT,
    streak_strength TEXT NOT NULL,
    computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(symbol, window_end_date)
);

-- Feature: Stock Fundamentals
CREATE TABLE IF NOT EXISTS stock_fundamentals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL UNIQUE,
    company_name TEXT,
    sector TEXT,
    industry TEXT,
    market_cap_cr REAL,
    current_price REAL,
    roce_current REAL,
    roce_3yr_avg REAL,
    roce_5yr_avg REAL,
    roe_current REAL,
    roe_5yr_avg REAL,
    debt_to_equity REAL,
    interest_coverage REAL,
    sales_cagr_5yr REAL,
    profit_cagr_5yr REAL,
    sales_growth_stddev REAL,
    fcf_5yr_cumulative REAL,
    profit_5yr_cumulative REAL,
    fcf_conversion REAL,
    pe_current REAL,
    pe_5yr_median REAL,
    pe_vs_median REAL,
    peg_ratio REAL,
    promoter_holding_pct REAL,
    promoter_pledge_pct REAL,
    quality_score REAL,
    quality_tier TEXT,
    red_flags TEXT,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    source TEXT DEFAULT 'Screener.in'
);
        """)

        # Create consolidated signals view (drop+recreate to allow schema updates)
        conn.execute("DROP VIEW IF EXISTS consolidated_signals")
        conn.execute("""
CREATE VIEW consolidated_signals AS
SELECT
    'INSIDER_BUY' as signal_type,
    symbol,
    company_name,
    insider_name as entity_name,
    person_category as entity_type,
    transaction_type as action,
    value as value_inr,
    quantity,
    disclosure_date as signal_date,
    CASE
        WHEN person_category = 'Promoters' AND transaction_type = 'Buy' AND value > 10000000 THEN 'HIGH'
        WHEN person_category = 'Promoters' AND transaction_type = 'Buy' THEN 'MEDIUM'
        WHEN transaction_type = 'Buy' THEN 'LOW'
        ELSE 'INFO'
    END as signal_strength,
    created_at
FROM insider_trades

UNION ALL

SELECT
    'SAST_ACCUMULATION' as signal_type,
    symbol,
    company_name,
    acquirer_name as entity_name,
    'Acquirer' as entity_type,
    transaction_type as action,
    NULL as value_inr,
    shares_transacted as quantity,
    disclosure_date as signal_date,
    CASE
        WHEN holding_after_pct > holding_before_pct AND holding_after_pct >= 10 THEN 'HIGH'
        WHEN holding_after_pct > holding_before_pct THEN 'MEDIUM'
        ELSE 'INFO'
    END as signal_strength,
    created_at
FROM sast_disclosures

UNION ALL

SELECT
    'BULK_BLOCK_DEAL' as signal_type,
    symbol,
    company_name,
    client_name as entity_name,
    deal_type as entity_type,
    buy_sell as action,
    value as value_inr,
    quantity,
    deal_date as signal_date,
    CASE
        WHEN buy_sell = 'BUY' AND value > 50000000 THEN 'HIGH'
        WHEN buy_sell = 'BUY' THEN 'MEDIUM'
        ELSE 'INFO'
    END as signal_strength,
    created_at
FROM bulk_block_deals

ORDER BY signal_date DESC
        """)

    logger.info("Database initialized at %s", DB_PATH)


# ---------- Generic helpers ----------

def insert_many(table: str, rows: list, conn: sqlite3.Connection):
    """INSERT OR IGNORE a list of dicts into table."""
    if not rows:
        return 0
    cols = list(rows[0].keys())
    placeholders = ", ".join("?" * len(cols))
    col_list = ", ".join(cols)
    sql = f"INSERT OR IGNORE INTO {table} ({col_list}) VALUES ({placeholders})"
    data = [tuple(r.get(c) for c in cols) for r in rows]
    cursor = conn.executemany(sql, data)
    return cursor.rowcount


def query(sql: str, params: tuple = (), as_dict: bool = True):
    """Run a SELECT and return list of dicts (or Row objects)."""
    with db_conn() as conn:
        cursor = conn.execute(sql, params)
        rows = cursor.fetchall()
        if as_dict:
            return [dict(r) for r in rows]
        return rows


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
    print("DB initialized. Tables created.")
    with db_conn() as conn:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"
        ).fetchall()
        for t in tables:
            print(" -", t[0])
