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


def _data_time_line(result: dict, label: str = "获取时间") -> str:
    """返回数据时间标注行。

    服务端 SWR stale 命中时, 响应含 stale/fetched_at(真实抓取时刻)——
    必须标注, 防止 LLM 把旧数据当最新数据; 否则渲染层"获取时间=现在"会误导。
    """
    if result.get("stale") and result.get("fetched_at"):
        return f"# ⚠️ 数据时间: {result['fetched_at']} (缓存旧数据, 后台刷新中)"
    return f"# {label}: {_now()}"


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
            _data_time_line(result),
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
            _data_time_line(result),
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
        if pledge is not None or pledge_pct is not None:
            if pledge is not None:
                pct_str = f" ({_fmt_num(pledge_pct, '.2f')}%)" if isinstance(pledge_pct, (int, float)) else ""
                lines.append(f"质押: {_fmt_num(pledge, '.4f')}亿股{pct_str}")
            else:
                lines.append(f"质押比例: {_fmt_num(pledge_pct, '.2f')}%")
        return "\n".join(lines)
    except Exception as e:
        return f"[首页] 获取异常: {e}"


@tool("get_stock_dividend")
def get_stock_dividend(code: str) -> str:
    """获取分红融资数据（分红方案历史+分红诊断+增发/配股/增发获配明细），数据源: 同花顺F10 (astockpc #/bonus)"""
    try:
        client = _get_client()
        result = client.stock_dividend(code)
        if not result.get("success"):
            return f"[分红融资] {code}: {result.get('error', '')}"
        data = result.get("data", {})
        lines = [
            f"# 分红融资: {code}",
            f"# 数据源: 同花顺F10 (astockpc)",
            _data_time_line(result),
            "",
        ]

        # 1. 分红方案历史
        prog = data.get("programme") or {}
        if prog.get("status_code") == 0:
            pdata = prog.get("data", {})
            total_cash = pdata.get("total_cash_dividend") or pdata.get("stock_cash_dividend")
            if total_cash:
                try:
                    lines.append(f"上市后累计派现: {float(total_cash) / 1e8:.2f}亿元")
                except (TypeError, ValueError):
                    lines.append(f"上市后累计派现: {total_cash}元")
            page_result = pdata.get("page_result") or {}
            items = page_result.get("data") or []
            if items:
                lines.append(f"\n## 分红方案历史 ({page_result.get('total', len(items))} 期，最新 {min(10, len(items))} 期)")
                lines.append(f"  {'报告期':<12} {'方案':<24} {'登记日':<12} {'除息日':<12} {'总额(亿)':<10} {'支付率':<8} {'进度'}")
                lines.append("  " + "-" * 96)
                for it in items[:10]:
                    date = (it.get("date") or "")[:10]
                    plan = (it.get("dividend_plan") or "")[:22]
                    reg = it.get("equity_registration_date") or "--"
                    exd = it.get("ex_dividend_date") or "--"
                    total = it.get("stock_dividend_total")
                    try:
                        total_yi = f"{float(total) / 1e8:.2f}" if total else "--"
                    except (TypeError, ValueError):
                        total_yi = "--"
                    pay = it.get("payment_rate")
                    try:
                        pay_s = f"{float(pay) * 100:.1f}%" if pay else "--"
                    except (TypeError, ValueError):
                        pay_s = "--"
                    progress = it.get("progress_name") or "--"
                    lines.append(f"  {date:<12} {plan:<24} {reg:<12} {exd:<12} {total_yi:<10} {pay_s:<8} {progress}")

        # 2. 分红诊断
        labels = data.get("label") or {}
        if labels.get("status_code") == 0 and labels.get("data"):
            names = [str(x.get("name", "")) for x in labels["data"] if x.get("name")]
            if names:
                lines.append(f"\n## 分红诊断: {' / '.join(names)}")

        # 3. 增发
        add = data.get("additional") or {}
        if add.get("status_code") == 0:
            stats = add.get("data", {}).get("additional_statistics", {})
            details = add.get("data", {}).get("additional_details", [])
            if stats:
                lines.append(
                    f"\n## 增发概况: 共{stats.get('issue_num', '--')}次"
                    f"(成功{stats.get('issue_success_num', '--')}次)"
                )
            if details:
                latest = details[0]
                lines.append(
                    f"  最近一次: {latest.get('date') or '--'} | 发行价 {latest.get('price')}元"
                    f" | 募资 {float(latest.get('total_cash') or 0) / 1e8:.2f}亿"
                )

        # 4. 配股
        allot = data.get("allotment") or {}
        if allot.get("status_code") == 0:
            stats = allot.get("data", {}).get("allotment_statistics", {})
            details = allot.get("data", {}).get("allotment_details", [])
            if stats:
                lines.append(
                    f"\n## 配股概况: 共{stats.get('issue_num', '--')}次"
                    f"(成功{stats.get('issue_success_num', '--')}次)"
                )
            if details:
                latest = details[0]
                lines.append(
                    f"  最近一次: {latest.get('allotment_name') or latest.get('date') or '--'}"
                    f" | 配售比例 {latest.get('allotment_ratio')}"
                )

        # 5. 增发获配机构
        org = data.get("org_allocated_detail") or {}
        if org.get("status_code") == 0:
            od = org.get("data", {})
            stats = od.get("allocated_statistics", {})
            if stats.get("allocated_org_num"):
                lines.append(
                    f"\n## 增发获配机构: {stats.get('allocated_org_num')}家"
                    f" | 发行价 {stats.get('allocated_price', {}).get('value', '--')}元"
                )
            detail = od.get("allocated_detail", {})
            orgs = detail.get("data") or []
            if orgs:
                lines.append("  机构: " + "、".join(str(o.get("org_name", "")) for o in orgs[:5]))

        # 6. 分红比率
        ratio = data.get("dividend_ratio") or {}
        if ratio.get("status_code") == 0:
            rd = ratio.get("data", {})
            if rd.get("divided_result"):
                lines.append(f"\n## 近三年分红比率: {float(rd['divided_result']) * 100:.2f}%")

        return "\n".join(lines)
    except Exception as e:
        return f"[分红融资] 获取异常: {e}"


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
            _data_time_line(result),
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
            _data_time_line(result),
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
            _data_time_line(result),
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
            _data_time_line(result),
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
        result = client.market_overview(timeout=120)
        if not result.get("success"):
            err = result.get("error", "")
            if "熔断" in err:
                return f"[大盘数据] 获取失败: {err}"
            from time import sleep
            sleep(1)
            result = client.market_overview(timeout=120)
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

        # 三大报表全量科目明细（同花顺F10, 单位: 亿元）
        for key, label in [("balance_sheet", "资产负债表"),
                           ("income_statement", "利润表"),
                           ("cash_flow", "现金流量表")]:
            st = result.get(key)
            if not st:
                continue
            periods = st.get("periods") or []
            items = st.get("items") or {}
            yoy = st.get("yoy") or {}
            lines.append(f"\n## {label} (全量 {len(items)} 科目 × {len(periods)} 期, 单位: 亿元, 报告期由新到旧)")
            if not items:
                lines.append(f"  [数据缺失: {label}]")
                continue
            lines.append("  报告期: " + ", ".join(periods))
            for name, vals in items.items():
                if isinstance(vals, list) and vals:
                    row = f"  {name}: " + ", ".join(
                        "--" if v is False or v is None else str(v) for v in vals
                    )
                else:
                    row = f"  {name}: {vals}"
                if yoy.get(name):
                    row += f"  | 同比: " + ", ".join(
                        "--" if v is False or v is None else str(v) for v in yoy[name]
                    )
                lines.append(row)

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

@tool("get_industry_hotmap")
def get_industry_hotmap(
    level: str = "bk2",
    top_n: int = 20,
    ticker: str = "",
) -> str:
    """获取大盘星图行业热力（全市场个股按行业聚合排名，判断板块轮动/主力资金集中度）。数据源: 东财大盘星图。

    Args:
        level: 行业层级 bk1(一级31个)/bk2(二级128个)/bk3(三级337个)，默认 bk2
        top_n: 返回涨跌幅加权前 top_n 与后 top_n 个行业（默认 20）
        ticker: 目标股票 6 位代码，可选；提供时返回该股所属行业定位（行业名/排名/涨跌幅/主力净占比/换手率）

    返回每个行业: 涨跌家数/流通市值加权涨跌幅(近似)/主力净占比均值/换手率均值/领涨领跌股。
    注意: 行业涨跌幅为个股流通市值加权近似值，与东财官方板块指数口径存在偏差。
    """
    try:
        client = _get_client()
        result = client.industry_hotmap(level, top_n, ticker)
        if not result.get("success"):
            return f"[行业热力] 获取失败: {result.get('error', '')}"
        lines = [
            f"# 大盘星图行业热力 (东财) | {result.get('level', level)} | 共 {result.get('total_industries', 0)} 个行业",
            _data_time_line(result, "行情时间"),
            "",
            "# 说明: 行业涨跌幅为个股流通市值加权近似值（非官方板块指数口径）",
            "",
        ]
        target = result.get("target")
        if target:
            chg = _fmt_num(target.get("chg"), '+.2f', "N/A") + "%" if target.get("chg") is not None else "N/A"
            zljzb = _fmt_num(target.get("zljzb"), '+.2f', "N/A") + "%" if target.get("zljzb") is not None else "N/A"
            turn = _fmt_num(target.get("turnover"), '.2f', "N/A") + "%" if target.get("turnover") is not None else "N/A"
            lines.append(
                f"## ★ 目标股 {target.get('code', ticker)} 所属行业: {target.get('industry','')}"
                f" (BK{target.get('industry_code','')}) | 排名 {target.get('rank','?')}/{target.get('total','?')}"
            )
            lines.append(
                f"    加权涨跌幅 {chg} | 主力净占比 {zljzb} | 换手率 {turn} | 涨/跌家数 {target.get('up','?')}/{target.get('down','?')}"
            )
            lines.append("")
        top = result.get("top", [])
        bottom = result.get("bottom", [])
        if top:
            lines.append(f"## 涨幅居前 {len(top)} 个行业")
            lines.append(f"  {'行业':<12} {'涨跌幅':<8} {'涨/跌家数':<10} {'主力净占比':<10} {'换手率':<8} {'领涨股'}")
            lines.append("  " + "-" * 78)
            for it in top:
                chg = _fmt_num(it.get("chg"), '+.2f', "N/A") + "%" if it.get("chg") is not None else "N/A"
                zljzb = _fmt_num(it.get("zljzb"), '+.2f', "N/A") + "%" if it.get("zljzb") is not None else "N/A"
                turn = _fmt_num(it.get("turnover"), '.2f', "N/A") + "%" if it.get("turnover") is not None else "N/A"
                lines.append(
                    f"  {it.get('name',''):<12} {chg:<8} "
                    f"{it.get('up',0)}/{it.get('down',0):<7} {zljzb:<10} {turn:<8} "
                    f"{it.get('leader','')}"
                )
        if bottom:
            lines.append(f"\n## 涨幅垫底 {len(bottom)} 个行业")
            lines.append(f"  {'行业':<12} {'涨跌幅':<8} {'涨/跌家数':<10} {'主力净占比':<10} {'换手率':<8} {'领跌股'}")
            lines.append("  " + "-" * 78)
            for it in bottom:
                chg = _fmt_num(it.get("chg"), '+.2f', "N/A") + "%" if it.get("chg") is not None else "N/A"
                zljzb = _fmt_num(it.get("zljzb"), '+.2f', "N/A") + "%" if it.get("zljzb") is not None else "N/A"
                turn = _fmt_num(it.get("turnover"), '.2f', "N/A") + "%" if it.get("turnover") is not None else "N/A"
                lines.append(
                    f"  {it.get('name',''):<12} {chg:<8} "
                    f"{it.get('up',0)}/{it.get('down',0):<7} {zljzb:<10} {turn:<8} "
                    f"{it.get('lagger','')}"
                )
        if len(lines) <= 4:
            return "[行业热力] 无数据"
        return "\n".join(lines)
    except Exception as e:
        return f"[行业热力] 获取异常: {e}"


# ═══════════════════════════════════════════════════════════════
# 搜索引擎 (Bing 国内直连) + 文章正文抓取
# ═══════════════════════════════════════════════════════════════

@tool("get_web_search")
def get_web_search(
    query: Annotated[str, "搜索词，如 '宁德时代 欧盟反补贴' / '证监会 减持新规' / '白酒板块 资金'"],
    count: Annotated[int, "返回结果条数 1-20，默认 20"] = 20,
    freshness: Annotated[str, "时间过滤: ''(不限) / 'day'(24小时) / 'week'(一周) / 'month'(一月)，默认 week"] = "week",
) -> str:
    """网页搜索（国内直连，无需代理）: 返回标题/URL/摘要/来源域名/发布时间。

    搜索引擎由配置 search_engine 决定: quark(夸克 AI, 含 AI 结构化总结) 或 bing(Bing cn)。
    用于检索 get_news 覆盖不到的定向问题（特定事件、传闻、政策原文、行业动态）。
    命中权威来源（财联社/证券时报/东财等）且需要看细节时，可再调用 get_article_content 抓正文。
    """
    try:
        from tradingagents.dataflows.config import get_config

        engine = (get_config().get("search_engine") or "quark").strip().lower()
        client = _get_client()
        if engine == "bing":
            result = client.search_bing(query, count, freshness)
            engine_label = "Bing (cn)"
        else:
            result = client.search_quark(query, min(count, 20))
            engine_label = "夸克 AI"
        if not result.get("success"):
            return f"[搜索] {query}: {result.get('error', '')}"
        results = result.get("results", [])
        if not results and not result.get("ai_summary"):
            return f"[搜索] {query}: 无结果"
        lines = [
            f"# 网页搜索: {query} | 共 {result.get('count', len(results))} 条",
            f"# 数据源: {engine_label}",
            "",
        ]
        ai_summary = result.get("ai_summary", "")
        if ai_summary:
            lines.append("## AI 总结")
            lines.append(ai_summary.strip())
            lines.append("")
        if results:
            lines.append("## 搜索结果")
            for i, r in enumerate(results, 1):
                lines.append(f"### [{i}] {r.get('title', '')}")
                if r.get("publish_time"):
                    lines.append(f"  时间: {r['publish_time']}")
                if r.get("source_domain"):
                    lines.append(f"  来源: {r['source_domain']}")
                if r.get("snippet"):
                    lines.append(f"  摘要: {r['snippet']}")
                if r.get("url"):
                    lines.append(f"  链接: {r['url']}")
                lines.append("")
        return "\n".join(lines)
    except Exception as e:
        return f"[搜索] 获取异常: {e}"


@tool("get_stock_news")
def get_stock_news(
    limit: Annotated[int, "返回条数 1-120，默认 120（全量）"] = 120,
) -> str:
    """东财股票频道新闻汇总（https://stock.eastmoney.com/ 页面爬取）。

    按区块返回重点栏目新闻：股市聚焦(焦点/题材/个股/市场/主力)、大盘分析、板块聚焦、
    行业研究、热门股追踪、主力动态、股市直播、港股聚焦、亚太市场等。
    每条含: 完整标题/文章链接/发布时间(如有)/所属区块。
    用于快速浏览当日 A 股市场要闻全貌，补充 get_news/get_global_news 的覆盖。
    """
    try:
        client = _get_client()
        result = client.stock_news_em(limit)
        if not result.get("success"):
            return f"[股市新闻] 获取失败: {result.get('error', '')}"
        items = result.get("data", [])
        if not items:
            return "[股市新闻] 无数据"
        lines = [
            f"# 东财股市聚焦新闻 (stock.eastmoney.com) | 共 {result.get('total', len(items))} 条，显示 {len(items)} 条",
            _data_time_line(result),
            "",
        ]
        for it in items:
            sec = it.get("section", "")
            title = it.get("title", "")
            t = it.get("time", "")
            url = it.get("url", "")
            prefix = f"[{sec}] " if sec else ""
            time_str = f" ({t})" if t else ""
            lines.append(f"### {prefix}{title}{time_str}")
            if url:
                lines.append(f"  链接: {url}")
            lines.append("")
        return "\n".join(lines)
    except Exception as e:
        return f"[股市新闻] 获取异常: {e}"


@tool("get_f10_news")
def get_f10_news(
    code: str,
    limit: Annotated[int, "新闻返回条数，默认 15"] = 15,
) -> str:
    """获取个股新闻公告与机构研报（同花顺F10 news.html）。

    返回:
    1. 个股新闻列表（标题/日期/来源/作者/链接，来源含同花顺iNews等）
    2. 机构研报列表（评级/研报标题/机构/报告日期）
    用于补充 get_news 的个股新闻覆盖（F10 口径，含研报评级）。
    """
    try:
        client = _get_client()
        result = client.stock_news_f10(code, limit)
        if not result.get("success"):
            return f"[F10新闻] {code}: {result.get('error', '')}"
        data = result.get("data", {})
        lines = [
            f"# F10 新闻公告: {code}",
            f"# 数据源: 同花顺F10 (news.html)",
            _data_time_line(result),
            "",
        ]
        news = data.get("news") or []
        if news:
            lines.append(f"## 个股新闻 (共{data.get('total', '?')}条，显示{len(news)}条)")
            for it in news:
                title = it.get("title", "")
                date = it.get("date", "")
                source = it.get("source", "")
                url = it.get("url", "")
                date_str = f" ({date})" if date else ""
                src_str = f" [{source}]" if source else ""
                lines.append(f"### {title}{date_str}{src_str}")
                if url:
                    lines.append(f"  链接: {url}")
            lines.append("")
        research = data.get("research_reports") or []
        if research:
            lines.append(f"## 机构研报 ({len(research)} 条)")
            lines.append(f"  {'评级':<6} {'研报标题':<44} {'机构':<16} {'日期'}")
            lines.append("  " + "-" * 90)
            for r in research:
                lines.append(
                    f"  {str(r.get('rating', ''))[:6]:<6} "
                    f"{str(r.get('report', ''))[:44]:<44} "
                    f"{str(r.get('institution', ''))[:16]:<16} "
                    f"{r.get('date', '')}"
                )
        if not news and not research:
            return f"[F10新闻] {code}: 无数据"
        return "\n".join(lines)
    except Exception as e:
        return f"[F10新闻] 获取异常: {e}"


@tool("get_article_content")
def get_article_content(
    url: Annotated[str, "文章完整 URL（必须是 http/https）"],
    max_chars: Annotated[int, "正文最大字符数，默认 3000"] = 3000,
) -> str:
    """打开网页抓取正文（CDP 真实浏览器）: 返回标题/发布时间/正文文本。

    站点专用选择器（东财/财联社/新浪/证券时报/同花顺/微信）+ 通用启发式兜底。
    用于搜索结果命中权威来源后阅读全文细节；抓取失败时回退 SERP 摘要。
    """
    try:
        client = _get_client()
        result = client.fetch_article(url, max_chars)
        if not result.get("success"):
            return f"[正文抓取] {url}: {result.get('error', '')}"
        lines = [
            f"# {result.get('title', '')}",
            f"# 来源: {result.get('source_domain', '')}",
        ]
        if result.get("publish_time"):
            lines.append(f"# 发布时间: {result['publish_time']}")
        lines.append("")
        lines.append(result.get("text", ""))
        if result.get("truncated"):
            lines.append(f"\n...(已截断，全文超 {max_chars} 字符)")
        return "\n".join(lines)
    except Exception as e:
        return f"[正文抓取] 获取异常: {e}"


def get_concept_blocks_playwright(
    ticker: Annotated[str, "A-stock code (e.g. 688017)"],
) -> str:
    """概念板块归属（同花顺F10概念题材页）: 相关概念明细 + 上涨周期。

    数据源: 新版 F10 astockpc #/concept（问财通道概念数据不稳定，已切换）。
    上涨周期含: 概念组合上涨时间/区间涨幅/主力资金/区间换手 + 驱动逻辑(AI解读)。
    """
    try:
        client = _get_client()
        result = client.stock_concept(ticker)
        if not result.get("success"):
            return f"[概念板块] {ticker}: {result.get('error', '')}"
        data = result.get("data", {})
        concepts = data.get("concepts", [])
        rise_cycles = data.get("rise_cycles") or []
        if not concepts and not rise_cycles:
            return f"[概念板块] {ticker}: 未查询到概念归属"
        lines = [
            f"# 概念板块归属: {ticker}",
            f"# 数据源: 同花顺F10 (astockpc #/concept)",
            "",
        ]
        if concepts:
            lines.append(f"相关概念 ({len(concepts)} 个):")
            for i, c in enumerate(concepts, 1):
                name = c.get("name", "")
                tag = c.get("tag", "")
                stocks = c.get("leading_stocks", [])
                analysis = c.get("analysis", "")
                title = f"{i}. {name}"
                if tag:
                    title += f" [{tag}]"
                lines.append(f"\n{title}")
                if stocks:
                    lines.append(f"  龙头股: {'、'.join(stocks)}")
                if analysis:
                    lines.append(f"  概念解析: {analysis[:200]}")
        if rise_cycles:
            lines.append("\n## 上涨周期（概念组合走势与驱动逻辑）")
            for rc in rise_cycles:
                tags = rc.get("concepts", [])
                metrics = rc.get("metrics", {})
                interp = rc.get("interpretation", "")
                tag_str = " + ".join(tags) if tags else "—"
                lines.append(f"\n### {tag_str}")
                if metrics:
                    parts = []
                    for k in ("上涨时间", "区间涨幅", "区间涨跌", "主力资金", "区间换手"):
                        if metrics.get(k):
                            parts.append(f"{k}: {metrics[k]}")
                    if parts:
                        lines.append("  " + " | ".join(parts))
                if interp:
                    lines.append(f"  驱动逻辑: {interp[:300]}")
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
    curr_date: Annotated[str, "Analysis date in YYYY-MM-DD format"] = None,
) -> str:
    """机构盈利预测（同花顺F10详细版）: EPS/净利润一致预期 + 机构预测明细 + 详细指标 + 研报观点。

    注意: 签名与 a_stock 实现一致（ticker, curr_date）——经 route_to_vendor 以
    双参调用；历史日期在正文顶部加未来函数告警。
    """
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
        # 一致预期只有"当前"版本，没有历史时点值——复盘历史时必须明说
        if curr_date:
            from tradingagents.dataflows.a_stock import _is_historical, _snapshot_notice

            if _is_historical(curr_date):
                lines.insert(0, _snapshot_notice(curr_date, "分析师一致预期"))

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
