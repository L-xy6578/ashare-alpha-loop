"""板块反转影子台账（2026-11-11 新增观察层，不改任何口径）

背景：
    扩样本回测里唯一有方向的线索是「板块 20 日涨幅高 → 之后 20 日回落」
    （同日超额：<50 分位 +0.175%，≥85 分位 −0.552%），但非重叠独立时点只有 32 个，
    过拟合风险高。本模块用**真实时点、无前视**的日频记录，在冻结期（2026-11-11 起
    20 个交易日）继续观察这条假设是否成立。

硬约束（用户明确）：
    - **只记录**：不建仓、不进观察池、不改评分/权重/桶表/门槛/ABSTAIN/ENTRY；
    - 挂载位置在决策落库之后（收盘闭环 6.9），结构上不可能影响决策；
    - 失败只进 warnings，不改收盘闭环退出码；
    - 不写 knowledge.db，不动 decision_plans / decision_trades；
    - 数据不够就留 NULL，不编造。

记录内容（每个交易日、每只主板观察标的）：
    - 所属行业板块的 20 日涨幅（%）、该涨幅在当日**行业板块池**中的分位（0-100）；
    - 分位档位：<50 / 50~65 / 65~85 / ≥85；
    - 个股当日收盘价；
    - 之后 5/10/20 个交易日的实际涨跌与同日沪深300 超额（按交易日历回填）。

口径：
    - 板块 20 日涨幅 = `sector_price_history` 当日收盘 / 20 个交易日前收盘 − 1；
    - 分位池 = 当日 `kind='plate'`（仅行业板块，概念板块不参与），
      剔除 zdf20 为 NULL / 0 者，池内 < `MIN_BOARD_POOL` 记缺失；
      直接复用生产 `src.factors.industry_percentile_factor`，保证与评分同尺；
    - 观察池内外 = `watchlist` 全部主板标的（active=池内，removed=池外对照）；
    - 超额基准 = 沪深300（sh000300）同期涨跌，缺失回退上证指数（sh000001），
      两者都没有则超额留 NULL。

用法:
    python -m src.sector_reversal_shadow --record   # 记录最近一个数据齐全交易日
    python -m src.sector_reversal_shadow --backfill # 回填可算出的 5/10/20 日收益
    python -m src.sector_reversal_shadow --report   # 产出 markdown 报表
    python -m src.sector_reversal_shadow --all      # 记录 + 回填 + 报表
"""

from __future__ import annotations

import argparse
import math
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.config import MARKET_DB_PATH
from src.db import get_connection
from src.executable import is_buyable_code
from src.factors import MIN_BOARD_POOL, industry_percentile_factor, industry_pool, _resolve_plate
from src.logger import get_logger

log = get_logger(__name__)

HORIZONS = (5, 10, 20)
BUCKETS = ("<50", "50~65", "65~85", "≥85")
OUT_DIR = Path("outputs") / "板块反转影子"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sector_reversal_shadow (
    date        TEXT NOT NULL,              -- 信号日 d（真实交易日）
    symbol      TEXT NOT NULL,
    code        TEXT NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    in_pool     INTEGER NOT NULL DEFAULT 1, -- 1=观察池内(active) / 0=池外(removed)
    sector      TEXT NOT NULL DEFAULT '',   -- watchlist.sector 原值
    board       TEXT,                       -- 解析到的行业板块名（解析不到 NULL）
    board_zdf20 REAL,                       -- 板块 20 日涨幅 %
    sector_pct  REAL,                       -- 当日行业板块分位 0-100（算不出 NULL）
    bucket      TEXT,                       -- <50 / 50~65 / 65~85 / ≥85
    close       REAL,                       -- 个股当日收盘（缺数据 NULL）
    base_date   TEXT,                       -- 20 日回看基准日
    ret5        REAL, excess5  REAL,
    ret10       REAL, excess10 REAL,
    ret20       REAL, excess20 REAL,
    filled_at   TEXT,
    PRIMARY KEY (date, symbol)
)
"""
INDEX_SQL = ("CREATE INDEX IF NOT EXISTS idx_srs_date "
             "ON sector_reversal_shadow(date)")


# ============================================================
# 板块分位（与生产 industry_percentile_factor 同一把尺子）
# ============================================================

@dataclass(frozen=True)
class _Plate:
    """满足 industry_percentile_factor 所需的最小板块快照"""

    name: str
    code: str
    kind: str
    zdf20: float | None


def _trading_days_upto(market_db: str, day: str, n: int) -> list[str]:
    """day（含）往前 n 个交易日，升序；日历不可用时返回空列表"""
    try:
        with get_connection(market_db, readonly=True) as conn:
            rows = conn.execute(
                "SELECT date FROM trade_calendar WHERE is_trading=1 AND date<=? "
                "ORDER BY date DESC LIMIT ?", (day, n)).fetchall()
    except Exception as exc:
        log.warning("交易日历读取失败（%s）：%s", day, exc)
        return []
    return [r[0] for r in reversed(rows)]


def load_plates(market_db: str, day: str) -> tuple[list[_Plate], str | None]:
    """当日行业板块快照（20 日涨幅），返回 (plates, 基准日)

    plates 只含 kind='plate'；历史不足 21 个交易日时返回 ([], None)，
    调用方必须按缺失处理，不得用 0 或继承值顶替。
    """
    window = _trading_days_upto(market_db, day, 21)
    if len(window) < 21:
        return [], None
    base = window[0]
    try:
        with get_connection(market_db, readonly=True) as conn:
            cur = conn.execute(
                "SELECT code, name, kind, close FROM sector_price_history WHERE date=?",
                (day,)).fetchall()
            old = {r[0]: r[1] for r in conn.execute(
                "SELECT code, close FROM sector_price_history WHERE date=?", (base,))}
    except Exception as exc:
        log.warning("板块历史读取失败（%s）：%s", day, exc)
        return [], None

    plates: list[_Plate] = []
    for code, name, kind, close in cur:
        if kind != "plate" or not close:
            continue
        prev = old.get(code)
        if not prev or prev <= 0:
            continue
        plates.append(_Plate(name=name or "", code=code, kind=kind,
                             zdf20=round((close / prev - 1) * 100, 2)))
    return plates, base


def bucket_of(pct: float | None) -> str | None:
    """分位档位（<50 / 50~65 / 65~85 / ≥85）；缺失返回 None"""
    if pct is None:
        return None
    if pct < 50:
        return "<50"
    if pct < 65:
        return "50~65"
    if pct < 85:
        return "65~85"
    return "≥85"


# ============================================================
# 写入 / 回填
# ============================================================

def latest_data_date(market_db: str, day: str | None = None) -> str | None:
    """最近一个「个股日K + 板块历史」都齐全的交易日（个股含新补采的样本日K）"""
    try:
        with get_connection(market_db, readonly=True) as conn:
            if day:
                row = conn.execute("""
                    SELECT MAX(date) FROM (
                      SELECT date FROM daily_bars WHERE date<=?
                      UNION SELECT date FROM shadow_daily_bars WHERE date<=?
                    )""", (day, day)).fetchone()
            else:
                row = conn.execute("""
                    SELECT MAX(b.date) FROM
                      (SELECT date FROM daily_bars
                       UNION SELECT date FROM shadow_daily_bars) b
                    JOIN (SELECT DISTINCT date FROM sector_price_history) s
                      ON s.date = b.date""").fetchone()
        return row[0] if row and row[0] else None
    except Exception as exc:
        log.warning("数据日期探测失败（回退 daily_bars）：%s", exc)
        try:
            with get_connection(market_db, readonly=True) as conn:
                row = conn.execute("SELECT MAX(date) FROM daily_bars").fetchone()
            return row[0] if row and row[0] else None
        except Exception:
            return None


def _universe(market_db: str) -> list[dict]:
    """观察池内外的全部主板标的

    - 池内：`watchlist` 中 active 的主板标的；
    - 池外：固定抽样名单 `shadow_sample`（200 只，含已移出观察池的标的）；
    同一标的以观察池信息为准（池内优先级最高）。
    """
    out: dict[str, dict] = {}
    try:
        with get_connection(market_db, readonly=True) as conn:
            rows = conn.execute(
                """SELECT symbol, code, name, sector, status FROM watchlist
                   WHERE status='active' ORDER BY symbol""").fetchall()
    except Exception as exc:
        log.warning("观察池读取失败：%s", exc)
        rows = []
    for symbol, code, name, sector, status in rows:
        if not is_buyable_code(code, symbol)[0]:   # 只能买主板，与可执行性口径一致
            continue
        out[symbol] = {"symbol": symbol, "code": code, "name": name or "",
                       "sector": sector or "", "in_pool": 1 if status == "active" else 0}

    try:
        with get_connection(market_db, readonly=True) as conn:
            sample = conn.execute(
                "SELECT symbol, code, name, industry FROM shadow_sample").fetchall()
    except Exception:
        sample = []
    for symbol, code, name, industry in sample:
        if symbol in out:
            continue
        out[symbol] = {"symbol": symbol, "code": code, "name": name or "",
                       "sector": industry or "", "in_pool": 0}
    return [out[k] for k in sorted(out)]


def record(day: str | None = None, *, market_db: str = MARKET_DB_PATH,
           dry_run: bool = False) -> dict:
    """记录某一交易日的板块反转影子样本（幂等：同日重复运行不新增行）

    Returns: {"day","rows","inserted","skipped","reason"}
    """
    if dry_run:
        return {"day": day, "rows": 0, "inserted": 0, "skipped": True,
                "reason": "dry-run 跳过（不写库）"}
    day = day or latest_data_date(market_db)
    if not day:
        return {"day": None, "rows": 0, "inserted": 0, "skipped": True,
                "reason": "无可用交易日数据"}
    universe = _universe(market_db)
    if not universe:
        return {"day": day, "rows": 0, "inserted": 0, "skipped": True,
                "reason": "观察池与抽样名单均为空"}
    plates, base_date = load_plates(market_db, day)
    pool = industry_pool(plates)
    try:
        with get_connection(market_db, readonly=True) as conn:
            closes = {r[0]: r[1] for r in conn.execute(
                "SELECT symbol, close FROM daily_bars WHERE date=?", (day,))}
            # 新采集的 200 只样本优先（取到即覆盖生产库旧值，取不到回退 daily_bars）
            try:
                for sym, close in conn.execute(
                        "SELECT symbol, close FROM shadow_daily_bars WHERE date=?", (day,)):
                    if close:
                        closes[sym] = close
            except Exception:
                pass
    except Exception as exc:
        log.warning("个股收盘读取失败（%s）：%s", day, exc)
        closes = {}

    rows = []
    for s in universe:
        pct = industry_percentile_factor(pool, s["sector"]) if pool else None
        chosen = _resolve_plate(pool, s["sector"]) if pool else None
        rows.append((
            day, s["symbol"], s["code"], s["name"], s["in_pool"], s["sector"],
            chosen.name if chosen else None,
            chosen.zdf20 if chosen else None,
            pct, bucket_of(pct), closes.get(s["symbol"]), base_date,
        ))

    with get_connection(market_db) as conn:
        conn.execute(SCHEMA_SQL)
        conn.execute(INDEX_SQL)
        before = conn.execute(
            "SELECT COUNT(*) FROM sector_reversal_shadow WHERE date=?", (day,)).fetchone()[0]
        conn.executemany(
            """INSERT INTO sector_reversal_shadow
                 (date, symbol, code, name, in_pool, sector, board, board_zdf20,
                  sector_pct, bucket, close, base_date)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(date, symbol) DO UPDATE SET
                 close = COALESCE(sector_reversal_shadow.close, excluded.close),
                 board = COALESCE(sector_reversal_shadow.board, excluded.board),
                 board_zdf20 = COALESCE(sector_reversal_shadow.board_zdf20,
                                        excluded.board_zdf20),
                 sector_pct = COALESCE(sector_reversal_shadow.sector_pct,
                                       excluded.sector_pct),
                 bucket = COALESCE(sector_reversal_shadow.bucket, excluded.bucket)""",
            rows)
        after = conn.execute(
            "SELECT COUNT(*) FROM sector_reversal_shadow WHERE date=?", (day,)).fetchone()[0]
    return {"day": day, "rows": len(rows), "inserted": after - before,
            "skipped": False, "reason": ""}


def _series(conn, symbol: str) -> list[tuple[str, float]]:
    """个股收盘序列：新采集的 shadow_daily_bars 优先，缺失回退 daily_bars"""
    merged: dict[str, float] = {}
    for table in ("daily_bars", "shadow_daily_bars"):   # 后者覆盖前者
        try:
            for d, c in conn.execute(
                    f"SELECT date, close FROM {table} WHERE symbol=? ORDER BY date",
                    (symbol,)):
                if c:
                    merged[d] = c
        except Exception:
            continue
    return sorted(merged.items())


def _benchmark_series(conn) -> tuple[str, list[tuple[str, float]]]:
    """沪深300 → 上证指数 → 无基准"""
    for symbol, name in (("sh000300", "沪深300"), ("sh000001", "上证指数")):
        series = _series(conn, symbol)
        if series:
            return name, series
    return "无基准", []


def backfill(*, market_db: str = MARKET_DB_PATH, dry_run: bool = False,
             horizons: tuple[int, ...] = HORIZONS) -> dict:
    """回填之后 5/10/20 日的实际涨跌与同日大盘超额（只补算得出来的，算不出留 NULL）"""
    stats = {"checked": 0, "updated": 0, "skipped": False, "benchmark": "无基准"}
    if dry_run:
        stats["skipped"] = True
        return stats
    try:
        with get_connection(market_db, readonly=True) as conn:
            rows = conn.execute(
                """SELECT date, symbol FROM sector_reversal_shadow
                   WHERE ret5 IS NULL OR ret10 IS NULL OR ret20 IS NULL
                   ORDER BY date, symbol""").fetchall()
            bench_name, bench = _benchmark_series(conn)
            bench_pos = {d: i for i, (d, _) in enumerate(bench)}
            series = {sym: _series(conn, sym) for sym in {r[1] for r in rows}}
    except Exception as exc:
        log.warning("回填读取失败：%s", exc)
        return stats
    stats["benchmark"] = bench_name
    stats["checked"] = len(rows)

    updates = []
    for day, symbol in rows:
        bars = series.get(symbol) or []
        pos = {d: i for i, (d, _) in enumerate(bars)}
        i = pos.get(day)
        if i is None:
            continue
        base = bars[i][1]
        bi = bench_pos.get(day)
        vals: dict[str, float | None] = {}
        for h in horizons:
            if i + h >= len(bars):
                vals[h] = None
                continue
            exit_day, exit_close = bars[i + h]
            ret = (exit_close / base - 1) * 100
            excess = None
            if bi is not None and bi + h < len(bench) and bench[bi][1]:
                b_h = bench[bi + h]
                if b_h[0] == exit_day:      # 交易日必须对齐，错位不硬凑
                    excess = ret - (b_h[1] / bench[bi][1] - 1) * 100
            vals[h] = ret
            vals[f"excess{h}"] = excess
        if not any(vals.get(h) is not None for h in horizons):
            continue
        updates.append((
            vals.get(5), vals.get("excess5"),
            vals.get(10), vals.get("excess10"),
            vals.get(20), vals.get("excess20"),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"), day, symbol,
        ))

    if not updates:
        return stats
    try:
        with get_connection(market_db) as conn:
            conn.executemany(
                """UPDATE sector_reversal_shadow SET
                     ret5=COALESCE(ret5, ?), excess5=COALESCE(excess5, ?),
                     ret10=COALESCE(ret10, ?), excess10=COALESCE(excess10, ?),
                     ret20=COALESCE(ret20, ?), excess20=COALESCE(excess20, ?),
                     filled_at=?
                   WHERE date=? AND symbol=?""", updates)
    except Exception as exc:
        log.warning("回填写入失败：%s", exc)
        return stats
    stats["updated"] = len(updates)
    return stats


# ============================================================
# 报表
# ============================================================

def _mean(xs):
    return round(statistics.fmean(xs), 3) if xs else None


def _median(xs):
    return round(statistics.median(xs), 3) if xs else None


def summarize(rows: list[dict]) -> dict:
    """按档位 × 持有期汇总（只统计非 NULL 样本）"""
    out = {}
    for b in BUCKETS:
        out[b] = {}
        for h in HORIZONS:
            rets = [r[f"ret{h}"] for r in rows if r["bucket"] == b and r.get(f"ret{h}") is not None]
            excs = [r[f"excess{h}"] for r in rows if r["bucket"] == b and r.get(f"excess{h}") is not None]
            out[b][h] = {
                "n": len(rets),
                "ret_mean": _mean(rets),
                "ret_median": _median(rets),
                "excess_mean": _mean(excs),
                "excess_median": _median(excs),
                "excess_win": (round(sum(1 for x in excs if x > 0) / len(excs) * 100, 1)
                               if excs else None),
            }
    return out


def write_report(*, market_db: str = MARKET_DB_PATH,
                 out_dir: Path = OUT_DIR, day: str | None = None) -> Path:
    """产出 markdown 报表（各档位实际超额收益一眼可见）"""
    try:
        with get_connection(market_db, readonly=True) as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM sector_reversal_shadow ORDER BY date, symbol")]
    except Exception as exc:
        log.warning("报表读取失败：%s", exc)
        rows = []
    day = day or (rows[-1]["date"] if rows else None)
    sample = {"counts": {}, "today": {}, "seed": None, "sampled_for": None, "rule": ""}
    try:
        with get_connection(market_db, readonly=True) as conn:
            for b, n in conn.execute(
                    "SELECT bucket, COUNT(*) FROM shadow_sample GROUP BY bucket"):
                sample["counts"][b] = n
            meta = conn.execute(
                "SELECT seed, sampled_for, rule FROM shadow_sample LIMIT 1").fetchone()
            if meta:
                sample.update(seed=meta[0], sampled_for=meta[1], rule=meta[2] or "")
            if day:
                for b, n in conn.execute(
                        """SELECT s.bucket, COUNT(*) FROM shadow_sample s
                           JOIN shadow_daily_bars b ON b.symbol = s.symbol AND b.date = ?
                           WHERE b.close IS NOT NULL GROUP BY s.bucket""", (day,)):
                    sample["today"][b] = n
    except Exception:
        pass
    day = day or datetime.now().date().isoformat()
    stats = summarize(rows)
    days = sorted({r["date"] for r in rows})
    lines = [
        f"# 板块反转影子台账（{day}）",
        "",
        "> **只记录，不影响生产决策**：不建仓、不改评分/权重/桶表/门槛，"
        "不参与 ABSTAIN/ENTRY 判定；挂在决策落库之后。",
        "> 验证假设：板块 20 日涨幅分位越高 → 之后 20 日越可能回落。",
        "",
        f"- 记录区间：{days[0] if days else '—'} ~ {days[-1] if days else '—'}"
        f"（{len(days)} 个交易日，{len(rows)} 行）",
        "- 档位＝信号日所属行业板块 20 日涨幅在当日行业板块池中的分位；"
        "分位算不出记 NULL（不编造）。",
        "- 收益＝该股之后 5/10/20 个交易日收盘涨跌；超额＝收益 − 同期沪深300"
        "（缺失回退上证指数，两者都无则 NULL）。",
        "",
        "## 各档位实际超额（按持有期）",
        "",
        "| 档位 | 持有 | 样本 | 收益均值% | 收益中位% | 超额均值% | 超额中位% | 跑赢大盘占比% |",
        "|------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|",
    ]
    for b in BUCKETS:
        for h in HORIZONS:
            st = stats[b][h]
            if not st["n"]:
                lines.append(f"| {b} | {h}日 | 0 | — | — | — | — | — |")
                continue
            lines.append(
                f"| {b} | {h}日 | {st['n']} | {st['ret_mean']} | {st['ret_median']} | "
                f"{st['excess_mean']} | {st['excess_median']} | {st['excess_win']} |")
    sample_total = sum(sample["counts"].values())
    today_total = sum(sample["today"].values())
    pool_out = [r for r in rows if not r["in_pool"]]
    pool_out_ok = [r for r in pool_out if r["close"] is not None]
    lines += [
        "",
        "## 样本够不够（固定抽样名单）",
        "",
        f"- 固定名单 {sample_total} 只（各档 50），抽样基准日 {sample['sampled_for'] or '—'}，"
        f"seed={sample['seed']}，规则：{sample['rule'] or '—'}。名单一次性生成，不每日重抽。",
        f"- **当前信号日 {day} 名单有价覆盖率：{today_total}/{sample_total}"
        f"（{round(today_total / sample_total * 100, 1) if sample_total else 0}%）**；"
        f"对照（池外）样本覆盖率：{len(pool_out_ok)}/{len(pool_out)}"
        f"（{round(len(pool_out_ok) / len(pool_out) * 100, 1) if pool_out else 0}%）。",
        "",
        "| 档位 | 固定名单 | 当日名单有价 | 台账有效行(有价) | 已回填5日 | 已回填10日 | 已回填20日 |",
        "|------|:---:|:---:|:---:|:---:|:---:|:---:|",
    ]
    for b in BUCKETS:
        valid = [r for r in rows if r["bucket"] == b and r["close"] is not None]
        lines.append(
            f"| {b} | {sample['counts'].get(b, 0)} | {sample['today'].get(b, 0)} | "
            f"{len(valid)} | "
            f"{sum(1 for r in valid if r.get('ret5') is not None)} | "
            f"{sum(1 for r in valid if r.get('ret10') is not None)} | "
            f"{sum(1 for r in valid if r.get('ret20') is not None)} |")
    lines += [
        "",
        "## 覆盖与缺口",
        "",
        f"- 分位可算行：{sum(1 for r in rows if r['sector_pct'] is not None)} / {len(rows)}；"
        f"板块解析失败：{sum(1 for r in rows if not r['board'])}；"
        f"个股当日价缺失：{sum(1 for r in rows if r['close'] is None)}。",
    ]
    for h in HORIZONS:
        n = sum(1 for r in rows if r.get(f"ret{h}") is not None)
        lines.append(f"- {h} 日收益已回填：{n} / {len(rows)}（未到期的留 NULL，不编造）。")
    lines += [
        "",
        "## 最近信号日明细",
        "",
        "| 日期 | 标的 | 池内 | 板块 | 板块20日% | 分位 | 档位 | 收盘 | 5日超额% | 10日超额% | 20日超额% |",
        "|------|------|:---:|------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|",
    ]
    recent = [r for r in rows if r["date"] == days[-1]] if days else []
    for r in recent:
        lines.append(
            f"| {r['date']} | {r['name']}({r['code']}) | "
            f"{'是' if r['in_pool'] else '否'} | {r['board'] or '—'} | "
            f"{'—' if r['board_zdf20'] is None else r['board_zdf20']} | "
            f"{'—' if r['sector_pct'] is None else r['sector_pct']} | "
            f"{r['bucket'] or '—'} | {'—' if r['close'] is None else r['close']} | "
            f"{'—' if r['excess5'] is None else r['excess5']} | "
            f"{'—' if r['excess10'] is None else r['excess10']} | "
            f"{'—' if r['excess20'] is None else r['excess20']} |")
    if not recent:
        lines.append("| — | — | — | — | — | — | — | — | — | — | — |")
    lines += [
        "",
        "## 口径与复查",
        "",
        "- 板块 20 日涨幅：`sector_price_history` 当日收盘 ÷ 20 个交易日前收盘 − 1。",
        "- 分位池：仅 `kind='plate'` 行业板块，剔除 zdf20 为 NULL/0 者，"
        f"池内 < {MIN_BOARD_POOL} 个记缺失；复用 `src.factors.industry_percentile_factor`。",
        "- 主键 `(date, symbol)`：同日重跑不重复写入；回填只补算得出的，已有值不覆盖。",
        "",
    ]
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"板块反转影子_{day}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run(day: str | None = None, *, market_db: str = MARKET_DB_PATH,
        dry_run: bool = False) -> dict:
    """记录 + 回填 + 报表（供收盘闭环调用；异常由调用方降级为 warnings）"""
    rec = record(day, market_db=market_db, dry_run=dry_run)
    bf = backfill(market_db=market_db, dry_run=dry_run)
    path = None if dry_run else write_report(market_db=market_db, day=rec.get("day"))
    return {"record": rec, "backfill": bf, "report": str(path) if path else None}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="板块反转影子台账（只记录，不影响决策）")
    p.add_argument("--record", action="store_true", help="记录最近数据齐全交易日")
    p.add_argument("--backfill", action="store_true", help="回填 5/10/20 日收益")
    p.add_argument("--report", action="store_true", help="产出 markdown 报表")
    p.add_argument("--all", action="store_true", help="记录 + 回填 + 报表")
    p.add_argument("--date", default=None, help="指定信号日 YYYY-MM-DD")
    p.add_argument("--dry-run", action="store_true", help="只演练，不写库不落盘")
    p.add_argument("--market-db", default=MARKET_DB_PATH)
    args = p.parse_args()

    if args.all or args.record:
        print(record(args.date, market_db=args.market_db, dry_run=args.dry_run))
    if args.all or args.backfill:
        print(backfill(market_db=args.market_db, dry_run=args.dry_run))
    if args.all or args.report:
        if args.dry_run:
            print("（dry-run 跳过报表落盘）")
        else:
            print(write_report(market_db=args.market_db, day=args.date))
    if not (args.all or args.record or args.backfill or args.report):
        p.print_help()


if __name__ == "__main__":
    main()
