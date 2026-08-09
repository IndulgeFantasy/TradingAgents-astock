#!/usr/bin/env python3
"""
Playwright 数据服务
===================
在独立环境 (worktrade2) 中运行，为主项目提供 A 股数据。
所有数据通过 playwright + Chrome CDP 抓取（同花顺F10/问财/东财行情），
不依赖 akshare。

启动:
    conda activate worktrade2
    python playwright_service/server.py [--port 8765]

支持的环境变量:
    AKD_PORT=8765        监听端口 (默认 8765)
    AKD_HOST=0.0.0.0     监听地址 (默认 127.0.0.1)
    AKD_CACHE_TTL=300    缓存过期秒数 (默认 300, 0=禁用)
    WENCAI_CDP=http://127.0.0.1:9222  Chrome CDP 地址
"""

import json
import time
import os
import sys
import argparse
import traceback
import threading
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from functools import wraps
from datetime import datetime, timedelta

# ═══════════════════════════════════════════════════════════
# 补丁: mcp_query_table 问财解析 ARRAY 类型（所属概念/所属行业）
# ═══════════════════════════════════════════════════════════
try:
    import mcp_query_table.sites.iwencai as _iwencai
    _orig_convert = _iwencai.convert_type
    def _patched_convert(type):
        if type == 'ARRAY':
            return str  # ARRAY 转为字符串，兼容下游处理
        return _orig_convert(type)
    _iwencai.convert_type = _patched_convert
except Exception:
    pass


# ── 配置 ──
HOST = os.getenv("AKD_HOST", "127.0.0.1")
PORT = int(os.getenv("AKD_PORT", "8765"))
CACHE_TTL = int(os.getenv("AKD_CACHE_TTL", "300"))

logger = logging.getLogger("playwright_service")

# Windows GBK console cannot encode ⚠/emoji in startup prints (crashes when
# stdout is redirected). Force UTF-8 output so the service never dies on print.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


def _setup_file_logging() -> str:
    """Attach a rotating file handler to the playwright_service logger.

    Standalone service (own conda env), so it cannot import the main
    project's logging_setup; keep a small local copy here.
    """
    level = getattr(logging, os.getenv("AKD_LOG_LEVEL", "INFO").strip().upper(),
                    logging.INFO)
    log_dir = Path(__file__).resolve().parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "server.log"
    if not any(
        isinstance(h, RotatingFileHandler)
        and Path(getattr(h, "baseFilename", "")).resolve() == log_path.resolve()
        for h in logger.handlers
    ):
        handler = RotatingFileHandler(
            log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return str(log_path)

# 指数白名单：000xxx 同时是沪市指数与深市主板股票代码
# （如 000963 华东医药、000001 平安银行），必须以白名单区分指数；
# 其余 000/002/300/301 开头一律按深市个股处理。
_INDEX_CODES = {"000001", "000010", "000016", "000300", "000688", "000852",
                "000905", "000906", "399001", "399006", "399106", "399303"}

_cache = {}

# ── SWR 缓存控制 ──
# 缓存条目结构: {"ts", "data", "fresh_until", "hard_until", "is_error"}
#   fresh_until 前        → fresh, 直接返回
#   fresh_until..hard     → stale, 返回旧数据 + 后台刷新(请求零等待)
#   hard_until 后         → miss, single-flight 合并并发冷调用
# 失败结果也缓存(负缓存, _FAIL_CACHE_TTL 秒), 避免失败风暴反复打 Chrome
_HARD_TTL_FACTOR = float(os.getenv("AKD_HARD_TTL_FACTOR", "6"))
_FAIL_CACHE_TTL = float(os.getenv("AKD_FAIL_CACHE_TTL", "15"))
_cache_lock = threading.Lock()    # 保护 _cache / _refresh_workers / _single_flight
_refresh_workers = set()          # 后台刷新单飞行: key 集合
_single_flight = {}               # key -> {"event": threading.Event, "data": result}

# Chrome CDP 地址（playwright 通过 CDP 连接浏览器）
_WENCAI_CDP = os.getenv("WENCAI_CDP", "http://127.0.0.1:9222")

# Serialize all Chrome page operations: ThreadingHTTPServer spawns a thread per
# request, but Chrome CDP cannot handle concurrent page creation reliably.
# This lock ensures only one fetch_* runs at a time. Cached hits bypass it.
_cdp_lock = threading.Lock()


def _validate_code(code: str) -> str | None:
    """校验股票代码格式: 必须为 6 位数字。返回 None 表示合法，否则返回错误信息。"""
    import re
    if not code or not re.match(r'^\d{6}$', str(code)):
        return f"无效的股票代码: {code}（必须为6位数字）"
    return None


def _parse_sse_lines(text: str):
    """解析 SSE 流中所有 data: 行的 JSON，单行畸形跳过不影响整体。"""
    for line in text.strip().split("\n"):
        if not line.startswith("data:"):
            continue
        try:
            yield json.loads(line[5:])
        except (json.JSONDecodeError, ValueError):
            continue


def _extract_wencai_components(data_list):
    """Extract components list from iwencai response.

    Supports two API formats:
    - v2 JSON (get-robot-data): data.answer[0].txt[0].content.components
    - Legacy SSE (stream-query): each line has section.result_page.components
    """
    all_comps = []
    for d in data_list:
        # v2 JSON format: top-level {status_code, data: {answer: [...]}}
        # also handle case where d itself is the inner data dict
        root = d.get("data", d)
        answer = root.get("answer", []) if isinstance(root, dict) else []
        if answer:
            txt = answer[0].get("txt", [])
            if txt:
                comps = txt[0].get("content", {}).get("components", [])
                all_comps.extend(comps)
        # Legacy SSE format (fallback)
        comps = d.get("section", {}).get("result_page", {}).get("components", [])
        all_comps.extend(comps)
    return all_comps


async def _fetch_wencai_page(page, code):
    """Navigate to iwencai and capture the API response.

    Supports both v2 (get-robot-data, JSON) and legacy (stream-query, SSE).
    Returns a list of parsed JSON dicts.
    """
    async with page.expect_event(
        "response",
        predicate=lambda r: "get-robot-data" in r.url or "stream-query" in r.url,
        timeout=20000,
    ) as event_info:
        await page.goto(
            f"https://www.iwencai.com/unifiedwap/result?w={code}",
            wait_until="domcontentloaded",
        )
    response = await event_info.value
    text = await response.text()

    # v2 JSON format: single JSON object
    import json
    try:
        data = json.loads(text)
        return [data]
    except (json.JSONDecodeError, ValueError):
        pass

    # Legacy SSE format: multiple "data: {...}" lines
    results = []
    for line in text.strip().split("\n"):
        if not line.startswith("data:"):
            continue
        try:
            results.append(json.loads(line[5:]))
        except (json.JSONDecodeError, ValueError):
            continue
    return results


# ── 缓存装饰器 (SWR: stale-while-revalidate + single-flight + 负缓存) ──
def _cache_key(func_name, args, kwargs):
    """Build cache key matching @cached decorator."""
    return f"{func_name}:{args}:{ {k: v for k, v in kwargs.items() if v is not None} }"


def _store_cache(key, result, ttl, is_error=False):
    """写缓存条目(线程安全)。失败结果也缓存(负缓存, 短 TTL)。"""
    now = time.time()
    if is_error:
        fresh_until = now + _FAIL_CACHE_TTL
        entry = {"ts": now, "data": result, "is_error": True,
                 "fresh_until": fresh_until, "hard_until": fresh_until}
    else:
        hard_until = now + max(ttl * _HARD_TTL_FACTOR, ttl + 300)
        entry = {"ts": now, "data": result, "is_error": False,
                 "fresh_until": now + ttl, "hard_until": hard_until}
    with _cache_lock:
        _cache[key] = entry


def _cache_status(func, args=(), kwargs=None):
    """缓存状态检查(线程安全)。Returns (status, data), status ∈ fresh/stale/miss."""
    kwargs = kwargs or {}
    ttl = getattr(func, '_cached_ttl', 0)
    if ttl <= 0:
        return "miss", None
    key = _cache_key(func.__name__, args, kwargs)
    now = time.time()
    with _cache_lock:
        entry = _cache.get(key)
    if not entry:
        return "miss", None
    if now < entry["fresh_until"]:
        return "fresh", entry["data"]
    if now < entry["hard_until"]:
        return "stale", entry["data"]
    return "miss", None


def _refresh_in_background(key, func, args, kwargs, ttl):
    """stale 后台刷新(每 key 单飞行): 拿 _cdp_lock 执行原始函数并回写缓存。

    请求线程不等待刷新结果, 直接返回旧数据; 刷新与前台 Chrome 操作互斥。
    """
    with _cache_lock:
        if key in _refresh_workers:
            return
        _refresh_workers.add(key)

    def worker():
        try:
            with _cdp_lock:
                raw = getattr(func, "__wrapped__", func)
                result = raw(*args, **kwargs)
            ok = isinstance(result, dict) and result.get("success")
            _store_cache(key, result, ttl, is_error=not ok)
        except Exception as e:
            _store_cache(key, {"success": False,
                               "error": f"{type(e).__name__}: {str(e)[:200]}"},
                         ttl, is_error=True)
        finally:
            with _cache_lock:
                _refresh_workers.discard(key)

    threading.Thread(target=worker, daemon=True,
                     name=f"swr-refresh-{key[:40]}").start()


def _single_flight_fetch(func, args, kwargs, ttl):
    """缓存 miss 的并发合并(必须在 _cdp_lock 之外调用)。

    leader 持锁执行原始函数, followers 等待 leader 结果复用 ——
    并发雪崩(N 个 miss 排队冷调用)收敛为 1 次冷调用 + N-1 个等待者。
    """
    key = _cache_key(func.__name__, args, kwargs)
    with _cache_lock:
        is_leader = key not in _single_flight
        if is_leader:
            waiter = _single_flight[key] = {"event": threading.Event(), "data": None}
        else:
            waiter = _single_flight[key]
    if not is_leader:
        waiter["event"].wait(timeout=120)
        return waiter["data"]
    try:
        with _cdp_lock:
            raw = getattr(func, "__wrapped__", func)
            result = raw(*args, **kwargs)
        ok = isinstance(result, dict) and result.get("success")
        _store_cache(key, result, ttl, is_error=not ok)
        waiter["data"] = result
    except Exception as e:
        result = {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
        waiter["data"] = result
    finally:
        waiter["event"].set()
        with _cache_lock:
            _single_flight.pop(key, None)
    return waiter["data"]


def _serve_cached(func, args, kwargs):
    """统一缓存服务入口: fresh → 直返; stale → 锁内 wrapper(启动刷新, 零等待);
    miss → single-flight 合并并发冷调用。"""
    status, cached_data = _cache_status(func, args, kwargs)
    if status == "fresh":
        return cached_data
    ttl = getattr(func, "_cached_ttl", 0) or CACHE_TTL
    if status == "stale":
        with _cdp_lock:
            return func(*args, **kwargs)
    return _single_flight_fetch(func, args, kwargs, ttl)


def cached(ttl=None):
    ttl = ttl or CACHE_TTL
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if ttl <= 0:
                return func(*args, **kwargs)
            key = _cache_key(func.__name__, args, kwargs)
            now = time.time()
            with _cache_lock:
                entry = _cache.get(key)
            if entry and now < entry["fresh_until"]:
                return entry["data"]
            if entry and now < entry["hard_until"]:
                # SWR: 返回旧数据 + 后台刷新(单飞行), 请求零等待
                _refresh_in_background(key, func, args, kwargs, ttl)
                return entry["data"]
            # miss/hard-expired: 当前线程执行(调用方持锁或走 single-flight)
            result = func(*args, **kwargs)
            ok = isinstance(result, dict) and result.get("success")
            _store_cache(key, result, ttl, is_error=not ok)
            return result
        wrapper._cached_ttl = ttl
        return wrapper
    return decorator


async def _wait_for_ready(page, check, timeout=12.0, poll_ms=250, confirm_rounds=2):
    """信号驱动等待：轮询 `check`（async callable，返回真值=数据就绪）直到满足或超时。

    替代固定 wait_for_timeout：页面数据就绪即返回（通常 0.5~2s），
    慢时最多等到 timeout（与原固定等待同量级，不改变提取兜底逻辑）。
    confirm_rounds: 连续 N 次轮询都为真才认为就绪（SPA 渐进渲染防抖）。
    返回 True=就绪，False=超时（调用方走原有兜底）。
    """
    deadline = time.time() + timeout
    hits = 0
    while time.time() < deadline:
        try:
            ok = await check()
        except Exception:
            ok = False
        if ok:
            hits += 1
            if hits >= confirm_rounds:
                return True
        else:
            hits = 0
        await page.wait_for_timeout(poll_ms)
    return hits >= confirm_rounds


# ── fetch_stock_basic: 股本结构（同花顺 equity.html）──
@cached(ttl=3600)
def fetch_stock_basic(code: str):
    """通过 playwright 访问同花顺 equity 页面，获取股本信息。"""
    import asyncio
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"success": False, "error": "playwright 未安装"}

    async def _do_query():
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(_WENCAI_CDP)
            try:
                ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await ctx.new_page()
                await page.set_viewport_size({"width": 1280, "height": 800})
                await page.goto(
                    f"https://basic.10jqka.com.cn/{code}/equity.html",
                    wait_until="domcontentloaded", timeout=20000
                )
                # 信号等待: 等"总股本"数据渲染完成（固定 6s -> 通常 0.5~2s）
                await _wait_for_ready(
                    page,
                    lambda: page.evaluate(
                        "() => !!(document.body && document.body.innerText.includes('总股本'))"
                    ),
                    timeout=12.0,
                )

                # 提取股本表格 + 多期历史
                equity = await page.evaluate("""() => {
                    const tables = document.querySelectorAll('table');
                    const result = { totalShares: null, floatShares: null,
                                     restrictedShares: null, shareHistory: [] };

                    // 1. 当前股本（搜索所有 table）
                    for (const table of tables) {
                        for (const tr of table.querySelectorAll('tr')) {
                            const cells = tr.querySelectorAll('td, th');
                            if (cells.length >= 2) {
                                const label = cells[0].textContent.trim();
                                const val = cells[1].textContent.trim();
                                if (label.includes('A股总股本') || label.includes('变动后A股总股本'))
                                    if (!result.totalShares) result.totalShares = val;
                                if (label.includes('流通A股') || label.includes('变动后流通A股'))
                                    if (!result.floatShares) result.floatShares = val;
                                if (label.includes('限售A股') || label.includes('变动后限售A股'))
                                    if (!result.restrictedShares) result.restrictedShares = val;
                            }
                        }
                    }
                    // fallback: table[1] 总股本(股)
                    if (!result.totalShares && tables.length >= 2) {
                        for (const tr of tables[1].querySelectorAll('tr')) {
                            const cells = tr.querySelectorAll('td, th');
                            if (cells.length >= 2 && cells[0].textContent.trim().includes('总股本')) {
                                result.totalShares = cells[1].textContent.trim();
                            }
                        }
                    }

                    // 2. 多期历史 (table[1] 股份构成)
                    if (tables.length >= 2) {
                        const headerCells = tables[1].querySelectorAll('th');
                        const dates = [];
                        for (let i = 1; i < headerCells.length; i++) {
                            const d = headerCells[i].textContent.trim();
                            if (d) dates.push(d);
                        }
                        for (const tr of tables[1].querySelectorAll('tr')) {
                            const cells = tr.querySelectorAll('td, th');
                            if (cells.length >= 2 && cells[0].textContent.trim().includes('总股本')) {
                                for (let i = 1; i < cells.length && i-1 < dates.length; i++) {
                                    const v = cells[i].textContent.trim();
                                    if (v) result.shareHistory.push({ date: dates[i-1], value: v });
                                }
                            }
                        }
                    }
                    return result;
                }""")
                if not equity or not equity.get("totalShares"):
                    return {"success": False, "error": f"equity.html 无 {code} 股本数据"}

                # 从页面标题取股票名
                name = await page.evaluate("() => document.title.split('(')[0].trim()")

                # 解析亿/万单位
                import re
                def parse_shares(s):
                    if not s: return None
                    s = s.replace(",", "").replace(" ", "").strip()
                    neg = 1
                    if s.startswith("-"): neg = -1; s = s[1:]
                    unit = 1
                    if "万亿" in s: unit = 1e4; s = s.replace("万亿", "")
                    elif "亿" in s: unit = 1; s = s.replace("亿", "")
                    elif "万" in s: unit = 0.0001; s = s.replace("万", "")
                    m = re.search(r'[\d.]+', s)
                    if m:
                        try: return round(neg * float(m.group()) * unit, 4)
                        except ValueError: return None
                    return None

                ts = parse_shares(equity.get("totalShares"))
                fs = parse_shares(equity.get("floatShares"))
                rs = parse_shares(equity.get("restrictedShares"))

                data = {"code": code, "name": name or ""}
                if ts is not None:
                    data["总股本"] = ts  # 亿
                    data["总股本(亿)"] = f"{ts:.2f}"
                if fs is not None:
                    data["流通股本"] = fs  # 亿
                    data["流通股本(亿)"] = f"{fs:.2f}"
                if rs is not None:
                    data["限售A股"] = rs  # 亿

                # 多期历史股本变化
                history = equity.get("shareHistory", [])
                if history:
                    parsed = []
                    for h in history:
                        pv = parse_shares(h.get("value"))
                        if pv is not None:
                            parsed.append({"date": h.get("date", ""), "totalShares": pv})
                    if parsed:
                        data["shareHistory"] = parsed

                return {"success": True, "data": data, "source": "同花顺F10"}

            finally:
                try:
                    await page.close()
                except Exception:
                    pass

    try:
        return asyncio.run(_do_query())
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ── fetch_market_overview: 大盘概览（东财 zs 页面）──
async def _fetch_index_page(ctx, idx, code, name, extra):
    """并行抓取单个指数页（fetch_market_overview 的单元）。

    每个指数用独立 page；extra 仅由 idx==0 页写入（单写者，无需锁）。
    返回 (name, result_entry|None, errors:list)。
    """
    secid = f"1.{code}" if code.startswith(("0", "6")) else f"0.{code}"
    url = f"https://quote.eastmoney.com/zs{code}.html"
    captured = {"kline_list": [], "price": None}
    errors = []

    async def on_response(resp):
        url_match = resp.url
        try:
            import re, json
            body = await resp.text()
            body = re.sub(r'^\w+\(|\)[^)]*$', '', body)
            d = json.loads(body)

            # push2 实时行情
            if "api/qt/stock/get" in url_match and "kline" not in url_match and "ulist" not in url_match:
                data = d.get("data", {})
                if data:
                    captured["price"] = data.get("f43", 0) / 100 if data.get("f43") else None

            # K 线历史 — 用列表收集所有 kline 响应，取第一个有效结果
            # (东财指数页面可能同时请求多个 kline API，后到的会覆盖先到的)
            if "api/qt/stock/kline/get" in url_match and "smplmt" not in url_match:
                klines_raw = d.get("data", {}).get("klines", [])
                if klines_raw:
                    parsed = []
                    for k in klines_raw:
                        parts = k.split(",")
                        if len(parts) >= 6:
                            try:
                                entry = {
                                    "date": parts[0],
                                    "open": float(parts[1]),
                                    "close": float(parts[2]),
                                    "high": float(parts[3]),
                                    "low": float(parts[4]),
                                    "volume": float(parts[5]),
                                }
                                if len(parts) >= 7:
                                    entry["amount"] = float(parts[6])
                                if len(parts) >= 9:
                                    entry["pctChg"] = float(parts[8])
                                if len(parts) >= 11:
                                    entry["turnover"] = float(parts[10])
                                parsed.append(entry)
                            except (ValueError, IndexError):
                                pass
                    if len(parsed) >= 2:
                        captured["kline_list"].append(parsed)

            # 互联互通资金流向 (北向+南向，只在第一次加载时捕获)
            if idx == 0 and "api/qt/kamt/get" in url_match:
                data = d.get("data", {})
                # 北向净买入: hk2sh(沪股通) hk2sz(深股通) — 外资买A股净额(万元)
                # 注意: 2024-08-16 起交易所停止发布北向实时净买入，netBuyAmt
                #       恒为 0 或字段缺失；上游任何非零值都只是占位/旧快照。
                #       因此无条件标记不可用，禁止把净买入数值泄漏给 LLM。
                extra["north_net_sh"] = None
                extra["north_net_sz"] = None
                extra["north_unavailable"] = True
                # 北向成交额: hk2sh(沪) hk2sz(深) — 外资在A股总成交额(万元)
                hk_bs_sh_raw = data.get("hk2sh", {}).get("buySellAmt", 0)
                hk_bs_sz_raw = data.get("hk2sz", {}).get("buySellAmt", 0)
                extra["hk_bs_sh"] = round(hk_bs_sh_raw / 10000, 2) if hk_bs_sh_raw else None
                extra["hk_bs_sz"] = round(hk_bs_sz_raw / 10000, 2) if hk_bs_sz_raw else None
                # 南向净买入: sh2hk(沪港通) sz2hk(深港通) — 内资买港股净额(万元)
                sh_net = data.get("sh2hk", {}).get("netBuyAmt", 0)
                sz_net = data.get("sz2hk", {}).get("netBuyAmt", 0)
                extra["hk_net_sh"] = round(sh_net / 10000, 2) if sh_net else None
                extra["hk_net_sz"] = round(sz_net / 10000, 2) if sz_net else None
                # 南向成交额: sh2hk(沪) sz2hk(深) — 内资买港股总成交额(万元)
                ss_bs_sh = data.get("sh2hk", {}).get("buySellAmt", 0)
                ss_bs_sz = data.get("sz2hk", {}).get("buySellAmt", 0)
                extra["hk_bs_ss_sh"] = round(ss_bs_sh / 10000, 2) if ss_bs_sh else None
                extra["hk_bs_ss_sz"] = round(ss_bs_sz / 10000, 2) if ss_bs_sz else None

            # 行业板块排行 (只在第一次加载时捕获)
            if idx == 0 and "api/qt/clist/get" in url_match and "t:2" in url_match:
                items = d.get("data", {}).get("diff", [])
                if items and len(items) >= 3:
                    top5 = []
                    for item in items[:5]:
                        top5.append({
                            "name": item.get("f14", ""),
                            "chg": item.get("f3", 0),
                        })
                    if top5 and not extra["top_sectors"]:
                        extra["top_sectors"] = top5

            # 融资融券 (只在第一次加载时捕获，且只取第一次有效值)
            if idx == 0 and "RPT_MARGIN" in url_match:
                items = d.get("result", {}).get("data", [])
                if items and extra["margin_balance"] is None:
                    extra["margin_balance"] = items[0].get("MARGIN_BALANCE", 0)

        except Exception:
            pass

    page = await ctx.new_page()
    await page.set_viewport_size({"width": 1280, "height": 800})
    page.on("response", on_response)

    entry = None
    try:
        async with page.expect_response(
            lambda r, s=secid: f"secid={s}" in r.url and "kline" in r.url,
            timeout=15000
        ) as resp_info:
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        # 信号等待: 已捕获到K线后留约 0.5s 让多套K线响应到齐
        await _wait_for_ready(
            page,
            lambda: bool(captured["kline_list"]),
            timeout=8.0,
            poll_ms=200,
        )

        # 东财指数页面可能返回多套 K 线（长期日K + 短期日K），
        # 取最后捕获的那个（通常是页面主图的日K，长度 60-120）
        klines = captured["kline_list"][-1] if captured["kline_list"] else []

        if klines and len(klines) >= 62:
            last = klines[-1]
            sixtieth = klines[-60]
            close_now = last["close"]
            close_prev = klines[-2]["close"]
            close_60d_ago = sixtieth["close"]

            pct_chg = round((close_now - close_prev) / close_prev * 100, 4)
            chg_60d = round((close_now - close_60d_ago) / close_60d_ago * 100, 4) if close_60d_ago else None
            entry = {
                "最新": close_now,
                "涨跌幅": pct_chg,
                "近60日涨跌幅": chg_60d,
            }

            # 所有指数: 均线 + 量价 + 近5日K线摘要
            if len(klines) >= 60:
                closes = [k["close"] for k in klines]
                vols = [k.get("volume", 0) for k in klines]
                turnovers = [k.get("turnover", 0) for k in klines if k.get("turnover")]
                ma5 = sum(closes[-5:]) / 5
                ma10 = sum(closes[-10:]) / 10
                ma20 = sum(closes[-20:]) / 20
                ma60 = sum(closes[-60:]) / 60
                vol_now = vols[-1]
                vol_ma5 = sum(vols[-5:]) / 5
                # 多空排列判断
                bull = ma5 > ma10 > ma20 > ma60
                bear = ma5 < ma10 < ma20 < ma60
                # 量价关系
                if close_now > ma5 and vol_now > vol_ma5 * 1.3:
                    vol_price = "放量上涨"
                elif close_now < ma5 and vol_now > vol_ma5 * 1.3:
                    vol_price = "放量下跌"
                elif close_now > ma5 and vol_now < vol_ma5 * 0.7:
                    vol_price = "缩量上涨"
                elif close_now < ma5 and vol_now < vol_ma5 * 0.7:
                    vol_price = "缩量下跌"
                else:
                    vol_price = "正常"
                entry["均线"] = {
                    "MA5": round(ma5, 2),
                    "MA10": round(ma10, 2),
                    "MA20": round(ma20, 2),
                    "MA60": round(ma60, 2),
                    "排列": "多头" if bull else ("空头" if bear else "震荡"),
                }
                entry["量价"] = vol_price
                entry["成交量"] = round(vol_now, 0)
                if turnovers:
                    entry["换手率"] = round(turnovers[-1], 2)
                # MACD (12,26,9)
                if len(closes) >= 35:
                    ema12 = closes[0]
                    for c in closes[1:]:
                        ema12 = c * 2 / 13 + ema12 * 11 / 13
                    ema26 = closes[0]
                    for c in closes[1:]:
                        ema26 = c * 2 / 27 + ema26 * 25 / 27
                    dif = ema12 - ema26
                    # DEA 是 DIF 的 9 日 EMA，简化用最近 9 日 DIF 序列
                    difs = []
                    ema12_r = closes[0]
                    ema26_r = closes[0]
                    for c in closes[1:]:
                        ema12_r = c * 2 / 13 + ema12_r * 11 / 13
                        ema26_r = c * 2 / 27 + ema26_r * 25 / 27
                        difs.append(ema12_r - ema26_r)
                    if len(difs) >= 9:
                        dea = difs[-9]
                        for d in difs[-8:]:
                            dea = d * 2 / 10 + dea * 8 / 10
                        macd = (dif - dea) * 2
                        entry["MACD"] = {
                            "DIF": round(dif, 2),
                            "DEA": round(dea, 2),
                            "MACD": round(macd, 2),
                        }
                # 近5日K线摘要
                recent_5 = klines[-5:]
                entry["近5日"] = [
                    {
                        "date": k.get("date", ""),
                        "open": k.get("open", 0),
                        "close": k["close"],
                        "high": k.get("high", 0),
                        "low": k.get("low", 0),
                        "pctChg": k.get("pctChg", 0),
                        "volume": k.get("volume", 0),
                        "amount": k.get("amount", 0),
                        "turnover": k.get("turnover", 0),
                    }
                    for k in recent_5
                ]
        else:
            errors.append(f"{code}: 无K线数据")

    except Exception as e:
        errors.append(f"{code}: {type(e).__name__}: {str(e)[:60]}")

    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass

    # 首次加载完成后，从 DOM 提取额外数据（仅 idx==0；页面此刻仍打开）
    if idx == 0:
        try:
            dom_data = await page.evaluate("""() => {
                const result = { total_amount: null, up_count: null, down_count: null };
                const text = document.body.innerText;
                const lines = text.split('\\n').map(l => l.trim()).filter(l => l);

                // 两市成交额: 上证 + 深证
                let counted = new Set();
                let total = 0;
                for (const l of lines) {
                    for (const mkt of ['上证', '深证']) {
                        if (l.includes(mkt) && !counted.has(mkt)) {
                            const m = l.match(/([\\d.]+)(万亿|亿)/);
                            if (m) {
                                const val = parseFloat(m[1]);
                                total += m[2] === '万亿' ? val * 10000 : val;
                                counted.add(mkt);
                            }
                        }
                    }
                }
                if (total > 0) result.total_amount = total;

                // 涨跌家数
                let totalUp = 0;
                let totalDown = 0;
                for (const l of lines) {
                    const m = l.match(/涨:(\\d+)\\s*平:(\\d+)\\s*跌:(\\d+)/);
                    if (m) {
                        totalUp += parseInt(m[1]);
                        totalDown += parseInt(m[3]);
                    }
                }
                if (totalUp > 0) result.up_count = totalUp;
                if (totalDown > 0) result.down_count = totalDown;

                return result;
            }""")
            if dom_data.get("total_amount"):
                extra["total_amount"] = dom_data["total_amount"]
            if dom_data.get("up_count") is not None:
                extra["up_count"] = dom_data["up_count"]
            if dom_data.get("down_count") is not None:
                extra["down_count"] = dom_data["down_count"]

        except Exception as e:
            import logging
            logging.getLogger("playwright_service").warning(
                "fetch_market_overview: DOM extra data extraction failed: %s", e
            )

    # 关闭页面，避免页面泄漏
    try:
        await page.close()
    except Exception:
        pass

    return name, entry, errors


@cached(ttl=600)
def fetch_market_overview():
    import asyncio
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"success": False, "error": "playwright 未安装"}

    async def _do_query():
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(_WENCAI_CDP)
            try:
                ctx = browser.contexts[0] if browser.contexts else await browser.new_context()

                indices = [
                    ("000001", "上证指数"),
                    ("000300", "沪深300"),
                    ("399001", "深证成指"),
                    ("399006", "创业板指"),
                    ("000688", "科创50"),
                    ("000905", "中证500"),
                    ("399303", "国证2000"),
                ]
                results = {}
                errors = []
                extra = {"total_volume": None, "total_amount": None,
                         "up_count": None, "down_count": None,
                         "north_net_sh": None, "north_net_sz": None,
                         "north_unavailable": False,
                         "hk_net_sh": None, "hk_net_sz": None,
                         "hk_bs_sh": None, "hk_bs_sz": None,
                         "hk_bs_ss_sh": None, "hk_bs_ss_sz": None,
                         "top_sectors": [], "margin_balance": None}

                # 并行抓取 7 个指数页（各自独立 page）。串行最坏 ~78s，
                # 并行实测 ~20s，低于客户端默认 30s 超时。extra 仅由 idx==0
                # 页写入（单写者），errors 由各 task 返回后合并。
                tasks = [
                    asyncio.create_task(_fetch_index_page(ctx, idx, code, name, extra))
                    for idx, (code, name) in enumerate(indices)
                ]
                outcomes = await asyncio.gather(*tasks, return_exceptions=True)

                for outcome in outcomes:
                    if isinstance(outcome, Exception):
                        errors.append(
                            f"并行抓取异常: {type(outcome).__name__}: {str(outcome)[:80]}"
                        )
                        continue
                    name, entry, page_errors = outcome
                    errors.extend(page_errors)
                    if entry is not None:
                        results[name] = entry

                # 并行创建页面后统一让 Chrome CDP 喘息一次，避免连续开关页面过快
                await asyncio.sleep(1.5)

                if not results:
                    return {"success": False, "error": "获取大盘数据失败", "details": errors}

                # 组装返回
                ret = {
                    "success": True,
                    "data": results,
                    "source": "东财行情",
                }

                # 添加额外数据
                extra_data = {}
                if extra.get("total_amount"):
                    extra_data["两市成交额(亿)"] = round(extra["total_amount"], 0)
                if extra.get("up_count") is not None and extra.get("down_count") is not None:
                    total = extra["up_count"] + extra["down_count"]
                    extra_data["上涨家数"] = extra["up_count"]
                    extra_data["下跌家数"] = extra["down_count"]
                    extra_data["涨跌比"] = f"{extra['up_count']/extra['down_count']:.2f}" if extra["down_count"] else "N/A"

                # 北向资金（外资通过港股通买A股，hk2sh=沪股通 hk2sz=深股通）
                if extra.get("north_unavailable"):
                    extra_data["北向资金净买入"] = "已停止发布（交易所2024年起停止公布北向实时净买入数据）"
                else:
                    north_parts = []
                    if extra.get("north_net_sh") is not None:
                        extra_data["北向资金(沪股通)净买入(亿)"] = extra["north_net_sh"]
                        north_parts.append(extra["north_net_sh"])
                    if extra.get("north_net_sz") is not None:
                        extra_data["北向资金(深股通)净买入(亿)"] = extra["north_net_sz"]
                        north_parts.append(extra["north_net_sz"])
                    if north_parts:
                        extra_data["北向资金净买入合计(亿)"] = round(sum(north_parts), 2)
                if extra.get("hk_bs_sh") is not None and extra.get("hk_bs_sz") is not None:
                    extra_data["北向资金成交额合计(亿)"] = round(extra["hk_bs_sh"] + extra["hk_bs_sz"], 0)

                # 南向资金（内资通过港股通买港股，sh2hk=沪港通 sz2hk=深港通）
                south_parts = []
                if extra.get("hk_net_sh") is not None:
                    extra_data["南向资金(沪港通)净买入(亿)"] = extra["hk_net_sh"]
                    south_parts.append(extra["hk_net_sh"])
                if extra.get("hk_net_sz") is not None:
                    extra_data["南向资金(深港通)净买入(亿)"] = extra["hk_net_sz"]
                    south_parts.append(extra["hk_net_sz"])
                if south_parts:
                    extra_data["南向资金净买入合计(亿)"] = round(sum(south_parts), 2)
                if extra.get("hk_bs_ss_sh") is not None:
                    extra_data["南向资金成交额合计(亿)"] = extra["hk_bs_ss_sh"] + (extra.get("hk_bs_ss_sz", 0) or 0)

                if extra.get("top_sectors"):
                    extra_data["领涨板块"] = [s["name"] for s in extra["top_sectors"][:3]]
                if extra.get("margin_balance") is not None:
                    extra_data["融资余额(亿)"] = round(extra["margin_balance"] / 1e8, 0)

                if extra_data:
                    ret["extra"] = extra_data

                if errors:
                    ret["details"] = errors

                return ret

            finally:
                # 页面已在各 _fetch_index_page task 内关闭，无需在此处理
                pass

    try:
        return asyncio.run(_do_query())
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ── 13. 个股增强K线（东财 push2his，含换手率/涨跌幅/成交量）──
@cached(ttl=600)
def fetch_stock_kline_full(code: str, days: int = 120, fqt: int = 0):
    """
    通过 playwright 访问东财个股/指数页面，获取含换手率的增强K线。
    K线格式: date, open, close, high, low, volume, amount, amplitude%, pctChg%, turnover%
    支持指数代码（000/399 开头，如 000300 沪深300），指数页为 zs 前缀。

    fqt: 重取历史K线时的复权方式（0=不复权, 1=前复权）。
        页面默认加载前复权(fqt=1)数据；天数超出页面窗口时需要按 fqt 重取。
        筹码分布(CYQ)官方口径为前复权，应传 fqt=1。
    """
    try:
        days = max(1, min(int(days), 10000))
    except (ValueError, TypeError):
        days = 120
    import asyncio
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"success": False, "error": "playwright 未安装"}

    async def _do_query():
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(_WENCAI_CDP)
            try:
                ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await ctx.new_page()
                await page.set_viewport_size({"width": 1280, "height": 800})

                # 指数识别：000xxx 同时是沪市指数与深市主板股票（如 000963 华东医药、
                # 000001 平安银行），必须用白名单区分。
                # 沪市指数(000xxx): 上证/沪深300/科创50/中证500 → secid 1.xxx、zs 页面
                # 深市指数(399xxx): 深成/创业板/国证2000    → secid 0.xxx、zs 页面
                # 股票: 6/9 开头为沪市(secid 1.xxx、sh 页面)，其余为深市(secid 0.xxx、sz 页面)
                is_index = code in _INDEX_CODES
                if is_index:
                    prefix = "zs"
                    market_id = "1" if code.startswith("000") else "0"
                else:
                    prefix = "sh" if code.startswith(("6", "9")) else "sz"
                    market_id = "1" if code.startswith(("6", "9")) else "0"
                url = f"https://quote.eastmoney.com/{prefix}{code}.html"

                # Eastmoney stock pages load K-line data for BOTH the requested
                # stock AND market indices (上证指数/深证成指/创业板指) for the
                # comparison chart.  Collect all push2his kline responses and
                # select the one matching the requested stock's secid, so index
                # data never overwrites stock data (issue: 收盘价与创业板指高度吻合).
                expected_secid = f"secid={market_id}.{code}"

                captured_list = []
                limit_prices = {}  # 涨停价/跌停价 from push2 stock/get
                async def on_resp(resp):
                    if "push2his.eastmoney.com/api/qt/stock/kline/get" in resp.url and "smplmt" not in resp.url:
                        try:
                            body = await resp.text()
                            import re, json
                            body = re.sub(r'^\w+\(|\)[^)]*$', '', body)
                            data = json.loads(body)
                            captured_list.append((resp.url, data))
                        except Exception:
                            pass
                    elif "push2.eastmoney.com/api/qt/stock/get" in resp.url:
                        # 捕获涨停价(f51)/跌停价(f52)/最新价(f43)/昨收(f60)
                        try:
                            body = await resp.text()
                            import re, json
                            body = re.sub(r'^\w+\(|\)[^)]*$', '', body)
                            data = json.loads(body)
                            d = data.get("data", {}) or {}
                            if d.get("f51") and d.get("f52") and str(d.get("f57","")) == code:
                                limit_prices["limit_up"] = d["f51"] / 100
                                limit_prices["limit_down"] = d["f52"] / 100
                                if d.get("f43"):
                                    limit_prices["price"] = d["f43"] / 100
                                if d.get("f60"):
                                    limit_prices["last_close"] = d["f60"] / 100
                        except Exception:
                            pass

                page.on("response", on_resp)
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                # 信号等待: 拦截到 push2his K线响应即就绪（顺带修复响应慢时误报"未获取到K线数据"）
                await _wait_for_ready(
                    page,
                    lambda: bool(captured_list),
                    timeout=12.0,
                    poll_ms=250,
                )

                if not captured_list:
                    return {"success": False, "error": "未获取到K线数据"}

                # Select the stock's K-line response, not the index comparison data.
                captured = None
                captured_url = None
                for resp_url, data in captured_list:
                    if expected_secid in resp_url:
                        captured = data
                        captured_url = resp_url
                        break
                if captured is None:
                    # Fallback: match by code field in the response payload.
                    for resp_url, data in captured_list:
                        resp_code = str(data.get("data", {}).get("code", ""))
                        if resp_code == code:
                            captured = data
                            captured_url = resp_url
                            break
                if captured is None:
                    # Last resort: use the first captured response.
                    captured, captured_url = captured_list[0]

                klines = captured.get("data", {}).get("klines", [])
                if not klines:
                    return {"success": False, "error": "K线数据为空"}

                # 页面默认只加载约120根日K（半屏窗口），且 chart 请求的是前复权(fqt=1)数据。
                # 若请求天数更多，则通过浏览器上下文 (page.request, Chrome 网络栈+身份)
                # 用相同 URL 重新请求 lmt=days、fqt=fqt(默认0不复权，筹码分布用1前复权)
                # 的完整历史。push2his 会封 python-requests 指纹，直接 requests.get 会被断连。
                # 仅当 URL 属于目标个股时才重取——兜底分支可能拿到指数对比图的 URL。
                if len(klines) < days and captured_url and expected_secid in captured_url:
                    try:
                        import re as _re, json as _json, urllib.parse as _up
                        _u = _up.urlsplit(captured_url)
                        _qs = _up.parse_qs(_u.query)
                        _qs["lmt"] = [str(days)]
                        _qs["fqt"] = [str(fqt)]
                        _long_url = _up.urlunsplit(
                            (_u.scheme, _u.netloc, _u.path, _up.urlencode(_qs, doseq=True), ""))
                        _resp = await page.request.get(_long_url, timeout=30000)
                        if _resp.ok:
                            _d = _json.loads(_re.sub(r'^\w+\(|\)[^)]*$', '', await _resp.text()))
                            _k2 = _d.get("data", {}).get("klines", []) or []
                            if len(_k2) > len(klines):
                                klines = _k2
                    except Exception:
                        pass

                # K线格式: date, open, close, high, low, volume, amount, amplitude, pctChg, ?, turnover
                records = []
                for k in klines:
                    parts = k.split(",")
                    if len(parts) >= 11:
                        try:
                            records.append({
                                "date": parts[0],
                                "open": float(parts[1]),
                                "close": float(parts[2]),
                                "high": float(parts[3]),
                                "low": float(parts[4]),
                                "volume": float(parts[5]),
                                "amount": float(parts[6]),
                                "amplitude": float(parts[7]),
                                "pctChg": float(parts[8]),
                                "turnover": float(parts[10]),
                            })
                        except (ValueError, TypeError):
                            pass

                # 只保留需要的天数
                if len(records) > days:
                    records = records[-days:]

                if not records:
                    return {"success": False, "error": f"解析后K线为空"}

                # 计算日均换手率和统计
                turns = [r["turnover"] for r in records]
                avg_turn = round(sum(turns) / len(turns), 4) if turns else 0

                stock_name = captured.get("data", {}).get("name", "")
                resp_code = str(captured.get("data", {}).get("code", ""))
                result = {
                    "success": True,
                    "data": records,
                    "rows": len(records),
                    "days": days,
                    "avg_turnover": avg_turn,
                    "source": "东财行情",
                    "stock_name": stock_name,
                    "resp_code": resp_code,
                }
                if limit_prices:
                    result["limit_prices"] = limit_prices
                if resp_code and resp_code != code:
                    result["warning"] = f"响应代码({resp_code})与请求代码({code})不匹配，可能数据源返回了指数"
                if len(records) < days:
                    _prev = result.get("warning", "")
                    _msg = f"仅获取{len(records)}根K线(<请求{days}根)，历史窗口不足（次新股或重取失败）"
                    result["warning"] = f"{_prev}; {_msg}" if _prev else _msg
                return result

            finally:
                try:
                    await page.close()
                except Exception:
                    pass

    try:
        return asyncio.run(_do_query())
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ── fetch_stock_homepage: 同花顺 F10 首页综合信息（PE/PB/市值/质押/分类）──
@cached(ttl=3600)
def fetch_stock_homepage(code: str):
    """
    通过 playwright 访问同花顺 F10 首页，提取:
    估值: PE(动态/静态), PB, 总市值
    股本: 总股本, 流通A股
    质押: 质押股份数量, 质押比例
    分类: 超大盘股/大盘股/中盘股/小盘股
    """
    import asyncio
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"success": False, "error": "playwright 未安装"}

    async def _do_query():
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(_WENCAI_CDP)
            try:
                ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await ctx.new_page()
                await page.set_viewport_size({"width": 1280, "height": 800})
                await page.goto(
                    f"https://basic.10jqka.com.cn/{code}/",
                    wait_until="domcontentloaded", timeout=15000
                )
                # 信号等待: 估值数据渲染完成
                await _wait_for_ready(
                    page,
                    lambda: page.evaluate(
                        "() => !!(document.body && (document.body.innerText.includes('市盈率') || document.body.innerText.includes('总市值')))"
                    ),
                    timeout=12.0,
                )

                text = await page.evaluate("() => document.body.innerText")
                import re as re_h

                data = {"code": code}
                # 取股票名
                title_m = re_h.search(r'(.+?)\(\d{6}\)', text)
                if title_m: data["name"] = title_m.group(1).strip()

                # 优先从 table DOM 提取 label-value 对
                dom_pairs = await page.evaluate("""() => {
                    const pairs = {};
                    for (const table of document.querySelectorAll('table')) {
                        for (const tr of table.querySelectorAll('tr')) {
                            const cells = tr.querySelectorAll('td, th');
                            for (let i = 0; i < cells.length - 1; i++) {
                                const label = cells[i].textContent.trim();
                                const val = cells[i+1].textContent.trim();
                                if (label && val && val.length < 60 && !pairs[label]) pairs[label] = val;
                            }
                        }
                    }
                    return pairs;
                }""")

                def _get_dom_value(cn_label):
                    for k, v in dom_pairs.items():
                        if cn_label in k:
                            return v
                    return None

                # PE(动态), PE(静态), PB, 总市值
                pe_dyn_val = _get_dom_value('市盈率(动态)') or _get_dom_value('市盈率（动态）')
                # 页面嵌套布局下 DOM 提取可能命中整个估值卡片文本
                # （"每股收益：…查看明细>>"），必须先校验为纯数字/亏损，否则丢弃走正则回退
                if pe_dyn_val and not re_h.fullmatch(r'[\d.]+|亏损', pe_dyn_val.strip()):
                    pe_dyn_val = None
                if not pe_dyn_val:
                    m = re_h.search(r'市盈率[（(]动态[）)][：:]\s*([\d.]+|亏损)', text)
                    if m: pe_dyn_val = m.group(1)
                if pe_dyn_val: data["pe_dynamic"] = pe_dyn_val

                pe_sta_val = _get_dom_value('市盈率(静态)') or _get_dom_value('市盈率（静态）')
                if pe_sta_val and not re_h.fullmatch(r'[\d.]+|亏损', pe_sta_val.strip()):
                    pe_sta_val = None
                if not pe_sta_val:
                    m = re_h.search(r'市盈率[（(]静态[）)][：:]\s*([\d.]+)', text)
                    if m: pe_sta_val = m.group(1)
                if pe_sta_val:
                    try: data["pe_static"] = float(pe_sta_val)
                    except ValueError: pass

                pb_val = _get_dom_value('市净率')
                if pb_val and not re_h.fullmatch(r'[\d.]+', pb_val.strip()):
                    pb_val = None
                if not pb_val:
                    m = re_h.search(r'市净率[：:]\s*([\d.]+)', text)
                    if m: pb_val = m.group(1)
                if pb_val:
                    try: data["pb"] = float(pb_val)
                    except ValueError: pass

                mcap_val = _get_dom_value('总市值')
                if not mcap_val:
                    m = re_h.search(r'总市值[：:]\s*([\d.]+)亿', text)
                    if m: mcap_val = m.group(1)
                if mcap_val:
                    try: data["total_mcap_yi"] = float(str(mcap_val).replace('亿', ''))
                    except ValueError: pass

                # 分类
                cls_val = _get_dom_value('分类')
                if not cls_val:
                    m = re_h.search(r'分类[：:]\s*(\S+)', text)
                    if m: cls_val = m.group(1)
                if cls_val: data["category"] = cls_val

                # 总股本, 流通A股
                ts_val = _get_dom_value('总股本')
                if not ts_val:
                    m = re_h.search(r'总股本[：:]\s*([\d.]+)亿', text)
                    if m: ts_val = m.group(1)
                if ts_val:
                    try: data["total_shares_yi"] = float(str(ts_val).replace('亿', ''))
                    except ValueError: pass

                fss_val = _get_dom_value('流通A股')
                if not fss_val:
                    m = re_h.search(r'流通A股[：:]\s*([\d.]+)亿', text)
                    if m: fss_val = m.group(1)
                if fss_val:
                    try: data["float_shares_yi"] = float(str(fss_val).replace('亿', ''))
                    except ValueError: pass

                # 质押
                pledge_val = _get_dom_value('质押股份数量')
                if not pledge_val:
                    m = re_h.search(r'质押股份数量[：:]\s*([\d.]+)万?股?', text)
                    if m: pledge_val = m.group(0)
                if pledge_val:
                    raw = str(pledge_val)
                    val_m = re_h.search(r'([\d.]+)', raw)
                    if val_m:
                        val = float(val_m.group(1))
                        data["pledge_shares"] = round(val / 10000, 4) if "万" in raw else round(val, 4)

                pledge_pct_val = _get_dom_value('质押股份占A股总股本比')
                if not pledge_pct_val:
                    m = re_h.search(r'质押股份占A股总股本比[：:]\s*([\d.]+)%', text)
                    if m: pledge_pct_val = m.group(1)
                if pledge_pct_val:
                    try: data["pledge_ratio"] = float(str(pledge_pct_val).replace('%', ''))
                    except ValueError: pass

                # 记录解析失败的关键字段
                missing_fields = [f for f in ["pe_dynamic", "pb", "total_mcap_yi", "pledge_ratio"]
                                  if f not in data]
                if missing_fields:
                    import logging
                    logging.getLogger("playwright_service").warning(
                        "fetch_stock_homepage: %s missing fields for %s: %s",
                        code, code, ", ".join(missing_fields)
                    )

                if not data.get("pe_dynamic") and not data.get("total_mcap_yi"):
                    return {"success": False, "error": f"首页未提取到有效数据"}

                return {"success": True, "data": data, "source": "同花顺F10"}

            finally:
                try:
                    await page.close()
                except Exception:
                    pass

    try:
        return asyncio.run(_do_query())
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ── fetch_stock_equity_history: 股本历史变动（同花顺 equity.html）──
@cached(ttl=3600)
def fetch_stock_equity_history(code: str):
    """
    提取 equity.html 的:
    1. 多期股本结构时序 (A股总股本/流通A股/限售A股)
    2. A股历次股本变动（含变动原因/日期/数量）
    """
    import asyncio
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"success": False, "error": "playwright 未安装"}

    async def _do_query():
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(_WENCAI_CDP)
            try:
                ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await ctx.new_page()
                await page.set_viewport_size({"width": 1280, "height": 800})
                await page.goto(f"https://basic.10jqka.com.cn/{code}/equity.html",
                                wait_until="domcontentloaded", timeout=15000)
                # 信号等待: 股本结构表渲染完成（至少 2 张表且有数据行）
                await _wait_for_ready(
                    page,
                    lambda: page.evaluate(
                        "() => { const ts = document.querySelectorAll('table'); return ts.length >= 2 && !!ts[1].querySelectorAll('tr').length; }"
                    ),
                    timeout=12.0,
                )

                result = await page.evaluate("""() => {
                    const ts = document.querySelectorAll('table');
                    const out = { shareStructure: [], historicalChanges: [] };
                    if (ts.length >= 2) {
                        const hds = ts[1].querySelectorAll('th');
                        const dates = Array.from(hds).slice(1).map(d=>d.textContent.trim()).filter(Boolean);
                        for (const tr of ts[1].querySelectorAll('tr')) {
                            const cells = tr.querySelectorAll('td,th');
                            if (cells.length<2) continue;
                            const label = cells[0].textContent.trim();
                            for (let i=1; i<cells.length && i-1<dates.length; i++) {
                                const v = cells[i].textContent.trim();
                                if (v) out.shareStructure.push({date:dates[i-1], label, value:v});
                            }
                        }
                    }
                    for (const table of ts) {
                        if ((table.innerText||'').includes('变动日期') && (table.innerText||'').includes('变动原因')) {
                            for (const tr of table.querySelectorAll('tr')) {
                                const cells = tr.querySelectorAll('td,th');
                                if (cells.length>=5 && /^\\d{4}/.test(cells[0].textContent.trim())) {
                                    out.historicalChanges.push({
                                        date: cells[0].textContent.trim(),
                                        reason: cells[1].textContent.trim(),
                                        totalAfter: cells[2].textContent.trim(),
                                        floatAfter: cells[3].textContent.trim(),
                                        restrictedAfter: cells[4].textContent.trim(),
                                    });
                                }
                            }
                        }
                    }
                    return out;
                }""")
                if not result.get("shareStructure") and not result.get("historicalChanges"):
                    return {"success": False, "error": f"equity.html 无 {code} 数据"}
                return {"success": True, "data": result, "source": "同花顺F10"}
            finally:
                try:
                    await page.close()
                except Exception:
                    pass

    try:
        return asyncio.run(_do_query())
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ── fetch_stock_holder: 股东研究（同花顺 holder.html，通过 playwright）──
@cached(ttl=3600)
def fetch_stock_holder(code: str):
    """
    通过 playwright 访问同花顺 F10 holder 页面，提取:
    1. 股东人数多期时序 (10期，含股东人数/环比变化/行业平均/户均流通股/户均流通市值)
    2. 前十大流通股东 (多期，含持股数/增减/占比/质押比例/变动比例)
    3. 前十大股东 (按总股本，含持股数/增减/占比/质押比例/实控人性质)
    4. 退出前十大流通股东列表 (减持信号)
    5. 退出前十大股东列表
    6. 同业股东人数变化对比 (top10 增加/减少最多, 默认隐藏 tab 内嵌表, 数据已在 DOM)
    """
    import asyncio
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"success": False, "error": "playwright 未安装"}

    async def _do_query():
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(_WENCAI_CDP)
            try:
                ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await ctx.new_page()
                await page.set_viewport_size({"width": 1280, "height": 800})
                await page.goto(
                    f"https://basic.10jqka.com.cn/{code}/holder.html",
                    wait_until="domcontentloaded", timeout=15000
                )
                # 信号等待: 股东数据渲染完成（出现股东人数表）
                await _wait_for_ready(
                    page,
                    lambda: page.evaluate(
                        "() => !!(document.body && (document.body.innerText.includes('股东人数') || document.querySelectorAll('table').length >= 3))"
                    ),
                    timeout=12.0,
                )

                result = await page.evaluate("""() => {
                    const tables = document.querySelectorAll('table');
                    const out = {
                        shareHolderCount: [],
                        top10Holders: [],
                        top10Shareholders: [],
                        exitedFloatHolders: [],
                        exitedShareholders: [],
                        peerComparison: { topIncrease: [], topDecrease: [] }
                    };

                    // Helper: collect date labels from fdates/tdates links
                    // fdates -> 流通股东 period tabs, tdates -> 十大股东 period tabs
                    function collectDates(cls) {
                        const links = document.querySelectorAll('a.' + cls);
                        return Array.from(links).map(a => a.textContent.trim()).filter(Boolean);
                    }
                    const floatDates = collectDates('fdates');
                    const totalDates = collectDates('tdates');

                    // Helper: extract holder rows from a table by header-name mapping
                    // This avoids column-index fragility when optional columns (pledge) exist.
                    function extractHolders(table) {
                        const rows = table.rows;
                        if (rows.length < 2) return [];
                        // find header row: the row containing "机构或基金名称" or "股东名称"
                        let headerRowIdx = -1;
                        for (let i = 0; i < Math.min(rows.length, 3); i++) {
                            const txt = (rows[i].textContent || '').trim();
                            if (txt.includes('机构或基金名称') || txt.includes('股东名称')) {
                                headerRowIdx = i;
                                break;
                            }
                        }
                        if (headerRowIdx < 0) return [];

                        // build column index map from header cell text
                        const headerCells = Array.from(rows[headerRowIdx].querySelectorAll('td, th'));
                        const colMap = {};
                        headerCells.forEach((c, idx) => {
                            const t = (c.textContent || '').trim();
                            if (t.includes('名称')) colMap.name = idx;
                            else if (t.includes('持有数量') || t.includes('持股数')) colMap.shares = idx;
                            else if (t.includes('持股变化') || t.includes('增减')) colMap.change = idx;
                            else if (t.includes('占流通') || t.includes('占总股') || t.includes('占比')) colMap.ratio = idx;
                            else if (t.includes('质押') || t.includes('冻结')) colMap.pledgeRatio = idx;
                            else if (t.includes('变动比例')) colMap.changePct = idx;
                            else if (t.includes('股份类型') || t.includes('持股性质')) colMap.shareType = idx;
                        });

                        const holders = [];
                        for (let r = headerRowIdx + 1; r < rows.length; r++) {
                            const cells = Array.from(rows[r].querySelectorAll('td, th')).map(c => (c.textContent || '').trim());
                            if (cells.length < 3) continue;
                            const name = colMap.name !== undefined ? cells[colMap.name] : cells[0];
                            if (!name || name.includes('机构或基金名称') || name.includes('股东名称')
                                || name.includes('前十大') || name.includes('累计持有')
                                || name.includes('退出')) continue;
                            const holder = { name: name };
                            if (colMap.shares !== undefined) holder.shares = cells[colMap.shares] || '';
                            if (colMap.change !== undefined) holder.change = (cells[colMap.change] || '').slice(0, 40);
                            if (colMap.ratio !== undefined) holder.ratio = cells[colMap.ratio] || '';
                            if (colMap.pledgeRatio !== undefined) holder.pledgeRatio = cells[colMap.pledgeRatio] || '';
                            if (colMap.changePct !== undefined) holder.changePct = cells[colMap.changePct] || '';
                            if (colMap.shareType !== undefined) holder.shareType = cells[colMap.shareType] || '';
                            holders.push(holder);
                        }
                        return holders;
                    }

                    // Helper: classify a table by which h2 section it belongs to
                    const allH2 = document.querySelectorAll('h2');
                    const h2List = Array.from(allH2);
                    function sectionOf(table) {
                        let section = '';
                        for (const h of h2List) {
                            if (h.compareDocumentPosition(table) & Node.DOCUMENT_POSITION_FOLLOWING) {
                                section = h.textContent.trim();
                            } else {
                                break;
                            }
                        }
                        return section;
                    }

                    // 1. 股东人数时序: table[1]标签, table[2]日期, table[3]数据
                    if (tables.length >= 4) {
                        const labels = Array.from(tables[1].querySelectorAll('td, th')).map(td => td.textContent.trim());
                        const dates = Array.from(tables[2].querySelectorAll('td, th')).map(td => td.textContent.trim());
                        const dataRows = [];
                        for (const tr of tables[3].querySelectorAll('tr')) {
                            const cells = Array.from(tr.querySelectorAll('td, th')).map(td => td.textContent.trim());
                            dataRows.push(cells);
                        }
                        for (let i = 0; i < dates.length && i < 10; i++) {
                            const entry = { date: dates[i] };
                            for (let j = 0; j < labels.length && j < dataRows.length; j++) {
                                if (i < dataRows[j].length) {
                                    entry[labels[j].replace(/\\s+/g, '_')] = dataRows[j][i];
                                }
                            }
                            out.shareHolderCount.push(entry);
                        }
                    }

                    // 2 & 3. 十大流通股东 + 十大股东: classify by section, attach dates from fdates/tdates
                    let floatIdx = 0;
                    let totalIdx = 0;
                    for (let ti = 0; ti < tables.length; ti++) {
                        const table = tables[ti];
                        const section = sectionOf(table);
                        if (!section) continue;
                        if (section.includes('股东人数')) continue;
                        if (section.includes('同业')) continue;
                        if (table.rows.length < 3) continue;

                        const summary = (table.rows[0]?.textContent || '').trim().slice(0, 200);
                        const holders = extractHolders(table);
                        if (holders.length === 0) continue;

                        if (section.includes('流通')) {
                            const period = floatIdx < floatDates.length ? floatDates[floatIdx] : '';
                            floatIdx++;
                            out.top10Holders.push({ summary: summary, period: period, holders: holders });
                        } else if (section.includes('十大股东')) {
                            const period = totalIdx < totalDates.length ? totalDates[totalIdx] : '';
                            totalIdx++;
                            out.top10Shareholders.push({ summary: summary, period: period, holders: holders });
                        }
                    }

                    // 4 & 5. 退出前十大: tables whose text contains "退出前十大"
                    for (let ti = 0; ti < tables.length; ti++) {
                        const table = tables[ti];
                        const txt = (table.textContent || '').trim();
                        if (txt.includes('退出前十大流通股东')) {
                            const holders = extractHolders(table);
                            for (const h of holders) out.exitedFloatHolders.push(h);
                        } else if (txt.includes('退出前十大股东')) {
                            const holders = extractHolders(table);
                            for (const h of holders) out.exitedShareholders.push(h);
                        }
                    }

                    // 6. 同业股东人数变化对比: 表头含"股东人数"+"增减量"
                    // 标题格式: "同行业股东人数增减量前10名" / "同行业股东人数增减量后10名"
                    // (该区块在 chart_nav "股东人数增减排名" tab 下, 默认隐藏但数据已在 DOM,
                    //   h2 section 仍为"股东人数", 故按表头文本而非 section 匹配)
                    const tcode = (document.getElementById('stockCode')?.value || '').trim();
                    for (let ti = 0; ti < tables.length; ti++) {
                        const table = tables[ti];
                        const head = (table.rows[0]?.textContent || '').trim();
                        if (!head.includes('股东人数') || !head.includes('增减量')) continue;
                        const isDecrease = head.includes('后10名');
                        const rows = [];
                        for (const tr of table.querySelectorAll('tr')) {
                            const cells = Array.from(tr.querySelectorAll('td, th')).map(td => td.textContent.trim());
                            if (cells.length >= 3) rows.push(cells);
                        }
                        const target = isDecrease ? out.peerComparison.topDecrease : out.peerComparison.topIncrease;
                        for (const r of rows.slice(1)) {
                            // 跳过标题行/表头行(标题含"增减量", 表头含"股票简称"/"股东人数")
                            if (!r[0]) continue;
                            if (r[0].includes('增减量') || r[0].includes('股票简称') || r[0].includes('股东人数')) continue;
                            if (r[0] !== tcode) {
                                target.push({ name: r[0], count: r[1] || '', change: r[2] || '' });
                            }
                        }
                    }

                    return out;
                }""")

                if not result.get("shareHolderCount") and not result.get("top10Holders"):
                    return {"success": False, "error": f"holder.html 无 {code} 股东数据"}

                return {"success": True, "data": result, "source": "同花顺F10"}

            finally:
                try:
                    await page.close()
                except Exception:
                    pass

    try:
        return asyncio.run(_do_query())
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ── fetch_stock_position: 主力持仓/机构持股（同花顺 position 页面）──
# marketid 复用 _infer_marketid（旧版 F10 #marketId 官方值提取: 17/33/18/34/151）。
# 实测 basicapi 接口不校验 marketid（只认 code），此处保持与分红接口同规则统一。

@cached(ttl=3600)
def fetch_stock_position(code: str):
    """
    通过 playwright 访问同花顺主力持仓页面，提取:
    1. 机构持股汇总（5期: 机构数量/累计持仓/持仓比例/变化）
    2. 机构持股明细（各机构名称/类型/持股数/增减/占比）
    """
    import asyncio
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"success": False, "error": "playwright 未安装"}

    async def _do_query():
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(_WENCAI_CDP)
            try:
                ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await ctx.new_page()
                await page.set_viewport_size({"width": 1280, "height": 800})

                # 根据代码前缀决定 marketid（与分红接口同一套规则）
                marketid = _infer_marketid(code)
                url = (f"https://basic.10jqka.com.cn/astockpc/astockmain/index.html"
                       f"#/position?code={code}&marketid={marketid}&code_name=")

                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                # 信号等待: 机构持股汇总表出现且行数>=6（提取逻辑要求）
                await _wait_for_ready(
                    page,
                    lambda: page.evaluate(
                        "() => { const t = document.querySelectorAll('table')[0]; return !!t && t.querySelectorAll('tr').length >= 6; }"
                    ),
                    timeout=12.0,
                )

                result = await page.evaluate("""() => {
                    const tables = document.querySelectorAll('table');
                    const out = { institutionSummary: [], institutionDetail: [] };

                    // 1. 机构持股汇总 table[0]: 主力进出\报告期 + 5期数据
                    if (tables.length >= 1) {
                        const trs = tables[0].querySelectorAll('tr');
                        if (trs.length >= 6) {
                            // 表头: 报告期
                            const headers = trs[0].querySelectorAll('td, th');
                            const periods = Array.from(headers).slice(1).map(h => h.textContent.trim()).filter(Boolean);
                            // 各行数据
                            const labels = ['机构数量(家)', '累计持有数量(股)', '累计市值(元)', '持仓比例', '较上期变化(股)'];
                            for (let i = 1; i < trs.length && i-1 < labels.length; i++) {
                                const cells = trs[i].querySelectorAll('td, th');
                                for (let j = 1; j < cells.length && j-1 < periods.length; j++) {
                                    out.institutionSummary.push({
                                        period: periods[j-1],
                                        label: labels[i-1],
                                        value: cells[j].textContent.trim()
                                    });
                                }
                            }
                        }
                    }

                    // 2. 机构持股明细 table[1]: 机构或基金名称/类型/持股/占比/增减
                    if (tables.length >= 2) {
                        for (const tr of tables[1].querySelectorAll('tr')) {
                            const cells = tr.querySelectorAll('td, th');
                            if (cells.length >= 6) {
                                const name = cells[0].textContent.trim();
                                if (!name || name.includes('机构或基金名称')) continue;
                                out.institutionDetail.push({
                                    name: name,
                                    type: cells[1].textContent.trim(),
                                    shares: cells[2].textContent.trim(),
                                    marketValue: cells[3].textContent.trim(),
                                    ratio: cells[4].textContent.trim(),
                                    change: cells[5].textContent.trim()
                                });
                            }
                        }
                    }

                    return out;
                }""")

                if not result.get("institutionSummary") and not result.get("institutionDetail"):
                    return {"success": False, "error": f"position 无 {code} 持仓数据"}

                return {"success": True, "data": result, "source": "同花顺F10"}

            finally:
                try:
                    await page.close()
                except Exception:
                    pass

    try:
        return asyncio.run(_do_query())
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ── 14. 季频成长+现金流（同花顺 finance.html，通过 playwright）──
@cached(ttl=3600)
def fetch_financial_quarterly(code: str):
    """
    通过 playwright 访问同花顺 F10 finance 页面，提取:
    1. 财务指标矩阵 (29 个指标 × 最近 8 期，覆盖成长/每股/盈利/运营/偿债五大维度)
    2. 指标变动说明 (5 个子表：成长/盈利/负债/运营/现金流，含变动原因文字说明)
    3. 财务报告审计意见 (最近 4 年年报审计意见)
    4. 资产负债构成 (资产 6 行 + 负债 5 行)
    5. 三大报表全量科目明细 (资产负债表/利润表/现金流量表, 全量期数, 经 getFinanceEdit.php)
    """
    import asyncio
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"success": False, "error": "playwright 未安装"}

    async def _do_query():
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(_WENCAI_CDP)
            try:
                ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await ctx.new_page()
                await page.set_viewport_size({"width": 1280, "height": 800})
                await page.goto(
                    f"https://basic.10jqka.com.cn/{code}/finance.html",
                    wait_until="domcontentloaded", timeout=20000
                )
                # 信号等待: 财务指标矩阵表渲染完成（提取逻辑要求 tables>=5）
                await _wait_for_ready(
                    page,
                    lambda: page.evaluate("() => document.querySelectorAll('table').length >= 5"),
                    timeout=14.0,
                )

                # === 1. 财务指标矩阵 + 2. 指标变动说明 + 3. 审计意见 + 4. 资产负债构成 ===
                raw = await page.evaluate("""() => {
                    const tables = document.querySelectorAll('table');
                    const out = { matrix: null, changes: [], audit: [], assets: [], liabilities: [] };

                    // --- 1. 财务指标矩阵 ---
                    if (tables.length >= 5) {
                        const labels = Array.from(tables[1].querySelectorAll('td, th')).map(td => td.textContent.trim());
                        const dates = Array.from(tables[2].querySelectorAll('td, th')).map(td => td.textContent.trim());
                        const dataRows = [];
                        for (const tr of tables[4].querySelectorAll('tr')) {
                            const cells = Array.from(tr.querySelectorAll('td, th')).map(td => td.textContent.trim());
                            dataRows.push(cells);
                        }
                        out.matrix = { labels, dates, dataRows };
                    }

                    // --- 2. 指标变动说明: header 含"变动科目"+"变动原因"的表格 ---
                    for (const t of tables) {
                        const headerText = (t.rows[0]?.textContent || '').trim();
                        if (headerText.includes('变动科目') && headerText.includes('变动原因')) {
                            const rows = [];
                            for (const tr of t.querySelectorAll('tr')) {
                                const cells = Array.from(tr.querySelectorAll('td, th')).map(c => c.textContent.trim());
                                if (cells.length >= 5) rows.push(cells);
                            }
                            if (rows.length > 1) out.changes.push(rows);
                        }
                    }

                    // --- 3. 审计意见: header 含"年份"+"审计意见" ---
                    for (const t of tables) {
                        const headerText = (t.rows[0]?.textContent || '').trim();
                        if (headerText.includes('年份') && headerText.includes('审计意见')) {
                            for (let ri = 1; ri < t.rows.length; ri++) {  // skip header row
                                const cells = Array.from(t.rows[ri].querySelectorAll('td, th')).map(c => c.textContent.trim());
                                if (cells.length >= 6 && cells[0] && cells[0] !== '年份') out.audit.push({
                                    year: cells[0], q1: cells[1], mid: cells[2],
                                    q3: cells[3], annual: cells[4], opinion: cells[5]
                                });
                            }
                            break;
                        }
                    }

                    // --- 4. 资产负债构成: header 含"科目"+"金额"，不含"变动" ---
                    for (const t of tables) {
                        const headerText = (t.rows[0]?.textContent || '').trim();
                        if (headerText.includes('科目') && headerText.includes('金额') && !headerText.includes('变动')) {
                            const rows = [];
                            for (let ri = 1; ri < t.rows.length; ri++) {  // skip header row
                                const cells = Array.from(t.rows[ri].querySelectorAll('td, th')).map(c => c.textContent.trim());
                                if (cells.length >= 2 && cells[0] && cells[0] !== '科目') rows.push({ name: cells[0], value: cells[1] });
                            }
                            // 区分资产表 vs 负债表：资产表含"流动资产"或"资产总计"
                            const allText = rows.map(r => r.name).join('');
                            if (allText.includes('资产总计') || allText.includes('流动资产')) {
                                out.assets = rows;
                            } else if (allText.includes('负债总计') || allText.includes('流动负债')) {
                                out.liabilities = rows;
                            }
                        }
                    }

                    return out;
                }""")

                if not raw or not raw.get("matrix"):
                    return {"success": False, "error": "未找到财务数据表格"}

                matrix = raw["matrix"]
                labels = matrix["labels"]
                dates = matrix["dates"]
                dataRows = matrix["dataRows"]

                # 找各指标的行索引
                def idx_of(keywords):
                    for i, lbl in enumerate(labels):
                        if any(k in lbl for k in keywords):
                            return i
                    return None

                # 全部 29 个指标的行索引映射
                indicator_map = {
                    # 成长能力 (7)
                    "NetProfit": ["净利润(元)"],
                    "YOYNI": ["净利润同比增长率"],
                    "CoreProfit": ["扣非净利润(元)"],
                    "YOYCoreProfit": ["扣非净利润同比增长率"],
                    "Revenue": ["营业总收入(元)"],
                    "YOYRevenue": ["营业总收入同比增长率"],
                    # 每股指标 (5)
                    "EPS": ["基本每股收益(元)"],
                    "BPS": ["每股净资产(元)"],
                    "CapitalReserve": ["每股资本公积金(元)"],
                    "RetainedEarning": ["每股未分配利润(元)"],
                    "CFPS": ["每股经营现金流(元)"],
                    # 盈利能力 (4)
                    "NetMargin": ["销售净利率"],
                    "GrossMargin": ["销售毛利率"],
                    "ROE": ["净资产收益率"],
                    "ROEDiluted": ["净资产收益率-摊薄"],
                    # 运营能力 (4)
                    "OperatingCycle": ["营业周期"],
                    "InventoryTurnover": ["存货周转率"],
                    "InventoryDays": ["存货周转天数"],
                    "ReceivableDays": ["应收账款周转天数"],
                    # 偿债能力 (5)
                    "CurrentRatio": ["流动比率"],
                    "QuickRatio": ["速动比率"],
                    "ConservativeQuickRatio": ["保守速动比率"],
                    "EquityRatio": ["产权比率"],
                    "DebtRatio": ["资产负债率"],
                }

                idx_map = {}
                for key, keywords in indicator_map.items():
                    idx = idx_of(keywords)
                    if idx is not None:
                        idx_map[key] = idx

                # 辅助: 解析数值字符串 ("53.95亿" -> 53.95, "48.74亿" -> 48.74)
                import re
                def parse_val(s):
                    if not s or s == "--" or s == "-":
                        return None
                    s = s.replace(",", "").replace(" ", "").replace("\u00a0", "")
                    neg = 1
                    if s.startswith("-"):
                        neg = -1
                        s = s[1:]
                    unit = 1
                    if "万亿" in s:
                        unit = 1e4  # convert to 亿
                        s = s.replace("万亿", "")
                    elif "亿" in s:
                        unit = 1
                        s = s.replace("亿", "")
                    elif "万" in s:
                        unit = 0.0001  # 万 -> 亿
                        s = s.replace("万", "")
                    m = re.search(r'[-]?[\d.]+', s)
                    if m:
                        try:
                            return round(neg * float(m.group()) * unit, 4)
                        except ValueError:
                            return None
                    return None

                # 取最近 8 期数据
                max_cols = min(len(dates), len(dataRows[0]) if dataRows else 0)
                num_periods = min(8, max_cols)
                results = []
                for col in range(0, num_periods):
                    ds = dates[col] if col < len(dates) else ""
                    # 提取季度标识
                    period = ""
                    if len(ds) >= 7:
                        ym = ds[:7]
                        try:
                            from datetime import datetime
                            dt = datetime.strptime(ym, "%Y-%m")
                            q = (dt.month - 1) // 3 + 1
                            period = f"{dt.year}Q{q}"
                        except ValueError:
                            period = ds[:7]

                    entry = {"period": period, "report_date": ds}

                    def row_val(idx, col):
                        if idx is not None and idx < len(dataRows) and col < len(dataRows[idx]):
                            return parse_val(dataRows[idx][col])
                        return None

                    # 抓全部 29 个指标
                    for key, idx in idx_map.items():
                        val = row_val(idx, col)
                        if val is not None:
                            entry[key] = round(val, 4) if abs(val) > 100 else round(val, 2)
                            # 百分比指标加 _label
                            if key in ("YOYNI", "YOYCoreProfit", "YOYRevenue", "ROE", "ROEDiluted",
                                       "NetMargin", "GrossMargin", "DebtRatio"):
                                entry[f"{key}_label"] = f"{val:+.2f}%" if key.startswith("YOY") else f"{val:.2f}%"

                    # 经营现金流/净利润比 (CFPS / EPS)
                    if entry.get("CFPS") and entry.get("EPS") and entry["EPS"] != 0:
                        entry["CFOToNP"] = round(entry["CFPS"] / entry["EPS"], 4)

                    results.append(entry)

                if not results:
                    return {"success": False, "error": f"finance.html 无 {code} 财务数据"}

                # 构建 summary（最新一期）
                latest = results[0]
                summary = {}
                for key, label in [
                    ("YOYNI_label", "净利润同比"),
                    ("YOYRevenue_label", "营收同比"),
                    ("YOYCoreProfit_label", "扣非净利润同比"),
                    ("ROE_label", "ROE"),
                    ("GrossMargin_label", "毛利率"),
                    ("NetMargin_label", "净利率"),
                    ("DebtRatio_label", "资产负债率"),
                    ("CFOToNP", "经营现金流/净利润"),
                ]:
                    if latest.get(key):
                        summary[label] = latest[key]
                if latest.get("EPS"):
                    summary["每股收益"] = latest["EPS"]

                # === 2. 指标变动说明 ===
                changes_data = []
                for table_rows in raw.get("changes", []):
                    for r in table_rows[1:]:  # skip header
                        if len(r) >= 5:
                            changes_data.append({
                                "subject": r[0], "current": r[1], "previous": r[2],
                                "change_pct": r[3], "reason": r[4][:150]
                            })

                # === 3. 审计意见 ===
                audit_data = raw.get("audit", [])

                # === 4. 资产负债构成 ===
                assets_data = raw.get("assets", [])
                liabilities_data = raw.get("liabilities", [])

                # === 5. 三大报表全量科目明细（getFinanceEdit.php 分批拉取）===
                statements = await _fetch_three_statements(page, code)

                return {
                    "success": True,
                    "data": results,
                    "rows": len(results),
                    "source": "同花顺F10",
                    "summary": summary,
                    "changes": changes_data,
                    "audit": audit_data,
                    "assets": assets_data,
                    "liabilities": liabilities_data,
                    "balance_sheet": statements.get("balanceSheet"),
                    "income_statement": statements.get("incomeState"),
                    "cash_flow": statements.get("cashFlow"),
                }

            finally:
                try:
                    await page.close()
                except Exception:
                    pass

    try:
        return asyncio.run(_do_query())
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ═══════════════════════════════════════════════════════════════
# 三大报表全量科目明细（finance.html 自定义指标面板 + getFinanceEdit.php）
# 页面内嵌科目库（final_result: balanceSheet/incomeState/cashFlow），
# 勾选科目后由 JS 经 getFinanceEdit.php?type=all&data=[["科目","元",0,false,true],...]
# 拉取全量期数数值（约 8 年 100+ 期）。此处复刻该机制，分批 ≤20 科目/请求。
# ═══════════════════════════════════════════════════════════════

_STATEMENT_BATCH = 20  # getFinanceEdit.php data 参数单批科目上限


async def _fetch_three_statements(page, code: str) -> dict:
    """从已打开的 finance.html 页面提取三大报表全量科目明细。

    Returns:
        {"balanceSheet": {...}, "incomeState": {...}, "cashFlow": {...}}
        每个报表: {"periods": [报告期...], "items": {科目: [数值...]}, "yoy": {科目: [同比...]} | None}
        任一步骤失败返回空 dict（不影响指标矩阵等主数据）。
    """
    try:
        # 1. 从自定义指标面板提取三组科目名
        groups = await page.evaluate("""() => {
            const out = {balanceSheet: [], incomeState: [], cashFlow: []};
            document.querySelectorAll('.final_result li').forEach(li => {
                const cls = li.getAttribute('data-class') || '';
                const name = (li.textContent || '').trim().replace(/\\s+/g, '');
                if (out[cls] && name) out[cls].push(name);
            });
            return out;
        }""")

        # 2. 每类报表分批拉取（data 参数 = [["科目","元",0,false,true],...]）
        result = {}
        for cls, names in groups.items():
            if not names:
                result[cls] = None
                continue
            periods = None
            items = {}
            yoy = {}
            for i in range(0, len(names), _STATEMENT_BATCH):
                batch = names[i:i + _STATEMENT_BATCH]
                payload = json.dumps([[n, "元", 0, False, True] for n in batch], ensure_ascii=False)
                text = await page.evaluate("""async (args) => {
                    const params = new URLSearchParams({type: 'all', data: args.payload, code: args.code});
                    const m = document.cookie.match(/(?:^|;\\s*)userid=([^;]+)/);
                    if (m) params.set('userid', m[1]);
                    const r = await fetch(
                        'https://basic.10jqka.com.cn/api/getFinanceEdit.php?' + params.toString(),
                        {credentials: 'include'}
                    );
                    return await r.text();
                }""", {"payload": payload, "code": code})
                try:
                    d = json.loads(text)
                except (ValueError, TypeError):
                    continue
                dd = d.get("data") or {}
                title = dd.get("title") or []
                report = dd.get("report") or []
                if not report or not title:
                    continue
                if periods is None:
                    periods = list(report[0])
                # report[0]=报告期, report[i]=第 i 个科目的数值; title[i]=[科目名,单位]
                for i2 in range(1, min(len(report), len(title))):
                    name = title[i2][0] if isinstance(title[i2], (list, tuple)) else str(title[i2])
                    if not name:
                        continue
                    items[name] = list(report[i2])
                    # 同比列（若存在且与数值等长）
                    ry = dd.get("report_yoy") or dd.get("yoy")
                    if ry and i2 < len(ry):
                        yoy[name] = list(ry[i2])
            if periods:
                result[cls] = {
                    "periods": periods,
                    "items": items,
                    "yoy": yoy if yoy else None,
                    "rows": len(items),
                    "total_periods": len(periods),
                }
            else:
                result[cls] = None
        return result
    except Exception as e:
        logger.warning("fetch three statements failed for %s: %s", code, str(e)[:200])
        return {}


# ── fetch_stock_industry_peers: 同行业对标（同花顺 field.html）──
@cached(ttl=3600)
def fetch_stock_industry_peers(code: str):
    """
    通过 playwright 访问同花顺 F10 field 页面，提取同行业公司财务指标对比。
    返回: 行业分类、同行公司列表(含每股收益/ROE/毛利率/净利润等)、本公司排名。
    """
    import asyncio
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"success": False, "error": "playwright 未安装"}

    async def _do_query():
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(_WENCAI_CDP)
            try:
                ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await ctx.new_page()
                await page.set_viewport_size({"width": 1280, "height": 800})
                await page.goto(
                    f"https://basic.10jqka.com.cn/{code}/field.html",
                    wait_until="domcontentloaded", timeout=15000
                )
                # 信号等待: 行业分类/同行对比渲染完成
                await _wait_for_ready(
                    page,
                    lambda: page.evaluate(
                        "() => !!(document.body && (document.body.innerText.includes('行业分类') || document.body.innerText.includes('同行业')))"
                    ),
                    timeout=12.0,
                )

                text = await page.evaluate("() => document.body.innerText")
                lines = [l.strip() for l in text.split("\n") if l.strip()]
                import re as re_f

                out = {"industry": "", "peers": [], "companyRank": ""}

                # 行业分类: 优先从 DOM 元素提取，fallback 到 innerText 正则
                industry_found = await page.evaluate("""() => {
                    for (const el of document.querySelectorAll('div, span, p, td, th')) {
                        const t = el.textContent.trim();
                        if (t.includes('行业分类') && t.length < 200) {
                            const m = t.match(/行业分类[：:]\\s*(.+?)(?:（共\\d+家）|$)/);
                            if (m) return m[1].trim();
                        }
                    }
                    return null;
                }""")
                if industry_found:
                    out["industry"] = industry_found
                else:
                    for l in lines:
                        m = re_f.search(r'行业分类[：:]\s*(.+?)(?:（共\d+家）|$)', l)
                        if m:
                            out["industry"] = m.group(1).strip()
                            break

                # 排名: 优先从 DOM 元素提取，fallback 到 innerText 正则
                rank_found = await page.evaluate("""() => {
                    for (const el of document.querySelectorAll('div, span, p, td, th')) {
                        const t = el.textContent.trim();
                        if (t.length < 50) {
                            const m = t.match(/第(\\d+)名/);
                            if (m) return '第' + m[1] + '名';
                        }
                    }
                    return null;
                }""")
                if rank_found:
                    out["companyRank"] = rank_found
                else:
                    for l in lines:
                        m = re_f.search(r'第(\d+)名', l)
                        if m:
                            out["companyRank"] = f"第{m.group(1)}名"
                            break

                if not out["industry"]:
                    import logging
                    logging.getLogger("playwright_service").warning(
                        "fetch_stock_industry_peers: industry not found for %s", code
                    )

                # 同行数据: 从页面表格提取
                tables = await page.evaluate("""() => {
                    const ts = document.querySelectorAll('table');
                    const result = [];
                    for (const table of ts) {
                        const rows = [];
                        for (const tr of table.querySelectorAll('tr')) {
                            const cells = Array.from(tr.querySelectorAll('td, th')).map(td => td.textContent.trim());
                            if (cells.length >= 3) rows.push(cells);
                        }
                        if (rows.length >= 3) result.push(rows);
                    }
                    return result;
                }""")

                if tables:
                    # 找最大的表格（同行数据）
                    largest = max(tables, key=lambda t: len(t))
                    if len(largest) >= 2:
                        headers = largest[0]
                        for row in largest[1:]:
                            if len(row) >= 2:
                                peer = {"name": row[0]}
                                for i in range(1, min(len(row), len(headers))):
                                    if headers[i] and row[i]:
                                        peer[headers[i]] = row[i]
                                out["peers"].append(peer)

                if not out.get("peers"):
                    return {"success": False, "error": f"field.html 无 {code} 行业对比数据"}

                return {"success": True, "data": out, "source": "同花顺F10"}
            finally:
                try:
                    await page.close()
                except Exception:
                    pass

    try:
        return asyncio.run(_do_query())
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ── 15. 个股概念归属（通过问财查询）──


@cached(ttl=3600)
def fetch_concept_blocks_wencai(code: str):
    """通过问财查询个股所属概念板块（适配 v2 API，不再依赖 mcp_query_table）。"""
    import asyncio
    try:
        from playwright.async_api import async_playwright

        async def _do_query():
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(_WENCAI_CDP)
                try:
                    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                    await page.set_viewport_size({"width": 1280, "height": 800})

                    data = await _fetch_wencai_page(page, f"{code}概念板块")
                    comps = _extract_wencai_components(data)

                    concepts = []
                    industry = ""
                    stock_name = ""
                    stock_code = ""

                    for comp in comps:
                        cd = comp.get("data", {}) if isinstance(comp.get("data"), dict) else {}
                        datas = cd.get("datas", [])
                        if not datas:
                            continue
                        row = datas[0]

                        concept_val = row.get("所属概念", [])
                        if isinstance(concept_val, list):
                            concepts = [c.strip() for c in concept_val if c]
                        elif isinstance(concept_val, str):
                            concepts = [c.strip() for c in concept_val.split(",") if c.strip()]

                        ind_val = row.get("所属同花顺行业", [])
                        if isinstance(ind_val, list):
                            industry = " > ".join(ind_val)
                        elif isinstance(ind_val, str):
                            industry = ind_val

                        stock_name = row.get("股票简称", "")
                        stock_code = row.get("股票代码", code)

                    if not concepts and not industry:
                        return {"success": False, "error": "问财无返回概念数据"}

                    return {
                        "success": True,
                        "data": {
                            "code": str(stock_code).strip(),
                            "name": str(stock_name).strip(),
                            "concepts": concepts,
                            "industry": industry,
                        },
                        "source": "问财(iwencai)",
                    }
                finally:
                    pass  # CDP browser shared - don't close

        return asyncio.run(_do_query())
    except ImportError as e:
        return {"success": False, "error": f"依赖缺失: {e}"}
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ── 16. 个股资金流时序+概念（通过 playwright 拉问财 barline3）──
@cached(ttl=600)
def fetch_fund_flow_wencai(code: str):
    """
    通过 playwright 查询问财，提取:
    1. barline3: 30日主力资金时间序列（替代东财 push2）
    2. barline3: dde散户数量变化趋势
    3. impressionLabel: 所属概念板块

    适配问财 v2 API (get-robot-data)，兼容旧版 stream-query SSE。
    """
    import asyncio
    try:
        from playwright.async_api import async_playwright

        async def _do_query():
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(_WENCAI_CDP)
                try:
                    ctx = browser.contexts[0]
                    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                    await page.set_viewport_size({"width": 1280, "height": 800})

                    data = await _fetch_wencai_page(page, code)
                    comps = _extract_wencai_components(data)

                    fund_flow = []
                    dde_flow = []
                    dde_retail_quantity = []
                    concepts = []
                    stock_name = ""

                    for comp in comps:
                        st = comp.get("show_type", "")
                        comp_data = comp.get("data", {}) if isinstance(comp.get("data"), dict) else {}
                        raw_cols = comp_data.get("columns", [])
                        # columns 可能是 list[dict]（含 index_name）或 list[str]
                        cols = []
                        for c in raw_cols:
                            if isinstance(c, dict):
                                cols.append(c.get("index_name", ""))
                            elif isinstance(c, str):
                                cols.append(c)
                        datas = comp_data.get("datas", [])

                        if st == "barline3" and datas:
                            if "主力资金" in cols:
                                for row in datas:
                                    fund_flow.append({
                                        "date": row.get("时间", "") or row.get("时间周期", ""),
                                        "main_force_net": row.get("主力资金"),
                                        "volume": row.get("成交额") or row.get("成交量"),
                                    })
                            elif "dde散单净流入" in cols:
                                for row in datas:
                                    dde_flow.append({
                                        "date": row.get("时间", ""),
                                        "dde_retail_net": row.get("dde散单净流入"),
                                        "close": row.get("收盘价") or row.get("股价走势"),
                                    })
                            if "dde散户数量" in cols:
                                for row in datas:
                                    dde_retail_quantity.append({
                                        "date": row.get("时间", ""),
                                        "dde_retail_qty": row.get("dde散户数量"),
                                    })

                        if st == "impressionLabel" and datas:
                            for row in datas:
                                label = row.get("看点", "") or row.get("标签", "")
                                cat = row.get("类型", "") or row.get("类别", "")
                                if cat and label:
                                    concepts.append({"category": cat, "label": label})

                        if st == "kline2" and datas and not stock_name:
                            stock_name = datas[0].get("股票简称", "") or datas[0].get("股票名称", "")

                    result = {
                        "fund_flow": fund_flow,
                        "fund_flow_days": len(fund_flow),
                        "dde_flow": dde_flow,
                        "dde_retail_quantity": dde_retail_quantity,
                        "concepts": concepts,
                        "stock_name": stock_name,
                    }
                    return {"success": True, "data": result, "source": "问财(iwencai)"}
                finally:
                    pass  # CDP browser shared - don't close

        return asyncio.run(_do_query())
    except ImportError as e:
        return {"success": False, "error": f"依赖缺失: {e}"}
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ── 17. 个股支撑位/压力位（通过 playwright 拉问财 kline2）──
@cached(ttl=600)
def fetch_stock_levels(code: str):
    """通过 playwright 查询问财 kline2 组件，获取支撑位/压力位"""
    import asyncio
    try:
        from playwright.async_api import async_playwright

        async def _do_query():
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(_WENCAI_CDP)
                try:
                    ctx = browser.contexts[0]
                    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                    await page.set_viewport_size({"width": 1280, "height": 800})

                    data = await _fetch_wencai_page(page, code)
                    comps = _extract_wencai_components(data)

                    support = None
                    resistance = None
                    stock_name = ""

                    for comp in comps:
                        if comp.get("show_type") != "kline2":
                            continue
                        datas = comp.get("data", {}).get("datas", [])
                        if datas:
                            stock_name = datas[0].get("股票简称", "") or datas[0].get("股票名称", "")
                            support = datas[0].get("止盈止损(支撑位)")
                            resistance = datas[0].get("止盈止损(压力位)")
                            if isinstance(support, (int, float)):
                                support = round(float(support), 2)
                            if isinstance(resistance, (int, float)):
                                resistance = round(float(resistance), 2)

                    if support is None and resistance is None:
                        return {"success": False, "error": "问财未返回支撑位数据"}
                    return {
                        "success": True,
                        "data": {
                            "stock_name": stock_name,
                            "support": support,
                            "resistance": resistance,
                        },
                        "source": "问财(iwencai)",
                    }
                finally:
                    pass  # CDP browser shared - don't close

        return asyncio.run(_do_query())
    except ImportError as e:
        return {"success": False, "error": f"依赖缺失: {e}"}
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ── 18. 问财通用查询（整合所有可用组件）──
@cached(ttl=600)
def fetch_wencai_all(code: str):
    """一次问财查询，返回所有可用数据组件

    适配问财 v2 API (get-robot-data)，兼容旧版 stream-query SSE。
    """
    import asyncio
    try:
        from playwright.async_api import async_playwright

        async def _do_query():
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(_WENCAI_CDP)
                try:
                    ctx = browser.contexts[0]
                    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                    await page.set_viewport_size({"width": 1280, "height": 800})

                    data = await _fetch_wencai_page(page, code)
                    comps = _extract_wencai_components(data)

                    result = {"fund_flow": [], "dde_retail_quantity": [], "levels": {}, "concepts": [], "finance": [], "stock_name": ""}

                    for comp in comps:
                        st = comp.get("show_type", "")
                        comp_data = comp.get("data", {}) if isinstance(comp.get("data"), dict) else {}
                        raw_cols = comp_data.get("columns", [])
                        # columns 可能是 list[dict]（含 index_name）或 list[str]
                        cols = []
                        for c in raw_cols:
                            if isinstance(c, dict):
                                cols.append(c.get("index_name", ""))
                            elif isinstance(c, str):
                                cols.append(c)
                        datas = comp_data.get("datas", [])

                        if st == "barline3" and datas:
                            if "主力资金" in cols:
                                for row in datas:
                                    result["fund_flow"].append({
                                        "date": row.get("时间", "") or row.get("时间周期", ""),
                                        "main_force_net": row.get("主力资金"),
                                    })
                            if "dde散户数量" in cols:
                                for row in datas:
                                    result["dde_retail_quantity"].append({
                                        "date": row.get("时间", ""),
                                        "dde_retail_qty": row.get("dde散户数量"),
                                    })
                        elif st == "kline2" and datas:
                            r = datas[0]
                            result["levels"] = {
                                "support": r.get("止盈止损(支撑位)"),
                                "resistance": r.get("止盈止损(压力位)"),
                            }
                            if not result["stock_name"]:
                                result["stock_name"] = r.get("股票简称", "") or r.get("股票名称", "")
                        elif st == "impressionLabel" and datas:
                            for row in datas:
                                result["concepts"].append({
                                    "label": row.get("看点", "") or row.get("标签", ""),
                                    "category": row.get("类别", ""),
                                })

                    return {"success": True, "data": result, "source": "问财(iwencai)"}
                finally:
                    pass  # CDP browser shared - don't close

        return asyncio.run(_do_query())
    except ImportError as e:
        return {"success": False, "error": f"依赖缺失: {e}"}
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ── 19. EPS一致预期（通过 playwright 拉同花顺F10）──
@cached(ttl=3600)
def fetch_eps_forecast(code: str):
    """通过 playwright 访问同花顺F10 worth页面，提取完整数据。

    使用 table DOM API (document.querySelectorAll('table')) 提取结构化表格数据，
    避免 innerText 行级解析因空单元格换行导致的数据错位问题。
    """
    import asyncio
    import re
    try:
        from playwright.async_api import async_playwright

        async def _do_query():
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(_WENCAI_CDP)
                try:
                    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                    page = await ctx.new_page()
                    await page.set_viewport_size({"width": 1280, "height": 800})
                    await page.goto(
                        f"https://basic.10jqka.com.cn/{code}/worth.html",
                        wait_until="domcontentloaded", timeout=20000
                    )
                    # 信号等待: 机构预测数据渲染完成
                    await _wait_for_ready(
                        page,
                        lambda: page.evaluate(
                            "() => !!(document.body && document.body.innerText.includes('预测') && document.querySelectorAll('table').length >= 1)"
                        ),
                        timeout=12.0,
                    )

                    # 单次 evaluate 提取所有结构化数据：
                    # - tables: 所有 <table> 的二维数组
                    # - summaryText: 机构覆盖摘要行
                    # - indicatorsText: 详细指标预测 section 原始文本
                    # - researchText: 研报评级 section 原始文本
                    # - pageTitle: 页面标题（含股票名称）
                    raw = await page.evaluate("""() => {
                        const result = {
                            tables: [],
                            summaryText: '',
                            indicatorsText: '',
                            researchText: '',
                            pageTitle: document.title || ''
                        };

                        // 提取所有 table 为二维数组
                        const tables = document.querySelectorAll('table');
                        for (const table of tables) {
                            const rows = [];
                            for (const tr of table.querySelectorAll('tr')) {
                                const cells = [];
                                for (const cell of tr.querySelectorAll('td, th')) {
                                    cells.push(cell.textContent.trim());
                                }
                                if (cells.length > 0) rows.push(cells);
                            }
                            if (rows.length > 0) result.tables.push(rows);
                        }

                        // 提取机构预测明细的调高/调低标记
                        // <s class="up"> = 调高, <s class="down"> = 调低, <s class=""> = 无变化
                        const forecastAdjustments = [];
                        for (const table of tables) {
                            const headerText = (table.rows[0]?.textContent || '').trim();
                            if (headerText.includes('机构名称') && headerText.includes('研究员')) {
                                for (let ri = 2; ri < table.rows.length; ri++) {
                                    const row = table.rows[ri];
                                    const cells = row.querySelectorAll('td, th');
                                    if (cells.length >= 8) {
                                        const adjustments = [];
                                        for (let ci = 2; ci < Math.min(8, cells.length); ci++) {
                                            const s = cells[ci].querySelector('s');
                                            if (s) {
                                                if (s.className.includes('up')) adjustments.push('调高');
                                                else if (s.className.includes('down')) adjustments.push('调低');
                                                else adjustments.push('不变');
                                            } else {
                                                adjustments.push('');
                                            }
                                        }
                                        forecastAdjustments.push({
                                            institution: cells[0]?.textContent?.trim() || '',
                                            adjustments: adjustments
                                        });
                                    }
                                }
                                break;
                            }
                        }
                        result.forecastAdjustments = forecastAdjustments;

                        // 从 body innerText 提取各 section
                        const bodyText = document.body.innerText;
                        const lines = bodyText.split('\\n');

                        // 摘要行: 包含 "家机构" 和 "预测" 和 "截至"
                        for (const line of lines) {
                            const t = line.trim();
                            if (t.includes('家机构') && t.includes('预测') && t.includes('截至')) {
                                result.summaryText = t;
                                break;
                            }
                        }

                        // 详细指标预测 section: 从 "详细指标预测" 到 "预测数据根据"
                        let inInd = false;
                        let indLines = [];
                        for (const line of lines) {
                            const t = line.trim();
                            if (t === '详细指标预测') { inInd = true; continue; }
                            if (inInd) {
                                if (t.includes('预测数据根据') || t === '研报评级') break;
                                indLines.push(t);
                            }
                        }
                        result.indicatorsText = indLines.join('\\n');

                        // 研报评级 section: 从 "研报评级" 到 "评级根据"
                        let inRes = false;
                        let resLines = [];
                        for (const line of lines) {
                            const t = line.trim();
                            if (t === '研报评级') { inRes = true; continue; }
                            if (inRes) {
                                if (t.includes('评级根据') || t.includes('免责声明')) break;
                                resLines.push(t);
                            }
                        }
                        result.researchText = resLines.join('\\n');

                        // 研报评级分布统计: 找含"买入"/"增持"/"中性"/"减持"/"卖出"且含数字的行
                        const ratingDist = [];
                        const ratingRegex = /(买入|增持|中性|减持|卖出)\\s*[（(](\\d+)[)）]/;
                        for (const line of lines) {
                            const t = line.trim();
                            const m = t.match(ratingRegex);
                            if (m) {
                                ratingDist.push({rating: m[1], count: parseInt(m[2])});
                            }
                        }
                        result.ratingDistribution = ratingDist;

                        // 评级时间范围: 找"6个月内"或类似
                        for (const line of lines) {
                            const t = line.trim();
                            if (t.includes('个月内') && t.length < 20) {
                                result.ratingPeriod = t;
                                break;
                            }
                        }

                        // 逐条研报评级: 从 div.profit-forecast-box 提取
                        // 格式: 评级(买入/增持) + 机构：标题 + 日期 + 摘要
                        const ratingDetails = [];
                        const ratingBoxes = document.querySelectorAll('.profit-forecast-box');
                        for (const box of ratingBoxes) {
                            const boxText = box.innerText.trim();
                            const boxLines = boxText.split('\\n').map(l => l.trim()).filter(l => l);
                            let currentRating = '';
                            let currentInstitution = '';
                            let currentTitle = '';
                            for (let li = 0; li < boxLines.length; li++) {
                                const t = boxLines[li];
                                // 评级行: "买      入" / "增      持" 等（含空格）
                                const cleanRating = t.replace(/\\s+/g, '');
                                if (cleanRating === '买入' || cleanRating === '增持' || cleanRating === '中性' || cleanRating === '减持' || cleanRating === '卖出') {
                                    if (currentRating && currentInstitution) {
                                        ratingDetails.push({rating: currentRating, institution: currentInstitution, title: currentTitle, date: ''});
                                    }
                                    currentRating = cleanRating;
                                    continue;
                                }
                                // 机构+标题行: 含"："
                                if (currentRating && t.includes('：') && !t.startsWith('摘要')) {
                                    const colonIdx = t.indexOf('：');
                                    currentInstitution = t.substring(0, colonIdx).trim();
                                    currentTitle = t.substring(colonIdx + 1).trim().slice(0, 100);
                                    // 下一行可能是日期
                                    if (li + 1 < boxLines.length && /^\\d{4}-\\d{2}-\\d{2}/.test(boxLines[li + 1])) {
                                        ratingDetails.push({rating: currentRating, institution: currentInstitution, title: currentTitle, date: boxLines[li + 1].substring(0, 10)});
                                        currentRating = '';
                                        currentInstitution = '';
                                        currentTitle = '';
                                    }
                                }
                            }
                            // 处理最后一条
                            if (currentRating && currentInstitution) {
                                ratingDetails.push({rating: currentRating, institution: currentInstitution, title: currentTitle, date: ''});
                            }
                        }
                        result.ratingDetails = ratingDetails;

                        // 各指标机构明细+评级 (hidden tables: 研究机构/研究员/预测值/评级)
                        const indicatorRatings = [];
                        for (let ti = 0; ti < tables.length; ti++) {
                            const table = tables[ti];
                            if (!table || table.length < 2) continue;
                            const header = (table[0] || []).join(' ');
                            if (header.includes('研究机构') && header.includes('评级')) {
                                for (let ri = 1; ri < table.length; ri++) {
                                    const row = table[ri];
                                    if (row.length >= 4) {
                                        indicatorRatings.push({
                                            institution: row[0],
                                            analyst: row[1],
                                            value: row[2],
                                            rating: row[3],
                                        });
                                    }
                                }
                                break; // 只取第一个（营收的机构明细）
                            }
                        }
                        result.indicatorRatings = indicatorRatings;

                        return result;
                    }""")

                    tables = raw.get("tables", [])
                    summary_text = raw.get("summaryText", "")
                    indicators_text = raw.get("indicatorsText", "")
                    research_text = raw.get("researchText", "")
                    page_title = raw.get("pageTitle", "")

                    # 股票名称: 从页面标题提取 "贵州茅台(600519)..." → "贵州茅台"
                    stock_name = ""
                    if page_title:
                        stock_name = page_title.split("(")[0].strip()

                    # --- 机构覆盖摘要 ---
                    institution_count = None
                    if summary_text:
                        m = re.search(r"(\d+)\s*家机构", summary_text)
                        if m:
                            institution_count = int(m.group(1))

                    # --- Table #0/#1: EPS/NP 年度汇总 ---
                    # 两张表表头相同("预测机构数")，按出现顺序区分:
                    # 第1张 = EPS 汇总, 第2张 = 净利润汇总（同花顺页面固定布局）
                    eps_summary = []
                    np_summary = []
                    summary_table_idx = 0
                    for table in tables:
                        if not table or len(table) < 2:
                            continue
                        header = " ".join(table[0])
                        if "预测机构数" not in header:
                            continue
                        target = eps_summary if summary_table_idx == 0 else np_summary
                        summary_table_idx += 1
                        for row in table[1:]:
                            if len(row) >= 5:
                                target.append({
                                    "year": row[0],
                                    "institution_count": row[1],
                                    "min": row[2],
                                    "avg": row[3],
                                    "max": row[4],
                                    "industry_avg": row[5] if len(row) > 5 else "",
                                })

                    # --- Table #2: 机构预测明细 ---
                    # 表头含 "机构名称" 和 "研究员"，跳过前2行表头(列名+子列名)
                    # 同时合并调高/调低标记（来自 forecastAdjustments）
                    forecast_adjustments = raw.get("forecastAdjustments", [])
                    adj_map = {a["institution"]: a["adjustments"] for a in forecast_adjustments}
                    institution_forecasts = []
                    for table in tables:
                        if not table or len(table) < 3:
                            continue
                        header = " ".join(table[0])
                        if "机构名称" not in header or "研究员" not in header:
                            continue
                        for row in table[2:]:
                            if len(row) >= 8:
                                inst = row[0]
                                adj = adj_map.get(inst, [])
                                entry = {
                                    "institution": inst,
                                    "analyst": row[1],
                                    "eps_2026E": row[2],
                                    "eps_2027E": row[3],
                                    "eps_2028E": row[4],
                                    "np_2026E": row[5],
                                    "np_2027E": row[6],
                                    "np_2028E": row[7],
                                    "report_date": row[8] if len(row) > 8 else "",
                                }
                                # 合并调高/调低标记（6个值：EPS×3 + NP×3）
                                if len(adj) >= 6:
                                    entry["eps_2026E_adj"] = adj[0]
                                    entry["eps_2027E_adj"] = adj[1]
                                    entry["eps_2028E_adj"] = adj[2]
                                    entry["np_2026E_adj"] = adj[3]
                                    entry["np_2027E_adj"] = adj[4]
                                    entry["np_2028E_adj"] = adj[5]
                                institution_forecasts.append(entry)
                        break

                    # --- 详细指标预测 (text multi-line merging) ---
                    # innerText 因 rowspan 将 2026E/2027E/2028E 值拆到独立行:
                    #   营业收入(元)\t1476.94亿\t1708.99亿\t1688.38亿
                    #   1802.78亿
                    #   1895.30亿
                    #   1983.19亿
                    # 解析策略: 名称行(含tab分隔的实际值) + 后续3个非空行(预测值)
                    indicators = []
                    ind_lines = [l.strip() for l in indicators_text.split("\n") if l.strip()]
                    i = 0
                    # 跳过表头行 "预测指标\t2023（实际值）..."
                    while i < len(ind_lines):
                        if "预测指标" in ind_lines[i] and "实际值" in ind_lines[i]:
                            i += 1
                            break
                        i += 1

                    while i < len(ind_lines):
                        l = ind_lines[i]
                        parts = l.split("\t") if "\t" in l else l.split()
                        # 名称行: 第一部分非数字(是指标名), 后续部分含数字
                        if (len(parts) >= 2 and parts[0]
                                and not parts[0][0].isdigit()
                                and any(re.search(r'\d', v) for v in parts[1:])):
                            name = parts[0]
                            actual_vals = [v for v in parts[1:] if v]
                            # 收集后续3个非空行作为预测值
                            predicted_vals = []
                            j = i + 1
                            while j < len(ind_lines) and len(predicted_vals) < 3:
                                val = ind_lines[j].strip()
                                if val:
                                    predicted_vals.append(val)
                                j += 1
                            entry = {"name": name}
                            for idx, v in enumerate(actual_vals[:3]):
                                entry[["2023", "2024", "2025"][idx]] = v
                            for idx, v in enumerate(predicted_vals[:3]):
                                entry[["2026E", "2027E", "2028E"][idx]] = v
                            indicators.append(entry)
                            i = j
                        else:
                            i += 1

                    # --- 研报摘要 ---
                    research_summaries = []
                    for l in research_text.split("\n"):
                        l = l.strip()
                        if l.startswith("摘要"):
                            research_summaries.append(l[:300])

                    # --- 评级分布统计 ---
                    rating_distribution = raw.get("ratingDistribution", [])
                    rating_period = raw.get("ratingPeriod", "")

                    # --- 逐条研报评级 ---
                    rating_details = raw.get("ratingDetails", [])

                    # --- 各指标机构明细+评级 ---
                    indicator_ratings = raw.get("indicatorRatings", [])

                    result = {
                        "code": code,
                        "stock_name": stock_name,
                        "institution_count": institution_count,
                        "summary_text": summary_text,
                        "eps_summary": eps_summary,
                        "np_summary": np_summary,
                        "institution_forecasts": institution_forecasts,
                        "indicators": indicators,
                        "research_summaries": research_summaries,
                        "rating_distribution": rating_distribution,
                        "rating_period": rating_period,
                        "rating_details": rating_details,
                        "indicator_ratings": indicator_ratings,
                    }

                    return {
                        "success": True,
                        "data": result,
                        "source": "同花顺F10",
                    }
                finally:
                    try:
                        await page.close()
                    except Exception:
                        pass

        return asyncio.run(_do_query())
    except ImportError as e:
        return {"success": False, "error": f"依赖缺失: {e}"}
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ── fetch_executive_changes: 高管持股变动（东方财富 gdggcg）──
@cached(ttl=3600)
def fetch_executive_changes(code: str):
    """
    通过 playwright 访问东方财富股东高管持股页面，提取:
    1. 高管持股变动明细（日期/变动人/变动方向/变动股数/成交均价/变动金额/变动原因/变动比例/变动后持股/职务等）
    默认页面显示最近 40 条变动记录
    """
    import asyncio
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"success": False, "error": "playwright 未安装"}

    async def _do_query():
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(_WENCAI_CDP)
            try:
                ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await ctx.new_page()
                await page.set_viewport_size({"width": 1280, "height": 800})
                await page.goto(
                    f"https://data.eastmoney.com/gdggcg/ggdetail/{code}.html",
                    wait_until="domcontentloaded", timeout=20000
                )
                # 信号等待: 高管变动表数据行数稳定后再提取（渐进渲染防半成品；
                # 行数极少的股票也会快速稳定，避免无谓等待）
                _EXEC_ROWS_JS = (
                    "() => { for (const t of document.querySelectorAll('table')) {"
                    " const h = (t.rows[0]?.textContent || '') + (t.rows[1]?.textContent || '');"
                    " if (h.includes('变动人')) return t.querySelectorAll('tr').length; } return 0; }"
                )

                async def _exec_rows_settled():
                    n = await page.evaluate(_EXEC_ROWS_JS)
                    if n < 2:
                        return False
                    await page.wait_for_timeout(400)
                    return (await page.evaluate(_EXEC_ROWS_JS)) == n

                await _wait_for_ready(page, _exec_rows_settled, timeout=12.0, confirm_rounds=1)

                result = await page.evaluate("""() => {
                    const tables = document.querySelectorAll('table');
                    let targetTable = null;
                    for (const t of tables) {
                        const headerText = (t.rows[0]?.textContent || '') + (t.rows[1]?.textContent || '');
                        if (t.rows.length >= 2 && headerText.includes('变动人')) {
                            targetTable = t;
                            break;
                        }
                    }
                    if (!targetTable) return { changes: [], totalCount: 0, noData: false };

                    // 检查是否"暂无数据"
                    const bodyText = targetTable.textContent || '';
                    if (bodyText.includes('暂无数据') || bodyText.includes('暂无记录')) {
                        return { changes: [], totalCount: 0, noData: true };
                    }

                    const headerCells = Array.from(targetTable.rows[0].querySelectorAll('th, td'));
                    const headers = headerCells.map(c => c.textContent.trim().replace(/\\s+/g, ''));

                    const changes = [];
                    for (let i = 1; i < targetTable.rows.length; i++) {
                        const cells = Array.from(targetTable.rows[i].querySelectorAll('td')).map(c => c.textContent.trim());
                        if (cells.length < 5) continue;
                        const entry = {};
                        for (let j = 0; j < headers.length && j < cells.length; j++) {
                            entry[headers[j]] = cells[j];
                        }
                        if (entry['日期'] || entry['变动人']) {
                            changes.push(entry);
                        }
                    }
                    return { changes: changes, totalCount: changes.length, noData: false };
                }""")

                changes = result.get("changes", [])
                no_data = result.get("noData", False)
                if not changes:
                    if no_data:
                        return {"success": True, "data": {"code": code, "changes": [], "totalCount": 0, "noData": True}, "source": "东方财富"}
                    return {"success": False, "error": f"gdggcg 页面无 {code} 高管持股变动数据"}

                return {
                    "success": True,
                    "data": {
                        "code": code,
                        "changes": changes,
                        "totalCount": result.get("totalCount", len(changes)),
                    },
                    "source": "东方财富",
                }

            finally:
                try:
                    await page.close()
                except Exception:
                    pass

    try:
        return asyncio.run(_do_query())
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ── fetch_company_events: 公司大事（同花顺 F10 event.html）──
@cached(ttl=3600)
def fetch_company_events(code: str):
    """
    通过 playwright 访问同花顺 F10 event 页面，提取:
    1. 近期重要事件（日期+事件类型+描述，含财报披露/公告/融资融券/大宗交易/业绩披露/股东大会/分红/回购等）
    2. 高管持股变动（变动日期/变动人/与公司高管关系/变动数量/交易均价/剩余股数/股份变动途径）
    3. 股东持股变动（公告日期/变动股东/变动数量/交易均价/剩余股份总数/变动期间/变动途径）
    4. 担保明细（序号/担保金额/币种/担保期限/担保方/担保类型/被担保方）
    5. 违规处理（公告日期/处罚金额/处罚类型/处理人/处罚原因）
    """
    import asyncio
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"success": False, "error": "playwright 未安装"}

    async def _do_query():
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(_WENCAI_CDP)
            try:
                ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await ctx.new_page()
                await page.set_viewport_size({"width": 1280, "height": 800})
                await page.goto(
                    f"https://basic.10jqka.com.cn/{code}/event.html",
                    wait_until="domcontentloaded", timeout=20000
                )
                # 信号等待: 事件表格+章节标题渲染完成（section 分类依赖 h2）
                await _wait_for_ready(
                    page,
                    lambda: page.evaluate(
                        "() => document.querySelectorAll('table').length >= 2 && document.querySelectorAll('h2').length >= 1"
                    ),
                    timeout=12.0,
                )

                result = await page.evaluate("""() => {
                    const tables = document.querySelectorAll('table');
                    const out = {
                        events: [],
                        executive_changes: [],
                        shareholder_changes: [],
                        guarantees: [],
                        violations: [],
                        research_visits: []
                    };

                    // Helper: extract rows from table as array of cell-text arrays
                    function extractRows(table) {
                        const rows = [];
                        for (const tr of table.querySelectorAll('tr')) {
                            const cells = Array.from(tr.querySelectorAll('td, th')).map(c => c.textContent.trim());
                            if (cells.length > 0) rows.push(cells);
                        }
                        return rows;
                    }

                    // Classify tables by their section heading
                    const allH2 = document.querySelectorAll('h2');
                    const h2List = Array.from(allH2);
                    function sectionOf(table) {
                        let section = '';
                        for (const h of h2List) {
                            if (h.compareDocumentPosition(table) & Node.DOCUMENT_POSITION_FOLLOWING) {
                                section = h.textContent.trim();
                            } else {
                                break;
                            }
                        }
                        return section;
                    }

                    for (let i = 0; i < tables.length; i++) {
                        const table = tables[i];
                        const section = sectionOf(table);
                        const rows = extractRows(table);
                        if (rows.length < 1) continue;

                        // 1. 近期重要事件: section含"近期重要事件"且2列(日期+描述)，无表头行
                        // 注意: 页面可能将"今天"和"历史"拆成多个 table
                        if (section.includes('近期重要事件') && rows[0].length === 2) {
                            for (let r = 0; r < rows.length; r++) {
                                if (rows[r].length >= 2) {
                                    out.events.push({
                                        date: rows[r][0],
                                        description: rows[r][1].replace(/\\n/g, ' ').replace(/\\s+/g, ' ').trim().slice(0, 200)
                                    });
                                }
                            }
                        }

                        // 2. 高管持股变动: header含"变动日期"+"变动人"
                        else if (section.includes('高管持股变动') || (rows[0].join('').includes('变动日期') && rows[0].join('').includes('变动人') && rows[0].join('').includes('高管关系'))) {
                            const headers = rows[0];
                            for (let r = 1; r < rows.length; r++) {
                                if (rows[r].length >= 6) {
                                    out.executive_changes.push({
                                        date: rows[r][0],
                                        person: rows[r][1],
                                        relationship: rows[r][2],
                                        change: rows[r][3].replace(/\\s+/g, ' ').trim(),
                                        price: rows[r][4],
                                        remaining: rows[r][5],
                                        method: rows[r][6] || ''
                                    });
                                }
                            }
                        }

                        // 3. 股东持股变动: header含"变动股东"
                        else if (section.includes('股东持股变动') || rows[0].join('').includes('变动股东')) {
                            const headers = rows[0];
                            for (let r = 1; r < rows.length; r++) {
                                if (rows[r].length >= 5) {
                                    out.shareholder_changes.push({
                                        announcement_date: rows[r][0],
                                        shareholder: rows[r][1] || '',
                                        change: (rows[r][2] || '').replace(/\\s+/g, ' ').trim(),
                                        price: rows[r][3] || '',
                                        remaining: rows[r][4] || '',
                                        period: rows[r][5] || '',
                                        method: rows[r][6] || ''
                                    });
                                }
                            }
                        }

                        // 4. 担保明细: header含"担保金额"
                        else if (section.includes('担保明细') || rows[0].join('').includes('担保金额')) {
                            const cells0 = rows[0];
                            const cells1 = rows.length > 1 ? rows[1] : [];
                            const allCells = [...cells0, ...cells1];
                            const guarantee = {};
                            for (let c = 0; c < allCells.length; c++) {
                                const t = allCells[c];
                                if (t.includes('序') && t.includes('号')) guarantee.seq = t;
                                else if (t.includes('担保金额')) guarantee.amount = t;
                                else if (t.includes('币种')) guarantee.currency = t;
                                else if (t.includes('担保期限')) guarantee.period = t;
                                else if (t.includes('担') && t.includes('保') && t.includes('方')) guarantee.guarantor = t;
                                else if (t.includes('担保类型')) guarantee.type = t;
                                else if (t.includes('被担保方')) guarantee.guaranteed = t;
                            }
                            if (Object.keys(guarantee).length > 0) out.guarantees.push(guarantee);
                        }

                        // 5. 违规处理: header含"处罚"，数据跨多行
                        else if (section.includes('违规处理') || rows[0].join('').includes('处罚')) {
                            const violation = {};
                            for (let r = 0; r < rows.length; r++) {
                                for (let c = 0; c < rows[r].length; c++) {
                                    const t = rows[r][c];
                                    if (t.includes('公告日期')) violation.date = t;
                                    else if (t.includes('处罚金额')) violation.fine = t;
                                    else if (t.includes('处罚类型')) violation.type = t;
                                    else if (t.includes('处理人')) violation.handler = t;
                                    else if (t.includes('处罚对象')) violation.target = t;
                                    else if (t.includes('违规行为')) violation.reason = t;
                                    else if (t.includes('处罚说明')) violation.description = t.replace(/\\n/g, ' ').replace(/\\s+/g, ' ').trim().slice(0, 200);
                                }
                            }
                            if (Object.keys(violation).length > 0) out.violations.push(violation);
                        }

                        // 6. 机构调研: header含"机构类别"+"调研机构"
                        else if (section.includes('机构调研') || rows[0].join('').includes('机构类别')) {
                            for (let r = 1; r < rows.length; r++) {
                                if (rows[r].length >= 2) {
                                    const category = rows[r][0];
                                    const institutions = rows[r][1].replace(/查看更多|收起更多/g, '').replace(/\\n/g, ' ').replace(/\\s+/g, ' ').trim();
                                    if (category && institutions) {
                                        out.research_visits.push({
                                            category: category,
                                            institutions: institutions.slice(0, 300)
                                        });
                                    }
                                }
                            }
                        }
                    }

                    return out;
                }""")

                # 检查是否有数据
                total = (len(result.get("events", [])) + len(result.get("executive_changes", [])) +
                         len(result.get("shareholder_changes", [])) + len(result.get("guarantees", [])) +
                         len(result.get("violations", [])) + len(result.get("research_visits", [])))
                if total == 0:
                    return {"success": False, "error": f"event.html 无 {code} 数据"}

                return {
                    "success": True,
                    "data": {
                        "code": code,
                        **result,
                    },
                    "source": "同花顺F10",
                }

            finally:
                try:
                    await page.close()
                except Exception:
                    pass

    try:
        return asyncio.run(_do_query())
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ── fetch_stock_dividend: 分红融资（同花顺 F10 astockpc SPA #/bonus）──
# 新版页面为 JS 渲染，真实数据来自 basicapi REST 接口。通过 playwright 打开
# SPA 页面并拦截 basicapi 响应（直连同接口亦可，此处以渲染方式保证稳定性）。


def _infer_marketid(code: str) -> int:
    """按 6 位代码前缀推断同花顺 marketid（新版 astockpc F10 URL 参数）。

    实测验证: 60/68(沪A+科创)→17, 90(沪B)→18, 00/30(深A)→33,
    20(深B)→34, 8/4(北交所)→151
    """
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


@cached(ttl=3600)
def fetch_stock_dividend(code: str, market: str = "", name: str = ""):
    """
    通过 playwright 访问同花顺新版 F10 分红融资页（astockpc SPA #/bonus），
    拦截 basicapi REST 响应提取:
    1. programme           分红方案历史（报告期/董事会/股东大会预案/实施公告/
                           股权登记日/除权除息日/方案/分红总额/进度/股利支付率/分配对象）
    2. label               分红诊断（送转潜力/派现概率等标签）
    3. share_info          股票基础信息（ths_code/上市日期）
    4. additional          增发概况+明细
    5. allotment           配股概况+明细
    6. org_allocated_detail 增发机构获配明细
    7. dividend_ratio      近三年分红比率
    market 未提供时按代码前缀推断（60/68→17, 90→18, 00/30→33, 20→34, 8/4→151）。
    """
    import asyncio
    import json
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"success": False, "error": "playwright 未安装"}

    if not market:
        market = str(_infer_marketid(code))

    async def _do_query():
        from urllib.parse import quote

        name_q = quote(name or code)
        url = (
            "https://basic.10jqka.com.cn/astockpc/astockmain/index.html"
            f"#/bonus?code={code}&marketid={market}&code_name={name_q}"
        )
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(_WENCAI_CDP)
            try:
                ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await ctx.new_page()
                await page.set_viewport_size({"width": 1280, "height": 900})

                captured = {}
                api_prefix = "https://basic.10jqka.com.cn/basicapi"

                async def on_response(resp):
                    u = resp.url
                    key = None
                    if "dividend_ratio" in u:
                        key = "dividend_ratio"
                    elif u.startswith(api_prefix):
                        if "/finance/dividends/v1/programme" in u:
                            key = "programme"
                        elif "/finance/dividends/v1/label" in u:
                            key = "label"
                        elif "/component/share/v1/share_info" in u:
                            key = "share_info"
                        elif "/finance/financing/v1/additional" in u:
                            key = "additional"
                        elif "/finance/financing/v1/allotment" in u:
                            key = "allotment"
                        elif "/finance/financing/v1/org_allocated_detail" in u:
                            key = "org_allocated_detail"
                    if key and key not in captured:
                        try:
                            body = await resp.text()
                            try:
                                captured[key] = json.loads(body)
                            except Exception:
                                captured[key] = {"_raw": body[:2000]}
                        except Exception:
                            pass

                page.on("response", on_response)
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                # 信号等待: 分红模块渲染完成
                await _wait_for_ready(
                    page,
                    lambda: page.evaluate(
                        "() => !!(document.body && document.body.innerText.includes('分红'))"
                    ),
                    timeout=15.0,
                )
                await asyncio.sleep(2)  # 等全部 basicapi 响应到齐

                if not captured.get("programme"):
                    text = ""
                    try:
                        text = await page.evaluate("() => document.body.innerText")
                    except Exception:
                        pass
                    return {
                        "success": False,
                        "error": "未拦截到分红数据接口响应",
                        "page_preview": text[:300],
                    }

                data = {"code": code, "marketid": market}
                data.update(captured)
                return {"success": True, "data": data, "source": "同花顺F10(astockpc)"}
            finally:
                try:
                    await page.close()
                except Exception:
                    pass

    try:
        return asyncio.run(_do_query())
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ── fetch_stock_news_f10: 新闻公告（同花顺 F10 news.html）──
# 新闻列表走 basicapi/notice/news 接口（页面内拦截），研报列表为页面渲染表格。

@cached(ttl=3600)
def fetch_stock_news_f10(code: str, limit: int = 15):
    """
    通过 playwright 访问同花顺 F10 news 页面（news.html），提取:
    1. 新闻列表（拦截 basicapi/notice/news: 标题/来源/作者/日期/链接）
    2. 研报列表（页面表格: 评级/研报标题/机构/报告日期）
    """
    import asyncio
    import json
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"success": False, "error": "playwright 未安装"}

    async def _do_query():
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(_WENCAI_CDP)
            try:
                ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await ctx.new_page()
                await page.set_viewport_size({"width": 1280, "height": 900})
                url = f"https://basic.10jqka.com.cn/{code}/news.html"
                captured = {"news": None}

                async def on_response(resp):
                    u = resp.url
                    if "basicapi/notice/news" in u and captured["news"] is None:
                        try:
                            captured["news"] = json.loads(await resp.text())
                        except Exception:
                            pass

                page.on("response", on_response)
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                # 信号等待: 公告/研报板块渲染完成
                await _wait_for_ready(
                    page,
                    lambda: page.evaluate(
                        "() => !!(document.body && (document.body.innerText.includes('公告列表') "
                        "|| document.body.innerText.includes('研报列表')))"
                    ),
                    timeout=15.0,
                )
                await asyncio.sleep(2)  # 等新闻接口响应到齐

                # 研报表: 表头含"评级"（评级/研报标题/机构/报告日期）
                research = await page.evaluate(
                    """() => {
                        const out = [];
                        for (const t of document.querySelectorAll('table')) {
                            const head = (t.innerText || '').slice(0, 40);
                            if (!head.includes('评级')) continue;
                            const rows = t.querySelectorAll('tr');
                            for (let i = 1; i < rows.length && out.length < 20; i++) {
                                const cells = Array.from(rows[i].querySelectorAll('td, th'))
                                    .map(c => (c.textContent || '').trim());
                                if (cells.length >= 4) {
                                    out.push({rating: cells[0], report: cells[1],
                                              institution: cells[2], date: cells[3]});
                                }
                            }
                        }
                        return out;
                    }"""
                )

                if captured["news"] is None:
                    return {"success": False, "error": "未拦截到新闻接口响应"}

                nd = captured["news"].get("data", {})
                news_items = []
                for it in (nd.get("data") or [])[:limit]:
                    news_items.append({
                        "seq": it.get("seq"),
                        "title": it.get("title"),
                        "date": it.get("date"),
                        "source": it.get("source"),
                        "author": it.get("author"),
                        "url": it.get("pc_url") or it.get("mobile_url") or it.get("client_url"),
                    })
                return {
                    "success": True,
                    "data": {
                        "code": code,
                        "total": nd.get("total"),
                        "news": news_items,
                        "research_reports": research,
                    },
                    "source": "同花顺F10",
                }
            finally:
                try:
                    await page.close()
                except Exception:
                    pass

    try:
        return asyncio.run(_do_query())
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ── fetch_industry_hotmap: 大盘星图行业热力 (东财 stockhotmap) ──
# 全市场个股 → 三级行业归属聚合。数据源: quote.eastmoney.com/stockhotmap 页面的
# getmcode/getquotebasedata/getquotedata 三个 API（绕过 push2 风控），其中约 5%
# 个股行经过 AES 加密，需在页面内用 CryptoJS 解密（密钥 = Datas 头 + mcode 尾段）。
# 注意: 全量聚合数据(重)与 ticker 定位(轻)解耦——数据本体按 (level, top_n) 缓存,
# ticker 不参与缓存键, 避免不同 analyst 传不同 ticker 导致重复爬取同一份大盘星图。

def _hotmap_locate_ticker(
    ticker: str,
    stock_bk: dict,
    bk_list: dict,
    industries: list,
    level: str,
) -> dict | None:
    """在已聚合的 industries 上定位目标股票所属行业(纯计算, 不爬取)。"""
    tcode = str(ticker).strip()
    if not (tcode.isdigit() and len(tcode) == 6):
        return None
    market = "1" if tcode.startswith(("6", "9")) else "0"
    tidx = stock_bk.get((market, tcode))
    if tidx is None:
        for m in ("0", "1"):
            if (m, tcode) in stock_bk:
                tidx = stock_bk[(m, tcode)]
                break
    if tidx is None or tidx >= len(bk_list[level]):
        return None
    tname = bk_list[level][tidx][0]
    ind = next((i for i in industries if i["name"] == tname), None)
    rank = industries.index(ind) + 1 if ind is not None else None
    return {
        "code": tcode,
        "industry": tname,
        "industry_code": (
            bk_list[level][tidx][2] if len(bk_list[level][tidx]) > 2 else ""
        ),
        "rank": rank,
        "total": len(industries),
        "count": ind["count"] if ind else None,
        "up": ind["up"] if ind else None,
        "down": ind["down"] if ind else None,
        "chg": ind["chg"] if ind else None,
        "zljzb": ind["zljzb"] if ind else None,
        "turnover": ind["turnover"] if ind else None,
    }


@cached(ttl=600)
def _fetch_industry_hotmap_core(level: str = "bk2", top_n: int = 20):
    """大盘星图行业热力全量聚合数据(按 level/top_n 缓存, 不含 ticker)。

    与 fetch_industry_hotmap 解耦: ticker 定位是纯计算, 不参与缓存键,
    同一份全量数据可服务任意目标股票, 避免跨 analyst 重复爬取。
    """
    import asyncio
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"success": False, "error": "playwright 未安装"}

    async def _do_query():
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(_WENCAI_CDP)
            try:
                ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await ctx.new_page()
                await page.set_viewport_size({"width": 1280, "height": 900})
                await page.goto(
                    "https://quote.eastmoney.com/stockhotmap/",
                    wait_until="domcontentloaded", timeout=20000,
                )
                # 信号等待: SPA 加载完成（内部 fetch 解密依赖 CryptoJS 库）
                await _wait_for_ready(
                    page,
                    lambda: page.evaluate(
                        "() => !!(window.CryptoJS && document.body && document.body.innerText.length)"
                    ),
                    timeout=8.0,
                    poll_ms=200,
                )

                payload = await page.evaluate("""async () => {
                    const mcodeResp = await fetch('api/getmcode', {method:'POST'});
                    const {mcode} = await mcodeResp.json();
                    const baseResp = await fetch('api/getquotebasedata');
                    const base = await baseResp.json();
                    const quoteResp = await fetch('api/getquotedata?' +
                        new URLSearchParams({quotedata_hash: base.hash || ''}));
                    const datas = quoteResp.headers.get('Datas') || '';
                    const quote = await quoteResp.json();
                    const encRows = quote.data.filter(r => r.indexOf('|') < 0);
                    const key = datas + mcode.split('|').slice(1).join('|');
                    const decrypted = encRows.map(e => {
                        try { return CryptoJS.AES.decrypt(e, key).toString(CryptoJS.enc.Utf8); }
                        catch (err) { return null; }
                    });
                    return { base, quote, decrypted };
                }""")

                base = payload.get("base", {})
                quote = payload.get("quote", {})
                decrypted = payload.get("decrypted", [])
                if not base or not quote:
                    return {"success": False, "error": "大盘星图数据为空"}

                def _parse(row):
                    o = row.split("|")
                    if len(o) < 17:
                        return None
                    try:
                        zdf = float(o[3]) / 100.0 if o[3] not in ("-", "") else None
                        ltsz = float(o[13]) if o[13] not in ("-", "") else None
                        hsl = float(o[11]) / 100.0 if o[11] not in ("-", "") else None
                        zljzb = float(o[16]) / 100.0 if o[16] not in ("-", "") else None
                    except (ValueError, TypeError):
                        return None
                    return {"market": o[0], "code": o[1], "zdf": zdf,
                            "ltsz": ltsz, "hsl": hsl, "zljzb": zljzb}

                # merge plaintext + decrypted rows by market|code
                merged = {}
                for row in quote.get("data", []):
                    r = _parse(row)
                    if r:
                        merged[(r["market"], r["code"])] = r
                for drow in decrypted:
                    if drow:
                        r = _parse(drow)
                        if r:
                            merged[(r["market"], r["code"])] = r

                # baseinfo: bk1_idx|bk2_idx|bk3_idx|name|market|code|labels
                bk_idx = {"bk1": 0, "bk2": 1, "bk3": 2}[level]
                bk_list = {k: [x.split("|") for x in base.get(k, [])]
                           for k in ("bk1", "bk2", "bk3")}
                stock_bk = {}
                for row in base.get("baseinfo", []):
                    p = row.split("|")
                    if len(p) >= 7:
                        stock_bk[(p[4], p[5])] = int(p[bk_idx])

                from collections import defaultdict
                agg = defaultdict(lambda: {"n": 0, "up": 0, "down": 0,
                                           "chg_w": 0.0, "ltsz": 0.0,
                                           "zljzb_sum": 0.0, "zljzb_n": 0,
                                           "hsl_sum": 0.0, "hsl_n": 0,
                                           "leader": None, "leader_zdf": -999.0,
                                           "lagger": None, "lagger_zdf": 999.0})
                for (mkt, code), r in merged.items():
                    idx = stock_bk.get((mkt, code))
                    if idx is None:
                        continue
                    g = agg[idx]
                    g["n"] += 1
                    if r["zdf"] is not None:
                        if r["zdf"] > 0:
                            g["up"] += 1
                        elif r["zdf"] < 0:
                            g["down"] += 1
                        if r["ltsz"]:
                            g["chg_w"] += r["zdf"] * r["ltsz"]
                            g["ltsz"] += r["ltsz"]
                        if r["zdf"] > g["leader_zdf"]:
                            g["leader_zdf"] = r["zdf"]
                            g["leader"] = f"{r['code']} {r['zdf']:+.2f}%"
                        if r["zdf"] < g["lagger_zdf"]:
                            g["lagger_zdf"] = r["zdf"]
                            g["lagger"] = f"{r['code']} {r['zdf']:+.2f}%"
                    if r["zljzb"] is not None:
                        g["zljzb_sum"] += r["zljzb"]
                        g["zljzb_n"] += 1
                    if r["hsl"] is not None:
                        g["hsl_sum"] += r["hsl"]
                        g["hsl_n"] += 1

                industries = []
                for idx, g in agg.items():
                    if idx >= len(bk_list[level]):
                        continue
                    name = bk_list[level][idx][0]
                    industries.append({
                        "name": name,
                        "code": bk_list[level][idx][2] if len(bk_list[level][idx]) > 2 else "",
                        "count": g["n"],
                        "up": g["up"],
                        "down": g["down"],
                        "chg": round(g["chg_w"] / g["ltsz"], 2) if g["ltsz"] else None,
                        "zljzb": round(g["zljzb_sum"] / g["zljzb_n"], 2) if g["zljzb_n"] else None,
                        "turnover": round(g["hsl_sum"] / g["hsl_n"], 2) if g["hsl_n"] else None,
                        "leader": g["leader"],
                        "lagger": g["lagger"],
                    })
                industries.sort(key=lambda x: -(x["chg"] if x["chg"] is not None else -999))

                ret = {
                    "success": True,
                    "level": level,
                    "top_n": top_n,
                    "total_industries": len(industries),
                    "quotetime": quote.get("quotetime"),
                    "source": "东财大盘星图",
                }
                ret["top"] = industries[:top_n]
                ret["bottom"] = industries[-top_n:] if len(industries) > top_n * 2 else []
                # 供外层 ticker 定位复用（不参与返回给调用方）
                ret["_industries"] = industries
                ret["_stock_bk"] = stock_bk
                ret["_bk_list"] = bk_list
                return ret

            finally:
                try:
                    await page.close()
                except Exception:
                    pass

    try:
        return asyncio.run(_do_query())
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


def fetch_industry_hotmap(level: str = "bk2", top_n: int = 20, ticker: str = ""):
    """获取大盘星图行业热力数据（全市场个股按三级行业聚合）。

    level: bk1(一级行业) / bk2(二级行业) / bk3(三级行业)
    top_n: 返回涨跌幅加权前 top_n 与后 top_n 个行业（默认 20）
    ticker: 可选目标股票 6 位代码，返回其所属行业定位（行业名/排名/聚合数据）。
            仅参与结果组装, 不参与缓存键, 同一份全量数据可服务不同 ticker。

    Returns per-industry: 行业名/代码、个股数、上涨/下跌家数、
    流通市值加权涨跌幅(近似)、主力净占比均值、换手率均值、领涨/领跌股。
    """
    if level not in ("bk1", "bk2", "bk3"):
        level = "bk2"
    try:
        top_n = max(1, min(int(top_n), 100))
    except (ValueError, TypeError):
        top_n = 20

    ret = _fetch_industry_hotmap_core(level, top_n)
    if not ret.get("success"):
        return ret

    # 目标股票行业定位: 纯计算复用 core 的全量数据, 不触发重新爬取。
    # 注意: 缓存对象不可修改(多调用方共享), 先浅拷贝再组装 target。
    import copy
    ret = copy.copy(ret)
    industries = ret.pop("_industries", None)
    stock_bk = ret.pop("_stock_bk", None)
    bk_list = ret.pop("_bk_list", None)
    if ticker and industries is not None and stock_bk is not None and bk_list is not None:
        target_info = _hotmap_locate_ticker(ticker, stock_bk, bk_list, industries, level)
        if target_info:
            ret["target"] = target_info
    return ret


@cached(ttl=600)
def fetch_industry_board(top_n: int = 20):
    """获取东财官方行业板块排名（Playwright 爬取 gridlist#industry_board_2 页面）。

    数据源: https://quote.eastmoney.com/center/gridlist.html#industry_board_2
    页面按涨跌幅降序渲染全部行业板块（约128个），每页20行，底部 qtpager 翻页。
    字段: 排名/板块名/最新价/涨跌额/涨跌幅/总市值/换手率/上涨家数/下跌家数/领涨股。
    """
    import asyncio
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"success": False, "error": "playwright 未安装"}

    try:
        top_n = max(1, min(int(top_n), 100))
    except (ValueError, TypeError):
        top_n = 20

    PER_PAGE = 20

    async def _do_query():
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(_WENCAI_CDP)
            try:
                ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await ctx.new_page()
                try:
                    await page.set_viewport_size({"width": 1400, "height": 900})
                except Exception:
                    pass
                try:
                    await page.goto(
                        "https://quote.eastmoney.com/center/gridlist.html#industry_board_2",
                        wait_until="domcontentloaded", timeout=20000,
                    )
                except Exception:
                    pass
                try:
                    await page.wait_for_selector(".quotetable tbody tr", timeout=15000)
                except Exception as e:
                    return {"success": False, "error": f"行业板块表格未渲染: {e}"}

                # 表格快照: 行数|首行rank|首行名称|末行rank|末行名称
                SNAPSHOT_JS = """() => {
                    const trs = document.querySelectorAll('.quotetable tbody tr');
                    if (!trs.length) return '';
                    const c = (tds) => tds.length
                        ? tds[0].textContent.trim() + '|' + tds[1].textContent.trim()
                        : '';
                    return trs.length + '|' + c(trs[0].querySelectorAll('td'))
                                    + '|' + c(trs[trs.length - 1].querySelectorAll('td'));
                }"""

                async def _wait_page_settled(expected_rank, timeout=12.0):
                    """等待表格渲染稳定: 首行rank匹配, 且间隔350ms两次快照完全一致。

                    SPA 翻页时 rank 列先按 pageIndex 刷新、行内容后渲染,
                    只查首行 rank 会读到"旧内容+新排名"的脏数据, 必须等快照稳定。
                    """
                    import time as _t
                    deadline = _t.time() + timeout
                    while _t.time() < deadline:
                        sig = await page.evaluate(SNAPSHOT_JS)
                        if sig and sig.split("|")[1] == str(expected_rank):
                            await page.wait_for_timeout(350)
                            sig2 = await page.evaluate(SNAPSHOT_JS)
                            if sig2 == sig:
                                return True
                        else:
                            await page.wait_for_timeout(350)
                    return False

                total_pages = await page.evaluate("""() => {
                    const links = Array.from(document.querySelectorAll('.qtpager a'));
                    const nums = links.map(a => parseInt(a.textContent)).filter(n => !isNaN(n));
                    return nums.length ? Math.max(...nums) : 1;
                }""")

                items = []
                seen_names = set()
                MAX_READ_TRIES = 3

                for pn in range(1, total_pages + 1):
                    if pn > 1:
                        try:
                            await page.click(f'.qtpager a:text-is("{pn}")', timeout=3000)
                        except Exception:
                            try:
                                await page.fill('.qtpager .gotoform input[type="text"]', str(pn))
                                await page.click('.qtpager .gotoform input[type="submit"]')
                            except Exception:
                                pass

                    rows = []
                    ok = False
                    for _try in range(MAX_READ_TRIES):
                        if not await _wait_page_settled((pn - 1) * PER_PAGE + 1):
                            if _try == MAX_READ_TRIES - 1:
                                return {"success": False, "error": f"翻页到第{pn}页后表格未稳定"}
                            continue
                        rows = await page.evaluate("""() => {
                            const num = (td) => {
                                const t = td ? td.textContent.trim() : '';
                                const m = t.match(/-?[0-9.]+/);
                                return m ? parseFloat(m[0]) : null;
                            };
                            return Array.from(document.querySelectorAll('.quotetable tbody tr')).map(tr => {
                                const tds = Array.from(tr.querySelectorAll('td'));
                                const nameA = tds[1] ? tds[1].querySelector('a') : null;
                                const name = nameA ? nameA.textContent.trim()
                                                    : (tds[1] ? tds[1].textContent.trim() : '');
                                const href = nameA ? (nameA.getAttribute('href') || '') : '';
                                const m = href.match(/BK\\d+/);
                                return {
                                    rank: num(tds[0]),
                                    code: m ? m[0] : '',
                                    name: name,
                                    price: num(tds[3]),
                                    change: num(tds[4]),
                                    chg: num(tds[5]),
                                    mktcap: tds[6] ? tds[6].textContent.trim() : '',
                                    turnover: num(tds[7]),
                                    up: num(tds[8]),
                                    down: num(tds[9]),
                                    leader_name: tds[10] ? tds[10].textContent.trim() : '',
                                    leader_chg: num(tds[11]),
                                };
                            });
                        }""")
                        names = [r.get("name", "") for r in rows]
                        dup_names = [n for n in names if n in seen_names]
                        if rows and not dup_names:
                            ok = True
                            break
                        # 翻页竞态脏数据(旧行内容+新rank列), 等渲染完成后重读
                        await page.wait_for_timeout(1500)
                    if not ok:
                        return {"success": False, "error": f"第{pn}页数据校验失败(重复板块名称)"}
                    seen_names.update(r.get("name", "") for r in rows)
                    items.extend(rows)

                if not items:
                    return {"success": False, "error": "行业板块表格无数据行"}

                # 兜底去重: 防止极端情况下读到重渲染残留
                seen = set()
                dedup = []
                for it in items:
                    if it["rank"] in seen:
                        continue
                    seen.add(it["rank"])
                    dedup.append(it)
                items = dedup
                items.sort(key=lambda x: x["rank"] if x["rank"] is not None else 99999)
                return {
                    "success": True,
                    "total_industries": len(items),
                    "top_n": top_n,
                    "source": "东财行业板块页面 gridlist#industry_board_2 (Playwright)",
                    "top": items[:top_n],
                    "bottom": items[-top_n:] if len(items) > top_n * 2 else [],
                }

            finally:
                try:
                    await page.close()
                except Exception:
                    pass

    try:
        return asyncio.run(_do_query())
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


@cached(ttl=180)
def fetch_global_news_cls(limit: int = 20):
    """财联社电报全球快讯（Playwright 爬取）。

    数据源: https://www.cls.cn/telegraph
    财联社电报页会请求带 sign 签名的 api/cache 接口（直接 HTTP 已 404），
    通过拦截页面响应获取 roll_data。
    字段: title/brief/content/ctime(unix秒)/level(A/B/C重要度)。
    """
    import asyncio
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"success": False, "error": "playwright 未安装"}

    try:
        limit = max(1, min(int(limit), 50))
    except (ValueError, TypeError):
        limit = 20

    async def _do_query():
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(_WENCAI_CDP)
            try:
                ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await ctx.new_page()
                try:
                    try:
                        await page.set_viewport_size({"width": 1400, "height": 900})
                    except Exception:
                        pass
                    captured = []

                    async def _on_resp(resp):
                        if "api/cache" in resp.url and "telegraph" in resp.url:
                            try:
                                body = await resp.text()
                                data = json.loads(body)
                                roll = (data.get("data") or {}).get("roll_data") or []
                                if roll:
                                    captured.append(roll)
                            except Exception:
                                pass

                    page.on("response", lambda r: asyncio.create_task(_on_resp(r)))
                    try:
                        await page.goto(
                            "https://www.cls.cn/telegraph",
                            wait_until="domcontentloaded", timeout=20000,
                        )
                    except Exception:
                        pass
                    # 等待拦截到数据（最长 15s）
                    deadline = time.time() + 15
                    while not captured and time.time() < deadline:
                        await page.wait_for_timeout(500)

                    if not captured:
                        return {"success": False, "error": "未捕获到财联社电报数据 (api/cache 未返回)"}

                    roll = captured[0]
                    items = []
                    for it in roll:
                        title = (it.get("title") or "").strip() or (it.get("brief") or "").strip()
                        if not title:
                            continue
                        ctime = it.get("ctime", "")
                        pub_time = ""
                        if ctime:
                            try:
                                pub_time = datetime.fromtimestamp(int(ctime)).strftime("%Y-%m-%d %H:%M")
                            except (ValueError, TypeError, OSError):
                                pub_time = str(ctime)
                        items.append({
                            "title": title,
                            "content": (it.get("content") or "").strip() or (it.get("brief") or "").strip(),
                            "time": pub_time,
                            "ctime": ctime,
                            "level": it.get("level", ""),
                            "source": "CLS Wire",
                        })
                    if not items:
                        return {"success": False, "error": "财联社电报无数据条目"}
                    return {
                        "success": True,
                        "data": items[:limit],
                        "total": len(roll),
                        "limit": limit,
                        "source": "财联社电报 cls.cn/telegraph (Playwright)",
                    }
                finally:
                    try:
                        await page.close()
                    except Exception:
                        pass
            except Exception as e:
                return {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}

    try:
        return asyncio.run(_do_query())
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


@cached(ttl=180)
def fetch_global_news_em(limit: int = 20):
    """东方财富 7x24 全球快讯（Playwright 爬取）。

    数据源: https://kuaixun.eastmoney.com/
    页面请求 np-weblist.eastmoney.com/comm/web/getFastNewsList（JSONP 格式），
    通过拦截页面响应剥壳解析 fastNewsList。
    字段: title/summary/showTime。
    """
    import asyncio
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"success": False, "error": "playwright 未安装"}

    try:
        limit = max(1, min(int(limit), 50))
    except (ValueError, TypeError):
        limit = 20

    async def _do_query():
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(_WENCAI_CDP)
            try:
                ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await ctx.new_page()
                try:
                    try:
                        await page.set_viewport_size({"width": 1400, "height": 900})
                    except Exception:
                        pass
                    captured = []

                    async def _on_resp(resp):
                        if "getFastNewsList" in resp.url:
                            try:
                                body = await resp.text()
                                # JSONP 剥壳: jQueryxxx({...})
                                if body.startswith("jQuery"):
                                    body = body[body.index("(") + 1: body.rindex(")")]
                                data = json.loads(body)
                                items = (data.get("data") or {}).get("fastNewsList") or []
                                if items:
                                    captured.append(items)
                            except Exception:
                                pass

                    page.on("response", lambda r: asyncio.create_task(_on_resp(r)))
                    try:
                        await page.goto(
                            "https://kuaixun.eastmoney.com/",
                            wait_until="domcontentloaded", timeout=20000,
                        )
                    except Exception:
                        pass
                    # 等待拦截到数据（最长 15s）
                    deadline = time.time() + 15
                    while not captured and time.time() < deadline:
                        await page.wait_for_timeout(500)

                    if not captured:
                        return {"success": False, "error": "未捕获到东财7x24快讯数据 (getFastNewsList 未返回)"}

                    items = []
                    for it in captured[0]:
                        title = (it.get("title") or "").strip()
                        if not title:
                            continue
                        items.append({
                            "title": title,
                            "content": (it.get("summary") or "").strip(),
                            "time": it.get("showTime", ""),
                            "source": "Eastmoney Global",
                        })
                    if not items:
                        return {"success": False, "error": "东财7x24快讯无数据条目"}
                    return {
                        "success": True,
                        "data": items[:limit],
                        "total": len(captured[0]),
                        "limit": limit,
                        "source": "东财7x24 kuaixun.eastmoney.com (Playwright)",
                    }
                finally:
                    try:
                        await page.close()
                    except Exception:
                        pass
            except Exception as e:
                return {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}

    try:
        return asyncio.run(_do_query())
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


@cached(ttl=180)
def fetch_stock_news_em(limit: int = 20):
    """东财股票频道新闻汇总（Playwright 爬取 DOM）。

    数据源: https://stock.eastmoney.com/
    按区块提取重点栏目新闻（股市聚焦[焦点/题材/个股/市场/主力]/大盘分析/板块聚焦/
    行业研究/热门股追踪/主力动态/股市直播/港股聚焦/亚太市场/美股聚焦/欧洲市场等），
    每条: 标题(a.title 完整标题)/URL/发布时间(如有)/所属区块。
    """
    import asyncio
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"success": False, "error": "playwright 未安装"}

    try:
        limit = max(1, min(int(limit), 120))
    except (ValueError, TypeError):
        limit = 20

    EXTRACT_JS = """() => {
        const out = [];
        const seen = new Set();
        document.querySelectorAll('div.card_title, div.card_header').forEach(titleEl => {
            const sec = (titleEl.textContent || '').replace(/更多\\s*$/, '').trim();
            if (!sec) return;
            let node = titleEl.parentElement;
            if (!node) return;
            if (!/card/i.test(node.className)) {
                for (let d = 0; d < 3; d++) {
                    if (!node.parentElement) break;
                    node = node.parentElement;
                    if (/card/i.test(node.className)) break;
                }
            }
            if (!/card/i.test(node.className)) return;
            node.querySelectorAll('a[href*="finance.eastmoney.com/a/"]').forEach(a => {
                const href = (a.href || '').split('#')[0];
                if (!href || seen.has(href)) return;
                const title = (a.title || a.textContent || '').trim().replace(/\\s+/g, ' ');
                if (title.length < 6) return;
                seen.add(href);
                let sub = '';
                const li = a.closest('li');
                if (li && /list-title/.test(li.className)) {
                    const tag = li.querySelector('span, strong');
                    if (tag) sub = (tag.textContent || '').trim();
                }
                let time = '';
                const parent = a.parentElement;
                if (parent) {
                    const sp = parent.querySelector('span.pull-right');
                    if (sp) time = sp.textContent.trim();
                }
                out.push({title: title, url: href, time: time, section: sub ? sec + '-' + sub : sec});
            });
        });
        // 行业研报表 (日期/板块名/相关链接/涨跌幅/行业研报标题)
        document.querySelectorAll('table').forEach(t => {
            const head = (t.innerText || '').slice(0, 60);
            if (!(head.includes('涨跌幅') && head.includes('行业研报'))) return;
            t.querySelectorAll('tbody tr').forEach(tr => {
                const tds = tr.querySelectorAll('td');
                if (tds.length < 5) return;
                const date = (tds[0].textContent || '').trim();
                const nameA = tds[1].querySelector('a');
                const name = nameA ? nameA.textContent.trim() : (tds[1].textContent || '').trim();
                const chg = (tds[3].textContent || '').trim();
                const reportA = tds[4].querySelector('a');
                const rtitle = reportA ? (reportA.textContent || '').trim() : (tds[4].textContent || '').trim();
                const rurl = reportA ? reportA.href : '';
                if (!name || !rtitle) return;
                const key = date + name + rtitle;
                if (seen.has(key)) return;
                seen.add(key);
                out.push({
                    title: [date, name, chg, rtitle].filter(Boolean).join(' ') + ' ',
                    url: rurl,
                    time: date,
                    section: '行业研报表'
                });
            });
        });
        return out;
    }"""

    async def _do_query():
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(_WENCAI_CDP)
            try:
                ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await ctx.new_page()
                try:
                    try:
                        await page.set_viewport_size({"width": 1400, "height": 900})
                    except Exception:
                        pass
                    try:
                        await page.goto(
                            "https://stock.eastmoney.com/",
                            wait_until="domcontentloaded", timeout=20000,
                        )
                    except Exception:
                        pass
                    try:
                        await page.wait_for_selector("div.card_title", timeout=15000)
                    except Exception as e:
                        return {"success": False, "error": f"东财股票频道区块未渲染: {e}"}
                    items = await page.evaluate(EXTRACT_JS)
                    if not items:
                        return {"success": False, "error": "东财股票频道无新闻条目"}
                    return {
                        "success": True,
                        "data": items[:limit],
                        "total": len(items),
                        "limit": limit,
                        "source": "东财股票频道 stock.eastmoney.com (Playwright)",
                    }
                finally:
                    try:
                        await page.close()
                    except Exception:
                        pass
            except Exception as e:
                return {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}

    try:
        return asyncio.run(_do_query())
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ── API 路由表 ──
# ── 18. Bing 搜索 (国内直连) ──

_BING_FRESHNESS_FILTER = {
    "day": 'ex1:"ez5_1704"',
    "week": 'ex1:"ez5_1703"',
    "month": 'ex1:"ez5_1702"',
}

@cached(ttl=180)
def fetch_search_bing(q: str, count: int = 20, freshness: str = ""):
    """Bing 网页搜索（www.bing.com 国内直连, 自动落到 cn.bing.com）。

    返回每条: 标题/URL/摘要/来源域名/发布时间(如有)。
    freshness: "" | "day" | "week" | "month" → Bing filters 时间过滤。
    """
    import asyncio
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"success": False, "error": "playwright 未安装"}

    try:
        count = max(1, min(int(count), 20))
    except (ValueError, TypeError):
        count = 20

    filters = _BING_FRESHNESS_FILTER.get(str(freshness).strip().lower(), "")

    async def _do_query():
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(_WENCAI_CDP)
            try:
                ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await ctx.new_page()
                try:
                    await page.set_viewport_size({"width": 1280, "height": 900})
                    from urllib.parse import quote

                    async def _extract():
                        results = await page.evaluate("""() => {
                            const out = [];
                            document.querySelectorAll('li.b_algo').forEach(li => {
                                const a = li.querySelector('h2 a');
                                if (!a) return;
                                const cap = li.querySelector('.b_caption p, .b_caption');
                                const cite = li.querySelector('cite');
                                const t = li.querySelector('time');
                                const snippet = cap ? cap.textContent.trim() : '';
                                let dom = '';
                                if (cite) {
                                    dom = cite.textContent.trim()
                                        .replace(/^https?:\\/\\//, '').replace(/^www\\./, '');
                                } else {
                                    try {
                                        const u = new URL(a.href);
                                        dom = u.hostname.replace(/^www\\./, '');
                                    } catch (e) {}
                                }
                                out.push({
                                    title: a.textContent.trim(),
                                    url: a.href,
                                    snippet: snippet.slice(0, 400),
                                    source_domain: dom,
                                    publish_time: t ? t.textContent.trim() : '',
                                });
                            });
                            return out;
                        }""")
                        return results

                    # Bing cn 每页最多 10 条, count>10 时翻页 (first=11) 取第二页
                    pages_to_fetch = 2 if count > 10 else 1
                    all_results = []
                    for pn in range(pages_to_fetch):
                        if pn == 0:
                            url = (
                                "https://www.bing.com/search?q=" + quote(q)
                                + f"&setlang=zh-hans&cc=cn&count=10"
                            )
                        else:
                            url = (
                                "https://www.bing.com/search?q=" + quote(q)
                                + f"&setlang=zh-hans&cc=cn&first={pn * 10 + 1}"
                            )
                        if filters:
                            url += "&filters=" + quote(filters)
                        try:
                            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                        except Exception:
                            pass
                        try:
                            await page.wait_for_selector("li.b_algo", timeout=12000)
                        except Exception:
                            pass
                        # 信号等待: 结果条数稳定后再提取（懒加载渐进渲染）
                        async def _bing_results_settled():
                            n = await page.evaluate("() => document.querySelectorAll('li.b_algo').length")
                            if n == 0:
                                return False
                            await page.wait_for_timeout(400)
                            return (await page.evaluate("() => document.querySelectorAll('li.b_algo').length")) == n
                        await _wait_for_ready(page, _bing_results_settled, timeout=6.0, confirm_rounds=1)
                        page_results = await _extract()
                        seen = {r["url"] for r in all_results}
                        for r in page_results:
                            if r["url"] not in seen:
                                all_results.append(r)
                    all_results = all_results[:count]
                    if not all_results:
                        return {"success": False, "error": "Bing 无搜索结果或页面结构变化"}
                    return {
                        "success": True, "query": q,
                        "count": len(all_results), "results": all_results,
                        "source": "Bing (cn)",
                    }
                finally:
                    try:
                        await page.close()
                    except Exception:
                        pass
            except Exception as e:
                return {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}

    try:
        return asyncio.run(_do_query())
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ── 19. 夸克 AI 搜索 (ai.quark.cn, 结构化 AI 总结 + 资讯列表) ──
# 纯 URL 直访: https://ai.quark.cn/s/x?from=kkframenew_resultsearch&by=submit&q={查询词}
# pageid 随意(页面自动重定向生成), 关键参数是 q。AI 回答异步生成约 5-10s。
# 结果页: .results 为 AI 总结区(直接给答案), .result-EzdYH 为资讯卡片。

_QUARK_ITEM_SEL = '[class*="result-EzdYH"], [class*="result-"]'
# AI 总结容器: .sgs-container (流式生成, 可能很短或不存在——不是每个查询都触发 AI 总结)
# 资讯列表: .results (预渲染, t≈1s 即出现, 稳定)
_QUARK_SUMMARY_SEL = '.sgs-container'
_QUARK_RESULTS_SEL = '.results'

@cached(ttl=600)
def fetch_search_quark(q: str, count: int = 10):
    """夸克 AI 网页搜索（ai.quark.cn 国内直连）。

    返回: AI 结构化总结(ai_summary) + 资讯条目列表(标题/URL/摘要/来源/日期)。
    """
    import asyncio
    import re
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"success": False, "error": "playwright 未安装"}

    try:
        count = max(1, min(int(count), 20))
    except (ValueError, TypeError):
        count = 10

    async def _do_query():
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(_WENCAI_CDP)
            try:
                ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await ctx.new_page()
                try:
                    await page.set_viewport_size({"width": 1280, "height": 900})
                    from urllib.parse import quote
                    url = (
                        "https://ai.quark.cn/s/x?from=kkframenew_resultsearch"
                        f"&by=submit&q={quote(q)}"
                    )
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    except Exception:
                        pass
                    # AI 回答异步生成: 轮询等待 .sgs-container (AI 总结) 出现并稳定。
                    # 注意: 不是每个查询都触发 AI 总结——资讯列表(.results)预渲染稳定,
                    # AI 总结(.sgs-container)流式生成, 且生成前容器内是"以上内容由AI生成"
                    # 的模板占位文本(~41字符)。因此:
                    #   - 长度 <=100 或含模板关键词 → 视为"未开始生成", 不计入稳定判定
                    #   - 真实内容(>100字符) 连续两次采样长度相同 → 生成完成
                    #   - 15s 上限兜底; 超时后 sgs 仍为模板/空 → ai_summary 置空(降级)
                    summary = ""
                    deadline = time.time() + 15
                    last_len = -1
                    stable_count = 0
                    _QUARK_TEMPLATE_MARK = "以上内容由AI生成"
                    while time.time() < deadline:
                        try:
                            await page.wait_for_selector(
                                _QUARK_SUMMARY_SEL, state="attached", timeout=4000
                            )
                            await page.wait_for_timeout(600)
                            summary = await page.evaluate(
                                """() => {
                                    const el = document.querySelector('.sgs-container');
                                    return el ? el.innerText.trim() : '';
                                }"""
                            )
                            cur_len = len(summary)
                            # 模板占位: 未开始生成, 重置稳定计数继续等
                            if cur_len <= 100 or _QUARK_TEMPLATE_MARK in summary[:200]:
                                stable_count = 0
                                last_len = cur_len
                                await page.wait_for_timeout(1500)
                                continue
                            if cur_len == last_len:
                                stable_count += 1
                                if stable_count >= 2:
                                    break  # 真实内容连续 2 次相同 = 生成完成
                            else:
                                stable_count = 0
                            last_len = cur_len
                            # 足够长直接收(避免长回答浪费轮询)
                            if cur_len >= 3000:
                                break
                        except Exception:
                            pass
                        await page.wait_for_timeout(1500)

                    # 模板/占位文本不入库
                    if len(summary) <= 100 or _QUARK_TEMPLATE_MARK in summary[:200]:
                        summary = ""

                    # 资讯条目: 从 .results 内的 result 卡片提取
                    items = await page.evaluate(
                        """(maxItems) => {
                        const out = [];
                        // 资讯卡片: article 下的链接组; 用 article a[href^=http] 提取标题/链接,
                        // 再用卡片文本解析来源与日期
                        const cards = document.querySelectorAll('article [class*="result-"]');
                        const seen = new Set();
                        for (const card of cards) {
                            const a = card.querySelector('a[href^="http"]');
                            if (!a) continue;
                            const title = (a.textContent || '').trim();
                            if (title.length < 5 || seen.has(title)) continue;
                            seen.add(title);
                            const href = a.href;
                            const txt = (card.textContent || '').trim();
                            // 来源: 匹配常见媒体名
                            const srcM = txt.match(/(新浪财经|新浪|有驾|百家号|今日头条|网易|腾讯|东方财富|证券时报|每经网|财联社|雪球|同花顺|搜狐|凤凰|澎湃|界面|第一财经|21财经|中国证券报|上海证券报|证券日报|华夏时报|经济观察报)/);
                            const dateM = txt.match(/(20\\d{2})[-/年.](\\d{1,2})[-/月.](\\d{1,2})/);
                            out.push({
                                title: title.slice(0, 120),
                                url: href,
                                snippet: txt.replace(/window\\._q_wl_sc_\\d+ = Date\\.now\\(\\)/g, '').slice(0, 300),
                                source_domain: srcM ? srcM[1] : '',
                                publish_time: dateM ? `${dateM[1]}-${dateM[2].padStart(2,'0')}-${dateM[3].padStart(2,'0')}` : '',
                            });
                            if (out.length >= maxItems) break;
                        }
                        return out;
                    }""",
                        count,
                    )
                    # 降级判定: summary 与 items 双空才算失败; 任一有值即返回部分结果
                    if not summary and not items:
                        return {"success": False, "error": "夸克未返回结果(可能被风控或查询异常)"}
                    return {
                        "success": True,
                        "query": q,
                        "count": len(items),
                        "results": items,
                        "ai_summary": summary[:4000],
                        "partial": len(summary) <= 100,
                        "source": "夸克 AI",
                    }
                finally:
                    try:
                        await page.close()
                    except Exception:
                        pass
            except Exception as e:
                return {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}

    try:
        return asyncio.run(_do_query())
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ── 20. 文章正文抓取 (站点专用选择器 + 通用启发式兜底) ──

_ARTICLE_SELECTORS = {
    "eastmoney.com":     (".txtinfos", ".article-content", ".ContentBody", "#ContentBody"),
    "cls.cn":            (".article-content", ".rich_media_content", "#content"),
    "stcn.com":          (".article-content", "#content"),
    "sina.com.cn":       (".article", "#artibody", "#article"),
    "10jqka.com.cn":     (".news-content.article-content", ".news-content-parsed", ".main-content", "#content", ".atc-content"),
    "mp.weixin.qq.com":  (".rich_media_content", "#js_content"),
}

# 正文截断: 尽量在句子边界(。！？；\n)处截断, 避免切断在句中
_SENTENCE_BOUNDARIES = ("。", "！", "？", "；", "\n")


def _truncate_at_sentence(text: str, limit: int) -> str:
    """在 limit 内找最后一个句子边界标点截断; 找不到则硬截断。"""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # 边界不能太靠前(低于 limit 的一半则视为无有效边界)
    floor = limit // 2
    for sep in _SENTENCE_BOUNDARIES:
        idx = cut.rfind(sep)
        if idx >= floor:
            return cut[: idx + 1].rstrip()
    return cut.rstrip()

@cached(ttl=300)
def fetch_article(url: str, max_chars: int = 3000):
    """抓取网页正文: 标题/发布时间/正文(按域名选择器优先, 通用启发式兜底)。

    安全: 仅允许 http(s) URL; 超时/无正文时返回 error, 由调用方回退 SERP 摘要。
    """
    import asyncio
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"success": False, "error": "playwright 未安装"}

    if not url.lower().startswith(("http://", "https://")):
        return {"success": False, "error": "仅支持 http/https URL"}
    try:
        max_chars = max(500, min(int(max_chars), 20000))
    except (ValueError, TypeError):
        max_chars = 3000

    from urllib.parse import urlparse
    host = urlparse(url).netloc.lower()
    preferred = []
    for dom, sels in _ARTICLE_SELECTORS.items():
        if dom in host:
            preferred = list(sels)
            break

    async def _do_query():
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(_WENCAI_CDP)
            try:
                ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await ctx.new_page()
                try:
                    await page.set_viewport_size({"width": 1280, "height": 900})
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    except Exception as e:
                        return {"success": False, "error": f"页面打开失败: {str(e)[:120]}"}

                    # 智能等待: 优先等待正文容器出现(动态/懒加载页面), 超时则继续走启发式兜底
                    wait_sel = (preferred[0] if preferred else "article")
                    try:
                        await page.wait_for_selector(
                            wait_sel, state="attached", timeout=8000
                        )
                    except Exception:
                        pass

                    # 信号等待: 正文文本量稳定后再提取（懒加载渐进渲染, 最长 6s 兜底）
                    async def _article_text_settled():
                        n = await page.evaluate("() => (document.body ? document.body.innerText.length : 0)")
                        if n < 100:
                            return False
                        await page.wait_for_timeout(400)
                        return (await page.evaluate("() => (document.body ? document.body.innerText.length : 0)")) == n
                    await _wait_for_ready(page, _article_text_settled, timeout=6.0, confirm_rounds=1)

                    data = await page.evaluate(
                        """(preferred) => {
                        const pick = (q) => {
                            const el = document.querySelector(q);
                            return el ? el.textContent.trim() : '';
                        };
                        const title = pick('meta[property="og:title"]')
                            || pick('meta[name="title"]') || document.title || '';
                        const time = pick('meta[property="article:published_time"]')
                            || pick('meta[name="publishdate"]')
                            || pick('meta[name="pubdate"]')
                            || (document.querySelector('time') ? document.querySelector('time').textContent.trim() : '');
                        const sels = [
                            ...(preferred || []),
                            'article', 'main', '[role="main"]',
                            '.article-content', '.article', '.content', '.post-content',
                            '#artibody', '.rich_media_content', '#js_content', '.ContentBody',
                        ].filter((v, i, a) => a.indexOf(v) === i);
                        let best = null, bestLen = 0;
                        for (const s of sels) {
                            const els = document.querySelectorAll(s);
                            for (const el of els) {
                                const t = (el.innerText || '').trim();
                                if (t.length > bestLen) { best = t; bestLen = t.length; }
                            }
                        }
                        // 兜底: 取 body 文本, 去掉脚本/样式
                        if (!best || bestLen < 100) {
                            const b = document.body.cloneNode(true);
                            b.querySelectorAll('script, style, noscript, iframe, nav, header, footer, aside').forEach(n => n.remove());
                            best = (b.innerText || '').trim();
                        }
                        return { title, time, text: best };
                    }""",
                        preferred,
                    )
                    text = (data.get("text") or "").strip()
                    if not text:
                        return {"success": False, "error": "未提取到正文（可能需要登录/反爬）"}
                    text = "\n".join(
                        line.strip() for line in text.split("\n") if line.strip()
                    )
                    truncated = len(text) > max_chars
                    if truncated:
                        text = _truncate_at_sentence(text, max_chars)
                    return {
                        "success": True,
                        "url": url,
                        "title": (data.get("title") or "").strip(),
                        "publish_time": (data.get("time") or "").strip(),
                        "text": text,
                        "truncated": truncated,
                        "source_domain": host,
                    }
                finally:
                    try:
                        await page.close()
                    except Exception:
                        pass
            except Exception as e:
                return {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}

    try:
        return asyncio.run(_do_query())
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ═══════════════════════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════════════════════

ROUTES = {
    "/api/fund-flow":            ("个股资金流+概念(问财)",   fetch_fund_flow_wencai, ["code"]),
    "/api/stock-basic":          ("股本结构(同花顺F10)",     fetch_stock_basic, ["code"]),
    "/api/stock-homepage":       ("首页综合(同花顺F10)",    fetch_stock_homepage, ["code"]),
    "/api/stock-holder":         ("股东研究(同花顺F10)",    fetch_stock_holder, ["code"]),
    "/api/stock-equity-history": ("股本历史(同花顺F10)",    fetch_stock_equity_history, ["code"]),
    "/api/stock-industry-peers": ("同行业对标(同花顺)",      fetch_stock_industry_peers, ["code"]),
    "/api/market-overview":      ("大盘概览(东财行情)",     fetch_market_overview, []),
    "/api/stock-position":       ("主力持仓(同花顺F10)",    fetch_stock_position, ["code"]),
    "/api/stock-kline-full":     ("个股增强K线(东财)",      fetch_stock_kline_full, ["code"]),
    "/api/financial-quarterly":  ("财务指标(同花顺F10)",    fetch_financial_quarterly, ["code"]),
    "/api/concept-blocks":       ("个股概念归属(问财)",      fetch_concept_blocks_wencai, ["code"]),
    "/api/stock-levels":         ("支撑位/压力位(问财)",     fetch_stock_levels, ["code"]),
    "/api/wencai-all":           ("问财全数据(问财)",        fetch_wencai_all, ["code"]),
    "/api/eps-forecast":         ("EPS一致预期(同花顺F10)",  fetch_eps_forecast, ["code"]),
    "/api/executive-changes":    ("高管持股变动(东方财富)",  fetch_executive_changes, ["code"]),
    "/api/company-events":      ("公司大事(同花顺F10)",    fetch_company_events, ["code"]),
    "/api/stock-dividend":      ("分红融资(同花顺F10)",    fetch_stock_dividend, ["code"]),
    "/api/stock-news-f10":      ("新闻公告(同花顺F10)",    fetch_stock_news_f10, ["code"]),
    "/api/industry-hotmap":      ("大盘星图行业热力(东财)",  fetch_industry_hotmap, ["level", "top_n"]),
    "/api/industry-board":       ("行业板块排名(东财页面爬取)", fetch_industry_board, ["top_n"]),
    "/api/global-news-cls":      ("全球快讯(财联社电报爬取)",  fetch_global_news_cls, ["limit"]),
    "/api/global-news-em":       ("全球快讯(东财7x24爬取)",   fetch_global_news_em, ["limit"]),
    "/api/stock-news-em":        ("股市聚焦新闻(东财股票频道)", fetch_stock_news_em, ["limit"]),
    "/api/search-bing":          ("Bing 搜索(国内直连)",      fetch_search_bing, ["q"]),
    "/api/search-quark":         ("夸克 AI 搜索(国内直连)",   fetch_search_quark, ["q"]),
    "/api/fetch-article":        ("文章正文抓取(选择器+启发式)", fetch_article, ["url"]),
}


# ═══════════════════════════════════════════════════════════════
# HTTP Server
# ═══════════════════════════════════════════════════════════════

class DataHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器"""

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status, message):
        self._send_json({"success": False, "error": message}, status)

    def _handle_request(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = {k: v[0] if v else "" for k, v in parse_qs(parsed.query).items()}

        # ── 健康检查 ──
        if path == "/api/health":
            self._send_json({
                "success": True,
                "service": "playwright-data-service",
                "cache_keys": len(_cache),
                "uptime": round(time.time() - _start_time, 1),
            })
            return

        # ── 路由列表 ──
        if path == "/api/routes":
            routes_info = []
            for p, (name, _, req_params) in ROUTES.items():
                routes_info.append({"path": p, "name": name, "params": req_params})
            self._send_json({"success": True, "routes": routes_info})
            return

        # ── 执行路由 ──
        route = ROUTES.get(path)
        if route is None:
            self._send_error(404, f"未知路径: {path}。访问 /api/routes 查看可用路径。")
            return

        name, func, required_params = route

        for p in required_params:
            if p not in params:
                self._send_error(400, f"缺少必需参数: {p}")
                return

        if "code" in params:
            err = _validate_code(params["code"])
            if err:
                self._send_error(400, err)
                return

        try:
            import inspect
            sig = inspect.signature(func)
            func_params = sig.parameters
            if required_params:
                args = [params[p] for p in required_params]
                # Pass optional query params that the function accepts (e.g. start, end, days)
                kwargs = {}
                for k, v in params.items():
                    if k not in required_params and k in func_params:
                        ann = func_params[k].annotation
                        if ann == int:
                            try:
                                v = int(v)
                            except (ValueError, TypeError):
                                pass
                        elif ann == float:
                            try:
                                v = float(v)
                            except (ValueError, TypeError):
                                pass
                        kwargs[k] = v
                # 缓存快速路径 + SWR/single-flight:
                # fresh → 直返(无锁); stale → 返回旧数据+后台刷新(零等待);
                # miss → single-flight 合并并发冷调用(锁外等待 leader)。
                if required_params:
                    args = [params[p] for p in required_params]
                    # Pass optional query params that the function accepts (e.g. start, end, days)
                    kwargs = {}
                    for k, v in params.items():
                        if k not in required_params and k in func_params:
                            ann = func_params[k].annotation
                            if ann == int:
                                try:
                                    v = int(v)
                                except (ValueError, TypeError):
                                    pass
                            elif ann == float:
                                try:
                                    v = float(v)
                                except (ValueError, TypeError):
                                    pass
                            kwargs[k] = v
                    result = _serve_cached(func, args, kwargs)
                else:
                    result = _serve_cached(func, (), {})
            try:
                self._send_json(result)
            except Exception:
                pass  # 客户端已断开连接，忽略发送失败
        except Exception as e:
            try:
                self._send_error(500, f"{type(e).__name__}: {str(e)[:300]}")
            except Exception:
                pass  # 客户端已断开连接，忽略发送失败

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        self._request_time = time.time()
        self._handle_request()

    def log_message(self, format, *args):
        elapsed = ""
        if hasattr(self, '_request_time'):
            elapsed = f" [{time.time() - self._request_time:.2f}s]"
        msg = f"{self.client_address[0]} - {args[0]} {args[1]}{elapsed}"
        print(f"[{time.strftime('%H:%M:%S')}] {msg}")
        logger.info("%s", msg)

_start_time = time.time()


def main():
    parser = argparse.ArgumentParser(description="Playwright 数据服务")
    parser.add_argument("--port", type=int, default=PORT, help=f"监听端口 (默认 {PORT})")
    parser.add_argument("--host", type=str, default=HOST, help=f"监听地址 (默认 {HOST})")
    args = parser.parse_args()

    log_path = _setup_file_logging()

    def _log_and_print(msg: str, level=logging.INFO):
        print(f"[{time.strftime('%H:%M:%S')}] {msg}")
        logger.log(level, "%s", msg)

    # 检查 Chrome CDP 可达性
    import urllib.request, json
    cdp_ok = False
    try:
        resp = urllib.request.urlopen(f"{_WENCAI_CDP}/json/version", timeout=3)
        info = json.loads(resp.read().decode())
        chrome_ver = info.get("Browser", "unknown")
        _log_and_print(f"Chrome CDP: 已连接 ({_WENCAI_CDP}) 版本={chrome_ver[:60]}")
        cdp_ok = True
    except Exception as e:
        _log_and_print(f"⚠ Chrome CDP: 未连接 ({_WENCAI_CDP}) - {e}", logging.WARNING)
        _log_and_print("  依赖 Chrome 的接口(行情/财务/支撑位等)将在首次调用时返回错误", logging.WARNING)

    server = ThreadingHTTPServer((args.host, args.port), DataHandler)
    _log_and_print(f"服务启动: http://{args.host}:{args.port}")
    _log_and_print(f"健康检查: http://{args.host}:{args.port}/api/health")
    _log_and_print(f"路由列表: http://{args.host}:{args.port}/api/routes")
    _log_and_print(f"缓存 TTL: {CACHE_TTL}s")
    _log_and_print(f"日志文件: {log_path}")
    if not cdp_ok:
        _log_and_print("⚠ Chrome CDP 不可用，部分功能受限", logging.WARNING)
    _log_and_print("按 Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\n[{time.strftime('%H:%M:%S')}] 正在停止...")
        server.shutdown()
        print(f"[{time.strftime('%H:%M:%S')}] 已停止")


if __name__ == "__main__":
    main()
