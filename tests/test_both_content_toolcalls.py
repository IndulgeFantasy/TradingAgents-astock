"""Check if LLM returns both content and tool_calls simultaneously."""
import sys, os, logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_core.messages import HumanMessage, ToolMessage
from tradingagents.agents.utils.agent_utils import (
    build_instrument_context, get_indicators, get_language_instruction,
    get_market_context, get_stock_kline_full, get_stock_levels,
    get_chip_distribution, analyze_pattern,
)
from tradingagents.dataflows.config import get_config
from tradingagents.llm_clients.factory import create_llm_client
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

config = get_config()
provider = os.getenv("TEST_LLM_PROVIDER", "volcengine")
model = os.getenv("TEST_LLM_MODEL", "glm-5.2")
client = create_llm_client(provider=provider, model=model, base_url=config.get("backend_url"))
llm = client.get_llm()

tools = [get_stock_kline_full, get_indicators, get_market_context, get_stock_levels, get_chip_distribution, analyze_pattern]
tool_map = {t.name: t for t in tools}

ticker = "002797"
current_date = "2026-07-13"
instrument_context = build_instrument_context(ticker)

system_message = ("你是一位专注于 A 股市场的技术分析师。\n"
    "操作要求：\n"
    "1. 先调用 get_stock_kline_full(code, days) 获取 K 线数据\n"
    "2. 调用 get_market_context() 了解大盘环境\n"
    "3. 撰写详细的技术分析报告\n"
    + get_language_instruction())

prompt = ChatPromptTemplate.from_messages([
    ("system",
     "You are a helpful AI assistant. Use tools to progress. "
     "You have access to: {tool_names}.\n{system_message}"
     "Current date: {current_date}. {instrument_context}"),
    MessagesPlaceholder(variable_name="messages"),
])
prompt = prompt.partial(system_message=system_message)
prompt = prompt.partial(tool_names=", ".join([t.name for t in tools]))
prompt = prompt.partial(current_date=current_date)
prompt = prompt.partial(instrument_context=instrument_context)

chain = prompt | llm.bind_tools(tools)
messages = [HumanMessage(content=f"Analyze {ticker}")]

print("=== Round 1 ===")
result = chain.invoke(messages)
messages.append(result)
tc = result.tool_calls or []
content = result.content or ""
print(f"tool_calls: {len(tc)}, content_len: {len(content)}")
print(f"  HAS BOTH: {len(tc) > 0 and len(content) > 10}")

if tc:
    for t in tc:
        print(f"  -> {t['name']}({t['args']})")
        tr = tool_map[t['name']].invoke(t['args'])
        messages.append(ToolMessage(content=tr, tool_call_id=t['id'], name=t['name']))

print("\n=== Round 2 ===")
result = chain.invoke(messages)
messages.append(result)
tc = result.tool_calls or []
content = result.content or ""
print(f"tool_calls: {len(tc)}, content_len: {len(content)}")
print(f"  HAS BOTH: {len(tc) > 0 and len(content) > 10}")
if content:
    p = content[:200].encode('ascii','replace').decode('ascii')
    print(f"  content_preview: {p}")
if tc:
    for t in tc:
        print(f"  -> {t['name']}({t['args']})")
        tr = tool_map[t['name']].invoke(t['args'])
        messages.append(ToolMessage(content=tr, tool_call_id=t['id'], name=t['name']))

    print("\n=== Round 3 ===")
    result = chain.invoke(messages)
    messages.append(result)
    tc = result.tool_calls or []
    content = result.content or ""
    print(f"tool_calls: {len(tc)}, content_len: {len(content)}")
    print(f"  HAS BOTH: {len(tc) > 0 and len(content) > 10}")
    if content:
        p = content[:200].encode('ascii','replace').decode('ascii')
        print(f"  content_preview: {p}")
    if tc:
        for t in tc:
            print(f"  -> {t['name']}({t['args']})")
    elif not content.strip():
        print("  EMPTY CONTENT - testing retry...")
        from tradingagents.agents.utils.agent_utils import retry_report_generation
        report = retry_report_generation(llm, messages[:-1], result, "market_analyst")
        print(f"  Retry result: {len(report)} chars")
        if report:
            p = report[:300].encode('ascii','replace').decode('ascii')
            print(f"  Retry preview: {p}")
