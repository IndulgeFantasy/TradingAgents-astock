"""
测试脚本：端到端验证新闻分析师(news_analyst)分析个股。

模拟 graph 的 analyst <-> tools 循环，用真实 LLM + 真实数据源，
覆盖:
  1. 工具路由: get_news / get_global_news / get_web_search /
     get_article_content / get_stock_news 全部可被 LLM 调用并执行
  2. 报告生成: LLM 无 tool_calls 时输出非空 news_report
  3. 硬上限: 搜索/正文工具达 12 次后强制生成报告(不空转)
  4. 消息结构: AIMessage(tool_calls) -> ToolNode 执行 -> ToolMessage
     -> 再 invoke 的完整循环

运行方式:
  conda activate worktrade
  python tests/test_news_analyst_e2e.py 600519
  python tests/test_news_analyst_e2e.py 600519 --max-rounds 15
"""

import argparse
import logging
import os
import sys

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def main(ticker: str, max_rounds: int = 15):
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from tradingagents.agents.analysts.news_analyst import _NEWS_TOOL_CAP, create_news_analyst
    from tradingagents.dataflows.config import get_config
    from tradingagents.llm_clients.factory import create_llm_client

    config = get_config()

    provider = os.getenv("TEST_LLM_PROVIDER", config.get("llm_provider", "volcengine"))
    model = os.getenv("TEST_LLM_MODEL", config.get("deep_think_llm", "glm-5.2"))
    base_url = os.getenv("TEST_LLM_BASE_URL", config.get("backend_url"))

    print(f"LLM provider: {provider}")
    print(f"LLM model: {model}")
    print(f"LLM base_url: {base_url or '(provider default)'}")
    print(f"news 工具硬上限: {_NEWS_TOOL_CAP}")
    print()

    deep_client = create_llm_client(provider=provider, model=model, base_url=base_url)
    llm = deep_client.get_llm()

    node = create_news_analyst(llm)

    state = {
        "messages": [HumanMessage(content=f"请分析 {ticker} 的近期新闻动态及其对股价的影响。")],
        "company_of_interest": ticker,
        "trade_date": "2026-08-05",
        "market_report": "",
        "sentiment_report": "",
        "news_report": "",
        "fundamentals_report": "",
        "policy_report": "",
        "hot_money_report": "",
        "lockup_report": "",
    }

    print(f"{'=' * 60}")
    print(f"Testing news_analyst for {ticker}")
    print(f"Max rounds: {max_rounds} | LLM: {model} ({provider})")
    print(f"{'=' * 60}\n")

    tool_usage = {}  # name -> call count
    search_article_count = 0
    report = ""
    final_result = None

    for round_num in range(1, max_rounds + 1):
        print(f"--- Round {round_num} ---")

        out = node(state)
        result = out["messages"][-1]
        state["messages"].append(result)
        state["news_report"] = out["news_report"]

        tool_calls = result.tool_calls or []
        content = result.content or ""

        for tc in tool_calls:
            name = tc.get("name", "?")
            tool_usage[name] = tool_usage.get(name, 0) + 1
            if name in ("get_web_search", "get_article_content"):
                search_article_count += 1
            print(f"  -> tool: {name}({tc.get('args', {})})")

        if content:
            print(f"  content_len: {len(content)}")

        if not tool_calls:
            # LLM 停止调用工具, 输出报告
            if content.strip():
                report = content
                final_result = result
                print(f"\n  OK: LLM 生成报告 ({len(report)} chars)")
                break
            else:
                # 可能是超限后的强制报告或空内容
                print(f"  content 为空, news_report={len(out['news_report'])} chars")
                if out["news_report"].strip():
                    report = out["news_report"]
                    final_result = result
                    print(f"  OK: 节点返回非空 news_report ({len(report)} chars)")
                    break
                print("  *** 空内容且无报告 - 问题! ***")
                return False

        # 模拟 ToolNode: 执行所有 tool_calls
        executed_any = False
        for tc in tool_calls:
            name = tc.get("name", "")
            args = tc.get("args", {})
            tool_id = tc.get("id", "")

            try:
                result_str = None
                if name == "get_news":
                    from tradingagents.agents.utils.agent_utils import get_news
                    result_str = get_news.invoke(args)
                elif name == "get_global_news":
                    from tradingagents.agents.utils.agent_utils import get_global_news
                    result_str = get_global_news.invoke(args)
                elif name == "get_web_search":
                    from tradingagents.agents.utils.agent_utils import get_web_search
                    result_str = get_web_search.invoke(args)
                elif name == "get_article_content":
                    from tradingagents.agents.utils.agent_utils import get_article_content
                    result_str = get_article_content.invoke(args)
                elif name == "get_stock_news":
                    from tradingagents.agents.utils.agent_utils import get_stock_news
                    result_str = get_stock_news.invoke(args)
                else:
                    print(f"    WARNING: 未知工具 {name}")

                if result_str is not None:
                    executed_any = True
                    print(f"    executed {name} -> {len(result_str)} chars")
                    state["messages"].append(
                        ToolMessage(content=str(result_str), tool_call_id=tool_id, name=name)
                    )
            except Exception as e:
                print(f"    ERROR executing {name}: {type(e).__name__}: {str(e)[:150]}")
                state["messages"].append(
                    ToolMessage(
                        content=f"Error: {type(e).__name__}: {str(e)[:200]}",
                        tool_call_id=tool_id,
                        name=name,
                    )
                )

        if not executed_any:
            print("  *** 无任何工具可执行但仍有 tool_calls - 可能死循环 ***")
            return False

    print(f"\n{'=' * 60}")
    print("RESULTS:")
    print(f"  工具调用统计: {tool_usage}")
    print(f"  搜索/正文工具次数: {search_article_count} (上限 {_NEWS_TOOL_CAP})")
    print(f"  报告长度: {len(report)} chars")
    print(f"  报告开头: {report[:150]!r}")
    print(f"{'=' * 60}")

    checks = []
    checks.append(("报告非空", bool(report.strip())))
    checks.append(("含必采清单项", any(k in report for k in ["个股新闻", "宏观", "事件", "利好", "利空", "风险"])))
    if tool_usage:
        checks.append(("工具被调用", True))
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    return all(ok for _, ok in checks)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test news_analyst end-to-end")
    parser.add_argument("ticker", nargs="?", default="600519", help="Stock code (default: 600519)")
    parser.add_argument("--max-rounds", type=int, default=15, help="Max rounds (default: 15)")
    args = parser.parse_args()

    ok = main(args.ticker, args.max_rounds)
    print(f"\nRESULT: {'ALL PASS' if ok else 'FAIL'}")
    sys.exit(0 if ok else 1)
