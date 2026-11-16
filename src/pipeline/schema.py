"""
三角闭环选股体系 — 行情数据库表结构（market.db）

独立于知识库 knowledge.db：
    - knowledge.db：交易知识（对题本/错题本）
    - market.db   ：原始行情数据（daily_bars）

设计原则：
    - 幂等 upsert：同一天同一标的重复写入只更新不新增（PRIMARY KEY(symbol, date)）
    - WAL 模式：并发读写友好
    - 可追溯：rows_source 记录数据来源（westock/akshare/tencent）
"""

MARKET_DB_TABLES = """
-- 日K线（核心表）
CREATE TABLE IF NOT EXISTS daily_bars (
    symbol       TEXT    NOT NULL,           -- 标的代码，如 sh600535
    date         TEXT    NOT NULL,           -- 交易日 YYYY-MM-DD
    open         REAL    NOT NULL,
    close        REAL    NOT NULL,
    high         REAL    NOT NULL,
    low          REAL    NOT NULL,
    volume       REAL,                       -- 成交量（手），可选
    amount       REAL,                       -- 成交额（元），可选
    rows_source  TEXT    NOT NULL DEFAULT 'unknown',  -- 数据来源
    adjust       TEXT    NOT NULL DEFAULT 'unknown',  -- 复权口径：qfq=前复权 / raw=不复权（#84）
    collected_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (symbol, date)
);

-- 采集日志（追踪每次采集任务）
CREATE TABLE IF NOT EXISTS collect_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT    NOT NULL,
    start_date   TEXT,
    end_date     TEXT,
    rows_upserted INTEGER NOT NULL DEFAULT 0,
    source       TEXT    NOT NULL DEFAULT 'unknown',
    status       TEXT    NOT NULL DEFAULT 'ok',   -- ok / error
    message      TEXT,
    started_at   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 数据质量快照（每次 quality check 写一行）
CREATE TABLE IF NOT EXISTS quality_snapshot (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT    NOT NULL,
    check_date   TEXT    NOT NULL DEFAULT (date('now')),
    total_rows   INTEGER NOT NULL DEFAULT 0,
    missing_days INTEGER NOT NULL DEFAULT 0,
    ohlc_errors  INTEGER NOT NULL DEFAULT 0,
    zero_price   INTEGER NOT NULL DEFAULT 0,
    max_gap_pct  REAL    NOT NULL DEFAULT 0,   -- 最大单日跳空（相邻收盘）
    grade        TEXT    NOT NULL DEFAULT 'A',  -- A/B/C/D
    detail       TEXT,
    checked_at   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 交易日历（P0-1：消除节假日导致的缺失误报）
-- is_trading: 1=交易日 0=节假日/周末
CREATE TABLE IF NOT EXISTS trade_calendar (
    date         TEXT PRIMARY KEY,           -- YYYY-MM-DD
    is_trading   INTEGER NOT NULL DEFAULT 1, -- 1 交易日 / 0 休市
    note         TEXT                        -- 节假日说明，如 "春节"
);

-- 查询索引（P0-2：symbol 单独查询加速 + symbol+date 覆盖索引）
CREATE INDEX IF NOT EXISTS idx_daily_bars_symbol ON daily_bars(symbol);
CREATE INDEX IF NOT EXISTS idx_daily_bars_symbol_date ON daily_bars(symbol, date);
CREATE INDEX IF NOT EXISTS idx_quality_symbol ON quality_snapshot(symbol, check_date);
CREATE INDEX IF NOT EXISTS idx_collect_symbol ON collect_log(symbol, started_at);
"""


def init_market_db(conn) -> None:
    """初始化行情库表结构（幂等，可重复调用）；旧库自动补 adjust 列并回填口径

    Args:
        conn: sqlite3 连接（来自 src.db.get_connection 或测试临时库）
    """
    conn.executescript(MARKET_DB_TABLES)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(daily_bars)").fetchall()]
    if "adjust" not in cols:
        conn.execute(
            "ALTER TABLE daily_bars ADD COLUMN adjust TEXT NOT NULL DEFAULT 'unknown'"
        )
        conn.execute(
            "UPDATE daily_bars SET adjust='qfq' WHERE rows_source='tencenthttp'"
        )
        conn.execute(
            "UPDATE daily_bars SET adjust='raw' "
            "WHERE rows_source IN ('tdx', 'tdxconnector', 'akshare')"
        )
