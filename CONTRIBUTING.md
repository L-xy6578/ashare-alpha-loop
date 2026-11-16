# 参与开发

感谢你愿意一起把 AlphaLoop 做得更好。这个项目面向 A 股量化研究，欢迎代码、文档、数据源适配、测试和 issue 分类等各类贡献。

## 可以先做什么

- 带 `good first issue` 标签的任务：适合第一次参与，范围清晰。
- 带 `help wanted` 标签的任务：欢迎有经验的贡献者认领。
- 文档改进：安装步骤、命令示例、报错排查、英文 README。
- 测试补充：边界条件、数据缺失、异常路径。
- 数据源适配：新的公开行情/资讯接口，注意遵守数据方条款。

## 开发环境

```bash
git clone https://github.com/L-xy6578/ashare-alpha-loop.git
cd ashare-alpha-loop

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -e ".[dev]"
python -m pytest -q
```

## 提交 Pull Request

1. 先从 `main` 建分支：`feat/xxx`、`fix/xxx`、`docs/xxx`。
2. 一个 PR 只解决一个问题，避免混入无关重构。
3. 行为变更补测试；修 bug 优先修根因。
4. 跑通 `python -m pytest -q`。
5. 在 PR 描述里写清：改了什么、为什么改、怎么验证。

## 代码约定

- Python 3.11+，优先使用标准库。
- 路径、阈值、账户参数放 `src/config.py`，不要散落在业务代码里。
- 数据库读写统一走 `src/db.py`，不要在每个模块里自己连 SQLite。
- 新增数据表/字段时，同时更新建表、迁移和测试。
- 对外输出（报表、CLI）保持中文，变量名和函数名使用英文。

## 数据与隐私

提交前请确认：

- 没有提交 `data/`、`logs/`、`outputs/` 下的运行产物；
- 没有提交真实成交记录、账户规模、券商信息、token 或 `.env`；
- 示例数据明确标注为虚构；
- 新增数据源符合提供方的服务条款，不做高频抓取。

## Issue 与讨论

- Bug 请用 Bug report 模板，附复现步骤和报错。
- 新功能请用 Feature request 模板，说明使用场景。
- 使用问题可以开 Discussion。
- 安全问题不要公开开 Issue，见 [SECURITY.md](SECURITY.md)。

## 提交信息

使用 `feat:`、`fix:`、`docs:`、`refactor:`、`test:`、`chore:` 前缀。
标题写结论，正文写关键取舍和验证方式。

## 许可证

贡献代码即表示同意以 AGPL-3.0-or-later 发布。
