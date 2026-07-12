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


# ── 缓存装饰器 ──
def cached(ttl=None):
    ttl = ttl or CACHE_TTL
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if ttl <= 0:
                return func(*args, **kwargs)
            key = f"{func.__name__}:{args}:{ {k: v for k, v in kwargs.items() if v is not None} }"
            now = time.time()
            if key in _cache:
                ts, data = _cache[key]
                if now - ts < ttl:
                    return data
            result = func(*args, **kwargs)
            if isinstance(result, dict) and result.get("success"):
                _cache[key] = (now, result)
            return result
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
                page = await ctx.new_page()
                await page.set_viewport_size({"width": 1280, "height": 800})

                indices = [
                    ("000001", "上证指数"),
                    ("000300", "沪深300"),
                    ("399001", "深证成指"),
                    ("399006", "创业板指"),
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
                            if "api/qt/stock/kline/get" in url_match:
                                klines = d.get("data", {}).get("klines", [])
                                if klines:
                                    parsed = []
                                    for k in klines:
                                        parts = k.split(",")
                                        if len(parts) >= 5:
                                            try: parsed.append({"close": float(parts[2]), "volume": float(parts[5]) if len(parts) >= 6 else 0})
                                            except: pass
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

                        klines = captured["kline_list"][0] if captured["kline_list"] else []

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

                            # 上证指数额外计算: 均线 + 量价分析
                            if idx == 0 and len(klines) >= 60:
                                closes = [k["close"] for k in klines]
                                vols = [k.get("volume", 0) for k in klines]
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
                        else:
                            errors.append(f"{code}: 无K线数据")

                    except Exception as e:
                        errors.append(f"{code}: {type(e).__name__}: {str(e)[:60]}")

                    finally:
                        try:
                            page.remove_listener("response", on_response)
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
                async def on_resp(resp):
                    if "push2his.eastmoney.com/api/qt/stock/kline/get" in resp.url:
                        try:
                            body = await resp.text()
                            import re, json
                            body = re.sub(r'^\w+\(|\)[^)]*$', '', body)
                            data = json.loads(body)
                            captured_list.append((resp.url, data))
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
    1. 股东人数多期时序 (10期)
    2. 前十大流通股东 (多期)
    3. 机构/基金持股变化
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
                    const out = { shareHolderCount: [], top10Holders: [] };

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
                                    entry[labels[j].replace(/\\\\s/g, '_')] = dataRows[j][i];
                                }
                            }
                            out.shareHolderCount.push(entry);
                        }
                    }

                    // 2. 前十大流通股东: table[6]及后续table为多期
                    for (let ti = 6; ti < tables.length && ti < 10; ti++) {
                        const table = tables[ti];
                        if (table.rows.length < 3) continue;
                        // 第一行通常有汇总文字
                        const summary = (table.rows[0]?.textContent || '').trim().slice(0, 150);
                        const holderInfo = { summary: summary, holders: [] };
                        // 跳过表头行
                        let startRow = 1;
                        for (let r = startRow; r < table.rows.length; r++) {
                            const cells = table.rows[r].querySelectorAll('td, th');
                            if (cells.length >= 4) {
                                const name = cells[0]?.textContent?.trim() || '';
                                if (name && !name.includes('机构或基金名称') && !name.includes('前十大')) {
                                    holderInfo.holders.push({
                                        name: name,
                                        shares: cells[1]?.textContent?.trim() || '',
                                        change: (cells[2]?.textContent?.trim() || '').slice(0, 30),
                                        ratio: cells[3]?.textContent?.trim() || '',
                                    });
                                }
                            }
                        }
                        if (holderInfo.holders.length > 0) {
                            out.top10Holders.push(holderInfo);
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
    通过 playwright 访问同花顺 F10 finance 页面，提取财务指标矩阵。
    替换原 baostock 实现。

    返回最近 4 期的: 营收/净利润同比、ROE、毛利率、每股现金流等。
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

                # 提取财务矩阵: labels(指标名), dates(报告期), dataRows(数值)
                matrix = await page.evaluate("""() => {
                    const tables = document.querySelectorAll('table');
                    if (tables.length < 5) return null;
                    // table[0] = "科目\\年度", table[1] = 行标签, table[2] = 日期头, table[4] = 数据
                    const labels = Array.from(tables[1].querySelectorAll('td, th')).map(td => td.textContent.trim());
                    const dates = Array.from(tables[2].querySelectorAll('td, th')).map(td => td.textContent.trim());
                    const dataRows = [];
                    for (const tr of tables[4].querySelectorAll('tr')) {
                        const cells = Array.from(tr.querySelectorAll('td, th')).map(td => td.textContent.trim());
                        dataRows.push(cells);
                    }
                    return { labels, dates, dataRows };
                }""")
                if not matrix:
                    return {"success": False, "error": "未找到财务数据表格"}

                labels = matrix["labels"]
                dates = matrix["dates"]
                dataRows = matrix["dataRows"]

                # 找各指标的行索引
                def idx_of(keywords):
                    for i, lbl in enumerate(labels):
                        if any(k in lbl for k in keywords):
                            return i
                    return None

                idx_ni = idx_of(["净利润(元)"])
                idx_ni_yoy = idx_of(["净利润同比增长率"])
                idx_rev = idx_of(["营业总收入(元)"])
                idx_rev_yoy = idx_of(["营业总收入同比增长率"])
                idx_kj_ni = idx_of(["扣非净利润(元)"])
                idx_kj_ni_yoy = idx_of(["扣非净利润同比增长率"])
                idx_eps = idx_of(["基本每股收益(元)"])
                idx_bps = idx_of(["每股净资产(元)"])
                idx_cfps = idx_of(["每股经营现金流(元)"])
                idx_roe = idx_of(["净资产收益率"])
                idx_gross = idx_of(["销售毛利率"])
                idx_net_margin = idx_of(["销售净利率"])
                idx_debt = idx_of(["资产负债率"])
                idx_capital = idx_of(["每股资本公积金(元)"])
                idx_retained = idx_of(["每股未分配利润(元)"])

                # 辅助: 解析数值字符串 ("53.95亿" → 53.95, "48.74亿" → 48.74)
                import re
                def parse_val(s):
                    if not s or s == "--" or s == "—":
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
                        unit = 0.0001  # 万 → 亿
                        s = s.replace("万", "")
                    m = re.search(r'[-]?[\d.]+', s)
                    if m:
                        try:
                            return round(neg * float(m.group()) * unit, 4)
                        except ValueError:
                            return None
                    return None

                # 取最近 4 期数据（每 3 个月一期）
                max_cols = min(12, len(dates), len(dataRows[0]) if dataRows else 0)
                results = []
                for col in range(0, min(4, max_cols)):
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

                    # 净利润同比
                    ni_yoy = row_val(idx_ni_yoy, col)
                    if ni_yoy is not None:
                        entry["YOYNI"] = round(ni_yoy, 2)
                        entry["YOYNI_label"] = f"{ni_yoy:+.2f}%"

                    # 营收同比
                    rev_yoy = row_val(idx_rev_yoy, col)
                    if rev_yoy is not None:
                        entry["YOYRevenue"] = round(rev_yoy, 2)
                        entry["YOYRevenue_label"] = f"{rev_yoy:+.2f}%"

                    # 扣非净利润同比
                    kj_ni_yoy = row_val(idx_kj_ni_yoy, col)
                    if kj_ni_yoy is not None:
                        entry["YOYCoreProfit"] = round(kj_ni_yoy, 2)
                        entry["YOYCoreProfit_label"] = f"{kj_ni_yoy:+.2f}%"

                    # 每股收益
                    eps = row_val(idx_eps, col)
                    if eps is not None:
                        entry["EPS"] = eps

                    # 每股净资产
                    bps = row_val(idx_bps, col)
                    if bps is not None:
                        entry["BPS"] = bps

                    # 每股经营现金流
                    cfps = row_val(idx_cfps, col)
                    if cfps is not None:
                        entry["CFPS"] = cfps

                    # ROE
                    roe = row_val(idx_roe, col)
                    if roe is not None:
                        entry["ROE"] = round(roe, 2)
                        entry["ROE_label"] = f"{roe:.2f}%"

                    # 毛利率
                    gross = row_val(idx_gross, col)
                    if gross is not None:
                        entry["GrossMargin"] = round(gross, 2)
                        entry["GrossMargin_label"] = f"{gross:.2f}%"

                    # 净利率
                    net_margin = row_val(idx_net_margin, col)
                    if net_margin is not None:
                        entry["NetMargin"] = round(net_margin, 2)
                        entry["NetMargin_label"] = f"{net_margin:.2f}%"

                    # 资产负债率
                    debt = row_val(idx_debt, col)
                    if debt is not None:
                        entry["DebtRatio"] = round(debt, 2)
                        entry["DebtRatio_label"] = f"{debt:.2f}%"

                    # 净利润(元)
                    ni = row_val(idx_ni, col)
                    if ni is not None:
                        entry["NetProfit"] = ni  # 亿

                    # 营业总收入(元)
                    rev = row_val(idx_rev, col)
                    if rev is not None:
                        entry["Revenue"] = rev  # 亿

                    # 经营现金流/净利润比 (CFPS / EPS)
                    if entry.get("CFPS") and entry.get("EPS") and entry["EPS"] != 0:
                        entry["CFOToNP"] = round(entry["CFPS"] / entry["EPS"], 4)

                    results.append(entry)

                if not results:
                    return {"success": False, "error": f"finance.html 无 {code} 财务数据"}

                # 构建 summary
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

                return {
                    "success": True,
                    "data": results,
                    "rows": len(results),
                    "source": "同花顺F10",
                    "summary": summary,
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
    2. impressionLabel: 所属概念板块

    性能优化: 用 expect_event(stream-query) + domcontentloaded 替代
    networkidle + 固定8秒等待，耗时从 ~14s 降至 ~4s。
    """
    import asyncio
    try:
        from playwright.async_api import async_playwright

        async def _do_query():
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(_WENCAI_CDP)
                try:
                    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                    page = await ctx.new_page()
                    await page.set_viewport_size({"width": 1280, "height": 800})

                    # 用 expect_event 等待 stream-query 响应到达，避免
                    # networkidle(5s+) + 固定8秒等待 = 14s 的开销
                    async with page.expect_event(
                        "response",
                        predicate=lambda r: "stream-query" in r.url,
                        timeout=20000
                    ) as event_info:
                        await page.goto(
                            f"https://www.iwencai.com/unifiedwap/result?w={code}",
                            wait_until="domcontentloaded"
                        )
                    response = await event_info.value
                    text = await response.text()
                    fund_flow = []
                    dde_flow = []
                    dde_retail_quantity = []
                    concepts = []
                    stock_name = ""

                    for d in _parse_sse_lines(text):
                        comps = d.get("section", {}).get("result_page", {}).get("components", [])
                        for comp in comps:
                            st = comp.get("show_type", "")
                            data = comp.get("data", {})
                            cols = [c.get("index_name", "") for c in data.get("columns", [])]
                            datas = data.get("datas", [])

                            if st == "barline3" and datas:
                                if "主力资金" in cols:
                                    for row in datas:
                                        fund_flow.append({
                                            "date": row.get("时间", "") or row.get("时间周期", ""),
                                            "main_force_net": row.get("主力资金"),
                                            "volume": row.get("成交量"),
                                        })
                                elif "dde散单净流入" in cols:
                                    for row in datas:
                                        dde_flow.append({
                                            "date": row.get("时间", ""),
                                            "dde_retail_net": row.get("dde散单净流入"),
                                            "close": row.get("收盘价"),
                                        })
                                if "dde散户数量" in cols:
                                    for row in datas:
                                        dde_retail_quantity.append({
                                            "date": row.get("时间", ""),
                                            "dde_retail_qty": row.get("dde散户数量"),
                                        })

                            if st == "impressionLabel" and datas:
                                for row in datas:
                                    label = row.get("标签", "")
                                    cat = row.get("类别", "")
                                    if cat and label:
                                        concepts.append({"category": cat, "label": label})

                            if st == "kline2" and datas and not stock_name:
                                stock_name = datas[0].get("股票名称", "")

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
                    try:
                        await page.close()
                    except Exception:
                        pass

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
                    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                    page = await ctx.new_page()
                    await page.set_viewport_size({"width": 1280, "height": 800})

                    # 同时导航和等待 stream-query 响应（替代固定 8s 等待）
                    async with page.expect_event(
                        "response",
                        predicate=lambda r: "stream-query" in r.url,
                        timeout=20000
                    ) as event_info:
                        await page.goto(
                            f"https://www.iwencai.com/unifiedwap/result?w={code}",
                            wait_until="domcontentloaded"
                        )
                    response = await event_info.value
                    text = await response.text()
                    support = None
                    resistance = None
                    stock_name = ""

                    for d in _parse_sse_lines(text):
                        comps = d.get("section", {}).get("result_page", {}).get("components", [])
                        for comp in comps:
                            if comp.get("show_type") != "kline2":
                                continue
                            datas = comp.get("data", {}).get("datas", [])
                            if datas:
                                stock_name = datas[0].get("股票名称", "")
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
                    try:
                        await page.close()
                    except Exception:
                        pass

        return asyncio.run(_do_query())
    except ImportError as e:
        return {"success": False, "error": f"依赖缺失: {e}"}
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ── 18. 问财通用查询（整合所有可用组件）──
@cached(ttl=120)
def fetch_wencai_all(code: str):
    """一次问财查询，返回所有可用数据组件

    性能优化: 用 expect_event(stream-query) + domcontentloaded 替代
    networkidle + 固定8秒等待，耗时从 ~14s 降至 ~4s。
    """
    import asyncio
    try:
        from playwright.async_api import async_playwright

        async def _do_query():
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(_WENCAI_CDP)
                try:
                    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                    page = await ctx.new_page()
                    await page.set_viewport_size({"width": 1280, "height": 800})

                    async with page.expect_event(
                        "response",
                        predicate=lambda r: "stream-query" in r.url,
                        timeout=20000
                    ) as event_info:
                        await page.goto(
                            f"https://www.iwencai.com/unifiedwap/result?w={code}",
                            wait_until="domcontentloaded"
                        )
                    response = await event_info.value
                    text = await response.text()
                    result = {"fund_flow": [], "levels": {}, "concepts": [], "finance": [], "stock_name": ""}

                    for d in _parse_sse_lines(text):
                        comps = d.get("section", {}).get("result_page", {}).get("components", [])
                        for comp in comps:
                            st = comp.get("show_type", "")
                            data = comp.get("data", {})
                            cols = [c.get("index_name", "") for c in data.get("columns", [])]
                            datas = data.get("datas", [])

                            if st == "barline3" and datas:
                                if "主力资金" in cols:
                                    for row in datas:
                                        result["fund_flow"].append({
                                            "date": row.get("时间", "") or row.get("时间周期", ""),
                                            "main_force_net": row.get("主力资金"),
                                        })
                            elif st == "kline2" and datas:
                                r = datas[0]
                                result["levels"] = {
                                    "support": r.get("止盈止损(支撑位)"),
                                    "resistance": r.get("止盈止损(压力位)"),
                                }
                                if not result["stock_name"]:
                                    result["stock_name"] = r.get("股票名称", "")
                            elif st == "impressionLabel" and datas:
                                for row in datas:
                                    result["concepts"].append({
                                        "label": row.get("标签", ""),
                                        "category": row.get("类别", ""),
                                    })

                    return {"success": True, "data": result, "source": "问财(iwencai)"}
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
                    institution_forecasts = []
                    for table in tables:
                        if not table or len(table) < 3:
                            continue
                        header = " ".join(table[0])
                        if "机构名称" not in header or "研究员" not in header:
                            continue
                        for row in table[2:]:
                            if len(row) >= 8:
                                institution_forecasts.append({
                                    "institution": row[0],
                                    "analyst": row[1],
                                    "eps_2026E": row[2],
                                    "eps_2027E": row[3],
                                    "eps_2028E": row[4],
                                    "np_2026E": row[5],
                                    "np_2027E": row[6],
                                    "np_2028E": row[7],
                                    "report_date": row[8] if len(row) > 8 else "",
                                })
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
                result = func(*args, **kwargs)
            else:
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
