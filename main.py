from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Create a custom config
config = DEFAULT_CONFIG.copy()
config["llm_provider"] = "openai"
config["deep_think_llm"] = "glm-5.2"
config["quick_think_llm"] = "glm-5.2"
config["max_debate_rounds"] = 1

# Configure data vendors (A-stock direct HTTP APIs, no yfinance dependency)
config["data_vendors"] = {
    "core_stock_apis": "a_stock",
    "technical_indicators": "a_stock",
    "fundamental_data": "a_stock",
    "news_data": "a_stock",
}

# Initialize with custom config
ta = TradingAgentsGraph(debug=True, config=config)

# forward propagate
_, decision = ta.propagate("600519", "2024-05-10")
print(decision)

# Memorize mistakes and reflect
# ta.reflect_and_remember(1000) # parameter is the position returns
