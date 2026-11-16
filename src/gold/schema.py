"""
三角闭环选股体系 — Gold 层表结构（技术指标物化）

Gold 层 = 分析就绪资产：从 Bronze(daily_bars) 计算技术指标，物化到 gold_bars。
消费方（可视化/报表/指标分析）直接读 Gold，不再重复计算。

指标列：
    - pct_chg       涨跌幅（相对前一交易日收盘）
    - ma5/ma10/ma20/ma60  均线
    - rsi14         RSI(14)
    - limit_up/limit_down  涨跌停标记（±10% 主板 / ±20% 创业板科创板近似）
    - volume_ratio  量比（当日量 / 前5日均量）
    - regime        市场状态快照（trend_up/trend_down/range/defensive）

设计原则：
    - 主键 (symbol, date)，INSERT OR REPLACE 幂等
    - 指标为 nullable（窗口不足时 NULL），消费方自行处理
"""

GOLD_DB_TABLES = """
-- 技术指标物化表（Gold 层）
CREATE TABLE IF NOT EXISTS gold_bars (
    symbol       TEXT    NOT NULL,
    date         TEXT    NOT NULL,
    open         REAL    NOT NULL,
    close        REAL    NOT NULL,
    high         REAL    NOT NULL,
    low          REAL    NOT NULL,
    volume       REAL,
    pct_chg      REAL,                        -- 涨跌幅（小数，如 0.021 = +2.1%）
    ma5          REAL,
    ma10         REAL,
    ma20         REAL,
    ma60         REAL,
    rsi14        REAL,
    limit_up     INTEGER NOT NULL DEFAULT 0,  -- 1=涨停 0=否
    limit_down   INTEGER NOT NULL DEFAULT 0,  -- 1=跌停 0=否
    volume_ratio REAL,                        -- 量比（当日量/前5日均量）
    regime       TEXT,                        -- 市场状态快照
    computed_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (symbol, date)
);

-- 指标计算日志（追踪每次 Gold 构建）
CREATE TABLE IF NOT EXISTS gold_build_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT    NOT NULL,
    start_date   TEXT,
    end_date     TEXT,
    rows_upserted INTEGER NOT NULL DEFAULT 0,
    status       TEXT    NOT NULL DEFAULT 'ok',   -- ok / error
    message      TEXT,
    built_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 查询索引
CREATE INDEX IF NOT EXISTS idx_gold_symbol ON gold_bars(symbol);
CREATE INDEX IF NOT EXISTS idx_gold_symbol_date ON gold_bars(symbol, date);
"""


def init_gold_db(conn) -> None:
    """初始化 Gold 层表结构（幂等）"""
    conn.executescript(GOLD_DB_TABLES)
