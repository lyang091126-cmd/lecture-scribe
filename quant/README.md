# JEV 量化交易回测与模拟盘系统

> 基于 **TypeSafe Jev 决策大模型（Choice 原语）** 的美股 / 国内商品期货量化策略系统，
> 支持历史 Tick 回测与实时盘口模拟盘（Paper Trading），自带可视化大屏与后台参数控制面板。

```
[行情 / 盘口 Tick] ──> [State Builder 状态构建]
                              │
                              ▼
[控制面板(阈值)] ──> [Jev 决策引擎 (Choice 概率)] ──> [风控校验 + 撮合执行]
                                                          │
[可视化大屏 (胜率/资金曲线/触发条件)] <── [SSE 实时推送] <──┘
```

---

## 1. 快速开始

### 方式一（macOS / Linux）：一键脚本

```bash
cd quant
cp .env.example .env          # 可选：填入 TYPESAFE_API_KEY / 行情 Key
./start.sh                    # 构建前端 + 启动后端，单端口 http://localhost:8000
```

其他用法：

| 命令 | 作用 |
| --- | --- |
| `./start.sh` | 构建前端并由 FastAPI 单端口托管 → `http://localhost:8000` |
| `./start.sh dev` | 后端 `:8000` + Vite 热更新前端 `:5173`（前端已配 `/api` 代理） |
| `./start.sh backend` | 只跑后端（`--reload`），API 文档在 `/docs` |
| `./start.sh test` | 跑后端测试（55 个用例） |

### 方式一（Windows）：双击 `run.bat`

```bat
cd quant
copy .env.example .env       :: 可选：填入 TYPESAFE_API_KEY / 行情 Key
run.bat                      :: 构建前端 + 启动后端 -> http://localhost:8000
```

| 命令 | 作用 |
| --- | --- |
| `run.bat` | 构建前端并由后端单端口托管 → `http://localhost:8000` |
| `run.bat dev` | 只启动后端（自动重载），前端另开 `cd frontend && npm run dev` |
| `run.bat test` | 跑后端测试 |

端口被占用时：`set PORT=8010 && run.bat`。没装 Node 也能启动，只是没有大屏，仅提供 `/docs` API。

### 方式二：Docker Compose

```bash
cd quant
cp .env.example .env
docker compose up --build
# 前端 http://localhost:8080   后端 http://localhost:8000/docs
```

### 方式三：手动

```bash
# 后端
cd quant/backend && python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.server:app --port 8000

# 前端（另开终端）
cd quant/frontend && npm install && npm run dev   # http://localhost:5173
```

> **离线可用**：未配置 `TYPESAFE_API_KEY` 时，Jev 客户端自动降级为本地动量/均线 Mock 模型，
> 输出同样的 Choice 概率分布；未配置行情 Key 时使用可复现的合成盘口。
> 整条链路（决策 → 风控 → 撮合 → 推送 → 大屏）无需任何外部依赖即可完整演示。

---

## 2. 目录结构

```
quant/
├── backend/
│   ├── app/
│   │   ├── server.py         FastAPI：控制面板 API + SSE + 前端托管
│   │   ├── engine.py         事件驱动引擎：行情→状态→决策→风控→撮合
│   │   ├── jev_client.py     Jev Choice 客户端（重试 / 容错解析 / Mock 降级）
│   │   ├── datafeed.py       行情源：CSV 回放 / 合成行情 / Alpaca / TqSdk
│   │   ├── portfolio.py      模拟账户、撮合结算、胜率/夏普/回撤统计
│   │   ├── indicators.py     增量 RSI / MACD / 均线 / 波动率 / 盘口失衡
│   │   ├── instruments.py    美股与商品期货的乘数、最小变动价位、保证金
│   │   ├── state_builder.py  Jev State JSON 组装
│   │   ├── events.py         事件总线（SSE 广播，慢消费者 drop-oldest）
│   │   └── models.py         Tick / Position / TradeLog / StrategyConfig
│   └── tests/                55 个用例：指标、撮合、风控、API、SSE 端到端
├── frontend/                 Vue 3 + Vite + Tailwind + ECharts 大屏
├── data/                     示例历史行情 CSV（rb2610.SHFE / AAPL）
├── docker-compose.yml
└── start.sh
```

---

## 3. Jev 决策接入

### 3.1 State（引擎实际发送的输入）

```json
{
  "symbol": "rb2610.SHFE",
  "market": "cn_future",
  "timestamp": "2026-09-21T10:30:00.120Z",
  "orderbook": { "ask_price_1": 3450, "ask_vol_1": 120, "bid_price_1": 3449, "bid_vol_1": 85, "spread": 1, "last_price": 3449.5 },
  "indicators": { "rsi_14": 68.5, "macd_histogram": 2.4, "price_change_1m_pct": 0.12, "price_change_5m_pct": 0.45, "ma_gap_pct": 0.18, "volatility_pct": 0.04, "imbalance": -0.17, "samples": 812 },
  "current_position": { "side": "none", "qty": 0, "avg_price": 0, "unrealized_pnl_pct": 0.0, "holding_sec": 0.0 },
  "account": { "equity": 1000000.0, "currency": "CNY" }
}
```

### 3.2 Choice 请求 / 响应

* **Question**：`基于当前盘口和技术指标，下一步交易动作是什么？`
* **Options**：`["open_long", "open_short", "close_position", "hold"]`
* **请求**：`POST {TYPESAFE_BASE_URL}/v1/choice`，`Authorization: Bearer $TYPESAFE_API_KEY`，
  body 含 `model=jev-latest`、`primitive=choice`、`question`、`options`、`state`。
* **响应**：概率分布 + 置信度，例如

```json
{ "choice": { "probabilities": { "open_long": 0.78, "open_short": 0.08, "close_position": 0.04, "hold": 0.10 },
              "confidence": 0.85, "reasoning": "…" } }
```

解析层同时兼容 `{"probabilities": …}`、`{"options":[{"label":…,"probability":…}]}`、
`{"selected": "hold"}` 等形状；HTTP 429/5xx/超时按指数退避重试，重试耗尽后降级 Mock 并在
事件与前端标注降级原因，**热循环永不中断**。

### 3.3 信号 → 下单的判定链

1. `top_action` 的概率 ≥ `buy_threshold`（开仓）或 `close_threshold`（平仓）；
2. `confidence` ≥ `min_confidence`；
3. 风控防刷：价差上限、同标的开仓冷却、每分钟成交上限、可用资金校验、熔断状态；
4. 撮合：`IOC` 按对手价（含滑点）成交，`Maker` 在买一/卖一挂单、对手价穿越才成交、超时撤单；
5. 止盈止损与最大回撤熔断优先于模型信号，每个 Tick 都检查。

每一笔成交都会记录人类可读的触发条件，例如：

```
JEV Choice: open_long, Prob: 96.1%, Confidence: 0.82, Spread: 2, RSI: 66.9, MACD: +1.946, Src: mock
Take Profit: +1.53% ≥ 1.50% (持仓 long, 均价 3450)
风控熔断: 回撤 -10.01% 触及上限 -10.00%
```

---

## 4. API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/snapshot` | 资产、持仓、收益率、资金曲线、指标与引擎状态 |
| GET | `/api/trade_logs?limit&symbol` | 成交明细：标的、动作、价格数量、盈亏、**触发条件**、Jev 概率与置信度 |
| GET | `/api/decisions?limit` | 最近的 Jev 决策过程（输入 State + 概率分布 + 是否触发 + 拦截原因） |
| GET | `/api/config` | 当前参数 + 预置标的池 |
| GET | `/api/instruments?symbols=` | 合约乘数 / 最小变动价位 / 保证金比例 |
| POST | `/api/control/config` | 热更新参数（阈值、止盈止损、资金、标的池、模式…） |
| POST | `/api/control/action` | `start` / `pause` / `resume` / `reset` / `flatten` |
| GET | `/api/events` | **SSE**：实时推送 `decision` / `trade` / `order` / `status` / `feed` / `error` |
| GET | `/api/health`、`/docs` | 健康检查与交互式 API 文档 |

示例：

```bash
curl -s localhost:8000/api/control/config \
  -H 'Content-Type: application/json' \
  -d '{"mode":"backtest","symbols":["AAPL","rb2610.SHFE"],"buy_threshold":0.75,"take_profit_pct":1.2}'

curl -s localhost:8000/api/control/action -H 'Content-Type: application/json' -d '{"action":"start"}'
curl -N localhost:8000/api/events          # 实时看 Jev 决策流
```

---

## 5. 可调参数（控制面板 / `POST /api/control/config`）

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `mode` | `backtest` | `backtest` 历史回测 / `paper` 实时模拟盘 |
| `symbols` | `["AAPL","rb2610.SHFE"]` | 标的池，美股与商品期货可混跑 |
| `buy_threshold` / `close_threshold` | 0.70 / 0.60 | 开仓 / 平仓概率阈值 |
| `min_confidence` | 0.60 | Jev 置信度下限 |
| `take_profit_pct` / `stop_loss_pct` | 1.5 / 0.8 | 止盈 / 止损（按持仓价格变动百分比） |
| `max_spread` / `cooldown_sec` / `max_trades_per_min` | 5 / 30 / 30 | 防刷与滑点保护 |
| `max_daily_loss_pct` | 10 | 权益回撤熔断：全部平仓并禁止开仓 |
| `initial_capital` / `order_qty` / `order_type` | 100 万 / 10 / `ioc` | 资金与撮合方式（`ioc` / `maker`） |
| `commission_rate` / `slippage_ticks` | 0.0002 / 0 | 交易成本 |
| `decision_interval_ms` / `backtest_ticks` / `replay_speed` | 3000 / 3000 / 0 | 决策节流、回测长度、回放倍速（0=全速） |
| `jev_enabled` / `jev_model` | true / `jev-latest` | 关闭后强制使用本地 Mock 模型 |

> 修改 `mode` / `symbols` / `initial_capital` / `backtest_ticks` / `replay_speed` / `commission_rate`
> 会按新配置**重启引擎并清空账户**；其余参数热更新，正在运行的策略立即生效。

---

## 6. 数据源

| 市场 | 回测 | 模拟盘 |
| --- | --- | --- |
| 国内商品期货 | `data/<symbol>.csv`，否则合成行情 | TqSdk（配 `TQ_USER`/`TQ_PASSWORD` 且已安装 `tqsdk`），否则合成实时盘口 |
| 美股 | 同上 | Alpaca（配 `APCA_API_KEY_ID`/`APCA_API_SECRET_KEY`），否则合成实时盘口 |

CSV 列名大小写不敏感，支持 `ts|timestamp|datetime|date`、`bid|ask`（缺失时按最小变动价位从
`last|close|price` 合成一档盘口）、`bid_vol|ask_vol`、`volume`。仓库内已附
`data/rb2610.SHFE.csv`（2400 笔）与 `data/AAPL.csv`（1800 笔）示例数据；删除即回落到合成行情。
前端「行情源」卡片会明确标注当前用的是 `csv:` / `synthetic` / `alpaca` / `tqsdk`。

---

## 7. 前端大屏

* **绩效看板**：账户权益、累计收益率、胜率、成交笔数、盈亏比/盈利因子、夏普、最大回撤、浮动盈亏、Jev 决策次数。
* **资金曲线**：ECharts 实时刷新，带初始资金基准线，涨红跌绿。
* **Jev 实时决策流**：每次调用的盘口/指标输入、四个动作的概率柱、置信度、延迟、
  数据来源（在线 Jev / Mock 降级），以及**是否触发交易与拦截原因**（可筛选标的 / 只看触发）。
* **成交明细**：时间、标的、动作、价格数量、盈亏、触发条件徽章（Jev 信号 / 止盈 / 止损 / 强平 / 熔断），支持关键字搜索。
* **持仓 / 行情源 / 运行日志**：持仓浮盈、各标的 Tick 数与 Jev 调用数、Maker 挂单状态、事件日志。
* **控制面板抽屉**：概率阈值与置信度滑杆、止盈止损、风控、资金与撮合、决策间隔、模式切换、标的池多选与自定义代码。

数据通过 `EventSource('/api/events')` 实时推送（断线自动重连），并以 2.5s 快照轮询兜底，
保证资金曲线与持仓在任何情况下都不会长时间失真。

---

## 8. 测试

```bash
cd quant && ./start.sh test
# 或
cd quant/backend && pip install -r requirements-dev.txt && pytest -q
```

覆盖：增量指标正确性、合约参数、Mock/在线 Jev 的概率解析与降级、账户盈亏与保证金结算、
胜率/回撤/夏普统计、止盈止损、冷却与价差防刷、阈值拦截、暂停/恢复/重置/一键平仓、
配置热更新与重启、REST 全量接口、以及跑真实 uvicorn 的 SSE 端到端推送。

---

## 9. 说明与边界

* 内置 Mock 模型是**演示用动量/均线策略**，不构成任何交易建议；默认参数下的回测收益可能为负，
  这正说明系统如实统计了手续费、滑点与胜率，而不是做了美化。
* 模拟盘为本地撮合（按实时买一/卖一或挂单排队），不接任何真实交易通道；接入实盘请自行实现券商/期货
  柜台适配器并补充资金、持仓、报单状态的对账逻辑。
* TqSdk / Alpaca 适配器只在配置了对应凭证（且已安装 `tqsdk`）时启用，否则透明回落到合成行情。
