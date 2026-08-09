from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from tradingagents.agents.utils.agent_utils import (
    build_instrument_context,
    get_article_content,
    get_web_search,
    get_global_news,
    get_language_instruction,
    get_news,
    get_stock_news,
    retry_report_generation,
)
from tradingagents.dataflows.config import get_config

import logging
logger = logging.getLogger(__name__)

# 搜索/正文工具单次分析硬上限。达到上限后节点自动切换为"无工具 LLM 调用",
# 强制基于已有信息生成最终报告(避免只记录搜索过程而无分析结论)。
_NEWS_TOOL_CAP = 12
_NEWS_TOOL_NAMES = ("get_web_search", "get_article_content")


def _count_news_tools(messages) -> int:
    """统计 messages 中 news 专用工具(搜索/正文)的调用次数。"""
    count = 0
    for msg in messages:
        if isinstance(msg, dict):
            tools = msg.get("tool_calls") or []
        else:
            tools = getattr(msg, "tool_calls", None) or []
        for tc in tools:
            if isinstance(tc, dict) and tc.get("name") in _NEWS_TOOL_NAMES:
                count += 1
    return count


def create_news_analyst(llm):
    def news_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = build_instrument_context(state["company_of_interest"])

        tools = [
            get_news,
            get_global_news,
            get_web_search,
            get_article_content,
            get_stock_news,
        ]

        system_message = (
            "你是一位专注于 A 股市场的新闻与政策分析师。你的任务是分析近期新闻动态，评估其对目标公司和 A 股市场的影响。"
            "\n\n⚠️ A 股新闻分析框架："
            "\n- **政策敏感度**：A 股是典型的「政策市」，国务院/证监会/央行/发改委的政策发布对市场影响巨大。重点关注：货币政策（降准降息）、产业政策（扶持/限制）、监管政策（IPO 节奏、再融资、减持新规）。"
            "\n- **消息来源权重**：财联社快讯（最快）> 新华财经/证券时报（权威）> 东方财富/同花顺（广泛）。注意区分官方消息与市场传闻。"
            "\n- **行业轮动**：A 股板块轮动特征明显，一个行业利好政策可能带动整个板块，分析时需关注产业链上下游联动。"
            "\n- **事件驱动**：关注财报预告/业绩快报、股东大会决议、重大合同公告、机构调研记录等公司层面事件。"
            "\n\n🚧 职责边界（重要）——以下内容已有专门分析师覆盖，**不要搜索、不要采集、不要分析**："
            "\n- 股价走势/技术指标/支撑压力位/筹码分布（市场分析师）"
            "\n- 财务三表/估值/高管持股变动/股东变动/担保/违规/机构调研等公司治理数据（基本面分析师）"
            "\n- 龙虎榜/主力资金/涨停池/解禁减持（游资追踪/解禁监控分析师）"
            "\n- 这些维度最终由牛熊辩论家汇总全部分析师报告，你无需重复。"
            "\n- 你的唯一职责：新闻事件、政策发布、市场传闻、行业动态及其影响评估。"
            "\n- 若新闻中涉及高管变动/业绩公告/解禁等事项，只需**引用新闻事实本身**（如\"6月17日公告执行董事辞任\"），不要补充人名/财务数值等细节——细节由对应分析师提供。"
            "\n\n请使用以下工具："
            "\n- `get_news(ticker, start_date, end_date)`：获取公司相关的个股新闻，ticker 必须使用目标股票的 6 位代码"
            "\n- `get_global_news(curr_date, look_back_days, limit)`：获取宏观经济和市场整体新闻"
            "\n- `get_web_search(query, count, freshness)`：网页搜索（国内直连，引擎由配置 search_engine 决定，默认夸克 AI 含结构化 AI 总结，可切 Bing），检索定向问题——特定事件/传闻/政策原文/行业动态，返回标题/URL/摘要/来源域名/发布时间。用于补充 get_news 覆盖不到的深度信息"
            "\n- `get_article_content(url, max_chars)`：打开搜索结果中的链接抓取正文（东财/财联社/新浪/证券时报/同花顺/微信有专用解析），命中权威来源且需看细节时调用；抓取失败回退摘要"
            "\n- `get_stock_news(limit)`：东财股票频道重点栏目新闻汇总（股市聚焦/大盘分析/板块聚焦/行业研究/热门股追踪/主力动态/股市直播等，含完整标题+链接+所属区块），快速浏览当日 A 股市场要闻全貌"
            "\n\n📡 最佳使用时序（严格按此顺序）："
            "\n① 先调用 get_news(ticker, ...) + get_global_news(...) 覆盖常规新闻（财联社/东财结构化快讯作为报告底座）"
            "\n② 识别信息缺口（政策原文核实/传闻交叉验证/突发行业事件/时间窗覆盖不到）后再用 get_web_search 定向检索"
            "\n③ 仅当搜索结果命中权威源（财联社/证券时报/东财/新浪）且摘要不足时，用 get_article_content 读全文"
            "\n④ 基于以上信息撰写报告。注意区分官方消息与市场传闻，来源权重：财联社快讯 > 新华财经/证券时报 > 东方财富/同花顺。"
            "\n\n🔍 查询词构造技巧（Bing 中文分词敏感，直接影响结果质量）："
            "\n- 股票/公司名加英文双引号 + 年份：\"贵州茅台\" 2026 半年报（不加引号或去年份可能退化为地名/无关结果）"
            "\n- 用具体事件词而非泛词：\"宁德时代\" 欧盟 反补贴 优于 宁德时代 新闻"
            "\n- 避免与地名/人名歧义的高频词（如\"贵州\"\"宁德\"单独出现会当地名）"
            "\n- 结果跑偏或无结果时，换一组关键词重试"
            "\n\n⏱️ 轮次纪律（防止多轮失控）："
            "\n- get_web_search 调用不超过 10 次"
            "\n- get_article_content 调用不超过 10 篇"
            "\n- 达到上限后基于已有信息完成报告，不无限检索"
            "\n\n撰写全面的新闻分析报告，区分利好/利空/中性消息，评估影响程度和持续时间。报告末尾附 Markdown 表格汇总关键新闻事件及其影响评级。"
            "\n\n📋 必采清单 — 以下数据点必须出现在报告中，无法获取时标注 [数据缺失: xxx]："
            "\n1. 个股新闻条数和时间范围"
            "\n2. 宏观新闻条数和时间范围"
            "\n3. 关键事件时间线（至少列出 3 个重要事件及日期）"
            "\n4. 利好/利空/中性事件分类统计"
            "\n5. 风险事件清单（如有）"
            + get_language_instruction()
        )

        prompt = ChatPromptTemplate.from_messages(
            [
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
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([tool.name for tool in tools]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        messages = state["messages"]

        # 硬上限: 搜索/正文工具达上限后, 强制切换为无工具 LLM 调用生成最终报告。
        # 这样即使 LLM 一直想继续检索, 也绝不会出现"只记录搜索过程无分析结论"。
        news_tool_count = _count_news_tools(messages)
        if news_tool_count >= _NEWS_TOOL_CAP:
            logger.warning(
                "news_analyst: 搜索/正文工具达上限 %d 次, 强制生成最终报告",
                news_tool_count,
            )
            final_chain = prompt | llm
            cap_notice = (
                "\n\n[系统提示] 你的检索工具（get_web_search/get_article_content）"
                f"已使用满 {_NEWS_TOOL_CAP} 次上限。请立即停止检索，"
                "基于以上已获取的所有新闻/搜索信息，撰写完整的新闻分析报告"
                "（必须包含必采清单全部 5 项，无法覆盖的标注 [数据缺失]）。"
                "不要调用任何工具。"
            )
            final_messages = list(messages) + [
                {"role": "user", "content": cap_notice}
            ]
            result = final_chain.invoke(final_messages)
            report = result.content if result.content else ""
            if not report.strip():
                report = retry_report_generation(
                    llm, final_messages, result, "news_analyst"
                )
                # retry 用的是无工具 llm, 返回新消息避免残留 tool_calls 导致循环不停
                return {
                    "messages": [AIMessage(content=report)] if report.strip()
                               else [result],
                    "news_report": report,
                }
            return {
                "messages": [result],
                "news_report": report,
            }

        chain = prompt | llm.bind_tools(tools)
        result = chain.invoke(messages)

        report = ""

        if len(result.tool_calls) == 0:
            report = result.content if result.content else ""
            if not report.strip():
                report = retry_report_generation(
                    llm, messages, result, "news_analyst"
                )
        else:
            # LLM may return both tool_calls and content simultaneously.
            # Keep the content as a candidate report so it's not lost.
            report = result.content if result.content else ""
            tool_names = [tc.get("name", "?") for tc in result.tool_calls]
            logger.info("news_analyst: tool_calls=%s", tool_names)

        return {
            "messages": [result],
            "news_report": report,
        }

    return news_analyst_node
