"""A-stock (China mainland) data vendor for TradingAgents.

Zero third-party data dependency (no akshare). All sources are direct HTTP APIs
or mootdx TCP.

Data sources:
- mootdx (TCP 7709): OHLCV K-lines, financial snapshots, F10 text
- Tencent Finance (HTTP GBK): PE/PB/market cap/turnover
- 东方财富 push2 / datacenter-web (direct HTTP): stock info, dragon-tiger, lockup
- 新浪财经 (direct HTTP): K-line fallback, financial statements
- 同花顺 (direct HTTP): consensus EPS, hot stocks, northbound capital flow
- 财联社 (direct HTTP): global news wire
"""

from __future__ import annotations

from typing import Annotated
from datetime import date, datetime, timedelta, timezone
from dateutil.relativedelta import relativedelta
import contextlib
import json as _json
import os
import logging
import math
import random
import re as _re
import socket
import threading
import time
import uuid
import urllib.request

import pandas as pd
import numpy as np
import requests as _requests

from .utils import safe_ticker_component

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers: ticker format & market detection
# ---------------------------------------------------------------------------

def _get_prefix(code: str) -> str:
    """6-digit A-stock code -> market prefix for Tencent API.

    The 92 prefix must be checked before the leading-9 rule: the Beijing Stock
    Exchange started issuing 920xxx codes for new listings in October 2024, and
    a bare ``startswith("9")`` routes them to Shanghai, where the Tencent quote
    endpoint returns an empty payload (issue #85).  Only 900xxx (Shanghai B
    shares) legitimately belongs to ``sh``.
    """
    if code.startswith("92"):
        return "bj"
    if code.startswith(("6", "9")):
        return "sh"
    elif code.startswith("8"):
        return "bj"
    return "sz"


def _reject_non_a_share(original: str, code: str) -> None:
    """港股/美股代码走到 A 股数据层时当场报错，而不是拿去查 A 股（#43）。

    A 股代码恒为 6 位数字。港股是 4~5 位（`00700`）或带 `.HK` 后缀，美股是字母。
    这些代码此前会被**原样放行**，然后拿去问 mootdx / 腾讯 / 东财——而这些源对
    不存在的代码往往不报错，只返回空值或僵尸报价（北交所 920 号段就踩过，见
    `_normalize_ticker` 上游的 `_get_prefix`）。于是模型会拿着一份看起来正常、
    实际属于别的市场或根本不存在的数据写完整篇报告，报告里完全看不出来。
    """
    if code.isdigit() and len(code) == 6:
        return
    upper = original.strip().upper()
    if upper.endswith(".HK") or (code.isdigit() and len(code) in (4, 5)):
        raise ValueError(
            f"'{original}' 是港股代码。本数据层只支持 A 股（6 位数字代码，"
            f"如 600519 / 000001）。港股数据请用姊妹项目 global-stock-data，"
            f"多 Agent 港股分析仍在 roadmap（issue #43）。"
        )
    if code and not code.isdigit():
        raise ValueError(
            f"'{original}' 不是 A 股代码。本数据层只支持 A 股 6 位数字代码"
            f"（如 600519）；美股/港股请用姊妹项目 global-stock-data。"
        )
    raise ValueError(
        f"'{original}' 不是有效的 A 股代码：A 股代码恒为 6 位数字（如 600519），"
        f"这里解析出的是 '{code}'。"
    )


def _normalize_ticker(symbol: str) -> str:
    """Strip exchange prefix/suffix, return pure 6-digit code.

    Handles: '688017', 'SH688017', '688017.SH', 'sh688017'

    非 A 股代码（港股 `00700` / `0700.HK`、美股 `AAPL`）会直接报错，不再原样
    放行去查 A 股数据源（#43）。
    """
    s = symbol.strip().upper()
    # Remove .SH / .SZ / .BJ suffix
    for suffix in (".SH", ".SZ", ".BJ"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    # Remove SH / SZ / BJ prefix
    for prefix in ("SH", "SZ", "BJ"):
        if s.startswith(prefix):
            s = s[len(prefix) :]
            break
    code = safe_ticker_component(s)
    _reject_non_a_share(symbol, code)
    return code


# ---------------------------------------------------------------------------
# Stock name <-> code mapping (cached)
# ---------------------------------------------------------------------------

_name_to_code: dict[str, str] | None = None
_code_to_name: dict[str, str] | None = None


def _build_name_code_map() -> tuple[dict[str, str], dict[str, str]]:
    """Build name→code and code→name maps via mootdx (both SH & SZ markets)."""
    global _name_to_code, _code_to_name
    if _name_to_code is not None:
        return _name_to_code, _code_to_name

    n2c: dict[str, str] = {}
    c2n: dict[str, str] = {}

    try:
        for market in (0, 1):  # 0=SZ, 1=SH
            stocks = _mootdx_call("stocks", market=market)
            if stocks is None or stocks.empty:
                continue
            for _, row in stocks.iterrows():
                code = str(row["code"]).strip()
                name = str(row["name"]).strip()
                if not _re.match(r"^[036]\d{5}$", code):
                    continue
                clean_name = name.replace(" ", "").replace("　", "")
                n2c[clean_name] = code
                c2n[code] = clean_name
    except Exception as e:
        # 网络抖动/通达信不可达时给出明确提示，而非冒泡成风马牛不相及的报错（#46/#66）
        raise ValueError(
            "无法通过 mootdx 解析股票名称（通达信服务暂时不可达）：%s。"
            "请稍后重试，或直接输入 6 位股票代码。" % e
        ) from e

    _name_to_code = n2c
    _code_to_name = c2n
    logger.info("Built stock name-code map: %d entries", len(n2c))
    return _name_to_code, _code_to_name


def resolve_ticker(user_input: str) -> str:
    """Resolve user input (code or Chinese name) to a 6-digit A-stock code.

    Accepts: '600379', 'SH600379', '600379.SH', '宝光股份'
    Returns: '600379'
    Raises: ValueError if not resolvable.
    """
    s = user_input.strip()
    if not s:
        raise ValueError("输入不能为空")

    has_chinese = any("一" <= ch <= "鿿" for ch in s)

    if not has_chinese:
        return _normalize_ticker(s)

    clean = s.replace(" ", "").replace("　", "")
    n2c, _ = _build_name_code_map()

    if clean in n2c:
        return n2c[clean]

    matches = {name: code for name, code in n2c.items() if clean in name}
    if len(matches) == 1:
        return next(iter(matches.values()))
    if len(matches) > 1:
        examples = ", ".join(f"{n}({c})" for n, c in list(matches.items())[:5])
        raise ValueError(f"'{s}' 匹配到多只股票: {examples}，请输入完整名称或代码")

    # LLM 有时会把行业/概念名（如 '游戏'、'白酒'）当 ticker 传进来（#76）。
    # 报错必须写明原因和正确用法，让模型能在下一次工具调用中自我纠正。
    raise ValueError(
        f"找不到股票 '{s}'。ticker 参数只接受 6 位股票代码（如 '600519'）"
        f"或完整股票名称（如 '贵州茅台'）；行业/概念/板块名（如 '游戏'）不是"
        f"有效的股票标识。请改用目标个股的 6 位股票代码重试。"
    )


# ---------------------------------------------------------------------------
# 未来函数防护（point-in-time）
# ---------------------------------------------------------------------------


# A 股市场时区。判"今天"必须按市场所在地算，不能用主机本地时区——
# 主机在 UTC+9 以东（如新西兰 UTC+13）时，当地已过零点而上海还在前一天，
# 当天的分析会被判成"复盘历史"：实时资金流被略去、快照工具打出莫须有的未来函数
# 警告。反过来主机在西半球也会把已经过去的交易日当成"今天"。
_MARKET_TZ = timezone(timedelta(hours=8))


def _market_today() -> "date":
    """A 股市场当前日期（Asia/Shanghai），与主机时区无关。"""
    return datetime.now(_MARKET_TZ).date()


def _is_historical(curr_date) -> bool:
    """分析日期是否早于市场当天。早于 = 这次是在复盘历史，不能拿实时数据当事实。"""
    if not curr_date:
        return False
    try:
        return (
            datetime.strptime(str(curr_date)[:10], "%Y-%m-%d").date()
            < _market_today()
        )
    except ValueError:
        return False


def _snapshot_notice(curr_date: str, what: str) -> str:
    """实时快照被用在历史日期上时，在正文顶部明说。

    有些数据源只提供"此刻"的值（腾讯实时行情、同花顺当前一致预期），拿不到
    某个历史日的原值。既然补不上，就必须**说出来**——否则模型会把今天的估值
    当成分析日当天的事实写进报告，而这种污染在报告里完全看不出来。
    """
    return (
        f"⚠️ 未来函数警告：以下{what}是**此刻的实时快照**，不是 {curr_date} 当天的值。"
        f"本数据源不提供历史时点数据。在复盘历史日期时，**不得**把这些数字当作"
        f"{curr_date} 当天已知的事实，也不要据此推断当时的判断。\n"
    )


# ---------------------------------------------------------------------------
# mootdx client (singleton)
# ---------------------------------------------------------------------------

_mootdx_client = None

# 实测可用的通达信备选服务器（按延迟排序，2026-06 验证）。用于规避 mootdx
# 0.11.x 全新安装时 BESTIP.HQ 为空串导致的 `ValueError: not enough values to unpack`。
_TDX_SERVERS = [
    ("119.97.185.59", 7709), ("124.70.133.119", 7709), ("116.205.183.150", 7709),
    ("123.60.73.44", 7709), ("116.205.163.254", 7709), ("121.36.225.169", 7709),
    ("123.60.70.228", 7709), ("124.71.9.153", 7709), ("110.41.147.114", 7709),
    ("124.71.187.122", 7709),
]


# 探测用的探针股票：主板老票，任何通达信服务器都应能返回它的日线。
_TDX_CANARY_SYMBOL = "600519"

# 全部服务器都验不过之后，隔多久才允许再探一轮（秒）。没有这个负缓存，
# 每一次取数都会把整张服务器表重探一遍（10 台 × TCP 超时），把"取不到数"
# 放大成"每个请求卡几十秒"。
_MOOTDX_RETRY_AFTER_S = 300.0
_mootdx_unavailable_until = 0.0

# ⚠️ 曾经加过「连续 N 台协议失败就停手」的提前退出，已移除：三台远端拒绝**证明不了**
# 本地网络封了协议，而列表里靠后的服务器完全可能是好的。提前收手会让那台可用服务器
# 永远试不到，还顺手记下 5 分钟负缓存。省下的十几秒不值得换这个风险——真正的耗时
# 大头是 bestip 全表测速，那个已经单独规避了。


def _candidate_tdx_servers() -> list[tuple[str, int]]:
    """待试的通达信服务器：先用实测精选的 `_TDX_SERVERS`，再补 mootdx 自带的完整主机表。

    只试精选的那 10 台是不够的——它们要是恰好都不可用，而 mootdx 自带表里还有活着的
    主机，就会被判成"全网不可达"并记 5 分钟负缓存。这里把两张表合起来去重后逐台验证，
    覆盖面等同 `bestip`，但不做它那套要跑几分钟的全表测速。
    """
    servers = list(_TDX_SERVERS)
    seen = set(servers)
    try:
        from mootdx.consts import HQ_HOSTS
        for entry in HQ_HOSTS:
            # 形如 ("深圳双线主站1", "110.41.147.114", 7709)
            host = (entry[1], entry[2]) if len(entry) >= 3 else None
            if host and host not in seen:
                seen.add(host)
                servers.append(host)
    except Exception as e:  # mootdx 版本变动导致取不到就只用精选表，不影响主流程
        logger.debug("读取 mootdx HQ_HOSTS 失败，仅使用内置精选表：%s", e)
    return servers


def _reachable_tdx_servers(servers, timeout: float = 2.0):
    """并发做 TCP 预筛，返回可连的那些（保持原顺序）。

    只是把"等超时"这件事并行化，不改变优先级：返回顺序仍是候选表顺序，所以实测
    精选的服务器依旧排在前面、依旧第一个被真实验证。
    """
    from concurrent.futures import ThreadPoolExecutor

    if not servers:
        return []
    with ThreadPoolExecutor(max_workers=min(16, len(servers))) as pool:
        flags = list(pool.map(lambda s: _probe_tdx(s[0], s[1], timeout), servers))
    return [srv for srv, ok in zip(servers, flags) if ok]


def _probe_tdx(ip: str, port: int, timeout: float = 2.0) -> bool:
    """TCP 握手探测通达信服务器端口是否开着。

    ⚠️ 只是**廉价预筛**，通过不代表能取到数：实测存在大量"TCP 三次握手成功、
    通达信协议握手立刻被 RST"的服务器。选服务器必须再走 `_tdx_client_works()`
    做一次真实取数验证（#90）。
    """
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def _tdx_client_works(client) -> bool:
    """真实拉一根 K 线来验证这个 client 确实能取数。"""
    try:
        df = client.bars(symbol=_TDX_CANARY_SYMBOL, category=4, offset=1)
        return df is not None and not df.empty
    except Exception:
        return False


def reset_mootdx_client() -> None:
    """丢弃缓存的 client，让下一次调用重新选服务器。

    单例一旦钉在一台"当时能用、后来挂了"的服务器上，之后每次取数都失败降级且
    永远不会重选。数据调用发现 mootdx 出错时调它，下一次就能换一台（#90）。
    """
    global _mootdx_client, _mootdx_unavailable_until
    _mootdx_client = None
    _mootdx_unavailable_until = 0.0


@contextlib.contextmanager
def _preserve_mootdx_bestip():
    """探测期间保护 mootdx 的持久化服务器配置，退出时按需还原。

    `StdQuotes.__init__` 里有 `config.set('BESTIP', {'HQ': self.server})`——**每建一次
    带 server 的 client 都会写进 mootdx 的配置文件**。逐台探测 38 个候选就等于把用户
    原本配好的服务器一路覆写，最后留下的是最后一台**失败的**服务器，还会连累同一台
    机器上其它用 mootdx 的程序。

    🔴 必须先 `setup()` 再快照：新进程里 `config.get("BESTIP")` 返回的是模块默认空值，
    用户持久化的值要等 `BaseQuotes.__init__` 调 `setup()` 才读进来。快照到空值的话，
    "还原"反而会把真实配置抹成空——比不还原更糟。
    实测（mootdx 0.11.7）：setup 前 `{'HQ': ''}`，setup 后 `{'HQ': ['218.6.x.x', 7709]}`。

    用法：`with _preserve_mootdx_bestip() as keep:` —— 选出可用服务器时调 `keep()`
    表示"这次的覆写是我们想要的，别还原"；不调就在退出时还原。

    ⚠️ **做成上下文管理器而不是手动调还原函数**：此前是在两处分别调 `_restore_bestip()`，
    再加一条提前返回就会漏掉一处，而漏掉的后果是静默留下一台死服务器。
    """
    saved = None
    try:
        from mootdx import config as _cfg
        _cfg.setup()
        saved = _cfg.get("BESTIP")
        if isinstance(saved, dict):
            saved = dict(saved)
    except Exception as e:  # 版本差异导致取不到就跳过保护，别影响主流程
        logger.debug("读取 mootdx BESTIP 失败，本次探测不做保护：%s", e)

    keep = {"flag": False}
    try:
        yield lambda: keep.__setitem__("flag", True)
    finally:
        if saved is not None and not keep["flag"]:
            try:
                from mootdx import config as _cfg2
                _cfg2.set("BESTIP", saved)
            except Exception as e:
                logger.debug("恢复 mootdx BESTIP 失败：%s", e)


def _get_mootdx_client():
    """Lazy-init 健壮版 mootdx Quotes client（TCP 连接，可复用）。

    选服务器的顺序：内置服务器表（TCP 预筛 + 真实取数验证）→ bestip 测速 →
    裸 factory（老用户 config 里已有 IP）。每一级都必须真正取到数据才会被采用，
    避免把 client 钉死在一台"端口开着但协议不通"的服务器上（#90）。
    全部失败时抛 RuntimeError，并在 `_MOOTDX_RETRY_AFTER_S` 内直接快速失败，
    不再逐台重探。
    """
    global _mootdx_client, _mootdx_unavailable_until
    if _mootdx_client is not None:
        return _mootdx_client

    now = time.time()
    if now < _mootdx_unavailable_until:
        raise RuntimeError(
            "mootdx 通达信服务器暂不可用（%.0f 秒内不再重试）。"
            "已尝试全部内置服务器：端口能连上的也没能完成通达信协议取数。"
            "请检查网络环境（代理/防火墙/公司网络常拦 TCP 7709），"
            "或改用 6 位股票代码直接查询。" % (_mootdx_unavailable_until - now)
        )

    from mootdx.quotes import Quotes

    tcp_ok_but_dead = 0
    # 探测会覆写 mootdx 的持久化配置——包在这里，只有真选出可用服务器时才 keep()，
    # 其余每条退出路径（含异常）都自动还原。
    with _preserve_mootdx_bestip() as keep_bestip:
        # TCP 预筛并发跑：38 台里多数是"连都连不上"，串行每台要等满超时（实测整轮
        # 73.7s，首次调用像卡死）。预筛纯粹是等 IO，并发不改变选取语义——下面仍按
        # 原顺序、逐台做真实取数验证，精选表依旧优先。
        reachable = _reachable_tdx_servers(_candidate_tdx_servers())

        for ip, port in reachable:
            # 「TCP 通但通达信协议不通」有两种表现：factory 建连时握手就被拒，
            # 或者建出来了但取不到数。**两种都要算**——只统计后者的话，计数永远是 0
            # （实测这批服务器全是在 factory 里抛 ConnectionReset），下面的快速失败
            # 判断就失效了。
            try:
                candidate = Quotes.factory(market="std", server=(ip, port))
            except Exception as e:
                tcp_ok_but_dead += 1
                logger.debug("mootdx %s:%s 握手失败（%s），换下一台", ip, port, type(e).__name__)
            else:
                if _tdx_client_works(candidate):
                    logger.info("mootdx server selected: %s:%s", ip, port)
                    keep_bestip()   # 这次的覆写正是我们想要的，别还原
                    _mootdx_client = candidate
                    return _mootdx_client
                tcp_ok_but_dead += 1
                logger.debug("mootdx %s:%s 建连成功但取不到数，换下一台", ip, port)

    # 走到这里说明逐台探测都没成——上面的 with 已经把 BESTIP 还原成用户原本的配置，
    # 下面的裸 factory 读的正是它，这个兜底才有意义。
    # ⚠️ 刻意**不用** `bestip=True`：它会把整张主机表做一遍测速，实测要几分钟。
    # `_candidate_tdx_servers()` 已经把 mootdx 自带的完整主机表逐台验证过了，
    # 覆盖面不比 bestip 差，而且每台都是"真取到数才算通过"。
    try:
        candidate = Quotes.factory(market="std")
    except Exception as e:
        logger.debug("mootdx 裸 factory 失败 — %s", e)
    else:
        if _tdx_client_works(candidate):
            logger.info("mootdx client from 裸 factory（用户已有配置）")
            _mootdx_client = candidate
            return _mootdx_client

    _mootdx_unavailable_until = time.time() + _MOOTDX_RETRY_AFTER_S
    if tcp_ok_but_dead:
        # 说清楚是"协议被拒"而不是"连不上"——这两者的排查方向完全不同。
        cause = (
            "%d 台服务器端口能连上，但通达信协议握手/取数被拒。"
            "这通常是协议层被拦（代理、防火墙、公司网络对 TCP 7709 的策略），"
            "换服务器解决不了。" % tcp_ok_but_dead
        )
    else:
        cause = "内置服务器表里没有一台的 TCP 7709 能连上，请检查网络连通性。"
    raise RuntimeError(
        "mootdx 通达信服务器不可用：%s"
        "可改用 6 位股票代码直接查询。%.0f 秒内将直接快速失败、不再逐台重探。"
        % (cause, _MOOTDX_RETRY_AFTER_S)
    )


def _mootdx_call(method: str, **kwargs):
    """调用 mootdx 的某个方法，失败就弃用当前服务器。

    选中的服务器随时可能挂掉；不弃用的话单例会一直指着它，之后每次取数都失败降级
    且永不重选（#90 的「反复降级」）。取 client 本身失败时不清缓存——那条路径已经
    在 `_get_mootdx_client` 里做了负缓存，清掉等于取消快速失败。
    """
    client = _get_mootdx_client()
    try:
        return getattr(client, method)(**kwargs)
    except Exception:
        reset_mootdx_client()
        raise


# ---------------------------------------------------------------------------
# Tencent Finance API
# ---------------------------------------------------------------------------

def _tencent_quote(codes: list[str]) -> dict[str, dict]:
    """Batch real-time quotes from Tencent Finance (qt.gtimg.cn).

    Returns dict[code] -> {name, price, pe_ttm, pb, mcap_yi, ...}
    """
    prefixed = [f"{_get_prefix(c)}{c}" for c in codes]
    url = "https://qt.gtimg.cn/q=" + ",".join(prefixed)
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")
    resp = urllib.request.urlopen(req, timeout=10)
    raw = resp.read().decode("gbk")

    result = {}
    for line in raw.strip().split(";"):
        if not line.strip() or "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].split("_")[-1]
        vals = line.split('"')[1].split("~")
        if len(vals) < 53:
            continue
        code = key[2:]  # strip sh/sz/bj prefix
        result[code] = {
            "name": vals[1],
            "price": float(vals[3]) if vals[3] else 0,
            "last_close": float(vals[4]) if vals[4] else 0,
            "open": float(vals[5]) if vals[5] else 0,
            "change_pct": float(vals[32]) if vals[32] else 0,
            "high": float(vals[33]) if vals[33] else 0,
            "low": float(vals[34]) if vals[34] else 0,
            "turnover_pct": float(vals[38]) if vals[38] else 0,
            "pe_ttm": float(vals[39]) if vals[39] else 0,
            "mcap_yi": float(vals[45]) if vals[45] else 0,
            "float_mcap_yi": float(vals[44]) if vals[44] else 0,
            "pb": float(vals[46]) if vals[46] else 0,
            "limit_up": float(vals[47]) if vals[47] else 0,
            "limit_down": float(vals[48]) if vals[48] else 0,
            "pe_dynamic": float(vals[52]) if vals[52] else 0,
            "pe_static": float(vals[53]) if vals[53] else 0,
        }
    return result


# ---------------------------------------------------------------------------
# Eastmoney Datacenter unified helper (龙虎榜/解禁 etc.)
# ---------------------------------------------------------------------------

_DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


# ---------------------------------------------------------------------------
# 东财防封：全局节流 + 会话复用 (Eastmoney anti-ban: throttle + Keep-Alive)
# ---------------------------------------------------------------------------
# 东财系 HTTP 接口（push2 / push2his / datacenter-web / search-api / np-weblist）
# 有风控：每秒 >5 次 / 单 IP 并发 ≥10 / 1 分钟 ≥200 次 / 5 分钟 ≥300 次 → 临时封 IP。
# 多 Agent 投研跑批量分析时会高频请求东财，是被封的头号元凶。所有 eastmoney.com
# 请求一律走 _em_get()：串行限流（最小间隔 + 随机抖动）+ 复用 Keep-Alive 会话 + 默认 UA。
# 注意：仅东财接口走此入口；mootdx(TCP) / 腾讯 / 新浪 / 同花顺 / 财联社 / 百度 等
# 不限流（实测不封 IP 或风控极弱）。批量任务可调大 EM_MIN_INTERVAL 进一步降速。
_EM_SESSION = _requests.Session()
_EM_SESSION.headers.update({"User-Agent": _UA})
# 两次东财请求最小间隔(秒)；批量多 Agent 场景可设环境变量 EM_MIN_INTERVAL=1.5~2 降速。
_EM_MIN_INTERVAL = float(os.environ.get("EM_MIN_INTERVAL", "1.0"))
_em_last_call = [0.0]  # 模块级上次东财请求时间戳
_em_lock = threading.Lock()  # 保护 _em_last_call 的串行限流


def _em_get(url, params=None, headers=None, timeout=15, **kwargs):
    """东财统一请求入口：自动节流 + 复用 session + 默认 UA。

    所有 eastmoney.com 接口都应通过它请求，避免多 Agent 高频拉数据被封 IP。
    串行限流：与上次东财请求间隔 < EM_MIN_INTERVAL 时 sleep 补足 + 0.1~0.5s 随机抖动。
    传入的 headers 会覆盖 session 默认 UA（用于保留各端点自己的 Referer/Origin）。
    """
    with _em_lock:
        wait = _EM_MIN_INTERVAL - (time.time() - _em_last_call[0])
        if wait > 0:
            time.sleep(wait + random.uniform(0.1, 0.5))
        _em_last_call[0] = time.time()
    try:
        return _EM_SESSION.get(
            url, params=params, headers=headers, timeout=timeout, **kwargs
        )
    finally:
        with _em_lock:
            _em_last_call[0] = time.time()


def _eastmoney_datacenter(
    report_name: str,
    columns: str = "ALL",
    filter_str: str = "",
    page_size: int = 50,
    sort_columns: str = "",
    sort_types: str = "-1",
) -> list[dict]:
    """东财数据中心统一查询 — 龙虎榜/解禁 共用."""
    params = {
        "reportName": report_name,
        "columns": columns,
        "filter": filter_str,
        "pageNumber": "1",
        "pageSize": str(page_size),
        "sortColumns": sort_columns,
        "sortTypes": sort_types,
        "source": "WEB",
        "client": "WEB",
    }
    r = _em_get(_DATACENTER_URL, params=params, timeout=15)
    d = r.json()
    if d.get("result") and d["result"].get("data"):
        return d["result"]["data"]
    return []


# ---------------------------------------------------------------------------
# 同花顺 EPS forecast helper (direct HTTP, no akshare)
# ---------------------------------------------------------------------------


def _ths_eps_forecast(code: str) -> pd.DataFrame:
    """Fetch consensus EPS forecast from 同花顺 (direct HTTP).

    Returns DataFrame with columns roughly: 年度, 预测机构数, 最小值, 均值, 最大值.
    """
    url = f"https://basic.10jqka.com.cn/new/{code}/worth.html"
    headers = {
        "User-Agent": _UA,
        "Referer": "https://basic.10jqka.com.cn/",
    }
    r = _requests.get(url, headers=headers, timeout=15)
    r.encoding = "gbk"
    dfs = pd.read_html(r.text)
    # Find the table containing EPS data
    for df in dfs:
        cols = [str(c) for c in df.columns]
        if any("每股收益" in c or "均值" in c for c in cols):
            return df
    # Fallback: return first table if exists
    return dfs[0] if dfs else pd.DataFrame()


def _eps_forecast_playwright(code: str):
    """Fetch consensus EPS forecast via playwright_service (同花顺F10 CDP).

    Returns (eps_by_year, display_lines) or (None, None) on failure.
    eps_by_year: {year_str: mean_eps_float}
    display_lines: list[str] for get_fundamentals output.
    """
    try:
        from playwright_service.client import PlaywrightClient
        client = PlaywrightClient()
        result = client.eps_forecast(code)
    except Exception as e:
        logger.warning("playwright eps_forecast transport failed for %s: %s", code, str(e)[:200])
        return None, None

    if not result or not result.get("success"):
        return None, None

    data = result.get("data", {}) or {}
    eps_summary = data.get("eps_summary", []) or []
    if not eps_summary:
        return None, None

    eps_by_year = {}
    display_lines = ["\n--- Consensus EPS Forecast (同花顺F10 playwright) ---"]
    ic = data.get("institution_count")
    if ic:
        display_lines.append(f"覆盖机构数: {ic} 家")

    for r in eps_summary:
        year = str(r.get("year", ""))
        avg_val = r.get("avg")
        min_val = r.get("min", "N/A")
        max_val = r.get("max", "N/A")
        count_val = r.get("institution_count", 0)
        try:
            mean_eps = float(avg_val)
        except (ValueError, TypeError):
            mean_eps = 0
        try:
            count = int(count_val)
        except (ValueError, TypeError):
            count = 0
        display_lines.append(
            f"FY{year}: EPS={mean_eps} (range {min_val}~{max_val}, {count} analysts)"
        )
        if count < 3:
            display_lines.append("  Warning: low coverage (<3 analysts)")
        if year:
            eps_by_year[year] = mean_eps

    if not eps_by_year:
        return None, None
    return eps_by_year, display_lines


# ---------------------------------------------------------------------------
# Sina K-line fallback helper (direct HTTP, no akshare)
# ---------------------------------------------------------------------------


def _sina_kline_fallback(code: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
    """Fetch daily K-line from Sina HTTP API as mootdx fallback.

    Returns DataFrame with columns: Date, Open, High, Low, Close, Volume.
    """
    prefix = "sh" if code.startswith("6") else "sz"
    url = (
        "http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData"
    )
    params = {
        "symbol": f"{prefix}{code}",
        "scale": "240",  # daily
        "ma": "no",
        "datalen": "800",
    }
    r = _requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    data = _json.loads(r.text)

    if not data:
        return pd.DataFrame()

    rows = []
    for item in data:
        rows.append({
            "Date": item["day"],
            "Open": float(item["open"]),
            "High": float(item["high"]),
            "Low": float(item["low"]),
            "Close": float(item["close"]),
            "Volume": int(item["volume"]),
        })

    df = pd.DataFrame(rows)
    df["Date"] = pd.to_datetime(df["Date"])

    if start_date:
        df = df[df["Date"] >= pd.to_datetime(start_date)]
    if end_date:
        df = df[df["Date"] <= pd.to_datetime(end_date)]

    return df


def _last_ohlcv_date(df: pd.DataFrame) -> pd.Timestamp | None:
    """Return the latest OHLCV Date in a normalized dataframe."""
    if df is None or df.empty or "Date" not in df.columns:
        return None
    dates = pd.to_datetime(df["Date"], errors="coerce")
    if dates.dropna().empty:
        return None
    return dates.max().normalize()


def _normalize_ohlcv_dates(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize OHLCV Date values to daily granularity."""
    if df is None or df.empty or "Date" not in df.columns:
        return df
    df = df.copy()
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.normalize()
    return df.dropna(subset=["Date"])


def _needs_sina_supplement(df: pd.DataFrame, target_date: str | None) -> bool:
    """True when mootdx/cache data is older than the requested cutoff date."""
    if not target_date:
        return False
    last_date = _last_ohlcv_date(df)
    if last_date is None:
        return True
    target = pd.to_datetime(target_date).normalize()
    return last_date < target


def _merge_ohlcv(primary: pd.DataFrame, supplement: pd.DataFrame) -> pd.DataFrame:
    """Merge OHLCV frames, preferring supplement rows on duplicate dates."""
    frames = [frame for frame in (primary, supplement) if frame is not None and not frame.empty]
    if not frames:
        return pd.DataFrame(columns=["Date", "Open", "High", "Low", "Close", "Volume"])
    combined = pd.concat(frames, ignore_index=True)
    combined = _normalize_ohlcv_dates(combined)
    combined = combined.drop_duplicates(subset=["Date"], keep="last")
    combined = combined.sort_values("Date").reset_index(drop=True)
    return combined


def _supplement_stale_ohlcv_with_sina(
    code: str,
    df: pd.DataFrame,
    target_date: str | None,
    start_date: str | None = None,
) -> tuple[pd.DataFrame, bool]:
    """Use Sina daily K-line to fill dates missing from mootdx/cache data."""
    if not _needs_sina_supplement(df, target_date):
        return df, False
    try:
        sina_df = _sina_kline_fallback(code, start_date, target_date)
    except Exception as e:
        logger.warning("sina K-line supplement failed for %s: %s", code, e)
        return df, False
    if sina_df.empty:
        return df, False
    merged = _merge_ohlcv(df, sina_df)
    return merged, _last_ohlcv_date(merged) != _last_ohlcv_date(df)


# ---------------------------------------------------------------------------
# OHLCV loading with cache (mootdx -> CSV)
# ---------------------------------------------------------------------------

def _load_ohlcv_astock(symbol: str, curr_date: str) -> pd.DataFrame:
    """Fetch OHLCV via mootdx, cache to CSV, filter by curr_date.

    Mirrors stockstats_utils.load_ohlcv but uses mootdx instead of yfinance.
    Returns DataFrame with columns: Date, Open, High, Low, Close, Volume
    """
    from .config import get_config

    code = _normalize_ticker(symbol)
    config = get_config()
    cache_dir = config.get(
        "data_cache_dir", os.path.expanduser("~/.tradingagents/cache")
    )
    os.makedirs(cache_dir, exist_ok=True)

    cache_file = os.path.join(cache_dir, f"{code}-astock-daily.csv")

    if os.path.exists(cache_file):
        mtime = datetime.fromtimestamp(os.path.getmtime(cache_file))
        if mtime.date() == datetime.now().date():
            data = pd.read_csv(cache_file, on_bad_lines="skip", encoding="utf-8")
            data = _normalize_ohlcv_dates(data)
            data, supplemented = _supplement_stale_ohlcv_with_sina(
                code, data, curr_date, start_date=None
            )
            if supplemented:
                data.to_csv(cache_file, index=False, encoding="utf-8")
            cutoff = pd.to_datetime(curr_date)
            return data[data["Date"] <= cutoff]

    # Fetch from mootdx — 800 daily bars (~3 years of trading days)
    try:
        df = _mootdx_call("bars", symbol=code, category=4, offset=800)

        if df is None or df.empty:
            raise ValueError(f"No OHLCV data from mootdx for {code}")

        # mootdx returns index named 'datetime' AND a column named 'datetime'
        # (plus year/month/day/hour/minute/volume). Drop duplicates before reset.
        df = df.drop(columns=["datetime", "year", "month", "day", "hour", "minute"], errors="ignore")
        df = df.reset_index()  # moves index 'datetime' → column 'datetime'
        rename_map = {
            "datetime": "Date",
            "open": "Open",
            "close": "Close",
            "high": "High",
            "low": "Low",
            "volume": "Volume",
        }
        df = df.rename(columns=rename_map)
        df = df[["Date", "Open", "High", "Low", "Close", "Volume"]]
        df = _normalize_ohlcv_dates(df)
    except Exception as e:
        logger.debug("mootdx OHLCV failed for %s: %s, trying sina HTTP fallback", code, str(e)[:100])
        # Fallback: Sina direct HTTP API
        try:
            df = _sina_kline_fallback(code)
            if df.empty:
                raise ValueError(f"No OHLCV data from sina for {code}")
        except Exception:
            raise ValueError(f"No OHLCV data from mootdx/sina for {code}")

    df, _ = _supplement_stale_ohlcv_with_sina(code, df, curr_date, start_date=None)

    # Cache to disk
    df.to_csv(cache_file, index=False, encoding="utf-8")

    # Filter by curr_date to prevent look-ahead bias
    cutoff = pd.to_datetime(curr_date)
    return df[df["Date"] <= cutoff]


# ===========================================================================
# 9 Vendor Methods (matching interface.py VENDOR_METHODS signatures)
# ===========================================================================


# ---- 1. get_stock_data ----


def get_stock_data(
    symbol: Annotated[str, "A-stock code (e.g. 688017, SH688017)"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """Get OHLCV stock price data via mootdx."""
    code = _normalize_ticker(symbol)

    data_source = "mootdx (TCP)"
    try:
        df = _mootdx_call("bars", symbol=code, category=4, offset=800)

        if df is None or df.empty:
            raise ValueError(f"No data from mootdx for {code}")

        # Drop duplicate datetime column + extra columns before reset_index
        df = df.drop(
            columns=["datetime", "year", "month", "day", "hour", "minute"],
            errors="ignore",
        )
        df = df.reset_index()  # index 'datetime' → column 'datetime'
        df = df.rename(
            columns={
                "datetime": "Date",
                "open": "Open",
                "close": "Close",
                "high": "High",
                "low": "Low",
                "volume": "Volume",
                "amount": "Amount",
            }
        )
        df = _normalize_ohlcv_dates(df)

    except Exception as e:
        logger.warning("mootdx K-line failed for %s: %s, trying sina HTTP fallback", code, e)
        # Fallback: Sina direct HTTP API
        try:
            df = _sina_kline_fallback(code, start_date, end_date)
            if df.empty:
                return "K线数据获取失败：mootdx和新浪备用源均不可用，请检查网络连接"
            data_source = "sina HTTP (fallback)"
        except Exception:
            return "K线数据获取失败：mootdx和新浪备用源均不可用，请检查网络连接"

    df, supplemented = _supplement_stale_ohlcv_with_sina(code, df, end_date, start_date)
    if supplemented:
        data_source = f"{data_source} + sina HTTP supplement"

    # Filter by date range
    start_dt = pd.to_datetime(start_date)
    end_dt = pd.to_datetime(end_date)
    df = df[(df["Date"] >= start_dt) & (df["Date"] <= end_dt)]

    if df.empty:
        return (
            f"No data found for A-stock '{code}' "
            f"between {start_date} and {end_date}"
        )

    for col in ["Open", "High", "Low", "Close"]:
        if col in df.columns:
            df[col] = df[col].round(2)

    df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
    csv_out = df[["Date", "Open", "High", "Low", "Close", "Volume"]].to_csv(
        index=False
    )

    header = f"# Stock data for {code} (A-stock) from {start_date} to {end_date}\n"
    header += f"# Total records: {len(df)}\n"
    header += f"# Data source: {data_source}\n"
    header += (
        f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    )

    return header + csv_out


# ---- 2. get_indicators ----

# Supported technical indicators with descriptions
_INDICATOR_DESCRIPTIONS = {
    "close_50_sma": "50 SMA: Medium-term trend indicator.",
    "close_200_sma": "200 SMA: Long-term trend benchmark.",
    "close_10_ema": "10 EMA: Responsive short-term average.",
    "macd": "MACD: Momentum via EMA differences.",
    "macds": "MACD Signal: EMA smoothing of MACD line.",
    "macdh": "MACD Histogram: Gap between MACD and signal.",
    "rsi": "RSI: Momentum overbought/oversold indicator (70/30 thresholds).",
    "boll": "Bollinger Middle: 20 SMA basis for Bollinger Bands.",
    "boll_ub": "Bollinger Upper Band: 2 std devs above middle.",
    "boll_lb": "Bollinger Lower Band: 2 std devs below middle.",
    "atr": "ATR: Average True Range volatility measure.",
    "vwma": "VWMA: Volume-weighted moving average.",
    "mfi": "MFI: Money Flow Index (volume + price momentum).",
}


def get_indicators(
    symbol: Annotated[str, "A-stock code"],
    indicator: Annotated[
        str, "technical indicator (e.g. rsi, macd, close_50_sma)"
    ],
    curr_date: Annotated[str, "Current trading date, YYYY-mm-dd"],
    look_back_days: Annotated[int, "how many days to look back"],
) -> str:
    """Get technical indicators using stockstats on mootdx OHLCV data."""
    from stockstats import wrap

    code = _normalize_ticker(symbol)

    if indicator not in _INDICATOR_DESCRIPTIONS:
        raise ValueError(
            f"Indicator {indicator} not supported. "
            f"Choose from: {list(_INDICATOR_DESCRIPTIONS.keys())}"
        )

    try:
        data = _load_ohlcv_astock(code, curr_date)
        df = wrap(data)
        df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")

        # Trigger stockstats calculation
        df[indicator]

        # Build date -> value lookup
        ind_dict = {}
        for _, row in df.iterrows():
            d = row["Date"]
            v = row[indicator]
            ind_dict[d] = "N/A" if pd.isna(v) else str(round(float(v), 4))

        # Generate output for look_back window
        curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
        before = curr_dt - relativedelta(days=look_back_days)

        lines = []
        dt = curr_dt
        while dt >= before:
            ds = dt.strftime("%Y-%m-%d")
            val = ind_dict.get(ds, "N/A: Not a trading day (weekend or holiday)")
            lines.append(f"{ds}: {val}")
            dt -= relativedelta(days=1)

        result = (
            f"## {indicator} values for {code} "
            f"from {before.strftime('%Y-%m-%d')} to {curr_date}:\n\n"
            + "\n".join(lines)
            + "\n\n"
            + _INDICATOR_DESCRIPTIONS.get(indicator, "")
        )
        return result

    except Exception as e:
        return f"Error calculating {indicator} for {code}: {str(e)}"


# ---- 3. get_fundamentals ----


def get_fundamentals(
    ticker: Annotated[str, "A-stock code"],
    curr_date: Annotated[str, "current date"] = None,
) -> str:
    """Get company fundamentals from Tencent + mootdx + Eastmoney + 同花顺."""
    code = _normalize_ticker(ticker)

    try:
        lines = []
        # 腾讯行情只有"此刻"的 PE/PB/市值，拿不到历史时点值。复盘历史日期时
        # 必须明说，否则模型会把今天的估值写成分析日当天的事实（未来函数）。
        if _is_historical(curr_date):
            lines.append(_snapshot_notice(curr_date, "估值与行情数据"))

        # --- Tencent: real-time valuation ---
        try:
            tq = _tencent_quote([code])
            if code in tq:
                q = tq[code]
                lines.append("\n=== 当前估值（实时，基于已披露财报） ===")
                lines.extend(
                    [
                        f"Name: {q['name']}",
                        f"Price: {q['price']}",
                        f"PE (TTM): {q['pe_ttm']}",
                        f"PE (Dynamic): {q['pe_dynamic']}",
                        f"PE (Static): {q['pe_static']}",
                        f"PB: {q['pb']}",
                        f"Market Cap (100M CNY): {q['mcap_yi']}",
                        f"Float Market Cap (100M CNY): {q['float_mcap_yi']}",
                        f"Turnover Rate: {q['turnover_pct']}%",
                        f"Change: {q['change_pct']}%",
                        f"Limit Up: {q['limit_up']}",
                        f"Limit Down: {q['limit_down']}",
                    ]
                )
                # 涨跌停状态判断（涨停价已四舍五入到分，不能用涨幅==10%判断）
                try:
                    _price = float(q['price'])
                    _lu = float(q['limit_up'])
                    _ld = float(q['limit_down'])
                    if _price > 0 and _lu > 0 and abs(_price - _lu) < 0.001:
                        lines.append(f"⚠️ 已涨停 (price={_price} == limit_up={_lu})")
                    elif _price > 0 and _ld > 0 and abs(_price - _ld) < 0.001:
                        lines.append(f"⚠️ 已跌停 (price={_price} == limit_down={_ld})")
                except (ValueError, TypeError):
                    pass
        except Exception as e:
            logger.warning("Tencent quote failed for %s: %s", code, e)

        # --- mootdx: financial snapshot (quarterly) ---
        try:
            fin = _mootdx_call("finance", symbol=code)
            if fin is not None and not (
                isinstance(fin, pd.DataFrame) and fin.empty
            ):
                row = fin.iloc[0] if isinstance(fin, pd.DataFrame) else fin
                field_map = {
                    "eps": "EPS (Quarterly)",
                    "bvps": "Book Value Per Share",
                    "roe": "ROE (%)",
                    "profit": "Net Profit",
                    "income": "Revenue",
                    "liutongguben": "Float Shares",
                    "zongguben": "Total Shares",
                }
                idx = row.index if hasattr(row, "index") else []
                for field, label in field_map.items():
                    if field in idx:
                        val = row[field]
                        if val is not None and str(val) != "nan":
                            lines.append(f"{label}: {val}")
        except Exception as e:
            logger.warning("mootdx finance failed for %s: %s", code, e)

        # --- Eastmoney push2: basic stock info (direct HTTP) ---
        try:
            market_code = 1 if code.startswith("6") else 0
            _info_url = "https://push2.eastmoney.com/api/qt/stock/get"
            _info_params = {
                "fltt": "2",
                "invt": "2",
                "fields": "f57,f58,f84,f85,f127,f116,f117,f189,f43",
                "secid": f"{market_code}.{code}",
            }
            r = _em_get(_info_url, params=_info_params, timeout=10)
            d = r.json().get("data", {})
            if d:
                if d.get("f127"):
                    lines.append(f"行业: {d['f127']}")
                if d.get("f84"):
                    lines.append(f"总股本: {d['f84']}")
                if d.get("f85"):
                    lines.append(f"流通股本: {d['f85']}")
                if d.get("f116"):
                    lines.append(f"总市值: {d['f116']}")
                if d.get("f117"):
                    lines.append(f"流通市值: {d['f117']}")
                if d.get("f189"):
                    lines.append(f"上市日期: {d['f189']}")
        except Exception as e:
            logger.warning("eastmoney push2 stock info failed for %s: %s", code, str(e)[:200])

        # --- 同花顺 consensus EPS forecast (playwright preferred, direct HTTP fallback) ---
        try:
            eps_by_year, eps_lines = _eps_forecast_playwright(code)
            if eps_by_year is None:
                # Fallback: direct HTTP (lightweight, often blocked by anti-crawl)
                forecast_df = _ths_eps_forecast(code)
                eps_by_year = {}
                eps_lines = ["\n--- Consensus EPS Forecast (同花顺) ---"]
                if forecast_df is not None and not forecast_df.empty:
                    for _, row in forecast_df.iterrows():
                        year = str(row.iloc[0]) if len(row) > 0 else ""
                        mean_eps_val = row.iloc[3] if len(row) > 3 else 0
                        count_val = row.iloc[1] if len(row) > 1 else 0
                        min_eps_val = row.iloc[2] if len(row) > 2 else "N/A"
                        max_eps_val = row.iloc[4] if len(row) > 4 else "N/A"
                        try:
                            mean_eps = float(mean_eps_val)
                        except (ValueError, TypeError):
                            mean_eps = 0
                        try:
                            count = int(count_val)
                        except (ValueError, TypeError):
                            count = 0
                        eps_lines.append(
                            f"FY{year}: EPS={mean_eps} "
                            f"(range {min_eps_val}~{max_eps_val}, {count} analysts)"
                        )
                        if count < 3:
                            eps_lines.append("  Warning: low coverage (<3 analysts)")
                        eps_by_year[year] = mean_eps

            if eps_by_year:
                lines.append("\n=== 预期估值（前瞻，基于机构一致预测EPS） ===")
                lines.extend(eps_lines)

                # Forward PE / PEG / PE digestion
                try:
                    tq = _tencent_quote([code])
                    if code in tq:
                        price = tq[code]["price"]
                        years_sorted = sorted(eps_by_year.keys())
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
                    logger.warning("Forward PE calc failed for %s: %s", code, e)
        except Exception as e:
            logger.warning("Consensus EPS forecast failed for %s: %s", code, str(e)[:200])

        if not lines:
            return f"No fundamentals data found for A-stock '{code}'"

        header = f"# Company Fundamentals for {code} (A-stock)\n"
        header += (
            f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        )

        return header + "\n".join(lines)

    except Exception as e:
        return f"Error retrieving fundamentals for {code}: {str(e)}"


# ---- 4. get_balance_sheet / get_cashflow / get_income_statement ----
#
# ⚠️ 新浪直连已禁用（2026-08）：getFinanceReport2022 接口结构改版为
# result.data.report_list（按报告期嵌套），旧解析逻辑返回空数据。
# 三大报表全量明细改走同花顺F10 playwright 端点（/api/financial-quarterly，
# finance.html 自定义指标面板 + getFinanceEdit.php，102 期全量科目）。
# 下方 _get_financial_report_sina 保留仅供追溯，不再被调用。


def _sina_stock_code(code: str) -> str:
    """Pure 6-digit code → sina format (sh688017 / sz000001 / bj832000)."""
    return f"{_get_prefix(code)}{code}"


def _get_financial_report_sina(
    code: str, report_type: str, freq: str, curr_date: str = None,
) -> pd.DataFrame:
    """⚠️ 已禁用（新浪接口结构改版，返回空）。保留仅供追溯。"""
    _report_type_map = {
        "资产负债表": "fzb",
        "利润表": "lrb",
        "现金流量表": "llb",
    }
    source_type = _report_type_map.get(report_type, "lrb")

    prefix = "sh" if code.startswith("6") else "sz"
    paper_code = f"{prefix}{code}"
    url = "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"
    params = {
        "paperCode": paper_code,
        "source": source_type,
        "type": "0",
        "page": "1",
        "num": "20",
    }
    r = _requests.get(url, params=params, headers={"User-Agent": _UA}, timeout=15)
    d = r.json()

    result = d.get("result", {}).get("data", {})
    items = result.get(source_type, [])
    if not isinstance(items, list) or not items:
        return pd.DataFrame()

    df = pd.DataFrame(items)

    # Filter by curr_date
    if curr_date and "报告日" in df.columns:
        df["报告日"] = pd.to_datetime(df["报告日"], errors="coerce")
        cutoff = pd.to_datetime(curr_date)
        df = df[df["报告日"] <= cutoff]

    # Filter by frequency (annual = month 12 reports only)
    if freq.lower() == "annual" and "报告日" in df.columns:
        months = pd.to_datetime(df["报告日"], errors="coerce").dt.month
        df = df[months == 12]

    return df.head(8)


def _statement_time_line(result: dict) -> str:
    """三表渲染的数据时间行。服务端 SWR stale 命中时标注真实抓取时刻,
    防止 LLM 把缓存旧数据当最新数据。"""
    if result.get("stale") and result.get("fetched_at"):
        return (
            f"# ⚠️ 数据时间: {result['fetched_at']} "
            f"(缓存旧数据, 后台刷新中)"
        )
    return f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"


def _fetch_statement_playwright(code: str, key: str, label: str) -> str:
    """三大报表全量明细（同花顺F10 playwright，单位亿元，报告期由新到旧）。"""
    try:
        from playwright_service.client import PlaywrightClient

        result = PlaywrightClient().financial_quarterly(code)
        if not result.get("success"):
            return (
                f"No {label} data found for A-stock '{code}': "
                f"{result.get('error', '')}"
            )
        st = result.get(key)
        if not st:
            return f"No {label} data found for A-stock '{code}'"
        periods = st.get("periods") or []
        items = st.get("items") or {}
        if not items:
            return f"No {label} data found for A-stock '{code}'"
        lines = [
            f"# {label} for {code} (A-stock)",
            "# Data source: 同花顺F10 (playwright)",
            _statement_time_line(result),
            f"# 单位: 亿元 | {len(items)} 科目 × {len(periods)} 期 (报告期由新到旧)",
            "",
        ]
        lines.append("报告期: " + ", ".join(str(p) for p in periods))
        for name, vals in items.items():
            if isinstance(vals, list) and vals:
                lines.append(
                    f"{name}: " + ", ".join(
                        "--" if v is False or v is None else str(v) for v in vals
                    )
                )
            else:
                lines.append(f"{name}: {vals}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error retrieving {label} for {code}: {str(e)}"


def get_balance_sheet(
    ticker: Annotated[str, "A-stock code"],
    freq: Annotated[str, "frequency: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str, "current date in YYYY-MM-DD format"] = None,
) -> str:
    """Get balance sheet (同花顺F10 playwright, 全量科目×期数)."""
    code = _normalize_ticker(ticker)
    return _fetch_statement_playwright(code, "balance_sheet", "balance sheet")


# ---- 5. get_cashflow ----


def get_cashflow(
    ticker: Annotated[str, "A-stock code"],
    freq: Annotated[str, "frequency: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str, "current date in YYYY-MM-DD format"] = None,
) -> str:
    """Get cash flow statement (同花顺F10 playwright, 全量科目×期数)."""
    code = _normalize_ticker(ticker)
    return _fetch_statement_playwright(code, "cash_flow", "cash flow")


# ---- 6. get_income_statement ----


def get_income_statement(
    ticker: Annotated[str, "A-stock code"],
    freq: Annotated[str, "frequency: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str, "current date in YYYY-MM-DD format"] = None,
) -> str:
    """Get income statement (同花顺F10 playwright, 全量科目×期数)."""
    code = _normalize_ticker(ticker)
    return _fetch_statement_playwright(code, "income_statement", "income statement")


# ---- 7. get_news ----


def _fetch_news_eastmoney(code: str, page_size: int = 20) -> list[dict]:
    """Direct East Money search API for individual stock news."""
    url = "https://search-api-web.eastmoney.com/search/jsonp"
    inner_param = {
        "uid": "",
        "keyword": code,
        "type": ["cmsArticleWebOld"],
        "client": "web",
        "clientType": "web",
        "clientVersion": "curr",
        "param": {
            "cmsArticleWebOld": {
                "searchScope": "default",
                "sort": "default",
                "pageIndex": 1,
                "pageSize": page_size,
                "preTag": "",
                "postTag": "",
            }
        },
    }
    params = {
        "cb": "callback",
        "param": _json.dumps(inner_param, ensure_ascii=False),
        "_": "1",
    }
    headers = {
        "Referer": "https://so.eastmoney.com/",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
        ),
    }

    resp = _em_get(url, params=params, headers=headers, timeout=15)
    resp.raise_for_status()
    text = resp.text
    text = text[text.index("(") + 1 : text.rindex(")")]
    data = _json.loads(text)

    articles: list[dict] = []
    for item in data.get("result", {}).get("cmsArticleWebOld", []):
        articles.append({
            "title": item.get("title", ""),
            "content": item.get("content", ""),
            "time": item.get("date", ""),
            "source": item.get("mediaName", "东方财富"),
            "url": item.get("url", ""),
        })
    return articles


def _fetch_news_sina(code: str, page_size: int = 20) -> list[dict]:
    """Sina Finance stock news API (backup source)."""
    prefix = _get_prefix(code)
    url = (
        f"https://vip.stock.finance.sina.com.cn/corp/view/"
        f"vCB_AllNewsStock.php?symbol={prefix}{code}&Page=1"
    )
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
        ),
        "Referer": "https://finance.sina.com.cn/",
    }

    resp = _requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    resp.encoding = "gb2312"
    html = resp.text

    articles: list[dict] = []
    rows = _re.findall(
        r"(\d{4}-\d{2}-\d{2})\s*(?:&nbsp;)*(\d{2}:\d{2})\s*(?:&nbsp;)*"
        r"<a[^>]+href='([^']+)'[^>]*>([^<]+)</a>",
        html,
    )
    for date_str, time_str, link, title in rows[:page_size]:
        articles.append({
            "title": title.strip(),
            "content": "",
            "time": f"{date_str} {time_str}",
            "source": "新浪财经",
            "url": link,
        })
    return articles


def get_news(
    ticker: Annotated[str, "A-stock code"],
    start_date: Annotated[str, "Start date yyyy-mm-dd"],
    end_date: Annotated[str, "End date yyyy-mm-dd"],
) -> str:
    """Get stock-specific news via East Money direct API (Sina as fallback)."""
    code = _normalize_ticker(ticker)

    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    articles: list[dict] = []
    source_label = ""

    try:
        articles = _fetch_news_eastmoney(code)
        source_label = "东方财富"
    except Exception as e:
        logger.warning("East Money news fetch failed for %s: %s", code, e)

    if not articles:
        try:
            articles = _fetch_news_sina(code)
            source_label = "新浪财经"
        except Exception as e:
            logger.warning("Sina news fetch failed for %s: %s", code, e)

    if not articles:
        return f"No news found for A-stock '{code}'"

    news_str = ""
    count = 0
    for art in articles:
        pub_time = art.get("time", "")
        try:
            pub_dt = datetime.strptime(pub_time[:10], "%Y-%m-%d")
            if pub_dt < start_dt or pub_dt > end_dt:
                continue
        except (ValueError, IndexError):
            pass

        title = art["title"]
        content = art.get("content", "")
        source = art.get("source", source_label)
        link = art.get("url", "")

        news_str += f"### {title} (source: {source})\n"
        if content:
            snippet = content[:300] + "..." if len(content) > 300 else content
            news_str += f"{snippet}\n"
        if link and link != "nan":
            news_str += f"Link: {link}\n"
        news_str += "\n"
        count += 1

    if count == 0:
        return (
            f"No news found for A-stock '{code}' "
            f"between {start_date} and {end_date}"
        )

    return (
        f"## {code} (A-stock) News, from {start_date} to {end_date}:\n\n"
        + news_str
    )


# ---- 8. get_global_news ----


def get_global_news(
    curr_date: Annotated[str, "Current date yyyy-mm-dd"],
    look_back_days: Annotated[int, "Days to look back"] = 7,
    limit: Annotated[int, "Max articles"] = 20,
) -> str:
    """Get China/global financial news via CLS telegraph (playwright_service) + Eastmoney 7x24 fallback."""
    start_dt = datetime.strptime(curr_date, "%Y-%m-%d") - relativedelta(
        days=look_back_days
    )
    start_date = start_dt.strftime("%Y-%m-%d")

    all_news: list[dict] = []

    # Source 1: CLS wire (财联社电报) — playwright_service 爬取
    # (原 cls.cn/nodeapi/telegraphList 直连已 404 失效，改由 playwright 拦截页面 api/cache 响应)
    try:
        from playwright_service.client import PlaywrightClient
        result = PlaywrightClient().global_news_cls(limit)
        if result and result.get("success"):
            for item in result.get("data", []) or []:
                all_news.append({
                    "title": item.get("title", ""),
                    "content": item.get("content", ""),
                    "time": item.get("time", ""),
                    "source": "CLS Wire",
                })
        else:
            logger.warning("CLS news via playwright failed: %s",
                           (result or {}).get("error", "unknown"))
    except Exception as e:
        logger.warning("CLS news playwright transport failed: %s", str(e)[:200])

    # Source 2: Eastmoney 7x24 (东财快讯) — playwright_service 爬取
    try:
        from playwright_service.client import PlaywrightClient
        result = PlaywrightClient().global_news_em(limit)
        if result and result.get("success"):
            for item in result.get("data", []) or []:
                all_news.append({
                    "title": item.get("title", ""),
                    "content": item.get("content", ""),
                    "time": item.get("time", ""),
                    "source": "Eastmoney Global",
                })
        else:
            logger.warning("Eastmoney global news via playwright failed: %s",
                           (result or {}).get("error", "unknown"))
    except Exception as e:
        logger.warning("Eastmoney global news playwright transport failed: %s", str(e)[:200])

    # Source 3: Eastmoney 7x24 (direct HTTP) — 最后兜底 (playwright 不可用时)
    if not any(n["source"] == "Eastmoney Global" for n in all_news):
        try:
            em_url = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
            em_params = {
                "client": "web",
                "biz": "web_724",
                "fastColumn": "102",
                "sortEnd": "",
                "pageSize": str(limit),
                "req_trace": str(uuid.uuid4()),
            }
            em_headers = {"User-Agent": _UA, "Referer": "https://kuaixun.eastmoney.com/"}
            r_em = _em_get(em_url, params=em_params, headers=em_headers, timeout=10)
            d_em = r_em.json()
            for item in d_em.get("data", {}).get("fastNewsList", []):
                title = item.get("title", "")
                summary = item.get("summary", "")[:200]
                pub_time = item.get("showTime", "")
                all_news.append({
                    "title": title,
                    "content": summary,
                    "time": pub_time,
                    "source": "Eastmoney Global",
                })
        except Exception as e:
            logger.warning("Eastmoney global news HTTP fallback failed: %s", e)

    if not all_news:
        return f"No global news found for {curr_date}"

    # Deduplicate by title
    seen: set[str] = set()
    unique: list[dict] = []
    for n in all_news:
        if n["title"] not in seen:
            seen.add(n["title"])
            unique.append(n)

    # Interleave sources so one source cannot fill up the entire limit
    cls_items = [n for n in unique if n["source"] == "CLS Wire"]
    em_items = [n for n in unique if n["source"] != "CLS Wire"]
    interleaved: list[dict] = []
    i = j = 0
    while len(interleaved) < limit and (i < len(cls_items) or j < len(em_items)):
        if i < len(cls_items):
            interleaved.append(cls_items[i])
            i += 1
        if len(interleaved) >= limit:
            break
        if j < len(em_items):
            interleaved.append(em_items[j])
            j += 1

    news_str = ""
    for n in interleaved:
        news_str += f"### {n['title']} (source: {n['source']})\n"
        if n.get("content"):
            snippet = (
                n["content"][:300] + "..."
                if len(n["content"]) > 300
                else n["content"]
            )
            news_str += f"{snippet}\n"
        news_str += "\n"

    return (
        f"## China & Global Market News, from {start_date} to {curr_date}:\n\n"
        + news_str
    )


# ---- 9. get_company_events ----


def get_company_events(
    ticker: Annotated[str, "A-stock code"],
) -> str:
    """Get company events (重要事件+高管持股变动+股东持股变动+担保+违规) via 同花顺F10 event.html."""
    code = _normalize_ticker(ticker)

    try:
        result = _company_events_playwright(code)
        if result is not None:
            return result
    except Exception as e:
        logger.warning("playwright company_events failed for %s: %s", code, str(e)[:200])

    return f"[公司大事] {code}: playwright_service 不可用，无法获取公司大事数据。请确保 playwright_service 已启动。"


def _company_events_playwright(code: str):
    """Fetch company events via playwright_service (同花顺F10 event.html).

    Returns rendered string, or None on failure.
    """
    try:
        from playwright_service.client import PlaywrightClient
        client = PlaywrightClient()
        result = client.company_events(code)
    except Exception as e:
        logger.warning("playwright company_events transport failed for %s: %s", code, str(e)[:200])
        return None

    if not result or not result.get("success"):
        return None

    data = result.get("data", {}) or {}
    events = data.get("events", []) or []
    exec_changes = data.get("executive_changes", []) or []
    shareholder_changes = data.get("shareholder_changes", []) or []
    guarantees = data.get("guarantees", []) or []
    violations = data.get("violations", []) or []
    research_visits = data.get("research_visits", []) or []

    if not events and not exec_changes and not shareholder_changes and not guarantees and not violations and not research_visits:
        return None

    lines = [
        f"# 公司大事: {code}",
        f"# 数据源: 同花顺F10 (event.html)",
        f"# 获取时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]

    # 1. 近期重要事件
    if events:
        lines.append(f"## 近期重要事件 ({len(events)} 条)")
        lines.append(f"  {'日期':<12} {'事件描述'}")
        lines.append("  " + "-" * 100)
        for e in events[:20]:
            lines.append(f"  {e.get('date','')[:10]:<12} {e.get('description','')[:100]}")
        if len(events) > 20:
            lines.append(f"  ... (共 {len(events)} 条，仅显示前 20 条)")

    # 2. 高管持股变动
    if exec_changes:
        lines.append(f"\n## 高管持股变动 ({len(exec_changes)} 条)")
        lines.append(f"  {'变动日期':<12} {'变动人':<10} {'与高管关系':<16} {'变动数量':<16} {'均价':<8} {'剩余股数':<12} {'变动途径'}")
        lines.append("  " + "-" * 100)
        for c in exec_changes[:30]:
            lines.append(
                f"  {c.get('date','')[:10]:<12} {c.get('person','')[:8]:<10} "
                f"{c.get('relationship','')[:14]:<16} {c.get('change','')[:14]:<16} "
                f"{c.get('price',''):<8} {c.get('remaining','')[:10]:<12} {c.get('method','')}"
            )
        if len(exec_changes) > 30:
            lines.append(f"  ... (共 {len(exec_changes)} 条，仅显示前 30 条)")

    # 3. 股东持股变动
    if shareholder_changes:
        lines.append(f"\n## 股东持股变动 ({len(shareholder_changes)} 条)")
        lines.append(f"  {'公告日期':<12} {'变动股东':<20} {'变动数量':<16} {'均价':<8} {'剩余股份':<12} {'变动期间':<22} {'途径'}")
        lines.append("  " + "-" * 110)
        for c in shareholder_changes[:15]:
            lines.append(
                f"  {c.get('announcement_date','')[:10]:<12} {c.get('shareholder','')[:18]:<20} "
                f"{c.get('change','')[:14]:<16} {c.get('price',''):<8} {c.get('remaining','')[:10]:<12} "
                f"{c.get('period','')[:20]:<22} {c.get('method','')}"
            )
        if len(shareholder_changes) > 15:
            lines.append(f"  ... (共 {len(shareholder_changes)} 条，仅显示前 15 条)")

    # 4. 担保明细
    if guarantees:
        lines.append(f"\n## 担保明细 ({len(guarantees)} 条)")
        for g in guarantees[:10]:
            parts = []
            if g.get("amount"): parts.append(g["amount"])
            if g.get("period"): parts.append(g["period"])
            if g.get("guarantor"): parts.append(g["guarantor"])
            if g.get("type"): parts.append(g["type"])
            if g.get("guaranteed"): parts.append(f"被担保: {g['guaranteed']}")
            lines.append(f"  {' | '.join(parts)}")

    # 5. 违规处理
    if violations:
        lines.append(f"\n## 违规处理 ({len(violations)} 条)")
        for v in violations:
            parts = []
            if v.get("date"): parts.append(v["date"])
            if v.get("fine"): parts.append(v["fine"])
            if v.get("type"): parts.append(v["type"])
            if v.get("handler"): parts.append(v["handler"])
            if v.get("target"): parts.append(v["target"])
            if v.get("reason"): parts.append(v["reason"])
            if v.get("description"): parts.append(v["description"])
            lines.append(f"  {' | '.join(parts)}")

    # 6. 机构调研
    research_visits = data.get("research_visits", []) or []
    if research_visits:
        lines.append(f"\n## 机构调研 ({len(research_visits)} 组)")
        for r in research_visits:
            lines.append(f"  {r.get('category','')}: {r.get('institutions','')[:100]}")

    return "\n".join(lines)


# ---- 10. get_profit_forecast ----


def get_profit_forecast(
    ticker: Annotated[str, "A-stock code"],
    curr_date: Annotated[str, "current date — 用于判断是否在复盘历史"] = None,
) -> str:
    """Get consensus EPS forecasts with forward valuation (同花顺 direct HTTP)."""
    code = _normalize_ticker(ticker)

    try:
        df = _ths_eps_forecast(code)

        if df is None or df.empty:
            return f"No analyst coverage found for A-stock '{code}'"

        lines = [
            f"# Consensus EPS Forecast for {code} (A-stock)",
            f"# Source: 同花顺 analyst consensus (direct HTTP)",
            f"# Retrieved: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
        ]
        # 一致预期是"当前"的分析师预测，没有历史时点版本。同上，必须明说。
        if _is_historical(curr_date):
            lines.insert(0, _snapshot_notice(curr_date, "分析师一致预期"))

        eps_by_year = {}
        for _, row in df.iterrows():
            year = str(row.iloc[0]) if len(row) > 0 else ""
            count_val = row.iloc[1] if len(row) > 1 else 0
            mean_eps_val = row.iloc[3] if len(row) > 3 else 0
            min_eps_val = row.iloc[2] if len(row) > 2 else "N/A"
            max_eps_val = row.iloc[4] if len(row) > 4 else "N/A"
            try:
                count = int(count_val)
            except (ValueError, TypeError):
                count = 0
            try:
                mean_eps = float(mean_eps_val)
            except (ValueError, TypeError):
                mean_eps = 0
            lines.append(
                f"FY{year}: EPS={mean_eps} (range {min_eps_val}~{max_eps_val}), "
                f"analysts={count}"
            )
            if count < 3:
                lines.append("  Warning: low coverage (<3 analysts)")
            eps_by_year[year] = mean_eps

        # Forward valuation
        try:
            tq = _tencent_quote([code])
            if code in tq:
                price = tq[code]["price"]
                pe_ttm = tq[code]["pe_ttm"]
                lines.append(f"\nCurrent: price={price}, PE(TTM)={pe_ttm}")

                years_sorted = sorted(eps_by_year.keys())
                if years_sorted and eps_by_year.get(years_sorted[0], 0) > 0:
                    eps_cur = eps_by_year[years_sorted[0]]
                    fwd_pe = price / eps_cur
                    lines.append(
                        f"Forward PE (FY{years_sorted[0]}): {fwd_pe:.1f}x"
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
                                f"PEG: {peg:.2f} (CAGR={cagr * 100:.0f}%)"
                            )
                            if fwd_pe > 30:
                                digest = math.log(fwd_pe / 30) / math.log(
                                    1 + cagr
                                )
                                lines.append(
                                    f"PE Digestion to 30x: {digest:.1f} years"
                                )
                        else:
                            lines.append(
                                f"EPS declining ({cagr * 100:.0f}%), "
                                f"PEG not applicable"
                            )
        except Exception as e:
            logger.warning("Forward PE calc failed for %s: %s", code, e)

        return "\n".join(lines)

    except Exception as e:
        return f"Error retrieving profit forecast for {code}: {str(e)}"


# ---- 11. get_hot_stocks ----


def get_hot_stocks(
    curr_date: Annotated[str, "Date YYYY-MM-DD, empty string for today"] = "",
) -> str:
    """Get strong stocks with topic attribution from 同花顺 editorial team.

    Returns stocks that hit limit-up with human-curated reason tags
    explaining WHY they surged (e.g. '算力租赁+AI政务').
    """
    import requests

    if not curr_date or curr_date.strip() == "":
        curr_date = datetime.now().strftime("%Y-%m-%d")

    try:
        url = (
            f"http://zx.10jqka.com.cn/event/api/getharden/"
            f"date/{curr_date}/orderby/date/orderway/desc/charset/GBK/"
        )
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "Chrome/117.0.0.0 Safari/537.36"
            )
        }
        r = requests.get(url, headers=headers, timeout=10)
        data = r.json()

        if data.get("errocode", 0) != 0:
            return f"同花顺 API error: {data.get('errormsg', 'unknown')}"

        rows = data.get("data") or []
        if not rows:
            return (
                f"No hot stocks data for {curr_date} "
                f"(may be non-trading day or data not yet available)"
            )

        lines = [
            f"# Hot Stocks with Topic Attribution ({curr_date})",
            f"# Source: 同花顺 editorial (human-curated reason tags)",
            f"# Total: {len(rows)} stocks",
            "",
        ]

        from collections import Counter

        all_tags: list[str] = []

        for row in rows:
            code = row.get("code", "")
            name = row.get("name", "")
            reason = row.get("reason", "")
            zhangfu = row.get("zhangfu", "")
            huanshou = row.get("huanshou", "")
            chengjiaoe = row.get("chengjiaoe", "")
            dde = row.get("ddejingliang", "")

            lines.append(
                f"{code} {name}: +{zhangfu}% "
                f"换手{huanshou}% 成交额{chengjiaoe} "
                f"大单净量{dde} | {reason}"
            )

            if reason:
                tags = [t.strip() for t in str(reason).split("+") if t.strip()]
                all_tags.extend(tags)

        if all_tags:
            cnt = Counter(all_tags)
            lines.append(f"\n## Theme Frequency (top 15)")
            for tag, n in cnt.most_common(15):
                lines.append(f"  {tag}: {n} stocks")

        return "\n".join(lines)

    except Exception as e:
        return f"Error fetching hot stocks for {curr_date}: {str(e)}"


# ---- 12. get_northbound_flow ----


def _northbound_cache_path() -> str:
    """Path to local CSV cache for northbound daily close snapshots."""
    from .config import get_config

    config = get_config()
    cache_dir = config.get(
        "data_cache_dir", os.path.expanduser("~/.tradingagents/cache")
    )
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, "northbound_daily.csv")


def _save_northbound_snapshot(date_str: str, hgt: float, sgt: float) -> None:
    """Append today's northbound close to local CSV cache (dedup by date)."""
    import csv

    path = _northbound_cache_path()
    existing: dict[str, tuple[str, str]] = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            next(reader, None)
            for row in reader:
                if len(row) >= 3:
                    existing[row[0]] = (row[1], row[2])
    existing[date_str] = (f"{hgt:.2f}", f"{sgt:.2f}")
    sorted_dates = sorted(existing.keys())
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "hgt", "sgt"])
        for d in sorted_dates:
            writer.writerow([d, existing[d][0], existing[d][1]])


def _load_northbound_history(n: int = 20) -> list[tuple[str, float, float]]:
    """Load last N days of northbound close data from local cache."""
    import csv

    path = _northbound_cache_path()
    if not os.path.exists(path):
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
    return rows[-n:]


# 交易所自 2024-08-16 起停止发布北向实时净买入，同花顺 hsgtApi 自 2026-07
# 起仅返回同一份静态占位快照（收盘值整月不变，如 SGT=379.75/HGT=-9.28）。
# 置 True 时跳过实时抓取、只声明数据不可用，不再写缓存；数据源恢复后置 False 即可。
NORTHBOUND_REALTIME_DISABLED = True


def _dedupe_placeholder_rows(
    rows: list[tuple[str, float, float]],
) -> list[tuple[str, float, float]]:
    """剔除连续多日 HGT/SGT 收盘值完全一致的占位行（保留每组首次出现的行）。"""
    cleaned: list[tuple[str, float, float]] = []
    for date, h, s in rows:
        if cleaned and cleaned[-1][1] == h and cleaned[-1][2] == s:
            continue
        cleaned.append((date, h, s))
    return cleaned


def get_northbound_flow(
    curr_date: Annotated[str, "Date YYYY-MM-DD"],
    include_history: Annotated[
        bool, "Include historical daily data (last 20 trading days)"
    ] = False,
) -> str:
    """Get northbound capital flow (沪深股通) from 同花顺 hsgtApi.

    Realtime: minute-level cumulative net buying for HGT(沪股通) + SGT(深股通).
    History: self-cached daily close snapshots (upstream APIs stopped updating
    northbound history since 2024-08).
    NOTE: 实时净买入已于 2026-08 起暂停抓取（上游仅返回占位快照）。
    """
    if NORTHBOUND_REALTIME_DISABLED:
        lines = [
            f"# Northbound Capital Flow ({curr_date})",
            "# Source: 同花顺 hsgtApi (沪深股通) + local cache",
            "",
            "北向资金净买入已停止发布：交易所自 2024-08-16 起停止公布北向实时净买入，"
            "上游接口仅返回疑似缓存占位快照，实时抓取已暂停，净买入数据不可用。",
            "可参考：南向资金净买入（本工具历史数据）与北向资金成交额合计（get_market_context）。",
        ]
        if include_history:
            history = _dedupe_placeholder_rows(_load_northbound_history(20))
            if history:
                lines.append("\n## Historical Daily Close (local cache, 亿元)")
                lines.append("Date       | HGT(沪股通) | SGT(深股通) | Total")
                for date, h, s in history:
                    lines.append(f"  {date}: HGT={h:.2f} SGT={s:.2f} Total={h + s:.2f}")
            else:
                lines.append(
                    "\n## Historical Daily: No cached data yet. "
                    "History accumulates only while realtime fetch is enabled."
                )
        return "\n".join(lines)

    import requests

    hsgt_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "Chrome/117.0.0.0 Safari/537.36"
        ),
        "Host": "data.hexin.cn",
        "Referer": "https://data.hexin.cn/",
    }

    lines = [
        f"# Northbound Capital Flow ({curr_date})",
        "# Source: 同花顺 hsgtApi (沪深股通) + local cache",
        "",
    ]

    hgt_close = 0.0
    sgt_close = 0.0
    got_realtime = False

    try:
        url_rt = "https://data.hexin.cn/market/hsgtApi/method/dayChart/"
        r = requests.get(url_rt, headers=hsgt_headers, timeout=10)
        d = r.json()

        times = d.get("time", [])
        hgt = d.get("hgt", [])
        sgt = d.get("sgt", [])

        if times:
            lines.append("## Realtime (cumulative net buying, 亿元)")
            n = len(times)
            start_idx = max(0, n - 10)
            for i in range(start_idx, n):
                t = times[i]
                h = hgt[i] if i < len(hgt) else "N/A"
                s = sgt[i] if i < len(sgt) else "N/A"
                lines.append(f"  {t}: HGT={h} SGT={s}")

            hgt_close = float(hgt[-1]) if hgt else 0
            sgt_close = float(sgt[-1]) if sgt else 0
            total = hgt_close + sgt_close
            lines.append(
                f"\nClose: HGT(沪股通)={hgt_close:.2f}亿 "
                f"SGT(深股通)={sgt_close:.2f}亿 "
                f"Total={total:.2f}亿"
            )
            if total > 0:
                lines.append("Signal: Net northbound INFLOW (bullish)")
            elif total < 0:
                lines.append("Signal: Net northbound OUTFLOW (bearish)")
            got_realtime = True
        else:
            lines.append("No realtime data (non-trading hours or holiday)")

        if got_realtime:
            today_str = datetime.now().strftime("%Y-%m-%d")
            _save_northbound_snapshot(today_str, hgt_close, sgt_close)

        if include_history:
            history = _load_northbound_history(20)
            if history:
                lines.append("\n## Historical Daily Close (local cache, 亿元)")
                lines.append("Date       | HGT(沪股通) | SGT(深股通) | Total")
                for date, h, s in history:
                    lines.append(f"  {date}: HGT={h:.2f} SGT={s:.2f} Total={h + s:.2f}")
                avg_total = sum(h + s for _, h, s in history) / len(history)
                lines.append(
                    f"\n{len(history)}-day avg net flow: {avg_total:.2f}亿"
                )
                if got_realtime:
                    today_total = hgt_close + sgt_close
                    diff = today_total - avg_total
                    lines.append(
                        f"Today vs avg: {'+' if diff >= 0 else ''}{diff:.2f}亿 "
                        f"({'above' if diff >= 0 else 'below'} average)"
                    )
            else:
                lines.append(
                    "\n## Historical Daily: No cached data yet. "
                    "History accumulates automatically with each call."
                )

        return "\n".join(lines)

    except Exception as e:
        return f"Error fetching northbound flow: {str(e)}"


# ---------------------------------------------------------------------------
# Baidu PAE (百度股市通) helpers
# ---------------------------------------------------------------------------

_BAIDU_PAE_HEADERS = {
    "Host": "finance.pae.baidu.com",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) "
        "Gecko/20100101 Firefox/110.0"
    ),
    "Accept": "application/vnd.finance-web.v1+json",
    "Origin": "https://gushitong.baidu.com",
    "Referer": "https://gushitong.baidu.com/",
}


# ---- 13. get_concept_blocks ----


def get_concept_blocks(
    ticker: Annotated[str, "A-stock code (e.g. 688017)"],
) -> str:
    """Get concept/sector/region blocks that a stock belongs to (百度股市通).

    Returns industry classification (申万), concept themes, and region.
    Each block includes current day's change percentage.
    """
    import requests

    code = _normalize_ticker(ticker)

    try:
        url = (
            "https://finance.pae.baidu.com/api/getrelatedblock"
            f'?stock=[{{"code":"{code}","market":"ab","type":"stock"}}]'
            "&finClientType=pc"
        )
        r = requests.get(url, headers=_BAIDU_PAE_HEADERS, timeout=10)
        d = r.json()

        if str(d.get("ResultCode", -1)) != "0":
            return (
                f"Baidu PAE error: ResultCode={d.get('ResultCode')} "
                f"{d.get('ResultMsg', '')}"
            )

        result = d.get("Result", {})
        categories = result.get(code, [])
        if not categories:
            return f"No concept/block data for {code}"

        lines = [
            f"# Concept & Sector Blocks for {code} (A-stock)",
            f"# Source: 百度股市通 (Baidu PAE)",
            f"# Retrieved: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
        ]

        concept_names: list[str] = []

        for cat in categories:
            cat_name = cat.get("name", "")
            items = cat.get("list", [])
            if not items:
                continue
            lines.append(f"## {cat_name}")
            for item in items:
                name = item.get("name", "")
                ratio = item.get("ratio", "")
                desc = item.get("describe", "")
                suffix = f" ({desc})" if desc else ""
                lines.append(f"  {name}{suffix}: {ratio}")
                if cat_name == "概念":
                    concept_names.append(name)

        if concept_names:
            lines.append(f"\nConcept tags: {' / '.join(concept_names)}")

        return "\n".join(lines)

    except Exception as e:
        return f"Error fetching concept blocks for {code}: {str(e)}"


# ---- 14. get_fund_flow ----


def get_fund_flow(
    ticker: Annotated[str, "A-stock code"],
    curr_date: Annotated[str, "Date YYYY-MM-DD"],
    include_history: Annotated[
        bool, "Include historical daily fund flow (last 20 days)"
    ] = True,
) -> str:
    """Get individual stock fund flow from 东财 push2.

    Realtime: minute-level main/large/medium/small/super order net inflow.
    History: daily net inflow for 20 trading days (push2his).

    V0.2.7: replaced 百度 PAE (fundflow/fundsortlist, offline since 2026-05)
    with 东财 push2 fund flow API.
    """
    code = _normalize_ticker(ticker)
    secid = f"1.{code}" if code.startswith("6") else f"0.{code}"
    lines = [
        f"# Fund Flow for {code} (A-stock)",
        f"# Source: 东财 push2 (Eastmoney)",
        f"# Retrieved: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]

    historical = _is_historical(curr_date)
    if historical:
        # 分钟级资金流只有"今天"的，复盘历史日期时整段都是未来数据，直接不取。
        lines.append(
            f"（分析日期 {curr_date} 早于今天，已略去实时分钟资金流——"
            f"那是今天的盘中数据，不是 {curr_date} 当天的。）\n"
        )

    try:
        # Realtime minute-level fund flow
        url_rt = "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
        params_rt = {
            "secid": secid, "klt": 1,
            "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56,f57",
        }
        klines = []
        if not historical:
            r = _em_get(url_rt, params=params_rt, timeout=10)
            d = r.json()
            klines = d.get("data", {}).get("klines", [])

        if klines:
            lines.append(
                "## Realtime Minute Flow "
                "(主力/小单/中单/大单/超大单 净流入, 元)"
            )
            for line in klines[-10:]:
                parts = line.split(",")
                if len(parts) >= 6:
                    lines.append(
                        f"  {parts[0]}: "
                        f"主力={float(parts[1])/1e4:.0f}万 "
                        f"大单={float(parts[4])/1e4:.0f}万 "
                        f"超大单={float(parts[5])/1e4:.0f}万"
                    )

            last_parts = klines[-1].split(",")
            if len(last_parts) >= 2:
                main_net = float(last_parts[1])
                lines.append(
                    f"\nClose: 主力净流入={main_net/1e4:.0f}万元"
                )
                if main_net > 0:
                    lines.append(
                        "Signal: Net main force INFLOW (bullish)"
                    )
                elif main_net < 0:
                    lines.append(
                        "Signal: Net main force OUTFLOW (bearish)"
                    )
        else:
            lines.append(
                "No realtime fund flow (non-trading hours or holiday)"
            )

        # Historical daily fund flow (push2his)
        if include_history:
            url_hist = (
                "https://push2his.eastmoney.com"
                "/api/qt/stock/fflow/daykline/get"
            )
            # 接口返回的是"从今天回溯 lmt 个交易日"，没有 end_date 参数。复盘一个
            # 较早的日期时，若仍只要 20 天，过滤后会**一行不剩**——把"数据不对"
            # 变成"没有数据"，比不过滤更糟。按分析日与今天的间隔把窗口放大到能
            # 覆盖到那一段（上限 500，够回溯约两年）。
            hist_limit = 20
            if historical:
                gap_days = (_market_today() - datetime.strptime(
                    str(curr_date)[:10], "%Y-%m-%d").date()).days
                # 日历日 → 交易日约 ×0.7，再多留 20 天余量
                hist_limit = min(500, 20 + int(gap_days * 0.7) + 20)
            params_hist = {
                "secid": secid, "lmt": hist_limit, "klt": 101,
                "fields1": "f1,f2,f3,f7",
                "fields2": "f51,f52,f53,f54,f55,f56,f57",
            }
            rh = _em_get(url_hist, params=params_hist, timeout=10)
            dh = rh.json()
            hist_klines = dh.get("data", {}).get("klines", [])

            # 逐行按分析日截断：接口返回的是"从今天回溯 20 个交易日"，
            # 在历史日期上直接打印等于把未来的资金流喂给模型（未来函数）。
            if historical:
                cutoff = str(curr_date)[:10]
                hist_klines = [
                    k for k in hist_klines if k.split(",")[0][:10] <= cutoff
                ]
                # 窗口是为了"够回溯到分析日"才放大的，过滤完要裁回承诺的 20 个交易日。
                # 不裁的话，复盘 90 天前会返回约 40 行——既改变了请求的趋势窗口，
                # 又把每次情绪工具的返回体撑大一倍。
                hist_klines = hist_klines[-20:]

            if historical and not hist_klines:
                # 说清楚是"这个日期取不到"，而不是让正文里凭空少一段
                lines.append(
                    f"\n## Historical Daily Fund Flow\n"
                    f"（{str(curr_date)[:10]} 及之前的资金流未能取到：该接口只提供"
                    f"从今天回溯的窗口，分析日过早时可能已超出可回溯范围。）"
                )
            elif hist_klines:
                lines.append(
                    f"\n## Historical Daily Fund Flow "
                    f"(last {len(hist_klines)} trading days"
                    + (f", 截至 {str(curr_date)[:10]}" if historical else "")
                    + ")"
                )
                lines.append(
                    "Date | 主力净流入(万) | 大单(万) "
                    "| 中单(万) | 小单(万) | 超大单(万)"
                )
                for line in hist_klines:
                    parts = line.split(",")
                    if len(parts) >= 6:
                        lines.append(
                            f"  {parts[0]} "
                            f"| main={float(parts[1])/1e4:.0f} "
                            f"| large={float(parts[4])/1e4:.0f} "
                            f"| mid={float(parts[3])/1e4:.0f} "
                            f"| small={float(parts[2])/1e4:.0f} "
                            f"| super={float(parts[5])/1e4:.0f}"
                        )

        return "\n".join(lines)

    except Exception as e:
        return f"Error fetching fund flow for {code}: {str(e)}"


# ---------------------------------------------------------------------------
# 15. Dragon Tiger Board (龙虎榜)
# ---------------------------------------------------------------------------

def get_dragon_tiger_board(
    ticker: str,
    trade_date: str,
    look_back_days: int = 30,
) -> str:
    """Get dragon-tiger board (龙虎榜) appearances and seat details.

    Args:
        ticker: 6-digit A-share code, e.g. '000858'
        trade_date: YYYY-MM-DD
        look_back_days: how many days back to search (default 30)

    Returns:
        Formatted text with LHB appearances, top buyer/seller seats,
        and institutional activity.
    """
    code = _normalize_ticker(ticker)
    end_dt = datetime.strptime(trade_date, "%Y-%m-%d")
    start_dt = end_dt - pd.Timedelta(days=look_back_days)
    start_date_str = start_dt.strftime("%Y-%m-%d")
    lines = [f"# 龙虎榜数据 | {code} | {trade_date} (近{look_back_days}日)"]

    # 1. 上榜记录 — eastmoney datacenter direct HTTP
    try:
        data = _eastmoney_datacenter(
            "RPT_DAILYBILLBOARD_DETAILSNEW",
            filter_str=(
                f"(TRADE_DATE>='{start_date_str}')"
                f"(TRADE_DATE<='{trade_date}')"
                f"(SECURITY_CODE=\"{code}\")"
            ),
            page_size=50,
            sort_columns="TRADE_DATE",
            sort_types="-1",
        )
        if not data:
            lines.append(f"\n近{look_back_days}日未上龙虎榜。")
        else:
            lines.append(f"\n## 上榜记录 ({len(data)} 次)")
            lines.append("日期 | 原因 | 净买入(万) | 换手率")
            for row in data:
                net_buy = round((row.get("BILLBOARD_NET_AMT") or 0) / 10000, 1)
                turnover = round(float(row.get("TURNOVERRATE") or 0), 2)
                lines.append(
                    f"  {str(row.get('TRADE_DATE', ''))[:10]} "
                    f"| {row.get('EXPLANATION', '')} "
                    f"| {net_buy:.0f} "
                    f"| {turnover:.2f}%"
                )
    except Exception as e:
        lines.append(f"龙虎榜列表查询失败: {e}")

    # 2. 最近上榜的买卖席位 — eastmoney datacenter direct HTTP
    try:
        if data:
            latest_date = str(data[0].get("TRADE_DATE", ""))[:10]
            lines.append(f"\n## 最近上榜席位明细 ({latest_date})")

            # 买入席位
            buy_data = _eastmoney_datacenter(
                "RPT_BILLBOARD_DAILYDETAILSBUY",
                filter_str=f"(TRADE_DATE='{latest_date}')(SECURITY_CODE=\"{code}\")",
                page_size=10,
                sort_columns="BUY",
                sort_types="-1",
            )
            if buy_data:
                lines.append("\n### 买入席位 TOP5")
                lines.append("营业部 | 买入(万) | 卖出(万) | 净额(万)")
                for row in buy_data[:5]:
                    buy_amt = round((row.get("BUY") or 0) / 10000, 1)
                    sell_amt = round((row.get("SELL") or 0) / 10000, 1)
                    net = round((row.get("NET") or 0) / 10000, 1)
                    lines.append(
                        f"  {row.get('OPERATEDEPT_NAME', '')} "
                        f"| {buy_amt:.0f} | {sell_amt:.0f} | {net:.0f}"
                    )

            # 卖出席位
            sell_data = _eastmoney_datacenter(
                "RPT_BILLBOARD_DAILYDETAILSSELL",
                filter_str=f"(TRADE_DATE='{latest_date}')(SECURITY_CODE=\"{code}\")",
                page_size=10,
                sort_columns="SELL",
                sort_types="-1",
            )
            if sell_data:
                lines.append("\n### 卖出席位 TOP5")
                lines.append("营业部 | 买入(万) | 卖出(万) | 净额(万)")
                for row in sell_data[:5]:
                    buy_amt = round((row.get("BUY") or 0) / 10000, 1)
                    sell_amt = round((row.get("SELL") or 0) / 10000, 1)
                    net = round((row.get("NET") or 0) / 10000, 1)
                    lines.append(
                        f"  {row.get('OPERATEDEPT_NAME', '')} "
                        f"| {buy_amt:.0f} | {sell_amt:.0f} | {net:.0f}"
                    )
    except Exception:
        pass

    # 3. 机构动向 — 从买卖席位明细筛选机构专用席位 (OPERATEDEPT_CODE="0")
    try:
        inst_buy = 0.0
        inst_sell = 0.0
        for detail, side in [(buy_data, "buy"), (sell_data, "sell")]:
            for row in (detail or []):
                if str(row.get("OPERATEDEPT_CODE", "")) == "0":
                    if side == "buy":
                        inst_buy += (row.get("BUY") or 0)
                    else:
                        inst_sell += (row.get("SELL") or 0)
        if inst_buy > 0 or inst_sell > 0:
            lines.append("\n## 机构动向")
            lines.append(
                f"  机构买入 {inst_buy/1e4:.0f} 万 "
                f"| 卖出 {inst_sell/1e4:.0f} 万 "
                f"| 净额 {(inst_buy - inst_sell)/1e4:.0f} 万"
            )
    except Exception:
        pass

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 16. Lockup Expiry Calendar (限售解禁日历)
# ---------------------------------------------------------------------------

def get_lockup_expiry(
    ticker: str,
    trade_date: str,
    forward_days: int = 90,
) -> str:
    """Get lockup expiry schedule for a stock.

    Args:
        ticker: 6-digit A-share code
        trade_date: YYYY-MM-DD
        forward_days: how many days forward to check (default 90)

    Returns:
        Formatted text with historical unlock records and upcoming
        expiry calendar with impact metrics.
    """
    code = _normalize_ticker(ticker)
    lines = [f"# 限售解禁日历 | {code} | {trade_date}"]

    # 1. 历史解禁记录 — eastmoney datacenter direct HTTP
    try:
        history_data = _eastmoney_datacenter(
            "RPT_LIFT_STAGE",
            filter_str=f"(SECURITY_CODE=\"{code}\")",
            page_size=15,
            sort_columns="FREE_DATE",
            sort_types="-1",
        )
        if history_data:
            lines.append(f"\n## 个股解禁记录 (共 {len(history_data)} 批)")
            lines.append("解禁时间 | 类型 | 解禁数量 | 占比")
            for row in history_data:
                lines.append(
                    f"  {str(row.get('FREE_DATE', ''))[:10]} "
                    f"| {row.get('LIMITED_STOCK_TYPE', '')} "
                    f"| {row.get('FREE_SHARES_NUM', '')} "
                    f"| {row.get('FREE_RATIO', '')}"
                )
        else:
            lines.append("\n无历史解禁记录。")
    except Exception as e:
        lines.append(f"个股解禁查询失败: {e}")

    # 2. 未来待解禁 — eastmoney datacenter direct HTTP
    try:
        end_dt = datetime.strptime(trade_date, "%Y-%m-%d") + pd.Timedelta(
            days=forward_days
        )
        end_str = end_dt.strftime("%Y-%m-%d")
        upcoming_data = _eastmoney_datacenter(
            "RPT_LIFT_STAGE",
            filter_str=(
                f"(SECURITY_CODE=\"{code}\")"
                f"(FREE_DATE>='{trade_date}')"
                f"(FREE_DATE<='{end_str}')"
            ),
            page_size=20,
            sort_columns="FREE_DATE",
            sort_types="1",
        )
        if upcoming_data:
            lines.append(f"\n## 未来 {forward_days} 天待解禁")
            for row in upcoming_data:
                lines.append(
                    f"  {str(row.get('FREE_DATE', ''))[:10]} "
                    f"| {row.get('LIMITED_STOCK_TYPE', '')} "
                    f"| 数量 {row.get('FREE_SHARES_NUM', '')} "
                    f"| 占比 {row.get('FREE_RATIO', '')}"
                )
        else:
            lines.append(f"\n未来 {forward_days} 天无待解禁。")
    except Exception as e:
        lines.append(f"解禁日历查询失败: {e}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 17. Industry Comparison (行业横向对比)
# ---------------------------------------------------------------------------

def get_industry_comparison(
    ticker: str,
    trade_date: str,
    top_n: int = 20,
) -> str:
    """Get industry sector performance comparison.

    Args:
        ticker: 6-digit A-share code
        trade_date: YYYY-MM-DD (reference date shown in report header)
        top_n: number of top/bottom industries to show (default 20)

    Returns:
        Formatted markdown: industry board ranking (top gainers / bottom losers)
        with rank, latest index, change amount, change %, total market cap,
        turnover, up/down stock counts and leading stock, scraped from the
        Eastmoney gridlist#industry_board_2 page via playwright_service.
    """
    code = _normalize_ticker(ticker)
    lines = [f"# 行业横向对比 | {code} | {trade_date}"]

    # 东财行业板块页面爬取 (playwright_service, 直连 HTTP 被封)
    try:
        from playwright_service.client import PlaywrightClient
        client = PlaywrightClient()
        result = client.industry_board(top_n)
        if not result.get("success"):
            lines.append(f"行业对比查询失败: {result.get('error', '')}")
        else:
            top = result.get("top", [])
            bottom = result.get("bottom", [])
            total = result.get("total_industries", 0)
            if top or bottom:
                lines.append(
                    f"\n## 全行业表现 (东财 {total} 个行业, 官方板块指数口径, 按涨跌幅降序)"
                )
                lines.append("排名 | 行业 | 最新价 | 涨跌额 | 涨跌幅 | 总市值 | 换手率 | 上涨 | 下跌 | 领涨股")

                def _fmt(v, plus=False, suffix=""):
                    if v is None:
                        return "--"
                    try:
                        f = f"{float(v):+.2f}" if plus else f"{float(v):.2f}"
                    except (ValueError, TypeError):
                        return "--"
                    return f + suffix

                def _render_section(title, items):
                    lines.append(title)
                    for item in items:
                        leader = item.get("leader_name", "")
                        lchg = item.get("leader_chg")
                        if lchg is not None:
                            leader += f" {lchg:+.2f}%"
                        lines.append(
                            f"  {item.get('rank') or '?'} {item.get('name', '')} "
                            f"| {_fmt(item.get('price'))} "
                            f"| {_fmt(item.get('change'), plus=True)} "
                            f"| {_fmt(item.get('chg'), plus=True, suffix='%')} "
                            f"| {item.get('mktcap', '')} "
                            f"| {_fmt(item.get('turnover'), suffix='%')} "
                            f"| {item.get('up', 0)} | {item.get('down', 0)} "
                            f"| {leader}"
                        )

                _render_section("### 涨幅居前:", top)
                _render_section("### 跌幅居前:", bottom)
            else:
                lines.append("行业数据获取为空。")
    except Exception as e:
        lines.append(f"行业对比查询失败: {e}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 18. Chip Distribution (筹码分布 - Python CYQ algorithm)
# ---------------------------------------------------------------------------
# 模型: 自归一化迭代（chip = chip*(1-weight) + new_chip*weight，总量恒为1），
# 与通达信 WINNER/COST 同构。2026-08 经 fengwo(通达信DLL) + 东财官方 15 只
# 股票(2026-08-05)交叉验证: 平均误差 获利1.35pt / 90%集中度0.42pt / 70%0.61pt。
#
# 官方口径结论（实测）:
#   - 数据: 前复权(fqt=1)，窗口 ~350 根（约1.4年）
#   - 三角分布峰值: (最高+最低)/2（通达信 hlavg）
#   - 历史换手衰减系数: 1.0（通达信标准；此前"自动分档/0.5"与官方系统性背离）
#   - 量能加权 / 十大流通股东换手还原: 官方未采用，保留为可选参数(默认关闭)
#
# 窗口: 350 根前复权日K（实际根数以数据源返回为准）。

# 价格分桶: 桶宽目标 = 窗口最高价 * _CYQ_BUCKET_PCT (0.05%)，桶数限制在
# [_CYQ_BUCKETS_MIN, _CYQ_BUCKETS_MAX]。等宽固定150桶在高价股(如600519茅台
# 桶宽~13元)上分位/成本误差可达±半桶，动态分桶使桶宽随价格缩放到~0.05%，
# 茅台约0.7元/桶、601288约0.003元/桶，与官方0.5元级精度对齐。
_CYQ_BUCKET_PCT = 0.0005
_CYQ_BUCKETS_MIN = 150
_CYQ_BUCKETS_MAX = 2000
_CYQ_KLINE_COUNT = 350
_CYQ_DECAY_COEFF = 1.0  # 通达信标准衰减系数（decay_coeff=None 时按此兜底）
# 可选: 按近60日均换手率自动分档推断（仅 decay_coeff=None 显式启用时生效）
_CYQ_AUTO_TURNS = ((0.08, 0.5), (0.05, 0.8), (0.01, 1.0), (0.0, 1.5))
# 可选: 量能加权(vol_weighted=True 时生效) — 当日筹码替换比例乘以相对量能，
# 大成交日贡献更大。官方未采用，默认关闭。
_CYQ_VOL_WINDOW = 60
_CYQ_VOL_RATIO_MIN = 0.5
_CYQ_VOL_RATIO_MAX = 3.0
# 可选: 十大流通股东"死筹"换手还原（top10_ratios 传入时生效）—
# 实际换手率 = 名义换手率/(1-稳定占比)，仅统计持股≥4季度不变的股东。
# 官方未采用，默认关闭（helpers 保留供可选调用）。
_CYQ_TOP10_GATE = 0.30          # 稳定占比超过30%才调整
_CYQ_TOP10_MIN_PERIODS = 4      # 需在≥4期(≈4季度)中持续在榜
_CYQ_TOP10_MAX_CHANGE = 50.0    # 各期持股变动≤50%视为稳定(%)，送转/解禁等口径噪声的宽容阈值
_CYQ_TOP10_MAX_RATIO = 0.95     # 分母保护上限


def _parse_pct_text(s) -> float:
    """解析百分比文本: '22.77%' -> 22.77; '不变'/''/None -> 0.0; '-1.43%' -> -1.43"""
    if s is None:
        return 0.0
    m = _re.search(r"[-+]?\d+(?:\.\d+)?", str(s))
    return float(m.group()) if m else 0.0


def _stable_top10_ratio_series(holder_data: dict) -> list[tuple[str, float]] | None:
    """从股东研究数据计算各期"持股超4季度不变"的十大流通股东占比合计。

    口径（天狼50/指南针）: 仅统计在≥4期股东快照中持续在榜且持股变动不超过
    _CYQ_TOP10_MAX_CHANGE 的股东（国资/战投/公募重仓=死筹），量化基金/游资等
    短线股东进出快、不计入。返回 [(period_date, ratio), ...] 按日期升序；
    无数据或无可判定稳定股东时返回 None。
    """
    if not isinstance(holder_data, dict):
        return None
    periods = holder_data.get("top10Holders")
    if not periods:
        return None
    parsed = []
    for per in periods:
        date = per.get("period")
        holders = per.get("holders") or []
        names = {}
        for h in holders:
            nm = str(h.get("name") or "").strip()
            if not nm:
                continue
            names[nm] = (_parse_pct_text(h.get("ratio")), _parse_pct_text(h.get("changePct")))
        if names:
            parsed.append((str(date), names))
    if not parsed:
        return None
    parsed.sort(key=lambda x: x[0])

    all_names = {nm for _, ns in parsed for nm in ns}
    stable_names = set()
    for nm in all_names:
        appear = [ns[nm] for _, ns in parsed if nm in ns]
        if len(appear) < _CYQ_TOP10_MIN_PERIODS:
            continue
        changes = [abs(ch) for _, ch in appear]
        if changes and max(changes) <= _CYQ_TOP10_MAX_CHANGE:
            stable_names.add(nm)
    if not stable_names:
        return None

    series = []
    for date, ns in parsed:
        total = sum(ratio for nm, (ratio, _) in ns.items() if nm in stable_names)
        series.append((date, min(total / 100.0, _CYQ_TOP10_MAX_RATIO)))
    return series


def _top10_ratio_for_date(series: list[tuple[str, float]] | None, date: str) -> float:
    """按日期取稳定的十大流通股东占比（前向填充，早于首期用首期值）。"""
    if not series:
        return 0.0
    best = None
    for d, r in series:
        if d <= date:
            best = r
        else:
            break
    return best if best is not None else series[0][1]


def _infer_decay_coeff(rows: list[tuple]) -> float:
    """按近60日均换手率自动推断历史换手衰减系数（分档挤"成交量水分"）。

    rows: 预处理后的 (date, low, high, close, volume, turnover%) 列表。
    """
    turns = [r[5] for r in rows[-60:]]
    if not turns:
        return _CYQ_DECAY_COEFF
    avg_turn = sum(turns) / len(turns) / 100.0  # 百分比 -> 小数
    for threshold, coef in _CYQ_AUTO_TURNS:
        if avg_turn > threshold:
            return coef
    return _CYQ_DECAY_COEFF


def _compute_cyq(klines: list[dict], decay_coeff: float = 1.0,
                 top10_ratios: list[tuple[str, float]] | None = None,
                 vol_weighted: bool = False) -> dict:
    """Compute chip distribution (CYQ) from K-line data.

    通达信 WINNER/COST 同构的自归一化迭代模型（2026-08 经 fengwo 通达信DLL +
    东财官方 15 只股票验证，平均误差 获利1.35pt / 90%0.42pt / 70%0.61pt）:
    1. 价格轴: 动态分桶——桶宽目标 = 窗口最高价 * 0.05%，桶数 150~2000，
       分位/成本精度达价格 0.05% 级
    2. 每日: weight = min(换手率/100 * 衰减系数, 1.0)
             chip = chip * (1 - weight) + new_chip * weight
       当日新筹码 new_chip 为 low~high 间三角分布（峰值 (high+low)/2，
       通达信 hlavg 口径），归一化后以 weight 比例替换旧筹码，总量恒为 1
    3. 健壮处理: 过滤 NaN/0 成交量/非法价格的交易日（停牌日跳过），
       换手率为 NaN 时视为 0（当日不衰减），hi<=lo 退化日筹码落入单桶；
       分位价用桶内线性插值（COST(N) 亚桶精度）

    Args:
        klines: 日K列表（含 date/open/close/high/low/volume/turnover）。
            官方口径为前复权数据（见 get_astock_chip_distribution 的 fqt=1 请求）。
        decay_coeff: 历史换手衰减系数，默认 1.0（通达信标准）。None=按近60日
            均换手自动分档推断（>8%->0.5, 5~8%->0.8, <1%->1.5, 其余->1.0，
            与官方系统性背离，仅显式启用）。
        top10_ratios: [(period_date, 稳定十大流通股东占比), ...] 按日期升序，
            将名义换手率还原为实际换手率（占比>30%时生效）。官方未采用，
            默认 None 不调整。
        vol_weighted: 是否按相对量能(vol/近60日均量, 裁剪0.5~3)放大当日权重，
            使大成交日贡献更大。官方未采用，默认 False。

    Returns dict with: profit_ratio, avg_cost, cost_90_low, cost_90_high,
                       concentration_90, cost_70_low, cost_70_high, concentration_70
    """
    # ── 健壮预处理: 过滤 NaN/0/非法交易日 ──
    rows = []
    for k in klines:
        try:
            date = str(k.get("date") or "")
            lo = float(k.get("low"))
            hi = float(k.get("high"))
            cl = float(k.get("close"))
            vol = float(k.get("volume") or 0)
            turn = float(k.get("turnover") or 0)
        except (TypeError, ValueError):
            continue
        if not (vol > 0) or not (hi > 0) or not (lo > 0) or hi < lo:
            continue
        if not (turn >= 0):  # NaN 换手率视为 0
            turn = 0.0
        rows.append((date, lo, hi, cl, vol, turn))
    if len(rows) < 10:
        return {}

    if decay_coeff is None:
        decay_coeff = _infer_decay_coeff(rows)

    p_min = min(r[1] for r in rows)
    p_max = max(r[2] for r in rows)
    if p_max <= p_min:
        return {}

    # 动态分桶: 桶宽目标 = 最高价 * 0.05%，桶数限制 150~2000
    span = p_max - p_min
    target_bucket = max(p_max * _CYQ_BUCKET_PCT, 1e-9)
    n_buckets = int(min(max(span / target_bucket, _CYQ_BUCKETS_MIN), _CYQ_BUCKETS_MAX))
    bucket_size = span / n_buckets
    if bucket_size <= 0:
        return {}

    # 桶中心价格
    centers = np.linspace(p_min + bucket_size / 2.0, p_max - bucket_size / 2.0, n_buckets)

    # ── 逐日自归一迭代（通达信标准；量能/死筹调整为可选）──
    vols = [r[4] for r in rows]
    chip = np.zeros(n_buckets, dtype=np.float64)
    for i, (date, lo, hi, cl, vol, turn) in enumerate(rows):
        weight = min(turn / 100.0 * decay_coeff, 1.0)
        if vol_weighted:
            # 相对量能 = 当日量 / 前60日均量（不含当日），裁剪防极端放量
            start = max(0, i - _CYQ_VOL_WINDOW)
            avg_vol = sum(vols[start:i]) / (i - start) if i > start else vol
            vol_ratio = vol / avg_vol if avg_vol > 0 else 1.0
            vol_ratio = min(max(vol_ratio, _CYQ_VOL_RATIO_MIN), _CYQ_VOL_RATIO_MAX)
            weight = min(weight * vol_ratio, 1.0)
        if top10_ratios:
            # 死筹调整: 名义换手率 -> 实际换手率（稳定占比>30%才启用）
            top10_r = _top10_ratio_for_date(top10_ratios, date)
            if top10_r > _CYQ_TOP10_GATE:
                weight = min(weight / (1.0 - top10_r), 1.0)

        new_chip = np.zeros(n_buckets, dtype=np.float64)

        if hi > lo:
            # 通达信三角分布: 峰值 (high+low)/2，向两端线性衰减
            pk = (hi + lo) / 2.0
            idx = np.where((centers >= lo) & (centers <= hi))[0]
            if len(idx) == 0:
                # 当日价格区间完全落在桶间缝隙（罕见）: 落入最近桶
                b = int(np.clip((pk - p_min) / bucket_size, 0, n_buckets - 1))
                new_chip[b] = 1.0
            else:
                # 三角分布: 峰值 (high+low)/2（通达信 hlavg）, 向 low/high 线性衰减
                c_sel = centers[idx]
                left = c_sel <= pk
                li = idx[left]
                ri = idx[~left]
                if len(li):
                    if pk > lo:
                        new_chip[li] = (centers[li] - lo) / (pk - lo)
                    else:
                        new_chip[li] = 1.0
                if len(ri):
                    if hi > pk:
                        new_chip[ri] = (hi - centers[ri]) / (hi - pk)
                    else:
                        new_chip[ri] = 1.0
                s = new_chip.sum()
                if s > 0:
                    new_chip /= s
        else:
            b = int(np.clip((pk - p_min) / bucket_size, 0, n_buckets - 1))
            new_chip[b] = 1.0

        chip = chip * (1.0 - weight) + new_chip * weight

    total = chip.sum()
    if total <= 0:
        return {}
    chip /= total

    # ── 衍生指标 ──
    current_price = rows[-1][3]  # 最后一根有效K线的收盘价
    profit_ratio = float(chip[centers <= current_price].sum())
    avg_cost = float((centers * chip).sum())

    cum = np.cumsum(chip)

    def _percentile_price(pct: float) -> float:
        """COST(N) 分位价: 桶内线性插值（亚桶精度）。"""
        i = int(np.searchsorted(cum, pct))
        if i <= 0:
            return float(centers[0])
        if i >= n_buckets:
            return float(centers[-1])
        c_prev = cum[i - 1]
        c_cur = cum[i]
        if c_cur <= c_prev:
            return float(centers[i])
        f = (pct - c_prev) / (c_cur - c_prev)
        return float(centers[i - 1] + f * (centers[i] - centers[i - 1]))

    cost_90_low = _percentile_price(0.05)
    cost_90_high = _percentile_price(0.95)
    cost_70_low = _percentile_price(0.15)
    cost_70_high = _percentile_price(0.85)

    concentration_90 = (cost_90_high - cost_90_low) / (cost_90_high + cost_90_low) if (cost_90_high + cost_90_low) > 0 else 0
    concentration_70 = (cost_70_high - cost_70_low) / (cost_70_high + cost_70_low) if (cost_70_high + cost_70_low) > 0 else 0

    return {
        "profit_ratio": round(profit_ratio, 4),
        "avg_cost": round(avg_cost, 4),
        "cost_90_low": round(cost_90_low, 4),
        "cost_90_high": round(cost_90_high, 4),
        "concentration_90": round(concentration_90, 4),
        "cost_70_low": round(cost_70_low, 4),
        "cost_70_high": round(cost_70_high, 4),
        "concentration_70": round(concentration_70, 4),
        "current_price": round(current_price, 4),
    }


def get_astock_chip_distribution(
    ticker: str,
) -> str:
    """Get chip distribution (筹码分布) for an A-stock.

    通达信口径（经 fengwo 通达信DLL + 东财官方 15 只股票交叉验证）:
    ~350 根前复权(fqt=1)日K + 自归一三角分布(峰值 (H+L)/2) + 衰减系数 1.0。
    平均误差 vs 官方: 获利 1.35pt / 90% 0.42pt / 70% 0.61pt / 成本 2.45%。
    """
    code = safe_ticker_component(ticker)
    lines = [f"# 筹码分布 | {code}"]

    try:
        # 数据源: playwright_service 浏览器通道（规避东财 push2his 直连风控；
        # 服务端通过浏览器上下文重取 lmt=350、fqt=1 前复权历史——官方口径）。
        # records 含 date/open/close/high/low/volume/turnover，与 _compute_cyq 所需字段一致。
        # 350根需要 页面加载(≤15s)+浏览器重取(≤30s)，共享单例客户端默认30s超时会间歇性
        # TimeoutError，这里用独立长超时客户端。
        from playwright_service.client import PlaywrightClient
        client = PlaywrightClient(timeout=90)
        res = client.stock_kline_full(code, _CYQ_KLINE_COUNT, fqt=1)
        if not res.get("success"):
            return f"[筹码分布] {code}: {res.get('error', '')}"
        klines = res.get("data", []) or []
        if len(klines) < 10:
            return f"[筹码分布] {code}: K线数据不足({len(klines)}根)"

        cyq = _compute_cyq(klines)
        if not cyq:
            return f"[筹码分布] {code}: 筹码计算失败"

        current_price = cyq["current_price"]
        profit_ratio = cyq["profit_ratio"]
        avg_cost = cyq["avg_cost"]
        c90 = cyq["concentration_90"]
        c70 = cyq["concentration_70"]

        lines.append(f"# 数据源: 东财行情(playwright, {len(klines)}根, 前复权) + 通达信CYQ算法")
        lines.append("")

        # Chip health assessment
        if profit_ratio >= 0.9:
            health = "警惕（获利盘极高，抛压风险大）"
        elif c90 < 0.15 and 0.3 <= profit_ratio < 0.9:
            health = "健康（筹码集中且获利比例适中）"
        elif c90 >= 0.30:
            health = "警惕（筹码分散，主力控盘弱）"
        else:
            health = "一般"

        lines.append(f"当前价: {current_price}")
        lines.append(f"获利比例: {profit_ratio:.1%}")
        lines.append(f"平均成本: {avg_cost}")
        lines.append(f"90%成本区间: {cyq['cost_90_low']} ~ {cyq['cost_90_high']}")
        lines.append(f"90%集中度: {c90:.2%} {'(集中)' if c90 < 0.15 else '(分散)' if c90 > 0.30 else '(适中)'}")
        lines.append(f"70%成本区间: {cyq['cost_70_low']} ~ {cyq['cost_70_high']}")
        lines.append(f"70%集中度: {c70:.2%}")
        lines.append(f"筹码健康度: {health}")
        lines.append("")

        # Position relative to cost
        if current_price > avg_cost * 1.1:
            lines.append(f"当前价高于平均成本 {(current_price/avg_cost-1)*100:.1f}%，获利盘较多")
        elif current_price < avg_cost * 0.9:
            lines.append(f"当前价低于平均成本 {(1-current_price/avg_cost)*100:.1f}%，套牢盘较多")
        else:
            lines.append("当前价接近平均成本，筹码在成本附近")

        return "\n".join(lines)
    except Exception as e:
        return f"[筹码分布] {code}: 获取异常: {type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# 19. Limit Up Pool / Consecutive Boards (涨停池/连板梯队)
# ---------------------------------------------------------------------------

def get_astock_limit_up_pool(
    curr_date: str = "",
    n: int = 20,
) -> str:
    """Get limit-up pool with consecutive board ladder (涨停池/连板梯队).

    Direct HTTP to Eastmoney push2ex getTopicZTPool.

    Args:
        curr_date: YYYY-MM-DD format, empty for today
        n: number of top stocks to return (sorted by consecutive boards desc)
    Returns:
        Formatted text with limit-up pool and board ladder analysis
    """
    if not curr_date:
        curr_date = datetime.now().strftime("%Y-%m-%d")
    date_compact = curr_date.replace("-", "")

    lines = [f"# 涨停池/连板梯队 | {curr_date}"]

    try:
        url = "https://push2ex.eastmoney.com/getTopicZTPool"
        params = {
            "ut": "7eea3edcaed734bea9cbfc24409ed989",
            "dpt": "wz.ztzt",
            "Pageindex": "0",
            "pagesize": "10000",
            "sort": "fbt:asc",
            "date": date_compact,
        }
        r = _em_get(url, params=params, timeout=15)
        d = r.json()
        pool = d.get("data", {}).get("pool", [])

        if not pool:
            lines.append("当日无涨停股或数据未更新")
            return "\n".join(lines)

        # Parse and sort by consecutive boards desc, first limit time asc
        stocks = []
        for item in pool:
            stocks.append({
                "code": item.get("code", ""),
                "name": item.get("name", ""),
                "price": round(item.get("price", 0) / 1000, 2),
                "change_pct": item.get("zdp", 0),
                "amount": item.get("amount", 0),
                "turnover": item.get("hs", 0),
                "consecutive_boards": item.get("lbc", 0),
                "first_limit_time": str(item.get("fbt", "")).zfill(6),
                "last_limit_time": str(item.get("lbt", "")).zfill(6),
                "seal_amount": item.get("fund", 0),
                "break_count": item.get("zbc", 0),
                "industry": item.get("hy", ""),
            })

        stocks.sort(key=lambda x: (-x["consecutive_boards"], x["first_limit_time"]))
        top = stocks[:n]

        lines.append(f"# 数据源: 东财push2ex | 共 {len(pool)} 只涨停")
        lines.append("")

        # Board ladder summary
        board_counts = {}
        for s in stocks:
            bc = s["consecutive_boards"]
            board_counts[bc] = board_counts.get(bc, 0) + 1
        ladder_parts = []
        for bc in sorted(board_counts.keys(), reverse=True):
            suffix = "板" if bc == 1 else "连板"
            ladder_parts.append(f"{bc}{suffix}({board_counts[bc]}只)")
        lines.append(f"连板梯队: {' > '.join(ladder_parts)}")
        lines.append("")

        # Top stocks table
        lines.append(f"{'代码':<8} {'名称':<8} {'连板':>4} {'涨幅':>8} {'现价':>8} {'封板资金':>12} {'炸板':>4} {'行业':<8} {'首封时间':<8}")
        lines.append("-" * 80)
        for s in top:
            seal_str = f"{s['seal_amount']/1e8:.2f}亿" if s["seal_amount"] > 0 else "N/A"
            fbt = s["first_limit_time"]
            fbt_str = f"{fbt[:2]}:{fbt[2:4]}:{fbt[4:]}" if len(fbt) >= 6 else fbt
            lines.append(
                f"{s['code']:<8} {s['name']:<8} {s['consecutive_boards']:>4} "
                f"{s['change_pct']:>+7.1f}% {s['price']:>8.2f} {seal_str:>12} "
                f"{s['break_count']:>4} {s['industry']:<8} {fbt_str:<8}"
            )

        return "\n".join(lines)
    except Exception as e:
        return f"[涨停池] 获取异常: {type(e).__name__}: {e}"
