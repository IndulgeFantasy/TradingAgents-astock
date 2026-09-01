"""
Playwright 数据服务客户端
========================
通过 HTTP 调用 worktrade2 环境中的 playwright 数据服务。

用法:
    from playwright_service.client import PlaywrightClient

    client = PlaywrightClient()  # 默认 http://127.0.0.1:8765

    # 股本结构
    result = client.stock_basic("600519")

    # 大盘概览
    result = client.market_overview()

    # EPS预测
    result = client.eps_forecast("600519")
"""

import json
import os
import time
import threading
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

DEFAULT_BASE_URL = os.getenv("AKS_BASE_URL", "http://127.0.0.1:8765")
DEFAULT_TIMEOUT = int(os.getenv("AKS_TIMEOUT", "30"))

# Circuit breaker: after this many consecutive failures, short-circuit
# all requests for CIRCUIT_COOLDOWN seconds to avoid multi-minute hangs
# when the server is down.
CIRCUIT_FAILURE_THRESHOLD = int(os.getenv("AKS_CIRCUIT_THRESHOLD", "8"))
CIRCUIT_COOLDOWN = int(os.getenv("AKS_CIRCUIT_COOLDOWN", "60"))

# Min interval between requests to playwright_service (seconds).
# Each request opens/closes a Chrome page via CDP, too fast will crash Chrome.
PW_MIN_INTERVAL = float(os.getenv("PW_MIN_INTERVAL", "1.0"))


class PlaywrightClient:
    """Playwright 数据服务客户端"""

    def __init__(self, base_url: str = None, timeout: int = None):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout or DEFAULT_TIMEOUT
        self._fail_count = 0
        self._circuit_open_until = 0.0
        self._lock = threading.Lock()
        self._last_call = 0.0

    def _is_circuit_open(self) -> bool:
        with self._lock:
            return time.time() < self._circuit_open_until

    def _on_success(self):
        with self._lock:
            self._fail_count = 0
            self._circuit_open_until = 0.0

    def _on_failure(self):
        with self._lock:
            self._fail_count += 1
            if self._fail_count >= CIRCUIT_FAILURE_THRESHOLD:
                self._circuit_open_until = time.time() + CIRCUIT_COOLDOWN
                count = self._fail_count
        if self._fail_count >= CIRCUIT_FAILURE_THRESHOLD:
            print(
                f"[Playwright] 连续 {count} 次失败，"
                f"熔断 {CIRCUIT_COOLDOWN}s（期间跳过 HTTP 调用）"
            )

    def _get(self, path: str, params: dict = None, timeout: int = None) -> dict:
        """Send GET request with throttling to protect Chrome CDP."""
        # Throttle: enforce min interval between requests
        with self._lock:
            elapsed = time.time() - self._last_call
            if elapsed < PW_MIN_INTERVAL:
                time.sleep(PW_MIN_INTERVAL - elapsed)
            self._last_call = time.time()

        if self._is_circuit_open():
            with self._lock:
                remaining = int(self._circuit_open_until - time.time())
                fc = self._fail_count
            return {
                "success": False,
                "error": "服务不可用（熔断中）",
                "hint": f"连续失败 {fc} 次，等待 {remaining}s 后重试",
            }

        url = f"{self.base_url}{path}"
        if params:
            qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
            if qs:
                url = f"{url}?{qs}"

        req = Request(url, headers={"Accept": "application/json"})
        req_timeout = timeout if timeout is not None else self.timeout
        try:
            with urlopen(req, timeout=req_timeout) as resp:
                body = resp.read().decode("utf-8")
                try:
                    result = json.loads(body)
                except json.JSONDecodeError:
                    # Server returned a non-JSON body (e.g. proxy error page).
                    # This is NOT a transport failure - the server responded,
                    # just with unexpected content. Don't count toward circuit breaker.
                    return {"success": False, "error": f"非JSON响应: {body[:200]}"}
                # Only connection-level failures (exceptions below) count toward
                # the circuit breaker. A 200 OK with success=False means the
                # server is healthy but returned no data (e.g. new stock, no
                # report yet) - that's an application-level response, not a
                # transport failure. Counting it would falsely trip the breaker
                # when batching stocks that happen to have no data.
                if isinstance(result, dict) and result.get("success"):
                    self._on_success()
                return result
        except HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            self._on_failure()
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                return {"success": False, "error": f"HTTP {e.code}: {body[:200]}",
                        "http_status": e.code}
        except URLError as e:
            self._on_failure()
            return {"success": False, "error": f"连接失败: {e.reason}",
                    "hint": "请确保 playwright 服务已启动 (conda activate worktrade2 && python playwright_service/server.py)"}
        except Exception as e:
            self._on_failure()
            return {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}

    # ── 健康检查 ──
    def health(self) -> dict:
        """检查服务是否运行（短超时，不阻塞）"""
        return self._get("/api/health", timeout=min(self.timeout, 5))

    # ── 各数据接口 ──
    def fund_flow(self, code: str) -> dict:
        """个股资金流+概念(问财)"""
        return self._get("/api/fund-flow", {"code": code})

    def stock_basic(self, code: str) -> dict:
        """股本结构(同花顺F10): 总股本/流通股本/限售股/多期历史"""
        return self._get("/api/stock-basic", {"code": code})

    def stock_homepage(self, code: str) -> dict:
        """同花顺F10首页: PE/PB/总市值/质押/分类"""
        return self._get("/api/stock-homepage", {"code": code})

    def stock_holder(self, code: str) -> dict:
        """同花顺F10股东研究: 股东人数时序+前十大股东"""
        return self._get("/api/stock-holder", {"code": code})

    def stock_equity_history(self, code: str) -> dict:
        """同花顺F10股本历史: 多期股本结构+历次股本变动"""
        return self._get("/api/stock-equity-history", {"code": code})

    def stock_position(self, code: str) -> dict:
        """同花顺F10主力持仓: 机构持股汇总(5期)+明细"""
        return self._get("/api/stock-position", {"code": code})

    def stock_industry_peers(self, code: str) -> dict:
        """同花顺F10同行业对标: 同行财务指标排名"""
        return self._get("/api/stock-industry-peers", {"code": code})

    def market_overview(self, timeout: int = None) -> dict:
        """主要大盘指数概览（快速参考）。timeout 覆盖默认超时(大盘概览冷启动可 >30s)。"""
        return self._get("/api/market-overview", timeout=timeout)

    def stock_kline_full(self, code: str, days: int = 120, fqt: int = None) -> dict:
        """个股增强K线: 含换手率/涨跌幅/振幅。

        fqt: 0=不复权, 1=前复权。None=默认（页面数据即前复权；超窗口重取不复权）。
        筹码分布(CYQ)官方口径为前复权，调用时传 fqt=1。
        """
        params = {"code": code, "days": days}
        if fqt is not None:
            params["fqt"] = fqt
        return self._get("/api/stock-kline-full", params)

    def financial_quarterly(self, code: str) -> dict:
        """季频财务指标(同花顺F10): 净利润同比/ROE/毛利率/负债率/EPS"""
        return self._get("/api/financial-quarterly", {"code": code})

    def concept_blocks(self, code: str) -> dict:
        """个股概念归属 (问财): 所属概念板块列表"""
        return self._get("/api/concept-blocks", {"code": code})

    def stock_levels(self, code: str) -> dict:
        """支撑位/压力位 (问财)"""
        return self._get("/api/stock-levels", {"code": code})

    def wencai_all(self, code: str) -> dict:
        """问财全数据: 资金流+支撑位+概念 (一次查询)"""
        return self._get("/api/wencai-all", {"code": code})

    def eps_forecast(self, code: str) -> dict:
        """EPS一致预期 (同花顺F10): 机构盈利预测"""
        return self._get("/api/eps-forecast", {"code": code})

    def executive_changes(self, code: str) -> dict:
        """高管持股变动 (东方财富): 高管/大股东增减持明细"""
        return self._get("/api/executive-changes", {"code": code})

    def company_events(self, code: str) -> dict:
        """公司大事 (同花顺F10): 重要事件+高管持股变动+股东持股变动+担保+违规"""
        return self._get("/api/company-events", {"code": code})

    def stock_dividend(self, code: str, market: str = "", name: str = "") -> dict:
        """分红融资 (同花顺F10 astockpc SPA): 分红方案历史+分红诊断+增发/配股/获配明细

        market 留空时服务端按代码前缀自动推断（60/68→17, 90→18, 00/30→33, 20→34, 8/4→151）。
        """
        params = {"code": code}
        if market:
            params["market"] = market
        if name:
            params["name"] = name
        return self._get("/api/stock-dividend", params)

    def stock_news_f10(self, code: str, limit: int = 15) -> dict:
        """新闻公告 (同花顺F10 news.html): 新闻列表+研报列表"""
        return self._get("/api/stock-news-f10", {"code": code, "limit": limit})

    def stock_concept(self, code: str, market: str = "", name: str = "") -> dict:
        """概念板块明细 (同花顺F10 astockpc SPA #/concept): 概念名/龙头股/解析

        market 留空时服务端按代码前缀自动推断（与分红接口同一套规则）。
        """
        params = {"code": code}
        if market:
            params["market"] = market
        if name:
            params["name"] = name
        return self._get("/api/stock-concept", params)

    def industry_hotmap(self, level: str = "bk2", top_n: int = 20, ticker: str = "") -> dict:
        """大盘星图行业热力 (东财 stockhotmap): 全市场个股按行业聚合排名。

        ticker: 可选目标股票 6 位代码，返回其所属行业定位。
        """
        params = {"level": level, "top_n": top_n}
        if ticker:
            params["ticker"] = ticker
        return self._get("/api/industry-hotmap", params)

    def industry_board(self, top_n: int = 20) -> dict:
        """行业板块排名 (东财 push2 JSONP): 官方板块指数涨跌幅/涨跌家数/领涨股"""
        return self._get("/api/industry-board", {"top_n": top_n})

    def search_bing(self, q: str, count: int = 20, freshness: str = "") -> dict:
        """Bing 搜索 (国内直连): 网页搜索结果，返回标题/URL/摘要/来源域名

        freshness: "" | "day" | "week" | "month" — Bing 时间过滤
        """
        from urllib.parse import quote

        params = {"q": quote(q), "count": count}
        if freshness:
            params["freshness"] = freshness
        return self._get("/api/search-bing", params)

    def search_quark(self, q: str, count: int = 10) -> dict:
        """夸克 AI 搜索 (国内直连): AI 结构化总结 + 资讯列表 (标题/URL/摘要/来源/日期)

        夸克 AI 回答异步生成, 单次耗时可达 60s+, 显式放宽超时(默认 30s 会导致
        urlopen 超时计入熔断计数, 连续多次触发熔断中断全服务)。
        """
        from urllib.parse import quote

        return self._get(
            "/api/search-quark",
            {"q": quote(q), "count": count},
            timeout=90,
        )

    def fetch_article(self, url: str, max_chars: int = 3000) -> dict:
        """文章正文抓取: 标题/发布时间/正文 (站点专用选择器 + 通用启发式)"""
        return self._get("/api/fetch-article", {"url": url, "max_chars": max_chars})

    def global_news_cls(self, limit: int = 20) -> dict:
        """全球快讯(财联社电报): 标题/摘要/时间/重要度(level A/B/C)"""
        return self._get("/api/global-news-cls", {"limit": limit})

    def global_news_em(self, limit: int = 20) -> dict:
        """全球快讯(东财7x24): 标题/摘要/时间"""
        return self._get("/api/global-news-em", {"limit": limit})

    def stock_news_em(self, limit: int = 20) -> dict:
        """股市聚焦新闻(东财股票频道): 按区块(股市聚焦/大盘分析/板块聚焦等)返回标题/URL/时间"""
        return self._get("/api/stock-news-em", {"limit": limit})
