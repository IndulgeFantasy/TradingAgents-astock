"""同花顺 F10 astockpc #/concept 概念板块明细接口测试。

背景：
- 页面: https://basic.10jqka.com.cn/astockpc/astockmain/index.html#/concept
  ?code={code}&marketid={marketid}&code_name={name}
- 问财通道概念数据不稳定（fetch_concept_blocks_wencai 常返回空），
  已切换为 F10 概念题材页（playwright 渲染提取 detail-item 结构）：
  概念名称/标签(走势最相关等)/龙头股列表/概念解析
- marketid 规则与分红接口一致（_infer_marketid: 60/68→17, 00/30→33 等）
- playwright_service 端点: /api/stock-concept
- LLM 工具: get_concept_blocks_playwright（get_concept_blocks 的 playwright vendor，
  实现已切换数据源，工具名/注册不变）

测试约定：
- 端点用例需 playwright_service 运行且为新代码（未运行/未加载自动 skip）
- playwright 渲染用例在无 playwright 环境自动跳过
"""

import pytest

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    ),
    "Referer": "https://basic.10jqka.com.cn/",
}


def _concept_url(code: str, marketid: int) -> str:
    return (
        "https://basic.10jqka.com.cn/astockpc/astockmain/index.html"
        f"#/concept?code={code}&marketid={marketid}&code_name="
    )


@pytest.mark.integration
def test_playwright_service_stock_concept_endpoint():
    """playwright_service /api/stock-concept 端点（需服务运行且为新代码，否则 skip）。"""
    import requests

    try:
        routes = requests.get("http://127.0.0.1:8765/api/routes", timeout=5).json()
    except Exception as e:
        pytest.skip(f"playwright_service 未运行: {e}")
    paths = {r["path"] for r in routes.get("routes", [])}
    if "/api/stock-concept" not in paths:
        pytest.skip("服务未加载 /api/stock-concept（需重启 playwright_service 加载新代码）")

    d = requests.get("http://127.0.0.1:8765/api/stock-concept",
                     params={"code": "600887"}, timeout=120).json()
    assert d.get("success"), d.get("error")
    data = d["data"]
    assert data["marketid"] == "17"
    concepts = data["concepts"]
    assert concepts, "概念列表为空"
    first = concepts[0]
    assert first["name"], "概念名为空"
    assert first["leading_stocks"], "龙头股为空"
    assert first["analysis"], "概念解析为空"
    # 上涨周期（概念组合走势与驱动逻辑）
    rise = data.get("rise_cycles") or []
    assert rise, "上涨周期为空"
    rc = rise[0]
    assert rc["concepts"], "上涨周期概念组合为空"
    assert rc["metrics"].get("区间涨幅"), "上涨周期区间涨幅为空"
    assert rc["interpretation"], "上涨周期驱动逻辑为空"

    # marketid 自动推断（深市）
    d2 = requests.get("http://127.0.0.1:8765/api/stock-concept",
                      params={"code": "300750"}, timeout=120).json()
    assert d2["data"]["marketid"] == "33"
    assert d2["data"]["concepts"]


@pytest.mark.integration
def test_playwright_spa_concept_renders_and_extracts():
    """playwright 渲染 #/concept 页并提取概念明细（无 playwright 环境自动跳过）。"""
    pytest.importorskip("playwright")
    import asyncio

    async def _run():
        from playwright.async_api import async_playwright

        url = _concept_url("600887", 17)
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
            try:
                ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await ctx.new_page()
                await page.set_viewport_size({"width": 1280, "height": 900})
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                ok = False
                for _ in range(10):
                    await asyncio.sleep(1.5)
                    n = await page.evaluate(
                        "() => document.querySelectorAll('#concept_related .detail-item').length")
                    if n > 0:
                        ok = True
                        break
                concepts = await page.evaluate(
                    """() => {
                        const items = document.querySelectorAll('#concept_related .detail-item');
                        const out = [];
                        for (const it of items) {
                            const name = (it.querySelector('.name-text')?.textContent || '').trim();
                            const tag = (it.querySelector('.tag')?.textContent || '').trim();
                            const stocks = Array.from(it.querySelectorAll('.leading-stocks .stock-name'))
                                .map(s => (s.textContent || '').trim()).filter(Boolean);
                            const analysis = (it.querySelector('.analysis-text')?.textContent || '').trim();
                            if (name) out.push({name, tag, leading_stocks: stocks, analysis});
                        }
                        return out;
                    }"""
                )
                # 上涨周期
                await page.evaluate(
                    """() => {
                        const els = document.querySelectorAll('.sub-nav-item');
                        for (const el of els) {
                            if ((el.textContent || '').trim() === '上涨周期') { el.click(); return; }
                        }
                    }"""
                )
                rise_ok = False
                for _ in range(8):
                    await asyncio.sleep(1.5)
                    if await page.evaluate("() => !!document.querySelector('#concept_cycle .data-details')"):
                        rise_ok = True
                        break
                rise = await page.evaluate(
                    """() => {
                        const out = [];
                        for (const d of document.querySelectorAll('#concept_cycle .data-details')) {
                            const tags = Array.from(d.querySelectorAll('.concept-tags .tag'))
                                .map(t => (t.textContent || '').trim()).filter(Boolean);
                            const metrics = {};
                            for (const item of d.querySelectorAll('.metric-item')) {
                                const label = (item.querySelector('.metric-label')?.textContent || '').trim();
                                const value = (item.querySelector('.metric-value')?.textContent || '').trim();
                                if (label && value) metrics[label] = value;
                            }
                            const interpretation = (d.querySelector('.ai-content')?.textContent || '').trim();
                            out.push({concepts: tags, metrics, interpretation});
                        }
                        return out;
                    }"""
                )
                return ok, concepts, rise_ok, rise
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass

    try:
        ok, concepts, rise_ok, rise = asyncio.run(_run())
    except Exception as e:
        pytest.skip(f"Chrome CDP 不可用: {e}")

    assert ok, "concept 页未渲染出 detail-item"
    assert len(concepts) >= 5, f"概念数过少: {len(concepts)}"
    first = concepts[0]
    assert first["name"] and first["leading_stocks"] and first["analysis"]
    names = [c["name"] for c in concepts]
    assert len(names) == len(set(names)), "概念名重复"
    assert rise_ok, "上涨周期标签未渲染"
    assert rise, "上涨周期数据为空"
    assert rise[0]["metrics"].get("区间涨幅"), "上涨周期缺区间涨幅"
