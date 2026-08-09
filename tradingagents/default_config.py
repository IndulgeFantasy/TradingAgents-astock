import os

_TRADINGAGENTS_HOME = os.path.join(os.path.expanduser("~"), ".tradingagents")

DEFAULT_CONFIG = {
    "project_dir": os.path.abspath(os.path.join(os.path.dirname(__file__), ".")),
    "results_dir": os.getenv("TRADINGAGENTS_RESULTS_DIR", os.path.join(_TRADINGAGENTS_HOME, "logs")),
    "data_cache_dir": os.getenv("TRADINGAGENTS_CACHE_DIR", os.path.join(_TRADINGAGENTS_HOME, "cache")),
    "memory_log_path": os.getenv("TRADINGAGENTS_MEMORY_LOG_PATH", os.path.join(_TRADINGAGENTS_HOME, "memory", "trading_memory.md")),
    # Optional cap on the number of resolved memory log entries. When set,
    # the oldest resolved entries are pruned once this limit is exceeded.
    # Pending entries are never pruned. None disables rotation entirely.
    "memory_log_max_entries": None,
    # LLM settings
    "llm_provider": "openai",
    "deep_think_llm": "gpt-5.4",
    "quick_think_llm": "gpt-5.4-mini",
    # When None, each provider's client falls back to its own default endpoint
    # (api.openai.com for OpenAI, generativelanguage.googleapis.com for Gemini, ...).
    # The CLI overrides this per provider when the user picks one. Keeping a
    # provider-specific URL here would leak (e.g. OpenAI's /v1 was previously
    # being forwarded to Gemini, producing malformed request URLs).
    "backend_url": None,
    # 网页搜索引擎: "quark"(夸克 AI, 结构化总结+资讯) | "bing"(Bing cn, SERP 列表)
    "search_engine": "quark",
    # Provider-specific thinking configuration
    "google_thinking_level": None,      # "high", "minimal", etc.
    "openai_reasoning_effort": None,    # "medium", "high", "low"
    "anthropic_effort": None,           # "high", "medium", "low"
    # ── Claude Agent SDK provider（走个人 Pro/Max 订阅额度，可选依赖 [agentsdk]）──
    # 与内置 anthropic provider 的区别：anthropic 走 ANTHROPIC_API_KEY = **按 token 计费**；
    # 本 provider 走本机已登录的 claude CLI = **消耗订阅额度，不产生 API 账单**。
    # 设为 "claude_agent_sdk" 时，仅 deep_thinking_llm 节点（Research Manager /
    # Portfolio Manager）走订阅。None = 维持原行为。
    "deep_think_provider_override": None,
    # 同上，作用于 QUICK 节点（7 个工具分析师 + 多空/交易员/风险辩手）。
    # 与上一项同时开启 = 全节点走订阅。None = 分析师仍走 llm_provider（维持原行为）。
    "quick_think_provider_override": None,
    # Agent SDK 使用的 Claude 模型。**必须是真实 Claude 模型**，不要复用 deep_think_llm。
    # 用别名而非写死版本号：claude CLI 的 "opus"/"sonnet" 恒指向最新模型，
    # 写死 "claude-opus-4-8" 这类 ID 会随版本迭代过期。
    "agent_sdk_model": "opus",
    # QUICK/分析师节点用的 Claude 模型。默认 sonnet 而非 opus——quick 节点数量多
    # （7 分析师 + 辩手），订阅是按额度限流的，全用 opus 很快会撞到上限。
    "agent_sdk_quick_model": "sonnet",
    # 订阅调用失败 / 撞额度时的兜底。None → 回落到 llm_provider + deep_think_llm。
    "agent_sdk_fallback_provider": None,
    "agent_sdk_fallback_model": None,
    # Checkpoint/resume: when True, LangGraph saves state after each node
    # so a crashed run can resume from the last successful step.
    "checkpoint_enabled": False,
    # Output language for analyst reports and final decision
    # Internal agent debate stays in English for reasoning quality
    "output_language": "Chinese",
    # How many days of price/indicator history the market analyst covers
    # (the "analysis window", ending at the analysis date). Drives the
    # look_back_days the market analyst passes to get_stock_data /
    # get_indicators. The Web sidebar / CLI derive this from a user-picked
    # start date (default: first day of the current month → "monthly" view);
    # None keeps the previous behaviour (the model's own default, ~30). (#16)
    "market_lookback_days": None,
    # Debate and discussion settings
    "max_debate_rounds": 1,
    "max_risk_discuss_rounds": 1,
    "max_recur_limit": 150,
    # Data vendor configuration
    # Category-level configuration (default for all tools in category)
    "data_vendors": {
        "core_stock_apis": "a_stock",        # Options: a_stock, alpha_vantage, yfinance
        "technical_indicators": "a_stock",   # Options: a_stock, alpha_vantage, yfinance
        "fundamental_data": "a_stock",       # Options: a_stock, alpha_vantage, yfinance
        "news_data": "a_stock",              # Options: a_stock, alpha_vantage, yfinance
        "signal_data": "a_stock",            # A-stock only: topic attribution, capital flow, consensus
    },
    # Tool-level configuration (takes precedence over category-level)
    "tool_vendors": {
        # Example: "get_stock_data": "alpha_vantage",  # Override category default
        "get_fund_flow": "playwright",  # 问财版含 DDE 散户数量趋势
        "get_profit_forecast": "playwright",  # 同花顺F10详细版（轻量直连HTTP易被反爬）
        "get_concept_blocks": "playwright",  # 问财版含三级行业分类+同花顺概念
    },
}
