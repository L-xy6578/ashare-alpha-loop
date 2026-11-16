# AlphaLoop · A股量化研究框架

[![tests](https://github.com/L-xy6578/ashare-alpha-loop/actions/workflows/tests.yml/badge.svg)](https://github.com/L-xy6578/ashare-alpha-loop/actions/workflows/tests.yml)
[![license: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

> 一个面向 A 股的量化研究与纸面交易框架：行情采集、因子评分、观察池、模拟持仓、决策留痕、复盘报表。
>
> 设计原则：**不预测行情，只应对行情；先测量，再谈提升。**

**English:** AlphaLoop is a local-first A-share quantitative research and paper-trading framework. It covers market data collection, factor scoring, watchlists, simulated positions, decision logs and review reports. It does not place real orders and is not investment advice.

## 这是什么

- 一套可本地运行的 A 股研究基础设施：SQLite 行情库 + 知识库 + 观察池 + 模拟账户。
- 一套"调查员 → 交易员 → 评审员"的工作流骨架，方便把研究结论沉淀为可复盘的记录。
- 一套纯 Python 标准库实现的工程底座，不依赖数据库服务、消息队列或前端。

## 这不是什么

- **不是自动下单程序**。项目不连接券商交易接口，不会替你买卖股票。
- **不是投资建议**。所有评分、信号、报表仅用于研究与学习。
- **不保证收益**。历史回测和模拟结果不代表未来表现。

## 主要能力

| 模块 | 能力 |
|---|---|
| 行情管道 | 日 K 线增量采集、幂等落库、质量分级（A-D）、数据源可替换 |
| 因子引擎 | 行业景气 / 业绩趋势 / 估值 / 技术面 / 资金面五因子，缺失即不推荐 |
| 观察池 | SQLite 单一数据源，支持入池、移出、软删除、持仓保护、批量换池 |
| 模拟交易 | 仓位上限、止盈止损、分批减仓、费用计算、T+1、决策留痕 |
| 指数化组合 | 用 ETF 做长期配置记账，输出净值、回撤、相对基准表现 |
| 报表 | 收益报表、KPI 评估、健康检查、告警、龙虎榜简报、竞价预案 |
| 知识库 | 对题本 / 错题本 / 调研笔记 / 交叉验证，区分 sim 与 real 口径 |

## 项目结构

```text
.
├── src/
│   ├── pipeline/            # 行情采集、质量检查、交易日历
│   ├── knowledge/           # SQLite 知识库
│   ├── data_source.py       # 数据源接口：Static / Tencent / TDX / Akshare
│   ├── factors.py           # 五因子评分
│   ├── vcp.py               # VCP 形态识别
│   ├── watchlist.py         # 观察池
│   ├── pool_refresh.py      # 观察池换血
│   ├── portfolio.py         # 持仓状态（sim / real 分账）
│   ├── decision_log.py      # 决策单与成交记录
│   ├── decision_settle.py   # 决策单结算
│   ├── index_portfolio.py   # 指数化组合
│   ├── reports.py           # 收益报表
│   ├── kpi.py               # KPI 评估
│   ├── realtrade.py         # 真实成交录入（仅记账，不下单）
│   └── tests/               # pytest 测试
├── r47_closed_loop.py       # 收盘闭环：行情 → 评分 → 决策 → 结算 → 报表
├── morning_flow.py          # 盘前：龙虎榜简报 + 9:25 竞价预案
├── run_daily.py             # 自动任务编排与守护
├── archive_daily.py         # 产物归档（可选 git commit/push）
├── health_check.py          # 数据与运行健康检查
├── regression_check.py      # 一键回归
├── data/                    # 本地数据库（gitignore）
├── outputs/                 # 运行产物（gitignore，保留示例）
├── docs/ARCHITECTURE.md     # 架构与数据流
└── pyproject.toml
```

## 快速开始

要求 Python 3.11+。项目运行本身只依赖标准库，测试依赖 pytest。

```bash
git clone https://github.com/L-xy6578/ashare-alpha-loop.git
cd ashare-alpha-loop

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -e ".[dev]"
python -m pytest
```

首次运行会在 `data/` 下自动创建 SQLite 数据库，在 `outputs/` 下生成报表。仓库不自带行情数据。

### 采集行情

```bash
python -m src.pipeline --symbols sh600000,sz000001 --start 2026-01-01
```

### 跑一次收盘闭环（演练不写生产库）

```bash
python r47_closed_loop.py --dry-run
python r47_closed_loop.py
```

### 盘前流程

```bash
python morning_flow.py --lhb --dry-run
python morning_flow.py --auction --dry-run
```

### 自动任务

```bash
python run_daily.py --dry-run
python run_daily.py --watch
```

Windows 下可安装计划任务：

```powershell
.\scripts\install_daily_task.ps1
```

### 观察池

```bash
python -m src.watchlist --list
python -m src.watchlist --add sh600000 600000 示例银行 --sector 银行 --tag 示例
python -m src.watchlist --remove sh600000 --reason 示例
```

### 真实成交记账

`realtrade` 只把真实成交写入本地知识库，用于把真实盘与模拟盘分开复盘；它不会发送任何委托。

```bash
python -m src.realtrade buy  sh600000 600000 示例银行 10.00 200 2026-01-05 "示例买入"
python -m src.realtrade sell sh600000 9.20 200 2026-02-05 "示例卖出" --round RREAL
python -m src.realtrade list
python -m src.realtrade stop-alert
```

## 配置

路径和数据库位置集中在 `src/config.py`。真实账户风控参数可用环境变量覆盖：

```bash
# Windows PowerShell
$env:REAL_TOTAL_ASSETS = "100000"
$env:REAL_MAX_POSITION_PCT = "20"

# macOS / Linux
export REAL_TOTAL_ASSETS=100000
export REAL_MAX_POSITION_PCT=20
```

可参考 `.env.example`。项目不会自动读取 `.env`，请用 shell、IDE 或系统环境变量注入。

## 数据源

- **腾讯行情 HTTP**：日 K 线主源，零依赖。
- **通达信（pytdx，可选）**：腾讯失败或当日 K 线缺失时串行兜底；`pip install -e ".[tdx]"`。
- **东方财富公开接口**：板块、资金流、业绩、估值等资讯类数据。
- **Akshare（可选）**：独立环境备用实现；`pip install -e ".[akshare]"`。

这些接口多为公开但非官方承诺的接口，可能限流、改版或失效。使用前请自行确认并遵守数据提供方的服务条款，不要高频抓取或二次分发数据。

## 产物与示例

运行产物默认写入 `outputs/`，该目录已被 gitignore。仓库只保留脱敏后的格式示例：

```text
outputs/README.md
outputs/示例/收益报表_示例.md
outputs/示例/决策日志_示例.md
outputs/示例/健康检查_示例.md
```

示例中的标的、价格、盈亏均为虚构数据。

## 测试

```bash
python -m pytest -q
```

测试使用临时 SQLite 数据库和静态行情夹具，不依赖网络。需要联网的采集逻辑请用 `--dry-run` 手工验证。

## 贡献

欢迎一起开发。参与方式见 [CONTRIBUTING.md](CONTRIBUTING.md)，行为规范见 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。

适合第一次参与的方向：

- 补英文 README 或使用教程
- 增加券商成交单 CSV 导入
- 增加新的数据源适配器
- 改进报表模板与示例

带 `good first issue` 或 `help wanted` 标签的 Issue 可以直接认领。

提交前请确认：

1. `python -m pytest` 通过；
2. 不提交 `data/`、`logs/`、`outputs/` 中的运行产物；
3. 不提交任何 token、账号、真实成交记录或个人信息；
4. 行为变更同步更新 `README.md` 或 `docs/ARCHITECTURE.md`。

## 免责声明

本项目仅用于量化研究、软件工程学习与模拟盘验证，不构成任何投资建议或收益承诺。股市有风险，投资需谨慎。使用者应自行判断数据的准确性、完整性与合规性，并自行承担使用后果。

## 许可证

[GNU Affero General Public License v3.0 or later](LICENSE)（AGPL-3.0-or-later）。

如果你修改本项目并通过网络提供服务，需要按 AGPL 的要求向用户提供对应源代码。
