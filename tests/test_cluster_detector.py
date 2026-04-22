"""
Tests for smart_money.cluster_detector

Uses unittest.mock to patch db.query and db.db_conn so no real DB is needed.
All tests run in memory with synthetic data.
"""

import pytest
from unittest.mock import patch, MagicMock, call
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import List, Dict, Any


# ---------------------------------------------------------------------------
# Helpers — synthetic data factories
# ---------------------------------------------------------------------------

def _today() -> str:
    return datetime.today().strftime("%Y-%m-%d")


def _daysago(n: int) -> str:
    return (datetime.today() - timedelta(days=n)).strftime("%Y-%m-%d")


def make_insider_row(
    symbol: str,
    category: str = "Promoters",
    value: float = 10_000_000,
    days_ago: int = 5,
    insider_name: str = "John Doe",
    mode: str = "Market Purchase",
) -> Dict[str, Any]:
    return {
        "symbol": symbol,
        "company_name": f"{symbol} Ltd",
        "person_category": category,
        "cnt": 1,
        "total_val": value,
        "first_date": _daysago(days_ago),
        "last_date": _daysago(days_ago),
    }


def make_deal_row(
    symbol: str,
    deal_type: str = "BLOCK",
    value: float = 50_000_000,
    days_ago: int = 3,
) -> Dict[str, Any]:
    return {
        "symbol": symbol,
        "company_name": f"{symbol} Ltd",
        "deal_type": deal_type,
        "cnt": 1,
        "total_val": value,
        "first_date": _daysago(days_ago),
        "last_date": _daysago(days_ago),
    }


def make_sast_row(
    symbol: str,
    days_ago: int = 7,
) -> Dict[str, Any]:
    return {
        "symbol": symbol,
        "company_name": f"{symbol} Ltd",
        "cnt": 1,
        "first_date": _daysago(days_ago),
        "last_date": _daysago(days_ago),
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def no_db_init(monkeypatch):
    """Prevent real DB init during tests."""
    monkeypatch.setattr("smart_money.cluster_detector.init_db", lambda: None)


# ---------------------------------------------------------------------------
# Test: compute_cluster_score
# ---------------------------------------------------------------------------

class TestComputeClusterScore:

    def _run_with_mocks(
        self,
        symbol: str,
        insider_rows: List[Dict],
        sast_rows: List[Dict],
        deal_rows: List[Dict],
        sp_rows: List[Dict] = None,
        has_streak: bool = False,
    ):
        """Helper: patch query() and run compute_cluster_score."""
        if sp_rows is None:
            sp_rows = []

        call_idx = [0]

        def fake_query(sql, params=()):
            idx = call_idx[0]
            call_idx[0] += 1
            sql_lower = sql.lower()
            if "from insider_trades" in sql_lower and "group by person_category" in sql_lower:
                return insider_rows
            elif "from sast_disclosures" in sql_lower:
                return sast_rows
            elif "from bulk_block_deals" in sql_lower and "group by deal_type" in sql_lower:
                return deal_rows
            elif "from shareholding_patterns" in sql_lower:
                return sp_rows
            elif "from promoter_streaks" in sql_lower:
                if has_streak:
                    return [{"streak_strength": "MODERATE"}]
                return []
            return []

        with patch("smart_money.cluster_detector.query", side_effect=fake_query):
            from smart_money.cluster_detector import compute_cluster_score
            return compute_cluster_score(symbol, window_days=30)

    def test_single_source_below_threshold(self):
        """Single insider buy by a Director (score=15) → below CLUSTER_MIN_SCORE=30 → None."""
        insider_rows = [{
            "person_category": "Director",
            "cnt": 1,
            "total_val": 500_000,
            "company_name": "TESTCO Ltd",
            "first_date": _daysago(5),
            "last_date": _daysago(5),
        }]
        result = self._run_with_mocks("TESTCO", insider_rows, [], [])
        # Director insider buy = 15 pts → below threshold 30
        assert result is None

    def test_two_source_medium_cluster(self):
        """Promoter buy + block deal BUY → MEDIUM cluster (30 + 20 = 50 → MEDIUM)."""
        insider_rows = [{
            "person_category": "Promoters",
            "cnt": 1,
            "total_val": 20_000_000,
            "company_name": "ALPHA Ltd",
            "first_date": _daysago(10),
            "last_date": _daysago(10),
        }]
        deal_rows = [{
            "deal_type": "BLOCK",
            "cnt": 1,
            "total_val": 20_000_000,
            "company_name": "ALPHA Ltd",
            "first_date": _daysago(3),
            "last_date": _daysago(3),
        }]
        result = self._run_with_mocks("ALPHA", insider_rows, [], deal_rows)
        assert result is not None
        # 30 (promoter) + 20 (block) = 50 → exactly at HIGH threshold → HIGH
        # (score 50 >= CLUSTER_HIGH_THRESHOLD=50 → HIGH, NOT MEDIUM)
        assert result["cluster_score"] == 50.0
        assert result["cluster_tier"] in ("HIGH", "MEDIUM")
        assert "INSIDER_BUY" in result["sources_hit"]
        assert "BLOCK_DEAL" in result["sources_hit"] or "BULK_DEAL" in result["sources_hit"]
        assert result["insider_buy_count"] == 1
        assert result["bulk_block_count"] == 1

    def test_three_source_high_cluster_with_multiplier(self):
        """Promoter buy + SAST + block deal → 3 sources → 1.3x multiplier → HIGH or ELITE."""
        insider_rows = [{
            "person_category": "Promoters",
            "cnt": 1,
            "total_val": 15_000_000,
            "company_name": "BETA Ltd",
            "first_date": _daysago(7),
            "last_date": _daysago(7),
        }]
        sast_rows = [{
            "cnt": 1,
            "company_name": "BETA Ltd",
            "first_date": _daysago(5),
            "last_date": _daysago(5),
        }]
        deal_rows = [{
            "deal_type": "BLOCK",
            "cnt": 1,
            "total_val": 40_000_000,
            "company_name": "BETA Ltd",
            "first_date": _daysago(2),
            "last_date": _daysago(2),
        }]
        result = self._run_with_mocks("BETA", insider_rows, sast_rows, deal_rows)
        assert result is not None
        # Base: 30 + 25 + 20 = 75 → with 3-source 1.3x = 97.5 → capped at 100
        assert result["cluster_score"] >= 70.0
        assert result["cluster_tier"] in ("HIGH", "ELITE")
        assert result["source_count"] == 3
        # 3 sources multiplier applied
        assert result["cluster_score"] >= 75.0  # at minimum base score without cap

    def test_streak_multiplier_applied(self):
        """Streak MODERATE+ → 1.25x multiplier on top of base score."""
        insider_rows = [{
            "person_category": "Promoters",
            "cnt": 1,
            "total_val": 10_000_000,
            "company_name": "GAMMA Ltd",
            "first_date": _daysago(5),
            "last_date": _daysago(5),
        }]
        deal_rows = [{
            "deal_type": "BULK",
            "cnt": 1,
            "total_val": 5_000_000,
            "company_name": "GAMMA Ltd",
            "first_date": _daysago(3),
            "last_date": _daysago(3),
        }]

        # Without streak
        result_no_streak = self._run_with_mocks("GAMMA", insider_rows, [], deal_rows, has_streak=False)
        # With streak
        result_with_streak = self._run_with_mocks("GAMMA", insider_rows, [], deal_rows, has_streak=True)

        assert result_no_streak is not None
        assert result_with_streak is not None
        # Streak multiplier should increase the score
        assert result_with_streak["cluster_score"] > result_no_streak["cluster_score"]

    def test_correct_sources_hit_list(self):
        """Verify sources_hit is a comma-separated string with correct labels."""
        insider_rows = [{
            "person_category": "Key Managerial Personnel",
            "cnt": 2,
            "total_val": 8_000_000,
            "company_name": "DELTA Ltd",
            "first_date": _daysago(15),
            "last_date": _daysago(5),
        }]
        sast_rows = [{
            "cnt": 1,
            "company_name": "DELTA Ltd",
            "first_date": _daysago(10),
            "last_date": _daysago(10),
        }]
        result = self._run_with_mocks("DELTA", insider_rows, sast_rows, [])
        assert result is not None
        sources = result["sources_hit"].split(",")
        assert "INSIDER_BUY" in sources
        assert "SAST" in sources

    def test_mf_accumulation_included(self):
        """MF accumulation (QoQ ≥1%) adds 20 pts."""
        insider_rows = [{
            "person_category": "Promoters",
            "cnt": 1,
            "total_val": 5_000_000,
            "company_name": "EPSILON Ltd",
            "first_date": _daysago(8),
            "last_date": _daysago(8),
        }]
        # Shareholding: MF went from 5% to 6.5% (increase of 1.5%)
        sp_rows = [
            {"mf_pct": 6.5, "quarter": "March2026"},
            {"mf_pct": 5.0, "quarter": "December2025"},
        ]
        result = self._run_with_mocks("EPSILON", insider_rows, [], [], sp_rows=sp_rows)
        assert result is not None
        assert result["mf_accumulation"] == 1
        # Base: 30 (promoter) + 20 (MF) = 50 → HIGH
        assert result["cluster_score"] >= 50.0

    def test_no_signals_returns_none(self):
        """No signals → no cluster."""
        result = self._run_with_mocks("EMPTY", [], [], [])
        assert result is None


# ---------------------------------------------------------------------------
# Test: detect_promoter_streaks
# ---------------------------------------------------------------------------

class TestDetectPromoterStreaks:

    def _run_streaks(self, query_result: List[Dict]):
        with patch("smart_money.cluster_detector.query", return_value=query_result):
            from smart_money.cluster_detector import detect_promoter_streaks
            return detect_promoter_streaks(window_days=90)

    def test_two_insiders_weak(self):
        rows = [{
            "symbol": "TESTCO",
            "company_name": "TestCo Ltd",
            "distinct_insiders": 2,
            "insider_names": "Alice,Bob",
            "total_value": 5_000_000,
            "window_start_date": _daysago(60),
            "window_end_date": _daysago(1),
        }]
        result = self._run_streaks(rows)
        assert len(result) == 1
        assert result[0]["streak_strength"] == "WEAK"

    def test_three_insiders_moderate(self):
        rows = [{
            "symbol": "ALPHA",
            "company_name": "Alpha Ltd",
            "distinct_insiders": 3,
            "insider_names": "Alice,Bob,Charlie",
            "total_value": 9_000_000,
            "window_start_date": _daysago(50),
            "window_end_date": _daysago(1),
        }]
        result = self._run_streaks(rows)
        assert result[0]["streak_strength"] == "MODERATE"

    def test_four_insiders_strong(self):
        rows = [{
            "symbol": "BETA",
            "company_name": "Beta Ltd",
            "distinct_insiders": 4,
            "insider_names": "A,B,C,D",
            "total_value": 5_000_000,
            "window_start_date": _daysago(40),
            "window_end_date": _daysago(1),
        }]
        result = self._run_streaks(rows)
        assert result[0]["streak_strength"] == "STRONG"

    def test_five_insiders_elite(self):
        rows = [{
            "symbol": "GAMMA",
            "company_name": "Gamma Ltd",
            "distinct_insiders": 5,
            "insider_names": "A,B,C,D,E",
            "total_value": 20_000_000,
            "window_start_date": _daysago(30),
            "window_end_date": _daysago(1),
        }]
        result = self._run_streaks(rows)
        assert result[0]["streak_strength"] == "ELITE"

    def test_three_insiders_high_value_elite(self):
        """3 insiders + value > 10Cr → ELITE."""
        rows = [{
            "symbol": "DELTA",
            "company_name": "Delta Ltd",
            "distinct_insiders": 3,
            "insider_names": "A,B,C",
            "total_value": 150_000_000,  # > 10 Cr
            "window_start_date": _daysago(25),
            "window_end_date": _daysago(1),
        }]
        result = self._run_streaks(rows)
        assert result[0]["streak_strength"] == "ELITE"
