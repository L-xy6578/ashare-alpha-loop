"""
每日硬检查（收盘收口）— 三条硬指标合成一份日报

背景：11-04 行情断更 4 天、close 却记了 OK，因为缺少统一收口。本模块不再新造
判据，只把已有口径合成一道硬门禁：
    ① 行情最新交易日 = 当日（数据源：daily_bars，锚点为已纳入每日采集的沪深300）
    ② 板块快照完整且日期 = 当日（数据源：sector_snapshot_meta.complete）
    ③ 当日决策记录存在；若全部为弃权，弃权原因不得为空（decision_log）

任一不满足 → 整体 DEGRADED，由 run_daily 触发桌面告警并把 close 状态记
DEGRADED（不改变现有阶段退出码语义，复用 3=完成但 DEGRADED）。

产物：outputs/收盘报表/硬检查_YYYY-MM-DD.md
    放这里的理由：收盘报表目录随 archive_daily 一并提交进 git，可与收益报表/
    KPI/健康检查同处审计；logs/ 只用于运行日志（会轮转、不入库），不适合当凭证。

用法:
    python -m src.daily_audit --date 2026-11-06
    python -m src.daily_audit --date 2026-11-06 --out outputs/收盘报表/硬检查_2026-11-06.md
退出码：0=通过（含非交易日跳过）；2=存在不满足项（DEGRADED）；3=自身崩溃
    （3 与 1 区分开：未捕获异常不得与"有警告"混为一谈，调用方须按 DEGRADED 处理）
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from src.config import DB_PATH, MARKET_DB_PATH, ROOT
from src.decision_log import DecisionStore
from src.logger import get_logger

log = get_logger(__name__)

BENCHMARK_SYMBOL = "sh000300"          # 沪深300：已纳入每日采集的行情锚点
AUDIT_DIR = ROOT / "outputs" / "收盘报表"
OUTAGE_RATIO = 0.5                     # 观察池层断供阈值：有前日K线的 active 中 ≥50% 当日断供
SUSPEND_KEYWORDS = ("停牌", "暂停交易", "暂停上市")


@dataclass
class Check:
    key: str
    title: str
    ok: bool
    detail: str
    evidence: str = ""


@dataclass
class AuditResult:
    day: str
    ok: bool
    checks: list[Check] = field(default_factory=list)
    skipped: str = ""                   # 非交易日跳过原因（空=正常执行）
    report_path: str = ""


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _is_trading_day(day: str, market_db: str) -> bool:
    """按 trade_calendar 判定；日历缺失时按交易日处理（宁可检查，不静默放过）"""
    try:
        with _connect(market_db) as conn:
            row = conn.execute(
                "SELECT is_trading FROM trade_calendar WHERE date=?", (day,),
            ).fetchone()
        return True if row is None else bool(row["is_trading"])
    except sqlite3.Error:
        return True


@dataclass
class WatchlistCoverage:
    """active 观察池当日 K 线覆盖 —— 观察池层的唯一判据（health_check.F2 复用同一结论）

    口径：
    - 只统计 `watchlist.status='active'`；软删（removed 等）一律不计入
    - `tracked` = 前一交易日有 K 线的 active 数（覆盖率的分子分母都以此为准，
      避免"从未有过 K 线"的新标的把覆盖率稀释成假象）
    - `covered` = 当日有 K 线（停牌当天若有 0 成交 bar 也算有 K 线）
    - `suspended` = 停牌证据：当日 0 成交 bar，或当日无 bar 但备注含停牌字样
    - `missing` = 昨日有、今日无、且无停牌证据 —— 真断供，只有这一类参与 FAIL 判定
    """
    active: int = 0
    tracked: int = 0
    covered: int = 0
    suspended: int = 0
    missing: list[str] = field(default_factory=list)
    suspended_list: list[str] = field(default_factory=list)
    no_history: list[str] = field(default_factory=list)
    prev_day: str = ""

    @property
    def ratio(self) -> float:
        return self.covered / self.tracked if self.tracked else 0.0

    @property
    def outage(self) -> bool:
        """管道级断供：有前日K线的 active 中 ≥OUTAGE_RATIO 当日断供。

        单只停牌/退市只进 `missing` 不计停牌时不触发 FAIL（容忍个股异常），
        但"观察池整体停在昨天"必然触发。
        """
        return self.tracked > 0 and len(self.missing) / self.tracked >= OUTAGE_RATIO


def watchlist_coverage(day: str,
                       market_db: str = MARKET_DB_PATH) -> WatchlistCoverage:
    """统计 active 观察池当日 K 线覆盖与停牌/断供分类（唯一判据，供两处复用）"""
    with _connect(market_db) as conn:
        prev = conn.execute(
            "SELECT MAX(date) AS d FROM trade_calendar WHERE is_trading=1 AND date<?",
            (day,)).fetchone()["d"]
        if not prev:
            prev = conn.execute(
                "SELECT MAX(date) AS d FROM daily_bars WHERE date<?", (day,),
            ).fetchone()["d"] or ""
        rows = conn.execute(
            """SELECT w.symbol, w.name, COALESCE(w.note,'') AS note,
                      (SELECT b.volume FROM daily_bars b
                        WHERE b.symbol=w.symbol AND b.date=? LIMIT 1) AS vol,
                      EXISTS(SELECT 1 FROM daily_bars b
                             WHERE b.symbol=w.symbol AND b.date=?) AS has_today,
                      EXISTS(SELECT 1 FROM daily_bars b
                             WHERE b.symbol=w.symbol AND b.date=?) AS has_prev
                 FROM watchlist w WHERE w.status='active'""",
            (day, day, prev)).fetchall()
    cov = WatchlistCoverage(active=len(rows), prev_day=prev or "")
    for r in rows:
        label = f"{r['name'] or r['symbol']}({r['symbol']})"
        if r["has_prev"]:
            cov.tracked += 1
        if r["has_today"]:
            cov.covered += 1
            if r["vol"] is None or float(r["vol"]) == 0:   # 当日 0 成交 = 停牌证据
                cov.suspended += 1
                cov.suspended_list.append(label)
        elif not r["has_prev"]:
            cov.no_history.append(label)
        elif any(k in r["note"] for k in SUSPEND_KEYWORDS):
            cov.suspended += 1
            cov.suspended_list.append(label)
        else:
            cov.missing.append(label)
    return cov


def check_market(day: str, market_db: str = MARKET_DB_PATH) -> Check:
    """① 行情：两层判定

    - 指数层（锚点）：沪深300 最新 bar = 当日 → 只回答"采集管道是否通"
    - 观察池层（判据）：active 观察池当日 K 线覆盖率 → 回答"该看的标的有没有被采集"
    两层都通过才 PASS；只看指数会让"观察池整体停在昨天"照样 PASS（fail-open）。
    """
    try:
        with _connect(market_db) as conn:
            last = conn.execute(
                "SELECT MAX(date) AS d FROM daily_bars WHERE symbol=?",
                (BENCHMARK_SYMBOL,),
            ).fetchone()["d"]
        cov = watchlist_coverage(day, market_db)
    except sqlite3.Error as exc:
        return Check("market", "行情最新交易日 = 当日", False,
                     f"读取行情库失败：{exc}")
    index_ok = last == day
    # 观察池层：空池 / 无前日K线时明确跳过（写明"仅指数锚点判定"，不静默 PASS）
    if cov.active == 0:
        wl_ok, wl_note = True, "active 观察池为空，观察池层跳过（仅指数锚点判定）"
    elif cov.tracked == 0:
        wl_ok, wl_note = True, ("active 观察池均无前一交易日K线，观察池层无法判定"
                                "（跳过，仅指数锚点判定）")
    else:
        wl_ok = not cov.outage
        wl_note = (f"覆盖率 {cov.covered}/{cov.tracked}（{cov.ratio:.0%}），"
                   f"停牌 {cov.suspended} 只，断供 {len(cov.missing)} 只")
    ok = index_ok and wl_ok
    detail = (f"指数层：沪深300 最新 {last or '无数据'}"
              f"（{'PASS' if index_ok else 'FAIL'}，期望 {day}）；"
              f"观察池层：{wl_note}（{'PASS' if wl_ok else 'FAIL'}）"
              + ("" if ok else "；行情未覆盖到当日"))
    evidence = [f"覆盖分母=前一日({cov.prev_day or '—'})有K线的 active，"
                f"软删(removed 等)不计入"]
    if cov.suspended_list:
        evidence.append("停牌：" + "、".join(cov.suspended_list[:8]))
    if cov.missing:
        evidence.append("断供（前一日有、当日无、无停牌证据）："
                        + "、".join(cov.missing[:8]))
    if cov.no_history:
        evidence.append("无历史K线（不计入覆盖分母）："
                        + "、".join(cov.no_history[:8]))
    return Check("market", "行情最新交易日 = 当日", ok, detail,
                 "；".join(evidence))


def check_sector(day: str, market_db: str = MARKET_DB_PATH) -> Check:
    """② 板块快照完整（complete=1）且日期 = 当日"""
    try:
        with _connect(market_db) as conn:
            meta = conn.execute(
                "SELECT complete, source, plate_count, concept_count, note "
                "FROM sector_snapshot_meta WHERE date=?", (day,),
            ).fetchone()
            rows = conn.execute(
                "SELECT COUNT(*) AS n FROM sector_daily WHERE date=?", (day,),
            ).fetchone()["n"]
    except sqlite3.Error as exc:
        return Check("sector", "板块快照完整且日期 = 当日", False,
                     f"读取板块表失败：{exc}")
    if meta is None:
        return Check("sector", "板块快照完整且日期 = 当日", False,
                     f"{day} 无 sector_snapshot_meta（板块快照缺失）",
                     f"sector_daily 行数 {rows}")
    ok = bool(meta["complete"]) and rows > 0
    detail = (f"complete={int(bool(meta['complete']))}，"
              f"plate={meta['plate_count']} concept={meta['concept_count']}，"
              f"sector_daily={rows}"
              + ("" if ok else f"；{meta['note'] or '快照不完整'}"))
    return Check("sector", "板块快照完整且日期 = 当日", ok, detail,
                 f"source={meta['source'] or '—'}，日期键={day}")


def check_decisions(day: str, knowledge_db: str = DB_PATH) -> Check:
    """③ 当日决策记录存在；全为弃权时原因不得为空"""
    try:
        rows = DecisionStore(knowledge_db).rows_by_date(day)
    except (sqlite3.Error, OSError) as exc:
        return Check("decision", "当日决策记录存在（弃权须写原因）", False,
                     f"读取决策库失败：{exc}")
    if not rows:
        return Check("decision", "当日决策记录存在（弃权须写原因）", False,
                     f"{day} 无 decision_log 记录（收盘闭环未留痕）")
    abstain = [r for r in rows if r.get("status") == "ABSTAIN"
               or r.get("decision") == "NO_CANDIDATE"]
    observe = [r for r in rows if r.get("decision") == "OBSERVE"]
    empty_reason = [r for r in abstain if not (r.get("reason") or "").strip()]
    all_abstain = len(abstain) == len(rows)
    ok = not (all_abstain and empty_reason)
    detail = f"决策 {len(rows)} 条（弃权 {len(abstain)} 条"
    if observe:
        detail += f"，观察 {len(observe)} 条"
    detail += "）"
    if empty_reason:
        names = "、".join(str(r.get("symbol") or "—") for r in empty_reason[:5])
        detail += f"；弃权未写原因：{names}"
        if not all_abstain:
            detail += "（已有非弃权记录，不单独判失败）"
    elif not all_abstain:
        detail += "；含建仓/观察记录"
    return Check("decision", "当日决策记录存在（弃权须写原因）", ok, detail,
                 "来源 decision_log（knowledge.db）")


def run_audit(day: str | None = None, *, market_db: str = MARKET_DB_PATH,
              knowledge_db: str = DB_PATH, out_dir: Path | None = None,
              write: bool = True) -> AuditResult:
    """执行三条硬检查并写日报；返回结果（ok=False 即 DEGRADED）"""
    day = day or date.today().isoformat()
    res = AuditResult(day=day, ok=True)
    if not _is_trading_day(day, market_db):
        res.skipped = "非交易日，三条硬检查不适用"
        res.checks = []
        if write:
            res.report_path = str(_write_report(res, out_dir))
        return res
    res.checks = [
        check_market(day, market_db),
        check_sector(day, market_db),
        check_decisions(day, knowledge_db),
    ]
    res.ok = all(c.ok for c in res.checks)
    if write:
        res.report_path = str(_write_report(res, out_dir))
    return res


def _write_report(res: AuditResult, out_dir: Path | None = None) -> Path:
    """写 outputs/收盘报表/硬检查_YYYY-MM-DD.md（同日覆盖，幂等）"""
    out = Path(out_dir) if out_dir else AUDIT_DIR
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"硬检查_{res.day}.md"
    lines = [
        f"# 每日硬检查（{res.day}）",
        "",
        f"> 结论：**{'PASS' if res.ok else 'DEGRADED'}**"
        + (f"（{res.skipped}）" if res.skipped else ""),
        f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}；"
        "任一硬检查不满足即 DEGRADED，由 run_daily 告警并把 close 记 DEGRADED。",
        "",
    ]
    if res.checks:
        lines += ["| 检查 | 结果 | 说明 | 证据 |", "|---|---|---|---|"]
        for c in res.checks:
            lines.append(
                f"| {c.title} | {'PASS' if c.ok else 'FAIL'} | {c.detail} | {c.evidence or '—'} |")
        lines.append("")
    else:
        lines += [f"- {res.skipped}", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    log.info("每日硬检查 %s：%s（%s）", res.day,
             "PASS" if res.ok else "DEGRADED", path)
    return path


EXIT_OK, EXIT_DEGRADED, EXIT_CRASH = 0, 2, 3


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="每日硬检查（收盘收口）")
    parser.add_argument("--date", default=None, help="检查日期 YYYY-MM-DD（默认今天）")
    parser.add_argument("--market-db", default=MARKET_DB_PATH, help="行情库路径")
    parser.add_argument("--knowledge-db", default=DB_PATH, help="知识库路径")
    parser.add_argument("--out", default=None, help="日报文件路径（默认 outputs/收盘报表/）")
    parser.add_argument("--no-write", action="store_true", help="只检查不写日报")
    args = parser.parse_args(argv)

    out_path = Path(args.out) if args.out else None
    res = run_audit(
        args.date, market_db=args.market_db, knowledge_db=args.knowledge_db,
        out_dir=out_path.parent if out_path else None, write=not args.no_write,
    )
    if out_path and not args.no_write:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(Path(res.report_path).read_text(encoding="utf-8"),
                            encoding="utf-8")
        res.report_path = str(out_path)
    for c in res.checks:
        print(f"  {'PASS' if c.ok else 'FAIL'} {c.title}：{c.detail}")
    if res.skipped:
        print(f"  SKIP {res.skipped}")
    print(f"结论：{'PASS' if res.ok else 'DEGRADED'}"
          + (f"（日报 {res.report_path}）" if res.report_path else ""))
    return EXIT_OK if res.ok else EXIT_DEGRADED


def main(argv: list[str] | None = None) -> int:
    """入口：未捕获异常转为 EXIT_CRASH（3），不落 1（易被当成"仅警告"而 fail-open）"""
    try:
        return _main(argv)
    except SystemExit:
        raise                     # argparse 的 --help / 参数错误保持原语义
    except KeyboardInterrupt:
        raise
    except Exception:
        traceback.print_exc()
        print("每日硬检查自身崩溃（未捕获异常）：本次检查结论不可信、日报可能缺失，"
              "调用方须按 DEGRADED 处理", file=sys.stderr)
        return EXIT_CRASH


if __name__ == "__main__":
    sys.exit(main())
