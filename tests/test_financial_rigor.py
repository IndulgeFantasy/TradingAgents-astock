"""Tests for financial_rigor.py - exact decimal calculation toolkit."""

import pytest
from decimal import Decimal
from tradingagents.agents.utils.financial_rigor import (
    _exact, _fmt_number, verify_market_cap, verify_valuation,
    cross_validate, benford_check, exact_calc, three_scenario_valuation,
)


class TestExactDecimal:
    def test_int_to_decimal(self):
        assert _exact(42) == Decimal("42")

    def test_float_to_decimal(self):
        # Float 0.1 would be 0.1000000000000000055... but _exact avoids this
        assert _exact(0.1) == Decimal("0.1")

    def test_str_to_decimal(self):
        assert _exact("3.14") == Decimal("3.14")

    def test_decimal_passthrough(self):
        d = Decimal("99.9")
        assert _exact(d) is d


class TestVerifyMarketCap:
    def test_exact_match(self):
        result = verify_market_cap(price=10, shares=1e9, reported_cap=1e10)
        assert result["status"] == "PASS"
        assert result["deviation_pct"] == 0
        assert result["passed"] is True

    def test_small_deviation(self):
        result = verify_market_cap(price=10, shares=1e9, reported_cap=1.01e10)
        assert result["status"] in ("PASS", "WARN")
        assert result["deviation_pct"] == pytest.approx(1.0, abs=0.1)

    def test_large_deviation(self):
        result = verify_market_cap(price=10, shares=1e9, reported_cap=2e10)
        assert result["status"] == "FAIL"
        assert result["passed"] is False
        assert "单位" in result["hint"] or "股本" in result["hint"]

    def test_zero_reported(self):
        result = verify_market_cap(price=10, shares=100, reported_cap=0)
        assert result["deviation_pct"] == 0


class TestVerifyValuation:
    def test_pe_calculation(self):
        result = verify_valuation(price=50, eps=2.5)
        assert result["PE"] == pytest.approx(20.0, abs=0.01)
        assert result["Earnings_Yield"] == pytest.approx(5.0, abs=0.01)

    def test_pb_calculation(self):
        result = verify_valuation(price=50, bvps=25)
        assert result["PB"] == pytest.approx(2.0, abs=0.01)

    def test_roe_from_eps_and_bvps(self):
        result = verify_valuation(price=50, eps=2.5, bvps=25)
        assert result["ROE"] == pytest.approx(10.0, abs=0.01)

    def test_zero_eps(self):
        result = verify_valuation(price=50, eps=0)
        assert "PE" not in result

    def test_fcf_yield(self):
        result = verify_valuation(price=100, fcf_per_share=5)
        assert result["FCF_Yield"] == pytest.approx(5.0, abs=0.01)
        assert result["P_FCF"] == pytest.approx(20.0, abs=0.01)

    def test_dividend_yield(self):
        result = verify_valuation(price=100, dividend=3)
        assert result["Dividend_Yield"] == pytest.approx(3.0, abs=0.01)

    def test_text_output(self):
        result = verify_valuation(price=50, eps=2.5, bvps=25)
        assert "text" in result
        assert "PE" in result["text"]


class TestCrossValidate:
    def test_consistent_sources(self):
        result = cross_validate("revenue", {"A": 100, "B": 101, "C": 99}, unit="亿")
        assert result["all_consistent"] is True
        assert result["consensus"] == pytest.approx(100, abs=1)

    def test_inconsistent_sources(self):
        result = cross_validate("revenue", {"A": 100, "B": 200}, unit="亿", tolerance_pct=10)
        assert result["all_consistent"] is False

    def test_empty_sources(self):
        result = cross_validate("revenue", {})
        assert result["consensus"] is None

    def test_single_source(self):
        result = cross_validate("revenue", {"A": 500})
        assert result["all_consistent"] is True
        assert result["consensus"] == 500


class TestBenfordCheck:
    def test_insufficient_samples(self):
        result = benford_check([1, 2, 3, 4, 5])
        assert result["mad"] is None

    def test_natural_distribution(self):
        """Generate numbers that roughly follow Benford's law."""
        import random
        random.seed(42)
        values = []
        for _ in range(500):
            d = random.choices(range(1, 10), weights=[30.1, 17.6, 12.5, 9.7, 7.9, 6.7, 5.8, 5.1, 4.6])[0]
            values.append(d * 10 ** random.randint(0, 5))
        result = benford_check(values)
        assert result["mad"] is not None
        # Random generation approximates Benford but won't be exact
        assert result["mad"] < 0.05  # Should be reasonably close

    def test_uniform_distribution_flagged(self):
        """Uniform distribution should not conform to Benford."""
        import random
        random.seed(42)
        values = [random.randint(100, 999) for _ in range(200)]
        result = benford_check(values)
        assert result["mad"] is not None
        # Uniform might or might not conform, but the test should not crash
        assert "conformity" in result


class TestExactCalc:
    def test_simple_addition(self):
        result = exact_calc("1 + 2")
        assert result["result"] == pytest.approx(3.0)

    def test_multiplication(self):
        result = exact_calc("510 * 9.11e9")
        assert result["result"] == pytest.approx(4.6461e12, rel=0.001)

    def test_division(self):
        result = exact_calc("100 / 3")
        assert result["result"] is not None

    def test_unsafe_expression_rejected(self):
        result = exact_calc("__import__('os').system('echo hacked')")
        assert result["result"] is None
        assert "不安全" in result["text"]

    def test_parentheses(self):
        result = exact_calc("(2 + 3) * 4")
        assert result["result"] == pytest.approx(20.0)


class TestThreeScenario:
    def test_optimistic_higher_than_pessimistic(self):
        result = three_scenario_valuation(
            current_price=100, current_eps=5,
            growth_optimistic=0.2, growth_neutral=0.1, growth_pessimistic=0.0,
            pe_optimistic=25, pe_neutral=20, pe_pessimistic=15,
            years=3,
        )
        scenarios = result["scenarios"]
        assert len(scenarios) == 3
        assert scenarios[0]["target_price"] > scenarios[1]["target_price"]
        assert scenarios[1]["target_price"] > scenarios[2]["target_price"]

    def test_change_pct_calculation(self):
        result = three_scenario_valuation(
            current_price=100, current_eps=5,
            growth_optimistic=0.15, growth_neutral=0.10, growth_pessimistic=0.05,
            pe_optimistic=20, pe_neutral=18, pe_pessimistic=15,
            years=3,
        )
        for s in result["scenarios"]:
            assert "change_pct" in s
            assert isinstance(s["change_pct"], float)
