"""Tests for the shared rating heuristic and the SignalProcessor adapter.

The Portfolio Manager produces a typed PortfolioDecision via structured
output and renders it to markdown that always contains a ``**Rating**: X``
header.  The deterministic heuristic in ``tradingagents.agents.utils.rating``
is therefore sufficient to extract the rating downstream — no second LLM
call is needed — and SignalProcessor is now a thin adapter that delegates
to it.
"""

import pytest

from tradingagents.agents.utils.rating import RATINGS_5_TIER, parse_rating
from tradingagents.graph.signal_processing import SignalProcessor


# ---------------------------------------------------------------------------
# Heuristic parser
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestParseRating:
    def test_explicit_label_buy(self):
        assert parse_rating("Rating: Buy\nReasoning here.") == "Buy"

    def test_explicit_label_overweight(self):
        assert parse_rating("Rating: Overweight\nDetails.") == "Overweight"

    def test_explicit_label_with_markdown_bold_value(self):
        # Regression: Rating: **Sell** — markdown around the value.
        assert parse_rating("Rating: **Sell**\nExit immediately.") == "Sell"

    def test_explicit_label_with_markdown_bold_label(self):
        assert parse_rating("**Rating**: Underweight\nTrim exposure.") == "Underweight"

    def test_rendered_pm_markdown_shape(self):
        # The exact shape produced by render_pm_decision must always parse.
        text = (
            "**Rating**: Buy\n\n"
            "**Executive Summary**: Enter at $189-192, 6% portfolio cap.\n\n"
            "**Investment Thesis**: AI capex cycle intact; institutional flows constructive."
        )
        assert parse_rating(text) == "Buy"

    def test_explicit_label_wins_over_prose_with_markdown(self):
        text = (
            "The buy thesis is weakened by guidance.\n"
            "Rating: **Sell**\n"
            "Exit before earnings."
        )
        assert parse_rating(text) == "Sell"

    def test_no_rating_returns_default(self):
        assert parse_rating("No clear directional signal at this time.") == "Hold"

    def test_no_rating_custom_default(self):
        assert parse_rating("Plain prose.", default="Underweight") == "Underweight"

    def test_all_five_tiers_recognised(self):
        for r in RATINGS_5_TIER:
            assert parse_rating(f"Rating: {r}") == r


@pytest.mark.unit
class TestParseRatingChinese:
    """Chinese free-text path (issues #78 / #80).

    When output_language is Chinese and structured output falls back to
    free-text, the decision has no English ``Rating:`` header — only a
    Chinese label like ``最终评级：卖出``. These previously all defaulted
    to Hold.
    """

    def test_issue_78_exact_shape(self):
        # The exact decision shape from issue #78 that displayed HOLD.
        text = (
            "👔 最终投资建议\n"
            "研究经理：辩论复盘与最终投资计划书\n"
            "标的：贵州茅台\n"
            "辩论回合：牛熊充分交锋\n"
            "最终评级：卖出\n"
            "核心结论：熊方以详实的数据彻底拆解了牛方的黄金坑幻想。"
        )
        assert parse_rating(text) == "Sell"

    def test_cn_label_each_tier(self):
        cases = {
            "买入": "Buy", "增持": "Overweight", "持有": "Hold",
            "减持": "Underweight", "卖出": "Sell", "中性": "Hold",
        }
        for cn, en in cases.items():
            assert parse_rating(f"最终评级：{cn}\n理由若干。") == en

    def test_cn_label_variants(self):
        assert parse_rating("投资建议: **增持**\n分批建仓。") == "Overweight"
        assert parse_rating("评级：清仓") == "Sell"
        assert parse_rating("推荐评级 - 强烈买入") == "Buy"

    def test_cn_strong_beats_plain(self):
        # 强烈买入 must not be read as the shorter 买入 mapping (same tier here,
        # but the longest-match rule matters for correct term identification).
        assert parse_rating("最终评级：强烈卖出") == "Sell"

    def test_cn_label_wins_over_prose_term(self):
        # Prose mentions 大股东减持 (Underweight term) but the labelled rating
        # is 买入 — the label must win.
        text = (
            "分析：需警惕大股东减持压力与解禁风险。\n"
            "最终评级：买入\n"
            "综合判断上行空间显著。"
        )
        assert parse_rating(text) == "Buy"

    def test_cn_bare_term_last_resort(self):
        # No label at all, only a bare Chinese conclusion — better than Hold.
        assert parse_rating("综合来看应当卖出该标的。") == "Sell"

    def test_english_label_still_wins_in_mixed_text(self):
        # English structured render must be unaffected by the Chinese additions.
        assert parse_rating("**Rating**: Buy\n\n**投资论点**：AI 资本开支周期完好。") == "Buy"


# ---------------------------------------------------------------------------
# SignalProcessor: thin adapter over the heuristic
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSignalProcessor:
    def test_returns_rating_from_pm_markdown(self):
        sp = SignalProcessor()
        md = "**Rating**: Overweight\n\n**Executive Summary**: Build gradually."
        assert sp.process_signal(md) == "Overweight"

    def test_makes_no_llm_calls(self):
        """SignalProcessor must not invoke the LLM it was constructed with —
        the rating is parseable from the rendered PM markdown directly."""
        from unittest.mock import MagicMock

        llm = MagicMock()
        sp = SignalProcessor(llm)
        sp.process_signal("Rating: Buy\nDetails.")
        llm.invoke.assert_not_called()
        llm.with_structured_output.assert_not_called()

    def test_default_when_no_rating_present(self):
        sp = SignalProcessor()
        assert sp.process_signal("Plain prose without a recommendation.") == "Hold"
