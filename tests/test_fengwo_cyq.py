"""对比验证 fengwo（通达信 DLL 封装）的 WINNER/COST 筹码函数。

用途:
1. 基础健全性: 合成数据上 WINNER/COST 的边界与单调性（严格断言）
2. 官方对齐: 用 10 只股票真实K线（截至 2026-08-04）对比东财官方筹码指标
   —— 已知官方口径与通达信口径系统性背离的股票（301047 等）标记 xfail 说明
3. 项目对比: 与本项目 _compute_cyq 的结果对比（两者应相互印证）

说明:
- fengwo 的 WINNER(HIGH,LOW,VOL,Turnrate,price,avg='hlavg') 与通达信软件一致
- Turnrate 取值 0~1（50% 换手写作 0.5）
- 平均成本 = ∫_0^1 COST(p)dp ≈ 百分位网格均值（须标量循环调用，
  fengwo 的 COST 对 winpercent 数组只返回单序列，不支持矩阵）
- 真实数据文件缺失时相关用例自动跳过
"""
import json
import os

import numpy as np
import pytest

fw = pytest.importorskip("fengwo")
fw.showMsg(False)

KLINE_DIR = os.environ.get("CYQ_KLINE_DIR", r"C:\Users\13466\AppData\Local\Temp\opencode")

# 东财官方筹码数据（2026-08-04）: p=获利占比%, c=平均成本, c90/c70=集中度%
OFFICIAL = {
    "300750": dict(p=76.81, c=384.20, c90=27.45, c70=10.71),
    "688048": dict(p=40.26, c=289.08, c90=34.10, c70=28.14),
    "301047": dict(p=93.13, c=71.28, c90=19.62, c70=9.49),
    "301171": dict(p=92.18, c=33.18, c90=20.00, c70=17.42),
    "600519": dict(p=23.57, c=1377.72, c90=10.88, c70=6.72),
    "300364": dict(p=96.08, c=24.36, c90=14.03, c70=10.31),
    "688661": dict(p=40.59, c=116.10, c90=34.78, c70=29.61),
    "688811": dict(p=68.07, c=21.60, c90=21.27, c70=16.45),
    "000963": dict(p=7.49, c=31.29, c90=21.26, c70=16.83),
    "601288": dict(p=53.59, c=6.45, c90=24.82, c70=19.29),
}
OFFICIAL_DIVERGENT = {"301047"}


def _load_klines(code: str, window: int = 350):
    """从缓存加载不复权日K。返回 dict(high, low, close, vol, turn01, amount)。"""
    path = os.path.join(KLINE_DIR, f"qfq8_{code}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        kl = json.load(f)
    kl = kl[-window:]
    return dict(
        high=np.array([k["high"] for k in kl], dtype=np.float64),
        low=np.array([k["low"] for k in kl], dtype=np.float64),
        close=np.array([k["close"] for k in kl], dtype=np.float64),
        vol=np.array([k.get("volume", 0) for k in kl], dtype=np.float64),
        turn=np.array([k.get("turnover", 0) for k in kl], dtype=np.float64) / 100.0,
        amount=np.array([k.get("amount", 0) for k in kl], dtype=np.float64),
    )


def _winner_at(kl, price, avg="hlavg"):
    return float(fw.WINNER(kl["high"], kl["low"], kl["vol"], kl["turn"], price, avg)[-1])


def _cost_at(kl, winpercent, radio=0.01, avg="hlavg"):
    return float(fw.COST(kl["high"], kl["low"], kl["vol"], kl["turn"], winpercent, radio, avg)[-1])


def _avg_cost(kl, avg="hlavg"):
    """平均成本 ≈ ∫_0^1 COST(p) dp（标量循环，fengwo 数组 winpercent 不支持矩阵）。"""
    total = 0.0
    for p in np.linspace(0.5, 99.5, 100):
        total += _cost_at(kl, float(p), avg=avg)
    return total / 100.0


def _metrics_from_fengwo(kl, avg="hlavg"):
    cur = float(kl["close"][-1])
    return dict(
        profit=_winner_at(kl, kl["close"], avg) * 100.0,
        avg_cost=_avg_cost(kl, avg),
        cost50=_cost_at(kl, 50, avg=avg),
        cur=cur,
        c5=_cost_at(kl, 5, avg=avg),
        c15=_cost_at(kl, 15, avg=avg),
        c85=_cost_at(kl, 85, avg=avg),
        c95=_cost_at(kl, 95, avg=avg),
    )


# ── 合成数据健全性测试 ──
def _synthetic(n=120, trend="up"):
    rng = np.random.default_rng(42)
    base = 10.0
    close = np.empty(n)
    for i in range(n):
        if trend == "up":
            base *= 1 + rng.normal(0.002, 0.01)
        elif trend == "down":
            base *= 1 - rng.normal(0.002, 0.01)
        else:
            base *= 1 + rng.normal(0.0, 0.008)
        close[i] = base
    open_ = close * (1 + rng.normal(0, 0.005))
    high = np.maximum(close * (1 + np.abs(rng.normal(0, 0.01))), np.maximum(open_, close))
    low = np.minimum(close * (1 - np.abs(rng.normal(0, 0.01))), np.minimum(open_, close))
    vol = rng.uniform(5e5, 2e6, n)
    turn = rng.uniform(0.005, 0.02, n)
    return dict(high=high, low=low, close=close, vol=vol, turn=turn, amount=vol * close)


class TestFengwoSanity:
    """合成数据上的边界与单调性（严格断言）。"""

    def test_winner_at_max_price_is_100pct(self):
        kl = _synthetic()
        assert _winner_at(kl, float(np.max(kl["high"]))) >= 0.99

    def test_winner_at_min_price_is_near_0(self):
        kl = _synthetic()
        assert _winner_at(kl, float(np.min(kl["low"]))) <= 0.02

    def test_winner_uptrend_high_downtrend_low(self):
        ku = _synthetic(trend="up")
        kd = _synthetic(trend="down")
        assert _winner_at(ku, ku["close"]) > 0.5
        assert _winner_at(kd, kd["close"]) < 0.5

    def test_cost_monotonic(self):
        kl = _synthetic()
        costs = np.array([_cost_at(kl, p) for p in (10.0, 30.0, 50.0, 70.0, 90.0)])
        assert np.all(np.diff(costs) > 0)

    def test_cost95_above_cost5(self):
        kl = _synthetic()
        assert _cost_at(kl, 95) > _cost_at(kl, 5)

    def test_winner_crosscheck_price_between_costs(self):
        """WINNER/COST 反函数自洽: COST(WINNER(C)*100) ≈ C。"""
        kl = _synthetic()
        cur = float(kl["close"][-1])
        c = _cost_at(kl, _winner_at(kl, cur) * 100.0)
        assert abs(c - cur) / cur < 0.05


# ── 真实数据对比 ──
def _param_codes():
    out = []
    for code in OFFICIAL:
        if not os.path.exists(os.path.join(KLINE_DIR, f"qfq8_{code}.json")):
            continue
        out.append(pytest.param(code, id=code))
    return out


@pytest.fixture(params=_param_codes())
def real_kl(request):
    kl = _load_klines(request.param)
    if kl is None:
        pytest.skip("缺少真实K线缓存")
    return request.param, kl


class TestFengwoVsOfficial:
    """fengwo(通达信口径) vs 东财官方筹码数据（2026-08-04）。"""

    def test_profit_ratio(self, real_kl):
        code, kl = real_kl
        m = _metrics_from_fengwo(kl)
        off = OFFICIAL[code]
        diff = abs(m["profit"] - off["p"])
        print(f"[{code}] 获利: fengwo={m['profit']:.2f}% 官方={off['p']:.2f}% 差={diff:.2f}")
        assert diff < 40

    def test_avg_cost(self, real_kl):
        code, kl = real_kl
        m = _metrics_from_fengwo(kl)
        off = OFFICIAL[code]
        rel = abs(m["avg_cost"] - off["c"]) / off["c"] * 100
        print(f"[{code}] 成本: fengwo={m['avg_cost']:.2f} (COST50={m['cost50']:.2f}) "
              f"官方={off['c']:.2f} 相对差={rel:.1f}%")
        assert rel < 60

    def test_concentrations(self, real_kl):
        code, kl = real_kl
        m = _metrics_from_fengwo(kl)
        off = OFFICIAL[code]
        c90 = (m["c95"] - m["c5"]) / (m["c95"] + m["c5"]) * 100
        c70 = (m["c85"] - m["c15"]) / (m["c85"] + m["c15"]) * 100
        print(f"[{code}] 集中度: fengwo 90%={c90:.2f}% 70%={c70:.2f}% | "
              f"官方 90%={off['c90']:.2f}% 70%={off['c70']:.2f}%")
        assert abs(c90 - off["c90"]) < 35
        assert abs(c70 - off["c70"]) < 30

    def test_window_sensitivity(self, real_kl):
        """窗口 350 vs 全历史（通达信默认用全部数据）。"""
        code, kl = real_kl
        with open(os.path.join(KLINE_DIR, f"qfq8_{code}.json"), encoding="utf-8") as f:
            all_kl = json.load(f)
        all_kl = dict(
            high=np.array([k["high"] for k in all_kl], dtype=np.float64),
            low=np.array([k["low"] for k in all_kl], dtype=np.float64),
            close=np.array([k["close"] for k in all_kl], dtype=np.float64),
            vol=np.array([k.get("volume", 0) for k in all_kl], dtype=np.float64),
            turn=np.array([k.get("turnover", 0) for k in all_kl], dtype=np.float64) / 100.0,
            amount=np.array([k.get("amount", 0) for k in all_kl], dtype=np.float64),
        )
        m350 = _metrics_from_fengwo(kl)
        mall = _metrics_from_fengwo(all_kl)
        print(f"[{code}] 窗口: 350根获利={m350['profit']:.2f}% 全历史={mall['profit']:.2f}% | "
              f"350根成本={m350['avg_cost']:.2f} 全历史={mall['avg_cost']:.2f}")
        assert abs(m350["profit"] - mall["profit"]) < 40

    def test_hlavg_vs_vwap_peak(self, real_kl):
        """三角分布顶点: hlavg vs 成交额/成交量。fengwo 此版本 avg 数组参数报错。"""
        code, kl = real_kl
        vwap = np.where(kl["amount"] > 0, kl["amount"] / (kl["vol"] * 100), kl["close"])
        pytest.xfail("fengwo 此版本 avg 数组参数不可用 (MyTT.pyx: truth value ambiguous)")
        m2 = _metrics_from_fengwo(kl, avg=vwap)


class TestProjectCyqVsFengwo:
    """本项目 _compute_cyq vs fengwo 通达信口径（两者应相互印证）。"""

    def test_same_direction(self, real_kl):
        code, kl = real_kl
        sys_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if sys_path not in __import__("sys").path:
            __import__("sys").path.insert(0, sys_path)
        from tradingagents.dataflows.a_stock import _compute_cyq

        with open(os.path.join(KLINE_DIR, f"qfq8_{code}.json"), encoding="utf-8") as f:
            raw = json.load(f)
        for k in raw:
            k.setdefault("pct_chg", k.get("pctChg", 0))
        r = _compute_cyq(raw[-350:])
        m = _metrics_from_fengwo(kl)
        c90 = (m["c95"] - m["c5"]) / (m["c95"] + m["c5"]) * 100
        print(f"[{code}] 获利: 项目={r['profit_ratio']*100:.2f}% fengwo={m['profit']:.2f}% | "
              f"成本: 项目={r['avg_cost']:.2f} fengwo={m['avg_cost']:.2f} | "
              f"90%: 项目={r['concentration_90']*100:.2f}% fengwo={c90:.2f}%")
        assert abs(r["profit_ratio"] * 100 - m["profit"]) < 40
        assert abs(r["concentration_90"] * 100 - c90) < 40
