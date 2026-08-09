"""同花顺 F10 news.html 新闻公告接口测试。

背景：
- 页面: https://basic.10jqka.com.cn/{code}/news.html（JS 渲染）
- 新闻列表走 basicapi REST 接口: /basicapi/notice/news?type=stock&code=&current=&limit=
  （直连可抓；公告/研报无独立接口，研报为页面渲染表格）
- playwright_service 端点: /api/stock-news-f10（渲染 news.html + 拦截 notice/news + 抓研报表）
- LLM 工具: get_f10_news（news_analyst 绑定）

测试约定：
- 直连用例标记 integration，断网自动 skip
- 端点用例需 playwright_service 运行（未运行 skip）
- playwright 渲染用例在无 playwright 环境自动跳过
"""

import pytest

_BASE = "https://basic.10jqka.com.cn/basicapi"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    ),
    "Referer": "https://basic.10jqka.com.cn/600887/news.html",
    "Accept": "application/json, text/plain, */*",
}


@pytest.fixture()
def http_ok():
    import requests

    try:
        requests.get("https://basic.10jqka.com.cn/xml/defaultWord.json", headers=_HEADERS, timeout=8)
    except Exception as e:
        pytest.skip(f"网络不可达: {e}")
    return True


@pytest.mark.integration
def test_notice_news_api_has_news_items(http_ok):
    """basicapi/notice/news 直连：返回新闻列表（标题/日期/来源/链接）。"""
    import requests

    d = requests.get(
        f"{_BASE}/notice/news",
        params={"type": "stock", "code": "600887", "current": 1, "limit": 15},
        headers=_HEADERS,
        timeout=15,
    ).json()
    assert d.get("status_code") == 0
    data = d["data"]
    assert data["total"] > 0
    items = data["data"]
    assert items, "新闻列表为空"
    first = items[0]
    for key in ("seq", "title", "date", "source", "pc_url"):
        assert key in first, f"缺字段 {key}"
    assert first["title"], "新闻标题为空"
    assert first["pc_url"].startswith("http"), "新闻链接异常"


@pytest.mark.integration
def test_notice_news_api_works_for_deep_market(http_ok):
    """深市股票同样可抓。"""
    import requests

    d = requests.get(
        f"{_BASE}/notice/news",
        params={"type": "stock", "code": "300753", "current": 1, "limit": 5},
        headers=_HEADERS,
        timeout=15,
    ).json()
    assert d.get("status_code") == 0
    assert d["data"]["data"], "深市新闻列表为空"


@pytest.mark.integration
def test_playwright_service_stock_news_f10_endpoint():
    """playwright_service /api/stock-news-f10 端点（需服务运行且为新代码，否则 skip）。"""
    import requests

    try:
        routes = requests.get("http://127.0.0.1:8765/api/routes", timeout=5).json()
    except Exception as e:
        pytest.skip(f"playwright_service 未运行: {e}")
    paths = {r["path"] for r in routes.get("routes", [])}
    if "/api/stock-news-f10" not in paths:
        pytest.skip("服务未加载 /api/stock-news-f10（需重启 playwright_service 加载新代码）")

    d = requests.get("http://127.0.0.1:8765/api/stock-news-f10",
                     params={"code": "600887", "limit": 10}, timeout=120).json()
    assert d.get("success"), d.get("error")
    data = d["data"]
    assert data["code"] == "600887"
    assert data["total"] > 0
    news = data["news"]
    assert news, "新闻列表为空"
    assert news[0]["title"], "新闻标题为空"
    assert news[0]["url"], "新闻链接为空"


@pytest.mark.integration
def test_playwright_spa_news_renders_and_intercepts():
    """playwright 渲染 news.html + 拦截 notice/news 接口（无 playwright 环境自动跳过）。"""
    pytest.importorskip("playwright")
    import asyncio

    captured = {}

    async def _run():
        from playwright.async_api import async_playwright

        url = "https://basic.10jqka.com.cn/600887/news.html"
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
            try:
                ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await ctx.new_page()
                await page.set_viewport_size({"width": 1280, "height": 900})

                async def on_response(resp):
                    if "basicapi/notice/news" in resp.url:
                        try:
                            captured["news"] = await resp.text()
                        except Exception:
                            pass

                page.on("response", on_response)
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(6)
                text = await page.evaluate("() => document.body.innerText")
                return text
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass

    try:
        text = asyncio.run(_run())
    except Exception as e:
        pytest.skip(f"Chrome CDP 不可用: {e}")

    assert "公告列表" in text or "研报列表" in text
    assert "news" in captured, "未拦截到 notice/news 响应"
    body = captured["news"]
    assert '"status_code":0' in body
    assert '"title"' in body
