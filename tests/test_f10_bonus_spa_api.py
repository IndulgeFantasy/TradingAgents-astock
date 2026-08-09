"""同花顺新版 F10 (astockpc) 分红融资页抓取测试。

背景：
- 新版 SPA 页面: https://basic.10jqka.com.cn/astockpc/astockmain/index.html#/bonus
  ?code={code}&marketid={marketid}&code_name={name}
- 页面为 JS 渲染，真实数据来自 basicapi REST 接口（JSON，可直接 HTTP 抓取）：
    * /basicapi/finance/dividends/v1/programme          分红方案历史
    * /basicapi/finance/dividends/v1/label              分红诊断标签
    * /basicapi/component/share/v1/share_info           股票基础信息（累计派现等）
    * /basicapi/finance/financing/v1/additional         增发概况+明细
    * /basicapi/finance/financing/v1/allotment          配股概况+明细
    * /basicapi/finance/financing/v1/org_allocated_detail  增发机构获配明细
    * /basicapi/fuyao/concept_upgrade/concept/v2/dividend_ratio  近三年分红比率
- marketid 推断规则（实测 13 只股票验证）：
    60/68(沪A+科创)→17, 90(沪B)→18, 00/30(深A)→33, 20(深B)→34, 8/4(北交所)→151

本文件不修改生产代码；直连用例标记 integration，断网自动 skip；
playwright 渲染用例在无 playwright 环境自动跳过。
"""

import json

import pytest

_BASE = "https://basic.10jqka.com.cn/basicapi"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    ),
    "Referer": "https://basic.10jqka.com.cn/astockpc/astockmain/index.html",
    "Accept": "application/json, text/plain, */*",
}


def infer_marketid(code: str) -> int:
    """按 6 位代码前缀推断同花顺 marketid（与新版 SPA URL 的 marketid 参数一致）。"""
    c = str(code).zfill(6)
    if c.startswith(("60", "68")):
        return 17
    if c.startswith("90"):
        return 18
    if c.startswith(("00", "30")):
        return 33
    if c.startswith("20"):
        return 34
    if c.startswith(("8", "4")):
        return 151
    return 17


def _spa_bonus_url(code: str, name: str = "") -> str:
    from urllib.parse import quote

    marketid = infer_marketid(code)
    q = f"code={code}&marketid={marketid}&code_name={quote(name or code)}"
    return f"https://basic.10jqka.com.cn/astockpc/astockmain/index.html#/bonus?{q}"


def _get_json(url: str, params: dict):
    import requests

    r = requests.get(url, params=params, headers=_HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# 1. 单元测试：marketid 推断（纯逻辑，无网络）
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_infer_marketid_rules():
    cases = {
        "600887": 17, "601398": 17, "600519": 17, "688111": 17, "688981": 17,
        "000001": 33, "002644": 33, "300750": 33, "300753": 33,
        "900901": 18, "200011": 34, "830799": 151, "430047": 151,
    }
    for code, expect in cases.items():
        assert infer_marketid(code) == expect, f"{code}: {infer_marketid(code)} != {expect}"


@pytest.mark.unit
def test_spa_bonus_url_builds_with_marketid():
    url = _spa_bonus_url("600887", "伊利股份")
    assert "#/bonus?" in url
    assert "code=600887" in url
    assert "marketid=17" in url
    assert "code_name=" in url


# ---------------------------------------------------------------------------
# 2. 集成测试：basicapi 直连抓取（网络不可达自动 skip）
# ---------------------------------------------------------------------------


@pytest.fixture()
def http_ok():
    import requests

    try:
        requests.get("https://basic.10jqka.com.cn/xml/defaultWord.json", headers=_HEADERS, timeout=8)
    except Exception as e:
        pytest.skip(f"网络不可达: {e}")
    return True


@pytest.mark.integration
def test_dividend_programme_has_history_and_totals(http_ok):
    d = _get_json(f"{_BASE}/finance/dividends/v1/programme",
                  {"code": "600887", "market": "17", "showDividend": 0, "size": 0, "page": 1})
    assert d.get("status_code") == 0, d.get("status_msg")
    data = d["data"]
    assert float(data["stock_cash_dividend"]) > 0  # 上市后累计派现(元)
    page_result = data["page_result"]
    assert page_result["total"] > 0
    items = page_result["data"]
    assert items, "分红历史为空"
    first = items[0]
    for key in ("date", "dividend_plan", "board_date", "ex_dividend_date",
                "stock_dividend_total", "payment_rate", "progress_name"):
        assert key in first, f"缺字段 {key}"


@pytest.mark.integration
def test_dividend_label_has_diagnostics(http_ok):
    d = _get_json(f"{_BASE}/finance/dividends/v1/label",
                  {"code": "600887", "market": "17", "type": "stock", "platform": "client"})
    assert d.get("status_code") == 0
    labels = d.get("data") or []
    assert labels, "分红诊断标签为空"
    text = json.dumps(labels, ensure_ascii=False)
    assert any(k in text for k in ("每股", "派现", "送转", "资本公积"))


@pytest.mark.integration
def test_share_info_has_ths_code(http_ok):
    d = _get_json(f"{_BASE}/component/share/v1/share_info",
                  {"code": "600887", "market": "17", "type": "stock"})
    assert d.get("status_code") == 0
    data = d["data"]
    assert data["code"] == "600887"
    assert data["ths_code"] == "600887.SH"


@pytest.mark.integration
def test_financing_additional_has_issuances(http_ok):
    d = _get_json(f"{_BASE}/finance/financing/v1/additional",
                  {"code": "600887", "market": "17", "type": "stock"})
    assert d.get("status_code") == 0
    data = d["data"]
    assert int(data["additional_statistics"]["issue_num"]) > 0
    details = data["additional_details"]
    assert details, "增发明细为空"
    assert "date" in details[0] and "price" in details[0]


@pytest.mark.integration
def test_financing_allotment_has_records(http_ok):
    d = _get_json(f"{_BASE}/finance/financing/v1/allotment",
                  {"code": "600887", "market": "17", "type": "stock"})
    assert d.get("status_code") == 0
    data = d["data"]
    assert int(data["allotment_statistics"]["issue_num"]) > 0
    assert data["allotment_details"]


@pytest.mark.integration
def test_financing_org_allocated_detail_has_orgs(http_ok):
    d = _get_json(f"{_BASE}/finance/financing/v1/org_allocated_detail",
                  {"code": "600887", "size": 10, "market": "17", "page": 1, "type": "stock"})
    assert d.get("status_code") == 0
    data = d["data"]
    stats = data["allocated_statistics"]
    assert int(stats["allocated_org_num"]) > 0
    detail = data["allocated_detail"]
    assert detail["total"] > 0
    assert detail["data"], "获配机构明细为空"
    assert "org_name" in detail["data"][0]


@pytest.mark.integration
def test_dividend_ratio_api(http_ok):
    d = _get_json("https://basic.10jqka.com.cn/fuyao/concept_upgrade/concept/v2/dividend_ratio",
                  {"code": "600887", "market": "17"})
    assert d.get("status_code") == 0
    data = d["data"]
    assert float(data["divided_result"]) > 0  # 近三年分红比率
    assert float(data["accumulated_cash_dividend"]) > 0


@pytest.mark.integration
def test_marketid_rules_applied_to_deep_market(http_ok):
    """深市代码（marketid=33）接口同样可抓。"""
    d = _get_json(f"{_BASE}/finance/dividends/v1/programme",
                  {"code": "300753", "market": "33", "showDividend": 0, "size": 0, "page": 1})
    assert d.get("status_code") == 0
    assert d["data"]["page_result"]["total"] > 0


# ---------------------------------------------------------------------------
# 3. playwright 渲染兜底：SPA 页面真实加载 + 拦截分红数据响应
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_playwright_service_stock_dividend_endpoint():
    """playwright_service /api/stock-dividend 端点（需服务运行，未启动自动 skip）。"""
    import requests

    try:
        routes = requests.get("http://127.0.0.1:8765/api/routes", timeout=5).json()
    except Exception as e:
        pytest.skip(f"playwright_service 未运行: {e}")
    paths = {r["path"] for r in routes.get("routes", [])}
    if "/api/stock-dividend" not in paths:
        pytest.skip("服务未加载 /api/stock-dividend（需重启 playwright_service）")

    d = requests.get("http://127.0.0.1:8765/api/stock-dividend",
                     params={"code": "600887"}, timeout=120).json()
    assert d.get("success"), d.get("error")
    data = d["data"]
    assert data["marketid"] == "17"
    # 分红方案历史
    prog = data["programme"]["data"]
    assert float(prog["stock_cash_dividend"]) > 0
    assert prog["page_result"]["total"] > 0
    assert prog["page_result"]["data"][0]["dividend_plan"]
    # 增发/配股/获配
    assert data["additional"]["data"]["additional_statistics"]["issue_num"]
    assert data["org_allocated_detail"]["data"]["allocated_detail"]["total"] > 0
    # marketid 自动推断（深市）
    d2 = requests.get("http://127.0.0.1:8765/api/stock-dividend",
                      params={"code": "300753"}, timeout=120).json()
    assert d2["data"]["marketid"] == "33"
    assert d2["data"]["programme"]["data"]["page_result"]["total"] > 0


@pytest.mark.integration
def test_playwright_service_marketid_sensitive_fields():
    """marketid 敏感回归：share_info/dividend_ratio 校验 marketid（深市必须 33）。

    历史教训: _EM_MARKET_IDS({0:22, 3:23}) 无官方依据，导致深市股票
    dividend_ratio 为空、页面分红诊断模块消失；正确值来自旧版 F10 #marketId
    （000001 等深市实测 = 33）。
    """
    import requests

    try:
        requests.get("http://127.0.0.1:8765/api/routes", timeout=5)
    except Exception as e:
        pytest.skip(f"playwright_service 未运行: {e}")

    # 深市股票: share_info 必须返回 market_id=33（错误值会返回空）
    d = requests.get("http://127.0.0.1:8765/api/stock-dividend",
                     params={"code": "000001"}, timeout=120).json()
    assert d.get("success"), d.get("error")
    si = d["data"]["share_info"]["data"]
    assert si["market_id"] == "33", f"深市 market_id 应为 33，实际 {si.get('market_id')}"
    assert si["name"] == "平安银行"
    # dividend_ratio 有数据（错误 marketid 时为空）
    ratio = d["data"]["dividend_ratio"]["data"]
    assert ratio["divided_result"] is not None, "dividend_ratio 为空说明 marketid 推断错误"

    # 沪市股票: market_id=17
    d2 = requests.get("http://127.0.0.1:8765/api/stock-dividend",
                      params={"code": "600887"}, timeout=120).json()
    assert d2["data"]["share_info"]["data"]["market_id"] == "17"
    assert d2["data"]["dividend_ratio"]["data"]["divided_result"] is not None


@pytest.mark.integration
def test_playwright_service_stock_position_endpoint():
    """position 端点（主力持仓）marketid 修复回归：深市股票数据完整。"""
    import requests

    try:
        requests.get("http://127.0.0.1:8765/api/routes", timeout=5)
    except Exception as e:
        pytest.skip(f"playwright_service 未运行: {e}")
    d = requests.get("http://127.0.0.1:8765/api/stock-position",
                     params={"code": "000001"}, timeout=120).json()
    assert d.get("success"), d.get("error")
    data = d["data"]
    assert data["institutionSummary"], "机构持股汇总为空"
    assert data["institutionDetail"], "机构持股明细为空"


@pytest.mark.integration
def test_playwright_spa_bonus_renders_and_intercepts():
    pytest.importorskip("playwright")
    import asyncio

    captured = {}

    async def _run():
        from playwright.async_api import async_playwright

        url = _spa_bonus_url("600887", "伊利股份")
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
            try:
                ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await ctx.new_page()
                await page.set_viewport_size({"width": 1280, "height": 900})

                async def on_response(resp):
                    if "basicapi/finance/dividends/v1/programme" in resp.url:
                        try:
                            captured["programme"] = await resp.text()
                        except Exception:
                            pass

                page.on("response", on_response)
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(5)
                text = await page.evaluate("() => document.body.innerText")
                await browser.close()
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

    # 页面渲染出分红模块与历史行
    assert "分红" in text
    assert "分红总额" in text or "分红方案" in text
    assert "增发" in text and "配股" in text
    # 数据接口被拦截到且含分红历史 JSON
    assert "programme" in captured, "未拦截到 dividends/v1/programme 响应"
    body = captured["programme"]
    assert '"status_code":0' in body
    assert '"stock_cash_dividend"' in body
