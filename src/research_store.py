"""研究库（data/research.db）— 独立于生产行情库的全市场历史仓

用途：把 A 股回测样本从「37 只票、8 个月」扩到「全部沪深主板、近两年」。
硬约束（2026-11-10 用户明确）：
    - **只写 data/research.db，禁止往 data/market.db 的 daily_bars 追加全市场数据**；
      market.db 是生产复算与审计依据，混入 4000 只票会让现有复算链路错乱。
    - 本模块不参与 r47_closed_loop / run_daily 任何生产链路。

口径说明：
    - 个股日K为腾讯前复权（qfq），复权口径随分红送转变化，用于收益率横截面比较是一致口径；
    - 指数 sh000300 无复权，adjust 记 "none"；
    - 板块指数（东财 BK 代码）单独一表，只存收盘价序列，20 日涨幅在回测侧现算。

用法:
    from src.research_store import open_research, init_db, stats
    with open_research() as conn:
        init_db(conn)
        print(stats(conn))
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from src.config import DATA_DIR

RESEARCH_DB_PATH = str(Path(DATA_DIR) / "research.db")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS research_symbols (
    symbol         TEXT PRIMARY KEY,          -- sh600000 / sz000001
    code           TEXT NOT NULL,             -- 6 位代码
    market         TEXT NOT NULL,             -- sh / sz
    name           TEXT,
    industry       TEXT,                      -- 东财行业（f100），当前归属，非历史归属
    is_st          INTEGER NOT NULL DEFAULT 0,
    is_main_board  INTEGER NOT NULL DEFAULT 0,
    bar_count      INTEGER NOT NULL DEFAULT 0,
    first_date     TEXT,
    last_date      TEXT,
    collect_status TEXT NOT NULL DEFAULT '',  -- ok / error:<原因> / pending
    collected_at   TEXT
);

CREATE TABLE IF NOT EXISTS research_bars (
    symbol TEXT NOT NULL,
    date   TEXT NOT NULL,
    open   REAL, close REAL, high REAL, low REAL,
    volume REAL, amount REAL,
    adjust TEXT NOT NULL DEFAULT 'qfq',
    source TEXT NOT NULL DEFAULT 'tencent',
    PRIMARY KEY (symbol, date)
);
CREATE INDEX IF NOT EXISTS idx_rbars_symbol ON research_bars(symbol);

CREATE TABLE IF NOT EXISTS research_boards (
    code       TEXT PRIMARY KEY,              -- BK0475
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL,                 -- plate(行业) / concept(概念)
    bar_count  INTEGER NOT NULL DEFAULT 0,
    first_date TEXT,
    last_date  TEXT,
    status     TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS research_board_bars (
    code  TEXT NOT NULL,
    date  TEXT NOT NULL,
    close REAL NOT NULL,
    PRIMARY KEY (code, date)
);

CREATE TABLE IF NOT EXISTS research_valuation (
    symbol  TEXT NOT NULL,                    -- sh600000
    date    TEXT NOT NULL,                    -- TRADE_DATE（逐日）
    pe_ttm  REAL,                             -- 东财 PE_TTM
    pb_mrq  REAL,                             -- 东财 PB_MRQ
    PRIMARY KEY (symbol, date)
);
CREATE INDEX IF NOT EXISTS idx_rval_symbol ON research_valuation(symbol);

CREATE TABLE IF NOT EXISTS research_earnings (
    symbol    TEXT NOT NULL,
    report_date TEXT NOT NULL,                -- REPORTDATE（报告期）
    notice_date TEXT,                         -- NOTICE_DATE（首次公告日，时点还原用这个）
    update_date TEXT,                         -- UPDATE_DATE（最后修改日；仅作对照，不可当时点）
    sjltz     REAL,                           -- 归母净利同比 %
    parent_netprofit REAL,                    -- 归母净利（元，判亏损用）
    PRIMARY KEY (symbol, report_date)
);
CREATE INDEX IF NOT EXISTS idx_rern_symbol ON research_earnings(symbol);

CREATE TABLE IF NOT EXISTS research_collect_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    stage      TEXT NOT NULL,
    target     TEXT,
    status     TEXT NOT NULL,
    rows       INTEGER NOT NULL DEFAULT 0,
    message    TEXT,
    started_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""


@contextmanager
def open_research(readonly: bool = False):
    """打开研究库连接（WAL、row_factory、自动提交/回滚）"""
    path = RESEARCH_DB_PATH
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=15)
        conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        if not readonly:
            conn.commit()
    except Exception:
        if not readonly:
            conn.rollback()
        raise
    finally:
        conn.close()


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


def upsert_symbols(conn: sqlite3.Connection, rows: list[dict]) -> int:
    conn.executemany(
        """INSERT INTO research_symbols
             (symbol, code, market, name, industry, is_st, is_main_board, collect_status)
           VALUES (:symbol, :code, :market, :name, :industry, :is_st, :is_main_board, 'pending')
           ON CONFLICT(symbol) DO UPDATE SET
             name=excluded.name, industry=excluded.industry, is_st=excluded.is_st,
             is_main_board=excluded.is_main_board""",
        rows,
    )
    return len(rows)


def upsert_bars(conn: sqlite3.Connection, symbol: str, bars, source: str, adjust: str) -> int:
    """写入某标的日K（覆盖式 upsert，不删除历史）"""
    payload = [
        (symbol, b[0], b[1], b[2], b[3], b[4], b[5], b[6], adjust, source)
        for b in bars
    ]
    conn.executemany(
        """INSERT INTO research_bars
             (symbol, date, open, close, high, low, volume, amount, adjust, source)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(symbol, date) DO UPDATE SET
             open=excluded.open, close=excluded.close, high=excluded.high,
             low=excluded.low, volume=excluded.volume, amount=excluded.amount,
             adjust=excluded.adjust, source=excluded.source""",
        payload,
    )
    return len(payload)


def mark_symbol(conn: sqlite3.Connection, symbol: str, status: str,
                bar_count: int, first_date: str, last_date: str) -> None:
    conn.execute(
        """UPDATE research_symbols
              SET collect_status=?, bar_count=?, first_date=?, last_date=?,
                  collected_at=datetime('now','localtime')
            WHERE symbol=?""",
        (status, bar_count, first_date, last_date, symbol),
    )


def upsert_boards(conn: sqlite3.Connection, rows: list[dict]) -> int:
    conn.executemany(
        """INSERT INTO research_boards (code, name, kind)
           VALUES (:code, :name, :kind)
           ON CONFLICT(code) DO UPDATE SET name=excluded.name, kind=excluded.kind""",
        rows,
    )
    return len(rows)


def upsert_board_bars(conn: sqlite3.Connection, code: str, closes) -> int:
    conn.executemany(
        """INSERT INTO research_board_bars (code, date, close) VALUES (?,?,?)
           ON CONFLICT(code, date) DO UPDATE SET close=excluded.close""",
        [(code, d, c) for d, c in closes],
    )
    conn.execute(
        """UPDATE research_boards
              SET bar_count=?, first_date=?, last_date=?, status='ok'
            WHERE code=?""",
        (len(closes), closes[0][0] if closes else None,
         closes[-1][0] if closes else None, code),
    )
    return len(closes)


def upsert_valuation(conn: sqlite3.Connection, symbol: str, rows) -> int:
    """写入逐日估值（date 为 YYYY-MM-DD）"""
    conn.executemany(
        """INSERT INTO research_valuation (symbol, date, pe_ttm, pb_mrq)
           VALUES (?,?,?,?)
           ON CONFLICT(symbol, date) DO UPDATE SET
             pe_ttm=excluded.pe_ttm, pb_mrq=excluded.pb_mrq""",
        [(symbol, d, pe, pb) for d, pe, pb in rows],
    )
    return len(rows)


def upsert_earnings(conn: sqlite3.Connection, symbol: str, rows) -> int:
    """写入业绩报表（report_date 为报告期；同一报告期覆盖式 upsert）"""
    conn.executemany(
        """INSERT INTO research_earnings
             (symbol, report_date, notice_date, update_date, sjltz, parent_netprofit)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(symbol, report_date) DO UPDATE SET
             notice_date=excluded.notice_date, update_date=excluded.update_date,
             sjltz=excluded.sjltz, parent_netprofit=excluded.parent_netprofit""",
        [(symbol, rd, nd, ud, s, np_) for rd, nd, ud, s, np_ in rows],
    )
    return len(rows)


def log_collect(conn: sqlite3.Connection, stage: str, target: str,
                status: str, rows: int = 0, message: str = "") -> None:
    conn.execute(
        """INSERT INTO research_collect_log (stage, target, status, rows, message)
           VALUES (?,?,?,?,?)""",
        (stage, target, status, rows, message),
    )


def stats(conn: sqlite3.Connection) -> dict:
    """研究库规模快照（采集进度用）"""
    q = conn.execute

    def one(sql, default=0):
        v = q(sql).fetchone()
        return v[0] if v and v[0] is not None else default

    return {
        "symbols": one("SELECT COUNT(*) FROM research_symbols"),
        "symbols_ok": one("SELECT COUNT(*) FROM research_symbols WHERE collect_status='ok'"),
        "symbols_error": one("SELECT COUNT(*) FROM research_symbols WHERE collect_status LIKE 'error%'"),
        "bars": one("SELECT COUNT(*) FROM research_bars"),
        "bar_date_min": one("SELECT MIN(date) FROM research_bars", ""),
        "bar_date_max": one("SELECT MAX(date) FROM research_bars", ""),
        "boards": one("SELECT COUNT(*) FROM research_boards"),
        "boards_ok": one("SELECT COUNT(*) FROM research_boards WHERE status='ok'"),
        "board_bars": one("SELECT COUNT(*) FROM research_board_bars"),
        "valuation": one("SELECT COUNT(*) FROM research_valuation"),
        "valuation_dates": one("SELECT COUNT(DISTINCT date) FROM research_valuation"),
        "earnings": one("SELECT COUNT(*) FROM research_earnings"),
    }
