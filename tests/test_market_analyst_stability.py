"""技术分析师(market_analyst)数据链路稳定性 / 偶发失败诊断测试。

背景
----
市场分析师节点 (market_analyst) 依赖 7 个工具，其中 6 个走网络:

    get_market_context     -> playwright /api/market-overview    (东财7大指数页, 冷启动实测 ~78s)
    get_stock_kline_full   -> playwright /api/stock-kline-full   (东财push2his, 冷启动 ~14s)
    get_stock_levels       -> playwright /api/stock-levels       (问财 kline2, ~7s)
    get_chip_distribution  -> a_stock -> playwright fqt=1 350根   (独立 90s 超时客户端)
    get_industry_hotmap    -> playwright /api/industry-hotmap    (东财大盘星图, ~3s)
    get_indicators         -> mootdx TCP + 新浪 HTTP 兜底
    analyze_pattern        -> 复用 /api/stock-kline-full

已知风险点（偶发失败高发源）:
1. playwright_service 服务端所有 Chrome 页面操作经 _cdp_lock 全局串行化，
   并发/排队场景单请求耗时被放大；客户端默认超时仅 30s (AKS_TIMEOUT)
   -> get_market_context 冷启动 70s+ 时必现 TimeoutError
2. 客户端熔断器: 连续 5 次传输失败 -> 熔断 60s，期间所有 playwright 工具
   统一返回 "服务不可用（熔断中）"，表现为整批偶发失败
3. 服务端 TTL=300s 缓存命中时很快；缓存过期 + 多个工具排队 -> 超时集中爆发

运行前提: playwright 数据服务已启动 (worktrade2 环境)。
服务不可达时相关用例自动跳过。

用法
----
    pytest tests/test_market_analyst_stability.py -v -s
    TA_TEST_ROUNDS=3 TA_TEST_TICKERS=600519,300750 pytest tests/... -v -s
    python tests/test_market_analyst_stability.py            # 诊断模式(轮次更多+修复建议)

环境变量
--------
    AKS_BASE_URL         服务地址 (默认 http://127.0.0.1:8765)
    TA_TEST_TICKERS      标的列表，逗号分隔 (默认 600519,300750,000001)
    TA_TEST_ROUNDS       每个端点重复轮数 (默认 2)
    TA_TEST_FAIL_RATE    总体失败率断言上限 (默认 0.35)
    TA_TEST_WORKERS      并发模拟 worker 数 (默认 4)
    TA_TEST_SESSIONS     每个 worker 的会话数 (默认 2)
"""

import argparse
import logging
import os
import sys
import threading
import time
from collections import defaultdict, Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

from playwright_service.client import PlaywrightClient  # noqa: E402
from tradingagents.agents.utils.agent_utils import (  # noqa: E402
    get_indicators,
    get_market_context,
    get_stock_kline_full,
    get_stock_levels,
    get_chip_distribution,
    get_industry_hotmap,
    analyze_pattern,
)

TODAY = datetime.now().strftime("%Y-%m-%d")

DEFAULT_TICKERS = os.getenv("TA_TEST_TICKERS", "600519,300750,000001").split(",")
ROUNDS = int(os.getenv("TA_TEST_ROUNDS", "2"))
FAIL_RATE_MAX = float(os.getenv("TA_TEST_FAIL_RATE", "0.35"))
WORKERS = int(os.getenv("TA_TEST_WORKERS", "4"))
SESSIONS = int(os.getenv("TA_TEST_SESSIONS", "2"))

# 与 market_analyst.py 的 tools 列表保持一致
TOOLS = {
    "get_market_context": get_market_context,
    "get_stock_kline_full": get_stock_kline_full,
    "get_indicators": get_indicators,
    "get_stock_levels": get_stock_levels,
    "get_chip_distribution": get_chip_distribution,
    "get_industry_hotmap": get_industry_hotmap,
    "analyze_pattern": analyze_pattern,
}

SESSION_SEQUENCE = [
    ("get_market_context", lambda c: {}),
    ("get_stock_kline_full", lambda c: {"code": c, "days": 30}),
    ("get_indicators", lambda c: {"symbol": c, "indicator": "rsi",
                                  "curr_date": TODAY, "look_back_days": 30}),
    ("get_stock_levels", lambda c: {"code": c}),
    ("get_chip_distribution", lambda c: {"ticker": c}),
    ("get_industry_hotmap", lambda c: {"level": "bk2", "top_n": 20, "ticker": c}),
    ("analyze_pattern", lambda c: {"code": c, "days": 60}),
]

# 失败分类: 按出现顺序匹配，越靠前越具体
FAILURE_MARKERS = [
    ("circuit_breaker", ["熔断"]),
    ("timeout", ["TimeoutError", "timed out", "DeadlineExceeded"]),
    ("transport", ["URLError", "连接失败", "ConnectionError", "Connection refused"]),
    ("server_error", ["HTTP 500", "Internal Server Error"]),
    ("tool_exception", ["获取异常"]),
    ("fetch_failed", ["获取失败"]),
    ("empty_data", ["无数据", "数据不足", "未返回"]),
    ("indicator_error", ["Error calculating"]),
]


def classify(out: str):
    """按工具返回文本分类失败原因；返回 None 表示成功。"""
    for cat, markers in FAILURE_MARKERS:
        for m in markers:
            if m in out:
                return cat
    if out.startswith("["):
        return "unknown_failure"
    return None


class Stats:
    """线程安全的调用统计收集器。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.calls = defaultdict(list)  # tool -> [(ok, cat, elapsed, ticker)]

    def add(self, tool, ok, cat, elapsed, ticker):
        with self._lock:
            self.calls[tool].append((ok, cat, elapsed, ticker))

    def summary(self):
        rows = []
        total_ok = total = 0
        for tool, records in sorted(self.calls.items()):
            n = len(records)
            oks = sum(1 for r in records if r[0])
            els = [r[2] for r in records]
            cats = Counter(r[1] for r in records if r[1])
            rows.append({
                "tool": tool, "n": n, "ok": oks,
                "rate": oks / n if n else 1.0,
                "avg": sum(els) / n if n else 0,
                "max": max(els) if els else 0,
                "cats": dict(cats),
            })
            total_ok += oks
            total += n
        return rows, total_ok, total

    def print_report(self, title):
        rows, total_ok, total = self.summary()
        print(f"\n{'=' * 78}")
        print(f"  数据链路稳定性报告: {title}")
        print(f"{'=' * 78}")
        print(f"  {'端点':<22} {'调用':>4} {'成功':>4} {'成功率':>8} "
              f"{'平均':>8} {'最慢':>8}  失败分类")
        for r in rows:
            cats = " ".join(f"{k}={v}" for k, v in sorted(r["cats"].items()))
            print(f"  {r['tool']:<22} {r['n']:>4} {r['ok']:>4} "
                  f"{r['rate'] * 100:>6.1f}% {r['avg']:>7.1f}s {r['max']:>7.1f}s  {cats}")
        print(f"  {'-' * 72}")
        print(f"  合计: {total_ok}/{total} 成功 ({total_ok / total * 100:.1f}%) "
              f"| 阈值: {FAIL_RATE_MAX * 100:.0f}%")
        if total_ok < total:
            self._print_hints(rows)
        print(f"{'=' * 78}\n")

    def _print_hints(self, rows):
        print("  诊断提示:")
        for r in rows:
            if r["cats"].get("timeout"):
                print(f"    - {r['tool']}: 出现 {r['cats']['timeout']} 次超时。"
                      f"客户端默认超时 30s (AKS_TIMEOUT)，服务端 _cdp_lock 串行 + 冷启动耗时 "
                      f"最长可达 ~80s，缓存过期后排队请求极易超时。")
            if r["cats"].get("circuit_breaker"):
                print(f"    - {r['tool']}: 出现 {r['cats']['circuit_breaker']} 次熔断。"
                      f"连续 5 次传输失败触发熔断 60s，期间所有 playwright 工具全部失败。")
            if r["cats"].get("empty_data"):
                print(f"    - {r['tool']}: 出现 {r['cats']['empty_data']} 次空数据，"
                      f"该端点返回成功但无数据，多为页面未捕获到响应。")
        slow = [r for r in rows if r["max"] > 30]
        if slow:
            print("    - 最慢端点均超过客户端默认 30s 超时阈值: "
                  + ", ".join(f"{r['tool']}({r['max']:.0f}s)" for r in slow))
            print("      建议: 设 AKS_TIMEOUT=120 或提升 playwright_service 缓存命中率")
        total_fail = sum(1 for recs in self.calls.values()
                         for r in recs if not r[0])
        if total_fail == 0:
            print("    - 本轮全部成功（缓存命中为主），偶发失败在缓存过期后的冷调用时段出现")


def call_tool(name, args, stats, ticker):
    """执行一次工具调用，记录耗时与结果分类。"""
    tool = TOOLS[name]
    t0 = time.time()
    try:
        out = tool.invoke(args)
    except Exception as e:
        out = f"[{name}] 调用异常: {type(e).__name__}: {str(e)[:200]}"
    elapsed = time.time() - t0
    cat = classify(out)
    ok = cat is None
    stats.add(name, ok, cat, elapsed, ticker)
    return ok, cat, elapsed


# ── fixtures ──

@pytest.fixture(scope="session")
def pw_server():
    base = os.getenv("AKS_BASE_URL", "http://127.0.0.1:8765")
    try:
        c = PlaywrightClient(timeout=5)
        h = c.health()
        if not h.get("success"):
            pytest.skip(f"playwright 服务不可达: {base} - {h.get('error')}")
    except Exception as e:
        pytest.skip(f"playwright 服务不可达: {base} - {e}")
    return h


@pytest.fixture(scope="session", autouse=True)
def _reset_client_breaker(pw_server):
    """清零单例客户端的熔断器状态，避免受此前应用运行残留状态干扰。"""
    from tradingagents.agents.utils import playwright_tools
    client = playwright_tools._client
    if client is not None:
        with client._lock:
            client._fail_count = 0
            client._circuit_open_until = 0.0
    yield


@pytest.fixture()
def stats():
    return Stats()


# ── 测试 1: 服务健康 ──

def test_playwright_server_health(pw_server):
    print(f"\n服务: {pw_server.get('service')} | 缓存键数: {pw_server.get('cache_keys')} "
          f"| 运行时长: {pw_server.get('uptime'):.0f}s")
    assert pw_server.get("success")


# ── 测试 2: 逐端点稳定性（模拟分析师顺序调用全部工具）──

@pytest.mark.integration
def test_endpoint_stability(pw_server, stats):
    tickers = [t.strip() for t in DEFAULT_TICKERS if t.strip()]
    print(f"\n标的: {tickers} | 轮数: {ROUNDS} | 当前日期: {TODAY}")

    for code in tickers:
        for rnd in range(1, ROUNDS + 1):
            for name in TOOLS:
                args = SESSION_SEQUENCE[
                    next(i for i, (n, _) in enumerate(SESSION_SEQUENCE) if n == name)
                ][1](code)
                ok, cat, elapsed = call_tool(name, args, stats, code)
                mark = "OK " if ok else f"FAIL[{cat}]"
                print(f"  [{rnd}/{ROUNDS}] {code} {name:<22} {mark} {elapsed:6.1f}s")

    stats.print_report(f"顺序调用 {len(tickers)} 只 × {ROUNDS} 轮")

    rows, total_ok, total = stats.summary()
    rate = total_ok / total if total else 1.0
    assert rate >= 1 - FAIL_RATE_MAX, (
        f"总体成功率 {rate * 100:.1f}% 低于阈值 {(1 - FAIL_RATE_MAX) * 100:.0f}%"
    )


# ── 测试 3: 并发模拟（Web UI 多标的/多会话场景）──

@pytest.mark.integration
def test_concurrent_analyst_load(pw_server, stats):
    tickers = [t.strip() for t in DEFAULT_TICKERS if t.strip()]
    rng = __import__("random").Random(42)

    def worker(worker_id):
        for s in range(SESSIONS):
            code = rng.choice(tickers)
            for name, args_fn in SESSION_SEQUENCE:
                call_tool(name, args_fn(code), stats, f"{code}#w{worker_id}")

    print(f"\n并发 worker: {WORKERS} × 会话: {SESSIONS} | 标的池: {tickers}")
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = [pool.submit(worker, w) for w in range(WORKERS)]
        for f in as_completed(futs):
            f.result()
    print(f"并发压测耗时: {time.time() - t0:.1f}s")

    stats.print_report(f"并发 {WORKERS} worker × {SESSIONS} 会话")

    rows, total_ok, total = stats.summary()
    rate = total_ok / total if total else 1.0
    assert rate >= 1 - FAIL_RATE_MAX, (
        f"并发场景总体成功率 {rate * 100:.1f}% 低于阈值 {(1 - FAIL_RATE_MAX) * 100:.0f}%"
    )
    breaker = sum(r["cats"].get("circuit_breaker", 0) for r in rows)
    if breaker:
        print(f"\n⚠️ 并发期间熔断器被触发 {breaker} 次 —— 偶发批量失败的根因之一，"
              f"建议加大 AKS_TIMEOUT 或服务端提升缓存命中")


# ── 测试 4: get_market_context 冷启动耗时探测 ──

@pytest.mark.integration
def test_market_context_cold_start_probe(pw_server, stats):
    """探测大盘概览冷启动耗时，验证是否超过客户端默认 30s 超时。"""
    default_client = PlaywrightClient()
    t0 = time.time()
    r = default_client.market_overview()
    elapsed = time.time() - t0
    print(f"\nmarket_overview 耗时: {elapsed:.1f}s "
          f"(客户端默认超时: {default_client.timeout}s) ok={r.get('success')}")
    if elapsed > default_client.timeout:
        print(f"  -> 冷启动耗时 {elapsed:.0f}s 超过默认超时 {default_client.timeout}s，"
              f"缓存过期后 get_market_context 将间歇性 TimeoutError，"
              f"5 次连续超时还会触发客户端熔断 60s")
    assert r.get("success") or "熔断" not in r.get("error", ""), r.get("error", "")


# ── 诊断模式 ──

def diagnose(tickers, rounds):
    print(f"诊断模式 | 标的: {tickers} | 轮数: {rounds} | 日期: {TODAY}")
    print(f"服务健康: ", end="")
    try:
        h = PlaywrightClient(timeout=5).health()
        print(f"{h.get('success')} (缓存 {h.get('cache_keys')} 键, 运行 {h.get('uptime')}s)")
    except Exception as e:
        print(f"不可达: {e}")
        return 1

    from tradingagents.agents.utils import playwright_tools
    client = playwright_tools._client
    if client is not None:
        with client._lock:
            client._fail_count = 0
            client._circuit_open_until = 0.0

    stats = Stats()
    for code in tickers:
        for rnd in range(1, rounds + 1):
            for name, args_fn in SESSION_SEQUENCE:
                ok, cat, elapsed = call_tool(name, args_fn(code), stats, code)
                mark = "OK " if ok else f"FAIL[{cat}]"
                print(f"  [{rnd}/{rounds}] {code} {name:<22} {mark} {elapsed:6.1f}s")

    stats.print_report(f"诊断模式 {len(tickers)} 只 × {rounds} 轮")
    rows, total_ok, total = stats.summary()
    rate = total_ok / total if total else 1.0
    print(f"诊断结论: {'稳定' if rate >= 1 - FAIL_RATE_MAX else '不稳定'} "
          f"(成功率 {rate * 100:.1f}%, 阈值 {(1 - FAIL_RATE_MAX) * 100:.0f}%)")
    return 0 if rate >= 1 - FAIL_RATE_MAX else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="技术分析师数据链路偶发失败诊断")
    parser.add_argument("tickers", nargs="*", default=DEFAULT_TICKERS)
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()
    sys.exit(diagnose(args.tickers, args.rounds))
