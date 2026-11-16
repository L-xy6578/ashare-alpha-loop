"""
影子跟踪（shadow tracking）— 纯观察实验，不建仓、不进观察池、不写 positions

目的：把 2026-10-30 扩围 dry-run 的 9 只 ≥80 样本按"模型高分组 / 低基数对照组"固定下来，
每个交易日记录收盘价与相对基准表现，20 天后用真实数据验证评分模型是否有区分度。

数据：
    - 基线：2026-10-30 收盘价 + 五因子 + 所属板块 + 分组（来自扩围 dry-run 口径）
    - 增量：每个交易日收盘价（腾讯前复权优先，新浪兜底）；基准优先沪深300，缺失回退上证指数，
      两者都拿不到时标注"无基准"，并用样本等权均值代替比较。

产物：
    - outputs/影子跟踪/track.csv              旧基线（2026-10-30 固定 9 只）逐日明细，(date,symbol) 幂等覆盖
    - outputs/影子跟踪/影子组合_YYYY-MM-DD.md  旧基线当日表格 + 分组均值
    - outputs/影子跟踪/候选面板.csv            全候选面板（2026-11-09 起，主要证据），append-only
    - outputs/影子跟踪/面板收益.csv            面板 5/10/20 日收益，到期追加

用法:
    python -m src.shadow_track --init                 # 建立 2026-10-30 基线
    python -m src.shadow_track                        # 等同 --update：自动补齐缺失交易日
    python -m src.shadow_track --update --date 2026-11-05
    python -m src.shadow_track --no-backfill --date 2026-11-05  # 只更新该日，不补历史
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from datetime import date as date_cls, datetime, timedelta
from pathlib import Path

from src.logger import get_logger

log = get_logger(__name__)

BASELINE_DATE = "2026-10-30"
MAX_BACKFILL_DAYS = 10   # 单次自动补齐上限，防止 15:35 闭环被历史缺口拖慢
RETRY_WINDOW_DAYS = 10   # 只自动重试最近 10 个已记录交易日内的取价失败
OUT_DIR = Path("outputs") / "影子跟踪"
CSV_PATH = OUT_DIR / "track.csv"
CSV_FIELDS = [
    "date", "symbol", "name", "group", "board", "close", "day_pct", "cum_pct",
    "trade_days", "benchmark", "bench_cum", "rel_pct", "status",
    "industry", "growth", "valuation", "technical", "capital", "score",
]

# 2026-10-30 扩围 dry-run 的 9 只 ≥80 样本（因子分为当日报告值）
SHADOW_SAMPLE: list[dict] = [
    {"symbol": "sz002511", "code": "002511", "name": "中顺洁柔", "board": "生活用纸",
     "group": "model_high", "industry": 95, "growth": 90, "valuation": 78,
     "technical": 90, "capital": 78, "score": 87.0},
    {"symbol": "sz300244", "code": "300244", "name": "迪安诊断", "board": "诊断服务",
     "group": "control_low_base", "industry": 99, "growth": 90, "valuation": 75,
     "technical": 82, "capital": 74, "score": 85.6},
    {"symbol": "sh600000", "code": "600000", "name": "浦发银行", "board": "股份制银行Ⅲ",
     "group": "model_high", "industry": 97, "growth": 70, "valuation": 92,
     "technical": 90, "capital": 67, "score": 83.7},
    {"symbol": "sz000415", "code": "000415", "name": "渤海租赁", "board": "租赁",
     "group": "model_high", "industry": 93, "growth": 90, "valuation": 89,
     "technical": 100, "capital": 32, "score": 83.3},
    {"symbol": "sh688169", "code": "688169", "name": "石头科技", "board": "小家电",
     "group": "model_high", "industry": 93, "growth": 84, "valuation": 81,
     "technical": 82, "capital": 61, "score": 81.9},
    {"symbol": "sh603882", "code": "603882", "name": "金域医学", "board": "诊断服务",
     "group": "model_high", "industry": 99, "growth": 90, "valuation": 67,
     "technical": 67, "capital": 74, "score": 81.8},
    {"symbol": "sh601728", "code": "601728", "name": "中国电信", "board": "电信运营商",
     "group": "model_high", "industry": 98, "growth": 50, "valuation": 78,
     "technical": 100, "capital": 88, "score": 80.9},
    {"symbol": "sz001328", "code": "001328", "name": "登康口腔", "board": "个护用品",
     "group": "model_high", "industry": 93, "growth": 75, "valuation": 66,
     "technical": 90, "capital": 79, "score": 80.6},
    {"symbol": "sh600941", "code": "600941", "name": "中国移动", "board": "电信运营商",
     "group": "model_high", "industry": 98, "growth": 50, "valuation": 81,
     "technical": 90, "capital": 88, "score": 80.0},
]

# ---------- 交易权限硬过滤（只能买主板） ----------

def _buyable(s: dict) -> bool:
    from src.executable import is_buyable_code
    return is_buyable_code(s["code"], s["symbol"])[0]


BUYABLE_SHADOW_SAMPLE: list[dict] = [s for s in SHADOW_SAMPLE if _buyable(s)]
ANALYSIS_ONLY_SHADOW: list[dict] = [s for s in SHADOW_SAMPLE if not _buyable(s)]

# ---------- 数据获取 ----------

def _fetch_bars(symbol: str, start: str, end: str) -> tuple[list, str]:
    """日K：腾讯前复权优先，新浪不复权兜底（影子跟踪只用收盘价，口径不影响）"""
    try:
        from src.data_source import TencentHttpDataSource

        bars = TencentHttpDataSource().daily_kline(symbol, start, end)
        if bars:
            return bars, "tencent/qfq"
    except Exception as exc:
        log.warning("影子跟踪腾讯取价失败 %s: %s", symbol, exc)
    try:
        from src.pool_refresh import fetch_sina_bars

        bars = fetch_sina_bars(symbol, start, end)
        if bars:
            return bars, "sina/raw"
    except Exception as exc:
        log.warning("影子跟踪新浪取价失败 %s: %s", symbol, exc)
    return [], "none"


def close_on(symbol: str, day: str) -> tuple[float | None, str]:
    """单日收盘价（未取到返回 (None, 'none')）"""
    bars, src = _fetch_bars(symbol, day, day)
    if not bars:
        return None, "none"
    return bars[-1].close, src


def _benchmark(start: str, end: str) -> tuple[str, list]:
    """基准：沪深300 → 上证指数 → 无基准"""
    for symbol, name in (("sh000300", "沪深300"), ("sh000001", "上证指数")):
        bars, src = _fetch_bars(symbol, start, end)
        if bars:
            return name, bars
    return "无基准", []


def _latest_trading_day(day: str) -> str:
    """≤ day 的最近交易日（日历缺失时原样返回）"""
    try:
        from src.config import MARKET_DB_PATH
        from src.db import get_connection

        with get_connection(MARKET_DB_PATH, readonly=True) as conn:
            row = conn.execute(
                "SELECT MAX(date) FROM trade_calendar WHERE is_trading=1 AND date<=?",
                (day,),
            ).fetchone()
        return row[0] if row and row[0] else day
    except Exception:
        return day


def _latest_closed_day(day: str | None = None) -> str:
    """默认目标日：最近已收盘交易日（盘中运行不把今天当成已收盘）"""
    target = day or date_cls.today().isoformat()
    if day is None:
        now = datetime.now()
        if now.date().isoformat() == target and now.hour < 15:
            target = (now.date() - timedelta(days=1)).isoformat()
    return _latest_trading_day(target)


def _trading_days_between(start: str, end: str) -> list[str]:
    """(start, end] 内的交易日列表（升序）；日历不可用时退回工作日"""
    try:
        from src.config import MARKET_DB_PATH
        from src.db import get_connection

        with get_connection(MARKET_DB_PATH, readonly=True) as conn:
            rows = conn.execute(
                "SELECT date FROM trade_calendar WHERE is_trading=1 "
                "AND date>? AND date<=? ORDER BY date", (start, end),
            ).fetchall()
        return [r[0] for r in rows]
    except Exception:
        from datetime import date as d

        cur = d.fromisoformat(start) + timedelta(days=1)
        stop = d.fromisoformat(end)
        out = []
        while cur <= stop:
            if cur.weekday() < 5:
                out.append(cur.isoformat())
            cur += timedelta(days=1)
        return out


def _trade_days_between(start: str, end: str) -> int:
    """交易日计数（含 end，不含 start；日历缺失时退回自然日差）"""
    try:
        from src.config import MARKET_DB_PATH
        from src.db import get_connection

        with get_connection(MARKET_DB_PATH, readonly=True) as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM trade_calendar WHERE is_trading=1 "
                "AND date>? AND date<=?", (start, end),
            ).fetchone()[0]
        return int(n)
    except Exception:
        from datetime import date as d

        return max(0, (d.fromisoformat(end) - d.fromisoformat(start)).days)


# ---------- CSV 读写（(date,symbol) 幂等覆盖） ----------

def read_rows(path: Path = CSV_PATH) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _write_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(rows, key=lambda r: (r["date"], r["symbol"]))
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CSV_FIELDS})


def _upsert(path: Path, new_rows: list[dict]) -> list[dict]:
    keyed = {(r["date"], r["symbol"]): r for r in read_rows(path)}
    for r in new_rows:
        keyed[(r["date"], r["symbol"])] = r
    rows = list(keyed.values())
    _write_rows(path, rows)
    return rows


# ---------- 基线 ----------

def init(day: str = BASELINE_DATE, *, path: Path = CSV_PATH,
         close_provider=close_on) -> list[dict]:
    """建立基线；同日重复执行不产生重复行（幂等）"""
    existing = [r for r in read_rows(path) if r["date"] == day]
    if existing:
        log.info("影子跟踪基线已存在（%s），跳过重复写入", day)
        return existing
    rows = []
    for s in BUYABLE_SHADOW_SAMPLE:
        close, src = close_provider(s["symbol"], day)
        status = "ok" if close is not None else "未取到"
        rows.append({
            "date": day, "symbol": s["symbol"], "name": s["name"],
            "group": s["group"], "board": s["board"],
            "close": "" if close is None else f"{close:.2f}",
            "day_pct": "" if close is None else "0.00",
            "cum_pct": "" if close is None else "0.00",
            "trade_days": 0, "benchmark": "", "bench_cum": "",
            "rel_pct": "", "status": status, "industry": s["industry"],
            "growth": s["growth"], "valuation": s["valuation"],
            "technical": s["technical"], "capital": s["capital"],
            "score": s["score"],
        })
        log.info("影子基线 %s %s 收盘=%s（%s）", s["symbol"], s["name"], close, src)
    _upsert(path, rows)
    return rows


# ---------- 每日更新 ----------

def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def update(day: str | None = None, *, path: Path = CSV_PATH, out_dir: Path = OUT_DIR,
           close_provider=close_on, benchmark_provider=_benchmark) -> list[dict]:
    """追加/覆盖某一交易日的影子表现（缺价/取价异常标注"未取到"，不静默跳过）"""
    base_rows = {r["symbol"]: r for r in read_rows(path)
                 if r["date"] == BASELINE_DATE}
    if not base_rows:
        raise RuntimeError("影子基线不存在，请先执行 --init")
    day = _latest_closed_day(day)
    prev_rows = {}
    for r in read_rows(path):
        if r["date"] < day and _num(r.get("close")) is not None:
            if r["symbol"] not in prev_rows or r["date"] > prev_rows[r["symbol"]]["date"]:
                prev_rows[r["symbol"]] = r

    try:
        bench_name, bench_bars = benchmark_provider(BASELINE_DATE, day)
    except Exception as exc:
        log.warning("影子基准取价异常 %s: %s", day, exc)
        bench_name, bench_bars = "无基准", []
    bench_base = _num(bench_bars[0].close) if bench_bars else None
    bench_now = _num(bench_bars[-1].close) if bench_bars else None
    bench_cum = round((bench_now / bench_base - 1) * 100, 2) if bench_base and bench_now else None

    rows: list[dict] = []
    for s in BUYABLE_SHADOW_SAMPLE:
        sym = s["symbol"]
        try:
            close, src = close_provider(sym, day)
        except Exception as exc:
            log.warning("影子取价异常 %s %s: %s", sym, day, exc)
            close, src = None, "none"
        base_close = _num(base_rows[sym].get("close"))
        prev_close = _num(prev_rows[sym]["close"]) if sym in prev_rows else base_close
        status = "ok" if close is not None else "未取到"
        day_pct = cum_pct = None
        if close is not None and prev_close:
            day_pct = round((close / prev_close - 1) * 100, 2)
        if close is not None and base_close:
            cum_pct = round((close / base_close - 1) * 100, 2)
        rows.append({
            "date": day, "symbol": sym, "name": s["name"], "group": s["group"],
            "board": s["board"],
            "close": "" if close is None else f"{close:.2f}",
            "day_pct": "" if day_pct is None else f"{day_pct:.2f}",
            "cum_pct": "" if cum_pct is None else f"{cum_pct:.2f}",
            "trade_days": _trade_days_between(BASELINE_DATE, day),
            "benchmark": bench_name,
            "bench_cum": "" if bench_cum is None else f"{bench_cum:.2f}",
            "rel_pct": "", "status": status, "industry": s["industry"],
            "growth": s["growth"], "valuation": s["valuation"],
            "technical": s["technical"], "capital": s["capital"],
            "score": s["score"],
        })
        log.info("影子更新 %s %s 收盘=%s（%s）", sym, s["name"], close, src)

    # 相对基准：基准缺失时用样本等权均值
    ok_cum = [r["cum_pct"] for r in rows if r["status"] == "ok" and r["cum_pct"] != ""]
    mean_cum = round(sum(float(x) for x in ok_cum) / len(ok_cum), 2) if ok_cum else None
    for r in rows:
        cum = _num(r["cum_pct"])
        if cum is None:
            continue
        if bench_cum is not None:
            r["rel_pct"] = f"{cum - bench_cum:.2f}"
        elif mean_cum is not None:
            r["rel_pct"] = f"{cum - mean_cum:.2f}"
            if not r["benchmark"]:
                r["benchmark"] = "无基准（样本等权均值）"
    all_rows = _upsert(path, rows)
    write_report(day, all_rows, out_dir=out_dir)
    return rows


# ---------- 自动补齐缺失交易日 ----------

def backfill(target: str | None = None, *, path: Path = CSV_PATH, out_dir: Path = OUT_DIR,
             close_provider=close_on, benchmark_provider=_benchmark,
             max_days: int = MAX_BACKFILL_DAYS,
             days_provider=_trading_days_between) -> dict:
    """从 track.csv 最后已记录日期起补齐缺失交易日，并重试窗口内取价失败的日期

    - 新缺失日期优先占用单次额度；剩余额度用于重试最近 RETRY_WINDOW_DAYS 个
      已记录交易日内 status=未取到 的日期（覆盖同一 (date,symbol) 行，不产生重复）
    - 超出窗口的历史失败只记录提示，不自动重试
    - 单日单只取价失败只标"未取到"，不中断后续日期
    - 单次最多处理 max_days 个交易日，超出部分留给下次
    """
    existing = read_rows(path)
    if not any(r["date"] == BASELINE_DATE for r in existing):
        raise RuntimeError("影子基线不存在，请先执行 --init")
    day = _latest_closed_day(target)
    last = max((r["date"] for r in existing), default=BASELINE_DATE)
    recorded = {r["date"] for r in existing}
    pending = [d for d in days_provider(last, day) if d not in recorded]

    recent = set(sorted(recorded)[-RETRY_WINDOW_DAYS:])
    failed_dates = {r["date"] for r in existing
                    if r["status"] != "ok" and r["date"] <= day}
    retry = sorted(failed_dates & recent)
    stale = sorted(failed_dates - recent)
    if stale:
        log.warning("影子历史取价失败超出 %d 日重试窗口，不自动重试：%s",
                    RETRY_WINDOW_DAYS, ", ".join(stale))

    to_run_missing = pending[:max_days]
    left = max(0, max_days - len(to_run_missing))
    to_retry = retry[:left]
    to_run = sorted(set(to_run_missing) | set(to_retry))
    overflow = pending[len(to_run_missing):]
    retry_overflow = retry[len(to_retry):]

    filled: list[str] = []
    failed: list[dict] = []
    missing: dict[str, list[str]] = {}
    total = 0
    for d in to_run:
        try:
            rows = update(d, path=path, out_dir=out_dir,
                          close_provider=close_provider,
                          benchmark_provider=benchmark_provider)
        except Exception as exc:
            failed.append({"date": d, "error": str(exc)})
            log.warning("影子补齐 %s 失败：%s", d, exc)
            continue
        filled.append(d)
        total += len(rows)
        miss = [r["name"] for r in rows if r["status"] != "ok"]
        if miss:
            missing[d] = miss

    latest_known = max((r["date"] for r in read_rows(path) if r["date"] <= day),
                       default=BASELINE_DATE)
    return {
        "target": day,
        "last_recorded": last,
        "pending": pending,
        "filled": filled,
        "retried": to_retry,
        "retry_overflow": retry_overflow,
        "stale_failed": stale,
        "overflow": overflow,
        "failed": failed,
        "rows": total,
        "missing": missing,
        "report_day": latest_known,
    }


# ---------- 统计与报告 ----------

def group_stats(rows: list[dict]) -> dict:
    """分组均值（只统计 status=ok 且有累计涨跌的行）"""
    out: dict[str, dict] = {}
    for g in ("model_high", "control_low_base"):
        vals = [float(r["cum_pct"]) for r in rows
                if r.get("group") == g and r.get("status") == "ok"
                and r.get("cum_pct") not in ("", None)]
        out[g] = {
            "count": len(vals),
            "missing": sum(1 for r in rows
                           if r.get("group") == g and r.get("status") != "ok"),
            "mean_cum": round(sum(vals) / len(vals), 2) if vals else None,
        }
    return out


def write_report(day: str, all_rows: list[dict], out_dir: Path = OUT_DIR) -> Path:
    rows = [r for r in all_rows if r["date"] == day]
    rows.sort(key=lambda r: (r["group"] != "model_high", -(_num(r.get("cum_pct")) or -999)))
    stats = group_stats(rows)
    lines = [
        f"# 影子组合（{day}）",
        "",
        "> **旧基线·冻结（2026-10-30 固定 9 只，不再扩充，仅作历史对照）**；"
        "全候选证据请看同目录 `候选面板.csv` / `面板收益.csv`。",
        "",
        "> 纯观察实验：不建仓、不进观察池、不写 positions。"
        "分组：model_high = 模型高分组；control_low_base = 已知低基数对照组（迪安诊断）。",
        f"> 基线日：{BASELINE_DATE}；累计天数：{rows[0]['trade_days'] if rows else 0} 个交易日。",
        "",
        "| 标的 | 分组 | 板块 | 收盘 | 当日% | 累计% | 基准累计% | 相对基准% | 状态 |",
        "|------|------|------|:---:|:---:|:---:|:---:|:---:|:---:|",
    ]
    for r in rows:
        lines.append(
            f"| {r['name']} | {r['group']} | {r['board']} | {r['close'] or '—'} | "
            f"{r['day_pct'] if r['day_pct'] != '' else '—'} | "
            f"{r['cum_pct'] if r['cum_pct'] != '' else '—'} | "
            f"{r['bench_cum'] if r['bench_cum'] != '' else '—'} | "
            f"{r['rel_pct'] if r['rel_pct'] != '' else '—'} | {r['status']} |")
    lines += [
        "",
        "## 分组均值",
        "",
        "| 分组 | 有效样本 | 缺价 | 平均累计% |",
        "|------|:---:|:---:|:---:|",
    ]
    for g in ("model_high", "control_low_base"):
        st = stats[g]
        lines.append(f"| {g} | {st['count']} | {st['missing']} | "
                     f"{st['mean_cum'] if st['mean_cum'] is not None else '—'} |")
    lines += [
        "",
        "## 仅分析不可买（板块权限限制）",
        "",
        "| 标的 | 代码 | 板块 | 说明 |",
        "|------|------|------|------|",
    ]
    if ANALYSIS_ONLY_SHADOW:
        from src.executable import board_of
        for s in ANALYSIS_ONLY_SHADOW:
            lines.append(f"| {s['name']} | {s['code']} | {s['board']} | "
                         f"{board_of(s['code'], s['symbol'])}仅分析不可买（用户只能买主板） |")
    else:
        lines.append("| — | — | — | 无 |")
    lines += [
        "",
        "## 口径说明",
        "",
        f"- 基准：{rows[0]['benchmark'] if rows and rows[0]['benchmark'] else '无基准'}；"
        "取沪深300，缺失回退上证指数；两者都不可用时用样本等权均值比较。",
        "- 收盘价：腾讯前复权优先，新浪不复权兜底；单只取价失败会标「未取到」，不静默跳过。",
        "- 本表只记录价格表现，不构成任何交易建议。",
        "",
    ]
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"影子组合_{day}.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


# ---------- 全候选面板（2026-11-09 起，作为主要证据来源） ----------
#
# 与上方「旧基线 9 只」的关系：
#   - 旧基线（2026-10-30 固定 9 只）保留原样、不再扩充，只作为历史对照，报告标题已标「旧基线·冻结」；
#   - 新证据一律进本节的「候选面板」：每日进入五因子评分的全部候选（含 ABSTAIN、未达门槛），
#     不重复采集——因子读 factor_snapshot、决策/VCP 读 decision_log、收盘价读 daily_bars；
#   - 面板行 append-only（同日重复运行不覆盖、不追加），收益到期后单独追加到「面板收益」，
#     两个文件语义分离：面板=决策当时的事实，收益=事后派生。

PANEL_PATH = OUT_DIR / "候选面板.csv"
PANEL_FIELDS = [
    "day", "symbol", "name", "board", "score",
    "industry", "growth", "valuation", "technical", "capital", "missing",
    "vcp", "contractions", "last_depth", "vol_ratio", "sector_pct",
    "close", "decision",
]
RET_PATH = OUT_DIR / "面板收益.csv"
RET_FIELDS = ["day", "symbol", "horizon", "exit_day", "ret_pct"]


def _read_csv(path: Path, fields: list[str]) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _append_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def _fmt(v, digits: int = 1) -> str:
    if v is None or v == "":
        return ""
    try:
        return f"{float(v):.{digits}f}"
    except (TypeError, ValueError):
        return ""


def _panel_rows(day: str, market_db: str, kb_db: str) -> list[dict]:
    """从已有落库数据组装当日面板行（不联网、不重新评估）

    decision_log 缺该日记录时（2026-11-09 之前的回补），VCP 三项用当日之前的
    daily_bars 现场重算（与线上同一 detect_vcp、同一参数），名称取 watchlist。
    """
    from src.data_source import KlineBar
    from src.vcp import detect_vcp

    with sqlite3.connect(market_db) as m:
        m.row_factory = sqlite3.Row
        snaps = [dict(r) for r in m.execute(
            "SELECT * FROM factor_snapshot WHERE date=?", (day,))]
        closes = {r[0]: r[1] for r in m.execute(
            "SELECT symbol, close FROM daily_bars WHERE date=?", (day,))}
        names = {r[0]: r[1] for r in m.execute("SELECT symbol, name FROM watchlist")}
        hist: dict[str, list[KlineBar]] = {}
        for s in snaps:
            sym = s["symbol"]
            rows = m.execute(
                """SELECT date, open, close, high, low, volume, amount
                   FROM daily_bars WHERE symbol=? AND date<=?
                   ORDER BY date DESC LIMIT 120""", (sym, day)).fetchall()
            hist[sym] = [KlineBar(r["date"], r["open"], r["close"], r["high"],
                                  r["low"], volume=r["volume"], amount=r["amount"])
                         for r in reversed(rows)]
    with sqlite3.connect(kb_db) as k:
        k.row_factory = sqlite3.Row
        decs = {r["symbol"]: dict(r) for r in k.execute(
            "SELECT * FROM decision_log WHERE decision_date=?", (day,))}

    rows = []
    for s in snaps:
        sym = s["symbol"]
        d = decs.get(sym, {})
        depths = []
        if d.get("depths"):
            try:
                depths = json.loads(d["depths"])
            except (TypeError, ValueError):
                depths = []
        vcp = d.get("vcp")
        contractions = d.get("contractions")
        vol_ratio = d.get("vol_ratio")
        sector_pct = d.get("sector_pct")
        if not d:                                   # 无决策记录 → 用历史K线现场重算
            v = hist.get(sym) or []
            if len(v) >= 12:
                r = detect_vcp(v)
                vcp = int(r.is_vcp)
                contractions = r.contractions
                vol_ratio = r.last_vol_ratio
                depths = list(r.depths)
            sector_pct = s.get("industry")          # 与决策侧取值同源（见 r47_closed_loop 938）
        rows.append({
            "day": day, "symbol": sym,
            "name": d.get("name") or names.get(sym, ""),
            "board": s.get("resolved_board", ""),
            "score": _fmt(s.get("score")),
            "industry": _fmt(s.get("industry")), "growth": _fmt(s.get("growth")),
            "valuation": _fmt(s.get("valuation")), "technical": _fmt(s.get("technical")),
            "capital": _fmt(s.get("capital")), "missing": s.get("missing", ""),
            "vcp": "" if vcp is None else str(int(vcp)),
            "contractions": "" if contractions is None else str(int(contractions)),
            "last_depth": _fmt(depths[-1], 2) if depths else "",
            "vol_ratio": _fmt(vol_ratio, 2),
            "sector_pct": _fmt(sector_pct),
            "close": _fmt(closes.get(sym), 2),
            "decision": d.get("decision", ""),
        })
    return rows


def record_panel(day: str | None = None, *, panel_path: Path = PANEL_PATH,
                 market_db: str | None = None, kb_db: str | None = None) -> list[dict]:
    """登记某一日的全部评分候选（append-only：该日已有行则原样返回，不覆盖不追加）"""
    from src.config import DB_PATH, MARKET_DB_PATH

    day = day or _latest_closed_day()
    existing = [r for r in _read_csv(panel_path, PANEL_FIELDS) if r.get("day") == day]
    if existing:
        log.info("候选面板已存在（%s，%d 行），跳过", day, len(existing))
        return existing
    rows = _panel_rows(day, market_db or MARKET_DB_PATH, kb_db or DB_PATH)
    if not rows:
        log.warning("候选面板无数据（%s）：当日无评分候选", day)
        return []
    _append_csv(panel_path, PANEL_FIELDS, rows)
    log.info("候选面板登记 %s：%d 行", day, len(rows))
    return rows


def fill_returns(*, panel_path: Path = PANEL_PATH, ret_path: Path = RET_PATH,
                 market_db: str | None = None,
                 horizons: tuple[int, ...] = (5, 10, 20)) -> list[dict]:
    """到期回填 5/10/20 个交易日收益（只追加新增的 (day,symbol,horizon)）"""
    from src.config import MARKET_DB_PATH

    panel = _read_csv(panel_path, PANEL_FIELDS)
    if not panel:
        return []
    done = {(r["day"], r["symbol"], int(r["horizon"]))
            for r in _read_csv(ret_path, RET_FIELDS)}
    bars: dict[str, list[tuple[str, float]]] = {}
    pos: dict[str, dict[str, int]] = {}
    with sqlite3.connect(market_db or MARKET_DB_PATH) as conn:
        for sym in {r["symbol"] for r in panel}:
            rs = conn.execute(
                "SELECT date, close FROM daily_bars WHERE symbol=? ORDER BY date",
                (sym,)).fetchall()
            bars[sym] = [(d, c) for d, c in rs if c]
            pos[sym] = {d: i for i, (d, _) in enumerate(bars[sym])}

    new: list[dict] = []
    for r in panel:
        sym, day = r["symbol"], r["day"]
        i = pos.get(sym, {}).get(day)
        if i is None:
            continue
        base = bars[sym][i][1]
        if not base:
            continue
        for h in horizons:
            if (day, sym, h) in done or i + h >= len(bars[sym]):
                continue
            exit_day, exit_close = bars[sym][i + h]
            new.append({"day": day, "symbol": sym, "horizon": h,
                        "exit_day": exit_day,
                        "ret_pct": _fmt((exit_close / base - 1) * 100, 2)})
    if new:
        _append_csv(ret_path, RET_FIELDS, new)
    return new


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    """秩相关（同分取平均秩）"""
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for t in order[i:j + 1]:
                r[t] = avg
            i = j + 1
        return r
    n = len(xs)
    if n < 3:
        return None
    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else None


def panel_stats(*, panel_path: Path = PANEL_PATH, ret_path: Path = RET_PATH,
                horizon: int = 20, start: str | None = None,
                end: str | None = None) -> dict:
    """评分 → 未来收益的区分度：逐日 Spearman IC 均值 + 高/低分组收益差

    每日仅用当日候选做横截面排序（样本少时单日 IC 噪声大，故同时给有效天数）。
    """
    panel = {(r["day"], r["symbol"]): r for r in _read_csv(panel_path, PANEL_FIELDS)}
    rets = [r for r in _read_csv(ret_path, RET_FIELDS)
            if int(r["horizon"]) == horizon
            and (start is None or r["day"] >= start)
            and (end is None or r["day"] <= end)]
    by_day: dict[str, list[tuple[float, float]]] = {}
    for r in rets:
        p = panel.get((r["day"], r["symbol"]))
        if not p or p.get("score") in ("", None) or r.get("ret_pct") in ("", None):
            continue
        by_day.setdefault(r["day"], []).append((float(p["score"]), float(r["ret_pct"])))

    ics = [ic for g in by_day.values() if len(g) >= 4
           for ic in [_spearman([a for a, _ in g], [b for _, b in g])] if ic is not None]
    all_pairs = [p for g in by_day.values() for p in g]
    hi, lo = [], []
    for g in by_day.values():
        if len(g) < 4:
            continue
        g = sorted(g)
        k = max(1, len(g) // 3)
        lo += [b for _, b in g[:k]]
        hi += [b for _, b in g[-k:]]
    mean = lambda v: round(sum(v) / len(v), 2) if v else None
    return {
        "horizon": horizon,
        "days": len(by_day),
        "obs": len(all_pairs),
        "ic_mean": round(sum(ics) / len(ics), 3) if ics else None,
        "ic_days": len(ics),
        "ic_positive_share": round(sum(1 for x in ics if x > 0) / len(ics) * 100, 1) if ics else None,
        "top_mean": mean(hi), "top_n": len(hi),
        "bottom_mean": mean(lo), "bottom_n": len(lo),
        "spread": round(mean(hi) - mean(lo), 2) if hi and lo else None,
        "all_mean": mean([b for _, b in all_pairs]),
    }


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="影子跟踪（纯观察，不交易）")
    parser.add_argument("--init", action="store_true", help=f"建立 {BASELINE_DATE} 基线")
    parser.add_argument("--update", action="store_true", help="更新表现（默认行为，自动补齐）")
    parser.add_argument("--date", default=None, help="目标日期 YYYY-MM-DD（默认最近已收盘交易日）")
    parser.add_argument("--no-backfill", action="store_true",
                        help="只更新目标日，不补历史缺失交易日（排查用）")
    parser.add_argument("--panel", action="store_true",
                        help="登记候选面板（append-only）并回填到期收益")
    parser.add_argument("--stats", action="store_true",
                        help="打印评分区分度统计（IC / 高低温分组收益）")
    args = parser.parse_args()

    if args.panel or args.stats:
        if args.panel:
            rows = record_panel(args.date)
            new = fill_returns()
            print(f"✅ 候选面板 {rows[0]['day'] if rows else (args.date or '最近交易日')}："
                  f"{len(rows)} 行（append-only）；到期收益新增 {len(new)} 条")
        if args.stats:
            for h in (5, 10, 20):
                st = panel_stats(horizon=h)
                print(f"  持有 {h:2d} 日：有效 {st['days']} 天 / {st['obs']} 个样本，"
                      f"IC均值 {st['ic_mean']}（{st['ic_days']} 天，正 IC 占比 {st['ic_positive_share']}%），"
                      f"高分组 {st['top_mean']}% vs 低分组 {st['bottom_mean']}%"
                      f"（差 {st['spread']}%，全样本 {st['all_mean']}%）")
        return

    if args.init:
        day = args.date or BASELINE_DATE
        rows = init(day)
        write_report(day, read_rows(), out_dir=OUT_DIR)
        print(f"✅ 基线已建立：{len(rows)} 只（{day}）")
        print(f"📄 {CSV_PATH}")
        return

    if args.no_backfill:
        rows = update(args.date)
        day = rows[0]["date"] if rows else (args.date or "")
        print(f"✅ 影子更新完成：{len(rows)} 只（{day}，未补齐历史）")
    else:
        summary = backfill(args.date)
        filled = summary["filled"]
        print(f"✅ 影子补齐完成：目标日 {summary['target']}，"
              f"补齐 {len(filled)} 个交易日，共 {summary['rows']} 行")
        print(f"   已补：{', '.join(filled) or '无（没有缺失交易日）'}")
        if summary["retried"]:
            print(f"   🔁 重试已记录失败日：{', '.join(summary['retried'])}")
        if summary["retry_overflow"]:
            print(f"   ⏳ 单次额度不足，顺延重试：{', '.join(summary['retry_overflow'])}")
        if summary["stale_failed"]:
            print(f"   ℹ️ 超出最近 {RETRY_WINDOW_DAYS} 个交易日窗口，"
                  f"不自动重试：{', '.join(summary['stale_failed'])}")
        if summary["overflow"]:
            print(f"   ⏳ 超出单次上限 {MAX_BACKFILL_DAYS} 日，下次继续："
                  f"{', '.join(summary['overflow'])}")
        for f in summary["failed"]:
            print(f"   ⚠️ {f['date']} 补齐失败：{f['error']}")
        for d, names in summary["missing"].items():
            print(f"   ⚠️ {d} 未取到：{'、'.join(names)}")
        day = summary["report_day"]

    p = OUT_DIR / f"影子组合_{day}.md"
    if p.exists():
        print(p.read_text(encoding="utf-8"))
    else:
        print(write_report(day, read_rows(), out_dir=OUT_DIR).read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
