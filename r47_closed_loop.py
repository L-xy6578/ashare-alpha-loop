"""
R47 真实闭环 — 交易员数字化后的完整三角闭环

与 R46 的区别：交易员环节用 TradeExecutor 做真实决策
（数据质量门槛 / 仓位管控 / 先减后买 / 100股取整），不再只是记录。

数据：TencentHttpDataSource 自动拉取（零依赖），补拉行情自动落库 market.db
流程：数据自助 → 预检 → 调查员评估 → 交易员决策(TradeExecutor) → 评审员复盘回写
观察池：单一数据源 watchlist 表（src.watchlist），不再硬编码
评分：五因子数据化（行业/技术/资金/估值由真实数据计算，业绩保留调研评估+证据）

运行:
    python r47_closed_loop.py              # 正常闭环（写生产库）
    python r47_closed_loop.py --dry-run    # 演练：临时库副本，不写生产库
    python r47_closed_loop.py --no-valuation  # 跳过估值接口（视为数据缺失）
"""

import argparse
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from src.regime import Regime, detect_regime
from src.config import DB_PATH, MARKET_DB_PATH
from src.data_source import (
    DataSource,
    KlineBar,
    TdxDataSource,
    TencentHttpDataSource,
    fetch_valuation_pe_pb,
)
from src.dates import last_trading_day, recent_window
from src.decision_log import (
    CandidateInput,
    DecisionStore,
    append_csv,
    decision_kpi,
    make_decision_row,
    make_excluded_row,
    make_no_candidate_row,
    render_day_markdown,
    render_decision_kpi,
    write_day_markdown,
)
from src.decision_settle import is_managed, settle
from src.executable import filter_universe
from src.factor_store import FactorSnapshotStore
from src.forecast import generate_daily as forecast_generate_daily
from src.forecast import settle_due as forecast_settle_due
from src.forecast import write_hit_rate_report as forecast_write_hit_rate
from src.factors import (
    _resolve_plate,
    factor_benchmark,
    growth_from_earnings,
    industry_pool,
    score_candidate,
)
from src.kpi import KpiTracker
from src.knowledge import KnowledgeBase
from src.logger import get_logger
from src.pipeline.collector import Collector
from src.portfolio import PortfolioStore
from src.research import ResearchStore
from src.sector_analysis import SectorAnalyzer
from src.strategy_loop import apply_strategy_actions, select_strategy
from src.trader import TradeExecutor, Position
from src.trim_rules import MIRROR_MARK, SELL_LINE_MARK, apply_sell_line_rules, apply_trim_rules
from src.vcp import detect_vcp
from src.watchlist import WatchlistStore

log = get_logger(__name__)

# 动态日期窗口：最近 20 个交易日（收盘自动任务运行时自动含当日数据）
START, END = recent_window(days_back=20)
# 数据新鲜度门禁基准：END 当日或之前最近的交易日（trade_calendar 口径）。
# 非交易日手动运行时，期望数据日 = 最近交易日，而不是自然日。
EXPECTED_DATA_DATE = last_trading_day(END)

def _load_from_market_db(start: str, end: str, market_db: str) -> dict[str, list[KlineBar]]:
    """从 market.db 读取窗口内已采集数据（优先数据源：管道采集+连接器注入的真实行情）"""
    from src.pipeline.schema import init_market_db

    out: dict[str, list[KlineBar]] = {}
    with sqlite3.connect(market_db) as conn:
        conn.row_factory = sqlite3.Row
        init_market_db(conn)
        rows = conn.execute(
            """SELECT symbol, date, open, close, high, low, volume, amount
               FROM daily_bars WHERE date BETWEEN ? AND ? ORDER BY date""",
            (start, end),
        ).fetchall()
    for r in rows:
        out.setdefault(r["symbol"], []).append(
            KlineBar(r["date"], r["open"], r["close"], r["high"], r["low"],
                     volume=r["volume"], amount=r["amount"])
        )
    return out


def _git_state() -> tuple[str, bool]:
    """当前 git HEAD 短哈希与工作区是否有未提交改动（决策记录留痕用）"""
    import subprocess
    try:
        head = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True,
            stderr=subprocess.DEVNULL, timeout=10).strip()
        dirty = bool(subprocess.check_output(
            ["git", "status", "--porcelain"], text=True,
            stderr=subprocess.DEVNULL, timeout=10).strip())
        return head, dirty
    except Exception:
        return "", False


def _load_vcp_history(market_db: str, symbols: set[str],
                      limit: int = 120) -> dict[str, list[KlineBar]]:
    """读取最近 limit 根日K（VCP 波动收缩需要比 20 日窗口更长的历史）"""
    from src.pipeline.schema import init_market_db

    out: dict[str, list[KlineBar]] = {}
    with sqlite3.connect(market_db) as conn:
        conn.row_factory = sqlite3.Row
        init_market_db(conn)
        for sym in sorted(symbols):
            rows = conn.execute(
                """SELECT date, open, close, high, low, volume, amount
                   FROM daily_bars WHERE symbol=? ORDER BY date DESC LIMIT ?""",
                (sym, limit),
            ).fetchall()
            out[sym] = [
                KlineBar(r["date"], r["open"], r["close"], r["high"], r["low"],
                         volume=r["volume"], amount=r["amount"])
                for r in reversed(rows)
            ]
    return out


def _listed_days(market_db: str, symbols: set[str]) -> dict[str, int]:
    """本地历史K线总数（用于次新股判定；不足 60 根视为无法确认）"""
    from src.pipeline.schema import init_market_db

    out: dict[str, int] = {}
    with sqlite3.connect(market_db) as conn:
        init_market_db(conn)
        for sym in symbols:
            out[sym] = int(conn.execute(
                "SELECT COUNT(*) FROM daily_bars WHERE symbol=?", (sym,),
            ).fetchone()[0])
    return out


VCP_RATE_HEADER = """# VCP 通过率逐日记录

> 数据源：`decision_log` 当日 `decision IN ('ENTRY','ABSTAIN','OBSERVE')` 的行（即进入五因子评分段的标的）。
> 2026-11-11 起评分段记录统一为 `OBSERVE`（五因子选股退役），统计口径加入 OBSERVE 保证不断档。
> 只做统计与记录，不参与任何门槛判定，不改 VCP 参数；每个不成立标的可同时计入多条原因。
> 连续 5 个交易日通过率为 0 时在当日行标出「需人工复核」，用于区分"行情确实没有收缩形态"与"判定过严"。

| 日期 | 评分段 | VCP 成立 | 不成立 | 通过率 | 箱体未递减 | 量能比超标 | 分位越界 | 标记 |
|---|--:|--:|--:|--:|--:|--:|--:|---|
"""


def _vcp_rate_stats(kb_db: str, day: str) -> dict | None:
    """从 decision_log 统计某日 VCP 通过情况（仅评分段 ENTRY/ABSTAIN/OBSERVE）

    只做统计，不改任何 VCP 参数。每个不成立标的可同时计入多条原因。
    """
    with sqlite3.connect(kb_db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT vcp, reason FROM decision_log
               WHERE decision_date=? AND decision IN ('ENTRY','ABSTAIN','OBSERVE')""",
            (day,),
        ).fetchall()
    if not rows:
        return None
    ok = sum(1 for r in rows if r["vcp"])
    reasons = [r["reason"] or "" for r in rows]
    return {
        "n": len(rows), "ok": ok,
        "box": sum(1 for t in reasons if "回撤未逐次变浅" in t),
        "vol": sum(1 for t in reasons if "最后一次收缩量能比" in t),
        "sec": sum(1 for t in reasons if "板块分位" in t),
    }


def _write_vcp_pass_rate(kb_db: str, end: str, keep_days: int = 20) -> Path | None:
    """写 VCP 通过率逐日记录（outputs/收盘报表/VCP通过率_逐日.md，整表重写、可重复运行）

    连续 5 个交易日通过率为 0 时在当日行标出，作为人工复核信号
    （区分"行情确实没有收缩形态"与"判定过严"）。
    """
    with sqlite3.connect(kb_db) as conn:
        days = [r[0] for r in conn.execute(
            """SELECT DISTINCT decision_date FROM decision_log
               WHERE decision IN ('ENTRY','ABSTAIN','OBSERVE')
               ORDER BY decision_date DESC LIMIT ?""", (keep_days,))]
    rows = [(d, _vcp_rate_stats(kb_db, d)) for d in sorted(days)]
    rows = [(d, s) for d, s in rows if s]
    if not rows:
        return None
    body = []
    for i, (d, s) in enumerate(rows):
        flag = ""
        if d == end:
            last5 = [x[1] for x in rows[max(0, i - 4): i + 1]]
            if len(last5) == 5 and all(x["ok"] == 0 for x in last5):
                flag = "⚠️ 连续 5 个交易日通过率为 0，需人工复核"
            elif s["ok"] == 0:
                flag = "当日通过率为 0"
        body.append(
            f"| {d} | {s['n']} | {s['ok']} | {s['n'] - s['ok']} | "
            f"{s['ok'] / s['n'] * 100:.0f}% | {s['box']} | {s['vol']} | {s['sec']} | {flag} |")
    out = Path("outputs") / "收盘报表"
    out.mkdir(parents=True, exist_ok=True)
    path = out / "VCP通过率_逐日.md"
    path.write_text(VCP_RATE_HEADER + "\n".join(body) + "\n", encoding="utf-8")
    return path


def _load_index_history(market_db: str) -> list[KlineBar]:
    """读取指数（sz399006）全量日K，用于市场状态判定"""
    from src.pipeline.schema import init_market_db

    with sqlite3.connect(market_db) as conn:
        conn.row_factory = sqlite3.Row
        init_market_db(conn)
        rows = conn.execute(
            """SELECT date, open, close, high, low, volume, amount
               FROM daily_bars WHERE symbol='sz399006' ORDER BY date""",
        ).fetchall()
    return [KlineBar(r["date"], r["open"], r["close"], r["high"], r["low"],
                     volume=r["volume"], amount=r["amount"]) for r in rows]


def _prepare_db_dir(dry_run: bool, db_dir: str | None) -> tuple[str, str, bool]:
    """返回 (knowledge_db, market_db, is_dry)

    - 正常模式：直接用生产库路径
    - dry-run/db_dir：复制生产库到临时目录，演练不污染生产数据
    """
    if not dry_run and not db_dir:
        return DB_PATH, MARKET_DB_PATH, False
    if db_dir:
        path = Path(db_dir)
        path.mkdir(parents=True, exist_ok=True)
    else:
        path = Path(tempfile.mkdtemp(prefix="r47_dryrun_"))
    for src, dst in ((DB_PATH, path / "knowledge.db"), (MARKET_DB_PATH, path / "market.db")):
        if Path(src).exists() and not (path / dst.name).exists():
            _checkpoint(src)   # 先把 WAL 折叠进主库，避免复制出的临时库缺最新数据
            shutil.copy2(src, dst)
    return str(path / "knowledge.db"), str(path / "market.db"), True


def _fetch_with_fallback(sym: str, start: str, end: str,
                         net: DataSource, tdx: TdxDataSource | None
                         ) -> tuple[list[KlineBar], str]:
    """腾讯优先，失败/空时用通达信兜底（当日K线延迟问题双保险）

    Returns:
        (bars, adjust)——adjust 标记实际来源复权口径（#84），落库时按来源标注
    """
    try:
        bars = net.daily_kline(sym, start, end)
    except Exception:
        bars = []
    if bars:
        return bars, net.adjust_for(sym)
    if not bars and tdx is not None:
        try:
            bars = tdx.daily_kline(sym, start, end)
        except Exception:
            bars = []
        if bars:
            log.info("通达信兜底补拉 %s: %d 根（不复权）", sym, len(bars))
            return bars, tdx.adjust_for(sym)
    return [], "unknown"


def _checkpoint(db_path: str) -> None:
    """把 SQLite WAL 内容折叠回主库（复制/归档前保证数据完整）"""
    try:
        with sqlite3.connect(db_path, timeout=5) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        log.warning("WAL checkpoint 失败（可能被占用）：%s，按现有主库复制", db_path)


def _refresh_finance_evidence(symbols: list[str]) -> tuple[int, int]:
    """财报自动采集（业绩趋势数据自给）：拉最新一期财报写入调研库，返回(新增,跳过)"""
    from src.finance_feed import inject

    return inject(symbols=symbols)


def _candidates_markdown(end: str, rows: list[dict], notes: list[str] | None = None,
                         excluded: list[dict] | None = None) -> str:
    """当日候选池评分报告（五因子分项 + 缺失因子 + 强制未达标原因）"""
    weights = "行业25% 业绩25% 估值20% 技术15% 资金15%"
    lines = [
        f"# 选股结果（{end}）",
        "",
        f"> 数据化五因子：{weights}；加权 ≥85 且无缺失因子 = 达标（历史口径自动登记 KPI）。",
        "> 任因缺失即强制不达标（管住手，不猜不补）。",
        "> 策略路线（2026-11-11 起）：五因子选股已退役、转指数化——本表仅作观察记录，"
        "不产生建仓建议，建仓口径见指数化组合（收益报表）。",
        "> 本文件由 R47 收盘闭环（15:35 自动任务）生成；同日重跑覆盖，不追加。",
    ]
    for n in notes or []:
        lines.append(f"> ⚠️ 数据质量提示：{n}")
    lines += [
        "",
        "## 一、候选评分",
        "",
        "| 标的 | 代码 | 板块 | 总分 | 行业 | 业绩 | 估值 | 技术 | 资金 | 缺失因子 | 判定 | 动作/状态 |",
        "|------|------|------|:---:|:---:|:---:|:---:|:---:|:---:|------|:---:|------|",
    ]
    ordered = sorted(
        rows, key=lambda r: (r.get("result") is None, -(r["result"].weighted if r.get("result") else 0)))
    missing_counts: dict[str, int] = {}
    n_rec = n_wait = n_hold = n_nodata = 0
    for r in ordered:
        res = r.get("result")
        if res is None:
            lines.append(f"| {r['name']} | {r['code']} | {r['sector'] or '—'} | — | — | — | — | — | — | — | — | {r['status']} |")
            if "已持仓" in r["status"]:
                n_hold += 1
            else:
                n_nodata += 1
            continue
        miss = list(res.missing)
        for m in miss:
            missing_counts[m] = missing_counts.get(m, 0) + 1

        def cell(value: float, label: str) -> str:
            return "—" if label in miss else f"{value:.0f}"

        verdict = "✅达标" if res.recommended else "⛔未达标"
        lines.append(
            f"| {res.name} | {r['code']} | {r['sector'] or '—'} | {res.weighted:.1f} | "
            f"{cell(res.factors.industry, '行业景气')} | {cell(res.factors.growth, '业绩证据')} | "
            f"{cell(res.factors.valuation, '估值')} | {cell(res.factors.technical, '技术面')} | "
            f"{cell(res.factors.capital, '资金面')} | {'、'.join(miss) or '—'} | {verdict} | {r['status']} |")
        if res.recommended:
            n_rec += 1
        elif "策略买入信号" in r["status"] or "等待" in r["status"]:
            n_wait += 1

    analysis_only = [e for e in (excluded or []) if e.get("analysis_only")]
    other_excluded = [e for e in (excluded or []) if not e.get("analysis_only")]

    lines += ["", "## 二、仅分析不可买（板块权限限制）", ""]
    if analysis_only:
        lines += ["| 标的 | 代码 | 排除原因 |", "|------|------|------|"]
        lines += [f"| {e['name']} | {e['code']} | {e['reason']} |" for e in analysis_only]
    else:
        lines.append("- 无（当日观察池未出现创业板/科创板/北交所标的）")

    lines += ["", "## 三、其他可执行性排除（排序前排除，不静默丢弃）", ""]
    if other_excluded:
        lines += ["| 标的 | 代码 | 排除原因 |", "|------|------|------|"]
        lines += [f"| {e['name']} | {e['code']} | {e['reason']} |" for e in other_excluded]
    else:
        lines.append("- 无")

    lines += ["", "## 四、强制未达标原因汇总", ""]
    if missing_counts:
        lines.append(f"- 因子缺失（任一缺失即强制未达标）：{sum(missing_counts.values())} 项次")
        for k in sorted(missing_counts):
            lines.append(f"  - 缺{k}：{missing_counts[k]} 只")
    else:
        lines.append("- 无因子缺失")
    lines.append(f"- 无行情数据/无业绩证据休眠：{n_nodata} 只")
    lines += [
        "",
        "## 五、结论",
        "",
        f"- 观察池 {len(rows) + len(excluded or [])} 只：达标 {n_rec} 只 / 待策略信号 {n_wait} 只 / 已持仓 {n_hold} 只 / 数据缺失 {n_nodata} 只 / 仅分析不可买 {len(analysis_only)} 只 / 其他排除 {len(other_excluded)} 只",
        "- 策略路线（2026-11-11 起）：五因子选股退役、转指数化——以上全部为观察记录，不构成建仓建议。",
        "- 交易权限：只能买主板（600/601/603/605、000/001/002/003）；创业板/科创板/北交所仅分析不可买。",
        "- 其他排除：股价 >60 元、ST、停牌、上市不足 60 个交易日等（详见第三节）。",
        "",
        "---",
        "*R47 收盘闭环自动生成。不构成投资建议。*",
    ]
    return "\n".join(lines) + "\n"


def _write_candidates_report(end: str, rows: list[dict], out_dir: Path | None = None,
                             notes: list[str] | None = None,
                             excluded: list[dict] | None = None) -> Path:
    """写入当日选股结果（同日覆盖，幂等）"""
    out_dir = out_dir or (Path("outputs") / "选股")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"选股结果_{end}.md"
    path.write_text(_candidates_markdown(end, rows, notes, excluded),
                    encoding="utf-8")
    return path


def _run_shadow_tracking(is_dry: bool, warnings: list[str]) -> None:
    """收盘闭环可选最后一步：影子跟踪（失败只降级为告警，不影响退出码）"""
    print("\n【6.5】影子跟踪（纯观察 · 不建仓）")
    print("-" * 70)
    if is_dry:
        print("  （dry-run 跳过：不写影子产物）")
        return
    try:
        from src.shadow_track import backfill

        summary = backfill()
    except Exception as exc:
        warnings.append(f"影子跟踪更新失败（不影响闭环）: {exc}")
        print(f"  ⚠️ 影子跟踪更新失败: {exc}（后续步骤不受影响）")
        return
    filled = summary.get("filled") or []
    print(f"  补齐 {len(filled)} 个交易日 / {summary.get('rows', 0)} 行；"
          f"报告日 {summary.get('report_day')}")
    print(f"  已补：{', '.join(filled) or '无（没有缺失交易日）'}")
    if summary.get("overflow"):
        print(f"  ⏳ 超出单次上限，下次继续：{', '.join(summary['overflow'])}")
    for f in summary.get("failed") or []:
        warnings.append(f"影子跟踪 {f['date']} 补齐失败: {f['error']}")
        print(f"  ⚠️ {f['date']} 补齐失败: {f['error']}")
    for d, names in (summary.get("missing") or {}).items():
        print(f"  ⚠️ {d} 未取到：{'、'.join(names)}（已按缺口记录）")

    # 全候选面板（主要证据来源）：读 factor_snapshot / decision_log / daily_bars，不重复采集
    try:
        from src.shadow_track import fill_returns, record_panel

        panel = record_panel()
        new_ret = fill_returns()
        if panel:
            print(f"  候选面板 {panel[0]['day']}：{len(panel)} 行（append-only）")
        print(f"  到期收益回填：新增 {len(new_ret)} 条（5/10/20 日）")
    except Exception as exc:
        warnings.append(f"候选面板登记失败（不影响闭环）: {exc}")
        print(f"  ⚠️ 候选面板登记失败: {exc}（后续步骤不受影响）")


def _run_lockup_watch(items, is_dry: bool, warnings: list[str],
                      provider=None, market_db: str | None = None) -> None:
    """收盘闭环可选最后一步：解禁提示（只读观察层 · 不参与任何决策）

    - 判定结果只写 market.db.lockup_watch + outputs/收盘报表/解禁提示_YYYY-MM-DD.md；
    - 失败只进 warnings，不影响退出码（与影子跟踪一致）；
    - dry-run 且未注入 provider 时跳过（不访问外部接口、不写产物）。
    """
    print("\n【6.6】解禁提示（只读观察层 · 不影响决策）")
    print("-" * 70)
    if is_dry and provider is None:
        print("  （dry-run 跳过：不访问外部接口、不写解禁记录）")
        return
    try:
        from src import lockup_watch

        result = lockup_watch.run(items, END, is_dry=is_dry,
                                  warnings=warnings, provider=provider,
                                  market_db=market_db)
    except Exception as exc:
        warnings.append(f"解禁提示失败（不影响闭环）: {exc}")
        print(f"  ⚠️ 解禁提示失败: {exc}（后续步骤不受影响）")
        return
    rows = result.get("rows") or []
    hit = [r for r in rows if r["has_upcoming"]]
    miss = [r for r in rows if r["has_upcoming"] is None]
    print(f"  观察池 {len(rows)} 只 | 未来 {lockup_watch.HORIZON_DAYS} 日内有解禁 {len(hit)} 只 | "
          f"数据缺失 {len(miss)} 只")
    for r in hit:
        print(f"    {r['name']}({r['symbol']}): {r['nearest_date']} 解禁 "
              f"{r['nearest_ratio_pct']}% 流通股 → {r['risk_level']}"
              f"（{', '.join(r['signals']) or '—'}）")
    if is_dry:
        print("  （dry-run 不写解禁记录/报表）")
    else:
        print(f"  📄 已写入 {result.get('path')}（库内 {result.get('written', 0)} 条）")
        print("  仅提示，不影响评分与建仓判定")


def _run_hotmoney_watch(items, is_dry: bool, warnings: list[str],
                        provider=None, market_db: str | None = None) -> None:
    """收盘闭环可选最后一步：游资提示（只读观察层 · 不参与任何决策）

    - 判定结果只写 market.db.hotmoney_watch + outputs/收盘报表/游资提示_YYYY-MM-DD.md；
    - 失败只进 warnings，不影响退出码（与解禁提示一致）；
    - dry-run 且未注入 provider 时跳过（不访问外部接口、不写产物）。
    """
    print("\n【6.7】游资提示（只读观察层 · 不影响决策）")
    print("-" * 70)
    if is_dry and provider is None:
        print("  （dry-run 跳过：不访问外部接口、不写游资记录）")
        return
    try:
        from src import hot_money_watch

        result = hot_money_watch.run(items, END, is_dry=is_dry, warnings=warnings,
                                     provider=provider, market_db=market_db)
    except Exception as exc:
        warnings.append(f"游资提示失败（不影响闭环）: {exc}")
        print(f"  ⚠️ 游资提示失败: {exc}（后续步骤不受影响）")
        return
    rows = result.get("rows") or []
    hit = [r for r in rows if r["signals"]]
    miss = [r for r in rows if r["risk_level"] == hot_money_watch.LEVEL_MISSING]
    print(f"  观察池 {len(rows)} 只 | 触发风险信号 {len(hit)} 只 | 数据不足 {len(miss)} 只")
    for r in hit:
        print(f"    {r['name']}({r['symbol']}): {', '.join(r['signals'])} → {r['risk_level']}")
    if is_dry:
        print("  （dry-run 不写游资记录/报表）")
    else:
        print(f"  📄 已写入 {result.get('path')}（库内 {result.get('written', 0)} 条）")
        print("  仅提示，不影响评分与建仓判定")


def _run_macro_regime(is_dry: bool, warnings: list[str],
                      provider=None, market_db: str | None = None) -> None:
    """收盘闭环可选最后一步：宏观仓位提示（只读观察层 · 不参与任何决策）

    - 判定结果只写 market.db.macro_regime + outputs/收盘报表/宏观仓位提示_YYYY-MM-DD.md；
    - 宏观是市场级判定（不逐标的），故不对 items 循环；
    - 失败只进 warnings，不影响退出码（与解禁/游资提示一致）；
    - dry-run 且未注入 provider 时跳过（不访问外部接口、不写产物）。
    """
    print("\n【6.8】宏观仓位提示（只读观察层 · 不影响决策）")
    print("-" * 70)
    if is_dry and provider is None:
        print("  （dry-run 跳过：不访问宏观接口、不写宏观记录）")
        return
    try:
        from src import macro_regime

        result = macro_regime.run(END, is_dry=is_dry, warnings=warnings,
                                  provider=provider, market_db=market_db)
    except Exception as exc:
        warnings.append(f"宏观仓位提示失败（不影响闭环）: {exc}")
        print(f"  ⚠️ 宏观仓位提示失败: {exc}（后续步骤不受影响）")
        return
    rows = result.get("rows") or []
    row = rows[0] if rows else {}
    if row.get("score") is None:
        print(f"  宏观档位：{row.get('level', '数据不足')}（{row.get('note', '')}）")
    else:
        print(f"  宏观评分 {row['score']}/100（覆盖权重 {row['weight_covered']}/100）"
              f" → {row['level']} | 系数 {row['cap_ratio']}"
              f" → 建议上限 {row['suggest_cap']:,} 元")
        print(f"  缺失项：{'、'.join(row.get('missing') or []) or '无'}")
    if is_dry:
        print("  （dry-run 不写宏观记录/报表）")
    else:
        print(f"  📄 已写入 {result.get('path')}（库内 {result.get('written', 0)} 条）")
        print("  仅提示；实际下单仍执行既有硬上限 18,000 元")


def _run_sector_reversal_shadow(is_dry: bool, warnings: list[str],
                                market_db: str | None = None) -> None:
    """收盘闭环最后一步：板块反转影子台账（只记录 · 结构上不可能影响决策）

    - 挂在决策落库（4.5）与全部观察层之后，只写 market.db.sector_reversal_shadow
      与 outputs/板块反转影子/，不读不回写任何决策数据；
    - 失败只进 warnings，不影响退出码；
    - dry-run 跳过（不写库、不落盘）。
    """
    print("\n【6.9】板块反转影子台账（只记录 · 不影响决策）")
    print("-" * 70)
    if is_dry:
        print("  （dry-run 跳过：不采集、不写影子台账）")
        return
    try:
        from src.shadow_collect import collect as collect_shadow

        cs = collect_shadow(market_db=market_db)
        if cs.get("skipped"):
            print(f"  ℹ️ 样本日K未采集：{cs.get('reason')}")
        else:
            print(f"  样本日K补采：{cs.get('ok', 0)}/{cs.get('symbols', 0)} 只成功，"
                  f"请求 {cs.get('requests', 0)} 次，写入 {cs.get('rows', 0)} 行，"
                  f"耗时 {cs.get('elapsed_sec', 0)}s，失败 {cs.get('failed', 0)} 只")
            if cs.get("failed"):
                warnings.append(
                    f"影子台账样本日K失败 {cs['failed']} 只（不影响闭环）")
    except Exception as exc:
        warnings.append(f"影子台账样本日K采集失败（不影响闭环）: {exc}")
        print(f"  ⚠️ 样本日K采集失败: {exc}")
    try:
        from src.sector_reversal_shadow import run as run_shadow

        result = run_shadow(market_db=market_db)
    except Exception as exc:
        warnings.append(f"板块反转影子台账失败（不影响闭环）: {exc}")
        print(f"  ⚠️ 板块反转影子台账失败: {exc}（后续步骤不受影响）")
        return
    rec, bf = result.get("record") or {}, result.get("backfill") or {}
    if rec.get("skipped"):
        print(f"  ℹ️ 未记录：{rec.get('reason')}")
    else:
        print(f"  记录 {rec.get('day')}：{rec.get('rows', 0)} 行"
              f"（新增 {rec.get('inserted', 0)}，同日重跑不重复写）")
    print(f"  回填：检查 {bf.get('checked', 0)} 行，更新 {bf.get('updated', 0)} 行，"
          f"基准 {bf.get('benchmark', '无基准')}")
    if result.get("report"):
        print(f"  📄 已写入 {result['report']}")


def _run_index_portfolio(day: str, is_dry: bool, warnings: list[str],
                         market_db: str | None = None) -> None:
    """收盘闭环：指数化组合（sim 记账 · 真实 ETF 收盘价）

    - 五因子选股退役后的记账口径：配置写死三只 ETF、一次建满、季度再平衡；
    - 与 knowledge.db 完全分离（不写 real 口径），只记"该买什么、值多少钱"；
    - 失败只进 warnings，不影响退出码；dry-run 跳过。
    """
    if is_dry:
        print("\n【6.10】指数化组合（sim 记账 · 真实 ETF 收盘价）")
        print("-" * 70)
        print("  （dry-run 跳过：不写指数组合库）")
        return
    try:
        from src.index_portfolio import run as run_index

        run_index(day, is_dry=is_dry, warnings=warnings,
                  market_db=market_db or MARKET_DB_PATH)
    except Exception as exc:
        warnings.append(f"指数化组合更新失败（不影响闭环）: {exc}")
        print(f"  ⚠️ 指数化组合更新失败: {exc}（后续步骤不受影响）")


def _snapshot_quality_note(snapshot_info: dict) -> str:
    """B5：选股结果头部质量提示必须带快照日期与 source（避免旧快照条数被当成当日全量）"""
    if not snapshot_info.get("date"):
        return ""
    detail = snapshot_info.get("note") or (
        f"plate={snapshot_info.get('plate_count', 0)} / "
        f"concept={snapshot_info.get('concept_count', 0)}")
    return (f"板块快照 {snapshot_info['date']} · "
            f"source={snapshot_info.get('source') or '未知'} · {detail}")


def _ensure_current_sector_snapshot(sector, end: str, is_dry: bool) -> list[str]:
    """B3：收盘阶段保证有当日板块快照；缺失/滞后时自动补拉一次

    - 已是最新 → 直接返回；
    - dry-run → 只提示、不写库；
    - 补拉失败 → 不抛异常（沿用旧快照，告警由调用方的滞后检查负责）。

    Returns: 补拉后的可用快照日期列表（DESC，同 SectorAnalyzer.available_dates）
    """
    dates = sector.available_dates()
    latest = dates[0] if dates else ""
    if latest and latest >= end:
        return dates
    print(f"  ⚠️ 当日板块快照缺失（最新 {latest or '无'}），尝试自动补拉 {end} …")
    if is_dry:
        print("  （dry-run 跳过补拉：不写生产库）")
        return dates
    try:
        from src.sector_feed import inject as inject_sector

        rc = inject_sector(trade_date=end)
    except Exception as exc:
        rc = 1
        print(f"  ⚠️ 板块快照自动补拉异常: {exc}")
    dates = sector.available_dates()
    latest = dates[0] if dates else ""
    if rc == 0 and latest == end:
        print(f"  ✅ 已自动补拉 {end} 板块快照")
    else:
        print(f"  ⚠️ 板块快照自动补拉未成功（{end}），沿用 {latest or '无快照'}")
    return dates


def _ensure_bars(symbols, klines: dict, net: DataSource,
                 tdx: TdxDataSource | None, collector: Collector,
                 warnings: list[str]) -> None:
    """保证窗口内日K齐备：缺数据（<10根）或最新K线早于截止日 → 补拉并落库

    收盘阶段换血新增的标的也走同一入口，保证当日即可参与评分。
    """
    for sym in sorted(symbols):
        bars = klines.get(sym, [])
        stale = not bars or bars[-1].date < END
        if len(bars) < 10 or stale:
            # #47：窗口内不足 10 根时从窗口起点补拉，保证技术面因子有足够K线
            fetch_start = START if len(bars) < 10 else bars[-1].date
            try:
                fetched, fetch_adjust = _fetch_with_fallback(
                    sym, fetch_start, END, net, tdx)
            except Exception as e:
                warnings.append(f"{sym}: 补拉异常 {e}")
                fetched, fetch_adjust = [], "unknown"
            if fetched:
                # #47：合并去重：保留窗口内已有 bar，追加 fetched 中的新日期。
                # #84：兜底补拉只落新日期，不覆盖库内已有行（防不复权价覆盖前复权库）
                existing = {b.date for b in bars}
                new_dates = [b for b in fetched if b.date not in existing]
                merged = sorted(bars + new_dates, key=lambda b: b.date)
                klines[sym] = merged
                bars = merged
                if new_dates:
                    collector.save_bars(sym, new_dates, adjust=fetch_adjust)
            else:
                warnings.append(f"{sym}: 补拉无数据（数据源返回空/异常）")
        last = bars[-1] if bars else None
        print(f"  {sym}: {len(bars)}根 | 最新 {last.date if last else '无'} 收{last.close if last else '-'}")


def _run_pool_refresh(mkt_db: str, end: str, *, protected: set[str],
                      warnings: list[str]) -> dict:
    """收盘换血（评分前）：扫描 → 淘汰/容量 → 入池；失败只进 warnings

    Returns:
        {"removed": [...], "added": [...], "room": int, "stats": {...}}
    """
    from src import pool_refresh

    result = pool_refresh.plan(end, scope="all", max_new=pool_refresh.MAX_NEW_PER_RUN)
    rb = pool_refresh.rebalance(result, market_db=mkt_db, protected=protected)
    st = result["stats"]
    print(f"  扫描 {st['scanned']} 只 | ≥80 分 {st['ge80']} 只 | "
          f"移出 {len(rb['removed'])} 只 | 入池 {len(rb['added'])} 只 | "
          f"容量余量 {rb['room']}")
    for g in (result.get("gaps") or [])[:5]:
        warnings.append(f"观察池换血来源缺口：{g}")
    return {**rb, "stats": st}


def main(
    dry_run: bool = False,
    db_dir: str | None = None,
    source: DataSource | None = None,
    valuation_provider=None,
    earnings_provider=None,
    industry_mode: str = "percentile",
    lockup_provider=None,
    hotmoney_provider=None,
    macro_provider=None,
) -> None:
    """R47 完整三角闭环

    Args:
        dry_run: True 时使用临时库副本，不写生产库
        db_dir: 指定演练用数据库目录（隐含 dry-run）
        source: 数据源覆盖（测试注入 StaticDataSource；默认腾讯 HTTP）
        valuation_provider: 估值函数 symbol -> {"pe","pb"}（默认东财公开接口）
        earnings_provider: 业绩函数 code -> {"net_yoy","net_profit",...}
            （默认东财业绩报表 src.finance_feed.fetch_earnings）
        industry_mode: 行业景气口径 percentile（默认：自身板块 20 日涨幅分位）/
                       tracks（旧口径：当期主赛道 TOP7 名称匹配，仅对比用）
        lockup_provider: 解禁事件函数 (symbol, start, end) -> list[dict]
            （默认东财限售解禁 src.lockup_watch.fetch_events；dry-run 未注入时跳过）
        hotmoney_provider: 游资数据函数 (symbol, asof) -> dict
            （默认东财资金流+两融+龙虎榜席位 src.hot_money_watch.fetch_bundle；
             dry-run 未注入时跳过）
        macro_provider: 宏观指标函数 asof -> dict（默认东财 PMI/CPI/PPI/M2/LPR
            src.macro_regime.fetch_bundle；dry-run 未注入时跳过）
    """
    kb_db, mkt_db, is_dry = _prepare_db_dir(dry_run, db_dir)
    net = source or TencentHttpDataSource()
    tdx = TdxDataSource()
    val_provider = valuation_provider or fetch_valuation_pe_pb
    if earnings_provider is None:
        from src.finance_feed import fetch_earnings

        earnings_provider = fetch_earnings

    kb = KnowledgeBase(kb_db)
    kpi = KpiTracker(kb_db, mkt_db)   # KPI-07 推荐自动登记（洞1）
    store = PortfolioStore(kb_db)
    dstore = DecisionStore(kb_db)     # 决策日志（append-only）+ 决策单持仓状态
    research = ResearchStore(kb_db)
    sector = SectorAnalyzer(mkt_db)
    watch = WatchlistStore(mkt_db)
    collector = Collector(net, db_path=mkt_db)
    items = watch.all()

    mode = "DRY-RUN（临时库，不写生产数据）" if is_dry else "真实闭环（写生产库）"
    warnings: list[str] = []
    print("=" * 70)
    print(f"  R47 真实闭环（TradeExecutor 数字化交易员 · {mode}）")
    print("=" * 70)
    print(f"  观察池 {len(items)} 只 | 知识库 {kb_db} | 行情库 {mkt_db}")

    # ========== 1. 数据自助拉取（market.db 优先 · 缺失补拉并落库） ==========
    print("\n【1】数据自助拉取（market.db 优先 · 补拉自动落库）")
    print("-" * 70)
    klines = _load_from_market_db(START, END, mkt_db)
    # 拉取集合 = 观察池 + 当前持仓（保证持仓标的当日K线不陈旧）+ 指数
    # （sh000001/sz399001/sz399006：龙虎榜简报锚点 + 市场状态判定都要本地日K；
    #   sh000300：沪深300 基准落库，冻结期满"是否跑赢"可离线复算/审计）
    fetch_symbols = {i.symbol for i in items}
    fetch_symbols.update(p.symbol for p in store.holding(account="sim"))
    fetch_symbols.update(dstore.active_symbols())   # 未成交决策 + 未平仓决策单必须有当日行情
    fetch_symbols.update(("sh000001", "sz399001", "sz399006", "sh000300"))
    _ensure_bars(fetch_symbols, klines, net, tdx, collector, warnings)

    # ========== 1.5 财报自动采集（业绩趋势数据自给，失败降级为告警） ==========
    print("\n【1.5】财报自动采集（业绩证据 · 东财业绩报表）")
    print("-" * 70)
    if is_dry:
        print("  （dry-run 跳过：不写生产调研库）")
    else:
        try:
            added, skipped = _refresh_finance_evidence([i.symbol for i in items])
            print(f"  新增 {added} 条 / 跳过 {skipped} 条（已存在或无数据）")
        except Exception as e:
            warnings.append(f"财报自动采集失败: {e}")
            print(f"  ⚠️ 财报自动采集失败: {e}（业绩证据沿用调研库既有情报）")

    # ========== 2. 调查员预检（分口径胜率：KPI-07 只认真实） ==========
    print("\n【2】调查员预检（选股前取知识库情报）")
    print("-" * 70)
    pre = kb.preflight()
    print(f"  历史 {pre.total_trades} 笔 | 综合胜率 {pre.win_rate}%")
    print(f"  口径胜率: 模拟盘 {pre.by_source['sim']['win_rate']}% "
          f"(对{pre.by_source['sim']['correct']}/错{pre.by_source['sim']['mistake']}) | "
          f"真实 {pre.by_source['real']['win_rate']}% "
          f"(对{pre.by_source['real']['correct']}/错{pre.by_source['real']['mistake']})")
    for s in pre.danger_signals:
        print(f"    {s}")

    # ========== 2.5 赛道情报（月度/季度主赛道,数据驱动） ==========
    print("\n【2.5】赛道情报（月度/季度主赛道 · 全数据驱动）")
    print("-" * 70)
    top_ctx: dict[str, float] = {}
    sector_ctx: dict[str, float] = {}
    snapshot_info: dict = {"date": "", "source": "", "complete": False,
                           "plate_count": 0, "concept_count": 0, "note": ""}
    try:
        dates = _ensure_current_sector_snapshot(sector, END, is_dry)
        if dates:
            r = sector.analyze()
            print(f"  数据日期: {r['date']}（板块快照 {len(dates)} 个交易日）")
            snapshot_info = sector.snapshot_info(r["date"])
            print(f"  快照数据源: {snapshot_info['source'] or '未知'} | "
                  f"plate={snapshot_info['plate_count']} / "
                  f"concept={snapshot_info['concept_count']} | "
                  f"完整={'是' if snapshot_info['complete'] else '否'}")
            if snapshot_info["source"] != "eastmoney":
                warnings.append(
                    f"板块快照数据源={snapshot_info['source']}（非东财），行业/资金因子按缺失处理"
                )
                print("  ⚠️ 板块快照非东财口径，行业/资金因子按缺失处理")
            if not snapshot_info["complete"]:
                warnings.append(
                    "板块快照不完整：" + (snapshot_info.get("note") or "概念板块未拉全")
                )
                print(f"  ⚠️ 板块快照不完整：{snapshot_info.get('note') or '概念板块未拉全'}"
                      f"（行业/资金仍按行业板块分位计算）")
            if r["date"] < EXPECTED_DATA_DATE:
                # #46：板块注入失败时不再静默——收盘闭环显式告警数据滞后
                # 2026-11-06：滞后即门禁——行业/资金因子按缺失处理，不再用旧快照计分
                warnings.append(
                    f"板块快照滞后：{r['date']}（缺 {EXPECTED_DATA_DATE} 当日数据，行业/资金因子按缺失处理）"
                )
                print(f"  ⚠️ 板块快照滞后：{r['date']}（缺 {EXPECTED_DATA_DATE} 当日数据，"
                      f"行业/资金因子按缺失处理）")
            print("  月度主赛道 TOP3:")
            for t in r["monthly_tracks"][:3]:
                print(f"    - {t}")
            print("  季度主赛道 TOP3:")
            for t in r["quarterly_tracks"][:3]:
                print(f"    - {t}")
            # C 方案：全板块字典（仅用于解析/展示，评分用板块内分位）
            sector_ctx = {t.name: t.score for t in r.get("all_tracks", r["monthly_tracks"])}
            top_ctx = {t.name: t.score for t in r["monthly_tracks"][:5]}
            top_ctx.update({t.name: t.score for t in r["quarterly_tracks"][:3]})
        else:
            print("  ⚠️ 无板块快照（自动任务需先注入 data_sector 数据），跳过赛道情报")
            sector_ctx = {}
    except Exception as e:
        print(f"  ⚠️ 赛道情报加载失败: {e}（不影响主流程）")
        warnings.append(f"赛道情报加载失败: {e}")
        sector_ctx = {}
    try:
        # 可疑板块（点位与涨幅不自洽）不参与评分：不进分位基准
        plates = sector.load_date(kind="plate", exclude_suspicious=True)
        plates += sector.load_date(kind="concept", exclude_suspicious=True)
    except Exception:
        plates = []

    # ========== 2.6 观察池换血（评分前执行：新候选当日即参与评分） ==========
    print("\n【2.6】观察池换血（东财业绩口径 · 评分前接入）")
    print("-" * 70)
    if is_dry:
        print("  （dry-run 跳过：不写生产观察池）")
    else:
        try:
            held = {p.symbol for p in store.holding(account="sim")}
            held |= {p.symbol for p in store.holding(account="real")}   # 持仓保护：任一账户持仓不淘汰
            rb = _run_pool_refresh(mkt_db, END, protected=held, warnings=warnings)
            for r in rb["removed"]:
                print(f"    ➖ 移出 {r.get('name', r['symbol'])}({r['symbol']})：{r['reason']}")
            for s in rb["added"]:
                print(f"    ➕ 入池 {s}")
            if rb["removed"]:
                # 软删后仍留在 items 的标的会被评分，但不再有新决策意义；重载一次保持口径一致
                items = watch.all()
            if rb["added"]:
                # 新入池标的补拉当日K线，否则当日评分会因无行情被跳过
                _ensure_bars(set(rb["added"]), klines, net, tdx, collector, warnings)
                items = watch.all()
        except Exception as e:
            warnings.append(f"观察池换血失败: {e}（不影响收盘闭环）")
            print(f"  ⚠️ 观察池换血失败: {e}（不影响收盘闭环）")

    # ========== 3. 调查员评估（真实K线走势 + 调研弹药库情报 + 赛道上下文） ==========
    print("\n【3】调查员评估（真实K线 + 调研弹药库 + 赛道上下文）")
    print("-" * 70)
    for item in items:
        bars = klines.get(item.symbol, [])
        if not bars:
            print(f"  {item.name}: 无数据，跳过")
            continue
        first, last = bars[0], bars[-1]
        chg = (last.close - first.close) / first.close * 100
        trend = "走强" if chg > 0 else "走弱/震荡"
        print(f"  {item.name}: {trend}（{first.date}收{first.close} → {last.date}收{last.close}，区间{chg:+.2f}%）")
        if top_ctx:
            sec = item.sector
            # #36：与主赛道展示同方向（板块关键词 ∈ 主赛道名），避免
            # "消费电子"误配进"品牌消费电子"导致标注不一致
            kws = [k.strip() for k in sec.replace("Ⅱ", "").split("/") if len(k.strip()) >= 2]
            in_track = any(kw in tn for tn in top_ctx for kw in kws) if kws else False
            track_note = "🎯 属于当前主赛道" if in_track else "（不在主赛道内）"
            print(f"    🧭 所属板块: {sec or '未知'} {track_note}")
        notes = research.query(symbol=item.symbol, limit=3)
        for n in notes:
            print(f"    📋 [{n.category}] {n.title}: {n.content[:50]}...")

    # ========== 4. 交易员决策（TradeExecutor + 持仓表驱动 + 市场状态自适应） ==========
    print("\n【4】交易员决策（TradeExecutor · 持仓表驱动 · 市场状态自适应）")
    print("-" * 70)

    # 市场状态判定（指数日K → 趋势/震荡/下跌/防御 → 仓位档位）
    index_bars = _load_index_history(mkt_db)
    if len(index_bars) < 61:
        try:
            fetched, fetch_adjust = _fetch_with_fallback(
                "sz399006", recent_window(days_back=120)[0], END, net, tdx)
        except Exception as e:
            warnings.append(f"指数补拉异常: {e}")
            fetched, fetch_adjust = [], "unknown"
        if fetched:
            collector.save_bars("sz399006", fetched, adjust=fetch_adjust)
            index_bars = fetched
    index_fresh = bool(index_bars) and index_bars[-1].date == EXPECTED_DATA_DATE
    if index_bars and not index_fresh:
        warnings.append(
            f"指数K线陈旧：最新 {index_bars[-1].date} ≠ 当日交易日 {EXPECTED_DATA_DATE}"
            f"（市场状态按震荡兜底，不用旧指数bar判定）"
        )
        print(f"  ⚠️ 指数K线陈旧：最新 {index_bars[-1].date} ≠ {EXPECTED_DATA_DATE}")
    rr = detect_regime(index_bars) if (len(index_bars) >= 61 and index_fresh) else None
    regime = rr.regime if rr else Regime.RANGE
    if rr is None and len(index_bars) < 61:
        warnings.append("指数历史不足61根，市场状态按震荡兜底（RANGE 50%）")
    trader = TradeExecutor(total_assets=30000, regime=regime.value.upper(), stop_loss_pct=7.0)
    strategy = select_strategy(regime)
    print(f"  市场状态: {regime.label()}（{rr.detail if rr else '指数K线陈旧或历史不足，按震荡兜底'}）"
          f" → 仓位上限 {trader.macro_cap_pct:.0f}%")

    # 4.0 持仓状态：从持仓表读取（模拟盘 sim 与真实 real 分离）

    # 4.0.1 决策单结算（15:35）：止损/分批止盈/移动止损/加仓/伏击成交/3日作废
    print("  决策单结算（VCP伏击 · 止损/滚利润/作废）：")
    settle_result = settle(dstore, store, klines, END, dry_run=is_dry)
    for line in settle_result.lines:
        print(f"    {line}")
    if not settle_result.lines:
        print("    无待成交决策 / 无持仓触发")

    held_positions = store.holding(account="sim")
    print("  当前持仓（模拟盘 sim 账户）：")

    # #39：止损检查必须用当日新鲜收盘价（klines 有当日数据时），
    # 否则昨收价判断止损 → 当日大跌 -7% 要等第二天才触发（晚一天）
    def _current_px(sym: str) -> float:
        bars = klines.get(sym, [])
        if bars and bars[-1].date == END:
            return bars[-1].close
        cur = store.get(sym)
        return (cur.last_price or cur.cost_price) if cur else 0.0

    def _build_held() -> list[Position]:
        return [Position(p.symbol, p.name, p.shares, p.cost_price,
                         _current_px(p.symbol))
                for p in store.holding(account="sim")]

    for p in held_positions:
        px = _current_px(p.symbol)
        pnl = (px - p.cost_price) / p.cost_price * 100
        print(f"    {p.name}({p.symbol}): {p.shares}股 成本{p.cost_price} 现价{px} 浮盈{pnl:+.2f}% — {p.note}")
    held = _build_held()
    # 决策单持仓由 src.decision_settle 统一管理（-6%/结构止损、分批止盈、移动止损），
    # 旧的 -7% 止损 / 策略减仓 / 清仓线只作用于无标记的历史持仓，避免双重出场。
    managed_symbols = {p.symbol for p in store.holding(account="sim") if is_managed(p)}
    held_legacy = [p for p in held if p.symbol not in managed_symbols]
    if managed_symbols:
        print(f"  决策单持仓（由 VCP 结算接管）：{', '.join(sorted(managed_symbols))}")

    # 方案 A（#26）：只记录本轮真实发生的交易动作，浮动盈亏不再写入对题本/错题本
    items_by_sym = {i.symbol: i for i in items}
    realized: list[dict] = []

    def _entry_date(sym: str) -> str | None:
        p0 = store.get(sym)
        return p0.entry_date if p0 else None

    def _record_action(a, cost: float, tag: str, reason: str,
                       entry_date: str | None = None) -> None:
        item = items_by_sym.get(a.symbol)
        realized.append({
            "code": item.code if item else a.symbol[2:],
            "name": a.name, "cost": cost, "price": a.price,
            "shares": a.shares, "reason": reason, "tag": tag,
            "entry_date": entry_date,
        })

    # 4.0b 决策单卖出并入已实现（VCP 止损/分批止盈/移动止损）
    # 决策单持仓被 is_managed 排除在 held_legacy 之外，与 4.1~4.3 的 legacy 动作互斥；
    # 成本取 _sell 回传的摊薄成本，保证 book 的 profit_pct 与 decision_trades 一致。
    for s in settle_result.sells:
        item = items_by_sym.get(s["symbol"])
        realized.append({
            "code": item.code if item else s["symbol"][2:],
            "name": s["name"], "cost": s["cost"], "price": s["price"],
            "shares": s["shares"], "reason": s["reason"],
            "tag": item.strategy_tag if item else "",
            "entry_date": s.get("entry_date"),
        })

    # 4.1 止损检查（-7% 硬约束，仅历史持仓；决策单由 VCP 双层止损接管）
    print("  止损检查（-7%，仅历史持仓）：")
    held_by_sym = {p.symbol: p for p in held_legacy}
    stop_actions = trader.check_stop_loss(held_legacy)
    if stop_actions:
        for a in stop_actions:
            print(f"    {a}")
            store.reduce(a.symbol, a.shares, a.price, "触发止损")
            p = held_by_sym.get(a.symbol)
            _record_action(a, p.cost_price if p else 0.0,
                           items_by_sym.get(a.symbol).strategy_tag if a.symbol in items_by_sym else "",
                           f"触发止损：{a.reason}", entry_date=_entry_date(a.symbol))
    else:
        print("    无触发（全部持仓 -7% 阈值内）")

    # 4.2 策略信号检查（市场状态→策略→动作：卖出信号减仓50%，仅历史持仓）
    print("  策略信号检查（市场状态→策略→动作，仅历史持仓）：")
    held = _build_held()
    held_legacy = [p for p in held if p.symbol not in managed_symbols]
    strategy_actions = apply_strategy_actions(trader, store, regime, held_legacy, klines)

    if strategy_actions:
        for a in strategy_actions:
            print(f"    {a}")
            cur = store.get(a.symbol)
            _record_action(a, cur.cost_price if cur else 0.0,
                           items_by_sym.get(a.symbol).strategy_tag if a.symbol in items_by_sym else "",
                           f"策略减仓：{a.reason}", entry_date=cur.entry_date if cur else None)
    else:
        print("    无策略卖出信号（持仓按纪律持有）")

    # 4.2.6 清仓线自动执行（止损/止盈：9:25 预案触发 → 收盘落实，
    # 与 morning_flow._judge_position 同源 watchlist.take_profit/stop_loss）
    print("  清仓线检查（止损/止盈，自动执行）：")
    sell_lines = {
        i.symbol: {"sl": i.stop_loss, "tp": i.take_profit, "note": i.note or ""}
        for i in items
        if i.symbol not in managed_symbols   # 决策单持仓由 VCP 止盈止损接管
    }
    sell_actions = apply_sell_line_rules(store, klines, today=END, lines=sell_lines)
    if sell_actions:
        for a in sell_actions:
            print(f"    {a}")
            cur = store.get(a.symbol)
            _record_action(a, cur.cost_price if cur else 0.0,
                           items_by_sym.get(a.symbol).strategy_tag if a.symbol in items_by_sym else "",
                           f"清仓线执行：{a.reason}", entry_date=cur.entry_date if cur else None)
    else:
        print("    无触发（盘中未触止损线 / 未达止盈线 / 当日K线缺失 / 已执行）")

    # 4.2.7 预案减仓线自动执行（方案 A：9:25 预案减仓线收盘落实）
    print("  预案减仓线检查（自动执行）：")
    trim_actions = apply_trim_rules(store, klines, today=END)
    if trim_actions:
        for a in trim_actions:
            print(f"    {a}")
            cur = store.get(a.symbol)
            _record_action(a, cur.cost_price if cur else 0.0,
                           items_by_sym.get(a.symbol).strategy_tag if a.symbol in items_by_sym else "",
                           f"预案减仓：{a.reason}", entry_date=cur.entry_date if cur else None)
    else:
        print("    无触发（盘中最低未触减仓线 / 当日K线缺失 / 已执行）")
    # 预案/清仓线执行核对（#11 + 08-07 清仓线）：当日盘中触线但未落地的标的，告警提示
    from src.simulation import exit_signal
    from src.trim_rules import EXECUTED_MARK, TRIM_RULES

    for sym, rule in TRIM_RULES.items():
        bars = klines.get(sym, [])
        if not bars or bars[-1].date != END:
            continue
        if bars[-1].low > rule["price"]:
            continue
        cur = store.get(sym)
        note = (cur.note or "") if cur else ""
        if EXECUTED_MARK in note:
            print(f"    ✅ {cur.name}: 预案减仓已落地（盘中最低{bars[-1].low:.2f}≤{rule['price']:.2f}，不重复执行）")
        elif cur and cur.shares > 0:
            warnings.append(f"{cur.name}: 预案减仓线触发（盘中最低{bars[-1].low:.2f}≤{rule['price']:.2f}）但未落地")
            print(f"    ⚠️ {cur.name}: 预案减仓线触发但未落地")
        else:
            warnings.append(f"{sym}: 预案减仓线触发但无持仓（已清仓？），提示关注")
            print(f"    ⚠️ {sym}: 预案减仓线触发但无持仓")

    for sym, rule in sell_lines.items():
        sl, tp = rule.get("sl"), rule.get("tp")
        if not sl and not tp:
            continue
        bars = klines.get(sym, [])
        if not bars or bars[-1].date != END:
            continue
        cur = store.get(sym)
        if not (cur and cur.shares > 0):
            continue
        price, kind = exit_signal(bars[-1], tp, sl)
        if price is None:
            continue
        line = sl if kind == "止损" else tp
        line_txt = f"{kind}线 {line:.2f}"
        note = cur.note or ""
        if SELL_LINE_MARK in note:
            print(f"    ✅ {cur.name}: 清仓线已落地（{line_txt}）")
        else:
            warnings.append(f"{cur.name}: 清仓线触发（{line_txt}）但未落地")
            print(f"    ⚠️ {cur.name}: 清仓线触发但未落地")
    held = _build_held()

    # 4.3 持仓现价更新（校验K线新鲜度：缺当日数据不覆盖，标记 stale）
    for item in items:
        cur = store.get(item.symbol)
        if not (cur and cur.shares > 0 and item.symbol in klines):
            continue
        last_bar = klines[item.symbol][-1]
        if last_bar.date < END:
            warnings.append(
                f"{item.name}: 缺 {END} 当日K线（最新 {last_bar.date}），"
                f"持仓现价保持 {cur.last_price}（stale 不覆盖）"
            )
            print(f"    {item.name}: ⚠️ 缺当日K线（最新 {last_bar.date}），现价保持 {cur.last_price}（stale）")
            continue
        note = cur.note or ""
        if cur.entry_date and cur.entry_date < END and MIRROR_MARK in note:
            note = note.replace(MIRROR_MARK, "").strip(" |")   # 昨日建仓的跟单窗口已过
        store.set(item.symbol, item.name, cur.shares, cur.cost_price,
                  last_bar.close, note)
    print("  持仓现价已更新（stale 标的除外）")

    # 4.4 可执行性前置过滤（排序前排除）→ 五因子评分 → 观察记录（策略已退役，不建仓）
    print("  建仓评估（可执行性过滤 → 五因子 → 观察记录）：")
    no_evidence_count = 0
    candidate_rows: list[dict] = []   # 当日候选池全量记录（供选股结果落盘）
    factor_rows: list[dict] = []      # 当日五因子快照（供跨日回溯）
    decision_rows: list[dict] = []    # 决策日志（append-only）
    excluded_reports: list[dict] = [] # 候选报告"仅分析/其他排除"两节
    benchmark = factor_benchmark(plates)
    git_head, git_dirty = _git_state()
    snapshot_date = snapshot_info.get("date") or ""
    snapshot_complete = bool(snapshot_info.get("complete"))
    common_decision = dict(
        decision_date=END, data_date=END,
        snapshot_date=snapshot_date, snapshot_complete=snapshot_complete,
        git_head=git_head, git_dirty=git_dirty,
    )

    # 板块权限 + 价格/ST/次新/停牌 硬过滤（可执行性口径，排序前执行）
    listed_days = _listed_days(mkt_db, {i.symbol for i in items})
    eligible, excluded = filter_universe(
        items, klines, as_of=END, listed_days_by_symbol=listed_days,
    )
    eligible_by_sym = {item.symbol: e for item, e in eligible}
    for e in excluded:
        print(f"    ⛔ 排除 {e.name}({e.code})：{e.reason}")
        decision_rows.append(make_excluded_row(
            decision_date=END, data_date=END, eligibility=e,
            snapshot_date=snapshot_date, snapshot_complete=snapshot_complete,
            git_head=git_head, git_dirty=git_dirty,
        ))
        excluded_reports.append({
            "code": e.code, "name": e.name, "reason": e.reason,
            "analysis_only": e.analysis_only,
        })

    vcp_hist = _load_vcp_history(mkt_db, set(eligible_by_sym))

    for item in items:
        if item.symbol not in eligible_by_sym:
            continue   # 已在可执行性阶段排除并单列，不进入评分/排序
        e = eligible_by_sym[item.symbol]
        base = {"code": item.code, "name": item.name, "sector": item.sector}
        cur = store.get(item.symbol)
        if cur and cur.shares > 0:
            print(f"    {item.name}: 已有持仓 {cur.shares} 股，不重复建仓")
            candidate_rows.append({**base, "status": f"已持仓 {cur.shares} 股", "result": None})
            continue
        bars = klines.get(item.symbol, [])
        if not bars:
            candidate_rows.append({**base, "status": "无行情数据（跳过）", "result": None})
            continue
        price = bars[-1].close
        vcp = detect_vcp(vcp_hist.get(item.symbol) or bars)
        # 业绩因子（2026-11-10 起唯一口径）：东财业绩报表 归母净利同比 → 过桶
        try:
            earnings = earnings_provider(item.code)
        except Exception as e:
            warnings.append(f"{item.name}: 业绩接口异常 {e}，按数据缺失处理")
            earnings = None
        growth, growth_evidence = growth_from_earnings(earnings, item.code)
        if growth is None:
            no_evidence_count += 1   # 降噪（#19）：无业绩标的汇总一行，不再逐只刷屏
            candidate_rows.append({**base, "growth": None, "result": None,
                                   "status": "强制未达标：无业绩数据（东财业绩报表缺失/同比为空）"})
            decision_rows.append(make_decision_row(
                cand=CandidateInput(
                    symbol=item.symbol, code=item.code, name=item.name,
                    sector=item.sector, price=price, bars=bars, score=None,
                    factors={}, missing=("业绩证据",), vcp=vcp, sector_pct=None,
                    first_shares=e.first_shares,
                ), **common_decision,
            ))
            continue
        valuation = val_provider(item.symbol) or {}
        if not valuation:
            warnings.append(f"{item.name}: 估值接口失败/无数据，评分按数据缺失处理")
        result = score_candidate(
            item.symbol, item.name, item.sector,
            growth=growth, growth_evidence=growth_evidence,
            bars=bars, sector_ctx=sector_ctx, plates=plates,
            valuation=valuation, industry_mode=industry_mode,
            snapshot_source=snapshot_info.get("source") or "",
            snapshot_complete=bool(snapshot_info.get("complete")),
            expected_date=EXPECTED_DATA_DATE,
            snapshot_date=snapshot_info.get("date") or "",
        )
        resolved = _resolve_plate(industry_pool(plates), item.sector)
        factor_rows.append({
            "symbol": item.symbol,
            "industry": None if "行业景气" in result.missing else result.factors.industry,
            "growth": result.factors.growth,
            "valuation": None if "估值" in result.missing else result.factors.valuation,
            "technical": None if "技术面" in result.missing else result.factors.technical,
            "capital": None if "资金面" in result.missing else result.factors.capital,
            "score": result.weighted,
            "missing": "、".join(result.missing),   # 缺失即"无数据"，与 NULL 互为印证
            "source": snapshot_info.get("source") or "",
            "resolved_board": resolved.name if resolved else "",
            "benchmark": benchmark,
            # 业绩原始值落档：复算依据（第 7 节分数引用纪律），缺失记 NULL
            "growth_raw": (earnings or {}).get("net_yoy"),
            "growth_profitable": (
                None if (earnings or {}).get("net_profit") is None
                else float(earnings["net_profit"]) >= 0),
            "growth_source": "eastmoney",
        })
        print(f"    {result.summary} | 业绩证据: {growth_evidence[:42]}...")

        # VCP 埋伏条件：五因子 ≥70 + VCP 成立 + 板块中上位置（不追最热头部）
        # 2026-11-11 策略路线变更：只作观察记录，不再产生建仓计划
        sector_pct = None if "行业景气" in result.missing else result.factors.industry
        drow = make_decision_row(
            cand=CandidateInput(
                symbol=item.symbol, code=item.code, name=item.name,
                sector=item.sector, price=price, bars=bars,
                score=result.weighted,
                factors={
                    "industry": result.factors.industry,
                    "growth": result.factors.growth,
                    "valuation": result.factors.valuation,
                    "technical": result.factors.technical,
                    "capital": result.factors.capital,
                },
                missing=result.missing, vcp=vcp, sector_pct=sector_pct,
                first_shares=e.first_shares,
            ),
            **common_decision,
        )
        decision_rows.append(drow)
        status = f"观察：{drow['reason']}"
        print(f"    {item.name}: {drow['reason']}")

        if not result.recommended and result.incomplete:
            status += "｜强制未达标：缺" + "、".join(result.missing)
        if result.recommended:
            # 洞1：≥85 推荐自动登记 KPI-07（测选股质量，不等同建仓）
            kpi.register_recommendation(
                round_number=f"R{END}", code=item.code, name=item.name,
                rec_date=END, rec_close=price,
            )
        candidate_rows.append({**base, "result": result, "growth": growth,
                               "status": status})

    if not eligible:
        reason = f"可执行性过滤后 0 只可买候选（排除 {len(excluded)} 只）"
        decision_rows.append(make_no_candidate_row(
            decision_date=END, data_date=END, reason=reason,
            snapshot_date=snapshot_date, snapshot_complete=snapshot_complete,
            git_head=git_head, git_dirty=git_dirty,
        ))
        print(f"    ⚠️ {reason}")
    if no_evidence_count:
        warnings.append(f"{no_evidence_count} 只候选无业绩数据（东财业绩报表缺失/同比为空），休眠跳过")
        print(f"    ℹ️ {no_evidence_count} 只候选无业绩数据，休眠跳过（汇总）")
    # D 方案：每日因子快照落库（同日覆盖，幂等；dry-run 不写生产库）
    if is_dry:
        print("    （dry-run 不写因子快照）")
    else:
        try:
            fstore = FactorSnapshotStore(mkt_db)
            n = fstore.save_many(END, factor_rows)
            print(f"  🧾 因子快照已落库 {n} 条（{END}，同日覆盖）")
            bad = fstore.verify(END)   # 落档即可复算：对不上说明字段仍在丢
            if bad:
                warnings.append(f"因子快照复算不一致 {len(bad)} 条: {bad}")
                log.warning("因子快照复算不一致 %d 条: %s", len(bad), bad)
                print(f"    ⚠️ 因子快照复算不一致 {len(bad)} 条: {bad}")
            else:
                log.info("因子快照复算通过（%d 条加权分与落库分一致）", n)
                print(f"    ✅ 因子快照复算通过（{n} 条加权分与落库分一致）")
        except Exception as exc:
            warnings.append(f"因子快照落库失败: {exc}")
            print(f"    ⚠️ 因子快照落库失败: {exc}")
    # 候选落盘（同日覆盖幂等；dry-run 绝不写生产目录）
    if is_dry:
        print("    （dry-run 不写选股结果文件）")
    else:
        notes = []
        if snapshot_info.get("source") and snapshot_info["source"] != "eastmoney":
            notes.append(f"板块快照数据源={snapshot_info['source']}，行业/资金因子按缺失处理")
        quality = _snapshot_quality_note(snapshot_info)
        if quality:
            notes.append(quality)
        path = _write_candidates_report(END, candidate_rows, notes=notes,
                                        excluded=excluded_reports)
        print(f"  📄 已写入 {path}")

    # 4.5 决策日志（append-only）：观察记录 + 排除，全部留痕
    kpi_text = render_decision_kpi(decision_kpi(kb_db))
    decision_md = render_day_markdown(
        decision_date=END, data_date=END,
        rows=decision_rows, excluded=excluded_reports,
        snapshot_date=snapshot_date, snapshot_complete=snapshot_complete,
        git_head=git_head, git_dirty=git_dirty,
        settle_lines=settle_result.lines, kpi_text=kpi_text,
    )
    n_observe = sum(1 for r in decision_rows if r.get("decision") == "OBSERVE")
    if is_dry:
        print("  ---- 决策记录样例（dry-run 不写文件/库）----")
        print(decision_md)
    else:
        inserted, skipped = dstore.save_rows(decision_rows)
        path, created = write_day_markdown(END, decision_md)
        n_csv = append_csv(decision_rows)
        if created:
            print(f"  📄 决策日志已写入 {path}（新增 {inserted} 条，观察记录 {n_observe} 条，CSV +{n_csv}）")
        else:
            print(f"  ℹ️ 决策记录已存在：{path}（append-only，跳过不覆盖；库内新增 {inserted} 条 / 已存在 {skipped} 条）")
        # 4.6 VCP 通过率逐日记录（只统计，不参与任何门槛判定）
        try:
            rate_path = _write_vcp_pass_rate(kb_db, END)
            if rate_path:
                print(f"  📄 VCP 通过率已更新 {rate_path}")
        except Exception as e:
            warnings.append(f"VCP 通过率记录失败（不影响闭环）：{e}")

    # ========== 5. 评审员复盘回写 R47（方案 A：仅真实动作，source=sim） ==========
    print("\n【5】评审员复盘 → 回写知识库 R47（仅真实动作 · 浮盈不写入）")
    print("-" * 70)
    with sqlite3.connect(kb_db) as conn:
        # #49：幂等只清"本轮当日"的 R47 记录——不能全清，否则次日再跑会把
        # 前一日已实现的 R47 交易（如 08-07 宝钢 -3.19%）从对题本/错题本删除
        conn.execute(
            "DELETE FROM correct_book WHERE round_number='R47' AND date(created_at)=?",
            (END,),
        )
        conn.execute(
            "DELETE FROM mistake_book WHERE round_number='R47' AND date(created_at)=?",
            (END,),
        )
        conn.commit()
    print("  清理旧 R47（脚本幂等）")

    if realized:
        for t in realized:
            r = kb.record(
                stock_code=t["code"], stock_name=t["name"],
                entry_price=t["cost"], exit_price=t["price"],
                buy_reason=f"{t['tag']}（R47 持仓）",
                sell_reason=f"{t['reason']}（已实现 {t['shares']} 股）",
                strategy_tag=t["tag"], round_number="R47", holding_days=0,
                source="sim", entry_date=t.get("entry_date"), exit_date=END,
                # #99：机械规则动作（止损/减仓/清仓线）导致的亏损统一归类，
                # 避免错题本 mistake_type 全部落"未分类"导致预检归因失真
                mistake_type="规则执行亏损",
                loss_root_cause="机械规则执行（止损/减仓/清仓线）导致的已实现亏损",
                lesson_learned="规则止损/减仓按计划执行；亏损属规则成本，不因单笔亏损质疑纪律",
            )
            book_cn = "对题本" if r["book"] == "correct_book" else "错题本"
            print(f"  {t['name']}({t['code']}): 减 {t['shares']} 股@{t['price']} "
                  f"已实现 {r['return_pct']:+.2f}% → {book_cn}")
    else:
        print("  本轮无真实交易动作（浮动盈亏不写入知识库，见收益报表）")

    report = kb.round_summary("R47")
    print(f"\n  R47 复盘: {report.summary}")

    # ========== 6. 信号交叉验证 ==========
    print("\n【6】信号交叉验证（R47 策略历史表现）")
    print("-" * 70)
    seen = set()
    for item in items:
        if not item.strategy_tag or item.strategy_tag in seen:
            continue
        seen.add(item.strategy_tag)
        cc = kb.cross_check(item.strategy_tag)
        safe_cn = "✅安全" if cc["safe"] else "⚠️谨慎"
        print(f"  「{item.strategy_tag}」: {safe_cn} | 样本{cc['sample_count']}次 | 历史收益{cc['historical_return']}%")
        for risk in cc["risk_signals"]:
            print(f"      - {risk}")

    # ========== 6.4 每日预判（独立模块，不影响评分/选股） ==========
    print("\n【6.4】每日预判 + 到期对账（独立于五因子评分/选股）")
    print("-" * 70)
    if is_dry:
        print("  [DRY-RUN] 跳过预判（不写生产库/不落盘 outputs/预判）")
    else:
        try:
            settle_stats = forecast_settle_due(mkt_db, kb_db, today=EXPECTED_DATA_DATE)
            print(f"  到期结算：{settle_stats}")
            fr = forecast_generate_daily(mkt_db, kb_db, data_date=EXPECTED_DATA_DATE, run_date=END)
            if fr.get("skipped"):
                print(f"  ℹ️ {fr['reason']}（数据截止 {fr['data_date']}）")
            elif fr.get("ok"):
                print(f"  预判已落盘：{fr['path']}（大盘 {fr['market']} / 板块 {fr['sector']} / 个股 {fr['stock']}）")
            else:
                print(f"  ⚠️ 预判未生成：{fr.get('reason')}")
            hr = forecast_write_hit_rate(kb_db)
            print(f"  📄 命中率报表：{hr}")
        except Exception as e:
            warnings.append(f"每日预判失败（不影响闭环）：{e}")

    # ========== 6.5 影子跟踪（可选最后一步，失败只降级告警） ==========
    _run_shadow_tracking(is_dry, warnings)

    # ========== 6.6 解禁提示（只读观察层，失败只降级告警；不参与任何决策） ==========
    _run_lockup_watch(items, is_dry, warnings, provider=lockup_provider,
                      market_db=mkt_db)

    # ========== 6.7 游资提示（只读观察层，失败只降级告警；不参与任何决策） ==========
    _run_hotmoney_watch(items, is_dry, warnings, provider=hotmoney_provider,
                        market_db=mkt_db)

    # ========== 6.8 宏观仓位提示（只读观察层，失败只降级告警；不参与任何决策） ==========
    _run_macro_regime(is_dry, warnings, provider=macro_provider, market_db=mkt_db)

    # ========== 6.9 板块反转影子台账（只记录，不改口径；挂在决策落库之后） ==========
    _run_sector_reversal_shadow(is_dry, warnings, market_db=mkt_db)

    # ========== 6.10 指数化组合（sim 记账 · 真实 ETF 收盘价；失败只降级告警） ==========
    _run_index_portfolio(END, is_dry, warnings, market_db=mkt_db)

    # ========== 7. 运行告警汇总 ==========
    print("\n【7】运行告警（失败/数据缺失汇总）")
    print("-" * 70)
    if warnings:
        for w in warnings:
            print(f"  ⚠️ {w}")
        if not is_dry:
            out = Path("outputs") / "收盘报表"
            out.mkdir(parents=True, exist_ok=True)
            warn_file = out / f"告警_{END}.md"
            warn_file.write_text(
                "# 运行告警\n\n| # | 告警 |\n|---|------|\n" +
                "\n".join(f"| {i+1} | {w} |" for i, w in enumerate(warnings)) + "\n",
                encoding="utf-8",
            )
            print(f"  📄 已写入 {warn_file}")
    else:
        print("  ✅ 无告警")

    print("\n" + "=" * 70)
    print("  R47 真实闭环完成：TradeExecutor 决策 + 知识库回写 + 行情落库")
    if is_dry:
        print(f"  [DRY-RUN] 演练结束，未写入生产库（临时目录: {Path(kb_db).parent}）")
    print("=" * 70)


def _main_cli() -> None:
    parser = argparse.ArgumentParser(description="R47 真实闭环（三角闭环选股体系）")
    parser.add_argument("--dry-run", action="store_true",
                        help="演练模式：临时库副本，不写生产库")
    parser.add_argument("--db-dir", default=None,
                        help="指定演练数据库目录（隐含 dry-run）")
    parser.add_argument("--no-valuation", action="store_true",
                        help="跳过估值接口（估值视为数据缺失，评分未达标）")
    parser.add_argument("--industry-mode", choices=("tracks", "percentile"),
                        default="percentile",
                        help="行业景气口径：percentile=自身板块 20 日涨幅分位（默认，"
                             "2026-10-30 起），tracks=旧 TOP7 赛道名匹配（仅对比）")
    args = parser.parse_args()
    main(
        dry_run=args.dry_run or bool(args.db_dir),
        db_dir=args.db_dir,
        valuation_provider=(lambda sym: {}) if args.no_valuation else None,
        industry_mode=args.industry_mode,
    )


if __name__ == "__main__":
    _main_cli()
