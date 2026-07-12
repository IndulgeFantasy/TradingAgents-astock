"""Playwright-based A-stock data tools.

Tools that fetch data via an external playwright HTTP service
(playwright_service/server.py running in worktrade2 env).
All data is scraped from 同花顺F10/问财/东财行情 via Chrome CDP.

Two categories:
1. Standalone tools (9): only available via playwright service, no a_stock equivalent.
2. Vendor-routed tools (3): registered as "playwright" vendor in VENDOR_METHODS,
   providing richer data than the a_stock direct-HTTP implementations.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

_client = None


def _get_client():
    global _client
    if _client is None:
        from playwright_service.client import PlaywrightClient
        _client = PlaywrightClient()
    return _client


def _fmt_num(val, fmt: str = ".2f", default: str = "N/A") -> str:
    """Type-safe number formatting. Returns default for None/str/invalid values."""
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return f"{val:{fmt}}"
    try:
        return f"{float(val):{fmt}}"
    except (ValueError, TypeError):
        return default


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# ═══════════════════════════════════════════════════════════════
# Standalone tools (9) — no a_stock equivalent
# ═══════════════════════════════════════════════════════════════

@tool("get_stock_basic")
def get_stock_basic(code: str) -> str:
    """获取股本结构（总股本、流通股本、限售股、多期历史变化）。数据源: 同花顺F10"""
    try:
        client = _get_client()
        result = client.stock_basic(code)
        if not result.get("success"):
            return f"[股本结构] {code}: {result.get('error', '')}"
        data = result.get("data", {})
        lines = [
            f"# 股本结构: {data.get('name', code)} ({code})",
            f"# 数据源: 同花顺F10",
            f"# 获取时间: {_now()}",
            "",
        ]
        ts = data.get("总股本")
        fs = data.get("流通股本")
        rs = data.get("限售A股")
        if ts:
            lines.append(f"A股总股本: {_fmt_num(ts, '.2f')}亿")
        if fs:
            pct = _fmt_num(fs / ts * 100, '.1f') if ts else "N/A"
            lines.append(f"流通A股:   {_fmt_num(fs, '.2f')}亿 ({pct})")
        if rs:
            lines.append(f"限售A股:   {_fmt_num(rs, '.4f')}亿")
        history = data.get("shareHistory", [])
        if history:
            sorted_hist = sorted(history, key=lambda h: h.get("date", ""))
            lines.append(f"\n多期总股本变化 ({len(sorted_hist)} 期):")
            for h in sorted_hist:
                lines.append(f"  {h.get('date','')}: {_fmt_num(h.get('totalShares'), '.2f')}亿")
            vals = [h.get("totalShares") for h in sorted_hist if h.get("totalShares") is not None]
            if len(set(str(v) for v in vals)) > 1:
                changes = sum(1 for i in range(1, len(vals)) if vals[i] != vals[i-1])
                oldest, newest = vals[0], vals[-1]
                if newest > oldest:
                    pct = (newest - oldest) / oldest * 100
                    lines.append(f"\n趋势: 近{len(vals)}期有{changes}次变化, 股本扩张 {pct:.2f}%（定增/送转可能）")
                elif newest < oldest:
                    pct = (oldest - newest) / oldest * 100
                    lines.append(f"\n趋势: 近{len(vals)}期有{changes}次变化, 股本缩减 {pct:.2f}%（回购注销可能）")
                else:
                    lines.append(f"\n趋势: 近{len(vals)}期有{changes}次变化, 整体波动")
        if len(lines) <= 4:
            return f"[股本结构] {code}: 无数据"
        return "\n".join(lines)
    except Exception as e:
        return f"[股本结构] 获取异常: {e}"


@tool("get_stock_homepage")
def get_stock_homepage(code: str) -> str:
    """获取同花顺F10首页综合信息（PE/PB/总市值/质押比例/大盘股分类）"""
    try:
        client = _get_client()
        result = client.stock_homepage(code)
        if not result.get("success"):
            return f"[首页] {code}: {result.get('error', '')}"
        d = result.get("data", {})
        lines = [
            f"# 综合概要: {d.get('name', code)} ({code})",
            f"# 数据源: 同花顺F10",
            f"# 获取时间: {_now()}",
            "",
        ]
        pe_d = d.get("pe_dynamic", "N/A")
        pe_s = d.get("pe_static", "N/A")
        pb = d.get("pb", "N/A")
        mcap = d.get("total_mcap_yi", "N/A")
        category = d.get("category", "")
        lines.append(f"PE(动态): {pe_d}  PE(静态): {pe_s}  PB: {pb}  总市值: {mcap}亿  {category}")
        ts = d.get("total_shares_yi")
        fs = d.get("float_shares_yi")
        if ts is not None:
            lines.append(f"总股本: {_fmt_num(ts, '.2f')}亿  流通A股: {_fmt_num(fs, '.2f')}亿" if fs is not None else f"总股本: {_fmt_num(ts, '.2f')}亿")
        pledge = d.get("pledge_shares")
        pledge_pct = d.get("pledge_ratio")
        if pledge is not None:
            pct_str = f" ({_fmt_num(pledge_pct, '.2f')}%)" if isinstance(pledge_pct, (int, float)) else ""
            lines.append(f"质押: {_fmt_num(pledge, '.4f')}亿股{pct_str}")
        return "\n".join(lines)
    except Exception as e:
        return f"[首页] 获取异常: {e}"


@tool("get_stock_industry_peers")
def get_stock_industry_peers(code: str) -> str:
    """获取同行业公司财务指标对标（排名/每股收益/ROE/毛利率等）"""
    try:
        client = _get_client()
        result = client.stock_industry_peers(code)
        if not result.get("success"):
            return f"[行业对标] {code}: {result.get('error', '')}"
        data = result.get("data", {})
        lines = [
            f"# 同行业对标: {data.get('industry', 'N/A')}",
            f"# 数据源: 同花顺F10",
            f"# 获取时间: {_now()}",
            "",
        ]
        if data.get("companyRank"):
            lines.append(f"本公司排名: {data['companyRank']}")
        peers = data.get("peers", [])
        if peers:
            lines.append(f"\n同行业公司 ({len(peers)} 家):")
            for p in peers[:15]:
                items = [f"{k}={v}" for k, v in p.items() if k not in ("name", "股票简称")]
                lines.append(f"  {p.get('name', p.get('股票简称', '?')):<12} {'|'.join(items[:4])}")
        return "\n".join(lines)
    except Exception as e:
        return f"[行业对标] 获取异常: {e}"


@tool("get_stock_holder")
def get_stock_holder(code: str) -> str:
    """获取股东研究数据（股东人数多期时序+前十大流通股东变化）"""
    try:
        client = _get_client()
        result = client.stock_holder(code)
        if not result.get("success"):
            return f"[股东研究] {code}: {result.get('error', '')}"
        data = result.get("data", {})
        lines = [
            f"# 股东研究: {code}",
            f"# 数据源: 同花顺F10",
            f"# 获取时间: {_now()}",
            "",
        ]
        sc = data.get("shareHolderCount", [])
        if sc:
            lines.append(f"股东人数变化 ({len(sc)} 期):")
            for s in sc[:5]:
                holder_count = next((v for k, v in s.items() if "人数" in k), "?")
                change = next((v for k, v in s.items() if "变化" in k), "?")
                lines.append(f"  {s.get('date','')} 总人数={holder_count} 变化={change}")
        th = data.get("top10Holders", [])
        if th:
            lines.append(f"\n前十大流通股东:")
            for t in th[:2]:
                for h in t.get("holders", [])[:5]:
                    lines.append(f"  {h.get('name',''):<16} 持股={h.get('shares','')} 占比={h.get('ratio','')} 变化={h.get('change','')}")
        if len(lines) <= 4:
            return f"[股东研究] {code}: 无数据"
        return "\n".join(lines)
    except Exception as e:
        return f"[股东研究] 获取异常: {e}"


@tool("get_stock_equity_history")
def get_stock_equity_history(code: str) -> str:
    """获取股本历史变动（多期股本结构+历次变动原因）"""
    try:
        client = _get_client()
        result = client.stock_equity_history(code)
        if not result.get("success"):
            return f"[股本历史] {code}: {result.get('error', '')}"
        data = result.get("data", {})
        lines = [
            f"# 股本历史变动: {code}",
            f"# 数据源: 同花顺F10",
            f"# 获取时间: {_now()}",
            "",
        ]
        ss = data.get("shareStructure", [])
        if ss:
            periods = set(s.get("date") for s in ss)
            lines.append(f"股本结构 ({len(periods)} 期):")
            for s in ss[:8]:
                lines.append(f"  {s.get('date','')} {s.get('label','')}: {s.get('value','')}")
        hc = data.get("historicalChanges", [])
        if hc:
            lines.append(f"\n历次股本变动 ({len(hc)} 次):")
            for h in hc[:10]:
                lines.append(f"  {h.get('date','')} {h.get('reason','')} -> 总股本={h.get('totalAfter','')}")
        if len(lines) <= 4:
            return f"[股本历史] {code}: 无数据"
        return "\n".join(lines)
    except Exception as e:
        return f"[股本历史] 获取异常: {e}"


@tool("get_stock_position")
def get_stock_position(code: str) -> str:
    """获取主力持仓/机构持股数据（机构持股汇总5期+机构持股明细）"""
    try:
        client = _get_client()
        result = client.stock_position(code)
        if not result.get("success"):
            return f"[主力持仓] {code}: {result.get('error', '')}"
        data = result.get("data", {})
        lines = [
            f"# 主力持仓: {code}",
            f"# 数据源: 同花顺F10",
            f"# 获取时间: {_now()}",
            "",
        ]
        sm = data.get("institutionSummary", [])
        if sm:
            lines.append("机构持股汇总 (5期):")
            for label in ["机构数量(家)", "持仓比例", "累计持有数量(股)"]:
                vals = [f"{s.get('period','')}={s.get('value','')}" for s in sm if s.get("label") == label]
                if vals:
                    lines.append(f"  {label}: {' -> '.join(vals[:5])}")
        dt = data.get("institutionDetail", [])
        if dt:
            lines.append(f"\n机构持股明细 ({len(dt)} 家):")
            for d in dt[:8]:
                change = d.get("change", "")
                marker = "+" if "新进" in change or change.startswith("+") else ("-" if change.startswith("-") else "*")
                lines.append(f"  {marker} {d.get('name',''):<16} 持股={d.get('shares','')} 占比={d.get('ratio','')} 增减={change}")
        if len(lines) <= 4:
            return f"[主力持仓] {code}: 无数据"
        return "\n".join(lines)
    except Exception as e:
        return f"[主力持仓] 获取异常: {e}"


@tool("get_market_context")
def get_market_context() -> str:
    """获取主要大盘指数概况（上证/沪深300/深证/创业板+成交额+涨跌家数+北向南向资金+融资余额+领涨板块）"""
    try:
        client = _get_client()
        result = client.market_overview()
        if not result.get("success"):
            err = result.get("error", "")
            if "熔断" in err:
                return f"[大盘数据] 获取失败: {err}"
            from time import sleep
            sleep(1)
            result = client.market_overview()
        if not result.get("success"):
            details = result.get("details", [])
            if details:
                return f"[大盘数据] 获取失败:\n" + "\n".join(f"  {d}" for d in details)
            return f"[大盘数据] 获取失败: {result.get('error', '')}"
        lines = ["# 大盘环境参考 (东财行情)", ""]
        for name, info in result.get("data", {}).items():
            latest = info.get("最新")
            chg = info.get("涨跌幅")
            period = info.get("近60日涨跌幅")
            if latest:
                chg_str = _fmt_num(chg, '+.4f', "N/A") + "%" if chg is not None else "N/A"
                period_str = _fmt_num(period, '+.2f', "N/A") + "%" if period is not None else "N/A"
                line = f"  {name}: {_fmt_num(latest, '.2f')} (当日{chg_str}, 近60日{period_str})"
                ma = info.get("均线")
                if ma:
                    line += f" MA5={ma['MA5']} MA10={ma['MA10']} MA20={ma['MA20']} MA60={ma['MA60']} ({ma['排列']})"
                vp = info.get("量价")
                if vp:
                    line += f" 量价={vp}"
                lines.append(line)

        extra = result.get("extra", {})
        if extra:
            _NORTH_KEYS = {"北向资金(沪股通)净买入(亿)", "北向资金(深股通)净买入(亿)",
                           "北向资金净买入合计(亿)", "北向资金成交额合计(亿)",
                           "北向资金净买入"}
            _SOUTH_KEYS = {"南向资金(沪港通)净买入(亿)", "南向资金(深港通)净买入(亿)",
                           "南向资金净买入合计(亿)", "南向资金成交额合计(亿)"}
            north_items = {k: v for k, v in extra.items() if k in _NORTH_KEYS}
            south_items = {k: v for k, v in extra.items() if k in _SOUTH_KEYS}
            other_items = {k: v for k, v in extra.items() if k not in _NORTH_KEYS and k not in _SOUTH_KEYS}

            if north_items:
                lines.append("")
                lines.append("## 北向资金（外资通过港股通买A股，正=净流入，负=净流出）")
                for k, v in north_items.items():
                    lines.append(f"  {k}: {v}")
            if south_items:
                lines.append("")
                lines.append("## 南向资金（内资通过港股通买港股，正=净流入港股，负=净流出港股）")
                for k, v in south_items.items():
                    lines.append(f"  {k}: {v}")
            if other_items:
                lines.append("")
                lines.append("## 其他大盘指标")
                for k, v in other_items.items():
                    if isinstance(v, list):
                        lines.append(f"  {k}: {', '.join(str(x) for x in v)}")
                    else:
                        lines.append(f"  {k}: {v}")
        return "\n".join(lines)
    except Exception as e:
        return f"[大盘数据] 获取异常: {e}"


@tool("get_stock_kline_full")
def get_stock_kline_full(code: str, days: int = 120) -> str:
    """获取个股完整K线数据（含换手率、涨跌幅、成交量）。数据源: 东财 push2his"""
    try:
        client = _get_client()
        result = client.stock_kline_full(code, days)
        if not result.get("success"):
            return f"[K线增强] {code}: {result.get('error', '')}"
        records = result.get("data", [])
        if not records:
            return f"[K线增强] {code} 无数据"
        avg_turn = result.get("avg_turnover", 0)
        stock_name = result.get("stock_name", "")
        warning = result.get("warning", "")
        name_display = f" {stock_name}" if stock_name else ""
        lines = [
            f"# 完整K线数据 {code}{name_display} (东财push2his) | 近{days}日 | {len(records)}条",
            "",
            "# 字段说明: 换手率(turnover)判断筹码活跃度(>5%活跃,<1%低迷)",
            "# volume 字段用于计算近5日/近20日平均成交量",
            "",
            f"  日均换手率: {avg_turn:.2f}%",
            "",
        ]
        if warning:
            lines.append(f"  ⚠️ {warning}")
            lines.append("")
        header = f"  {'日期':<12} {'收盘':<10} {'涨跌幅':<10} {'换手率':<8} {'成交量':<12}"
        lines.append(header)
        lines.append("  " + "-" * (len(header) - 2))
        for r in records[-60:]:
            close = r.get("close")
            chg = r.get("pctChg")
            turn = r.get("turnover")
            vol = r.get("volume")
            date = r.get("date", "")
            chg_str = _fmt_num(chg, '+.2f') + "%" if chg is not None else "N/A"
            turn_str = _fmt_num(turn, '.2f') + "%" if turn is not None else "N/A"
            vol_str = _fmt_num(vol, '.0f') if vol is not None else "N/A"
            close_str = _fmt_num(close, '.2f') if close is not None else "N/A"
            lines.append(f"  {date:<12} {close_str:<10} {chg_str:<10} {turn_str:<8} {vol_str:<12}")
        return "\n".join(lines)
    except Exception as e:
        return f"[K线增强] 获取异常: {e}"


@tool("get_financial_quarterly")
def get_financial_quarterly(code: str) -> str:
    """获取最近4期财务指标（营收/净利润/扣非净利润同比增长率、ROE、毛利率、净利率、资产负债率、EPS、每股经营现金流）"""
    try:
        client = _get_client()
        result = client.financial_quarterly(code)
        if not result.get("success"):
            return f"[季频数据] {code}: {result.get('error', '')}"
        data = result.get("data", [])
        if not data:
            return f"[季频数据] {code} 无数据"
        lines = [
            f"# 季频财务数据 {code} (同花顺F10)",
            "",
            f"  最新净利润同比: {result.get('summary', {}).get('净利润同比', 'N/A')}",
            f"  最新营收同比: {result.get('summary', {}).get('营收同比', 'N/A')}",
            f"  最新扣非净利同比: {result.get('summary', {}).get('扣非净利润同比', 'N/A')}",
            f"  最新ROE: {result.get('summary', {}).get('ROE', 'N/A')}",
            f"  最新毛利率: {result.get('summary', {}).get('毛利率', 'N/A')}",
            f"  最新负债率: {result.get('summary', {}).get('资产负债率', 'N/A')}",
            f"  最新每股收益: {result.get('summary', {}).get('每股收益', 'N/A')}",
            f"  最新经营现金流/净利润: {result.get('summary', {}).get('经营现金流/净利润', 'N/A')}",
            "",
            "各期数据:",
            f"  {'期间':<10} {'营收同比':<12} {'净利同比':<12} {'扣非同比':<12} {'ROE':<8} {'毛利率':<8} {'负债率':<8} {'EPS':<8} {'CFPS':<8} {'CFO/NP':<8}",
            "  " + "-" * 100,
        ]
        for entry in data:
            period = entry.get("period", "")
            yoyni = entry.get("YOYNI_label", "N/A")
            revyoy = entry.get("YOYRevenue_label", "N/A")
            kjyoy = entry.get("YOYCoreProfit_label", "N/A")
            roe = entry.get("ROE_label", "N/A")
            gm = entry.get("GrossMargin_label", "N/A")
            dr = entry.get("DebtRatio_label", "N/A")
            eps = _fmt_num(entry.get("EPS"), '.2f')
            cfps = _fmt_num(entry.get("CFPS"), '.2f')
            cfo_np = _fmt_num(entry.get("CFOToNP"), '.2f')
            lines.append(f"  {period:<10} {revyoy:<12} {yoyni:<12} {kjyoy:<12} {roe:<8} {gm:<8} {dr:<8} {eps:<8} {cfps:<8} {cfo_np:<8}")
        return "\n".join(lines)
    except Exception as e:
        return f"[季频数据] 获取异常: {e}"


@tool("get_stock_levels")
def get_stock_levels(
    code: Annotated[str, "A-stock code (e.g. 600519)"],
) -> str:
    """获取个股支撑位/压力位。数据源: 同花顺问财 (kline2 组件)"""
    try:
        client = _get_client()
        result = client.stock_levels(code)
        if not result.get("success"):
            return f"[支撑压力] {code}: {result.get('error', '')}"
        data = result.get("data", {})
        support = data.get("support")
        resistance = data.get("resistance")
        name = data.get("stock_name", "") or code
        lines = [
            f"# 支撑位/压力位: {name} ({code})",
            f"# 数据源: 同花顺问财",
            "",
        ]
        if support is not None and support != "":
            lines.append(f"支撑位 (止损参考): {support}")
        if resistance is not None and resistance != "":
            lines.append(f"压力位 (止盈参考): {resistance}")
        if not support and not resistance:
            lines.append("暂无支撑压力位数据")
        return "\n".join(lines)
    except Exception as e:
        return f"[支撑压力] 获取异常: {e}"


# ═══════════════════════════════════════════════════════════════
# Vendor-routed implementations (3) — registered as "playwright" vendor
# These provide richer data than the a_stock direct-HTTP versions.
# ═══════════════════════════════════════════════════════════════

def get_concept_blocks_playwright(
    ticker: Annotated[str, "A-stock code (e.g. 688017)"],
) -> str:
    """概念板块归属（问财版）: 所属概念板块列表 + 行业分类。"""
    try:
        client = _get_client()
        result = client.concept_blocks(ticker)
        if not result.get("success"):
            return f"[概念板块] {ticker}: {result.get('error', '')}"
        data = result.get("data", {})
        concepts = data.get("concepts", [])
        industry = data.get("industry", "")
        name = data.get("name", "") or ticker
        if not concepts:
            return f"[概念板块] {ticker}: 未查询到概念归属"
        lines = [
            f"# 概念板块归属: {name} ({ticker})",
            f"# 数据源: 同花顺问财",
            "",
        ]
        if industry:
            ind_str = industry if isinstance(industry, str) else " -> ".join(industry)
            lines.append(f"行业分类: {ind_str}")
        lines.append(f"概念板块 ({len(concepts)} 个):")
        for c in concepts:
            lines.append(f"  - {c}")
        return "\n".join(lines)
    except Exception as e:
        return f"[概念板块] 获取异常: {e}"


def get_fund_flow_playwright(
    ticker: Annotated[str, "A-stock code"],
    *args,
    **kwargs,
) -> str:
    """资金流向分析（问财版）: 30日主力资金时间序列 + DDE散户数量 + 所属概念。"""
    try:
        client = _get_client()
        result = client.fund_flow(ticker)
        if not result.get("success"):
            return f"[资金流] {ticker}: {result.get('error', '')}"
        data = result.get("data", {})
        fund_flow = data.get("fund_flow", [])
        concepts = data.get("concepts", [])
        stock_name = data.get("stock_name", "") or ticker

        lines = [
            f"# 资金流向分析: {stock_name} ({ticker})",
            f"# 数据源: 同花顺问财 (playwright)",
            "",
        ]

        if fund_flow:
            lines.append(f"近{len(fund_flow)}日主力资金净流入 (元):")
            lines.append(f"  {'日期':<12} {'主力净流入':<16} {'成交量':<16}")
            lines.append("  " + "-" * 44)
            for item in fund_flow[-20:]:
                d = item.get("date", "")
                mf = item.get("main_force_net", "")
                vol = item.get("volume", "")
                mf_str = f"{mf:+,.0f}" if isinstance(mf, (int, float)) and mf else str(mf)
                vol_str = f"{vol:,.0f}" if isinstance(vol, (int, float)) and vol else str(vol)
                lines.append(f"  {d:<12} {mf_str:<16} {vol_str:<16}")
            lines.append("")

            vals = []
            for item in fund_flow:
                raw = item.get("main_force_net", 0)
                if isinstance(raw, str):
                    try:
                        raw = float(raw)
                    except (ValueError, TypeError):
                        raw = 0
                vals.append(raw if raw else 0)
            if vals:
                positive = sum(1 for v in vals if v > 0)
                total = len(vals)
                ratio = positive / total * 100
                lines.append(f"趋势: {positive}/{total} 日主力净流入 ({ratio:.0f}%)")
        else:
            lines.append("资金流数据暂不可用")

        dde_qty = data.get("dde_retail_quantity", [])
        if dde_qty:
            lines.append("")
            lines.append("【散户情绪指标】DDE散户数量变化（正=散户增加，负=散户减少）:")
            lines.append(f"  近{len(dde_qty)}日数据:")
            lines.append(f"  {'日期':<12} {'DDE散户数量':<16}")
            lines.append("  " + "-" * 30)
            for item in dde_qty[-20:]:
                d = item.get("date", "")
                val = item.get("dde_retail_qty", "")
                if isinstance(val, (int, float)):
                    lines.append(f"  {d:<12} {val:+.2f}")
                else:
                    lines.append(f"  {d:<12} {val}")
            pv = []
            for item in dde_qty:
                v = item.get("dde_retail_qty")
                if isinstance(v, (int, float)):
                    pv.append(v)
            if pv:
                avg_retail = sum(pv) / len(pv)
                recent_avg = sum(pv[-5:]) / min(5, len(pv))
                lines.append("")
                lines.append(f"  全部均值: {avg_retail:+.2f} | 近5日均值: {recent_avg:+.2f}")
                if recent_avg > 5:
                    lines.append("  解读: 散户近期持续流入，情绪偏乐观（可能为反向指标）")
                elif recent_avg < -5:
                    lines.append("  解读: 散户近期持续流出，情绪偏悲观（可能为反弹信号）")
                else:
                    lines.append("  解读: 散户情绪中性，无明显极端信号")

        if concepts:
            labels = [c.get("label", "") for c in concepts if c.get("category") == "股票特征"]
            if labels:
                lines.append("")
                lines.append(f"所属概念 ({len(labels)} 个):")
                for l in labels:
                    lines.append(f"  - {l}")

        return "\n".join(lines)
    except Exception as e:
        return f"[资金流] 获取异常: {e}"


def get_profit_forecast_playwright(
    ticker: Annotated[str, "A-stock code (e.g. 688017)"],
) -> str:
    """机构盈利预测（同花顺F10详细版）: EPS/净利润一致预期 + 机构预测明细 + 详细指标 + 研报观点。"""
    try:
        client = _get_client()
        result = client.eps_forecast(ticker)
        if not result.get("success"):
            return f"[数据获取失败] EPS预测 {ticker}: {result.get('error', '')}"
        data = result.get("data", {})
        stock_name = data.get("stock_name", "")
        ic = data.get("institution_count")
        st = data.get("summary_text", "")

        header = f"# 机构盈利预测: {ticker}"
        if stock_name:
            header += f" ({stock_name})"
        lines = [header, "# 数据源: 同花顺F10", ""]

        if ic:
            lines.append(f"覆盖机构数: {ic} 家")
        if st:
            lines.append(st)

        eps_sum = data.get("eps_summary", [])
        if eps_sum:
            lines.append("")
            lines.append("EPS一致预期 (元):")
            lines.append(f"  {'年度':<6} {'机构数':<6} {'最小值':<10} {'均值':<10} {'最大值':<10} {'行业均值':<10}")
            lines.append("  " + "-" * 56)
            for r in eps_sum:
                lines.append(f"  {r.get('year',''):<6} {r.get('institution_count',''):<6} {r.get('min',''):<10} {r.get('avg',''):<10} {r.get('max',''):<10} {r.get('industry_avg',''):<10}")

        np_sum = data.get("np_summary", [])
        if np_sum:
            lines.append("")
            lines.append("净利润一致预期 (亿元):")
            lines.append(f"  {'年度':<6} {'机构数':<6} {'最小值':<10} {'均值':<10} {'最大值':<10} {'行业均值':<10}")
            lines.append("  " + "-" * 56)
            for r in np_sum:
                lines.append(f"  {r.get('year',''):<6} {r.get('institution_count',''):<6} {r.get('min',''):<10} {r.get('avg',''):<10} {r.get('max',''):<10} {r.get('industry_avg',''):<10}")

        insts = data.get("institution_forecasts", [])
        valid_insts = [x for x in insts if x.get("institution")]
        if valid_insts:
            lines.append("")
            lines.append("机构预测明细:")
            lines.append(f"  {'机构':<14} {'研究员':<8} {'EPS-26E':<9} {'EPS-27E':<9} {'EPS-28E':<9} {'NP-26E':<10} {'NP-27E':<10} {'NP-28E':<10} {'日期':<12}")
            lines.append("  " + "-" * 96)
            for x in valid_insts[:15]:
                lines.append(f"  {x.get('institution',''):<14} {x.get('analyst',''):<8} {x.get('eps_2026E',''):<9} {x.get('eps_2027E',''):<9} {x.get('eps_2028E',''):<9} {x.get('np_2026E',''):<10} {x.get('np_2027E',''):<10} {x.get('np_2028E',''):<10} {x.get('report_date',''):<12}")
            if len(valid_insts) > 15:
                lines.append(f"  ... (共 {len(valid_insts)} 家机构)")

        indicators = data.get("indicators", [])
        if indicators:
            lines.append("")
            lines.append("详细指标预测 (实际值 vs 预测均值):")
            lines.append(f"  {'指标':<16} {'2023':<12} {'2024':<12} {'2025':<12} {'2026E':<12} {'2027E':<12} {'2028E':<12}")
            lines.append("  " + "-" * 86)
            for ind in indicators:
                name = ind.get("name", "")
                lines.append(f"  {name:<16} {ind.get('2023',''):<12} {ind.get('2024',''):<12} {ind.get('2025',''):<12} {ind.get('2026E',''):<12} {ind.get('2027E',''):<12} {ind.get('2028E',''):<12}")

        summaries = data.get("research_summaries", [])
        if summaries:
            lines.append("")
            lines.append("机构观点摘要:")
            for s in summaries[:5]:
                if s.strip():
                    lines.append(f"  {s.strip()[:250]}")

        if not valid_insts and not indicators and not summaries:
            lines.append("暂无盈利预测数据")
        return "\n".join(lines)
    except Exception as e:
        return f"[数据获取失败] EPS预测获取异常: {e}"
