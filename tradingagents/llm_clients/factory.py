from typing import Optional
import os

from .base_client import BaseLLMClient

# Providers that use the OpenAI-compatible chat completions API.
# "openai_compatible" is the generic pass-through for any relay/gateway that
# speaks the OpenAI Chat Completions API (9Router, AI Router, self-hosted
# proxies, …): the user supplies base_url + model + a generic API key, with no
# hard-coded vendor defaults (#77 / #81).
_OPENAI_COMPATIBLE = (
    "openai", "xai", "deepseek", "qwen", "glm", "ollama", "openrouter", "minimax",
    "volcengine", "opencodego", "openai_compatible",
)

# 全局默认单次回复输出上限（max output tokens）。模型可能只支持更小值，
# 但 32000 足够覆盖完整分析报告，避免"报告写到一半被掐断"（#91）。
# 覆盖优先级：显式传入的 max_tokens > 环境变量 TRADINGAGENTS_MAX_TOKENS > 本默认值。
_DEFAULT_MAX_TOKENS = 32000


def _resolve_default_max_tokens() -> int:
    """读取环境变量 TRADINGAGENTS_MAX_TOKENS，非法值回退到全局默认。"""
    env_mt = os.environ.get("TRADINGAGENTS_MAX_TOKENS")
    if env_mt:
        try:
            return int(env_mt)
        except ValueError:
            pass
    return _DEFAULT_MAX_TOKENS


def create_llm_client(
    provider: str,
    model: str,
    base_url: Optional[str] = None,
    **kwargs,
) -> BaseLLMClient:
    """Create an LLM client for the specified provider.

    Provider modules are imported lazily so that simply importing this
    factory (e.g. during test collection) does not pull in heavy LLM SDKs
    or fail when their API keys are absent.

    Args:
        provider: LLM provider name
        model: Model name/identifier
        base_url: Optional base URL for API endpoint
        **kwargs: Additional provider-specific arguments

    Returns:
        Configured BaseLLMClient instance

    Raises:
        ValueError: If provider is not supported
    """
    provider_lower = provider.lower()

    # 统一注入 max_tokens 默认值（显式传入的优先，setdefault 不覆盖）。
    # 没有它：anthropic 通道第三方模型被砍到 8192，OpenAI 兼容通道用 SDK 默认，
    # 报告长一点就被静默截断。
    kwargs.setdefault("max_tokens", _resolve_default_max_tokens())

    if provider_lower in _OPENAI_COMPATIBLE:
        from .openai_client import OpenAIClient
        return OpenAIClient(model, base_url, provider=provider_lower, **kwargs)

    if provider_lower == "anthropic":
        from .anthropic_client import AnthropicClient
        return AnthropicClient(model, base_url, **kwargs)

    if provider_lower == "claude_agent_sdk":
        from .claude_agent_sdk_client import ClaudeAgentSDKClient
        return ClaudeAgentSDKClient(model, base_url, **kwargs)

    if provider_lower == "google":
        from .google_client import GoogleClient
        return GoogleClient(model, base_url, **kwargs)

    if provider_lower == "azure":
        from .azure_client import AzureOpenAIClient
        return AzureOpenAIClient(model, base_url, **kwargs)

    raise ValueError(f"Unsupported LLM provider: {provider}")
