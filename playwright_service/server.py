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

_cache = {}

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


# ── 缓存装饰器 ──
def _cache_key(func_name, args, kwargs):
    """Build cache key matching @cached decorator."""
    return f"{func_name}:{args}:{ {k: v for k, v in kwargs.items() if v is not None} }"


def _cache_lookup(func, args=(), kwargs=None):
    """Check cache without calling func or acquiring _cdp_lock.
    Returns (hit: bool, data). Cache hits bypass _cdp_lock entirely."""
    kwargs = kwargs or {}
    ttl = getattr(func, '_cached_ttl', 0)
    if ttl <= 0:
        return False, None
    key = _cache_key(func.__name__, args, kwargs)
    now = time.time()
    entry = _cache.get(key)
    if entry:
        ts, data = entry
        if now - ts < ttl:
            return True, data
    return False, None


def cached(ttl=None):
    ttl = ttl or CACHE_TTL
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if ttl <= 0:
                return func(*args, **kwargs)
            key = _cache_key(func.__name__, args, kwargs)
            now = time.time()
            if key in _cache:
                ts, data = _cache[key]
                if now - ts < ttl:
                    return data
            result = func(*args, **kwargs)
            if isinstance(result, dict) and result.get("success"):
                _cache[key] = (now, result)
            return result
        wrapper._cached_ttl = ttl
        return wrapper
    return decorator


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
                await page.wait_for_timeout(6000)

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
@cached(ttl=60)
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

                for idx, (code, name) in enumerate(indices):
                    secid = f"1.{code}" if code.startswith(("0", "6")) else f"0.{code}"
                    url = f"https://quote.eastmoney.com/zs{code}.html"
                    captured = {"kline_list": [], "price": None}
                    # 每个指数用独立 page，避免 TargetClosedError
                    page = await ctx.new_page()
                    await page.set_viewport_size({"width": 1280, "height": 800})

                    async def on_response(resp, idx=idx):
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
                                # 注意: 2024年起交易所停止发布北向实时净买入，netBuyAmt 始终为0
                                #       当 netBuyAmt==0 且 buySellAmt>0 时，标注为"已停止发布"
                                nb_sh_raw = data.get("hk2sh", {}).get("netBuyAmt", 0)
                                nb_sz_raw = data.get("hk2sz", {}).get("netBuyAmt", 0)
                                hk_bs_sh_raw = data.get("hk2sh", {}).get("buySellAmt", 0)
                                hk_bs_sz_raw = data.get("hk2sz", {}).get("buySellAmt", 0)
                                north_unavailable = (nb_sh_raw == 0 and hk_bs_sh_raw > 0)
                                if north_unavailable:
                                    extra["north_net_sh"] = None
                                    extra["north_net_sz"] = None
                                    extra["north_unavailable"] = True
                                else:
                                    extra["north_net_sh"] = round(nb_sh_raw / 10000, 2) if nb_sh_raw else None
                                    extra["north_net_sz"] = round(nb_sz_raw / 10000, 2) if nb_sz_raw else None
                                    extra["north_unavailable"] = False
                                # 北向成交额: hk2sh(沪) hk2sz(深) — 外资在A股总成交额(万元)
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

                    page.on("response", on_response)

                    try:
                        async with page.expect_response(
                            lambda r, s=secid: f"secid={s}" in r.url and "kline" in r.url,
                            timeout=15000
                        ) as resp_info:
                            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                        await page.wait_for_timeout(2000)

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
                            results[name] = {
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
                                results[name]["均线"] = {
                                    "MA5": round(ma5, 2),
                                    "MA10": round(ma10, 2),
                                    "MA20": round(ma20, 2),
                                    "MA60": round(ma60, 2),
                                    "排列": "多头" if bull else ("空头" if bear else "震荡"),
                                }
                                results[name]["量价"] = vol_price
                                results[name]["成交量"] = round(vol_now, 0)
                                if turnovers:
                                    results[name]["换手率"] = round(turnovers[-1], 2)
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
                                        results[name]["MACD"] = {
                                            "DIF": round(dif, 2),
                                            "DEA": round(dea, 2),
                                            "MACD": round(macd, 2),
                                        }
                                # 近5日K线摘要
                                recent_5 = klines[-5:]
                                results[name]["近5日"] = [
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
                        # 每个指数用独立 page，处理完后关闭
                        if idx > 0:
                            try:
                                await page.close()
                            except Exception:
                                pass

                    # 首次加载完成后，从 DOM 提取额外数据
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

                    # 关闭当前页面，避免循环内页面泄漏
                    try:
                        await page.close()
                    except Exception:
                        pass
                    # 循环间隔: 让 Chrome CDP 回收资源，避免连续开关页面过快
                    if idx < len(indices) - 1:
                        import random
                        await asyncio.sleep(1.5 + random.random() * 0.5)

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
                try:
                    await page.close()
                except Exception:
                    pass

    try:
        return asyncio.run(_do_query())
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ── 13. 个股增强K线（东财 push2his，含换手率/涨跌幅/成交量）──
@cached(ttl=60)
def fetch_stock_kline_full(code: str, days: int = 120):
    """
    通过 playwright 访问东财个股页面，获取含换手率的增强K线。
    K线格式: date, open, close, high, low, volume, amount, amplitude%, pctChg%, turnover%
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

                prefix = "sh" if code.startswith(("6", "9")) else "sz"
                url = f"https://quote.eastmoney.com/{prefix}{code}.html"

                # Eastmoney stock pages load K-line data for BOTH the requested
                # stock AND market indices (上证指数/深证成指/创业板指) for the
                # comparison chart.  Collect all push2his kline responses and
                # select the one matching the requested stock's secid, so index
                # data never overwrites stock data (issue: 收盘价与创业板指高度吻合).
                market_id = "1" if code.startswith(("6", "9")) else "0"
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
                await page.wait_for_timeout(5000)

                if not captured_list:
                    return {"success": False, "error": "未获取到K线数据"}

                # Select the stock's K-line response, not the index comparison data.
                captured = None
                for resp_url, data in captured_list:
                    if expected_secid in resp_url:
                        captured = data
                        break
                if captured is None:
                    # Fallback: match by code field in the response payload.
                    for resp_url, data in captured_list:
                        resp_code = str(data.get("data", {}).get("code", ""))
                        if resp_code == code:
                            captured = data
                            break
                if captured is None:
                    # Last resort: use the first captured response.
                    captured = captured_list[0][1]

                klines = captured.get("data", {}).get("klines", [])
                if not klines:
                    return {"success": False, "error": "K线数据为空"}

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
                await page.wait_for_timeout(6000)

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
                                if (label && val && !pairs[label]) pairs[label] = val;
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
                if not pe_dyn_val:
                    m = re_h.search(r'市盈率[（(]动态[）)][：:]\s*([\d.]+|亏损)', text)
                    if m: pe_dyn_val = m.group(1)
                if pe_dyn_val: data["pe_dynamic"] = pe_dyn_val

                pe_sta_val = _get_dom_value('市盈率(静态)') or _get_dom_value('市盈率（静态）')
                if not pe_sta_val:
                    m = re_h.search(r'市盈率[（(]静态[）)][：:]\s*([\d.]+)', text)
                    if m: pe_sta_val = m.group(1)
                if pe_sta_val:
                    try: data["pe_static"] = float(pe_sta_val)
                    except ValueError: pass

                pb_val = _get_dom_value('市净率')
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
                await page.wait_for_timeout(6000)

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
    6. 同业股东人数变化对比 (top10 增加/减少最多)
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
                await page.wait_for_timeout(6000)

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

                    // 6. 同业股东人数变化对比: tables under "同业" section
                    // page has two sub-tables: "增加最多" and "减少最多"
                    // distinguish by scanning preceding sibling text
                    for (let ti = 0; ti < tables.length; ti++) {
                        const table = tables[ti];
                        const section = sectionOf(table);
                        if (!section || !section.includes('同业')) continue;
                        // find preceding heading text to classify increase vs decrease
                        let isDecrease = false;
                        let node = table.previousElementSibling;
                        while (node) {
                            const t = (node.textContent || '').trim();
                            if (t.includes('减少') || t.includes('下降')) { isDecrease = true; break; }
                            if (t.includes('增加') || t.includes('上升')) { isDecrease = false; break; }
                            node = node.previousElementSibling;
                        }
                        const rows = [];
                        for (const tr of table.querySelectorAll('tr')) {
                            const cells = Array.from(tr.querySelectorAll('td, th')).map(td => td.textContent.trim());
                            if (cells.length >= 3) rows.push(cells);
                        }
                        const target = isDecrease ? out.peerComparison.topDecrease : out.peerComparison.topIncrease;
                        for (const r of rows.slice(1)) {
                            if (r[0] && !r[0].includes('股票简称') && r[0] !== code) {
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
_EM_MARKET_IDS = {"6": 17, "0": 22, "3": 23, "8": 9, "9": 17}

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

                # 根据代码前缀决定 marketid
                prefix = code.strip().zfill(6)[0]
                marketid = _EM_MARKET_IDS.get(prefix, 17)
                url = (f"https://basic.10jqka.com.cn/astockpc/astockmain/index.html"
                       f"#/position?code={code}&marketid={marketid}&code_name=")

                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(8000)

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
                await page.wait_for_timeout(8000)

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
                await page.wait_for_timeout(6000)

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


def fetch_concept_blocks_wencai(code: str):
    """通过问财查询个股所属概念板块"""
    import asyncio
    try:
        from playwright.async_api import async_playwright
        from mcp_query_table import query as wc_query, QueryType, Site

        async def _do_query():
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(_WENCAI_CDP)
                try:
                    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                    page = await ctx.new_page()
                    await page.set_viewport_size({"width": 1280, "height": 800})
                    df = await wc_query(page, f"{code}概念板块",
                                         query_type=QueryType.CNStock,
                                         site=Site.THS, max_page=1)
                    if df is None or df.empty:
                        return {"success": False, "error": "问财无返回数据"}
                    cols = list(df.columns)
                    # 找概念相关列
                    concept_col = None
                    for c in cols:
                        if "概念" in c or "板块" in c:
                            concept_col = c
                            break
                    result_data = {}
                    result_data["_columns"] = cols
                    # 提取所有列数据
                    row = df.iloc[0].to_dict() if len(df) > 0 else {}
                    code_val = row.get("code", "")
                    name_val = row.get("股票名称", "")
                    concepts = row.get(concept_col, []) if concept_col else []
                    if isinstance(concepts, str):
                        concepts = [c.strip() for c in concepts.split(",") if c.strip()]
                    # 找行业列
                    industry_col = None
                    for c in cols:
                        if "行业" in c and "分类" not in c:
                            industry_col = c
                            break
                    industry = row.get(industry_col, "") if industry_col else ""
                    return {
                        "success": True,
                        "data": {
                            "code": str(code_val).strip(),
                            "name": str(name_val).strip(),
                            "concepts": concepts,
                            "industry": industry,
                            "raw_columns": cols,
                            "raw_row": {k: str(v)[:80] for k, v in row.items()},
                        },
                        "source": "问财(iwencai)",
                    }
                finally:
                    try:
                        await page.close()
                    except Exception:
                        pass

        return asyncio.run(_do_query())
    except ImportError as e:
        return {"success": False, "error": f"依赖缺失: {e}. 请运行: pip install mcp_query_table playwright"}
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ── 16. 个股资金流时序+概念（通过 playwright 拉问财 barline3）──
@cached(ttl=120)
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
@cached(ttl=120)
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
                    await page.wait_for_timeout(5000)

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
                await page.wait_for_timeout(5000)

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


# ── API 路由表 ──
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
                # Cache hits bypass _cdp_lock (no Chrome access needed).
                # Only cache misses (actual Chrome page operations) serialize.
                hit, cached_data = _cache_lookup(func, args, kwargs)
                if hit:
                    result = cached_data
                else:
                    with _cdp_lock:
                        result = func(*args, **kwargs)
            else:
                hit, cached_data = _cache_lookup(func)
                if hit:
                    result = cached_data
                else:
                    with _cdp_lock:
                        result = func()
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
        print(f"[{time.strftime('%H:%M:%S')}] {self.client_address[0]} - {args[0]} {args[1]}{elapsed}")

_start_time = time.time()


def main():
    parser = argparse.ArgumentParser(description="Playwright 数据服务")
    parser.add_argument("--port", type=int, default=PORT, help=f"监听端口 (默认 {PORT})")
    parser.add_argument("--host", type=str, default=HOST, help=f"监听地址 (默认 {HOST})")
    args = parser.parse_args()

    # 检查 Chrome CDP 可达性
    import urllib.request, json
    cdp_ok = False
    try:
        resp = urllib.request.urlopen(f"{_WENCAI_CDP}/json/version", timeout=3)
        info = json.loads(resp.read().decode())
        chrome_ver = info.get("Browser", "unknown")
        print(f"[{time.strftime('%H:%M:%S')}] Chrome CDP: 已连接 ({_WENCAI_CDP}) 版本={chrome_ver[:60]}")
        cdp_ok = True
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] ⚠ Chrome CDP: 未连接 ({_WENCAI_CDP}) - {e}")
        print(f"[{time.strftime('%H:%M:%S')}]   依赖 Chrome 的接口(行情/财务/支撑位等)将在首次调用时返回错误")

    server = ThreadingHTTPServer((args.host, args.port), DataHandler)
    print(f"[{time.strftime('%H:%M:%S')}] 服务启动: http://{args.host}:{args.port}")
    print(f"[{time.strftime('%H:%M:%S')}] 健康检查: http://{args.host}:{args.port}/api/health")
    print(f"[{time.strftime('%H:%M:%S')}] 路由列表: http://{args.host}:{args.port}/api/routes")
    print(f"[{time.strftime('%H:%M:%S')}] 缓存 TTL: {CACHE_TTL}s")
    if not cdp_ok:
        print(f"[{time.strftime('%H:%M:%S')}] ⚠ Chrome CDP 不可用，部分功能受限")
    print(f"[{time.strftime('%H:%M:%S')}] 按 Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\n[{time.strftime('%H:%M:%S')}] 正在停止...")
        server.shutdown()
        print(f"[{time.strftime('%H:%M:%S')}] 已停止")


if __name__ == "__main__":
    main()
