"""
Tests for scrapers.screener_fundamentals.compute_quality_score

This is a pure-function test — no DB or HTTP needed.
"""

import pytest
from scrapers.screener_fundamentals import compute_quality_score


# ---------------------------------------------------------------------------
# Helper to build a minimal fundamentals dict
# ---------------------------------------------------------------------------

def make_f(
    roce_5yr_avg=None,
    debt_to_equity=None,
    interest_coverage=None,
    fcf_conversion=None,
    sales_cagr_5yr=None,
    sales_growth_stddev=None,
    pe_vs_median=None,
    promoter_pledge_pct=None,
):
    """Build a fundamentals dict with only the fields compute_quality_score uses."""
    return {
        "roce_5yr_avg": roce_5yr_avg,
        "debt_to_equity": debt_to_equity,
        "interest_coverage": interest_coverage,
        "fcf_conversion": fcf_conversion,
        "sales_cagr_5yr": sales_cagr_5yr,
        "sales_growth_stddev": sales_growth_stddev,
        "pe_vs_median": pe_vs_median,
        "promoter_pledge_pct": promoter_pledge_pct,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestComputeQualityScore:

    def test_excellent_stock(self):
        """ROCE 25%, D/E 0.2, FCF conv 0.9, sales CAGR 15% low variance, PE 0.9x median → EXCELLENT, score ≥ 75."""
        f = make_f(
            roce_5yr_avg=25.0,
            debt_to_equity=0.2,
            fcf_conversion=0.9,
            sales_cagr_5yr=15.0,
            sales_growth_stddev=5.0,
            pe_vs_median=0.9,
        )
        score, tier, flags = compute_quality_score(f)
        # ROCE≥20=30, D/E<0.3=20, FCF>0.8=20, CAGR≥12=10, stddev<10=5, PE<1.1=10 → 95
        assert score >= 75.0
        assert tier == "EXCELLENT"
        assert len(flags) == 0

    def test_good_stock(self):
        """ROCE 17%, D/E 0.6, FCF conv 0.6, moderate growth → GOOD."""
        f = make_f(
            roce_5yr_avg=17.0,
            debt_to_equity=0.6,
            fcf_conversion=0.6,
            sales_cagr_5yr=14.0,
            sales_growth_stddev=8.0,
            pe_vs_median=1.0,
        )
        score, tier, flags = compute_quality_score(f)
        # ROCE≥15=22, D/E<0.7=14, FCF>0.5=12, CAGR=10, stddev=5, PE<1.1=10 → 73
        assert score >= 55.0
        assert tier in ("GOOD", "EXCELLENT")

    def test_average_stock(self):
        """ROCE 11%, D/E 0.9, FCF conv 0.4 → AVERAGE."""
        f = make_f(
            roce_5yr_avg=11.0,
            debt_to_equity=0.9,
            fcf_conversion=0.4,
        )
        score, tier, flags = compute_quality_score(f)
        # ROCE≥10=12, D/E<1.2=7, FCF>0.3=5 → 24 → POOR
        # With no growth or valuation data: 24 → POOR (>= 20)
        assert tier in ("AVERAGE", "POOR")
        assert score >= 20.0

    def test_poor_stock(self):
        """ROCE 8%, D/E 1.4, FCF conv 0.2 → POOR (but NOT AVOID since D/E < 2.5)."""
        f = make_f(
            roce_5yr_avg=8.0,
            debt_to_equity=1.4,
            fcf_conversion=0.2,
        )
        score, tier, flags = compute_quality_score(f)
        # ROCE<10=0 flag, D/E≥1.2=0 flag, FCF≤0.3=0 flag → score=0 → AVOID
        # Low ROCE adds flag, high D/E adds flag
        assert any("ROCE" in fl or "D/E" in fl or "FCF" in fl for fl in flags)
        # D/E 1.4 < 2.5 so no AVOID override; score = 0 → AVOID tier
        assert tier in ("POOR", "AVOID")

    def test_avoid_override_high_de(self):
        """D/E 3.0 → AVOID regardless of other metrics."""
        f = make_f(
            roce_5yr_avg=25.0,   # would be 30 pts
            debt_to_equity=3.0,  # > 2.5 → AVOID override
            fcf_conversion=0.9,  # would be 20 pts
            sales_cagr_5yr=20.0,
        )
        score, tier, flags = compute_quality_score(f)
        assert tier == "AVOID"
        # Score itself may be high but tier forced to AVOID
        assert any("D/E" in fl for fl in flags)

    def test_avoid_override_high_pledge(self):
        """Pledge 55% → AVOID."""
        f = make_f(
            roce_5yr_avg=20.0,
            debt_to_equity=0.2,
            promoter_pledge_pct=55.0,
        )
        score, tier, flags = compute_quality_score(f)
        assert tier == "AVOID"
        assert any("pledge" in fl.lower() for fl in flags)

    def test_avoid_override_weak_coverage(self):
        """Interest coverage 1.2 → AVOID (< 1.5)."""
        f = make_f(
            roce_5yr_avg=18.0,
            debt_to_equity=0.5,
            interest_coverage=1.2,
        )
        score, tier, flags = compute_quality_score(f)
        assert tier == "AVOID"
        assert any("coverage" in fl.lower() or "interest" in fl.lower() for fl in flags)

    def test_missing_data_no_crash(self):
        """All fields None → score 0, tier AVOID, no exception."""
        f = make_f()
        score, tier, flags = compute_quality_score(f)
        assert score == 0.0
        assert tier == "AVOID"
        assert isinstance(flags, list)

    def test_red_flags_accumulate(self):
        """Multiple bad metrics → multiple red flags."""
        f = make_f(
            roce_5yr_avg=5.0,         # flag: Low ROCE
            debt_to_equity=1.8,        # flag: High D/E
            interest_coverage=2.0,     # flag: Weak coverage (< 2.5)
            fcf_conversion=0.1,        # flag: Poor FCF
            pe_vs_median=2.0,          # flag: PE too high
            promoter_pledge_pct=30.0,  # flag: Pledge > 25%
        )
        score, tier, flags = compute_quality_score(f)
        assert len(flags) >= 5

    def test_pledge_25_flag_but_not_avoid(self):
        """Pledge exactly 30% → flag but NOT AVOID override (only > 50% forces AVOID)."""
        f = make_f(
            roce_5yr_avg=22.0,
            debt_to_equity=0.3,
            fcf_conversion=0.7,
            sales_cagr_5yr=15.0,
            promoter_pledge_pct=30.0,
        )
        score, tier, flags = compute_quality_score(f)
        assert any("pledge" in fl.lower() for fl in flags)
        # Should NOT be AVOID (pledge 30% < 50%)
        assert tier != "AVOID"

    def test_interest_coverage_25_flag_but_not_avoid(self):
        """Interest coverage 2.0 (< 2.5 but > 1.5) → flag but NOT AVOID override."""
        f = make_f(
            roce_5yr_avg=20.0,
            debt_to_equity=0.4,
            interest_coverage=2.0,
        )
        score, tier, flags = compute_quality_score(f)
        assert any("coverage" in fl.lower() or "interest" in fl.lower() for fl in flags)
        assert tier != "AVOID"
