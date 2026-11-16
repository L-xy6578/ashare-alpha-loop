#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
项目健康检查（防幻觉机制）——把"稳定/正常"变成可重复验证的证据

用法:
    python health_check.py                  # 查最近交易日（今天非交易日则取上一交易日）
    python health_check.py --date 2026-08-14
    python health_check.py --tests          # 追加跑全量 pytest

输出: 逐项 ✅/⚠️/❌ + 证据；退出码 0=全过 1=有警告 2=有失败 3=自身崩溃
原则: ✅ 只给当场验证过的项；未验证的标 ⚠️ 待验证，不默认通过。

退出码 3 专用于"健康检查自身未捕获异常崩溃"：此时检查结论不可信（报告可能都没写成），
调用方（run_daily）必须按 degraded 处理，不得与"有 WARN"（1）混淆。
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
import traceback
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.stdout.reconfigure(encoding="utf-8")

MARKET_DB = ROOT / "data" / "market.db"
KNOWLEDGE_DB = ROOT / "data" / "knowledge.db"
STATE_FILE = ROOT / "logs" / "daily_runner_state.txt"
ERR_LOG = ROOT / "logs" / "daily_runner.err.log"
PID_FILE = ROOT / "logs" / "run_daily.pid"

EXIT_OK, EXIT_WARN, EXIT_FAIL, EXIT_CRASH = 0, 1, 2, 3

results: list[tuple[str, str, str]] = []   # (PASS/WARN/FAIL/INFO, 检查项, 证据)


def rec(status: str, cid: str, msg: str) -> None:
    results.append((status, cid, msg))


def _conn(db: Path):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    return con


def latest_trading_day(day: str) -> str | None:
    with _conn(MARKET_DB) as con:
        row = con.execute(
            "SELECT MAX(date) FROM trade_calendar WHERE is_trading=1 AND date<=?",
            (day,),
        ).fetchone()
    return row[0] if row else None


def pid_alive(pid: int) -> bool:
    import ctypes

    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not h:
        return False
    ctypes.windll.kernel32.CloseHandle(h)
    return True


def git(*args: str) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=str(ROOT),
                           capture_output=True, text=True, timeout=20,
                           errors="replace")
        return (r.stdout or "").strip()
    except Exception as e:
        return f"ERR:{e}"


def check_schedule(day: str, pre_close: bool, in_close: bool = False) -> None:
    """A. 状态文件：目标交易日四阶段"""
    want = {"sector", "lhb", "auction", "close"}
    got: dict[str, str] = {}
    if STATE_FILE.exists():
        for line in STATE_FILE.read_text(encoding="utf-8-sig").splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0] == day and parts[1] in want:
                got[parts[1]] = parts[2]
    for ph in ("sector", "lhb", "auction", "close"):
        if ph in got:
            if got[ph] == "OK":
                rec("PASS", f"A1.{ph}", f"{day} {ph}=OK")
            elif got[ph] == "DEGRADED":
                # 2026-11-06：健康检查有 FAIL 时 close 记 DEGRADED（向后兼容旧 OK/FAIL 读法）
                rec("WARN", f"A1.{ph}", f"{day} {ph}=DEGRADED（跑完但有健康检查 FAIL）")
            else:
                rec("FAIL", f"A1.{ph}", f"{day} {ph}={got[ph]}")
        else:
            if ph == "close" and (pre_close or in_close):
                rec("INFO", f"A1.{ph}", f"{day} close 尚未运行（15:35 后生成）")
            else:
                rec("WARN", f"A1.{ph}", f"{day} 无 {ph} 记录（非交易日或未运行）")


def check_daemon() -> None:
    """B. 守护：单实例锁 + 进程存活"""
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
        except ValueError:
            pid = None
        if pid and pid_alive(pid):
            rec("PASS", "B1.daemon", f"守护 PID {pid} 存活（锁有效）")
        else:
            rec("WARN", "B1.daemon", f"锁文件存在但 PID {pid} 不可达（陈旧锁）")
    else:
        rec("WARN", "B1.daemon", "无锁文件：守护可能未启动，或为旧代码运行（#60 前）")


def check_heartbeat() -> None:
    """B2. 心跳：最后运行时间/来源（判断守护是否断档）"""
    hb = ROOT / "logs" / "run_daily_heartbeat.json"
    if not hb.exists():
        rec("WARN", "B2.heartbeat", "无心跳文件（守护未运行过或为旧版本）")
        return
    try:
        import json
        data = json.loads(hb.read_text(encoding="utf-8"))
        last = datetime.fromisoformat(data["last_run"])
        age_min = (datetime.now() - last).total_seconds() / 60
        status = data.get("status", "")
        source = data.get("source", "unknown")
        detail = f"最后运行 {data['last_run']}（{age_min:.0f} 分钟前，来源={source}，状态={status}）"
        if age_min > 30:
            rec("WARN", "B2.heartbeat", f"{detail} — 可能断档（>30 分钟无心跳）")
        else:
            rec("PASS", "B2.heartbeat", detail)
    except (ValueError, KeyError, OSError) as exc:
        rec("WARN", "B2.heartbeat", f"心跳文件不可解析: {exc}")


def check_logs(day: str) -> None:
    """C. 日志：err.log 无目标日条目"""
    if ERR_LOG.exists():
        bad = [l for l in ERR_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
               if day in l]
        rec("PASS" if not bad else "FAIL", "C1.errlog",
            f"{day} err.log 条目={len(bad)}")
    else:
        rec("PASS", "C1.errlog", "err.log 不存在（无错误记录）")


def check_products(day: str, pre_close: bool) -> None:
    """D. 产物：当日四类产物存在"""
    outs = ROOT / "outputs"
    checks = [
        ("D1.lhb", outs / "龙虎榜简报" / f"龙虎榜简报_{day}.md"),
        ("D2.auction", outs / "竞价预案" / f"竞价预案_{day}.md"),
        ("D3.report", outs / "收盘报表" / f"收益报表_{day}.md"),
        ("D4.kpi", outs / "收盘报表" / f"KPI07评估_{day}.md"),
    ]
    for cid, p in checks:
        if p.exists():
            rec("PASS", cid, f"{p.name}: 存在")
        elif pre_close and cid in ("D3.report", "D4.kpi"):
            rec("INFO", cid, f"{p.name}: 收盘前待生成")
        else:
            rec("WARN", cid, f"{p.name}: 缺失")


def check_git(in_close: bool = False) -> None:
    """E. git：工作区干净 + 无 ahead/behind"""
    dirty = git("status", "--porcelain")
    if not dirty:
        rec("PASS", "E1.dirty", "工作区干净")
    elif in_close:
        rec("INFO", "E1.dirty", "归档前待提交（收盘流程内属正常）")
    else:
        rec("WARN", "E1.dirty", f"有未提交: {dirty[:80]}")
    branch = git("status", "-sb").splitlines()
    line = branch[0] if branch else ""
    if "ahead" in line or "behind" in line:
        rec("WARN", "E2.sync", f"未同步: {line}")
    else:
        rec("PASS", "E2.sync", f"分支同步: {line}")


def check_data(day: str, pre_close: bool, historical: bool = False) -> None:
    """F. 数据：指数/观察池 bar 新鲜度、板块条数、无重复行"""
    with _conn(MARKET_DB) as con:
        for sym in ("sh000001", "sz399001", "sz399006", "sh000300"):
            row = con.execute(
                "SELECT MAX(date), close FROM daily_bars WHERE symbol=? ORDER BY date DESC LIMIT 1",
                (sym,),
            ).fetchone()
            ok = row and row[0] == day
            if ok:
                rec("PASS", f"F1.{sym}", f"{sym} 最新={day}")
            elif historical:
                # #113：补跑历史日期时，"最新=当天"不适用（库内已有更晚 bar）→ 降级 INFO
                rec("INFO", f"F1.{sym}",
                    f"{sym} 最新={row[0] if row else None}（补跑历史日期，跳过最新性检查）")
            elif pre_close:
                rec("INFO", f"F1.{sym}",
                    f"{sym} 最新={row[0] if row else None}（收盘前，当日 bar 15:35 生成）")
            else:
                rec("FAIL", f"F1.{sym}",
                    f"{sym} 最新={row[0] if row else None}（应={day}）")
        # 职责边界（2026-11-07）：观察池停更的唯一判据是
        # src.daily_audit.watchlist_coverage（收盘硬检查的"观察池层"用同一函数）。
        # F2 不重写阈值与停牌/断供分类，只把同一结论呈现：
        # outage=True → FAIL（与 daily_audit 对齐，run_daily 因 rc==2 置 DEGRADED）；
        # 有断供但未到管道级阈值 → WARN；全覆盖 → PASS。
        try:
            from src.daily_audit import OUTAGE_RATIO, watchlist_coverage
            cov = watchlist_coverage(day, str(MARKET_DB))
        except Exception as exc:   # 判据不可用时不静默放过
            cov = None
            rec("WARN", "F2.watchlist", f"观察池覆盖判据不可用：{exc}")
        if cov is not None and cov.active == 0:
            rec("INFO", "F2.watchlist",
                "active 观察池为空，观察池层跳过（仅指数锚点判定）")
        elif cov is not None and cov.tracked == 0:
            rec("INFO", "F2.watchlist",
                "active 观察池均无前一交易日K线，观察池层无法判定（跳过）")
        elif cov is not None and historical:
            rec("INFO", "F2.watchlist",
                f"观察池 {cov.active} 只（补跑历史日期，跳过新鲜度检查）")
        elif cov is not None and pre_close:
            rec("INFO", "F2.watchlist",
                f"观察池 {cov.active} 只，断供 {len(cov.missing)} 只待收盘生成")
        elif cov is not None and cov.outage:
            rec("FAIL", "F2.watchlist",
                f"观察池 {cov.active} 只，覆盖率 {cov.covered}/{cov.tracked}，"
                f"断供 {len(cov.missing)} 只 ≥{OUTAGE_RATIO:.0%}（管道级断供）："
                + "、".join(cov.missing[:5]))
        elif cov is not None and cov.missing:
            rec("WARN", "F2.watchlist",
                f"观察池 {cov.active} 只，覆盖率 {cov.covered}/{cov.tracked}，"
                f"断供 {len(cov.missing)} 只（未达 {OUTAGE_RATIO:.0%} 阈值）："
                + "、".join(cov.missing[:5]))
        elif cov is not None:
            rec("PASS", "F2.watchlist",
                f"观察池 {cov.active} 只，覆盖率 {cov.covered}/{cov.tracked}，"
                f"停牌 {cov.suspended} 只")
        cnt = con.execute(
            "SELECT COUNT(*) FROM sector_daily WHERE date=?", (day,)).fetchone()[0]
        if historical:
            rec("INFO", "F3.sector", f"{day} 板块条数={cnt}（补跑历史日期，仅参考）")
        else:
            rec("PASS" if cnt >= 900 else "WARN", "F3.sector",
                f"{day} 板块条数={cnt}（#75：允许日间波动，<900 告警）")
        dup = con.execute(
            "SELECT COUNT(*) FROM (SELECT symbol,date FROM daily_bars "
            "GROUP BY symbol,date HAVING COUNT(*)>1)").fetchone()[0]
        rec("PASS" if dup == 0 else "FAIL", "F4.dup", f"daily_bars 重复行={dup}")


def check_books() -> None:
    """G. 账本：数量、KPI、乱码"""
    with _conn(KNOWLEDGE_DB) as con:
        c = con.execute("SELECT COUNT(*) FROM correct_book").fetchone()[0]
        m = con.execute("SELECT COUNT(*) FROM mistake_book").fetchone()[0]
        rec("INFO", "G1.counts", f"对题本={c} 错题本={m}")
        for r in con.execute(
                "SELECT stock_name, target_pct, target_price, deadline, status "
                "FROM kpi_tracking"):
            rec("INFO", "G2.kpi",
                f"{r['stock_name']} +{r['target_pct']:.0f}%→{r['target_price']} "
                f"截止{r['deadline']} [{r['status']}]")
        moj = con.execute(
            "SELECT COUNT(*) FROM (SELECT title FROM research_notes WHERE title LIKE '%?%' "
            "UNION ALL SELECT note FROM kpi_tracking WHERE note LIKE '%?%')").fetchone()[0]
        rec("PASS" if moj == 0 else "FAIL", "G3.mojibake",
            f"乱码记录数={moj}")


def check_standards() -> None:
    """H. 文档标准化（2026-08-17）：根目录命名、BOM、行尾、半角标点夹中文"""
    scan_mds = (list(ROOT.glob("*.md"))
                + list((ROOT / "docs").rglob("*.md"))
                + list((ROOT / "outputs").rglob("*.md")))
    # H1: 根目录只允许核心 md（README/AGENTS/文档命名规范）
    root_mds = sorted(p.name for p in ROOT.glob("*.md"))
    allowed = {"README.md", "AGENTS.md", "文档命名规范.md"}
    stray = [n for n in root_mds if n not in allowed]
    rec("PASS" if not stray else "WARN", "H1.root_md",
        f"根目录 md={root_mds or '无'}，多余={stray or '无'}")
    # H2: 无 BOM（根/输出/归档文档）
    bom_list = []
    for p in scan_mds:
        if p.read_bytes().startswith(b"\xef\xbb\xbf"):
            bom_list.append(p.name)
    rec("PASS" if not bom_list else "WARN", "H2.bom",
        f"BOM 文件={bom_list or '无'}")
    # H3: 根/输出 md 行尾不混合（docs 仅顶层；归档文档不强求）
    mixed = []
    for p in (list(ROOT.glob("*.md")) + list((ROOT / "docs").glob("*.md"))
              + list((ROOT / "outputs").rglob("*.md"))):
        body = p.read_bytes()
        crlf = body.count(b"\r\n"); lf = body.count(b"\n") - crlf
        if crlf and lf:
            mixed.append(p.name)
    rec("PASS" if not mixed else "WARN", "H3.eol", f"混合行尾={mixed or '无'}")
    # H4: 半角逗号夹中文（仅逗号；句号可能是版本号/URL/层级点，不误报）
    import re as _re
    bad = []
    for p in scan_mds:
        text = p.read_text(encoding="utf-8", errors="replace")
        if _re.search(r"[\u4e00-\u9fff],[\u4e00-\u9fff]", text):
            bad.append(p.name)
    rec("PASS" if not bad else "WARN", "H4.punct",
        f"半角标点夹中文={bad or '无'}")


def run_tests() -> None:
    # #76：测试环境用项目 .venv（不依赖 Codex 运行时，运行时更新会丢 pytest）
    venv_py = ROOT / ".venv" / "Scripts" / "python.exe"
    py = str(venv_py) if venv_py.exists() else sys.executable
    r = subprocess.run([py, "-m", "pytest", "src/tests", "-q"],
                       cwd=str(ROOT), capture_output=True, text=True,
                       timeout=600, errors="replace")
    tail = [l for l in r.stdout.splitlines() if "passed" in l or "failed" in l]
    rec("PASS" if r.returncode == 0 else "FAIL", "H1.pytest",
        f"[{Path(py).name}] " + (tail[-1] if tail else f"exit={r.returncode}"))


def _main() -> int:
    ap = argparse.ArgumentParser(description="项目健康检查")
    ap.add_argument("--date", default=date.today().isoformat(), help="检查日")
    ap.add_argument("--tests", action="store_true", help="追加跑全量 pytest")
    ap.add_argument("--out", default=None, help="报告输出路径（写文件）")
    ap.add_argument("--in-close", action="store_true",
                    help="在收盘流程内运行（close 状态/待归档视为待生成）")
    args = ap.parse_args()

    day = latest_trading_day(args.date) or args.date
    historical = args.date < date.today().isoformat()
    pre_close = (day == args.date and datetime.now().strftime("%H:%M") < "15:35")
    lines = [f"# 健康检查（{args.date} → 目标交易日 {day}）", "=" * 64]
    check_schedule(day, pre_close, args.in_close)
    check_daemon()
    check_heartbeat()
    check_logs(day)
    check_products(day, pre_close)
    check_git(args.in_close)
    check_data(day, pre_close, historical)
    check_books()
    check_standards()
    if args.tests:
        run_tests()

    fails = warns = 0
    for status, cid, msg in results:
        mark = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌", "INFO": "ℹ️"}[status]
        lines.append(f"{mark} {cid}: {msg}")
        fails += status == "FAIL"
        warns += status == "WARN"
    lines.append("=" * 64)
    lines.append(f"结果: PASS={sum(1 for s, _, _ in results if s=='PASS')} "
                 f"WARN={warns} FAIL={fails} INFO={sum(1 for s, _, _ in results if s=='INFO')}")
    text = "\n".join(lines) + "\n"
    print(text, end="")
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"报告已写入: {args.out}")
    return 2 if fails else (1 if warns else 0)


def main() -> int:
    """入口：未捕获异常一律转成 EXIT_CRASH（3），不落 1（有 WARN），避免崩溃被当警告吞掉"""
    try:
        return _main()
    except SystemExit:
        raise                     # argparse 的 --help / 参数错误保持原语义
    except KeyboardInterrupt:
        raise
    except Exception:
        traceback.print_exc()
        print("健康检查自身崩溃（未捕获异常）：本次检查结论不可信、报告可能缺失，"
              "调用方须按 DEGRADED 处理", file=sys.stderr)
        return EXIT_CRASH


if __name__ == "__main__":
    sys.exit(main())
