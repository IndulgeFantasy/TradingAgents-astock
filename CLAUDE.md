# TradingAgents-Astock

## 项目概述
基于 [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents)（65K Stars）的 A 股深度特化 fork。多 Agent 投研框架，7 个 Analyst 角色通过 Bull/Bear 辩论 + 三方风险辩论生成投资报告。

- **仓库**: https://github.com/simonlin1212/TradingAgents-astock
- **协议**: Apache 2.0
- **Python**: >=3.10
- **当前版本**: 0.2.19

## 架构

### 数据层（v0.2.5 全部直连 HTTP，零第三方数据库依赖）
| 来源 | 协议 | 数据 |
|------|------|------|
| mootdx | TCP 7709 | OHLCV K线、财务快照、F10 文本 |
| 腾讯财经 | HTTP (qt.gtimg.cn) | PE/PB/市值/换手率 |
| 东方财富 datacenter | HTTP (datacenter-web) | 龙虎榜、限售解禁、板块行情 |
| 东方财富 push2/push2his | HTTP (push2.eastmoney) | 实时行情、个股信息、板块列表、资金流(分钟+日级)、筹码分布(CYQ)、后验收益 |
| 东方财富 push2ex | HTTP (push2ex.eastmoney) | 涨停池/连板梯队 |
| 东方财富 np-weblist | HTTP | 滚动新闻 |
| 新浪财经 | HTTP (money.finance.sina) | K线历史、财报三表 |
| 同花顺 10jqka | HTTP/playwright | EPS 一致预期、热股题材、F10 股本/股东/财务 |
| 问财 (iwencai) | playwright | 概念归属、资金流时序、支撑位/压力位 |
| 财联社 cls.cn | HTTP | 全球财经快讯 |
| 百度股市通 | HTTP (gushitong.baidu) | 概念板块归属（资金流已迁移至东财push2） |

### playwright_service 数据服务（独立 HTTP 服务，worktrade2 环境）
- 独立 conda 环境运行，通过 Chrome CDP 抓取同花顺F10/问财/东财页面
- 主程序通过 `PlaywrightClient` HTTP 调用，不直接依赖 playwright 库
- 熔断器保护（5次连续失败后熔断60秒，`threading.Lock` 线程安全）
- 16 个 API 端点：股本结构/首页综合/股东研究/股本历史/行业对标/大盘概览/主力持仓/增强K线/季频财务/概念归属/支撑压力/问财全数据/EPS预测/资金流/涨停池/筹码分布

### 技术分析能力
- **K线形态识别**（`analysis_tools.py`）：12种形态（十字星/锤子线/吞没/早晨之星/双底/放量突破/箱体震荡等），纯函数零依赖
- **筹码分布**（`a_stock.py`）：Python CYQ算法（150价格桶+换手率衰减+三角形分布），计算获利比例/平均成本/90%/70%集中度
- **涨停池连板梯队**（`a_stock.py`）：东财push2ex直连，连板数/封板资金/炸板次数/所属行业
- **精确十进制计算**（`financial_rigor.py`）：Decimal精确PE/PB/ROE验算、市值验算、多源交叉验证、Benford造假检测、三情景估值

### Agent 角色（7 个）
原版 4 个（市场/情绪/新闻/基本面）+ A 股特化 3 个（政策分析师/游资追踪/解禁监控）

### 质量门禁
- **Quality Gate 节点**（`quality_gate.py`）：分析师产出后、辩论前的强制关卡。Layer 1 硬检查（空报告/长度/失败标记/数据缺失，A-F评级），Layer 2 LLM 复审（数据时效/缺失项/可信度）
- **结构化输出校验**（`schemas.py`）：Pydantic BaseModel + Enum 强制 5 档评级（Buy/Overweight/Hold/Underweight/Sell）
- **确定性评级解析**（`rating.py`）：零 LLM 调用的启发式解析，集中定义避免漂移

### 关键路径
- `tradingagents/dataflows/a_stock.py` - A 股数据 vendor，所有数据获取入口（含 `_em_get` 线程安全限流）
- `tradingagents/dataflows/interface.py` - VENDOR_METHODS 路由表（19个方法）
- `tradingagents/dataflows/utils.py` - `safe_ticker_component` 路径安全校验 + 中文 ticker 自动解析
- `tradingagents/agents/utils/agent_utils.py` - 工具聚合导入
- `tradingagents/agents/utils/analysis_tools.py` - K线形态识别 @tool
- `tradingagents/agents/utils/financial_rigor.py` - 精确十进制计算 @tool
- `tradingagents/agents/utils/playwright_tools.py` - playwright_service 工具入口
- `tradingagents/agents/quality_gate.py` - 质量门禁节点
- `tradingagents/agents/` - 7 个 Analyst + Bull/Bear 辩论逻辑
- `tradingagents/graph/trading_graph.py` - 图编排 + ToolNode 注册
- `tradingagents/graph/propagation.py` - 初始 state 创建
- `web/` - Streamlit Web UI
- `cli/` - CLI 入口

### 中文股票名解析链路
用户/LLM 输入 -> `safe_ticker_component` 检测中文 -> `resolve_ticker()` -> `_build_name_code_map()`（mootdx 全市场映射，缓存）-> 返回 6 位代码

### 后验反思闭环
`_fetch_returns()` 使用东财 push2his HTTP API（而非 yfinance）获取个股和沪深300基准收益率，国内网络可靠可用。

## 已知问题与注意事项

### 依赖冲突（v0.2.6 已缓解）
mootdx 锁死 httpx==0.25.2，与 langchain-google-genai 的 httpx>=0.28.1 冲突。v0.2.6 将 google-genai 移至可选依赖 `[google]`，`pip install -e .` 不再冲突。需要 Google 模型时 `pip install -e ".[google]"`。

### akshare 已移除（v0.2.5）
v0.2.5 起完全移除 akshare 依赖，所有数据通过直连 HTTP API 或 playwright_service 获取。

### 百度 PAE 资金流接口已下线（v0.2.7 已修复）
`fundsortlist` 和 `fundflow` 两个接口返回空（2026-05-19 确认）。v0.2.7 已替换为东财 push2 资金流 API。

### 东财接口防封限流（v0.2.19 加 threading.Lock 线程安全）
`a_stock.py` 里所有指向 `eastmoney.com` 的请求统一走节流入口 `_em_get()`：`threading.Lock` 保护模块级时间戳串行限流（默认间隔 `EM_MIN_INTERVAL=1.0s`）+ 0.1~0.5s 随机抖动 + 复用 `requests.Session`（Keep-Alive）+ 默认 UA。Web UI 多 analyst 并发安全。批量场景可设 `EM_MIN_INTERVAL=1.5~2` 降速。

### yfinance 依赖（v0.2.19 移除顶层 import）
`trading_graph.py` 不再顶层 `import yfinance`。后验收益获取改用东财 push2his HTTP API。`main.py` 默认配置改为 `a_stock` vendor。yfinance 仍作为可选 vendor 保留在 `interface.py` 中。

### 模型兼容性
deepseek-v4-flash 等模型在 tool call 时可能返回中文股票名而非 6 位代码。`safe_ticker_component` 已加兜底自动转码，但不同模型表现仍有差异。

### 待处理 PR
- PR #18（hejingchi）：start_date 功能 + 主题切换 + Windows 字体。不建议直接 merge（与 v0.2.6 冲突），start_date 功能值得后续自行实现。

## Issue 归档
所有 GitHub Issue 的详细记录在 `issues/` 文件夹中，包含问题描述、根因分析、修复方案和当前状态。

## 开发规范
- 改动前先跑 `python -m pytest tests/ -v --ignore=tests/test_google_api_key.py` 确保不破坏现有测试
- `safe_ticker_component` 是安全边界，任何绕过路径校验的改动必须慎重评估
- 数据层新增接口遵循 `tradingagents/dataflows/interface.py` 的 vendor 路由模式
- 新增东财端点务必走 `_em_get` 而非裸 `requests.get`
- 新增 @tool 必须同时注册到 `agent_utils.py` 导入 + `trading_graph.py` 的 ToolNode
- analyst 的 tools 列表必须与 ToolNode 注册完全一致
- Web UI 改动在 `web/` 目录，用 `streamlit run web/launch.py` 本地测试

## 相关项目
- [a-stock-data](https://github.com/simonlin1212/a-stock-data) - A 股 MCP 数据服务（Claude Code 用的 skill）
- 上游 [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) - 原版框架
