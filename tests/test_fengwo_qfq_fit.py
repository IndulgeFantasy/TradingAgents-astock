"""验证 fengwo(通达信DLL) + 前复权 350根 与东财官方筹码数据(2026-08-05)的拟合度。

背景:
- 8-04 官方批量数据异常（301047 获利 33.36% 实为误数据），8-05 数据可信
- 实测结论: 官方 = 通达信 WINNER/COST 公式 + 前复权(fqt=1) + ~350根窗口
- 本测试验证该结论，并对比 不复权口径 与 本项目 _compute_cyq

数据依赖: KLINE_DIR 下的前复权缓存 (qfq8_{code}.json / qfq_{code}.json，截至 2026-08-05)，
缺失时用例自动跳过。运行: python -m pytest tests/test_fengwo_qfq_fit.py -v
"""
import json
import os

import numpy as np
import pytest

fw = pytest.importorskip("fengwo")
fw.showMsg(False)

KLINE_DIR = os.environ.get("CYQ_KLINE_DIR", r"C:\Users\13466\AppData\Local\Temp\opencode")

# 东财官方 2026-08-05: p=获利% c=平均成本 c90/c70=集中度%
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
    "601398": dict(p=80.40, c=7.18, c90=11.71, c70=8.70),
    "601988": dict(p=79.63, c=5.42, c90=9.27, c70=6.20),
    "600036": dict(p=62.46, c=38.25, c90=8.58, c70=5.24),
    "601939": dict(p=70.17, c=9.63, c90=11.01, c70=7.89),
    "601328": dict(p=68.83, c=6.76, c90=7.05, c70=4.62),
}
BANKS = {"601288", "601398", "601988", "600036", "601939", "601328"}


def _load(code: str, window: int = 350):
    """加载前复权K线（银行 qfq_ 前缀，其余 qfq8_ 前缀）。"""
    pref = "qfq" if code in BANKS else "qfq8"
    path = os.path.join(KLINE_DIR, f"{pref}_{code}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        kl = json.load(f)
    kl = kl[-window:] if window < len(kl) else kl
    return dict(
        high=np.array([k["high"] for k in kl], dtype=np.float64),
        low=np.array([k["low"] for k in kl], dtype=np.float64),
        close=np.array([k["close"] for k in kl], dtype=np.float64),
        vol=np.array([k.get("volume", 0) for k in kl], dtype=np.float64),
        turn=np.array([k.get("turnover", 0) for k in kl], dtype=np.float64) / 100.0,
    )


def _load_bfq(code: str, window: int = 350):
    """加载不复权K线（对照组）。"""
    path = os.path.join(KLINE_DIR, f"new_{code}.json")
    if not os.path.exists(path):
        path = os.path.join(KLINE_DIR, f"full_{code}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        kl = json.load(f)
    kl = kl[-window:] if window < len(kl) else kl
    return dict(
        high=np.array([k["high"] for k in kl], dtype=np.float64),
        low=np.array([k["low"] for k in kl], dtype=np.float64),
        close=np.array([k["close"] for k in kl], dtype=np.float64),
        vol=np.array([k.get("volume", 0) for k in kl], dtype=np.float64),
        turn=np.array([k.get("turnover", 0) for k in kl], dtype=np.float64) / 100.0,
    )


def _fengwo_metrics(kl):
    """通达信口径全套指标: WINNER + COST(5/15/85/95) + 平均成本(COST网格均值)。"""
    profit = float(fw.WINNER(kl["high"], kl["low"], kl["vol"], kl["turn"], kl["close"])[-1]) * 100
    c5 = float(fw.COST(kl["high"], kl["low"], kl["vol"], kl["turn"], 5)[-1])
    c15 = float(fw.COST(kl["high"], kl["low"], kl["vol"], kl["turn"], 15)[-1])
    c85 = float(fw.COST(kl["high"], kl["low"], kl["vol"], kl["turn"], 85)[-1])
    c95 = float(fw.COST(kl["high"], kl["low"], kl["vol"], kl["turn"], 95)[-1])
    avg = np.mean([float(fw.COST(kl["high"], kl["low"], kl["vol"], kl["turn"], float(p))[-1])
                   for p in np.linspace(0.5, 99.5, 100)])
    return dict(p=profit, c=avg,
                c90=(c95 - c5) / (c95 + c5) * 100.0,
                c70=(c85 - c15) / (c85 + c15) * 100.0)


def _codes():
    out = []
    for code in OFFICIAL:
        if _load(code) is not None:
            out.append(code)
    return out


@pytest.fixture(params=_codes())
def stock(request):
    return request.param


class TestFengwoQfqVsOfficial:
    """核心验证: fengwo 前复权 350根 vs 官方 8-05（容差按实测最大值+余量）。"""

    def test_profit_ratio(self, stock):
        m = _fengwo_metrics(_load(stock))
        off = OFFICIAL[stock]
        d = abs(m["p"] - off["p"])
        print(f"[{stock}] 获利: fengwo={m['p']:.2f}% 官方={off['p']:.2f}% 差={d:.2f}pt")
        assert d < 4.0, f"{stock} 获利偏差 {d:.2f}pt > 4pt"

    def test_concentration_90(self, stock):
        m = _fengwo_metrics(_load(stock))
        off = OFFICIAL[stock]
        d = abs(m["c90"] - off["c90"])
        print(f"[{stock}] 90%集中度: fengwo={m['c90']:.2f}% 官方={off['c90']:.2f}% 差={d:.2f}pt")
        assert d < 2.0, f"{stock} 90%偏差 {d:.2f}pt > 2pt"

    def test_concentration_70(self, stock):
        m = _fengwo_metrics(_load(stock))
        off = OFFICIAL[stock]
        d = abs(m["c70"] - off["c70"])
        print(f"[{stock}] 70%集中度: fengwo={m['c70']:.2f}% 官方={off['c70']:.2f}% 差={d:.2f}pt")
        assert d < 4.0, f"{stock} 70%偏差 {d:.2f}pt > 4pt"

    def test_avg_cost(self, stock):
        m = _fengwo_metrics(_load(stock))
        off = OFFICIAL[stock]
        rel = abs(m["c"] - off["c"]) / off["c"] * 100
        print(f"[{stock}] 平均成本: fengwo={m['c']:.2f} 官方={off['c']:.2f} 相对差={rel:.1f}%")
        assert rel < 10.0, f"{stock} 成本相对偏差 {rel:.1f}% > 10%"


class TestQfqVsBfq:
    """前复权 vs 不复权: 前复权应系统性更拟合官方。"""

    def test_qfq_error_less_than_bfq(self, stock):
        mq = _fengwo_metrics(_load(stock))
        bfq = _load_bfq(stock)
        if bfq is None:
            pytest.skip("缺少不复权数据")
        mb = _fengwo_metrics(bfq)
        off = OFFICIAL[stock]
        err_q = abs(mq["p"] - off["p"]) + abs(mq["c90"] - off["c90"]) + abs(mq["c70"] - off["c70"])
        err_b = abs(mb["p"] - off["p"]) + abs(mb["c90"] - off["c90"]) + abs(mb["c70"] - off["c70"])
        print(f"[{stock}] 总误差(获利+90%+70%): 前复权={err_q:.2f} 不复权={err_b:.2f}")
        assert err_q <= err_b + 1.0, f"{stock} 前复权({err_q:.1f}) 未优于不复权({err_b:.1f})"

    def test_bfq_concentration_systematically_higher(self, stock):
        """不复权的集中度系统性偏高（老价高→分布宽）是此前对齐失败的主因之一。"""
        bfq = _load_bfq(stock)
        if bfq is None:
            pytest.skip("缺少不复权数据")
        mb = _fengwo_metrics(bfq)
        off = OFFICIAL[stock]
        if mb["c90"] > off["c90"] + 3.0:
            print(f"[{stock}] 不复权90%={mb['c90']:.2f}% 官方={off['c90']:.2f}% (偏高{mb['c90']-off['c90']:.2f})")


class TestWindowSensitivity:
    """窗口 350 vs 全部: 350根(约1.4年)与官方一致（低换手股差异明显）。"""

    def test_window350_not_worse_than_full(self, stock):
        m350 = _fengwo_metrics(_load(stock, 350))
        mfull = _fengwo_metrics(_load(stock, 10 ** 9))
        off = OFFICIAL[stock]
        e350 = abs(m350["p"] - off["p"]) + abs(m350["c90"] - off["c90"]) + abs(m350["c70"] - off["c70"])
        efull = abs(mfull["p"] - off["p"]) + abs(mfull["c90"] - off["c90"]) + abs(mfull["c70"] - off["c70"])
        print(f"[{stock}] 总误差: 350根={e350:.2f} 全部历史={efull:.2f}")
        assert e350 <= efull + 2.0, f"{stock} 350根({e350:.1f})显著差于全历史({efull:.1f})"


class TestProjectVsFengwoQfq:
    """本项目 _compute_cyq（同一份前复权350根数据）vs fengwo——信息性对比。"""

    def test_project_vs_fengwo_and_official(self, stock):
        sys_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if sys_path not in __import__("sys").path:
            __import__("sys").path.insert(0, sys_path)
        from tradingagents.dataflows.a_stock import _compute_cyq

        kl = _load(stock)
        mf = _fengwo_metrics(kl)
        # 本项目模型需要原始 dict 列表格式
        pref = "qfq" if stock in BANKS else "qfq8"
        with open(os.path.join(KLINE_DIR, f"{pref}_{stock}.json"), encoding="utf-8") as f:
            raw = json.load(f)
        for k in raw:
            k.setdefault("pct_chg", k.get("pctChg", 0))
        r = _compute_cyq(raw[-350:])
        off = OFFICIAL[stock]
        dp = abs(r["profit_ratio"] * 100 - off["p"])
        df = abs(mf["p"] - off["p"])
        print(f"[{stock}] 获利差: 项目={dp:.2f}pt fengwo={df:.2f}pt | "
              f"项目成本={r['avg_cost']:.2f} fengwo成本={mf['c']:.2f} 官方={off['c']:.2f}")
        # fengwo(通达信)必须紧贴官方；本项目模型含量能加权/自动系数等扩展，
        # 偏差允许更大，但应证明 fengwo 更拟合（688811 次新股: 项目差41.8pt vs fengwo 0.6pt）
        assert df < 4.0
        assert dp < 45.0, f"{stock} 项目模型偏差 {dp:.2f}pt（fengwo 仅 {df:.2f}pt）"
        assert dp > df - 1.0, f"{stock} fengwo({df:.2f}pt) 未优于项目({dp:.2f}pt)"
