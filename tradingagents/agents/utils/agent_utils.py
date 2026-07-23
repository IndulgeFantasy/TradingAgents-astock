from langchain_core.messages import HumanMessage, RemoveMessage
import logging

# Import tools from separate utility files
from tradingagents.agents.utils.core_stock_tools import (
    get_stock_data
)
from tradingagents.agents.utils.technical_indicators_tools import (
    get_indicators
)
from tradingagents.agents.utils.fundamental_data_tools import (
    get_fundamentals,
    get_balance_sheet,
    get_cashflow,
    get_income_statement
)
from tradingagents.agents.utils.news_data_tools import (
    get_news,
    get_insider_transactions,
    get_global_news
)
from tradingagents.agents.utils.signal_data_tools import (
    get_profit_forecast,
    get_hot_stocks,
    get_northbound_flow,
    get_concept_blocks,
    get_fund_flow,
    get_dragon_tiger_board,
    get_lockup_expiry,
    get_industry_comparison,
    get_chip_distribution,
    get_limit_up_pool,
)
from tradingagents.agents.utils.playwright_tools import (
    get_stock_basic,
    get_stock_homepage,
    get_stock_industry_peers,
    get_stock_holder,
    get_stock_equity_history,
    get_stock_position,
    get_market_context,
    get_stock_kline_full,
    get_financial_quarterly,
    get_stock_levels,
)
from tradingagents.agents.utils.analysis_tools import (
    analyze_pattern,
)
from tradingagents.agents.utils.financial_rigor import (
    verify_stock_valuation,
)


def get_language_instruction() -> str:
    """Return a prompt instruction for the configured output language.

    Returns empty string when English (default), so no extra tokens are used.
    Only applied to user-facing agents (analysts, portfolio manager).
    Internal debate agents stay in English for reasoning quality.
    """
    from tradingagents.dataflows.config import get_config
    lang = get_config().get("output_language", "English")
    if lang.strip().lower() == "english":
        return ""
    return f" Write your entire response in {lang}."


_logger = logging.getLogger(__name__)


def retry_report_generation(llm, messages, result, analyst_name: str) -> str:
    """Retry report generation when LLM returns empty content after tool calls.

    Some models (e.g. glm-5.2) return tool_calls=[] with content="" after a
    tool-use loop, as if the task is done but without generating the final
    report text. This helper re-invokes the LLM *without* bound tools, forcing
    it to produce a text response.

    Args:
        llm: The base LLM (without bind_tools) for the retry call.
        messages: The conversation messages so far (state["messages"]).
        result: The last AIMessage from the LLM (with empty content).
        analyst_name: Name for logging (e.g. "market_analyst").

    Returns:
        The report text (may still be empty if retry also fails).
    """
    _logger.warning(
        "%s: LLM returned no tool_calls but content is empty. "
        "Retrying without tools to force report generation.",
        analyst_name,
    )
    retry_msg = HumanMessage(
        content="你已经获取了所有需要的数据。请根据上述工具返回的数据，"
        "撰写完整的分析报告。不要调用任何工具，直接输出报告内容。"
    )
    try:
        retry_result = llm.invoke(messages + [result, retry_msg])
        report = retry_result.content if retry_result.content else ""
        if report.strip():
            _logger.info(
                "%s: retry succeeded, report length=%d", analyst_name, len(report)
            )
        else:
            _logger.warning(
                "%s: retry also returned empty content", analyst_name
            )
        return report
    except Exception as e:
        _logger.warning(
            "%s: retry failed: %s: %s", analyst_name, type(e).__name__, str(e)[:200]
        )
        return ""


def build_instrument_context(ticker: str) -> str:
    """Describe the exact instrument so agents preserve exchange-qualified tickers."""
    return (
        f"The instrument to analyze is `{ticker}`. "
        "Use this exact ticker in every tool call, report, and recommendation, "
        "preserving any exchange suffix (e.g. `.TO`, `.L`, `.HK`, `.T`). "
        "When a tool argument is named `ticker`, pass only this ticker value; "
        "do not pass company names, sectors, concepts, or search keywords."
    )

def create_msg_delete():
    def delete_messages(state):
        """Clear messages and add placeholder for Anthropic compatibility"""
        messages = state["messages"]

        # Remove all messages
        removal_operations = [RemoveMessage(id=m.id) for m in messages]

        # Add a minimal placeholder message
        placeholder = HumanMessage(content="Continue")

        return {"messages": removal_operations + [placeholder]}

    return delete_messages


        
