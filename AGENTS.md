# AGENTS.md — 项目协作约定

本文件面向在本仓库中工作的 AI 编程助手与人类贡献者。目标：改得少、改得准、可验证。

## 1. 先读什么

- `README.md`：项目定位、命令、数据源、免责声明。
- `docs/ARCHITECTURE.md`：模块边界与数据流。
- `pyproject.toml`：Python 版本、测试配置、可选依赖。

## 2. 目录与职责

- `src/`：引擎代码。新增能力优先放进已有模块，避免在根目录堆脚本。
- `src/tests/`：pytest 测试。行为变更必须带测试。
- 根目录脚本：`r47_closed_loop.py`（收盘闭环）、`morning_flow.py`（盘前）、`run_daily.py`（守护）、`archive_daily.py`（归档）。
- `data/`、`logs/`、`outputs/`：运行时数据，**不入库**。`outputs/示例/` 除外。
- `.venv/`：本地虚拟环境，**不入库**。

## 3. 常用命令

```bash
python -m pytest -q
python r47_closed_loop.py --dry-run
python morning_flow.py --lhb --dry-run
python run_daily.py --dry-run
python -m src.pipeline --symbols sh600000 --start 2026-01-01
```

## 4. 修改纪律

- 改代码前先看 `git status` 和 `git log --oneline -5`，不要覆盖别人的未提交改动。
- 一次只解决一个明确问题；不要顺手重构无关模块。
- 修复 bug 时优先修根因，避免在调用方加特判。
- 新增配置项放到 `src/config.py`，路径与阈值不要散落在业务代码里。
- 涉及资金、仓位、止损止盈的改动，必须在测试里写清边界条件。

## 5. 数据与隐私

- 不提交真实成交记录、账户规模、盈亏报表、券商信息或个人信息。
- 不提交 `data/*.db`、`logs/`、`outputs/` 下的运行产物。
- 不提交 token、密钥、`.env`、本机绝对路径。
- 示例数据必须标注为虚构，并放在 `outputs/示例/` 或测试夹具中。

## 6. 测试要求

- 提交前跑 `python -m pytest -q`。
- 测试必须可离线运行；网络相关逻辑用注入的数据源或 `--dry-run` 验证。
- 新增数据表/字段时，同时更新建表语句、迁移逻辑和测试。

## 7. 提交信息

使用前缀：`feat:`、`fix:`、`docs:`、`refactor:`、`test:`、`chore:`。
提交信息写"为什么"，正文写关键取舍与验证方式。

## 8. 许可证

本项目使用 AGPL-3.0-or-later。贡献代码即表示同意以同一许可证发布。
