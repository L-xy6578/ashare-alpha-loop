# -*- coding: utf-8 -*-
"""
自动任务编排器 —— "电脑开着就自动跑"（纯本地，无外部依赖）

一天三个阶段（交易日）：
    08:55  盘前龙虎榜简报   morning_flow.py --lhb      （前一交易日盘后静态数据）
    09:26  竞价终局预案     morning_flow.py --auction   （9:25 集合竞价终局价）
    15:35  收盘闭环+归档    r47_closed_loop.py + archive_daily.py

模式一（一次性）：
    python run_daily.py                 # 收盘闭环 + 归档推送
    python run_daily.py --dry-run       # 演练：r47 --dry-run + 归档预览（不提交）
    python run_daily.py --no-push       # 闭环 + 提交，不推送

模式二（后台守护，免管理员）：
    python run_daily.py --watch             # 常驻：交易日按阶段自动跑（lhb/auction/close）
    python run_daily.py --watch --once      # 守护只检查一轮（测试用）
    python run_daily.py --install-startup   # 写入开机自启（登录后自动启动守护）
    python run_daily.py --uninstall-startup # 移除开机自启

说明：计划任务（Task Scheduler）需要管理员权限，本环境可能无法直接注册；
    推荐用 scripts/install_daily_task.ps1（管理员运行，登录自启、不依赖 VBS）；
    退化方案仍是"登录自启 VBS + 后台守护"——只要电脑开着且已登录，到点即自动运行。
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# 可移植：pythonw（无控制台）运行时 stdout/stderr 为 None，print 会崩 → 重定向到 DEVNULL
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

ROOT = Path(__file__).resolve().parent
TASK_NAME = "TriangleLoopDaily"
VBS_NAME = "TriangleLoopDaily.vbs"
STARTUP_DIR = (
    Path(os.environ.get("APPDATA", ""))
    / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
)
STATE_FILE = ROOT / "logs" / "daily_runner_state.txt"
WATCH_LOG = ROOT / "logs" / "daily_runner.log"
PID_FILE = ROOT / "logs" / "run_daily.pid"   # 单实例锁（#60）
HEARTBEAT_FILE = ROOT / "logs" / "run_daily_heartbeat.json"   # 心跳：最后运行时间/来源

# 状态文件的终态（旧读取方只认 OK/FAIL；DEGRADED / LATE 为新终态，字段格式不变保持兼容）
# LATE：阶段已过硬时点、不再补做（2026-11-09 竞价预案 13:28 才成功，不能算 OK）
TERMINAL_STATUS = ("OK", "FAIL", "DEGRADED", "LATE")


def _pid_alive(pid: int) -> bool:
    """Windows：进程是否存在（零依赖，ctypes）

    #2026-11-09 双守护根因：计划任务以其他账户/提权运行，本进程
    OpenProcess 被拒（ERROR_ACCESS_DENIED），旧实现把它当成"进程已退出"，
    于是删掉 PID 文件、后启动的实例覆盖锁 → 两个守护同时跑。
    这里把"拒绝访问"判定为存活（打不开 ≠ 不存在）。
    """
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if h:
        kernel32.CloseHandle(h)
        return True
    return ctypes.get_last_error() == 5   # ERROR_ACCESS_DENIED：进程存在，只是无权查询


def _acquire_single_instance() -> int | None:
    """单实例锁（#60）：已有存活守护返回其 PID；本进程取得锁返回 None。

    防多守护并发：任何来源（VBS/手动/对话）重复启动时直接退出，
    避免两个守护同时触发同一阶段（如并发收盘 → 数据库争用）。
    """
    if PID_FILE.exists():
        try:
            old = int(PID_FILE.read_text(encoding="ascii").strip())
        except (ValueError, OSError):
            old = None
        if old and _pid_alive(old):
            return old
        PID_FILE.unlink(missing_ok=True)   # 陈旧锁（进程已退出）
    PID_FILE.parent.mkdir(exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    return None


def _startup_provenance(source: str = "") -> str:
    """记录守护启动来源（显式 --source 优先，其次父进程名+PID，失败返回 manual）

    #2026-11-06：父进程查询在 VBS/计划任务拉起的场景下常常返回 unknown（父进程
    已退出）。改为启动方显式传入 --source（task-scheduler / startup-vbs / manual），
    查不到父进程时不再写 unknown，而是退回 manual，便于判断是否断档。
    """
    if source:
        return source
    ps = (
        '$p = Get-CimInstance Win32_Process -Filter "ProcessId=' + str(os.getpid()) + '"; '
        "$pp = Get-CimInstance Win32_Process -Filter \"ProcessId=$($p.ParentProcessId)\"; "
        'if ($pp) { Write-Output ($pp.Name + "|" + $pp.ProcessId) } else { Write-Output "manual" }'
    )
    for pwsh in (r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
                 "powershell.exe"):
        try:
            r = subprocess.run([pwsh, "-NoProfile", "-Command", ps],
                               capture_output=True, text=True, timeout=5,
                               errors="replace")
            out = r.stdout.strip()
            return out if out else "manual"
        except Exception:
            continue
    return "manual"


def _write_heartbeat(source: str, *, status: str = "running",
                     phase: str = "", extra: dict | None = None,
                     path: Path = HEARTBEAT_FILE) -> None:
    """写心跳文件（最后运行时间 + 来源 + 当前阶段），失败不阻断主流程

    文件为 JSON（logs/run_daily_heartbeat.json）：
        {"last_run": "...", "source": "...", "pid": ..., "status": "...",
         "phase": "...", "schedule": "...", ...}
    仅记录当前状态，不参与幂等判定；健康检查/人工排查据此判断是否断档。
    """
    try:
        data = {
            "last_run": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": source,
            "pid": os.getpid(),
            "status": status,
            "phase": phase,
        }
        if extra:
            data.update(extra)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:
        try:
            _log_watch(f"心跳写入失败: {e}")
        except Exception:
            pass

# 三阶段：名称 -> (默认触发时间, 运行函数)。close 的触发时间可用 --schedule 覆盖。
PHASES = [
    ("sector", "08:50", lambda: _run("-m", "src.sector_feed", "--inject")),
    ("lhb", "08:55", lambda: _run("morning_flow.py", "--lhb")),
    ("auction", "09:26", lambda: _run("morning_flow.py", "--auction")),
    ("close", "15:35", lambda: _run_daily_flow()),
]

# 阶段硬时点（2026-11-09）：过了时点的产物不再是"当时的打算"，一律记 LATE 不补做。
# auction 依赖 9:25 集合竞价终局价，9:30 开盘后再补做只能拿盘中数据冒充竞价预案。
PHASE_DEADLINE = {"auction": "09:30"}


def _deadline_passed(phase: str, now: datetime) -> bool:
    """该阶段是否已过硬时点（用于拒绝"过点补做"）"""
    deadline = PHASE_DEADLINE.get(phase)
    return bool(deadline) and now.strftime("%H:%M") >= deadline

# 每阶段最大尝试次数（#116/#117：早盘 DNS/网络常间歇不可用 30~60 分钟，
# 盘前两阶段给更长重试窗口（08:50 起 11 次×10 分钟 ≈ 覆盖到 10:30）；
# auction/close 依赖时点，保持 3 次）
PHASE_MAX_ATTEMPTS = {
    "sector": 11,   # 兼容旧逻辑；sector 实际按 SECTOR_RETRY_SLOTS 的时段重试
    "lhb": 11,
    "auction": 3,
    "close": 3,
}

# sector 同日多时段重试（2026-11-07）：
#   08:50 固定首发；随后 09:10/09:50/10:30 覆盖早盘 DNS/网络恢复窗口（#116 的 30~60 分钟
#   故障常见于 08:50~09:30）；11:20/13:05/14:05 兜底午后网络恢复；
#   15:10 是收盘（15:35）前最后时点，失败后由收盘闭环的“当日快照缺失自动补拉”接管。
#   间隔逐步拉长（20→30→40→50→105→60→65 分钟），避免对东财高频重试。
SECTOR_RETRY_SLOTS = ("08:50", "09:10", "09:50", "10:30",
                      "11:20", "13:05", "14:05", "15:10")

# 失败原因分类关键字（先网络/DNS，再数据不完整，避免把网络异常误判成数据问题）
_NET_HINTS = (
    "nameresolutionerror", "getaddrinfo failed", "temporary failure in name resolution",
    "nodename nor servname", "remote disconnected", "connection reset",
    "connectionerror", "max retries exceeded", "newconnectionerror",
    "read timed out", "connecttimeout", "timed out", "超时",
    "winerror 10054", "winerror 10060", "11001",
)
_DATA_HINTS = (
    "不落库", "快照不完整", "条数不足", "缺口过大", "板块列表不可用",
    "资金字段全 null", "资金字段全 0",
)


def _classify_failure(rc: int, stdout: str, stderr: str) -> tuple[str, str]:
    """把阶段失败归类为 (状态码, 中文原因)

    状态码写入状态文件（ASCII）：network=网络不可达/DNS 失败，incomplete=数据源
    返回不完整，unknown=其他。原因只在日志/通知里展示，便于区分故障类型。
    """
    text = f"{stdout or ''}\n{stderr or ''}".lower()
    for hint in _NET_HINTS:
        if hint in text:
            return "network", f"网络不可达/DNS 失败（命中 {hint}）"
    for hint in _DATA_HINTS:
        if hint in text:
            return "incomplete", f"数据源返回不完整（命中 {hint}）"
    if rc == 124:
        return "network", "运行超时（600s）"
    return "unknown", f"未知错误（exit={rc}）"


def _run_capture(*args: str) -> tuple[int, str, str]:
    """运行脚本并返回 (退出码, stdout, stderr)；失败时落盘 err.log（#5）"""
    try:
        proc = subprocess.run(
            [sys.executable, *args], cwd=str(ROOT),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=600,   # #117：单阶段最长 10 分钟，防单个阶段拖住后续阶段
        )
    except subprocess.TimeoutExpired:
        WATCH_LOG.parent.mkdir(exist_ok=True)
        with (ROOT / "logs" / "daily_runner.err.log").open("a", encoding="utf-8") as f:
            f.write(f"\n===== {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                    f"CMD: {' '.join(args)} 超时（600s） =====\n")
        return 124, "", "TimeoutExpired"
    if proc.returncode != 0:
        WATCH_LOG.parent.mkdir(exist_ok=True)
        with (ROOT / "logs" / "daily_runner.err.log").open("a", encoding="utf-8") as f:
            f.write(f"\n===== {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                    f"CMD: {' '.join(args)} exit={proc.returncode} =====\n")
            if proc.stdout:
                f.write("-- stdout --\n" + proc.stdout[-4000:] + "\n")
            if proc.stderr:
                f.write("-- stderr --\n" + proc.stderr[-4000:] + "\n")
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _run(*args: str) -> int:
    """用当前 Python 解释器运行脚本（保留原行为：只回显 stdout 尾部）"""
    rc, stdout, _ = _run_capture(*args)
    if stdout:
        print(stdout[-4000:], end="")
    return rc


def _run_sector() -> tuple[int, str, str]:
    """运行 sector 采集并返回 (退出码, 原因码, 原因说明)"""
    rc, stdout, stderr = _run_capture("-m", "src.sector_feed", "--inject")
    if stdout:
        print(stdout[-4000:], end="")
    code, reason = _classify_failure(rc, stdout, stderr)
    return rc, code, reason


def _log_watch(msg: str) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    WATCH_LOG.parent.mkdir(exist_ok=True)
    with WATCH_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


def _notify(title: str, msg: str) -> None:
    """Windows 桌面通知（零配置，登录会话托盘弹窗；失败不阻塞守护）

    title 用 ASCII（避免控制台/编码差异导致乱码），正文用中文（powershell 参数走
    Unicode API，实测正常）。
    """
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "Add-Type -AssemblyName System.Drawing;"
        "$n=New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon=[System.Drawing.SystemIcons]::Warning;"
        f"$n.BalloonTipTitle='{title}';"
        f"$n.BalloonTipText='{msg}';"
        "$n.Visible=$true;"
        "$n.ShowBalloonTip(8000);"
        "Start-Sleep -Seconds 6;"
        "$n.Dispose()"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
            capture_output=True, timeout=15,
        )
    except Exception as e:
        _log_watch(f"桌面通知失败: {e}")


def _run_daily_flow(dry_run: bool = False, no_push: bool = False,
                    backups: bool = True) -> int:
    """一次完整收盘流程：r47 闭环 → 报表/KPI → 健康检查 → 每日硬检查 → 归档 → 数据库备份

    返回码：0=完成；2=完成但推送失败（已本地提交归档）；
            3=完成但健康检查/每日硬检查有 FAIL 或自身崩溃（DEGRADED，状态文件写 DEGRADED）；
            其他=失败
    """
    if dry_run:
        rc = _run("r47_closed_loop.py", "--dry-run")
        if rc != 0:
            return rc
        return _run("archive_daily.py", "--dry-run")
    rc = _run("r47_closed_loop.py")
    if rc != 0:
        return rc
    rc = _run("-m", "src.reports")
    if rc != 0:
        return rc
    rc = _run("-m", "src.kpi", "--eval", "--report")
    if rc != 0:
        return rc
    # 收盘健康检查（成熟方案：结果入产物，FAIL 才通知）
    day_str = datetime.now().date().isoformat()
    hc_out = ROOT / "outputs" / "收盘报表" / f"健康检查_{day_str}.md"
    rc_hc = _run("health_check.py", "--in-close", "--out", str(hc_out))
    # 退出码语义：0=全过 / 1=有 WARN / 2=有 FAIL / 3=自身崩溃；超时(124)等其它码同为非预期。
    # 只有 0 和 1 表示"检查确实跑完了"，2 与一切非预期码都必须 degraded（崩溃不得 fail-open）。
    degraded = False
    if rc_hc == 0:
        pass
    elif rc_hc == 1:
        _log_watch(f"{day_str} 收盘健康检查有 WARN（见 健康检查_{day_str}.md）")
    elif rc_hc == 2:
        degraded = True
        _notify("TriLoop HEALTHCHECK FAIL",
                f"{day_str} 收盘健康检查存在 FAIL，close 记为 DEGRADED，"
                f"详见 outputs/收盘报表/健康检查_{day_str}.md")
    else:
        degraded = True
        _notify("TriLoop HEALTHCHECK CRASH",
                f"{day_str} 收盘健康检查自身崩溃（exit={rc_hc}），结论不可信，"
                f"close 记为 DEGRADED；报告可能缺失，详见 logs/daily_runner.err.log")

    # 每日硬检查（三条硬指标统一收口：行情=当日 / 板块快照完整=当日 / 决策留痕）
    # 放在健康检查之后、归档之前：日报随归档进 git，且不改变既有退出码语义（复用 3=DEGRADED）
    audit_out = ROOT / "outputs" / "收盘报表" / f"硬检查_{day_str}.md"
    rc_audit = _run("-m", "src.daily_audit", "--date", day_str, "--out", str(audit_out))
    # 0=通过 / 2=不满足项；2 以外的一切非 0（3=自身崩溃、124=超时、未知）同样判 degraded
    if rc_audit == 0:
        pass
    elif rc_audit == 2:
        degraded = True
        _notify("TriLoop DAILY AUDIT DEGRADED",
                f"{day_str} 每日硬检查有 FAIL（行情/板块快照/决策留痕），"
                f"close 记为 DEGRADED，详见 outputs/收盘报表/硬检查_{day_str}.md")
    else:
        degraded = True
        _notify("TriLoop DAILY AUDIT CRASH",
                f"{day_str} 每日硬检查自身崩溃（exit={rc_audit}），结论不可信，"
                f"close 记为 DEGRADED；日报可能缺失，详见 logs/daily_runner.err.log")

    # 归档（放在健康检查之后：健康检查报告随归档一起提交）
    push_failed = False
    if no_push:
        rc = _run("archive_daily.py")
        if rc != 0:
            return rc
    else:
        rc = _run("archive_daily.py", "--push")
        if rc != 0:
            # 推送失败（如 GitHub 网络不可达）：本地提交不受影响，退化为仅提交，不阻断当日闭环
            print("⚠ 推送失败（网络不可达？），已退化：本地提交归档照常，稍后可手动 git push")
            rc2 = _run("archive_daily.py")
            if rc2 != 0:
                return rc2
            push_failed = True

    # 数据库备份（归档之后；失败只告警，不阻断当日闭环）
    if backups:
        rcb = _run("-m", "src.backup_db")
        if rcb != 0:
            _log_watch(f"{day_str} 数据库备份失败（不阻断收盘流程），见 logs/app.log")
            _notify("TriLoop BACKUP FAILED",
                    f"{day_str} 数据库备份失败，详见 logs/app.log；收盘主流程已照常完成")

    if degraded:
        return 3
    return 2 if push_failed else 0


def _is_trading_day(day_str: str) -> bool:
    """交易日判定：优先用 market.db trade_calendar，缺表回退工作日判断"""
    from src.config import MARKET_DB_PATH

    try:
        with sqlite3.connect(MARKET_DB_PATH, timeout=5) as conn:
            row = conn.execute(
                "SELECT is_trading FROM trade_calendar WHERE date=?", (day_str,)
            ).fetchone()
        if row is not None:
            return row[0] == 1
    except Exception:
        pass
    return datetime.fromisoformat(day_str).weekday() < 5


def _sector_snapshot_ok(day_str: str, market_db: str | None = None) -> bool:
    """库内当日板块快照是否可用（sector_snapshot_meta.complete=1）"""
    if market_db is None:
        from src.config import MARKET_DB_PATH
        market_db = MARKET_DB_PATH
    try:
        with sqlite3.connect(market_db, timeout=5) as conn:
            row = conn.execute(
                "SELECT complete FROM sector_snapshot_meta WHERE date=?", (day_str,)
            ).fetchone()
        return bool(row and row[0])
    except Exception:
        return False


def _settle_sector_after_close(day_str: str) -> None:
    """收盘后收敛 sector 状态：快照已由闭环补拉则记 OK，否则记 FAIL

    避免 sector 停留在 RETRY（非终态）导致守护一直 pending。
    依据是库内 sector_snapshot_meta（收盘闭环的“当日快照缺失自动补拉”会写入），
    而不是猜测。
    """
    if _read_state().get((day_str, "sector")) == "OK":
        return
    if _sector_snapshot_ok(day_str):
        _mark_ran(day_str, "sector", ok=True)
        _log_watch(f"{day_str} sector 由收盘闭环补拉成功（快照 complete=1）✅")
    else:
        _mark_ran(day_str, "sector", ok=False)
        _log_watch(f"{day_str} sector 当日最终缺失（收盘闭环补拉未成功），记 FAIL")


def _last_trading_day(day_str: str, market_db: str | None = None) -> str:
    """``day_str`` 当日或之前最近的交易日（trade_calendar 口径，缺表回退工作日）"""
    if market_db is None:
        from src.config import MARKET_DB_PATH
        market_db = MARKET_DB_PATH
    try:
        with sqlite3.connect(market_db, timeout=5) as conn:
            row = conn.execute(
                "SELECT MAX(date) FROM trade_calendar WHERE is_trading=1 AND date<=?",
                (day_str,),
            ).fetchone()
        if row and row[0]:
            return row[0]
    except Exception:
        pass
    d = datetime.fromisoformat(day_str).date()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()


def _read_state(path: Path | None = None) -> dict[tuple[str, str], str]:
    """读状态文件 → {(日期, 阶段): 状态}；同一日期同阶段取最后一条"""
    path = path or STATE_FILE
    out: dict[tuple[str, str], str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1] in {p[0] for p in PHASES}:
            out[(parts[0], parts[1])] = parts[2]
        elif len(parts) == 2 and parts[1] in TERMINAL_STATUS:
            out[(parts[0], "close")] = parts[1]   # 兼容旧格式 "YYYY-MM-DD OK"
    return out


def _missing_phases(day_str: str, state: dict[tuple[str, str], str] | None = None) -> list[str]:
    """该交易日未达终态（OK/FAIL/DEGRADED）的阶段列表"""
    state = state if state is not None else _read_state()
    return [phase for phase, _, _ in PHASES
            if state.get((day_str, phase)) not in TERMINAL_STATUS]


def _recent_trading_days(day_str: str, n: int = 5,
                         market_db: str | None = None) -> list[str]:
    """``day_str``（含）往前最近 n 个交易日，倒序"""
    if market_db is None:
        from src.config import MARKET_DB_PATH
        market_db = MARKET_DB_PATH
    try:
        with sqlite3.connect(market_db, timeout=5) as conn:
            rows = conn.execute(
                "SELECT date FROM trade_calendar WHERE is_trading=1 AND date<=? "
                "ORDER BY date DESC LIMIT ?", (day_str, n),
            ).fetchall()
        if rows:
            return [r[0] for r in rows]
    except Exception:
        pass
    out: list[str] = []
    d = datetime.fromisoformat(day_str).date()
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return out


def _recent_gaps(day_str: str, n: int = 5,
                 market_db: str | None = None,
                 state: dict[tuple[str, str], str] | None = None) -> list[dict]:
    """最近 n 个交易日的阶段缺口（倒序）：[{"date": ..., "phases": [...]}, ...]

    为什么不只看"上一交易日"：11-04 缺收盘，若只检查上一交易日（11-05 完整），
    重启时永远发现不了 11-04。窗口取 5 个交易日，兼顾可发现性与噪音。
    """
    state = state if state is not None else _read_state()
    gaps = []
    for d in _recent_trading_days(day_str, n, market_db):
        missing = _missing_phases(d, state)
        if missing:
            gaps.append({"date": d, "phases": missing})
    return gaps


def _backfill_plan(now: datetime, close_time: str = "15:35",
                   state: dict[tuple[str, str], str] | None = None,
                   trading_day: bool | None = None,
                   market_db: str | None = None) -> list[str]:
    """启动时补跑计划（时间窗规则）：

    - 只补 close（盘前 sector/lhb/auction 都是时点性产物，过期数据补跑无意义）；
    - 仅当当日为交易日、且当前时间已过收盘时间（默认 15:35）才允许补；
    - 当日 close 已达终态则不重复补跑；
    - 补跑失败由调用方记 FAIL 并告警，不再无限重试。
    """
    today = now.date().isoformat()
    if trading_day is None:
        trading_day = _is_trading_day(today)
    if not trading_day:
        return []
    if now.strftime("%H:%M") < close_time:
        return []
    state = state if state is not None else _read_state()
    return [] if state.get((today, "close")) in TERMINAL_STATUS else ["close"]


def _finished(day_str: str, phase: str) -> bool:
    """当天该阶段是否已结束（OK / FAIL / DEGRADED）"""
    return _read_state().get((day_str, phase)) in TERMINAL_STATUS


def _finished_today(day_str: str) -> bool:
    """兼容旧调用：当天收盘阶段是否已结束"""
    return _finished(day_str, "close")


def _mark_ran(day_str: str, phase: str, ok: bool | str) -> None:
    """写入阶段终态；ok 传 bool（兼容旧调用）或状态字符串（OK/FAIL/DEGRADED）"""
    status = ok if isinstance(ok, str) else ("OK" if ok else "FAIL")
    STATE_FILE.parent.mkdir(exist_ok=True)
    with STATE_FILE.open("a", encoding="ascii") as f:
        f.write(f"{day_str} {phase} {status}\n")


def _mark_retry(day_str: str, phase: str, attempt: int, reason: str) -> None:
    """写入一次非终态重试记录：``date phase RETRYn/reason``（ASCII，不破坏旧读取方）

    reason: network / incomplete / unknown。非 TERMINAL_STATUS，视为仍在等待/重试；
    成功后追加 OK，按“同日同阶段取最后一条”的规则覆盖展示为 OK。
    已成功/已终态的日期不再写重试记录，防止后续重试把 OK 覆盖成 RETRY。
    """
    if _read_state().get((day_str, phase)) in TERMINAL_STATUS:
        return
    STATE_FILE.parent.mkdir(exist_ok=True)
    with STATE_FILE.open("a", encoding="ascii") as f:
        f.write(f"{day_str} {phase} RETRY{attempt}/{reason}\n")


def watch(schedule: str = "15:35", max_attempts: int = 3,
          retry_minutes: int = 10, once: bool = False,
          source: str = "") -> int:
    """后台守护：交易日按阶段（lhb 08:55 / auction 09:26 / close schedule）自动运行

    - 交易日判定走 trade_calendar（节假日不跑）
    - 幂等：当天某阶段已结束（OK/FAIL/DEGRADED）则跳过（重启守护不重跑）
    - 失败重试 max_attempts 次后记录 FAIL，次日自动再试
      （每阶段上限见 PHASE_MAX_ATTEMPTS：盘前 sector/lhb 给更长窗口，#116）
    - 启动时检查上一交易日阶段完整性（缺口告警），并按时间窗补跑当日 close
    - 心跳：logs/run_daily_heartbeat.json 记录最后运行时间/来源/阶段
    """
    phase_desc = " / ".join(f"{p} {t or schedule}" for p, t, _ in PHASES)
    dup = _acquire_single_instance()
    if dup:
        # #60：已有守护在跑，重复启动直接退出（防并发抢跑同一阶段）
        _log_watch(f"守护启动被拒绝：已有实例 PID {dup} 运行中（来源: {_startup_provenance(source)}）")
        print(f"已有守护实例运行（PID {dup}），本实例退出")
        return 0
    import atexit

    atexit.register(lambda: PID_FILE.unlink(missing_ok=True))
    who = _startup_provenance(source)
    _log_watch(f"守护启动（PID {os.getpid()}, 来源: {who}, 阶段: {phase_desc}, 状态文件 {STATE_FILE}）")
    _write_heartbeat(who, status="running", phase="startup",
                     extra={"schedule": schedule})

    # ---- 启动缺口检测：最近 5 个交易日是否有阶段缺失（11-04 缺收盘可被发现）----
    now0 = datetime.now()
    recent_gaps = _recent_gaps((now0.date() - timedelta(days=1)).isoformat(), 5)
    last_gap = None
    if recent_gaps:
        last_gap = {"date": recent_gaps[0]["date"], "phases": recent_gaps[0]["phases"],
                    "all": recent_gaps,
                    "checked_at": now0.strftime("%Y-%m-%d %H:%M:%S")}
        detail = "；".join(f"{g['date']} 缺 {','.join(g['phases'])}" for g in recent_gaps)
        _log_watch(f"缺口检测：最近交易日存在缺口 → {detail}")
        _notify("TriLoop GAP DETECTED",
                f"近5个交易日存在缺口：{detail}。盘前阶段不可回溯补算，"
                f"收盘缺口仅在当日 15:35 后可补；详见 outputs/收盘报表/")
        _write_heartbeat(who, status="gap-detected", phase="startup",
                         extra={"schedule": schedule, "last_gap": last_gap})
    else:
        _log_watch("缺口检测：最近 5 个交易日四阶段齐全")

    # 15:35 之后启动时，若当日 sector 仍非 OK，优先补拉（早于 close 闭环补跑）
    day0 = now0.date().isoformat()
    if (_is_trading_day(day0) and now0.strftime("%H:%M") >= "15:35"
            and _read_state().get((day0, "sector")) != "OK"):
        _log_watch(f"{day0} 启动时 sector 仍缺失，优先补拉（早于 close 闭环）")
        rc_s, code_s, reason_s = _run_sector()
        if rc_s == 0:
            _mark_ran(day0, "sector", ok=True)
            _log_watch(f"{day0} sector 启动补拉成功 ✅")
        else:
            _mark_retry(day0, "sector", 1, code_s)
            _log_watch(f"{day0} sector 启动补拉失败（{reason_s}），交由 close 闭环自动补拉")

    # 补跑当日 close（仅 15:35 后、交易日、当日 close 未完成）
    for phase in _backfill_plan(now0, close_time=schedule):
        _log_watch(f"{now0.date().isoformat()} 补跑 {phase}（启动时缺口检测触发）")
        _write_heartbeat(who, status="backfill", phase=phase,
                         extra={"schedule": schedule, "last_gap": last_gap})
        rc = _run_daily_flow()
        if rc in (0, 2):
            _mark_ran(now0.date().isoformat(), phase, ok=True)
            _log_watch(f"{now0.date().isoformat()} 补跑 {phase} 完成 ✅（exit={rc}）")
        elif rc == 3:
            _mark_ran(now0.date().isoformat(), phase, "DEGRADED")
            _log_watch(f"{now0.date().isoformat()} 补跑 {phase} 完成（DEGRADED）")
        else:
            _mark_ran(now0.date().isoformat(), phase, ok=False)
            _log_watch(f"{now0.date().isoformat()} 补跑 {phase} 失败（exit={rc}），记 FAIL")
            _notify("TriLoop BACKFILL FAILED",
                    f"{now0.date().isoformat()} 启动补跑 {phase} 失败（exit={rc}），已记 FAIL")

    # 收盘时点后启动：sector 仍非 OK 时收敛为终态（成功补拉→OK，否则 FAIL）
    if (now0.strftime("%H:%M") >= schedule
            and _read_state().get((day0, "sector")) != "OK"):
        _settle_sector_after_close(day0)

    last_today = None
    attempts: dict[str, int] = {}
    close_sector_try: set[str] = set()
    while True:
        now = datetime.now()
        today = now.date().isoformat()
        if today != last_today:
            last_today = today
            attempts = {}
            close_sector_try = set()
        _write_heartbeat(who, status="idle", phase="watch-loop",
                         extra={"schedule": schedule, "last_gap": last_gap})

        if not _is_trading_day(today):
            if once:
                _log_watch(f"{today} 非交易日，守护一轮结束")
                return 0
            time.sleep(300)          # 非交易日：5 分钟后再查
            continue

        pending = False
        failed = False
        for phase, phase_time_default, runner in PHASES:
            phase_time = phase_time_default or schedule
            if _finished(today, phase):
                continue
            pending = True

            if phase == "sector":
                # 同日多时段重试：第 n 次尝试固定在第 n 个时段（08:50/09:10/…/15:10）
                n = attempts.get("sector", 0)
                if now.strftime("%H:%M") >= schedule:
                    # 收盘时点后不再按盘前时段重试；留给 close 闭环的自动补拉，
                    # 由 _settle_sector_after_close 收敛状态
                    continue
                if n >= len(SECTOR_RETRY_SLOTS):
                    _mark_ran(today, phase, ok=False)
                    _log_watch(f"{today} sector 全部 {len(SECTOR_RETRY_SLOTS)} 个时段重试失败，"
                               f"已记录 FAIL；15:35 收盘闭环仍会优先补拉当日快照")
                    _notify(
                        "TriLoop SECTOR FAILED",
                        f"{today} sector 全部时段重试失败，已记录 FAIL；"
                        f"收盘闭环会优先补拉，详见 logs/daily_runner.log",
                    )
                    continue
                if now.strftime("%H:%M") < SECTOR_RETRY_SLOTS[n]:
                    continue
            else:
                if now.strftime("%H:%M") < phase_time:
                    continue
                # 过点补做：竞价预案等硬时点阶段一旦超时，不再运行，记 LATE 终态
                if _deadline_passed(phase, now):
                    _mark_ran(today, phase, "LATE")
                    _log_watch(
                        f"{today} {phase} 已过硬时点 {PHASE_DEADLINE[phase]}"
                        f"（当前 {now.strftime('%H:%M')}），取消补做，记 LATE"
                    )
                    _notify(
                        f"TriLoop {phase.upper()} LATE",
                        f"{today} {phase} 未能在 {PHASE_DEADLINE[phase]} 前完成，"
                        f"已跳过补做并记 LATE（不产出事后冒充的产物）",
                    )
                    continue
                limit = PHASE_MAX_ATTEMPTS.get(phase, max_attempts)
                if attempts.get(phase, 0) >= limit:
                    _mark_ran(today, phase, ok=False)
                    _log_watch(f"{today} {phase} 多次失败，已记录 FAIL，次日自动再试")
                    _notify(
                        f"TriLoop {phase.upper()} FAILED",
                        f"{today} {phase} 阶段多次重试失败，已记录 FAIL。"
                        f"请查看 outputs/收盘报表/告警_{today}.md 或 logs/daily_runner.err.log",
                    )
                    continue

            # 收盘闭环优先补拉：15:35 后 sector 仍非 OK 时，先补一次再进入闭环
            if (phase == "close" and today not in close_sector_try
                    and _read_state().get((today, "sector")) != "OK"):
                close_sector_try.add(today)
                _log_watch(f"{today} 收盘前 sector 仍缺失，优先补拉（早于 close 闭环）")
                rc_s, code_s, reason_s = _run_sector()
                if rc_s == 0:
                    _mark_ran(today, "sector", ok=True)
                    _log_watch(f"{today} sector 收盘前补拉成功 ✅")
                else:
                    _mark_retry(today, "sector", len(SECTOR_RETRY_SLOTS) + 1, code_s)
                    _log_watch(f"{today} sector 收盘前补拉失败（{reason_s}），"
                               f"交由 close 闭环自动补拉")

            attempts[phase] = attempts.get(phase, 0) + 1
            if phase == "sector":
                _log_watch(f"{today} 触发 sector（第{attempts['sector']}次尝试，"
                           f"时段 {SECTOR_RETRY_SLOTS[attempts['sector'] - 1]}）")
                _write_heartbeat(who, status="running", phase=phase,
                                 extra={"schedule": schedule, "last_gap": last_gap})
                rc, reason_code, reason_text = _run_sector()
            else:
                _log_watch(f"{today} 触发 {phase}（第{attempts[phase]}次尝试）")
                _write_heartbeat(who, status="running", phase=phase,
                                 extra={"schedule": schedule, "last_gap": last_gap})
                rc, reason_code, reason_text = runner(), "", ""

            if rc == 0:
                _mark_ran(today, phase, ok=True)
                _log_watch(f"{today} {phase} 完成 ✅")
            elif rc == 2:
                _mark_ran(today, phase, ok=True)
                _log_watch(f"{today} {phase} 完成 ✅（推送失败，已本地归档，可稍后手动推送）")
                _notify(
                    "TriLoop ARCHIVED (PUSH FAILED)",
                    f"{today} {phase} 完成但推送失败（网络），已本地归档，稍后手动 git push",
                )
            elif rc == 3:
                _mark_ran(today, phase, "DEGRADED")
                _log_watch(f"{today} {phase} 完成（DEGRADED：健康检查/每日硬检查有 FAIL 或自身崩溃）")
                _notify(
                    "TriLoop DEGRADED",
                    f"{today} {phase} 已运行但健康检查/每日硬检查有 FAIL 或自身崩溃，"
                    f"状态记 DEGRADED，详见 outputs/收盘报表/健康检查_{today}.md 与 硬检查_{today}.md",
                )
            else:
                if phase == "sector":
                    nxt = (SECTOR_RETRY_SLOTS[attempts["sector"]]
                           if attempts["sector"] < len(SECTOR_RETRY_SLOTS) else "无（已到 15:10）")
                    _mark_retry(today, "sector", attempts["sector"], reason_code)
                    _log_watch(f"{today} sector 第{attempts['sector']}次尝试失败（"
                               f"{reason_text}），下一时段 {nxt} 再试")
                    # 不置 failed：sector 按固定时段重试，避免 10 分钟睡眠拖延迟 lhb/auction
                else:
                    failed = True
                    _log_watch(f"{today} {phase} 运行失败（exit={rc}），{retry_minutes} 分钟后重试")

            if phase == "close" and rc in (0, 2, 3):
                # 收盘闭环（含“当日快照缺失自动补拉”）结束后收敛 sector 状态
                _settle_sector_after_close(today)

        if once:
            return 0
        if failed:
            time.sleep(retry_minutes * 60)
        elif pending:
            time.sleep(60)
        else:
            time.sleep(300)


def install_startup() -> int:
    """写入开机自启：登录后隐藏启动 --watch 守护（免管理员）"""
    if not STARTUP_DIR.exists():
        print(f"⚠ 找不到启动目录: {STARTUP_DIR}")
        return 1
    vbs = STARTUP_DIR / VBS_NAME
    python = sys.executable
    script = str(Path(__file__).resolve())
    content = (
        'Set sh = CreateObject("WScript.Shell")\r\n'
        f'sh.Run """{python}"" ""{script}"" --watch --source startup-vbs", 0, False\r\n'
    )
    vbs.write_text(content, encoding="ascii")
    print(f"✅ 开机自启已安装: {vbs}")
    print("   登录后自动启动后台守护（交易日 08:55 龙虎榜 / 09:26 竞价预案 / 15:35 收盘闭环）")
    print("   卸载: python run_daily.py --uninstall-startup")
    return 0


def uninstall_startup() -> int:
    vbs = STARTUP_DIR / VBS_NAME
    if vbs.exists():
        vbs.unlink()
        print(f"✅ 已移除开机自启: {vbs}")
    else:
        print("ℹ 未找到开机自启项")
    return 0


def _schtasks(*args: str) -> int:
    """计划任务（需管理员；注册成功时优先于自启守护）"""
    proc = subprocess.run(["schtasks", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.stdout:
        print(proc.stdout.strip())
    if proc.stderr:
        print(proc.stderr.strip(), file=sys.stderr)
    return proc.returncode


def install_task() -> int:
    """注册计划任务（登录触发常驻守护；推荐用 scripts/install_daily_task.ps1）

    与旧版的区别：旧版只在工作日 15:35 跑一次收盘，盘前 lhb/auction 无人触发；
    新版改为 ONLOGON 触发常驻 --watch（与自启 VBS 等价），并显式标记来源。
    """
    tr = (f'"{sys.executable}" "{Path(__file__).resolve()}" '
          f'--watch --source task-scheduler')
    rc = _schtasks(
        "/Create", "/F", "/TN", TASK_NAME,
        "/SC", "ONLOGON", "/RL", "LIMITED", "/TR", tr,
    )
    if rc == 0:
        print(f"✅ 已注册计划任务 {TASK_NAME}（登录后常驻守护；需管理员权限）")
        print(f"   立即启动: schtasks /Run /TN {TASK_NAME}")
    return rc


def uninstall_task() -> int:
    return _schtasks("/Delete", "/F", "/TN", TASK_NAME)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="自动任务编排器（盘前 + 收盘，纯本地自动运行）")
    parser.add_argument("--dry-run", action="store_true", help="演练：不写生产库、不提交")
    parser.add_argument("--no-push", action="store_true", help="收盘闭环 + 提交，但不推送")
    parser.add_argument("--watch", action="store_true", help="后台守护模式（交易日 lhb/auction/close 自动跑）")
    parser.add_argument("--schedule", default="15:35", help="守护收盘触发时间 HH:MM（默认 15:35）")
    parser.add_argument("--once", action="store_true", help="守护只检查一轮（测试用）")
    parser.add_argument("--source", default="",
                        help="守护启动来源标记（manual/task-scheduler/startup-vbs），写入心跳")
    parser.add_argument("--install-startup", action="store_true", help="写入开机自启（免管理员）")
    parser.add_argument("--uninstall-startup", action="store_true", help="移除开机自启")
    parser.add_argument("--install-task", action="store_true", help="注册计划任务（需管理员）")
    parser.add_argument("--uninstall-task", action="store_true", help="删除计划任务")
    args = parser.parse_args()

    if args.install_startup:
        sys.exit(install_startup())
    if args.uninstall_startup:
        sys.exit(uninstall_startup())
    if args.install_task:
        sys.exit(install_task())
    if args.uninstall_task:
        sys.exit(uninstall_task())
    if args.watch:
        sys.exit(watch(schedule=args.schedule, once=args.once, source=args.source))

    print("=" * 60)
    print("  收盘自动任务：R47 闭环 → 归档")
    print("=" * 60)
    sys.exit(_run_daily_flow(dry_run=args.dry_run, no_push=args.no_push))


if __name__ == "__main__":
    main()
