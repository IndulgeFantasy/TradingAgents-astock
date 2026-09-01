"""get_profit_forecast_playwright 双参签名回归测试。

背景（2026-08 事故）:
- signal_data_tools.get_profit_forecast(ticker, curr_date) → route_to_vendor 以双参
  调用 vendor 实现；tool_vendors 默认 get_profit_forecast → "playwright"。
- 但 get_profit_forecast_playwright 只声明 (ticker) 单参 → 默认配置下每次调用
  都抛 `TypeError: takes 1 positional argument but 2 were given`，
  导致 fundamentals 分析师整个失败（任务 tools_fundamentals 中断）。
- 修复: playwright 实现补 curr_date 参数（与 a_stock 实现一致），
  历史日期时正文顶部加未来函数告警（与 a_stock 行为一致）。

运行环境: 需要 langchain_core（缺失时自动跳过，base env 即如此）。
"""


def _make_fake_client():
    class FakeClient:
        def eps_forecast(self, ticker):
            return {
                "success": True,
                "data": {
                    "stock_name": "贵州茅台",
                    "institution_count": 30,
                    "summary_text": "盈利预测摘要",
                    "eps_summary": [],  # 空 → 跳过 _tencent_quote 分支
                    "np_summary": [],
                    "institution_forecasts": [],
                    "indicators": [],
                    "research_summaries": [],
                    "rating_distribution": [],
                },
            }

    return FakeClient()


def test_playwright_impl_accepts_two_positional_args(monkeypatch):
    """双参调用（ticker, curr_date）不应再抛 TypeError。"""
    pytest_import = __import__("pytest")
    pytest_import.importorskip("langchain_core")
    from tradingagents.agents.utils import playwright_tools

    monkeypatch.setattr(playwright_tools, "_get_client", lambda: _make_fake_client())
    out = playwright_tools.get_profit_forecast_playwright("600519", "2026-08-09")
    assert "机构盈利预测" in out
    assert "贵州茅台" in out
    assert "未来函数" not in out


def test_playwright_impl_historical_date_adds_future_notice(monkeypatch):
    """复盘历史日期时必须带未来函数告警（与 a_stock 实现一致）。"""
    pytest_import = __import__("pytest")
    pytest_import.importorskip("langchain_core")
    from tradingagents.agents.utils import playwright_tools

    monkeypatch.setattr(playwright_tools, "_get_client", lambda: _make_fake_client())
    out = playwright_tools.get_profit_forecast_playwright("600519", "2026-07-01")
    assert "未来函数" in out
    assert "2026-07-01" in out
    assert "机构盈利预测" in out


def test_playwright_impl_accepts_single_arg_for_backward_compat(monkeypatch):
    """仅传 ticker（无 curr_date）仍可用。"""
    pytest_import = __import__("pytest")
    pytest_import.importorskip("langchain_core")
    from tradingagents.agents.utils import playwright_tools

    monkeypatch.setattr(playwright_tools, "_get_client", lambda: _make_fake_client())
    out = playwright_tools.get_profit_forecast_playwright("600519")
    assert "机构盈利预测" in out
    assert "未来函数" not in out
