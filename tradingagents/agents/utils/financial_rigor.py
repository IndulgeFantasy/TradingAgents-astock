"""Financial Rigor Toolkit - 精确十进制金融计算工具。

All calculations use Python decimal.Decimal (exact base-10), not float.
Zero external dependencies - uses only Python stdlib.

Functions return strings (for LLM consumption) or dicts (for programmatic use).
Adapted from ai-berkshire's financial_rigor.py (removed CLI, added @tool wrapper).

Usage:
    from tradingagents.agents.utils.financial_rigor import verify_valuation, verify_market_cap

    # Programmatic
    result = verify_valuation(price=50, eps=2.5, bvps=15)

    # As @tool (for LLM agents)
    # verify_stock_valuation.invoke({"code": "600519"})
"""

from __future__ import annotations

import logging
import math
from decimal import Decimal, Context, ROUND_HALF_EVEN
from typing import Annotated

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)


def _exact(value) -> Decimal:
    """Convert any numeric to exact Decimal, avoiding float traps."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _fmt_number(d: Decimal, unit: str = "") -> str:
    """Format large numbers in human-readable form."""
    v = float(d)
    abs_v = abs(v)
    if unit in ("亿", "亿元", "亿港元", "亿美元"):
        if abs_v >= 10000:
            return f"{v/10000:.2f}万亿{unit[1:] if len(unit) > 1 else ''}"
        return f"{v:.2f}{unit}"
    if abs_v >= 1e12:
        return f"{v/1e12:.2f}T"
    if abs_v >= 1e9:
        return f"{v/1e9:.2f}B"
    if abs_v >= 1e6:
        return f"{v/1e6:.2f}M"
    return f"{v:,.2f}"


# ---------------------------------------------------------------------------
# 1. Market Cap Verification
# ---------------------------------------------------------------------------

def verify_market_cap(price, shares, reported_cap, currency="") -> dict:
    """Verify market cap = price x shares, compare with reported value.

    Returns dict with: calculated, reported, deviation_pct, passed, status.
    """
    p = _exact(price)
    s = _exact(shares)
    r = _exact(reported_cap)

    calculated = _CTX.multiply(p, s)
    deviation = abs(float(calculated - r) / float(r)) * 100 if r != 0 else 0

    if deviation > 5:
        status = "FAIL"
        hint = "偏差>5%，请检查：股本是否最新(回购/增发)？单位是否一致(港币/人民币/美元)？股价是否最新？"
    elif deviation > 1:
        status = "WARN"
        hint = "偏差1-5%，可能因股价波动/股本变化"
    else:
        status = "PASS"
        hint = ""

    return {
        "calculated": float(calculated),
        "reported": float(r),
        "deviation_pct": round(deviation, 2),
        "passed": deviation <= 5,
        "status": status,
        "hint": hint,
        "text": (
            f"市值验算: 计算={_fmt_number(calculated)} {currency} | "
            f"报告={_fmt_number(r)} {currency} | "
            f"偏差={deviation:.2f}% [{status}]"
        ),
    }


# ---------------------------------------------------------------------------
# 2. Valuation Metrics Verification
# ---------------------------------------------------------------------------

def verify_valuation(price, eps=None, bvps=None, fcf_per_share=None,
                     dividend=None, revenue_per_share=None) -> dict:
    """Calculate and verify key valuation ratios from raw inputs.

    All calculations use exact Decimal arithmetic.
    Returns dict with PE, PB, ROE, P_FCF, FCF_Yield, Dividend_Yield, PS (as applicable).
    """
    p = _exact(price)
    results = {}
    text_parts = [f"估值验算 (股价={p}):"]

    if eps is not None:
        e = _exact(eps)
        if e != 0:
            pe = _CTX.divide(p, e)
            results["PE"] = float(pe)
            ey = _CTX.divide(e, p) * 100
            results["Earnings_Yield"] = float(ey)
            text_parts.append(f"  PE = {p} / {e} = {pe:.2f}x (盈利收益率={ey:.2f}%)")

    if bvps is not None:
        b = _exact(bvps)
        if b != 0:
            pb = _CTX.divide(p, b)
            results["PB"] = float(pb)
            text_parts.append(f"  PB = {p} / {b} = {pb:.2f}x")
            if eps is not None and float(_exact(eps)) != 0:
                roe = _CTX.divide(_exact(eps), b) * 100
                results["ROE"] = float(roe)
                text_parts.append(f"  ROE = {_exact(eps)} / {b} = {roe:.2f}%")

    if fcf_per_share is not None:
        f = _exact(fcf_per_share)
        if f != 0:
            fcf_yield = _CTX.divide(f, p) * 100
            pfcf = _CTX.divide(p, f)
            results["P_FCF"] = float(pfcf)
            results["FCF_Yield"] = float(fcf_yield)
            text_parts.append(f"  P/FCF = {pfcf:.2f}x (FCF Yield={fcf_yield:.2f}%)")

    if dividend is not None:
        d = _exact(dividend)
        if p != 0:
            div_yield = _CTX.divide(d, p) * 100
            results["Dividend_Yield"] = float(div_yield)
            text_parts.append(f"  股息率 = {div_yield:.2f}%")

    if revenue_per_share is not None:
        r = _exact(revenue_per_share)
        if r != 0:
            ps = _CTX.divide(p, r)
            results["PS"] = float(ps)
            text_parts.append(f"  PS = {ps:.2f}x")

    results["text"] = "\n".join(text_parts)
    return results


# ---------------------------------------------------------------------------
# 3. Cross-Source Data Validation
# ---------------------------------------------------------------------------

def cross_validate(field_name, source_values: dict, unit="", tolerance_pct=2.0) -> dict:
    """Compare a data point across multiple sources, flag discrepancies.

    Returns dict with: consensus, all_consistent, details, text.
    """
    values = {k: _exact(v) for k, v in source_values.items()}
    nums = list(values.values())

    sorted_vals = sorted(float(v) for v in nums)
    n = len(sorted_vals)
    if n == 0:
        return {"consensus": None, "all_consistent": False, "text": "无数据"}
    median = sorted_vals[n // 2] if n % 2 == 1 else (sorted_vals[n//2-1] + sorted_vals[n//2]) / 2

    all_ok = True
    details = []
    text_parts = [f"交叉验证: {field_name} (参考中位数={_fmt_number(_exact(median))} {unit})"]

    for src, val in values.items():
        dev = abs(float(val) - median) / median * 100 if median != 0 else 0
        passed = dev <= tolerance_pct
        if not passed:
            all_ok = False
        status = "OK" if passed else "FAIL"
        details.append({"source": src, "value": float(val), "deviation_pct": round(dev, 2), "status": status})
        text_parts.append(f"  {'OK' if passed else 'FAIL'} {src}: {float(val)} {unit} (偏差 {dev:.2f}%)")

    if all_ok:
        text_parts.append(f"  所有来源偏差 <= {tolerance_pct}%, 数据一致")
    else:
        text_parts.append(f"  存在来源偏差 > {tolerance_pct}%, 建议优先采用年报/交易所数据")

    return {
        "consensus": median,
        "all_consistent": all_ok,
        "details": details,
        "text": "\n".join(text_parts),
    }


# ---------------------------------------------------------------------------
# 4. Benford's Law Check
# ---------------------------------------------------------------------------

_BENFORD = {d: math.log10(1 + 1/d) for d in range(1, 10)}


def benford_check(values: list) -> dict:
    """Quick Benford's Law check on a list of financial values.

    Returns dict with: mad, chi2, conformity, is_conforming, text.
    """
    digits = []
    for v in values:
        v = abs(float(v))
        if v > 0:
            sig = 10 ** (math.log10(v) - math.floor(math.log10(v)))
            d = int(sig)
            if 1 <= d <= 9:
                digits.append(d)

    n = len(digits)
    if n < 50:
        return {"mad": None, "is_conforming": None, "text": f"样本量不足: {n} < 50, Benford分析不可靠"}

    counts = {}
    for d in digits:
        counts[d] = counts.get(d, 0) + 1
    observed = {d: counts.get(d, 0) / n for d in range(1, 10)}

    mad = sum(abs(observed.get(d, 0) - _BENFORD[d]) for d in range(1, 10)) / 9
    chi2 = sum((counts.get(d, 0) - _BENFORD[d] * n) ** 2 / (_BENFORD[d] * n) for d in range(1, 10))

    if mad < 0.006:
        conformity = "高度符合"
    elif mad < 0.012:
        conformity = "可接受"
    elif mad < 0.015:
        conformity = "边缘"
    else:
        conformity = "不符合"

    is_ok = mad < 0.015
    text_parts = [
        f"Benford定律检测: 样本={n} | MAD={mad:.6f} | Chi-sq={chi2:.2f} | 符合度={conformity}",
    ]
    if is_ok:
        text_parts.append("数据首位数字分布符合Benford定律")
    else:
        text_parts.append("数据首位数字分布异常, 可能存在人为调整（不一定是造假，但值得调查）")

    return {
        "mad": mad,
        "chi2": chi2,
        "conformity": conformity,
        "is_conforming": is_ok,
        "text": "\n".join(text_parts),
    }


# ---------------------------------------------------------------------------
# 5. Exact Calculator
# ---------------------------------------------------------------------------

def exact_calc(expr: str) -> dict:
    """Evaluate a financial expression with exact decimal arithmetic.

    Supports: +, -, *, /, (), numbers (including scientific notation).
    Returns dict with: result, text.
    """
    allowed = set("0123456789.+-*/() eE")
    if not all(c in allowed for c in expr.replace(" ", "")):
        return {"result": None, "text": f"不安全的表达式: {expr}"}

    try:
        result = eval(expr, {"__builtins__": {}}, {})
        d_result = _exact(result)
        return {
            "result": float(d_result),
            "text": f"精确计算: {expr} = {_fmt_number(d_result)} (精确值: {d_result})",
        }
    except Exception as e:
        return {"result": None, "text": f"计算错误: {e}"}


# ---------------------------------------------------------------------------
# 6. Three-Scenario Valuation
# ---------------------------------------------------------------------------

def three_scenario_valuation(current_price, current_eps,
                             growth_optimistic, growth_neutral, growth_pessimistic,
                             pe_optimistic, pe_neutral, pe_pessimistic,
                             years=3, currency="") -> dict:
    """Calculate three-scenario target prices with exact arithmetic.

    Returns dict with: scenarios, text.
    """
    p = _exact(current_price)
    eps = _exact(current_eps)

    scenarios = [
        ("乐观 (Bull)", growth_optimistic, pe_optimistic),
        ("中性 (Base)", growth_neutral, pe_neutral),
        ("悲观 (Bear)", growth_pessimistic, pe_pessimistic),
    ]

    results = []
    text_parts = [
        f"三情景估值 (股价={p} {currency}, EPS={eps}, 预测期={years}年)",
        f"  {'情景':12} {'年增速':>8} {'目标PE':>8} {'目标EPS':>10} {'目标股价':>10} {'涨跌幅':>8}",
    ]

    for name, growth, pe in scenarios:
        g = _exact(growth)
        target_pe = _exact(pe)
        future_eps = eps
        for _ in range(years):
            future_eps = _CTX.multiply(future_eps, _CTX.add(Decimal("1"), g))
        target_price = _CTX.multiply(future_eps, target_pe)
        change = float(target_price - p) / float(p) * 100

        results.append({
            "scenario": name,
            "growth": float(g),
            "target_pe": float(target_pe),
            "future_eps": float(future_eps),
            "target_price": float(target_price),
            "change_pct": round(change, 1),
        })
        text_parts.append(
            f"  {name:12} {float(g)*100:>7.0f}% {float(target_pe):>7.0f}x "
            f"{float(future_eps):>10.2f} {float(target_price):>9.1f} {change:>+7.1f}%"
        )

    return {"scenarios": results, "text": "\n".join(text_parts)}


# ---------------------------------------------------------------------------
# @tool wrapper: verify_stock_valuation (for LLM agents)
# ---------------------------------------------------------------------------

@tool("verify_stock_valuation")
def verify_stock_valuation(
    code: Annotated[str, "A-stock code (e.g. 600519)"],
) -> str:
    """验算个股估值指标（PE/PB/ROE/市值），使用精确十进制计算避免浮点误差。
    自动从 playwright_service 获取总股本/EPS/每股净资产，从腾讯行情获取实时股价后验算。"""
    try:
        from tradingagents.agents.utils.playwright_tools import _get_client
        client = _get_client()

        # Get homepage data (PE/PB/market cap/shares)
        hp = client.stock_homepage(code)
        if not hp.get("success"):
            return f"[估值验算] {code}: 无法获取首页数据 - {hp.get('error', '')}"
        d = hp.get("data", {})

        pe_d = d.get("pe_dynamic")
        pe_s = d.get("pe_static")
        pb = d.get("pb")
        mcap_yi = d.get("total_mcap_yi")
        ts = d.get("total_shares_yi")
        fs = d.get("float_shares_yi")

        # Get precise real-time price from Tencent (avoid precision loss
        # from dividing market_cap_yi / shares_yi which are both in 亿 unit)
        price = None
        price_source = ""
        try:
            from tradingagents.dataflows.a_stock import _tencent_quote
            tq = _tencent_quote([code])
            if code in tq:
                price = tq[code].get("price")
                price_source = "腾讯行情"
        except Exception as e:
            logger.warning("Tencent quote failed in verify_stock_valuation for %s: %s", code, str(e)[:200])

        # Fallback 1: 东财 push2his 最新 K 线收盘价（精度到分）
        if price is None:
            try:
                from tradingagents.dataflows.a_stock import _em_fetch_klines
                klines = _em_fetch_klines(code, count=1)
                if klines:
                    price = klines[-1].get("close")
                    price_source = "东财push2his(最新收盘价)"
            except Exception as e:
                logger.warning("Eastmoney kline fallback failed for %s: %s", code, str(e)[:200])

        # Fallback 2: derive price from market_cap / shares (least precise, both in 亿)
        if price is None and mcap_yi and ts and ts > 0:
            price = mcap_yi / ts
            price_source = "市值/股本推算(低精度)"
            logger.info("Using fallback price %.4f (mcap/shares) for %s", price, code)

        lines = [f"# 估值验算 | {code}", "# 使用 Decimal 精确十进制计算", ""]

        if price is not None:
            lines.append(f"股价: {price:.2f} 元 ({price_source})")
        if ts is not None:
            lines.append(f"总股本: {ts:.4f}亿 | 流通A股: {fs:.4f}亿" if fs else f"总股本: {ts:.4f}亿")
        if mcap_yi is not None:
            lines.append(f"总市值: {mcap_yi:.2f}亿")

        # Verify market cap = price * shares (cross-check reported vs calculated)
        if price is not None and ts is not None and mcap_yi is not None:
            mc_result = verify_market_cap(
                price=price,
                shares=ts * 1e8,  # convert 亿 to 股
                reported_cap=mcap_yi * 1e8,  # convert 亿 to 元
            )
            lines.append(f"市值验算: {mc_result['text']} [{mc_result['status']}]")

        if pe_s is not None and pb is not None:
            # Cross-check PE and PB
            try:
                pe_val = float(pe_s) if not isinstance(pe_s, str) else float(pe_s)
                pb_val = float(pb) if not isinstance(pb, str) else float(pb)
                lines.append(f"报告PE(静态): {pe_val:.2f}x")
                lines.append(f"报告PB: {pb_val:.2f}x")
                if pe_val > 0 and pb_val > 0:
                    implied_roe = (pe_val / pb_val) * 100
                    lines.append(f"隐含ROE = PE/PB = {implied_roe:.2f}%")
                    if implied_roe > 30:
                        lines.append("  (ROE>30%, 优质企业)")
                    elif implied_roe > 15:
                        lines.append("  (ROE 15-30%, 良好)")
                    elif implied_roe > 8:
                        lines.append("  (ROE 8-15%, 一般)")
                    else:
                        lines.append("  (ROE<8%, 较低)")
            except (ValueError, TypeError):
                lines.append(f"PE={pe_s}, PB={pb} (无法计算隐含ROE)")

        # Get financial quarterly for EPS/BPS
        fq = client.financial_quarterly(code)
        if fq.get("success"):
            data = fq.get("data", [])
            if data:
                latest = data[0]
                eps = latest.get("EPS")
                bps = latest.get("BPS")
                if eps and bps:
                    if price is not None:
                        val_result = verify_valuation(
                            price=price,
                            eps=eps,
                            bvps=bps,
                        )
                        if "PE" in val_result:
                            lines.append(f"验算PE = 股价/EPS = {val_result['PE']:.2f}x")
                        if "PB" in val_result:
                            lines.append(f"验算PB = 股价/BPS = {val_result['PB']:.2f}x")
                        if "ROE" in val_result:
                            lines.append(f"验算ROE = EPS/BPS = {val_result['ROE']:.2f}%")
                    else:
                        lines.append("无法验算 PE/PB: 腾讯行情与市值推算股价均不可用")

        return "\n".join(lines)
    except Exception as e:
        return f"[估值验算] {code}: 获取异常: {type(e).__name__}: {e}"
