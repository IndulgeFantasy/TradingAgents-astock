"""
测试脚本：复现 market_analyst 在工具循环后返回空 content 的问题。

问题现象：
  market_analyst: LLM returned no tool_calls but content is empty.
  content_type=str, content_len=0

复现方式：
  模拟 graph 的 analyst <-> tools 循环：
  1. 第一次 invoke -> LLM 返回 tool_calls（调用 get_stock_kline_full 等）
  2. ToolNode 执行工具，返回 ToolMessage
  3. 第二次 invoke（带工具结果）-> LLM 可能又返回 tool_calls
  4. 循环直到 LLM 无 tool_calls -> 检查 content 是否为空

运行方式：
  conda activate worktrade
  python tests/test_market_analyst_empty_content.py 002797
  python tests/test_market_analyst_empty_content.py 002797 --max-rounds 10
"""

import sys
import os
import argparse
import logging

# Setup logging to see the debug output
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_market_analyst_empty_content(ticker: str, max_rounds: int = 10):
    """Simulate the graph's analyst<->tools loop and check for empty content.

    Args:
        ticker: Stock code (e.g. 002797)
        max_rounds: Max tool-call rounds before giving up
    """
    from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
    from tradingagents.agents.utils.agent_utils import (
        build_instrument_context,
        get_indicators,
        get_market_context,
        get_stock_kline_full,
        get_stock_levels,
        get_chip_distribution,
        analyze_pattern,
    )
    from tradingagents.dataflows.config import get_config
    from tradingagents.llm_clients.factory import create_llm_client

    config = get_config()

    # Determine provider/model from env or config (match Web UI behavior)
    provider = os.getenv("TEST_LLM_PROVIDER", config.get("llm_provider", "volcengine"))
    model = os.getenv("TEST_LLM_MODEL", config.get("deep_think_llm", "glm-5.2"))
    base_url = os.getenv("TEST_LLM_BASE_URL", config.get("backend_url"))

    print(f"LLM provider: {provider}")
    print(f"LLM model: {model}")
    print(f"LLM base_url: {base_url or '(provider default)'}")

    # Create LLM client
    deep_client = create_llm_client(
        provider=provider,
        model=model,
        base_url=base_url,
    )
    llm = deep_client.get_llm()

    tools = [
        get_stock_kline_full,
        get_indicators,
        get_market_context,
        get_stock_levels,
        get_chip_distribution,
        analyze_pattern,
    ]

    tool_map = {t.name: t for t in tools}

    # Build the same prompt as market_analyst.py (use the real system_message)
    from tradingagents.agents.analysts.market_analyst import create_market_analyst

    # Extract the system_message by inspecting the actual analyst factory.
    # We replicate the prompt construction here to stay faithful to production.
    from tradingagents.agents.utils.agent_utils import get_language_instruction

    system_message = (
        """你是一位专注于 A 股市场的技术分析师。你的任务是从以下技术指标中选择最多 **8 个**最相关的指标，为给定的 A 股标的提供技术面分析。选择时应注重指标间的互补性，避免冗余。

⚠️ A 股市场特殊规则（分析时必须纳入考量）：
- **涨跌停制度**：主板 ±10%，科创板/创业板 ±20%，ST 股 ±5%。触及涨跌停后流动性骤降，技术指标可能失真。
- **T+1 交易制度**：当日买入次日才能卖出，短线策略的可执行性受限。
- **北向资金**：外资通过沪深港通的流入流出是重要的市场风向标，大幅流入/流出常领先于趋势转折。
- **换手率**：A 股散户占比高，换手率是判断资金活跃度和筹码松动的关键指标。
- **量价关系**：A 股「量在价先」规律显著，放量突破和缩量回调是核心交易信号。

📊 大盘环境参考：调用 get_market_context() 获取当前大盘整体情况（上证指数、沪深300等主要指数走势+两市成交额+涨跌家数+北向/南向资金+融资余额+领涨板块），结合大盘环境判断个股技术面信号的有效性，不过大盘对个股影响相对较小。
- 大盘处于上升趋势时，个股的技术买入信号更可靠
- 大盘处于下跌趋势时，个股的技术支撑位更容易被跌破
- 大盘震荡时，优先选择与大盘走势独立（alpha）的个股

⚠️ 标签使用规范（必读）：
- 工具返回的标签带有方向前缀，**必须原样引用**，禁止改写为简称

📋 增强K线工具：调用 get_stock_kline_full(code, days) 获取含换手率/涨跌幅/成交量的完整K线数据（东财 push2his，约120根）。换手率是判断资金活跃度和筹码松动的关键指标：
- 日均换手率 > 5% 表示筹码活跃，资金接力意愿强
- 日均换手率 < 1% 表示交易低迷，流动性风险较高
- 返回数据含 volume 字段，直接用于计算近5日/近20日平均成交量（SUM(volume[-5:])/5, SUM(volume[-20:])/20），判断放量/缩量

📊 支撑位/压力位：调用 get_stock_levels(code) 获取关键价位。

📈 筹码分布：调用 get_chip_distribution(ticker) 获取筹码分布数据。

🔍 K线形态：调用 analyze_pattern(code) 识别12种K线形态。

可选技术指标（调用 get_indicators 时必须使用下列英文标识符作为参数名）：
均线类: close_50_sma, close_200_sma, close_10_ema
MACD类: macd, macds, macdh
动量类: rsi
波动率类: boll, boll_ub, boll_lb, atr
成交量类: vwma

操作要求：
1. **必须**先调用 get_stock_kline_full(code, days) 获取 K 线数据（含换手率/涨跌幅/ST标记）
2. 再调用 get_indicators 获取选定指标（参数名使用上述英文标识符，否则调用会失败）
3. **必须**调用 get_market_context() 了解当前大盘环境（上证指数/沪深300 走势是必采项，缺失会导致质量门降级）
4. 撰写详细的技术分析报告，包含具体数值和技术信号研判结论（仅供研究参考，不构成投资建议）
5. 报告**开头**必须包含「一、基础数据概览」表格，格式如下：
   | 项目 | 数值 |
   |------|------|
   | 最新收盘价 | X.XX 元（YYYY-MM-DD） |
   | 当日涨跌幅 | ±X.XX%（前日X.XX->今日X.XX） |
   | 近30日累计涨跌幅 | ±X.XX% |
   | 近5日平均成交量 | XXX 股 |
   | 近20日平均成交量 | XXX 股 |
   | 量比（近5日/近20日） | X.XX倍 -> 放量/缩量约XX% |
   | 所属板块 | 主板/创业板/科创板（±10%/±20%涨跌幅限制） |
   数据从 get_stock_kline_full 返回的 K 线数据中计算。所属板块根据股票代码判断（688=科创板，300=创业板，60/00=主板）。
6. 报告末尾附 Markdown 表格汇总关键技术信号和结论
7. **必须**调用 get_stock_levels(code) 获取支撑位和压力位，作为关键价位判断依据

📋 必采清单 - 以下数据点必须出现在报告中，无法获取时标注 [数据缺失: xxx]：
1. 最新收盘价、日期、当日涨跌幅
2. 近 30 日累计涨跌幅
3. 近 5 日平均成交量 vs 近 20 日平均成交量（判断放量/缩量）
4. 至少 3 个技术指标的当前数值和多空信号
5. 关键支撑位和阻力位
6. 当前大盘环境判断（上证指数/沪深300走势，结合 get_market_context）
7. 两市成交额（大盘流动性指标，>1.2万亿为放量，<8000亿为缩量）-- 调用 get_market_context() 获取
8. 上涨家数/下跌家数及涨跌比（市场广度，判断是否普涨/普跌）-- 调用 get_market_context() 获取
9. 融资余额（杠杆情绪指标，融资余额上升=多头加杠杆）-- 调用 get_market_context() 获取
10. 北向资金状况（净买入自2024年起已停止发布，需原样写「已停止发布」；北向成交额合计仍可用）-- 调用 get_market_context() 获取
11. 南向资金净买入（沪港通/深港通，外资流向港股的参考指标）-- 调用 get_market_context() 获取
12. 领涨板块（当日热门板块轮动信号，top 3）-- 调用 get_market_context() 获取
13. 筹码分布（获利比例/集中度/平均成本/健康度）-- 调用 get_chip_distribution(ticker) 获取
14. K线形态（十字星/锤子线/吞没/早晨之星等12种形态）-- 调用 analyze_pattern(code) 获取"""
        + get_language_instruction()
    )

    from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

    current_date = "2026-07-13"
    instrument_context = build_instrument_context(ticker)

    prompt = ChatPromptTemplate.from_messages([
        (
            "system",
            "You are a helpful AI assistant, collaborating with other assistants."
            " Use the provided tools to progress towards answering the question."
            " If you are unable to fully answer, that's OK; another assistant with different tools"
            " will help where you left off. Execute what you can to make progress."
            " If you or any other assistant has the FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** or deliverable,"
            " prefix your response with FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** so the team knows to stop."
            " You have access to the following tools: {tool_names}.\n{system_message}"
            "For your reference, the current date is {current_date}. {instrument_context}",
        ),
        MessagesPlaceholder(variable_name="messages"),
    ])

    prompt = prompt.partial(system_message=system_message)
    prompt = prompt.partial(tool_names=", ".join([t.name for t in tools]))
    prompt = prompt.partial(current_date=current_date)
    prompt = prompt.partial(instrument_context=instrument_context)

    chain = prompt | llm.bind_tools(tools)

    # Simulate the graph loop
    messages = [HumanMessage(content=f"Analyze {ticker} for technical analysis.")]

    print(f"\n{'='*60}")
    print(f"Testing market_analyst empty content issue for {ticker}")
    print(f"Max rounds: {max_rounds}")
    print(f"LLM: {model} ({provider})")
    print(f"{'='*60}\n")

    for round_num in range(1, max_rounds + 1):
        print(f"\n--- Round {round_num} ---")

        try:
            result = chain.invoke(messages)
        except Exception as e:
            print(f"  ERROR: LLM invoke failed: {type(e).__name__}: {e}")
            return False

        messages.append(result)

        # Check tool_calls
        tool_calls = result.tool_calls or []
        content = result.content or ""

        print(f"  tool_calls: {len(tool_calls)}")
        for tc in tool_calls:
            print(f"    -> {tc.get('name', '?')}({tc.get('args', {})})")

        print(f"  content_type: {type(content).__name__}")
        print(f"  content_len: {len(content)}")
        if content:
            # Safe preview: replace non-ascii to avoid gbk encoding errors
            preview = content[:200].encode('ascii', 'replace').decode('ascii')
            print(f"  content_preview: {preview}...")

        if len(tool_calls) == 0:
            # No more tool calls - this is where the report should be generated
            if not content.strip():
                print("\n  *** EMPTY CONTENT DETECTED - testing retry fix ***")
                print("  LLM returned no tool_calls but content is EMPTY!")
                print(f"  content_type={type(content).__name__}, content_len={len(content)}")
                print(f"  Total messages in conversation: {len(messages)}")

                # Print message sizes to check if context is too long
                print("\n  Message sizes:")
                total_chars = 0
                for i, msg in enumerate(messages):
                    msg_str = str(msg)
                    msg_len = len(msg_str)
                    total_chars += msg_len
                    msg_type = type(msg).__name__
                    print(f"    [{i}] {msg_type}: {msg_len} chars")
                print(f"  Total context: {total_chars} chars (~{total_chars//4} tokens)")

                # Test the fix: retry without tools
                print("\n  --- Applying retry_report_generation fix ---")
                from tradingagents.agents.utils.agent_utils import retry_report_generation
                report = retry_report_generation(llm, messages[:-1], result, "market_analyst")

                if report.strip():
                    preview = report[:300].encode('ascii', 'replace').decode('ascii')
                    print(f"\n  FIX VERIFIED: retry generated report ({len(report)} chars)")
                    print(f"  Report preview: {preview}...")
                    return False  # Bug fixed
                else:
                    print("\n  FIX FAILED: retry also returned empty content")
                    return True  # Bug not fixed
            else:
                print(f"\n  OK: LLM generated report ({len(content)} chars)")
                preview = content[:300].encode('ascii', 'replace').decode('ascii')
                print(f"  Report preview: {preview}...")
                return False  # No bug
        else:
            # Execute tool calls (simulate ToolNode)
            for tc in tool_calls:
                tool_name = tc.get("name", "")
                tool_args = tc.get("args", {})
                tool_id = tc.get("id", "")

                print(f"  Executing tool: {tool_name}({tool_args})")

                if tool_name in tool_map:
                    try:
                        tool_result = tool_map[tool_name].invoke(tool_args)
                        result_len = len(tool_result) if isinstance(tool_result, str) else len(str(tool_result))
                        print(f"    Result: {result_len} chars")

                        # Truncate for display
                        if result_len > 200:
                            print(f"    Preview: {tool_result[:200]}...")

                        tool_msg = ToolMessage(
                            content=tool_result,
                            tool_call_id=tool_id,
                            name=tool_name,
                        )
                        messages.append(tool_msg)
                    except Exception as e:
                        print(f"    ERROR: {type(e).__name__}: {e}")
                        error_msg = ToolMessage(
                            content=f"Error: {type(e).__name__}: {e}",
                            tool_call_id=tool_id,
                            name=tool_name,
                        )
                        messages.append(error_msg)
                else:
                    print(f"    WARNING: Tool '{tool_name}' not found in tool_map")

    print(f"\n  Reached max rounds ({max_rounds}) without generating report")
    print("  This may also indicate a problem (infinite tool calling loop)")
    return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test market_analyst empty content bug")
    parser.add_argument("ticker", nargs="?", default="002797", help="Stock code (default: 002797)")
    parser.add_argument("--max-rounds", type=int, default=10, help="Max tool-call rounds (default: 10)")
    args = parser.parse_args()

    bug_reproduced = test_market_analyst_empty_content(args.ticker, args.max_rounds)

    print(f"\n{'='*60}")
    if bug_reproduced:
        print("RESULT: BUG REPRODUCED - empty content after tool calls")
    else:
        print("RESULT: No bug - LLM generated report successfully")
    print(f"{'='*60}")

    sys.exit(0 if bug_reproduced else 1)
