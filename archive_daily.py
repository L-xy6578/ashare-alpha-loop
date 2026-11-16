"""
收盘产物自动归档 — 跑完即存档，避免"22 个提交未推送"类事故

自动暂存并提交当日产物：
    - outputs/ 目录（按类型子目录：龙虎榜简报/竞价预案/收盘报表/扫描发现/选股）
    - 根目录当日报告（*_YYYY-MM-DD.md）
    （注：知识库回写以 data/knowledge.db 为准）

用法:
    python archive_daily.py                 # 提交当日产物（不推送）
    python archive_daily.py --push          # 提交并推送 origin/master
    python archive_daily.py --date 2026-08-04 --dry-run   # 预览将提交的文件
"""

import argparse
import os
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent

STAGE_PATTERNS = [
    "outputs/",
]


def _git_cmd() -> str:
    """定位 git 可执行文件：系统 PATH → Codex 内置运行时 → 常见安装路径

    #42：守护进程由登录自启拉起，PATH 可能不含 git（归档在守护环境里
    FileNotFoundError），必须显式定位。
    """
    found = shutil.which("git")
    if found:
        return found
    # Codex 内置运行时：<runtime>/dependencies/python → ../native/git/cmd/git.exe
    py = Path(sys.executable)
    bundled = py.parent.parent / "native" / "git" / "cmd" / "git.exe"
    if bundled.exists():
        return str(bundled)
    # A2：守护可能改用 .venv/系统 python，不再等于 Codex 运行时——
    # 兜底改为扫描任意版本的 Codex 运行时（~/.cache/codex-runtimes/*/...）
    import glob as _glob

    for g in _glob.glob(
        str(Path.home() / ".cache" / "codex-runtimes" / "*" / "dependencies"
            / "native" / "git" / "cmd" / "git.exe")
    ):
        if Path(g).exists():
            return g
    for c in (Path(r"C:\Program Files\Git\cmd\git.exe"),
              Path(r"C:\Program Files (x86)\Git\cmd\git.exe")):
        if c.exists():
            return str(c)
    raise RuntimeError("找不到 git 可执行文件（请安装 Git 或配置 PATH）")


def _git_env() -> dict:
    """git 子进程环境：禁用一切交互式认证（守护无人值守，防弹窗挂起）

    GCM_INTERACTIVE=never   → Git Credential Manager 不再弹账号选择框
    GIT_TERMINAL_PROMPT=0   → git 本身不再提示输入用户名/密码
    缺凭据时直接失败（上游 run_daily 会退化为本地提交，不会把任务吊死）。
    """
    env = dict(os.environ)
    env["GCM_INTERACTIVE"] = "never"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def git(*args: str, timeout: int = 180) -> subprocess.CompletedProcess:
    """执行 git 命令（非交互 + 超时保护，防守护被弹窗/网络挂起）"""
    try:
        return subprocess.run(
            [_git_cmd(), *args],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=_git_env(),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=args, returncode=124, stdout="",
            stderr=f"git {' '.join(args)} 超时（{timeout}s），按失败处理",
        )


def remote_default_branch() -> str:
    """远程默认分支（origin/HEAD 指向，如 main/master），用于推送"""
    r = git("symbolic-ref", "refs/remotes/origin/HEAD")
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().rsplit("/", 1)[-1]
    return "master"


def main() -> None:
    parser = argparse.ArgumentParser(description="收盘产物自动归档")
    parser.add_argument("--date", default=date.today().isoformat(),
                        help="归档日期 YYYY-MM-DD（默认今天）")
    parser.add_argument("--push", action="store_true", help="提交后推送 origin/master")
    parser.add_argument("--dry-run", action="store_true", help="仅预览将提交的文件")
    args = parser.parse_args()

    patterns = list(STAGE_PATTERNS)
    # 根目录当日报告（显式文件，避免 glob 无匹配报错）
    daily_files = sorted(ROOT.glob(f"*_{args.date}.md"))
    patterns += [str(p.relative_to(ROOT)) for p in daily_files]
    if not daily_files:
        print(f"ℹ️ 根目录无 {args.date} 当日报告（*_{args.date}.md）")
    add = git("add", "--", *patterns)
    if add.returncode != 0:
        print(add.stderr.strip(), file=sys.stderr)
        sys.exit(1)

    status = git("status", "--porcelain")
    staged = [line for line in status.stdout.splitlines()
              if line.startswith(("M ", "A ", "D ", "R "))]
    if not staged:
        print(f"ℹ️ {args.date} 无待归档变更（或已提交），跳过")
        return

    print(f"待归档 {len(staged)} 个文件：")
    for line in staged:
        print(f"  {line}")

    if args.dry_run:
        print("(dry-run) 未实际提交")
        return

    msg = f"docs: 归档 {args.date} 收盘产物（{len(staged)} 个文件）"
    commit = git("commit", "-m", msg)
    if commit.returncode != 0:
        print(commit.stderr.strip(), file=sys.stderr)
        sys.exit(1)
    print(f"✅ 已提交: {commit.stdout.strip().splitlines()[-1]}")

    if args.push:
        branch = remote_default_branch()
        push = git("push", "origin", f"HEAD:{branch}")
        if push.returncode != 0:
            print(push.stderr.strip(), file=sys.stderr)
            sys.exit(1)
        print(f"✅ 已推送到 origin/{branch}")


if __name__ == "__main__":
    main()
