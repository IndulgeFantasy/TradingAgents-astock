"""数据驱动的情绪分析师。

情绪分析师不只读新闻：资金流是最硬的情绪证据，大盘情绪传导与行业热力给情绪
广度，新闻负责解释成因——而"新闻听起来利好"和"资金正在流出"完全可能同时发生。

这里锁的是两件最容易漂的事：
1. 分析师绑定的工具 与 图里 ToolNode 注册的工具 必须一致——不一致时模型会调用一个
   图里不存在的工具，直接报错，而且只在真跑分析时才暴露。
2. 提示词点名的工具必须真的绑上了——否则模型按提示词去调，调不到。
"""

import inspect
import re

from tradingagents.agents.analysts import social_media_analyst as sma


def _tool_names_in_source(func) -> set:
    """从 create_social_media_analyst 源码里取出 tools 列表的成员名。"""
    src = inspect.getsource(func)
    block = re.search(r"tools = \[(.*?)\]", src, re.S)
    assert block, "找不到 tools 列表"
    return {t.strip().rstrip(",") for t in block.group(1).split("\n") if t.strip().rstrip(",")
            and not t.strip().startswith("#")}


EXPECTED_TOOLS = {"get_news", "get_fund_flow", "get_market_context", "get_industry_hotmap"}


def test_analyst_binds_quantitative_tools():
    """光有新闻不足以判断情绪，资金流/大盘情绪/行业热力必须都在。"""
    assert _tool_names_in_source(sma.create_social_media_analyst) == EXPECTED_TOOLS


def test_graph_tool_node_matches_analyst_tools():
    """图里注册的 social 工具必须与分析师绑定的一致，否则运行时才炸。"""
    import tradingagents.graph.trading_graph as tg

    src = inspect.getsource(tg.TradingAgentsGraph._create_tool_nodes)
    social_block = re.search(r'"social": ToolNode\(\s*\[(.*?)\]\s*\)', src, re.S)
    assert social_block, "找不到 social 的 ToolNode 定义"
    registered = {
        t.strip().rstrip(",")
        for t in social_block.group(1).split("\n")
        if t.strip().rstrip(",") and not t.strip().startswith("#")
    }
    assert registered == EXPECTED_TOOLS


def test_prompt_names_every_bound_tool():
    """提示词点名的工具必须真的绑上了，否则模型照着调会调空。"""
    src = inspect.getsource(sma.create_social_media_analyst)
    for tool in EXPECTED_TOOLS:
        assert f"`{tool}(" in src or f"{tool}()" in src, f"提示词里没有引导模型使用 {tool}"


def test_prompt_requires_divergence_check():
    """资金面与消息面背离是情绪分析最有价值的产出，必须强制写进报告。"""
    src = inspect.getsource(sma.create_social_media_analyst)
    assert "一致/背离" in src, "提示词必须要求判断资金面与消息面是否一致/背离"
    assert "背离时说明方向" in src


def test_prompt_forbids_fabricating_missing_data():
    """取不到数就标注缺失，不许用新闻语气编一个数字出来。"""
    src = inspect.getsource(sma.create_social_media_analyst)
    assert "数据缺失" in src
