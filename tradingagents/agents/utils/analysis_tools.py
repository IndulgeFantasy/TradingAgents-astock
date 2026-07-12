"""K-line pattern recognition tools for A-stock technical analysis.

Pure algorithm, zero external dependencies beyond langchain_core.
Detects 12+ candlestick and chart patterns from OHLCV data.
"""

from __future__ import annotations

import logging
from typing import Annotated

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


def _detect_patterns(opens: list, highs: list, lows: list, closes: list,
                     volumes: list | None = None) -> list[dict]:
    """Detect candlestick and chart patterns from OHLCV arrays.

    Args:
        opens, highs, lows, closes: price arrays (oldest first)
        volumes: volume array (optional)
    Returns:
        list of pattern dicts: {pattern, type, day_offset, strength, desc}
    """
    n = len(closes)
    if n < 3:
        return []

    o = opens
    h = highs
    l = lows
    c = closes
    v = volumes

    def body(i):
        return abs(c[i] - o[i])

    def upper_shadow(i):
        return h[i] - max(c[i], o[i])

    def lower_shadow(i):
        return min(c[i], o[i]) - l[i]

    def is_bullish(i):
        return c[i] > o[i]

    def is_bearish(i):
        return c[i] < o[i]

    avg_body = sum(body(i) for i in range(n)) / n if n > 0 else 0
    if avg_body <= 0:
        return []

    patterns = []

    # ── Single-candle patterns (scan last 3 days) ──

    for i in range(max(0, n - 3), n):
        b = body(i)
        us = upper_shadow(i)
        ls = lower_shadow(i)

        # Doji (十字星)
        if b < avg_body * 0.1 and (us + ls) > b * 3:
            patterns.append({
                "pattern": "十字星 (Doji)",
                "type": "reversal_signal",
                "day_offset": i - n + 1,
                "strength": "弱",
                "desc": "多空力量均衡，可能反转",
            })

        # Hammer / Hanging Man (锤子线/上吊线)
        if ls > b * 2 and us < b * 0.5:
            if i > 0 and c[i] >= c[i - 1]:
                patterns.append({
                    "pattern": "锤子线 (Hammer)",
                    "type": "bullish_reversal",
                    "day_offset": i - n + 1,
                    "strength": "中",
                    "desc": "下影线长+小实体，底部反转信号",
                })
            else:
                patterns.append({
                    "pattern": "上吊线 (Hanging Man)",
                    "type": "bearish_reversal",
                    "day_offset": i - n + 1,
                    "strength": "中",
                    "desc": "高位出现长下影线，顶部警告",
                })

        # Shooting Star / Inverted Hammer (流星线/倒锤子)
        if us > b * 2 and ls < b * 0.5:
            if is_bearish(i):
                patterns.append({
                    "pattern": "流星线 (Shooting Star)",
                    "type": "bearish_reversal",
                    "day_offset": i - n + 1,
                    "strength": "中",
                    "desc": "上影线长+小实体，顶部反转信号",
                })
            else:
                patterns.append({
                    "pattern": "倒锤子 (Inverted Hammer)",
                    "type": "bullish_reversal",
                    "day_offset": i - n + 1,
                    "strength": "中",
                    "desc": "底部出现长上影线，可能反转",
                })

        # Large bullish/bearish candle (大阳线/大阴线)
        if b > avg_body * 2.5:
            if is_bullish(i):
                patterns.append({
                    "pattern": "大阳线 (Large Bullish)",
                    "type": "bullish",
                    "day_offset": i - n + 1,
                    "strength": "强",
                    "desc": "实体超过均值的2.5倍，强势做多",
                })
            else:
                patterns.append({
                    "pattern": "大阴线 (Large Bearish)",
                    "type": "bearish",
                    "day_offset": i - n + 1,
                    "strength": "强",
                    "desc": "实体超过均值的2.5倍，强势做空",
                })

    # ── Multi-candle patterns (use last 3 candles, i = n-1) ──

    i = n - 1

    if n >= 3:
        # Morning Star (早晨之星)
        if (is_bearish(i - 2) and body(i - 2) > avg_body * 1.5
                and body(i - 1) < avg_body * 0.4
                and is_bullish(i) and body(i) > avg_body * 1.5
                and c[i] > (o[i - 2] + c[i - 2]) / 2):
            patterns.append({
                "pattern": "早晨之星 (Morning Star)",
                "type": "bullish_reversal",
                "day_offset": -2,
                "strength": "强",
                "desc": "三根K线底部反转形态",
            })

        # Evening Star (黄昏之星)
        if (is_bullish(i - 2) and body(i - 2) > avg_body * 1.5
                and body(i - 1) < avg_body * 0.4
                and is_bearish(i) and body(i) > avg_body * 1.5
                and c[i] < (o[i - 2] + c[i - 2]) / 2):
            patterns.append({
                "pattern": "黄昏之星 (Evening Star)",
                "type": "bearish_reversal",
                "day_offset": -2,
                "strength": "强",
                "desc": "三根K线顶部反转形态",
            })

        # Bullish Engulfing (看涨吞没)
        if (is_bullish(i) and is_bearish(i - 1)
                and o[i] < c[i - 1] and c[i] > o[i - 1]):
            patterns.append({
                "pattern": "看涨吞没 (Bullish Engulfing)",
                "type": "bullish_reversal",
                "day_offset": -1,
                "strength": "强",
                "desc": "阳线完全吞没前阴线",
            })

        # Bearish Engulfing (看跌吞没)
        if (is_bearish(i) and is_bullish(i - 1)
                and o[i] > c[i - 1] and c[i] < o[i - 1]):
            patterns.append({
                "pattern": "看跌吞没 (Bearish Engulfing)",
                "type": "bearish_reversal",
                "day_offset": -1,
                "strength": "强",
                "desc": "阴线完全吞没前阳线",
            })

    # ── Chart patterns (entire window) ──

    # Double Bottom (双底)
    if n >= 15:
        recent_lows = sorted(range(n), key=lambda x: l[x])[:5]
        recent_lows.sort()
        if len(recent_lows) >= 2:
            lo1, lo2 = recent_lows[0], recent_lows[1]
            if lo2 - lo1 >= 5 and abs(l[lo1] - l[lo2]) / max(l[lo1], l[lo2]) < 0.03:
                mid_high = max(h[lo1:lo2 + 1]) if lo2 > lo1 else 0
                if mid_high > l[lo1] * 1.03:
                    patterns.append({
                        "pattern": "双底 (Double Bottom)",
                        "type": "bullish_reversal",
                        "day_offset": lo2 - n + 1,
                        "strength": "强",
                        "desc": "两个低点价格接近，中间有反弹",
                    })

    # Volume breakout above 20-day high (放量突破20日高点)
    if n >= 21:
        high_20 = max(h[n - 21:n - 1])
        vol_now = v[-1] if v else None
        vol_avg = sum(v[n - 6:n - 1]) / 5 if v and len(v) >= 6 else None
        if c[-1] > high_20:
            vol_ok = (vol_now and vol_avg and vol_now > vol_avg * 1.5) if vol_now else True
            if vol_ok:
                patterns.append({
                    "pattern": "放量突破20日高点 (Volume Breakout)",
                    "type": "bullish_breakout",
                    "day_offset": 0,
                    "strength": "强",
                    "desc": "收盘价突破20日最高点且放量确认",
                })

    # Box consolidation (箱体震荡)
    if n >= 10:
        recent_10_h = max(h[n - 10:])
        recent_10_l = min(l[n - 10:])
        if recent_10_l > 0 and (recent_10_h - recent_10_l) / recent_10_l * 100 < 8:
            patterns.append({
                "pattern": "箱体震荡 (Box Consolidation)",
                "type": "consolidation",
                "day_offset": 0,
                "strength": "中",
                "desc": f"近10日振幅仅{(recent_10_h - recent_10_l) / recent_10_l * 100:.1f}%，区间整理",
            })

    return patterns


@tool("analyze_pattern")
def analyze_pattern(
    code: Annotated[str, "A-stock code (e.g. 600519)"],
    days: Annotated[int, "Number of K-line days to analyze (default 60)"] = 60,
) -> str:
    """识别K线形态：十字星/锤子线/吞没/早晨之星/双底/放量突破/箱体震荡等12种形态。
    数据源: 东财K线（复用 get_stock_kline_full）"""
    try:
        from tradingagents.agents.utils.playwright_tools import _get_client
        client = _get_client()
        result = client.stock_kline_full(code, days)
        if not result.get("success"):
            return f"[K线形态] {code}: {result.get('error', '')}"
        records = result.get("data", [])
        if not records or len(records) < 3:
            return f"[K线形态] {code}: K线数据不足({len(records) if records else 0}根)"

        opens = [r.get("open", 0) for r in records]
        highs = [r.get("high", 0) for r in records]
        lows = [r.get("low", 0) for r in records]
        closes = [r.get("close", 0) for r in records]
        volumes = [r.get("volume", 0) for r in records]

        patterns = _detect_patterns(opens, highs, lows, closes, volumes)

        stock_name = result.get("stock_name", "")
        name_display = f" {stock_name}" if stock_name else ""
        lines = [
            f"# K线形态识别 {code}{name_display} | 近{len(records)}根K线",
            f"# 数据源: 东财push2his",
            "",
        ]

        if not patterns:
            lines.append("未发现明显K线形态")
        else:
            # Deduplicate by pattern name (keep most recent)
            seen = {}
            for p in patterns:
                name = p["pattern"]
                if name not in seen or p["day_offset"] > seen[name]["day_offset"]:
                    seen[name] = p
            unique = sorted(seen.values(), key=lambda x: x["day_offset"], reverse=True)

            lines.append(f"发现 {len(unique)} 个形态:")
            lines.append("")
            for p in unique:
                lines.append(f"  [{p['strength']}] {p['pattern']}")
                lines.append(f"    类型: {p['type']} | 偏移: {p['day_offset']}日 | {p['desc']}")

        return "\n".join(lines)
    except Exception as e:
        return f"[K线形态] 获取异常: {e}"
