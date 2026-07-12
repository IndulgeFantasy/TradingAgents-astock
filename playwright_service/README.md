# Playwright 数据服务

独立 HTTP 服务，通过 playwright + Chrome CDP 抓取同花顺F10/问财/东财行情数据。
不依赖 akshare，所有数据通过浏览器自动化获取。

## 架构

```
worktrade3环境(主程序)                    worktrade2环境(playwright服务)
┌─────────────────────┐                 ┌──────────────────────┐
│ TradingAgents主程序  │  HTTP :8765     │ playwright_service/   │
│  playwright_tools.py │ ──────────────> │  server.py            │
│  -> PlaywrightClient │  JSON响应       │  (playwright + CDP)   │
│  (熔断器+Lock保护)   │                 │  (每请求独立page)     │
└─────────────────────┘                 └──────────────────────┘
```

## 安全机制

- **熔断器**（`client.py`）：5次连续失败后熔断60秒，`threading.Lock` 线程安全
- **独立 page**（`server.py`）：每个请求 `ctx.new_page()` + `finally page.close()`，并发请求数据不串台
- **参数校验**（`server.py`）：`code` 参数正则校验 `^\d{6}$`
- **SSE 流容错**（`server.py`）：`_parse_sse_lines` 辅助函数，单行畸形 JSON 跳过不影响整体

## 安装

```bash
conda activate worktrade2
pip install -r playwright_service/requirements-server.txt
playwright install chromium
```

## 启动

```bash
conda activate worktrade2
python playwright_service/server.py [--port 8765]
```

需要 Chrome 以 `--remote-debugging-port=9222` 启动（CDP 连接）。

## API 端点

| 路径 | 参数 | 说明 | 数据源 |
|------|------|------|--------|
| `/api/health` | 无 | 健康检查 | - |
| `/api/routes` | 无 | 路由列表 | - |
| `/api/fund-flow` | code | 个股资金流+概念(问财) | 问财 barline3 |
| `/api/stock-basic` | code | 股本结构 | 同花顺F10 equity |
| `/api/stock-homepage` | code | PE/PB/市值/质押/分类 | 同花顺F10 首页 |
| `/api/stock-holder` | code | 股东人数+前十大股东 | 同花顺F10 holder |
| `/api/stock-equity-history` | code | 股本历史变动 | 同花顺F10 equity |
| `/api/stock-industry-peers` | code | 同行业对标 | 同花顺F10 field |
| `/api/market-overview` | 无 | 大盘概览 | 东财 zs 页面 |
| `/api/stock-position` | code | 机构持仓汇总+明细 | 同花顺F10 position |
| `/api/stock-kline-full` | code, days | 增强K线(含换手率) | 东财 push2his |
| `/api/financial-quarterly` | code | 季频财务指标 | 同花顺F10 finance |
| `/api/concept-blocks` | code | 概念归属 | 问财 |
| `/api/stock-levels` | code | 支撑位/压力位 | 问财 kline2 |
| `/api/wencai-all` | code | 问财全数据 | 问财 |
| `/api/eps-forecast` | code | EPS一致预期 | 同花顺F10 worth |
