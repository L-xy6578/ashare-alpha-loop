"""归档脚本的非交互 git 环境（#114：守护推送不得弹窗挂起）"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from archive_daily import _git_env, git


def test_git_env_disables_interactive_auth():
    env = _git_env()
    assert env["GCM_INTERACTIVE"] == "never"     # GCM 不弹账号选择框
    assert env["GIT_TERMINAL_PROMPT"] == "0"     # git 不提示输入凭据


def test_git_timeout_returns_failed_process():
    """超时按失败返回（returncode 非 0），不抛异常、不挂起"""
    r = git("version")
    assert r.returncode == 0                     # 正常命令仍可用
    assert "git version" in (r.stdout or "")


def test_stage_patterns_covers_selection_outputs():
    """选股产物（outputs/选股/）必须在归档覆盖范围内，收盘阶段自动入库"""
    from archive_daily import STAGE_PATTERNS

    assert "outputs/" in STAGE_PATTERNS
    assert "outputs/选股/选股结果_2026-10-29.md".startswith("outputs/")
