"""北向资金净买入明细不可用 — 回归测试（SGT 疑似缓存占位数据）。

背景：
- `tradingagents/dataflows/a_stock.py:get_northbound_flow()` 曾依赖同花顺
  `data.hexin.cn/market/hsgtApi/method/dayChart/`。
- 交易所自 2024-08-16 起停止发布北向实时净买入，该接口长期返回同一份
  静态序列：本地缓存 `northbound_daily.csv` 中 HGT 收盘值连续 27 天恒为
  -9.28，SGT 在 379.75 / -31.10 两个值间反复；当日 API 收盘值与之完全一致。
- 修复（2026-08）：实时抓取分支暂时禁用（`NORTHBOUND_REALTIME_DISABLED=True`），
  工具如实声明「已停止发布」、不再向 LLM 输出伪数字、不再写缓存，
  历史输出剔除连续同值的占位行。

本文件：
- 实时行为全部 mock 化，不依赖网络；
- 旧缓存/旧报告相关用例仅在对应本地文件存在时生效；
- 数据源恢复并重新启用实时分支后，`NORTHBOUND_REALTIME_DISABLED` 应置 False，
  缓存静态性用例将重新生效。
"""

import csv
import json
import os
from pathlib import Path

import pytest

from tradingagents.dataflows import a_stock

_STOPPED_NOTICE = "已停止发布"


def _longest_identical_run(values: list[float]) -> int:
    """最长连续相同值长度；空序列返回 0。"""
    if not values:
        return 0
    best = cur = 1
    for a, b in zip(values, values[1:]):
        if a == b:
            cur += 1
            best = max(best, cur)
        else:
            cur = 1
    return best


def _write_cache(path: Path, rows: list[tuple[str, float, float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "hgt", "sgt"])
        for date, h, s in rows:
            writer.writerow([date, f"{h:.2f}", f"{s:.2f}"])


def _load_northbound_cache() -> list[tuple[str, float, float]]:
    path = Path(a_stock._northbound_cache_path())
    if not path.exists():
        return []
    rows: list[tuple[str, float, float]] = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if len(row) >= 3:
                try:
                    rows.append((row[0], float(row[1]), float(row[2])))
                except ValueError:
                    continue
    return rows


def test_dedupe_placeholder_rows():
    """连续同值占位行应被剔除，保留每组首次出现的行。"""
    rows = [
        ("2026-08-03", -9.28, 379.75),
        ("2026-08-04", -9.28, 379.75),
        ("2026-08-05", -9.28, 379.75),
        ("2026-08-06", -9.28, -31.10),
        ("2026-08-07", -9.28, -31.10),
        ("2026-08-08", 5.12, 100.5),
    ]
    cleaned = a_stock._dedupe_placeholder_rows(rows)
    assert cleaned == [
        ("2026-08-03", -9.28, 379.75),
        ("2026-08-06", -9.28, -31.10),
        ("2026-08-08", 5.12, 100.5),
    ]
    assert a_stock._dedupe_placeholder_rows([]) == []
    assert a_stock._dedupe_placeholder_rows([("a", 1.0, 2.0)]) == [("a", 1.0, 2.0)]


def test_disabled_returns_stopped_notice_without_fake_numbers(tmp_path, monkeypatch):
    """禁用实时分支后：输出「已停止发布」声明，不含任何伪数字/实时段。"""
    monkeypatch.setattr(a_stock, "_northbound_cache_path", lambda: str(tmp_path / "northbound_daily.csv"))
    out = a_stock.get_northbound_flow("2026-08-09")

    assert _STOPPED_NOTICE in out
    assert "Realtime" not in out
    assert "Close:" not in out
    assert "SGT=" not in out
    assert "379.75" not in out
    assert "-9.28" not in out


def test_disabled_does_not_write_cache(tmp_path, monkeypatch):
    """禁用实时分支后：调用不应创建/追加 northbound_daily.csv。"""
    cache = tmp_path / "northbound_daily.csv"
    monkeypatch.setattr(a_stock, "_northbound_cache_path", lambda: str(cache))
    a_stock.get_northbound_flow("2026-08-09")
    a_stock.get_northbound_flow("2026-08-10")
    assert not cache.exists()


def test_disabled_include_history_dedupes_placeholder_rows(tmp_path, monkeypatch):
    """禁用实时分支 + include_history：既有缓存按占位去重输出，缓存文件不被改写。"""
    cache = tmp_path / "northbound_daily.csv"
    monkeypatch.setattr(a_stock, "_northbound_cache_path", lambda: str(cache))
    _write_cache(
        cache,
        [
            ("2026-08-03", -9.28, 379.75),
            ("2026-08-04", -9.28, 379.75),
            ("2026-08-05", -9.28, 379.75),
            ("2026-08-06", 5.12, 100.5),
        ],
    )
    before = cache.read_bytes()

    out = a_stock.get_northbound_flow("2026-08-09", include_history=True)

    assert "Historical Daily Close" in out
    assert out.count("HGT=-9.28") == 1, "连续占位行应只出现一次"
    assert "HGT=5.12" in out
    assert cache.read_bytes() == before, "禁用期间不得改写缓存"


@pytest.mark.xfail(strict=True, reason="已知问题: 修复前缓存被整月占位数据污染")
def test_real_cache_not_fully_static():
    """本地缓存不应出现整月不变的收盘值（现状: HGT 连续 27 天 -9.28）。

    仅在本地存在 northbound_daily.csv 时生效；修复后重新启用实时分支前
    该文件为空/不存在，自动 skip。
    """
    rows = _load_northbound_cache()
    if not rows:
        pytest.skip("本地 northbound_daily.csv 缓存不存在")
    hgt_run = _longest_identical_run([h for _, h, _ in rows])
    sgt_run = _longest_identical_run([s for _, _, s in rows])
    assert hgt_run <= 3, f"HGT 连续 {hgt_run} 天完全一致（疑似占位）"
    assert sgt_run <= 3, f"SGT 连续 {sgt_run} 天完全一致（疑似占位）"


@pytest.mark.xfail(strict=True, reason="已知问题: 修复前 SGT 仅在 379.75/-31.10 间反复")
def test_real_cache_has_plausible_daily_variation():
    """缓存中同一列不应只出现两个离散值（现状: SGT 在 379.75/-31.10 间反复）。"""
    rows = _load_northbound_cache()
    if not rows:
        pytest.skip("本地 northbound_daily.csv 缓存不存在")
    sgt_values = {s for _, _, s in rows}
    hgt_values = {h for _, h, _ in rows}
    assert len(sgt_values) > 3, f"SGT 仅 {len(sgt_values)} 个离散值: {sorted(sgt_values)}"
    assert len(hgt_values) > 3, f"HGT 仅 {len(hgt_values)} 个离散值: {sorted(hgt_values)}"


def _latest_report_texts() -> list[tuple[str, str]]:
    """返回 (ticker, 最新报告拼接文本) 列表，来自本地 full_states_log。"""
    from tradingagents.dataflows.config import get_config

    base = Path(get_config().get("project_dir")) / "logs"
    if not base.exists():
        base = Path(os.path.expanduser("~/.tradingagents/logs"))
    results: list[tuple[str, str]] = []
    if not base.exists():
        return results
    for ticker_dir in base.iterdir():
        logs_dir = ticker_dir / "TradingAgentsStrategy_logs"
        if not logs_dir.is_dir():
            continue
        jsons = sorted(logs_dir.glob("full_states_log_*.json"))
        if not jsons:
            continue
        latest = jsons[-1]
        try:
            data = json.loads(latest.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if str(data.get("trade_date", "")) < "2026-08-10":
            continue
        parts = [
            data.get(k, "")
            for k in (
                "market_report",
                "hot_money_report",
                "sentiment_report",
            )
        ]
        results.append((ticker_dir.name, "\n".join(p for p in parts if p)))
    return results


def test_reports_after_fix_do_not_self_mark_northbound_suspect():
    """修复（2026-08-10 起）后的最新报告不应再出现北向 [数据存疑] 自标注。"""
    reports = _latest_report_texts()
    if not reports:
        pytest.skip("本地暂无 2026-08-10 之后的 full_states_log，等新报告生成后再验证")
    affected = [
        (ticker, text.count("[数据存疑]"))
        for ticker, text in reports
        if "北向" in text and "[数据存疑]" in text
    ]
    assert not affected, f"修复后报告仍自标注 [数据存疑]: {affected}"
