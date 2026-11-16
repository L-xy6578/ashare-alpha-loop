"""
三角闭环选股体系 — 交易日历（P0-1）

解决审计发现的"节假日被误判为数据缺失"问题：
    - 内置 2026 年中国 A 股法定节假日（周末 + 休市日自动排除）
    - 交易日历写入 market.db trade_calendar 表
    - 质量检查基于交易日历统计真实缺失，而非"日期间隔>4天"粗判

数据来源：
    - 内置节假日表（春节/清明/五一/端午/中秋/国庆 + 调休）
    - 未覆盖的年份可扩展 HOLIDAYS 字典

用法:
    from src.pipeline.calendar import init_trade_calendar, count_missing_trading_days
    init_trade_calendar(db_path)                     # 建日历表并写入
    missing = count_missing_trading_days(db_path, symbol, start, end)
"""

from __future__ import annotations

from datetime import date, timedelta

from src.db import get_connection
from src.logger import get_logger
from src.pipeline.schema import init_market_db

log = get_logger(__name__)

# ============================================================
# 2026 中国 A 股休市日（法定节假日 + 调休补班但休市）
# 格式: {"YYYY-MM-DD": "节假日名称"}
# ============================================================
HOLIDAYS_2026: dict[str, str] = {
    # 元旦（1/1 周四休市；1/2 周五休市；1/5 周一为节后首个交易日，非休市）
    "2026-01-01": "元旦",
    "2026-01-02": "元旦",
    # 春节（2/15 除夕 ~ 2/23，休市 9 天；2/24 周二起照常开市，非休市）
    "2026-02-16": "春节", "2026-02-17": "春节", "2026-02-18": "春节",
    "2026-02-19": "春节", "2026-02-20": "春节", "2026-02-23": "春节",
    # 清明节（4/5 周日，休市）
    "2026-04-06": "清明节",
    # 劳动节（5/1 周五 ~ 5/5 周二，连休 5 天）
    "2026-05-01": "劳动节",
    "2026-05-04": "劳动节",
    "2026-05-05": "劳动节",
    # 端午节（6/19 周五，休市）
    "2026-06-19": "端午节",
    # 中秋节+国庆节（中秋 9/25 周五 ~ 9/27 周日休市，9/28 周一开市；
    # 国庆 10/1 ~ 10/7；10-08 为交易日，见 #111）
    "2026-09-25": "中秋节",
    "2026-10-01": "国庆节", "2026-10-02": "国庆节",
    "2026-10-05": "国庆节", "2026-10-06": "国庆节",
    "2026-10-07": "国庆节",
}

# 2026 全年日期范围（供生成日历用）
_YEAR = 2026


def _build_calendar_entries(year: int = _YEAR) -> list[tuple[str, int, str]]:
    """生成一年的日历条目（date, is_trading, note）

    规则：
        - 周末（六/日）→ 休市
        - 节假日表内日期 → 休市（含名称）
        - 其余 → 交易日
    """
    entries: list[tuple[str, int, str]] = []
    d = date(year, 1, 1)
    while d.year == year:
        iso = d.isoformat()
        if d.weekday() >= 5:  # 周六(5) 周日(6)
            entries.append((iso, 0, "周末"))
        elif iso in HOLIDAYS_2026:
            entries.append((iso, 0, HOLIDAYS_2026[iso]))
        else:
            entries.append((iso, 1, ""))
        d += timedelta(days=1)
    return entries


def init_trade_calendar(db_path: str, year: int = _YEAR) -> int:
    """初始化交易日历表并写入全年数据（幂等，INSERT OR REPLACE）

    Returns:
        写入/更新的行数
    """
    entries = _build_calendar_entries(year)
    with get_connection(db_path) as conn:
        init_market_db(conn)
        conn.executemany(
            """INSERT OR REPLACE INTO trade_calendar (date, is_trading, note)
               VALUES (?, ?, ?)""",
            entries,
        )
    log.info("交易日历已初始化 %d 条（%d年）", len(entries), year)
    return len(entries)


def trading_days_between(db_path: str, start: str, end: str) -> int:
    """统计 [start, end] 区间内的交易日数量（基于日历表）"""
    with get_connection(db_path, readonly=True) as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS c FROM trade_calendar
               WHERE is_trading = 1 AND date >= ? AND date <= ?""",
            (start, end),
        ).fetchone()
        return row["c"] if row else 0


def count_missing_trading_days(db_path: str, symbol: str,
                               start: str, end: str) -> int:
    """计算某标的在 [start, end] 区间内的真实缺失交易日数

    真实缺失 = 应有交易日数 - 实际数据行数（在区间内）
    """
    expected = trading_days_between(db_path, start, end)
    with get_connection(db_path, readonly=True) as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS c FROM daily_bars
               WHERE symbol = ? AND date >= ? AND date <= ?""",
            (symbol, start, end),
        ).fetchone()
        actual = row["c"] if row else 0
    missing = max(0, expected - actual)
    if missing:
        log.warning("真实缺失交易日 %s: %d（应有%d 实有%d）", symbol, missing, expected, actual)
    return missing
