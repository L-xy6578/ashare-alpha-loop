"""
每日决策记录（append-only）+ 决策口径 KPI

用户拍板口径（2026-11-06）：
- 15:35 收盘生成决策记录，含伏击价格区间、手数、金额、止损、止盈档位、失效期
- 次日进入伏击区间按区间成交价模拟买入；3 个交易日未成交则作废
- 不建仓也必须留记录，写明弃权原因
- 产物：outputs/决策日志/决策_YYYY-MM-DD.md（append-only）+ 决策记录.csv
- 同日重复运行只提示"已存在"并跳过，不覆盖/不删除当天记录
- KPI 只统计决策记录中标记为建仓并最终平仓的标的；候选排行不计入成绩

策略路线变更：五因子选股退役、转指数化（见 docs/ARCHITECTURE.md）。
新产生的记录一律 `decision='OBSERVE'`（观察记录），不再产出建仓计划/伏击区间；
历史 `ENTRY` 记录保留不动，结算与 KPI 仍只认历史 `ENTRY`，不会凭空产生模拟成交。

数据表落在 knowledge.db（sim 账户专用）：
    decision_log     每日决策（UNIQUE(decision_date, symbol)，只插入不覆盖）
    decision_plans   已成交持仓的分批止盈/移动止损状态
    decision_trades  成交明细（买入/加仓/止盈/止损）
    decision_account 模拟账户现金（初始 30,000）

用法:
    from src.decision_log import DecisionStore, CandidateInput, make_decision_row

    store = DecisionStore()
    row = make_decision_row(decision_date="2026-11-06", data_date="2026-11-06",
                            cand=..., snapshot_date="2026-11-06", snapshot_complete=True,
                            git_head="8dde356", git_dirty=False)
    store.save_rows([row])
"""

from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from src.config import DB_PATH, MARKET_DB_PATH
from src.db import get_connection
from src.logger import get_logger
from src.vcp import (SECTOR_PERCENTILE_MAX, SECTOR_PERCENTILE_MIN,
                     VcpResult, sector_position_ok)

log = get_logger(__name__)

DECISION_MARK = "VCP决策单"      # 写入 positions.note 的归属标记
DEFAULT_DECISION_DIR = Path("outputs") / "决策日志"
DECISION_CSV = "决策记录.csv"
SCORE_THRESHOLD = 70.0
EXPIRY_TRADING_DAYS = 3
MAX_POSITIONS = 3
MAX_TOTAL_AMOUNT = 18000.0
MIN_CASH = 12000.0

_CSV_COLUMNS = [
    "decision_date", "data_date", "symbol", "code", "name", "sector",
    "score", "industry", "growth", "valuation", "technical", "capital",
    "missing", "vcp", "contractions", "vol_ratio", "sector_pct",
    "decision", "reason", "zone_low", "zone_high", "shares", "amount",
    "stop_price", "tp1", "tp2", "expire_date", "snapshot_date",
    "snapshot_complete", "git_head", "git_dirty",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decision_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_date TEXT NOT NULL,
    data_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    sector TEXT NOT NULL DEFAULT '',
    score REAL,
    factors TEXT NOT NULL DEFAULT '{}',
    vcp INTEGER NOT NULL DEFAULT 0,
    contractions INTEGER NOT NULL DEFAULT 0,
    vol_ratio REAL,
    depths TEXT NOT NULL DEFAULT '[]',
    box_high REAL,
    box_low REAL,
    sector_pct REAL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    zone_low REAL,
    zone_high REAL,
    shares INTEGER NOT NULL DEFAULT 0,
    amount REAL NOT NULL DEFAULT 0,
    stop_price REAL,
    stop_structural REAL,
    tp1 REAL,
    tp2 REAL,
    expire_date TEXT,
    snapshot_date TEXT NOT NULL DEFAULT '',
    snapshot_complete INTEGER NOT NULL DEFAULT 0,
    git_head TEXT NOT NULL DEFAULT '',
    git_dirty INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'PENDING',
    fill_date TEXT,
    fill_price REAL,
    filled_shares INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE(decision_date, symbol)
);

CREATE TABLE IF NOT EXISTS decision_plans (
    symbol TEXT PRIMARY KEY,
    decision_date TEXT NOT NULL,
    name TEXT NOT NULL,
    entry_date TEXT NOT NULL,
    entry_price REAL NOT NULL,
    shares_total INTEGER NOT NULL,
    shares_remaining INTEGER NOT NULL,
    tier1_done INTEGER NOT NULL DEFAULT 0,
    tier2_done INTEGER NOT NULL DEFAULT 0,
    tp1 REAL,
    tp2 REAL,
    stop_price REAL NOT NULL,
    box_high REAL,
    box_low REAL,
    peak_price REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN',
    close_reason TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decision_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    side TEXT NOT NULL,
    shares INTEGER NOT NULL,
    price REAL NOT NULL,
    amount REAL NOT NULL,
    commission REAL NOT NULL DEFAULT 0,
    stamp_tax REAL NOT NULL DEFAULT 0,
    transfer_fee REAL NOT NULL DEFAULT 0,
    total_fee REAL NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    holding_days INTEGER,
    return_pct REAL,
    pnl REAL,
    created_at TEXT NOT NULL,
    UNIQUE(trade_date, symbol, side, reason)
);

CREATE TABLE IF NOT EXISTS decision_account (
    account TEXT PRIMARY KEY,
    initial_cash REAL NOT NULL,
    cash REAL NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def init_decision_db(db_path: str = DB_PATH, initial_cash: float = 30000.0) -> str:
    with get_connection(db_path) as conn:
        conn.executescript(_SCHEMA)
        # 旧库迁移：close_reason 用于写明平仓原因（分批止盈卖光 / 止损 / 移动止损）
        cols = {r[1] for r in conn.execute("PRAGMA table_info(decision_plans)")}
        if "close_reason" not in cols:
            conn.execute(
                "ALTER TABLE decision_plans ADD COLUMN close_reason TEXT NOT NULL DEFAULT ''")
        conn.execute(
            """INSERT OR IGNORE INTO decision_account
               (account, initial_cash, cash, updated_at) VALUES ('sim', ?, ?, ?)""",
            (initial_cash, initial_cash, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
    return db_path


def calc_fees(side: str, amount: float) -> dict:
    """A 股交易费用：佣金万三（最低 5 元/笔）、卖出印花税 0.05%、
    过户费 0.001% 双边"""
    amount = max(0.0, float(amount))
    commission = max(5.0, round(amount * 0.0003, 2)) if amount > 0 else 0.0
    transfer = round(amount * 0.00001, 2)
    stamp = round(amount * 0.0005, 2) if side.upper() == "SELL" else 0.0
    return {
        "commission": commission,
        "stamp_tax": stamp,
        "transfer_fee": transfer,
        "total_fee": round(commission + transfer + stamp, 2),
    }


def trading_days_after(start: str, n: int, market_db: str = MARKET_DB_PATH) -> str:
    """start 之后第 n 个交易日（YYYY-MM-DD）；日历缺失时回退为自然日"""
    try:
        with sqlite3.connect(market_db, timeout=5) as conn:
            rows = conn.execute(
                "SELECT date FROM trade_calendar WHERE is_trading=1 AND date>? "
                "ORDER BY date LIMIT ?", (start, n),
            ).fetchall()
        if len(rows) >= n:
            return rows[n - 1][0]
    except Exception:
        pass
    d = date.fromisoformat(start)
    added = 0
    while added < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            added += 1
    return d.isoformat()


@dataclass
class CandidateInput:
    """决策输入：一只通过可执行性过滤的候选

    score/factors 来自现有五因子评分（本模块不改权重与口径）；
    vcp 来自 src.vcp.detect_vcp；sector_pct 来自行业景气分位。
    """

    symbol: str
    code: str
    name: str
    sector: str
    price: float
    bars: list
    score: float | None = None
    factors: dict = field(default_factory=dict)
    missing: tuple[str, ...] = ()
    vcp: VcpResult | None = None
    sector_pct: float | None = None
    first_shares: int | None = None


def make_decision_row(
    *,
    decision_date: str,
    data_date: str,
    cand: CandidateInput,
    snapshot_date: str,
    snapshot_complete: bool,
    git_head: str,
    git_dirty: bool,
) -> dict:
    """把候选转成一条观察记录（五因子/VCP 结论留痕，不再产出建仓计划）"""
    reasons: list[str] = []
    factors = dict(cand.factors or {})
    factors_json = json.dumps({**factors, "_missing": list(cand.missing or ())},
                              ensure_ascii=False)
    vcp = cand.vcp

    if cand.score is None:
        reasons.append("无五因子评分（数据不足）")
    elif cand.score < SCORE_THRESHOLD:
        reasons.append(f"五因子 {cand.score:.1f} < {SCORE_THRESHOLD:.0f}")
    if vcp is None:
        reasons.append("VCP 无法识别（K线不足）")
    elif not vcp.is_vcp:
        reasons.append("VCP 不成立：" + "；".join(vcp.reasons or ("结构未满足",)))
    sec_ok, sec_note = sector_position_ok(cand.sector_pct)
    if not sec_ok:
        reasons.append(sec_note)

    reason = "；".join(reasons) if reasons else "五因子与 VCP 条件均满足"
    row = {
        "decision_date": decision_date,
        "data_date": data_date,
        "symbol": cand.symbol, "code": cand.code, "name": cand.name,
        "sector": cand.sector or "",
        "score": round(cand.score, 1) if cand.score is not None else None,
        "factors": factors_json,
        "vcp": 1 if (vcp and vcp.is_vcp) else 0,
        "contractions": vcp.contractions if vcp else 0,
        "vol_ratio": vcp.last_vol_ratio if vcp else None,
        "depths": json.dumps(list(vcp.depths) if vcp else [], ensure_ascii=False),
        "box_high": vcp.box_high if vcp else None,
        "box_low": vcp.box_low if vcp else None,
        "sector_pct": round(cand.sector_pct, 1) if cand.sector_pct is not None else None,
        # 2026-11-11 策略路线变更：五因子选股退役，评分段记录一律为观察记录
        "decision": "OBSERVE",
        "reason": "观察记录｜" + reason,
        "snapshot_date": snapshot_date,
        "snapshot_complete": 1 if snapshot_complete else 0,
        "git_head": git_head,
        "git_dirty": 1 if git_dirty else 0,
        "status": "OBSERVE",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    return row


def make_excluded_row(*, decision_date: str, data_date: str, eligibility,
                      snapshot_date: str, snapshot_complete: bool,
                      git_head: str, git_dirty: bool) -> dict:
    """可执行性排除记录（同样落盘，不做静默丢弃）"""
    return {
        "decision_date": decision_date, "data_date": data_date,
        "symbol": eligibility.symbol, "code": eligibility.code, "name": eligibility.name,
        "sector": "", "score": None, "factors": "{}",
        "vcp": 0, "contractions": 0, "vol_ratio": None, "depths": "[]",
        "decision": "EXCLUDED",
        "reason": "可执行性排除：" + eligibility.reason,
        "snapshot_date": snapshot_date,
        "snapshot_complete": 1 if snapshot_complete else 0,
        "git_head": git_head, "git_dirty": 1 if git_dirty else 0,
        "status": "EXCLUDED",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def make_no_candidate_row(*, decision_date: str, data_date: str, reason: str,
                         snapshot_date: str, snapshot_complete: bool,
                         git_head: str, git_dirty: bool) -> dict:
    """当日无可执行候选时也必须留记录"""
    return {
        "decision_date": decision_date, "data_date": data_date,
        "symbol": "—", "code": "—", "name": "无候选",
        "sector": "", "score": None, "factors": "{}",
        "vcp": 0, "contractions": 0, "vol_ratio": None, "depths": "[]",
        "decision": "NO_CANDIDATE", "reason": reason,
        "snapshot_date": snapshot_date,
        "snapshot_complete": 1 if snapshot_complete else 0,
        "git_head": git_head, "git_dirty": 1 if git_dirty else 0,
        "status": "ABSTAIN",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


class DecisionStore:
    """决策记录读写（knowledge.db，sim 账户专用）"""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        init_decision_db(db_path)

    # ---------------- 决策 ----------------
    def save_rows(self, rows: list[dict]) -> tuple[int, int]:
        """只插入不覆盖；返回 (新增, 已存在跳过)"""
        inserted = skipped = 0
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with get_connection(self.db_path) as conn:
            for r in rows:
                cur = conn.execute(
                    """INSERT OR IGNORE INTO decision_log
                       (decision_date, data_date, symbol, code, name, sector, score,
                        factors, vcp, contractions, vol_ratio, depths, box_high, box_low,
                        sector_pct, decision, reason, zone_low, zone_high, shares, amount,
                        stop_price, stop_structural, tp1, tp2, expire_date, snapshot_date,
                        snapshot_complete, git_head, git_dirty, status, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (r["decision_date"], r["data_date"], r["symbol"], r["code"], r["name"],
                     r.get("sector", ""), r.get("score"), r.get("factors", "{}"),
                     r.get("vcp", 0), r.get("contractions", 0), r.get("vol_ratio"),
                     r.get("depths", "[]"), r.get("box_high"), r.get("box_low"),
                     r.get("sector_pct"), r["decision"], r.get("reason", ""),
                     r.get("zone_low"), r.get("zone_high"), r.get("shares", 0),
                     r.get("amount", 0.0), r.get("stop_price"), r.get("stop_structural"),
                     r.get("tp1"), r.get("tp2"), r.get("expire_date"),
                     r.get("snapshot_date", ""), r.get("snapshot_complete", 0),
                     r.get("git_head", ""), r.get("git_dirty", 0),
                     r.get("status", "PENDING"), r.get("created_at", now)),
                )
                if cur.rowcount:
                    inserted += 1
                else:
                    skipped += 1
        return inserted, skipped

    def day_exists(self, decision_date: str) -> bool:
        with get_connection(self.db_path, readonly=True) as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM decision_log WHERE decision_date=?",
                (decision_date,),
            ).fetchone()[0]
        return n > 0

    def rows_by_date(self, decision_date: str) -> list[dict]:
        with get_connection(self.db_path, readonly=True) as conn:
            rows = conn.execute(
                "SELECT * FROM decision_log WHERE decision_date=? ORDER BY id",
                (decision_date,),
            ).fetchall()
        return [dict(r) for r in rows]

    def pending(self, as_of: str) -> list[dict]:
        with get_connection(self.db_path, readonly=True) as conn:
            rows = conn.execute(
                "SELECT * FROM decision_log WHERE status='PENDING' AND decision='ENTRY' "
                "AND decision_date<? ORDER BY decision_date, id", (as_of,),
            ).fetchall()
        return [dict(r) for r in rows]

    def active_symbols(self) -> set[str]:
        """需要保留行情的标的：未成交决策 + 未平仓计划"""
        with get_connection(self.db_path, readonly=True) as conn:
            rows = conn.execute(
                """SELECT symbol FROM decision_log WHERE status='PENDING' AND decision='ENTRY'
                   UNION SELECT symbol FROM decision_plans WHERE status='OPEN'"""
            ).fetchall()
        return {r[0] for r in rows}

    # ---------------- 计划 / 成交 ----------------
    def open_plans(self) -> list[dict]:
        with get_connection(self.db_path, readonly=True) as conn:
            rows = conn.execute(
                "SELECT * FROM decision_plans WHERE status='OPEN' ORDER BY symbol"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_plan(self, symbol: str) -> dict | None:
        with get_connection(self.db_path, readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM decision_plans WHERE symbol=?", (symbol,),
            ).fetchone()
        return dict(row) if row else None

    def upsert_plan(self, **p) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with get_connection(self.db_path) as conn:
            conn.execute(
                """INSERT INTO decision_plans
                   (symbol, decision_date, name, entry_date, entry_price, shares_total,
                    shares_remaining, tier1_done, tier2_done, tp1, tp2, stop_price,
                    box_high, box_low, peak_price, status, close_reason, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(symbol) DO UPDATE SET
                     decision_date=excluded.decision_date,
                     entry_date=excluded.entry_date,
                     entry_price=excluded.entry_price,
                     tp1=excluded.tp1, tp2=excluded.tp2,
                     box_high=excluded.box_high, box_low=excluded.box_low,
                     shares_total=excluded.shares_total,
                     shares_remaining=excluded.shares_remaining,
                     tier1_done=excluded.tier1_done, tier2_done=excluded.tier2_done,
                     stop_price=excluded.stop_price, peak_price=excluded.peak_price,
                     status=excluded.status, close_reason=excluded.close_reason,
                     updated_at=excluded.updated_at""",
                (p["symbol"], p["decision_date"], p["name"], p["entry_date"],
                 p["entry_price"], p["shares_total"], p["shares_remaining"],
                 p.get("tier1_done", 0), p.get("tier2_done", 0),
                 p.get("tp1"), p.get("tp2"), p["stop_price"],
                 p.get("box_high"), p.get("box_low"), p["peak_price"],
                 p.get("status", "OPEN"), p.get("close_reason", ""), now),
            )

    def add_trade(self, **t) -> bool:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with get_connection(self.db_path) as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO decision_trades
                   (decision_date, symbol, name, trade_date, side, shares, price, amount,
                    commission, stamp_tax, transfer_fee, total_fee, reason, holding_days,
                    return_pct, pnl, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (t["decision_date"], t["symbol"], t["name"], t["trade_date"],
                 t["side"], t["shares"], t["price"], t["amount"],
                 t["commission"], t["stamp_tax"], t["transfer_fee"], t["total_fee"],
                 t.get("reason", ""), t.get("holding_days"), t.get("return_pct"),
                 t.get("pnl"), now),
            )
        return bool(cur.rowcount)

    def trades(self) -> list[dict]:
        with get_connection(self.db_path, readonly=True) as conn:
            rows = conn.execute(
                "SELECT * FROM decision_trades ORDER BY trade_date, id"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- 现金 ----------------
    def cash(self) -> tuple[float, float]:
        with get_connection(self.db_path, readonly=True) as conn:
            row = conn.execute(
                "SELECT initial_cash, cash FROM decision_account WHERE account='sim'"
            ).fetchone()
        return (row[0], row[1]) if row else (30000.0, 30000.0)

    def set_cash(self, cash: float) -> None:
        with get_connection(self.db_path) as conn:
            conn.execute(
                "UPDATE decision_account SET cash=?, updated_at=? WHERE account='sim'",
                (round(cash, 2), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )

    def update_log_status(self, decision_date: str, symbol: str, status: str,
                          **fields) -> None:
        sets = ["status=?"] + [f"{k}=?" for k in fields]
        with get_connection(self.db_path) as conn:
            conn.execute(
                f"UPDATE decision_log SET {', '.join(sets)} "
                "WHERE decision_date=? AND symbol=?",
                (status, *fields.values(), decision_date, symbol),
            )


# ============================================================
# Markdown / CSV 产物
# ============================================================

def _fmt(v, digits: int = 2, dash: str = "—") -> str:
    return dash if v is None else f"{v:.{digits}f}"


def render_day_markdown(
    *,
    decision_date: str,
    data_date: str,
    rows: list[dict],
    excluded: list[dict],
    snapshot_date: str,
    snapshot_complete: bool,
    git_head: str,
    git_dirty: bool,
    settle_lines: list[str] | None = None,
    kpi_text: str = "",
) -> str:
    head = [
        f"# 决策日志（{decision_date}）",
        "",
        f"> 决策日期：{decision_date} · 数据截止：{data_date}",
        f"> git HEAD：{git_head or '未知'}"
        + ("（工作区有未提交改动）" if git_dirty else "（工作区干净）"),
        "> 账户：sim 模拟盘 · 本金 30,000 元 · 单只 ≤6,000 元 · 最多 3 只 · 总仓 ≤18,000 元 · 常留现金 ≥12,000 元",
        f"> 板块快照：{snapshot_date or '无'} · complete={'是' if snapshot_complete else '否'}",
        "> 策略路线（2026-11-11 起）：五因子选股已退役、转指数化；本文件仅记录观察结论，"
        "不再产出建仓计划/伏击区间，指数化组合净值见收益报表。",
        "> 历史 ENTRY 行（改造前）保留原文与结算口径，不追溯改写。",
        "",
        "## 一、候选与五因子",
        "",
        "| 代码 | 名称 | 板块 | 总分 | 行业 | 业绩 | 估值 | 技术 | 资金 | 缺失 | VCP | 收缩/量能比 | 板块分位 | 决策 | 伏击区间 | 首仓 | 止损 | 止盈档 | 失效日 | 说明 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    body: list[str] = []
    no_candidate_reason = ""
    for r in rows:
        if r.get("symbol") == "—":
            no_candidate_reason = r.get("reason") or ""
            continue
        if r.get("decision") == "EXCLUDED":
            continue   # 排除名单在第二/三节单列，避免候选表重复
        f = json.loads(r.get("factors") or "{}")
        miss = f.get("_missing") or []
        key_map = {"industry": "行业景气", "growth": "业绩证据",
                   "valuation": "估值", "technical": "技术面", "capital": "资金面"}

        def cell(key: str, digits: int = 0) -> str:
            return "—" if key_map[key] in miss else _fmt(f.get(key), digits)
        vcp_txt = "是" if r.get("vcp") else "否"
        if r.get("vcp") and r.get("vol_ratio") is not None:
            vcp_txt = f"是({r.get('contractions')}/{r.get('vol_ratio'):.2f})"
        elif r.get("contractions"):
            vcp_txt = f"否({r.get('contractions')})"
        zone = "—"
        if r.get("zone_low") and r.get("zone_high"):
            zone = f"{r['zone_low']:.2f}~{r['zone_high']:.2f}"
        tp = "—"
        if r.get("tp1") and r.get("tp2"):
            tp = f"{r['tp1']:.2f}/{r['tp2']:.2f}"
        decision = {"ENTRY": "建仓计划", "ABSTAIN": "弃权",
                    "OBSERVE": "观察记录"}.get(r.get("decision"), r.get("decision"))
        body.append(
            f"| {r.get('code')} | {r.get('name')} | {r.get('sector') or '—'} | "
            f"{_fmt(r.get('score'), 1)} | {_fmt(f.get('industry'), 0)} | "
            f"{_fmt(f.get('growth'), 0)} | {_fmt(f.get('valuation'), 0)} | "
            f"{_fmt(f.get('technical'), 0)} | {_fmt(f.get('capital'), 0)} | "
            f"{'、'.join(miss) or '—'} | {vcp_txt} | "
            f"{_fmt(r.get('sector_pct'), 1)} | {decision} | {zone} | "
            f"{r.get('shares') or '—'} | {_fmt(r.get('stop_price'))} | {tp} | "
            f"{r.get('expire_date') or '—'} | {r.get('reason') or ''} |"
        )
    if not body:
        body.append("| — | — | — | — | — | — | — | — | — | — | — | — | — | 无候选 | — | — | — | — | — | — |")
    head += body
    if no_candidate_reason:
        head += ["", f"- **当日无候选**：{no_candidate_reason}"]

    analysis_only = [e for e in excluded if e.get("analysis_only")]
    other_excluded = [e for e in excluded if not e.get("analysis_only")]

    head += ["", "## 二、仅分析不可买（板块权限限制）", ""]
    if analysis_only:
        head += ["| 代码 | 名称 | 排除原因 |", "|---|---|---|"]
        head += [f"| {e.get('code')} | {e.get('name')} | {e.get('reason')} |"
                 for e in analysis_only]
    else:
        head += ["（无：当日候选未出现创业板/科创板/北交所标的）"]

    head += ["", "## 三、其他可执行性排除", ""]
    if other_excluded:
        head += ["| 代码 | 名称 | 排除原因 |", "|---|---|---|"]
        head += [f"| {e.get('code')} | {e.get('name')} | {e.get('reason')} |"
                 for e in other_excluded]
    else:
        head += ["（无）"]

    head += ["", "## 四、成交与持仓（当日结算）", ""]
    head += settle_lines or ["（无成交 / 无持仓变动）"]

    head += ["", "## 五、KPI（决策口径）", ""]
    head += [kpi_text or "（无已平仓决策样本）"]
    head += ["", "---", "*本文件 append-only：同日重跑只提示已存在并跳过。*", ""]
    return "\n".join(head)


def write_day_markdown(decision_date: str, text: str,
                       out_dir: Path | None = None) -> tuple[Path, bool]:
    """写入决策日志；已存在则跳过（append-only）"""
    out_dir = out_dir or DEFAULT_DECISION_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"决策_{decision_date}.md"
    if path.exists():
        return path, False
    path.write_text(text, encoding="utf-8")
    return path, True


def append_csv(rows: list[dict], out_dir: Path | None = None) -> int:
    """追加 CSV（按 decision_date+symbol 去重）；返回新增行数"""
    out_dir = out_dir or DEFAULT_DECISION_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / DECISION_CSV
    existing: set[tuple[str, str]] = set()
    if path.exists():
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                existing.add((r.get("decision_date", ""), r.get("symbol", "")))
    new_rows = [r for r in rows if (r["decision_date"], r["symbol"]) not in existing]
    if not new_rows:
        return 0
    write_header = not path.exists()
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_CSV_COLUMNS, extrasaction="ignore")
        if write_header:
            w.writeheader()
        for r in new_rows:
            f_ = json.loads(r.get("factors") or "{}")
            w.writerow({
                **{k: r.get(k) for k in _CSV_COLUMNS},
                "industry": f_.get("industry"), "growth": f_.get("growth"),
                "valuation": f_.get("valuation"), "technical": f_.get("technical"),
                "capital": f_.get("capital"),
                "missing": "",
                "snapshot_complete": "1" if r.get("snapshot_complete") else "0",
                "git_dirty": "1" if r.get("git_dirty") else "0",
            })
    return len(new_rows)


# ============================================================
# 决策口径 KPI
# ============================================================

def decision_kpi(db_path: str = DB_PATH) -> dict:
    """只统计决策记录中建仓且最终平仓的标的（候选排行不计入）"""
    with get_connection(db_path, readonly=True) as conn:
        decisions = conn.execute(
            "SELECT * FROM decision_log WHERE decision='ENTRY' AND status='CLOSED'"
        ).fetchall()
        trades = conn.execute("SELECT * FROM decision_trades").fetchall()
    by_key: dict[tuple[str, str], list] = {}
    for t in trades:
        by_key.setdefault((t["decision_date"], t["symbol"]), []).append(t)
    samples = []
    for d in decisions:
        key = (d["decision_date"], d["symbol"])
        ts = by_key.get(key, [])
        buy = sum(t["amount"] + t["total_fee"] for t in ts if t["side"] == "BUY")
        sell = sum(t["amount"] - t["total_fee"] for t in ts if t["side"] == "SELL")
        if not ts or buy <= 0:
            continue
        pnl = round(sell - buy, 2)
        ret = round(pnl / buy * 100, 2) if buy else 0.0
        samples.append({"symbol": d["symbol"], "name": d["name"],
                        "decision_date": d["decision_date"],
                        "pnl": pnl, "return_pct": ret})
    wins = sum(1 for s in samples if s["pnl"] > 0)
    return {
        "closed": len(samples), "wins": wins,
        "win_rate": round(wins / len(samples) * 100, 1) if samples else None,
        "total_pnl": round(sum(s["pnl"] for s in samples), 2),
        "avg_return_pct": round(sum(s["return_pct"] for s in samples) / len(samples), 2)
        if samples else None,
        "samples": samples,
    }


def render_decision_kpi(kpi: dict) -> str:
    if not kpi.get("closed"):
        return ("- 已平仓决策样本：0 笔（候选排行不计入成绩）\n"
                "- 历史 sim 样本（宝钢）：旧口径，不计入新统计（保留不删除）")
    return (
        f"- 已平仓决策样本：{kpi['closed']} 笔｜胜率 {kpi['win_rate']}%"
        f"（{kpi['wins']} 胜）｜累计净盈亏 {kpi['total_pnl']:+.2f} 元"
        f"｜平均单笔 {kpi['avg_return_pct']:+.2f}%"
        "\n- 口径：仅统计 decision_log 中 ENTRY→CLOSED 的标的；候选排行/推荐不计入"
        "\n- 历史 sim 样本（宝钢）：旧口径，不计入新统计（保留不删除）"
    )
