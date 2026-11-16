"""
三角闭环选股体系 — 持仓状态表（决策连贯性修复）

背景：R44~R47 期间发现——交易员决策每次"硬编码假设持仓 1000 股"，
7-28 建议减仓 75% 后，8-03 却仍按满仓决策，前后矛盾。
本模块维护"当前持仓"状态表，让每次决策基于真实持仓，决策后回写，
实现"减仓 → 更新持仓 → 下次决策衔接"的连贯闭环。

账户分离：positions 表支持 account 维度——
    sim（系统模拟盘，默认） / real（用户真实持仓）。
    两套数据必须分开，复盘/决策前先确认真实持仓口径，禁止混用。

用法:
    from src.portfolio import PortfolioStore, Position

    store = PortfolioStore()
    pos = store.get("sh600000")                          # 读模拟盘持仓（默认 sim）
    pos = store.get("sh600000", account="real")          # 读真实持仓
    store.set("sh600000", "示例银行", 250, 5.50, 6.31, "示例建仓")
    store.reduce("sh600000", 250, 6.31, "示例清仓")
"""

from __future__ import annotations

from dataclasses import dataclass

from src.config import DB_PATH
from src.db import get_connection
from src.logger import get_logger

log = get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    account     TEXT NOT NULL DEFAULT 'sim',   -- sim=系统模拟盘 / real=真实持仓
    symbol      TEXT NOT NULL,
    name        TEXT NOT NULL,
    shares      INTEGER NOT NULL,          -- 当前持仓股数（0=已清仓）
    cost_price  REAL NOT NULL,             -- 持仓成本价
    last_price  REAL,                      -- 最近一次参考价
    note        TEXT,                      -- 最近操作/建议来源
    updated_at  TEXT NOT NULL,
    entry_date  TEXT,                      -- 建仓日期（#82：卖出时算 holding_days）
    PRIMARY KEY (account, symbol)
);
"""


@dataclass(frozen=True)
class Position:
    """一条持仓状态"""

    symbol: str
    name: str
    shares: int
    cost_price: float
    last_price: float | None = None
    note: str = ""
    account: str = "sim"
    entry_date: str | None = None


def init_portfolio_db(db_path: str = DB_PATH) -> str:
    with get_connection(db_path) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(positions)").fetchall()]
        if cols and "account" not in cols:
            # 迁移：旧表（symbol 主键）→ 新表（account+symbol 复合主键），存量归入 sim 账户
            conn.executescript(
                """
                ALTER TABLE positions RENAME TO positions_legacy;
                CREATE TABLE positions (
                    account     TEXT NOT NULL DEFAULT 'sim',
                    symbol      TEXT NOT NULL,
                    name        TEXT NOT NULL,
                    shares      INTEGER NOT NULL,
                    cost_price  REAL NOT NULL,
                    last_price  REAL,
                    note        TEXT,
                    updated_at  TEXT NOT NULL,
                    entry_date  TEXT,
                    PRIMARY KEY (account, symbol)
                );
                INSERT INTO positions (account, symbol, name, shares, cost_price,
                                       last_price, note, updated_at)
                    SELECT 'sim', symbol, name, shares, cost_price,
                           last_price, note, updated_at
                    FROM positions_legacy;
                DROP TABLE positions_legacy;
                """
            )
            log.info("持仓表已迁移: 新增 account 维度，存量归入 sim 账户")
        else:
            conn.executescript(_SCHEMA)
        # 兼容旧表：补 entry_date 列（#82）
        cols = [r[1] for r in conn.execute("PRAGMA table_info(positions)").fetchall()]
        if "entry_date" not in cols:
            conn.execute("ALTER TABLE positions ADD COLUMN entry_date TEXT")
            log.info("持仓表迁移: positions 新增 entry_date 列")
    return db_path


class PortfolioStore:
    """持仓状态表读写"""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        init_portfolio_db(db_path)

    def get(self, symbol: str, account: str = "sim") -> Position | None:
        with get_connection(self.db_path, readonly=True) as conn:
            row = conn.execute(
                """SELECT symbol, name, shares, cost_price, last_price, note, account, entry_date
                   FROM positions WHERE account=? AND symbol=?""",
                (account, symbol),
            ).fetchone()
        if not row:
            return None
        return Position(*row)

    def all(self, account: str | None = None) -> list[Position]:
        """全部持仓（account=None 返回所有账户）"""
        with get_connection(self.db_path, readonly=True) as conn:
            if account:
                rows = conn.execute(
                    """SELECT symbol, name, shares, cost_price, last_price, note, account, entry_date
                       FROM positions WHERE account=? ORDER BY symbol""",
                    (account,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT symbol, name, shares, cost_price, last_price, note, account, entry_date
                       FROM positions ORDER BY account, symbol"""
                ).fetchall()
        return [Position(*r) for r in rows]

    def holding(self, account: str = "sim") -> list[Position]:
        """当前实际持仓（shares > 0）；默认模拟盘账户"""
        return [p for p in self.all(account) if p.shares > 0]

    def set(
        self,
        symbol: str,
        name: str,
        shares: int,
        cost_price: float,
        last_price: float | None = None,
        note: str = "",
        account: str = "sim",
        entry_date: str | None = None,
    ) -> Position:
        """写入/覆盖持仓状态（建仓/更新）"""
        from datetime import datetime

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if entry_date is None:
            cur = self.get(symbol, account=account)
            entry_date = cur.entry_date if cur else None
        with get_connection(self.db_path) as conn:
            conn.execute(
                """INSERT INTO positions (account, symbol, name, shares, cost_price, last_price, note, updated_at, entry_date)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(account, symbol) DO UPDATE SET
                     name=excluded.name, shares=excluded.shares, cost_price=excluded.cost_price,
                     last_price=excluded.last_price, note=excluded.note, updated_at=excluded.updated_at,
                     entry_date=excluded.entry_date""",
                (account, symbol, name, shares, cost_price, last_price, note, now, entry_date),
            )
        log.info("持仓更新 [%s] %s(%s): %d股 @%.2f 建仓%s %s",
                 account, name, symbol, shares, cost_price, entry_date or "?", note)
        return Position(symbol, name, shares, cost_price, last_price, note, account, entry_date)

    def reduce(
        self,
        symbol: str,
        reduce_shares: int,
        price: float,
        reason: str = "",
        account: str = "sim",
    ) -> Position | None:
        """减仓：从当前持仓中扣减股数，写回持仓表"""
        cur = self.get(symbol, account=account)
        if cur is None or cur.shares <= 0:
            log.warning("减仓 %s 失败：无持仓", symbol)
            return None
        new_shares = max(0, cur.shares - reduce_shares)
        action = "清仓" if new_shares == 0 else f"减仓{reduce_shares}股"
        note = f"{action}@{price} {reason}".strip()
        return self.set(symbol, cur.name, new_shares, cur.cost_price, price, note, account=account)

    def clear(self, symbol: str, reason: str = "清仓", account: str = "sim") -> Position | None:
        cur = self.get(symbol, account=account)
        if cur is None:
            return None
        return self.set(symbol, cur.name, 0, cur.cost_price, cur.last_price, reason, account=account)

    def total_market_value(self, account: str = "sim") -> float:
        """持仓总市值（按 last_price 估算）"""
        return sum(p.shares * (p.last_price or p.cost_price) for p in self.holding(account))

    def position_pct(self, total_assets: float, account: str = "sim") -> float:
        """总仓位率（%）"""
        if total_assets <= 0:
            return 0.0
        return self.total_market_value(account) / total_assets * 100.0


if __name__ == "__main__":
    store = PortfolioStore()
    for p in store.holding():
        print(f"  [sim] {p.name}({p.symbol}): {p.shares}股 @{p.cost_price} 现{p.last_price} — {p.note}")
    for p in store.all("real"):
        print(f"  [real] {p.name}({p.symbol}): {p.shares}股 @{p.cost_price} 现{p.last_price} — {p.note}")
