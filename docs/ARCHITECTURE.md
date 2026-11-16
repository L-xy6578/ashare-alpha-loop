# 架构与数据流

## 1. 总览

```text
行情源（腾讯 / 通达信 / 东财 / Akshare）
        │
        ▼
src.pipeline  ──►  data/market.db（日K、板块、质量快照、观察池）
        │
        ▼
src.factors / src.vcp / src.pool_refresh
        │
        ▼
r47_closed_loop.py
        ├──► data/knowledge.db（决策、成交、对题本、错题本、调研笔记）
        ├──► data/index_portfolio.db（指数化组合净值）
        └──► outputs/（收益报表、KPI、告警、健康检查）
```

盘前流程由 `morning_flow.py` 独立完成：

```text
东财龙虎榜 ──► outputs/龙虎榜简报/
腾讯竞价快照 ──► outputs/竞价预案/
```

`run_daily.py` 负责按交易日历编排盘前与收盘阶段，并提供单实例守护、失败重试和状态落盘。

## 2. 数据库

| 数据库 | 内容 | 写入方 |
|---|---|---|
| `data/market.db` | `daily_bars` 日 K、`sector_daily` 板块、`quality_snapshot` 质量分级、`watchlist` 观察池 | `src.pipeline`、`src.watchlist`、`src.sector_*` |
| `data/knowledge.db` | `decision_plans`、`decision_trades`、`decision_account`、`correct_book`、`mistake_book`、`research_notes`、`factor_snapshot` | `src.decision_*`、`src.knowledge`、`src.factor_store` |
| `data/index_portfolio.db` | 指数化组合账户、持仓、净值 | `src.index_portfolio` |

所有数据库都在首次运行时自动建表，路径由 `src/config.py` 统一管理。

## 3. 关键口径

- **sim / real 分账**：`positions.account` 区分模拟盘与真实盘；KPI 只统计 real。
- **复权口径**：个股默认前复权；指数无复权。不同来源的复权价不得混写同一序列。
- **因子可复算**：落库的评分必须能由 `factor_snapshot` 原始值复算；缺失记 `missing`，不得用 0 分代替。
- **决策幂等**：同一交易日同一标的的建仓/减仓/结算只执行一次；重跑先 `--dry-run`。
- **产物可追溯**：报表包含数据日期、来源、缺失标注；无法计算时写"结论无效"，不静默跳过。

## 4. 模块边界

| 模块 | 职责 | 不负责 |
|---|---|---|
| `src/data_source.py` | 统一 K 线接口与多源实现 | 业务评分、落库 |
| `src/pipeline/` | 采集、增量、质量检查、交易日历 | 选股决策 |
| `src/factors.py` | 五因子归一化与加权 | 买卖执行 |
| `src/vcp.py` | 形态识别与量能比 | 仓位管理 |
| `src/executable.py` | 可执行性门槛（价格、手数、权限） | 选股 |
| `src/portfolio.py` | 持仓状态读写 | 行情采集 |
| `src/decision_log.py` | 决策单、成交、账户现金 | 行情采集 |
| `src/decision_settle.py` | 止盈止损、加仓、到期结算 | 选股 |
| `src/reports.py` | 报表渲染 | 数据采集 |
| `src/realtrade.py` | 真实成交记账 | 券商下单 |

## 5. 运行阶段

| 时间 | 阶段 | 入口 |
|---|---|---|
| 08:55 | 龙虎榜简报 | `morning_flow.py --lhb` |
| 09:26 | 竞价终局预案 | `morning_flow.py --auction` |
| 15:35 | 收盘闭环 + 归档 | `r47_closed_loop.py`、`archive_daily.py` |

非交易日不生成当日产物。周末或节假日手动跑闭环出现"快照滞后"属于预期噪音。

## 6. 扩展点

- 新数据源：实现 `src.data_source.DataSource`，在 `daily_kline` 返回 `KlineBar` 列表。
- 新因子：在 `src.factors` 增加归一化函数，更新权重与测试，并在 `factor_snapshot` 落原始值。
- 新报表：在 `src.reports` 增加渲染函数，输出到 `outputs/` 对应子目录。
- 新策略：在 `src.strategy_loop` 增加信号函数，保持"信号 → 动作"与仓位门槛解耦。

## 7. 测试策略

- 单元测试使用 `tmp_path` 建临时 SQLite，不触碰 `data/`。
- 行情用 `StaticDataSource` 注入固定 K 线。
- 网络接口只在手工 `--dry-run` 时验证，不进入 CI。
