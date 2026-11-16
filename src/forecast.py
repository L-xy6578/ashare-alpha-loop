"""每日预判 + 到期对账 + 累计命中率（独立于五因子评分/选股）

设计约束（2026-11-09 冻结期前新增，不改动任何既有模型参数）：
- 预判只读行情/板块/评分数据，不反写 factor_snapshot，不影响选股与评分；
- 每条预判落盘后 append-only：direction/statement/rationale 等字段由 SQLite 触发器锁定，
  仅允许结算字段（status/actual_return/benchmark_return/breadth/settled_at/settle_note）更新；
- 个股预判来源限定为“当日已打过分且可买”的标的，无合格标的明确写“无”，不硬凑；
  score 直接复用决策侧 factor_snapshot（同一交易日、同一标的、同一份入参的落库结果，
  由 r47_closed_loop.py 收盘闭环写入），预判不再自建快照、不再重算；
  表中任一因子为 NULL 即判缺失、不出分（显示“—（缺…，不出分）”），不用 0 分占位——
  与资金因子无数据返回 None、技术面 K 线不新鲜返回 None 的规矩一致。
  该分数只用于同日排序展示，不是第二套评分、不是置信度，也不作为开仓依据；
  开仓/弃权一律以决策日志（五因子 ≥85 且无因子缺失）为准。
- 判断全部写成可验证陈述，并带数据截止日与 git HEAD 短哈希。

用法：
    python -m src.forecast --data-date 2026-11-06          # 生成 + 结算到期预判
    python -m src.forecast --settle-only                   # 只结算已到期预判
"""

from __future__ import annotations

import bisect
import math
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable

from src.config import DB_PATH, DB_TIMEOUT, MARKET_DB_PATH, ROOT
from src.data_source import KlineBar
from src.executable import check_eligibility
from src.factors import growth_from_research, score_candidate
from src.sector_analysis import SectorAnalyzer

# ============================================================
# 常量（预判专用，不修改五因子/门槛/VCP/止损止盈参数）
# ============================================================
FORECAST_DIR = ROOT / "outputs" / "预判"
MARKET_SYMBOL = "sh000300"
INDEX_SYMBOLS = {"sh000001", "sh000300", "sz399001", "sz399006"}

HORIZONS = (1, 5)                 # 交易日
UP_DOWN_THRESHOLD = 0.5           # 大盘涨/跌判定阈值（%）
MIN_PLATE_BOARDS = 400            # 板块快照完整度门槛（行业板块条数）
MIN_COMPARABLE_SECTORS = 20       # 板块相对强弱对账的最小可比板块数

SECTOR_TOP_N = 5                  # 看强/看弱各取前 N
SECTOR_MIN_VALID = 40             # 有效板块数下限

STOCK_MAX_PICKS = 3               # 个股预判最多 3 只
STOCK_MIN_TECHNICAL = 60.0        # 预判筛选：技术面不弱
STOCK_MIN_INDUSTRY = 50.0         # 预判筛选：行业景气不弱于中位
STOCK_MIN_CAPITAL = 50.0          # 预判筛选：资金不弱于中位

FORECAST_TABLE = """
CREATE TABLE IF NOT EXISTS forecast_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL DEFAULT 'live',          -- live=每日实盘预判；backtest=历史回溯（本模块不落库）
    predict_date TEXT NOT NULL,                 -- 数据截止交易日
    run_date TEXT NOT NULL,                     -- 生成日
    scope TEXT NOT NULL,                        -- market / sector / stock
    target TEXT NOT NULL,                       -- 指数代码 / 板块代码 / 标的代码
    target_name TEXT NOT NULL,
    horizon INTEGER NOT NULL,                   -- 1 / 5 个交易日
    direction TEXT NOT NULL,                    -- 上涨 / 下跌 / 震荡 / 看强 / 看弱 / 上涨（个股）
    strength TEXT NOT NULL,
    statement TEXT NOT NULL,                    -- 可验证陈述
    rationale TEXT NOT NULL,                    -- 依据
    score REAL,                                 -- 个股记录：复用决策侧 factor_snapshot.score；
                                                -- 任一因子缺失（表中 NULL）时写 NULL = 不出分，不用 0 分占位
    target_date TEXT NOT NULL,                  -- 到期交易日
    data_date TEXT NOT NULL,
    git_head TEXT NOT NULL DEFAULT '',
    git_dirty INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    status TEXT NOT NULL DEFAULT 'PENDING',     -- PENDING / HIT / MISS / UNVERIFIABLE / VOID
    actual_return REAL,
    benchmark_return REAL,
    breadth REAL,
    settled_at TEXT,
    settle_note TEXT,
    voided INTEGER NOT NULL DEFAULT 0,          -- 1=作废样本，不进命中率
    void_reason TEXT,
    UNIQUE(mode, predict_date, run_date, scope, target, horizon)
);
"""

FORECAST_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS forecast_ledger_immutable_update
BEFORE UPDATE OF mode, predict_date, run_date, scope, target, target_name, horizon,
                 direction, strength, statement, rationale, score, target_date,
                 data_date, git_head, git_dirty, created_at
ON forecast_ledger
BEGIN
    SELECT RAISE(ABORT, '预判内容 append-only，不允许修改');
END;

CREATE TRIGGER IF NOT EXISTS forecast_ledger_immutable_delete
BEFORE DELETE ON forecast_ledger
BEGIN
    SELECT RAISE(ABORT, '预判内容 append-only，不允许删除');
END;

CREATE INDEX IF NOT EXISTS idx_forecast_scope ON forecast_ledger(mode, scope, predict_date);
CREATE INDEX IF NOT EXISTS idx_forecast_pending ON forecast_ledger(mode, status, target_date);
"""


# ============================================================
# 基础工具
# ============================================================
def init_forecast_tables(db_path: str = DB_PATH) -> None:
    """幂等建表 + append-only 触发器 + 唯一键迁移。

    唯一键从 (mode, predict_date, scope, target, horizon) 迁移为
    (mode, predict_date, run_date, scope, target, horizon)：同一数据截止日
    在不同生成日重跑时必须能留痕并存，而不是被静默忽略。
    """
    with sqlite3.connect(db_path, timeout=DB_TIMEOUT) as conn:
        conn.executescript(FORECAST_TABLE)
        _migrate_unique_key(conn)
        _ensure_void_columns(conn)
        conn.executescript(FORECAST_TRIGGERS)
        conn.commit()


def _ensure_void_columns(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(forecast_ledger)")}
    if "voided" not in cols:
        conn.execute("ALTER TABLE forecast_ledger ADD COLUMN voided INTEGER NOT NULL DEFAULT 0")
    if "void_reason" not in cols:
        conn.execute("ALTER TABLE forecast_ledger ADD COLUMN void_reason TEXT")


def _migrate_unique_key(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='forecast_ledger'"
    ).fetchone()
    if not row or not row[0]:
        return
    normalized = " ".join(row[0].split())
    if "UNIQUE(mode, predict_date, run_date" in normalized:
        return

    old_cols = [r[1] for r in conn.execute("PRAGMA table_info(forecast_ledger)")]
    conn.executescript(
        "DROP TRIGGER IF EXISTS forecast_ledger_immutable_update;"
        "DROP TRIGGER IF EXISTS forecast_ledger_immutable_delete;"
        "DROP INDEX IF EXISTS idx_forecast_scope;"
        "DROP INDEX IF EXISTS idx_forecast_pending;"
    )
    conn.execute("ALTER TABLE forecast_ledger RENAME TO forecast_ledger_old")
    conn.executescript(FORECAST_TABLE)
    new_cols = [r[1] for r in conn.execute("PRAGMA table_info(forecast_ledger)")]
    common = [c for c in old_cols if c in new_cols]
    collist = ", ".join(common)
    conn.execute(
        f"INSERT INTO forecast_ledger ({collist}) SELECT {collist} FROM forecast_ledger_old"
    )
    conn.execute("DROP TABLE forecast_ledger_old")


def mark_void(predict_date: str, reason: str, run_date: str = "",
              db_path: str = DB_PATH, mode: str = "live") -> int:
    """把指定数据截止日的预判标为作废：只改状态字段，不删除、不改判断内容。"""
    init_forecast_tables(db_path)
    with sqlite3.connect(db_path, timeout=DB_TIMEOUT) as conn:
        if run_date:
            cur = conn.execute(
                "UPDATE forecast_ledger SET status='VOID', voided=1, void_reason=?, "
                "settled_at=datetime('now','localtime'), settle_note=? "
                "WHERE mode=? AND predict_date=? AND run_date=?",
                (reason, reason, mode, predict_date, run_date),
            )
        else:
            cur = conn.execute(
                "UPDATE forecast_ledger SET status='VOID', voided=1, void_reason=?, "
                "settled_at=datetime('now','localtime'), settle_note=? "
                "WHERE mode=? AND predict_date=?",
                (reason, reason, mode, predict_date),
            )
        conn.commit()
        return cur.rowcount


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=DB_TIMEOUT)
    conn.row_factory = sqlite3.Row
    return conn


def _git_state() -> tuple[str, int]:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        return head or "", 1 if dirty else 0
    except Exception:
        return "", 0


_TRADING_DAYS_CACHE: dict[str, list[str]] = {}


def trading_days(market_db: str = MARKET_DB_PATH,
                 start: str | None = None, end: str | None = None) -> list[str]:
    days = _TRADING_DAYS_CACHE.get(market_db)
    if days is None:
        with _connect(market_db) as conn:
            rows = conn.execute(
                "SELECT date FROM trade_calendar WHERE is_trading=1 ORDER BY date"
            ).fetchall()
            if not rows:
                rows = conn.execute(
                    "SELECT DISTINCT date FROM daily_bars ORDER BY date"
                ).fetchall()
        days = [r["date"] for r in rows]
        _TRADING_DAYS_CACHE[market_db] = days
    if start:
        return [d for d in days if d >= start]
    if end:
        return [d for d in days if d <= end]
    return days


def resolve_data_date(market_db: str = MARKET_DB_PATH,
                      run_date: str | None = None) -> str | None:
    """最近一个“有沪深300行情”的交易日（≤ run_date）。"""
    run_date = run_date or datetime.now().strftime("%Y-%m-%d")
    with _connect(market_db) as conn:
        row = conn.execute(
            "SELECT MAX(date) FROM daily_bars WHERE symbol=? AND date<=?",
            (MARKET_SYMBOL, run_date),
        ).fetchone()
    return row[0] if row and row[0] else None


def next_trading_day(market_db: str, day: str, n: int = 1) -> str | None:
    days = trading_days(market_db)
    i = bisect.bisect_right(days, day)
    if i + n - 1 >= len(days):
        return None
    return days[i + n - 1]


def prev_trading_day(market_db: str, day: str) -> str | None:
    days = trading_days(market_db)
    i = bisect.bisect_left(days, day)
    return days[i - 1] if i > 0 else None


def load_bars(market_db: str, symbol: str, end_date: str,
              limit: int | None = None) -> list[KlineBar]:
    sql = (
        "SELECT date, open, close, high, low, volume, amount FROM daily_bars "
        "WHERE symbol=? AND date<=? ORDER BY date"
    )
    with _connect(market_db) as conn:
        if limit:
            rows = conn.execute(
                "SELECT date, open, close, high, low, volume, amount FROM daily_bars "
                "WHERE symbol=? AND date<=? ORDER BY date DESC LIMIT ?",
                (symbol, end_date, limit),
            ).fetchall()
            rows = list(reversed(rows))
        else:
            rows = conn.execute(sql, (symbol, end_date)).fetchall()
    return [
        KlineBar(
            date=r["date"], open=r["open"], close=r["close"],
            high=r["high"], low=r["low"], volume=r["volume"], amount=r["amount"],
        )
        for r in rows
    ]


def _close_on(market_db: str, symbol: str, day: str) -> float | None:
    with _connect(market_db) as conn:
        row = conn.execute(
            "SELECT close FROM daily_bars WHERE symbol=? AND date=?",
            (symbol, day),
        ).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def _return_pct(start: float | None, end: float | None) -> float | None:
    if start is None or end is None or start <= 0:
        return None
    return round((end / start - 1.0) * 100.0, 2)


def _rsi14(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains.append(diff)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(-diff)
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    vals = sorted(values)
    n = len(vals)
    mid = n // 2
    if n % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


# ============================================================
# 市场宽度（涨跌家数）
# ============================================================
def breadth_day(market_db: str, day: str,
                prev_day: str | None = None) -> dict | None:
    """本地存量标的的涨跌家数（非全市场，报告中明确标注口径）。"""
    prev_day = prev_day or prev_trading_day(market_db, day)
    if not prev_day:
        return None
    with _connect(market_db) as conn:
        cur_rows = conn.execute(
            "SELECT symbol, close FROM daily_bars WHERE date=?", (day,)
        ).fetchall()
        prev_rows = conn.execute(
            "SELECT symbol, close FROM daily_bars WHERE date=?", (prev_day,)
        ).fetchall()
    cur = {
        r["symbol"]: float(r["close"]) for r in cur_rows
        if r["symbol"] not in INDEX_SYMBOLS and r["close"] is not None
    }
    prev = {
        r["symbol"]: float(r["close"]) for r in prev_rows
        if r["symbol"] not in INDEX_SYMBOLS and r["close"] is not None
    }
    common = [s for s in cur if s in prev and prev[s] > 0]
    if not common:
        return None
    up = sum(1 for s in common if cur[s] > prev[s])
    down = sum(1 for s in common if cur[s] < prev[s])
    flat = len(common) - up - down
    valid = len(common)
    return {
        "date": day, "prev_date": prev_day,
        "up": up, "down": down, "flat": flat,
        "valid": valid, "ratio": up / valid if valid else None,
    }


def breadth_window(market_db: str, start_exclusive: str,
                   end_inclusive: str) -> dict | None:
    days = [d for d in trading_days(market_db)
            if start_exclusive < d <= end_inclusive]
    if not days:
        return None
    total_up = total_valid = 0
    details = []
    for day in days:
        b = breadth_day(market_db, day)
        if not b:
            continue
        total_up += b["up"]
        total_valid += b["valid"]
        details.append(b)
    if total_valid == 0:
        return None
    return {
        "days": details,
        "ratio": total_up / total_valid,
        "up": total_up,
        "valid": total_valid,
    }


# ============================================================
# 板块快照
# ============================================================
def sector_snapshot_quality(market_db: str, day: str) -> dict:
    with _connect(market_db) as conn:
        row = conn.execute(
            "SELECT COUNT(*) n, "
            "SUM(CASE WHEN level>0 THEN 1 ELSE 0 END) lv, "
            "SUM(CASE WHEN source='eastmoney' THEN 1 ELSE 0 END) em "
            "FROM sector_daily WHERE date=? AND kind='plate'",
            (day,),
        ).fetchone()
    n = int(row["n"] or 0)
    lv = int(row["lv"] or 0)
    em = int(row["em"] or 0)
    return {
        "date": day, "n": n, "level": lv, "eastmoney": em,
        "complete": n >= MIN_PLATE_BOARDS and em >= MIN_PLATE_BOARDS,
        "has_level": lv >= MIN_PLATE_BOARDS,
    }


def _load_plates(market_db: str, day: str) -> tuple[list, dict]:
    q = sector_snapshot_quality(market_db, day)
    if not q["complete"]:
        return [], q
    analyzer = SectorAnalyzer(market_db)
    return analyzer.load_date(day, kind="plate", exclude_suspicious=True), q


# ============================================================
# 大盘方向
# ============================================================
def classify_market(score: int) -> tuple[str, str]:
    """6 个多空信号 → (方向, 强弱)。确定性规则，便于事后对账。"""
    if score >= 5:
        return "上涨", ("强" if score >= 6 else "中")
    if score == 4:
        return "上涨", "弱"
    if score == 3:
        return "震荡", "偏强"
    if score == 2:
        return "震荡", "偏弱"
    if score == 1:
        return "下跌", "弱"
    return "下跌", "强"


def _market_statement(horizon: int, target_date: str, direction: str,
                      strength: str, predict_date: str) -> str:
    label = "次一交易日" if horizon == 1 else "未来5个交易日"
    span = f"{predict_date}→{target_date}"
    if direction == "上涨":
        cond = f"区间收盘涨跌幅 ≥ +{UP_DOWN_THRESHOLD:.1f}%，且区间涨跌家数占比 ≥ 50%"
    elif direction == "下跌":
        cond = f"区间收盘涨跌幅 ≤ -{UP_DOWN_THRESHOLD:.1f}%，且区间涨跌家数占比 ≤ 50%"
    else:
        cond = f"区间收盘涨跌幅在 -{UP_DOWN_THRESHOLD:.1f}% ~ +{UP_DOWN_THRESHOLD:.1f}% 之间"
    return f"沪深300 {label}（{span}）{direction}（{strength}）；判定标准：{cond}"


def build_market_predictions(market_db: str, as_of: str,
                             run_date: str | None = None,
                             mode: str = "live") -> list[dict]:
    """只用 as_of 及之前的沪深300日K生成大盘预判。"""
    bars = load_bars(market_db, MARKET_SYMBOL, as_of, limit=60)
    if len(bars) < 20:
        return []
    closes = [float(b.close) for b in bars]
    ma5 = sum(closes[-5:]) / 5.0
    ma20 = sum(closes[-20:]) / 20.0
    ret1 = _return_pct(closes[-2], closes[-1])
    ret5 = _return_pct(closes[-6], closes[-1]) if len(closes) >= 6 else None
    ret20 = _return_pct(closes[-21], closes[-1]) if len(closes) >= 21 else None
    rsi = _rsi14(closes)
    b = breadth_day(market_db, as_of)
    breadth_ratio = b["ratio"] if b else None

    out: list[dict] = []
    for horizon in HORIZONS:
        target = next_trading_day(market_db, as_of, horizon)
        if not target:
            continue
        if horizon == 1:
            signals = [
                ("收盘价>MA5", closes[-1] > ma5, f"收盘 {closes[-1]:.2f} / MA5 {ma5:.2f}"),
                ("当日涨跌", (ret1 or 0) > 0, f"当日 {ret1:+.2f}%" if ret1 is not None else "当日 缺失"),
                ("5日动能", (ret5 or 0) > 0, f"5日 {ret5:+.2f}%" if ret5 is not None else "5日 缺失"),
                ("RSI14健康", 45 <= (rsi or 0) <= 75, f"RSI14 {rsi:.1f}" if rsi is not None else "RSI14 缺失"),
                ("涨跌家数", (breadth_ratio or 0) >= 0.5,
                 f"{b['up']}/{b['valid']}（{breadth_ratio:.0%}）" if b else "涨跌家数 缺失"),
                ("中期趋势", closes[-1] > ma20, f"收盘 {closes[-1]:.2f} / MA20 {ma20:.2f}"),
            ]
        else:
            signals = [
                ("收盘价>MA20", closes[-1] > ma20, f"收盘 {closes[-1]:.2f} / MA20 {ma20:.2f}"),
                ("MA5>MA20", ma5 > ma20, f"MA5 {ma5:.2f} / MA20 {ma20:.2f}"),
                ("5日动能", (ret5 or 0) > 0, f"5日 {ret5:+.2f}%" if ret5 is not None else "5日 缺失"),
                ("20日动能", (ret20 or 0) > 0, f"20日 {ret20:+.2f}%" if ret20 is not None else "20日 缺失"),
                ("RSI14健康", 45 <= (rsi or 0) <= 75, f"RSI14 {rsi:.1f}" if rsi is not None else "RSI14 缺失"),
                ("涨跌家数", (breadth_ratio or 0) >= 0.5,
                 f"{b['up']}/{b['valid']}（{breadth_ratio:.0%}）" if b else "涨跌家数 缺失"),
            ]
        score = sum(1 for _, ok, _ in signals if ok)
        direction, strength = classify_market(score)
        rationale = "；".join(f"{name}{'✓' if ok else '✗'}（{detail}）" for name, ok, detail in signals)
        rationale += f"；信号得分 {score}/6"
        out.append({
            "mode": mode,
            "predict_date": as_of,
            "run_date": run_date or as_of,
            "scope": "market",
            "target": MARKET_SYMBOL,
            "target_name": "沪深300",
            "horizon": horizon,
            "direction": direction,
            "strength": strength,
            "statement": _market_statement(horizon, target, direction, strength, as_of),
            "rationale": rationale,
            "score": None,
            "target_date": target,
            "data_date": as_of,
        })
    return out


# ============================================================
# 板块强弱
# ============================================================
def build_sector_predictions(market_db: str, as_of: str,
                             run_date: str | None = None,
                             mode: str = "live") -> tuple[list[dict], list[str]]:
    plates, quality = _load_plates(market_db, as_of)
    if not quality["complete"]:
        return [], [f"板块快照不完整（行业板块 {quality['n']} 条，需 ≥{MIN_PLATE_BOARDS}）"]
    if not quality["has_level"]:
        return [], [f"板块点位缺失（level>0 仅 {quality['level']} 条，无法做 5 日相对强弱对账）"]
    valid = [p for p in plates
             if p.zdf20 is not None and p.zdf20 != 0 and p.level > 0]
    if len(valid) < SECTOR_MIN_VALID:
        return [], [f"有效板块仅 {len(valid)} 个，低于 {SECTOR_MIN_VALID} 个下限"]

    target = next_trading_day(market_db, as_of, 5)
    if not target:
        return [], ["交易日历不足，无法确定 5 日后的对账日"]

    valid.sort(key=lambda p: (p.zdf20, p.zdf5), reverse=True)
    ranks = {p.code: i + 1 for i, p in enumerate(valid)}
    strong = valid[:SECTOR_TOP_N]
    weak = list(reversed(valid[-SECTOR_TOP_N:]))

    out: list[dict] = []
    for p, direction in [(x, "看强") for x in strong] + [(x, "看弱") for x in weak]:
        # sector_daily.zljlr_d20 单位为万元（见 schema）；展示统一折算为亿元
        fund = (f"{p.zljlr_d20 / 10000.0:+.2f} 亿元"
                if p.zljlr_d20 is not None else "资金数据缺失")
        rationale = (
            f"20日涨幅 {p.zdf20:+.2f}%（第 {ranks[p.code]}/{len(valid)}）；"
            f"5日 {p.zdf5:+.2f}%；20日主力净流入 {fund}"
        )
        if direction == "看强":
            cond = "区间涨幅 > 全市场行业板块中位数"
        else:
            cond = "区间涨幅 < 全市场行业板块中位数"
        statement = (
            f"板块 {p.name} 未来5个交易日（{as_of}→{target}）相对全市场{direction}；"
            f"判定标准：{cond}"
        )
        out.append({
            "mode": mode,
            "predict_date": as_of,
            "run_date": run_date or as_of,
            "scope": "sector",
            "target": p.code,
            "target_name": p.name,
            "horizon": 5,
            "direction": direction,
            "strength": f"20日涨幅第{ranks[p.code]}名",
            "statement": statement,
            "rationale": rationale,
            "score": None,
            "target_date": target,
            "data_date": as_of,
        })
    return out, []


# ============================================================
# 个股
# ============================================================
def _stock_meta_map(market_db: str) -> dict[str, dict[str, str]]:
    with _connect(market_db) as conn:
        rows = conn.execute(
            "SELECT symbol, name, sector FROM watchlist WHERE status='active'"
        ).fetchall()
    return {
        r["symbol"]: {"name": r["name"], "sector": r["sector"] or ""}
        for r in rows
    }


def _stock_name_map(market_db: str) -> dict[str, str]:
    return {k: v["name"] for k, v in _stock_meta_map(market_db).items()}


def _live_stock_rows(market_db: str, as_of: str) -> list[sqlite3.Row]:
    """决策侧当日五因子结果 —— 个股预判的唯一数据源。

    factor_snapshot 由收盘闭环 r47_closed_loop.py 用同一份 score_candidate 入参
    （同一交易日、同一板块快照、同一实时 PE/PB、同一业绩证据）落库，
    即“决策侧同一日期、同一标的、同一份输入”的结果。预判只读此表，
    不自行拼快照、不重算，避免同一只票出现两套分数。
    """
    with _connect(market_db) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM factor_snapshot WHERE date=? ORDER BY score DESC", (as_of,)
        ).fetchall()]


# 决策侧快照的五个因子列 → 缺失项名称（与 score_candidate 的 missing 口径一致）
_STOCK_FACTOR_COLUMNS = (
    ("industry", "行业景气"),
    ("growth", "业绩趋势"),
    ("valuation", "估值"),
    ("technical", "技术面"),
    ("capital", "资金面"),
)


def _decision_score(row: dict) -> tuple[float | None, list[str]]:
    """从决策侧结果取加权分：任一因子缺失即不出分（不用 0 分占位）。

    决策侧 score 列是“缺失按 0 折损后”的加权分（见 src/factors.py:score_candidate），
    直接拿来当预判分等于把缺失当 0 分——0 分比“PE 为负”的 20 分还低，
    与资金/技术因子“缺数据返回 None”的规矩不一致。
    因此先按因子列判缺失：缺任一因子 → (None, 缺失项)，不出分。
    """
    missing = [label for key, label in _STOCK_FACTOR_COLUMNS if row.get(key) is None]
    if missing or row.get("score") is None:
        return None, missing or ["加权分"]
    return float(row["score"]), []


def _decision_factors(res) -> dict:
    """score_candidate 结果 → 决策侧快照口径的因子字典（回溯路径用，与落库列一一对应）。"""
    return {
        "industry": None if "行业景气" in res.missing else res.factors.industry,
        "growth": res.factors.growth,
        "valuation": None if "估值" in res.missing else res.factors.valuation,
        "technical": None if "技术面" in res.missing else res.factors.technical,
        "capital": None if "资金面" in res.missing else res.factors.capital,
        "score": res.weighted,
    }


def _stock_reasons(row: dict, eligibility=None, missing: list[str] | None = None) -> str:
    if eligibility is not None and not eligibility.ok:
        return eligibility.reason
    missing = missing or []

    def cell(key: str, label: str, minimum: float) -> str:
        if label in missing:
            return f"{label} —（缺失）"
        return f"{label} {row[key]:.1f}（≥{minimum:.0f}）"

    score = row.get("score")
    score_text = (
        f"五因子加权分 —（缺{'、'.join(missing) or '加权分'}，按规矩不出分）"
        if score is None else f"五因子加权分 {score:.1f}"
    )
    return (
        f"{cell('technical', '技术面', STOCK_MIN_TECHNICAL)}、"
        f"{cell('industry', '行业景气', STOCK_MIN_INDUSTRY)}、"
        f"{cell('capital', '资金面', STOCK_MIN_CAPITAL)}、{score_text}"
    )


def _stock_statement(symbol: str, name: str, as_of: str, target: str) -> str:
    return (
        f"个股 {name}（{symbol}）未来5个交易日（{as_of}→{target}）上涨；"
        f"判定标准：{target} 收盘价相对 {as_of} 收盘价涨幅 > 0%"
    )


def build_stock_predictions_live(market_db: str, as_of: str,
                                 run_date: str | None = None,
                                 mode: str = "live") -> tuple[list[dict], list[str]]:
    """个股预判：来源限定为当日 factor_snapshot 已打分的标的，且必须可买。"""
    rows = _live_stock_rows(market_db, as_of)
    if not rows:
        return [], ["当日无五因子评分快照，无法生成个股预判"]
    names = _stock_name_map(market_db)
    target = next_trading_day(market_db, as_of, 5)
    if not target:
        return [], ["交易日历不足，无法确定 5 日后的对账日"]

    notes: list[str] = []
    candidates: list[dict] = []
    for r in rows:
        sym = r["symbol"]
        name = names.get(sym, sym)
        bars = load_bars(market_db, sym, as_of)
        if not bars or bars[-1].date != as_of:
            notes.append(f"{sym} 当日无K线（停牌/缺数据），排除")
            continue
        price = float(bars[-1].close)
        elig = check_eligibility(
            sym, sym[2:], name, price, bars=bars, as_of=as_of,
        )
        if not elig.ok:
            notes.append(f"{sym} {elig.reason}")
            continue
        if r["technical"] is None or r["industry"] is None or r["capital"] is None:
            notes.append(f"{sym} 技术/行业/资金因子缺失，排除")
            continue
        if (r["technical"] < STOCK_MIN_TECHNICAL
                or r["industry"] < STOCK_MIN_INDUSTRY
                or r["capital"] < STOCK_MIN_CAPITAL):
            notes.append(
                f"{sym} 未过硬筛（技术{r['technical']:.1f}/行业{r['industry']:.1f}/"
                f"资金{r['capital']:.1f}）"
            )
            continue
        score, missing = _decision_score(r)
        candidates.append({
            "symbol": sym, "name": name, "price": price,
            "score": score, "missing": missing,
            # 仅作同日排序键：决策侧原始加权分。缺失因子若是当天系统性的（如估值全 NULL），
            # 各候选被折损的量相同，排序结果不受影响；该值不展示、不当作分数。
            "order": float(r["score"]) if r["score"] is not None else 0.0,
            "technical": float(r["technical"]),
            "industry": float(r["industry"]),
            "capital": float(r["capital"]),
            "resolved_board": r["resolved_board"] or "",
        })

    candidates.sort(key=lambda x: (-x["order"], -x["technical"], x["symbol"]))
    picked = candidates[:STOCK_MAX_PICKS]
    if not picked:
        return [], notes + ["无合格个股（当日已评分且可买标的中无同时满足技术/行业/资金硬筛的标的）"]

    out: list[dict] = []
    for c in picked:
        rationale = _stock_reasons(c, missing=c["missing"])
        rationale += f"；可买：主板、收盘价 {c['price']:.2f} 元 ≤60"
        strength = (f"预判分 {c['score']:.1f}" if c["score"] is not None
                    else f"不出分（缺{'、'.join(c['missing'])}）")
        out.append({
            "mode": mode,
            "predict_date": as_of,
            "run_date": run_date or as_of,
            "scope": "stock",
            "target": c["symbol"],
            "target_name": c["name"],
            "horizon": 5,
            "direction": "上涨",
            "strength": strength,
            "statement": _stock_statement(c["symbol"], c["name"], as_of, target),
            "rationale": rationale,
            "score": c["score"],
            "target_date": target,
            "data_date": as_of,
        })
    notes.append(
        f"候选池 {len(rows)} 只 → 可买且过硬筛 {len(candidates)} 只 → 入选 {len(picked)} 只"
    )
    return out, notes


def _research_asof(kb_db: str, as_of: str) -> list[SimpleNamespace]:
    """财报/调研证据按“入库时间 ≤ 当日”截断（比按 fiscal period end 更保守）。"""
    with _connect(kb_db) as conn:
        rows = conn.execute(
            "SELECT symbol, title, content, source_url, created_at FROM research_notes "
            "WHERE category='基本面' AND date(created_at) <= ? ORDER BY created_at",
            (as_of,),
        ).fetchall()
    return [
        SimpleNamespace(
            symbol=r["symbol"], title=r["title"], content=r["content"],
            source_url=r["source_url"], created_at=r["created_at"],
        )
        for r in rows
    ]


def build_stock_predictions_backtest(market_db: str, kb_db: str, as_of: str,
                                     universe: Iterable[str] | None = None
                                     ) -> tuple[list[dict], list[str]]:
    """历史回溯用个股预判：只用 as_of 及之前的数据重算因子，绝不读取未来数据。

    与实盘路径的差异（报告中必须披露）：
    - 历史 PE/PB 未落库 → 估值因子判缺失、不出分（不按 0 分占位）；
      其余因子用 score_candidate 重算，与决策侧同一函数、同一输入截断口径
      （即“同源同口径的独立快照”，预判不另立公式）；
    - 历史观察池成员状态无法还原，回测域取“当前 active 观察池”，存在幸存者偏差。
    """
    plates_plate, quality = _load_plates(market_db, as_of)
    if not quality["complete"]:
        return [], [f"{as_of} 板块快照不完整（行业板块 {quality['n']} 条）"]
    analyzer = SectorAnalyzer(market_db)
    plates = plates_plate + analyzer.load_date(as_of, kind="concept", exclude_suspicious=True)

    meta = _stock_meta_map(market_db)
    universe = list(universe) if universe is not None else list(meta)
    notes = _research_asof(kb_db, as_of)
    target = next_trading_day(market_db, as_of, 5)
    if not target:
        return [], [f"{as_of} 未来5个交易日超出交易日历"]

    reasons: list[str] = []
    candidates: list[dict] = []
    for sym in universe:
        name = meta.get(sym, {}).get("name", sym)
        sector = meta.get(sym, {}).get("sector", "")
        bars = load_bars(market_db, sym, as_of)
        if len(bars) < 10 or bars[-1].date != as_of:
            reasons.append(f"{sym} 历史K线不足/当日缺K线")
            continue
        sym_notes = [n for n in notes if n.symbol == sym]
        growth, evidence = growth_from_research(sym_notes)
        if growth is None or not evidence:
            reasons.append(f"{sym} 当日无业绩证据（不入评分）")
            continue
        res = score_candidate(
            sym, name, sector,
            growth=growth, growth_evidence=evidence,
            bars=bars, sector_ctx=None, plates=plates, valuation=None,
            industry_mode="percentile", snapshot_source="eastmoney",
            snapshot_complete=quality["complete"],
            expected_date=as_of, snapshot_date=as_of,
        )
        if any(k in res.missing for k in ("行业", "技术", "资金")):
            reasons.append(f"{sym} 历史因子缺失：{'、'.join(res.missing)}")
            continue
        price = float(bars[-1].close)
        elig = check_eligibility(sym, sym[2:], name, price, bars=bars, as_of=as_of)
        if not elig.ok:
            reasons.append(f"{sym} {elig.reason}")
            continue
        row = {
            "symbol": sym, "name": name, "price": price,
            "technical": float(res.factors.technical),
            "industry": float(res.factors.industry),
            "capital": float(res.factors.capital),
        }
        # 与实盘同一条规矩：缺任一因子就不出分，不把缺失折成 0 分
        row["score"], row["missing"] = _decision_score(_decision_factors(res))
        row["order"] = float(res.weighted)   # 仅作同日排序键，不展示为分数
        if (row["technical"] < STOCK_MIN_TECHNICAL
                or row["industry"] < STOCK_MIN_INDUSTRY
                or row["capital"] < STOCK_MIN_CAPITAL):
            continue
        candidates.append(row)

    candidates.sort(key=lambda x: (-x["order"], -x["technical"], x["symbol"]))
    picked = candidates[:STOCK_MAX_PICKS]
    if not picked:
        return [], reasons + [f"{as_of} 无合格个股"]

    out: list[dict] = []
    for c in picked:
        rationale = _stock_reasons(c, missing=c["missing"]) + "；可买：主板、价格≤60"
        strength = (f"预判分 {c['score']:.1f}" if c["score"] is not None
                    else f"不出分（缺{'、'.join(c['missing'])}）")
        out.append({
            "mode": "backtest",
            "predict_date": as_of,
            "run_date": as_of,
            "scope": "stock",
            "target": c["symbol"],
            "target_name": c["name"],
            "horizon": 5,
            "direction": "上涨",
            "strength": strength,
            "statement": _stock_statement(c["symbol"], c["name"], as_of, target),
            "rationale": rationale,
            "score": c["score"],
            "target_date": target,
            "data_date": as_of,
        })
    return out, reasons


# ============================================================
# 落盘（append-only）
# ============================================================
def save_predictions(rows: list[dict], db_path: str = DB_PATH) -> int:
    """只 INSERT，不 UPDATE；重复键直接跳过（保证 append-only）。"""
    if not rows:
        return 0
    init_forecast_tables(db_path)
    head, dirty = _git_state()
    inserted = 0
    with sqlite3.connect(db_path, timeout=DB_TIMEOUT) as conn:
        for r in rows:
            cur = conn.execute(
                "INSERT OR IGNORE INTO forecast_ledger "
                "(mode, predict_date, run_date, scope, target, target_name, horizon, "
                " direction, strength, statement, rationale, score, target_date, data_date, "
                " git_head, git_dirty) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    r.get("mode", "live"), r["predict_date"], r["run_date"],
                    r["scope"], r["target"], r["target_name"], r["horizon"],
                    r["direction"], r["strength"], r["statement"], r["rationale"],
                    r.get("score"), r["target_date"], r["data_date"], head, dirty,
                ),
            )
            inserted += cur.rowcount
        conn.commit()
    return inserted


def _exists_predictions(db_path: str, run_date: str, predict_date: str | None = None,
                        mode: str = "live") -> bool:
    """判重键必须含 predict_date：同一生成日可以有多个数据截止日的批次（手工盘中批次
    与收盘批次），只按 run_date 判重会把后跑的收盘批次误判为重复而跳过。"""
    init_forecast_tables(db_path)
    sql = "SELECT COUNT(*) FROM forecast_ledger WHERE mode=? AND run_date=?"
    args: list = [mode, run_date]
    if predict_date:
        sql += " AND predict_date=?"
        args.append(predict_date)
    with _connect(db_path) as conn:
        row = conn.execute(sql, args).fetchone()
    return bool(row and row[0])


def _non_close_reason(run_date: str, data_date: str) -> str:
    """非收盘批次的作废原因（运行日与数据截止日不一致）。"""
    return (f"非收盘批次：运行日 {run_date} 与数据截止日 {data_date} 不一致，"
            f"整份作废，不参与对账与命中率统计")


def void_non_close_batches(db_path: str = DB_PATH, mode: str = "live") -> int:
    """把「运行日 ≠ 数据截止日」的批次统一标 VOID，返回本次作废的行数。

    这类批次是用更早的数据截止日生成、却记在更晚的生成日下，属盘中手工批次；
    按新口径一律作废（不补生成、不改判断内容，只改状态与原因）。
    """
    init_forecast_tables(db_path)
    with _connect(db_path) as conn:
        groups = conn.execute(
            "SELECT DISTINCT predict_date, run_date, data_date FROM forecast_ledger "
            "WHERE mode=? AND data_date<>run_date AND status<>'VOID'",
            (mode,),
        ).fetchall()
    marked = 0
    for g in groups:
        marked += mark_void(g["predict_date"], _non_close_reason(g["run_date"], g["data_date"]),
                            run_date=g["run_date"], db_path=db_path, mode=mode)
    return marked


# ============================================================
# 报告渲染
# ============================================================
def _md_escape(text: str) -> str:
    return (text or "").replace("|", "｜").replace("\n", " ")


def render_forecast_markdown(rows: list[dict], data_date: str,
                             run_date: str, notes_map: dict[str, list[str]] | None = None,
                             generated_at: str | None = None,
                             void_reason: str | None = None) -> str:
    head, dirty = _git_state()
    generated_at = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"# 每日预判 · 生成 {run_date}（数据截止 {data_date}）",
        "",
        f"- 数据截止交易日：**{data_date}**",
        f"- 生成时间：**{generated_at}**（落盘日期 {run_date}）",
        f"- git HEAD：`{head or 'unknown'}`（工作区{'有未提交改动' if dirty else '干净'}）",
        "- 时点说明：预判基于数据截止日收盘数据生成；生成时间晚于数据截止日时，不得称为盘前预判。",
        "- 口径：预判独立于五因子评分与选股，不参与打分、不改变任何模型参数。",
        "- 约束：本文件 append-only，落盘后不因后续行情或结算结果修改；错就是错。",
        "",
    ]
    if void_reason:
        lines.insert(2, f"> ⚠️ **本批次已作废**：{void_reason}。判断内容原样保留，仅作留痕。")
        lines.insert(3, "")

    notes_map = notes_map or {}

    market = [r for r in rows if r["scope"] == "market"]
    lines += ["## 一、大盘方向", ""]
    if market:
        lines += [
            "| 周期 | 方向 | 强弱 | 到期日 | 可验证陈述 |",
            "|---|---|---|---|---|",
        ]
        for r in market:
            label = "次日" if r["horizon"] == 1 else "5日"
            lines.append(
                f"| {label} | {r['direction']} | {r['strength']} | {r['target_date']} | "
                f"{_md_escape(r['statement'])} |"
            )
        lines += ["", "**依据**："]
        for r in market:
            label = "次日" if r["horizon"] == 1 else "5日"
            lines.append(f"- {label}：{_md_escape(r['rationale'])}")
    else:
        lines.append("无（沪深300 历史K线不足 20 根，无法生成大盘方向）")

    lines += ["", "## 二、板块（未来 5 个交易日）", ""]
    sectors = [r for r in rows if r["scope"] == "sector"]
    if sectors:
        lines += [
            "| 方向 | 板块 | 依据 | 判定标准 |",
            "|---|---|---|---|",
        ]
        for r in sectors:
            lines.append(
                f"| {r['direction']} | {r['target_name']}（{r['target']}） | "
                f"{_md_escape(r['rationale'])} | {_md_escape(r['statement'])} |"
            )
    else:
        reason = "；".join(notes_map.get("sector", [])) or "当日无完整板块快照"
        lines.append(f"无（{reason}）")

    lines += ["", "## 三、个股（未来 5 个交易日）", ""]
    stocks = [r for r in rows if r["scope"] == "stock"]
    if stocks:
        lines += [
            "> **“评分”列的口径**：直接复用决策侧 `factor_snapshot`（同一交易日、同一标的、"
            "同一份入参的落库结果），不是预判自建的第二套评分，也不是置信度或上涨概率；"
            "五个因子任一为 NULL 即判缺失、**不出分**（显示“—”，依据列写明缺哪项），"
            "不用 0 分占位。该数字只用于同日候选排序，不作为入选门槛，也不作为开仓依据。",
            "",
            "| 标的 | 五因子加权分 | 方向 | 到期日 | 依据 | 判定标准 |",
            "|---|---|---|---|---|---|",
        ]
        for r in stocks:
            score = f"{r['score']:.1f}" if r["score"] is not None else "—"
            lines.append(
                f"| {r['target_name']}（{r['target']}） | {score} | {r['direction']} | "
                f"{r['target_date']} | {_md_escape(r['rationale'])} | "
                f"{_md_escape(r['statement'])} |"
            )
    else:
        reason = "；".join(notes_map.get("stock", [])) or "当日无合格标的"
        lines.append(f"无（{reason}）")

    lines += [
        "",
        "## 四、口径与免责",
        "",
        "- 大盘验证：区间涨跌幅 + 本地存量标的涨跌家数（`market.db`，非全市场）；方向判断要求两者同向。",
        "- 板块验证：板块点位区间涨幅与全市场行业板块中位数比较；快照缺失/点位缺失记为无法判定。",
        "- 个股验证：5 个交易日收盘价涨幅 > 0 记为命中；停牌或目标日缺 K 线记为无法判定。",
        "- 个股评分来源：复用决策侧 `factor_snapshot`（收盘闭环落库的同一份五因子结果），"
        "因子缺失判缺失、不出分；预判不另建快照、不另立公式。",
        "- **边界（重要）**：本预判是方向记录与观察名单，**不构成开仓建议**；"
        "开仓/弃权一律以决策日志为准（五因子 ≥85 且无因子缺失）。"
        "两者同日方向不一致时，以决策日志为准，预判只作事后检验样本，不得并入信号。",
        "- 无法判定不进入命中率分母；累计命中率见 `outputs/预判/预判命中率.md`。",
        "",
    ]
    return "\n".join(lines)


def write_forecast_report(rows: list[dict], data_date: str, run_date: str,
                          notes_map: dict[str, list[str]] | None = None,
                          generated_at: str | None = None,
                          void_reason: str | None = None) -> Path:
    """文件名按生成日（run_date）命名：落盘日期与生成时间一致。

    数据截止日写在文内，不再用数据截止日命名，避免出现“文件名看起来是事前、
    实际是事后生成”的样本。非收盘批次（void_reason 非空）在文首标作废。
    """
    FORECAST_DIR.mkdir(parents=True, exist_ok=True)
    path = FORECAST_DIR / f"预判_{run_date}.md"
    if path.exists():
        return path  # append-only：不覆盖已落盘文件
    path.write_text(
        render_forecast_markdown(rows, data_date, run_date, notes_map, generated_at, void_reason),
        encoding="utf-8",
    )
    return path


# ============================================================
# 到期对账
# ============================================================
def _eval_market(market_db: str, pred: dict) -> dict:
    start = _close_on(market_db, MARKET_SYMBOL, pred["predict_date"])
    end = _close_on(market_db, MARKET_SYMBOL, pred["target_date"])
    ret = _return_pct(start, end)
    if ret is None:
        return {"status": "UNVERIFIABLE", "note": "沪深300 起止日行情缺失"}
    if ret >= UP_DOWN_THRESHOLD:
        actual_class = "上涨"
    elif ret <= -UP_DOWN_THRESHOLD:
        actual_class = "下跌"
    else:
        actual_class = "震荡"
    if pred["horizon"] == 1:
        b = breadth_day(market_db, pred["target_date"])
    else:
        b = breadth_window(market_db, pred["predict_date"], pred["target_date"])
    breadth = b["ratio"] if b else None

    direction = pred["direction"]
    if direction == "上涨":
        hit = ret >= UP_DOWN_THRESHOLD and breadth is not None and breadth >= 0.5
    elif direction == "下跌":
        hit = ret <= -UP_DOWN_THRESHOLD and breadth is not None and breadth <= 0.5
    else:
        hit = -UP_DOWN_THRESHOLD <= ret <= UP_DOWN_THRESHOLD
    if direction != "震荡" and breadth is None:
        return {
            "status": "UNVERIFIABLE",
            "actual_return": ret,
            "actual_class": actual_class,
            "note": "涨跌家数缺失，方向无法确认",
        }
    note = (
        f"区间涨跌幅 {ret:+.2f}%；涨跌家数占比 "
        f"{('%.0f%%' % (breadth * 100)) if breadth is not None else '缺失'}"
    )
    return {
        "status": "HIT" if hit else "MISS",
        "actual_return": ret,
        "actual_class": actual_class,
        "breadth": round(breadth, 4) if breadth is not None else None,
        "note": note,
    }


def _eval_sector(market_db: str, pred: dict) -> dict:
    code = pred["target"]
    start, end = pred["predict_date"], pred["target_date"]
    with _connect(market_db) as conn:
        rows = conn.execute(
            "SELECT date, level FROM sector_daily "
            "WHERE code=? AND kind='plate' AND date IN (?,?)",
            (code, start, end),
        ).fetchall()
    levels = {r["date"]: float(r["level"]) for r in rows if r["level"] and r["level"] > 0}
    if start not in levels or end not in levels:
        return {"status": "UNVERIFIABLE", "note": "板块起止日点位缺失"}
    ret = _return_pct(levels[start], levels[end])

    with _connect(market_db) as conn:
        all_rows = conn.execute(
            "SELECT code, date, level FROM sector_daily "
            "WHERE kind='plate' AND date IN (?,?)",
            (start, end),
        ).fetchall()
    levels_by_code: dict[str, dict[str, float]] = {}
    for r in all_rows:
        if r["level"] and r["level"] > 0:
            levels_by_code.setdefault(r["code"], {})[r["date"]] = float(r["level"])
    comparable = []
    for c, d in levels_by_code.items():
        if start in d and end in d:
            comparable.append((d[end] / d[start] - 1.0) * 100.0)
    if len(comparable) < MIN_COMPARABLE_SECTORS:
        return {
            "status": "UNVERIFIABLE",
            "actual_return": ret,
            "note": f"可比板块仅 {len(comparable)} 个（需 ≥{MIN_COMPARABLE_SECTORS}）",
        }
    bench = _median(comparable)
    if ret is None or bench is None:
        return {"status": "UNVERIFIABLE", "note": "板块收益/基准不可计算"}
    if pred["direction"] == "看强":
        hit = ret > bench
    else:
        hit = ret < bench
    return {
        "status": "HIT" if hit else "MISS",
        "actual_return": ret,
        "benchmark_return": round(bench, 2),
        "note": f"板块 {ret:+.2f}% vs 可比板块中位数 {bench:+.2f}%（n={len(comparable)}）",
    }


def _eval_stock(market_db: str, pred: dict) -> dict:
    start = _close_on(market_db, pred["target"], pred["predict_date"])
    end = _close_on(market_db, pred["target"], pred["target_date"])
    ret = _return_pct(start, end)
    if ret is None:
        return {"status": "UNVERIFIABLE", "note": "标的起止日行情缺失（停牌/缺数据）"}
    return {
        "status": "HIT" if ret > 0 else "MISS",
        "actual_return": ret,
        "note": f"5日涨跌幅 {ret:+.2f}%（>0 命中）",
    }


def evaluate_prediction(market_db: str, pred: dict) -> dict:
    if pred["scope"] == "market":
        return _eval_market(market_db, pred)
    if pred["scope"] == "sector":
        return _eval_sector(market_db, pred)
    return _eval_stock(market_db, pred)


def settle_due(market_db: str = MARKET_DB_PATH, db_path: str = DB_PATH,
               today: str | None = None, mode: str = "live") -> dict:
    """结算到期（target_date ≤ today）的预判；只更新结算字段。"""
    init_forecast_tables(db_path)
    today = today or datetime.now().strftime("%Y-%m-%d")
    with _connect(db_path) as conn:
        pending = conn.execute(
            "SELECT * FROM forecast_ledger WHERE mode=? AND status='PENDING' "
            "AND target_date<=? ORDER BY target_date, id",
            (mode, today),
        ).fetchall()
    stats = {"settled": 0, "hit": 0, "miss": 0, "unverifiable": 0}
    if pending:
        with sqlite3.connect(db_path, timeout=DB_TIMEOUT) as conn:
            for row in pending:
                pred = dict(row)
                result = evaluate_prediction(market_db, pred)
                status = result["status"]
                conn.execute(
                    "UPDATE forecast_ledger SET status=?, actual_return=?, "
                    "benchmark_return=?, breadth=?, settled_at=?, settle_note=? "
                    "WHERE id=?",
                    (
                        status, result.get("actual_return"),
                        result.get("benchmark_return"), result.get("breadth"),
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        result.get("note", ""), row["id"],
                    ),
                )
                stats["settled"] += 1
                stats[status.lower()] = stats.get(status.lower(), 0) + 1
            conn.commit()
    return stats


def collect_hit_rate(db_path: str = DB_PATH, mode: str = "live") -> dict:
    init_forecast_tables(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM forecast_ledger WHERE mode=? ORDER BY predict_date, scope, horizon",
            (mode,),
        ).fetchall()
    groups: dict[tuple[str, int], dict] = {}
    recent = []
    for r in rows:
        key = (r["scope"], r["horizon"])
        g = groups.setdefault(
            key, {"hit": 0, "miss": 0, "unverifiable": 0, "pending": 0, "void": 0}
        )
        st = r["status"]
        if st == "HIT":
            g["hit"] += 1
        elif st == "MISS":
            g["miss"] += 1
        elif st == "UNVERIFIABLE":
            g["unverifiable"] += 1
        elif st == "VOID":
            g["void"] += 1
        else:
            g["pending"] += 1
        if st not in ("PENDING", "VOID"):
            recent.append(dict(r))
    voids = [dict(r) for r in rows if r["status"] == "VOID"]
    close_runs = {r["run_date"] for r in rows if r["data_date"] == r["run_date"]}
    batches: dict[tuple[str, str, str], dict] = {}
    for v in voids:
        key = (v["run_date"], v["predict_date"], v["data_date"])
        b = batches.setdefault(key, {
            "run_date": v["run_date"], "predict_date": v["predict_date"],
            "data_date": v["data_date"], "n": 0,
            "reason": v["void_reason"] or v["settle_note"] or "",
            "has_close_batch": v["run_date"] in close_runs,
        })
        b["n"] += 1
    return {"groups": groups, "recent": recent[-30:], "total": len(rows),
            "void": len(voids), "void_rows": voids,
            "void_batches": sorted(batches.values(), key=lambda x: (x["run_date"], x["predict_date"]))}


def render_hit_rate_markdown(stats: dict) -> str:
    head, dirty = _git_state()
    lines = [
        "# 预判累计命中率",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- git HEAD：`{head or 'unknown'}`（工作区{'有未提交改动' if dirty else '干净'}）",
        "- 统计口径：命中率 = 命中 /（命中 + 未命中）；无法判定（停牌/缺数据/快照缺失）单列，不进分母；作废样本（VOID）不进任何分母。",
        f"- 作废样本：{stats.get('void', 0)} 条（见文末“作废留痕”）。",
        "",
        "## 分组统计",
        "",
        "| 组别 | 周期 | 命中 | 未命中 | 无法判定 | 待结算 | 已作废 | 命中率（不含无法判定/作废） |",
        "|---|---|---|---|---|---|---|---|",
    ]
    label = {"market": "大盘", "sector": "板块", "stock": "个股"}
    for (scope, horizon), g in sorted(stats["groups"].items(), key=lambda x: (x[0][0], x[0][1])):
        denom = g["hit"] + g["miss"]
        rate = f"{g['hit'] / denom * 100:.1f}%" if denom else "—"
        lines.append(
            f"| {label.get(scope, scope)} | {horizon}日 | {g['hit']} | {g['miss']} | "
            f"{g['unverifiable']} | {g['pending']} | {g['void']} | {rate} |"
        )
    if not stats["groups"]:
        lines.append("| — | — | 0 | 0 | 0 | 0 | 0 | — |")

    lines += ["", "## 最近已结算明细（最多 30 条）", ""]
    if stats["recent"]:
        lines += [
            "| 数据截止日 | 组别 | 对象 | 周期 | 判断 | 实际 | 基准 | 结果 | 说明 |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for r in stats["recent"]:
            actual = f"{r['actual_return']:+.2f}%" if r["actual_return"] is not None else "—"
            bench = f"{r['benchmark_return']:+.2f}%" if r["benchmark_return"] is not None else "—"
            lines.append(
                f"| {r['predict_date']} | {label.get(r['scope'], r['scope'])} | "
                f"{r['target_name']}（{r['target']}） | {r['horizon']}日 | {r['direction']} | "
                f"{actual} | {bench} | {r['status']} | {_md_escape(r['settle_note'] or '')} |"
            )
    else:
        lines.append("暂无已结算预判。")

    lines += ["", "## 作废批次（按生成日）", ""]
    if stats.get("void_batches"):
        lines += [
            "| 生成日 | 数据截止日 | 条数 | 当日有收盘批次 | 作废原因 |",
            "|---|---|---:|---|---|",
        ]
        for b in stats["void_batches"]:
            lines.append(
                f"| {b['run_date']} | {b['predict_date']} | {b['n']} | "
                f"{'是' if b['has_close_batch'] else '否（当日无有效收盘批次）'} | "
                f"{_md_escape(b['reason'])} |"
            )
    else:
        lines.append("无作废批次。")

    lines += ["", "## 作废留痕", ""]
    if stats.get("void_rows"):
        lines += [
            "| 数据截止日 | 生成日 | 组别 | 对象 | 周期 | 判断 | 作废原因 |",
            "|---|---|---|---|---|---|---|",
        ]
        for r in stats["void_rows"]:
            lines.append(
                f"| {r['predict_date']} | {r['run_date']} | {label.get(r['scope'], r['scope'])} | "
                f"{r['target_name']}（{r['target']}） | {r['horizon']}日 | {r['direction']} | "
                f"{_md_escape(r['void_reason'] or r['settle_note'] or '')} |"
            )
    else:
        lines.append("无作废样本。")
    lines.append("")
    return "\n".join(lines)


def write_hit_rate_report(db_path: str = DB_PATH, mode: str = "live") -> Path:
    """写命中率报告；写之前先把非收盘批次统一作废，保证分母口径一致。"""
    void_non_close_batches(db_path, mode)
    FORECAST_DIR.mkdir(parents=True, exist_ok=True)
    path = FORECAST_DIR / "预判命中率.md"
    path.write_text(render_hit_rate_markdown(collect_hit_rate(db_path, mode)), encoding="utf-8")
    return path


# ============================================================
# 每日入口
# ============================================================
def generate_daily(market_db: str = MARKET_DB_PATH, db_path: str = DB_PATH,
                   data_date: str | None = None, run_date: str | None = None
                   ) -> dict:
    """生成当日预判（同一生成日 + 同一数据截止日只允许一份，重复调用跳过）。

    run_date ≠ data_date 的批次（盘中手工批次、非交易日补跑）一律标 VOID：
    这类批次不是收盘批次，不进命中率统计，只留痕。
    """
    run_date = run_date or datetime.now().strftime("%Y-%m-%d")
    data_date = data_date or resolve_data_date(market_db, run_date)
    if not data_date:
        return {"ok": False, "reason": "无可用行情数据"}
    if _exists_predictions(db_path, run_date, data_date, "live"):
        return {"ok": True, "skipped": True, "data_date": data_date, "run_date": run_date,
                "reason": "该生成日 + 数据截止日已有预判，append-only 不覆盖"}

    notes_map: dict[str, list[str]] = {}

    market_rows = build_market_predictions(market_db, data_date, run_date)
    if not market_rows:
        notes_map.setdefault("market", []).append("沪深300 历史K线不足 20 根")

    sector_rows, sector_notes = build_sector_predictions(market_db, data_date, run_date)
    notes_map["sector"] = sector_notes

    stock_rows, stock_notes = build_stock_predictions_live(market_db, data_date, run_date)
    notes_map["stock"] = stock_notes

    rows = market_rows + sector_rows + stock_rows
    inserted = save_predictions(rows, db_path)
    voided, void_reason = 0, ""
    if inserted and run_date != data_date:
        void_reason = _non_close_reason(run_date, data_date)
        voided = mark_void(data_date, void_reason, run_date=run_date, db_path=db_path)
    path = write_forecast_report(rows, data_date, run_date, notes_map,
                                 void_reason=void_reason or None)
    return {
        "ok": True, "skipped": False, "data_date": data_date, "run_date": run_date,
        "market": len(market_rows), "sector": len(sector_rows),
        "stock": len(stock_rows), "inserted": inserted, "path": str(path),
        "voided": voided, "void_reason": void_reason,
        "notes": notes_map,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="每日预判 + 到期对账")
    parser.add_argument("--data-date", default=None, help="数据截止交易日 YYYY-MM-DD")
    parser.add_argument("--run-date", default=None, help="生成日期 YYYY-MM-DD")
    parser.add_argument("--market-db", default=MARKET_DB_PATH)
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--settle-only", action="store_true")
    parser.add_argument("--no-settle", action="store_true")
    args = parser.parse_args(argv)

    run_date = args.run_date or datetime.now().strftime("%Y-%m-%d")
    data_date = args.data_date or resolve_data_date(args.market_db, run_date)
    if not data_date:
        print("无可用行情数据，无法预判")
        return 2

    if not args.settle_only and not args.no_settle:
        s = settle_due(args.market_db, args.db, today=data_date)
        print(f"到期结算：{s}")
        write_hit_rate_report(args.db)

    if args.settle_only:
        write_hit_rate_report(args.db)
        print(f"命中率报告：{FORECAST_DIR / '预判命中率.md'}")
        return 0

    result = generate_daily(args.market_db, args.db, data_date, run_date)
    print(f"预判结果：{result}")
    write_hit_rate_report(args.db)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
