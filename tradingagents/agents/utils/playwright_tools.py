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
import math
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
    """获取股东研究数据（股东人数时序+前十大流通股东+前十大股东+退出股东+同业对比）"""
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

        # 1. 股东人数时序（完整字段：股东总人数/较上期变化/行业平均/人均流通股/人均流通变化/人均持股金额）
        sc = data.get("shareHolderCount", [])
        if sc:
            lines.append(f"## 股东人数变化 ({len(sc)} 期)")
            lines.append(f"  {'日期':<12} {'股东总人数':<12} {'较上期变化':<10} {'行业平均':<10} {'人均流通股':<12} {'人均流通变化':<10} {'人均持股金额':<12}")
            lines.append("  " + "-" * 86)
            for s in sc:
                date = s.get("date", "")
                count = next((v for k, v in s.items() if "人数" in k and "行业" not in k and "平均" not in k), "?")
                change = next((v for k, v in s.items() if "变化" in k and "行业" not in k and "人均" not in k), "?")
                ind_avg = next((v for k, v in s.items() if "行业" in k and "平均" in k), "N/A")
                per_share = next((v for k, v in s.items() if "人均" in k and "流通股" in k and "变化" not in k), "N/A")
                per_share_chg = next((v for k, v in s.items() if "人均" in k and "流通变化" in k), "N/A")
                per_amount = next((v for k, v in s.items() if "人均" in k and "持股金额" in k), "N/A")
                lines.append(f"  {date:<12} {count:<12} {change:<10} {ind_avg:<10} {per_share:<12} {per_share_chg:<10} {per_amount:<12}")

        # 2. 前十大流通股东（多期，全 10 名）
        th = data.get("top10Holders", [])
        if th:
            lines.append(f"\n## 前十大流通股东 ({len(th)} 期)")
            for t in th:
                period = t.get("period", "")
                summary = t.get("summary", "")
                holders = t.get("holders", [])
                if not holders:
                    continue
                lines.append(f"\n### {period}" + (f"  | {summary}" if summary else ""))
                lines.append(f"  {'股东名称':<28} {'持股数':<14} {'增减':<16} {'占流通比':<8} {'变动比例':<8} {'质押比':<8}")
                lines.append("  " + "-" * 90)
                for h in holders[:10]:
                    name = h.get("name", "")[:26]
                    lines.append(
                        f"  {name:<28} {h.get('shares',''):<14} "
                        f"{h.get('change','')[:14]:<16} {h.get('ratio',''):<8} "
                        f"{h.get('changePct','N/A'):<8} {h.get('pledgeRatio','N/A'):<8}"
                    )

        # 3. 前十大股东（按总股本，非流通股）
        ts = data.get("top10Shareholders", [])
        if ts:
            lines.append(f"\n## 前十大股东-按总股本 ({len(ts)} 期)")
            for t in ts[:2]:  # 只渲染最近 2 期避免过长
                period = t.get("period", "")
                summary = t.get("summary", "")
                holders = t.get("holders", [])
                if not holders:
                    continue
                lines.append(f"\n### {period}" + (f"  | {summary}" if summary else ""))
                lines.append(f"  {'股东名称':<28} {'持股数':<14} {'增减':<16} {'占总股比':<8} {'变动比例':<8} {'质押比':<8}")
                lines.append("  " + "-" * 90)
                for h in holders[:10]:
                    name = h.get("name", "")[:26]
                    lines.append(
                        f"  {name:<28} {h.get('shares',''):<14} "
                        f"{h.get('change','')[:14]:<16} {h.get('ratio',''):<8} "
                        f"{h.get('changePct','N/A'):<8} {h.get('pledgeRatio','N/A'):<8}"
                    )

        # 4. 退出前十大流通股东（重要减持信号）
        ef = data.get("exitedFloatHolders", [])
        if ef:
            lines.append(f"\n## 退出前十大流通股东 ({len(ef)} 家)")
            for h in ef[:5]:
                lines.append(f"  - {h.get('name','')}: 末持 {h.get('shares','')} 占比 {h.get('ratio','')}")

        # 5. 退出前十大股东
        es = data.get("exitedShareholders", [])
        if es:
            lines.append(f"\n## 退出前十大股东 ({len(es)} 家)")
            for h in es[:5]:
                lines.append(f"  - {h.get('name','')}: 末持 {h.get('shares','')} 占比 {h.get('ratio','')}")

        # 6. 同业股东人数变化对比
        pc = data.get("peerComparison", {})
        ti = pc.get("topIncrease", [])
        td = pc.get("topDecrease", [])
        if ti or td:
            lines.append("\n## 同业股东人数变化对比")
            if ti:
                lines.append("  增加最多 top 5:")
                for p in ti[:5]:
                    lines.append(f"    {p.get('name',''):<12} 人数={p.get('count','')} 变化={p.get('change','')}")
            if td:
                lines.append("  减少最多 top 5:")
                for p in td[:5]:
                    lines.append(f"    {p.get('name',''):<12} 人数={p.get('count','')} 变化={p.get('change','')}")

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
    """获取主要大盘指数概况（上证/沪深300/深证成指/创业板指/科创50/中证500/国证2000，含均线/MACD/换手率/近5日K线(开盘/收盘/最高/最低/涨跌幅/成交量/成交额/换手率)+两市成交额+涨跌家数+北向南向资金+融资余额+领涨板块）。无需参数。"""
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
                turnover = info.get("换手率")
                if turnover is not None:
                    line += f" 换手率={turnover}%"
                macd = info.get("MACD")
                if macd:
                    line += f" MACD(DIF={macd['DIF']},DEA={macd['DEA']},柱={macd['MACD']})"
                lines.append(line)
                # 近5日K线
                recent = info.get("近5日", [])
                if recent:
                    lines.append(f"    近5日K线:")
                    lines.append(f"      {'日期':<12} {'开盘':<10} {'收盘':<10} {'最高':<10} {'最低':<10} {'涨跌幅':<8} {'成交量(万手)':<12} {'成交额(亿)':<10} {'换手率':<8}")
                    lines.append("      " + "-" * 100)
                    for k in recent:
                        dt = k.get("date", "")
                        op = _fmt_num(k.get("open"), '.2f')
                        cl = _fmt_num(k.get("close"), '.2f')
                        hi = _fmt_num(k.get("high"), '.2f')
                        lo = _fmt_num(k.get("low"), '.2f')
                        pc = _fmt_num(k.get("pctChg"), '+.2f') + "%"
                        vol = _fmt_num(k.get("volume", 0) and k.get("volume") / 10000, '.0f')
                        amt = _fmt_num(k.get("amount", 0) and k.get("amount") / 1e8, '.2f')
                        tr = _fmt_num(k.get("turnover"), '.2f') + "%"
                        lines.append(f"      {dt:<12} {op:<10} {cl:<10} {hi:<10} {lo:<10} {pc:<8} {vol:<12} {amt:<10} {tr:<8}")

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
        # 涨停价/跌停价状态判断（涨停价已四舍五入到分，不能用涨幅==10%判断）
        lp = result.get("limit_prices", {})
        if lp:
            lu = lp.get("limit_up")
            ld = lp.get("limit_down")
            lprice = lp.get("price")
            lclose = lp.get("last_close")
            if lu and ld:
                lines.append(f"  涨停价: {lu:.2f}  跌停价: {ld:.2f}", )
                if lprice:
                    if abs(lprice - lu) < 0.001:
                        lines.append(f"  ⚠️ 已涨停 (最新价 {lprice:.2f} == 涨停价 {lu:.2f})")
                    elif abs(lprice - ld) < 0.001:
                        lines.append(f"  ⚠️ 已跌停 (最新价 {lprice:.2f} == 跌停价 {ld:.2f})")
                    elif lclose and lu > lclose:
                        # 计算距涨停还有多少空间
                        gap_pct = (lu - lprice) / lprice * 100
                        lines.append(f"  距涨停: {gap_pct:+.2f}% (最新价 {lprice:.2f} -> 涨停价 {lu:.2f})")
                lines.append("")
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
    """获取财务综合数据（8期财务指标矩阵+指标变动说明+审计意见+资产负债构成）。数据源: 同花顺F10 finance.html"""
    try:
        client = _get_client()
        result = client.financial_quarterly(code)
        if not result.get("success"):
            return f"[季频数据] {code}: {result.get('error', '')}"
        data = result.get("data", [])
        if not data:
            return f"[季频数据] {code} 无数据"
        lines = [
            f"# 财务综合数据 {code} (同花顺F10)",
            "",
            f"## 最新一期概览",
            f"  最新净利润同比: {result.get('summary', {}).get('净利润同比', 'N/A')}",
            f"  最新营收同比: {result.get('summary', {}).get('营收同比', 'N/A')}",
            f"  最新扣非净利同比: {result.get('summary', {}).get('扣非净利润同比', 'N/A')}",
            f"  最新ROE: {result.get('summary', {}).get('ROE', 'N/A')}",
            f"  最新毛利率: {result.get('summary', {}).get('毛利率', 'N/A')}",
            f"  最新净利率: {result.get('summary', {}).get('净利率', 'N/A')}",
            f"  最新负债率: {result.get('summary', {}).get('资产负债率', 'N/A')}",
            f"  最新每股收益: {result.get('summary', {}).get('每股收益', 'N/A')}",
            f"  最新经营现金流/净利润: {result.get('summary', {}).get('经营现金流/净利润', 'N/A')}",
            "",
            f"## 财务指标矩阵 - 成长/盈利/每股 ({len(data)} 期)",
            f"  {'期间':<10} {'营收':<10} {'营收同比':<10} {'净利':<10} {'净利同比':<10} {'扣非净利':<10} {'扣非同比':<10} {'EPS':<8} {'BPS':<8} {'资本公积':<8} {'未分配':<8} {'CFPS':<8} {'CFO/NP':<8}",
            "  " + "-" * 140,
        ]
        for entry in data:
            period = entry.get("period", "")
            rev = _fmt_num(entry.get("Revenue"), '.2f')
            revyoy = entry.get("YOYRevenue_label", "N/A")
            ni = _fmt_num(entry.get("NetProfit"), '.2f')
            yoyni = entry.get("YOYNI_label", "N/A")
            kj = _fmt_num(entry.get("CoreProfit"), '.2f')
            kjyoy = entry.get("YOYCoreProfit_label", "N/A")
            eps = _fmt_num(entry.get("EPS"), '.2f')
            bps = _fmt_num(entry.get("BPS"), '.2f')
            cap = _fmt_num(entry.get("CapitalReserve"), '.2f')
            ret = _fmt_num(entry.get("RetainedEarning"), '.2f')
            cfps = _fmt_num(entry.get("CFPS"), '.2f')
            cfonp = _fmt_num(entry.get("CFOToNP"), '.2f')
            lines.append(f"  {period:<10} {rev:<10} {revyoy:<10} {ni:<10} {yoyni:<10} {kj:<10} {kjyoy:<10} {eps:<8} {bps:<8} {cap:<8} {ret:<8} {cfps:<8} {cfonp:<8}")

        lines.append(f"\n## 财务指标矩阵 - 盈利/运营/偿债 ({len(data)} 期)")
        lines.append(f"  {'期间':<10} {'毛利率':<8} {'净利率':<8} {'ROE':<8} {'ROE摊薄':<8} {'营业周期':<8} {'存货周转':<8} {'存货天数':<8} {'应收天数':<8} {'流动比':<8} {'速动比':<8} {'保守速动':<8} {'产权比':<8} {'负债率':<8}")
        lines.append("  " + "-" * 140)
        for entry in data:
            period = entry.get("period", "")
            gm = entry.get("GrossMargin_label", "N/A")
            nm = entry.get("NetMargin_label", "N/A")
            roe = entry.get("ROE_label", "N/A")
            roed = entry.get("ROEDiluted_label", "N/A")
            oc = _fmt_num(entry.get("OperatingCycle"), '.2f')
            inv = _fmt_num(entry.get("InventoryTurnover"), '.2f')
            invd = _fmt_num(entry.get("InventoryDays"), '.2f')
            recd = _fmt_num(entry.get("ReceivableDays"), '.2f')
            cr = _fmt_num(entry.get("CurrentRatio"), '.2f')
            qr = _fmt_num(entry.get("QuickRatio"), '.2f')
            cqr = _fmt_num(entry.get("ConservativeQuickRatio"), '.2f')
            er = _fmt_num(entry.get("EquityRatio"), '.2f')
            dr = entry.get("DebtRatio_label", "N/A")
            lines.append(f"  {period:<10} {gm:<8} {nm:<8} {roe:<8} {roed:<8} {oc:<8} {inv:<8} {invd:<8} {recd:<8} {cr:<8} {qr:<8} {cqr:<8} {er:<8} {dr:<8}")

        # 指标变动说明（显示全部）
        changes = result.get("changes", [])
        if changes:
            lines.append(f"\n## 指标变动说明 ({len(changes)} 项)")
            lines.append(f"  {'变动科目':<24} {'本期数值':<14} {'上期数值':<14} {'变动幅度':<10} {'变动原因'}")
            lines.append("  " + "-" * 120)
            for c in changes:
                lines.append(f"  {c.get('subject','')[:22]:<24} {c.get('current','')[:12]:<14} {c.get('previous','')[:12]:<14} {c.get('change_pct','')[:8]:<10} {c.get('reason','')[:80]}")

        # 审计意见
        audit = result.get("audit", [])
        if audit:
            lines.append(f"\n## 年报审计意见 ({len(audit)} 年)")
            lines.append(f"  {'年份':<8} {'审计意见'}")
            lines.append("  " + "-" * 30)
            for a in audit:
                opinion = a.get("opinion", "--")
                if opinion and opinion != "--":
                    lines.append(f"  {a.get('year',''):<8} {opinion}")

        # 资产负债构成
        assets = result.get("assets", [])
        liabilities = result.get("liabilities", [])
        if assets or liabilities:
            lines.append(f"\n## 资产负债构成（最新一期）")
            if assets:
                lines.append("  资产:")
                for a in assets:
                    lines.append(f"    {a.get('name',''):<16} {a.get('value','')}")
            if liabilities:
                lines.append("  负债:")
                for l in liabilities:
                    lines.append(f"    {l.get('name',''):<16} {l.get('value','')}")

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

            # Forward PE / PEG / PE digestion（从 eps_summary 提取年度 EPS 均值 + 腾讯实时价）
            try:
                eps_by_year = {}
                for r in eps_sum:
                    year = str(r.get("year", ""))
                    avg_val = r.get("avg")
                    try:
                        mean_eps = float(avg_val)
                    except (ValueError, TypeError):
                        continue
                    if year:
                        eps_by_year[year] = mean_eps

                if eps_by_year:
                    from tradingagents.dataflows.a_stock import _tencent_quote
                    tq = _tencent_quote([ticker])
                    if ticker in tq:
                        price = tq[ticker]["price"]
                        pe_ttm = tq[ticker].get("pe_ttm", 0)
                        years_sorted = sorted(eps_by_year.keys())
                        lines.append("")
                        lines.append(f"=== 预期估值（前瞻，基于机构一致预测EPS） ===")
                        lines.append(f"当前: price={price}, PE(TTM)={pe_ttm}")

                        if years_sorted and eps_by_year.get(years_sorted[0], 0) > 0:
                            eps_cur = eps_by_year[years_sorted[0]]
                            fwd_pe = price / eps_cur
                            lines.append(
                                f"Forward PE (FY{years_sorted[0]}): "
                                f"{fwd_pe:.1f}x (price={price}, EPS={eps_cur})"
                            )
                            if (
                                len(years_sorted) >= 2
                                and eps_by_year.get(years_sorted[1], 0) > 0
                            ):
                                eps_next = eps_by_year[years_sorted[1]]
                                cagr = eps_next / eps_cur - 1
                                if cagr > 0:
                                    peg = fwd_pe / (cagr * 100)
                                    lines.append(
                                        f"PEG: {peg:.2f} "
                                        f"(EPS CAGR={cagr * 100:.0f}%)"
                                    )
                                    if fwd_pe > 30:
                                        digest = math.log(fwd_pe / 30) / math.log(
                                            1 + cagr
                                        )
                                        lines.append(
                                            f"PE Digestion to 30x: {digest:.1f} years"
                                        )
                                    else:
                                        lines.append("PE already below 30x target")
                                else:
                                    lines.append(
                                        f"EPS declining ({cagr * 100:.0f}%), "
                                        f"PEG not applicable"
                                    )
            except Exception as e:
                logger.warning("Forward PE calc failed in playwright forecast for %s: %s", ticker, e)

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
            lines.append(f"  {'机构':<14} {'研究员':<8} {'EPS-26E':<10} {'EPS-27E':<10} {'EPS-28E':<10} {'NP-26E':<10} {'NP-27E':<10} {'NP-28E':<10} {'日期':<12}")
            lines.append("  " + "-" * 106)
            for x in valid_insts[:15]:
                def _adj(val, key):
                    v = x.get(key, '')
                    a = x.get(f"{key}_adj", '')
                    marker = '↑' if a == '调高' else ('↓' if a == '调低' else '')
                    return f"{v}{marker}" if v else ''
                lines.append(f"  {x.get('institution',''):<14} {x.get('analyst',''):<8} {_adj(x,'eps_2026E'):<10} {_adj(x,'eps_2027E'):<10} {_adj(x,'eps_2028E'):<10} {_adj(x,'np_2026E'):<10} {_adj(x,'np_2027E'):<10} {_adj(x,'np_2028E'):<10} {x.get('report_date',''):<12}")
            if len(valid_insts) > 15:
                lines.append(f"  ... (共 {len(valid_insts)} 家机构)")
            lines.append("  注: ↑=调高 ↓=调低 无标记=不变/首次")

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

        # 评级分布统计
        rating_dist = data.get("rating_distribution", [])
        if rating_dist:
            lines.append("")
            lines.append("## 机构评级分布" + (f"（{data.get('rating_period','')}）" if data.get("rating_period") else ""))
            dist_str = " | ".join(f"{r['rating']}({r['count']})" for r in rating_dist)
            lines.append(f"  {dist_str}")
            total = sum(r["count"] for r in rating_dist)
            buy_count = sum(r["count"] for r in rating_dist if r["rating"] in ("买入", "增持"))
            if total > 0:
                lines.append(f"  看多占比: {buy_count}/{total} = {buy_count/total*100:.0f}%")

        # 逐条研报评级
        rating_details = data.get("rating_details", [])
        if rating_details:
            lines.append(f"\n## 研报评级明细 ({len(rating_details)} 条)")
            lines.append(f"  {'评级':<6} {'机构':<16} {'日期':<12} {'标题'}")
            lines.append("  " + "-" * 80)
            for r in rating_details[:15]:
                lines.append(f"  {r.get('rating',''):<6} {r.get('institution','')[:14]:<16} {r.get('date',''):<12} {r.get('title','')[:50]}")
            if len(rating_details) > 15:
                lines.append(f"  ... (共 {len(rating_details)} 条，仅显示前 15 条)")

        # 各指标机构明细+评级
        indicator_ratings = data.get("indicator_ratings", [])
        if indicator_ratings:
            lines.append(f"\n## 营收预测机构明细+评级 ({len(indicator_ratings)} 家)")
            lines.append(f"  {'机构':<16} {'研究员':<8} {'预测值':<14} {'评级'}")
            lines.append("  " + "-" * 50)
            for r in indicator_ratings[:10]:
                lines.append(f"  {r.get('institution','')[:14]:<16} {r.get('analyst','')[:6]:<8} {r.get('value','')[:12]:<14} {r.get('rating','')}")

        if not valid_insts and not indicators and not summaries and not rating_dist and not rating_details:
            lines.append("暂无盈利预测数据")
        return "\n".join(lines)
    except Exception as e:
        return f"[数据获取失败] EPS预测获取异常: {e}"
