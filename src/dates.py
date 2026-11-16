"""
三角闭环选股体系 — 动态日期窗口工具

闭环脚本的日期必须动态化：收盘自动任务每天运行时，
要自动拉"最近 N 个交易日"的真实K线（含当日收盘），不能写死历史日期。
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from .config import MARKET_DB_PATH


def last_trading_day(day: str | None = None,
                     market_db: str | None = MARKET_DB_PATH) -> str:
    """``day``（默认今天）当日或之前最近的交易日（trade_calendar 口径）

    数据新鲜度门禁的"当日 K 线"标准一律用本函数：非交易日手动运行时，
    期望数据日 = 最近交易日，而不是自然日。
    """
    day_iso = day or date.today().isoformat()
    if market_db:
        try:
            with sqlite3.connect(market_db, timeout=5) as conn:
                row = conn.execute(
                    "SELECT MAX(date) FROM trade_calendar "
                    "WHERE is_trading=1 AND date<=?", (day_iso,),
                ).fetchone()
            if row and row[0]:
                return row[0]
        except Exception:
            pass
    # 日历不可用：退化为"该日往前找最近工作日"
    d = date.fromisoformat(day_iso)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()


def recent_window(days_back: int = 20, end: str | None = None,
                  market_db: str | None = MARKET_DB_PATH) -> tuple[str, str]:
    """返回截至 end（默认今天）的最近 days_back 个**交易日**的日期窗口（#112）

    Args:
        days_back: 往前回看的交易日数（含 end 当日；跨周末/长假也保证足够 K 线）
        end: 截止日期 YYYY-MM-DD，None 表示今天
        market_db: 交易日历来源；None 或日历不可用时回退为自然日窗口（留余量）

    Returns:
        (start, end) 均为 YYYY-MM-DD

    说明:
        #112：此前按自然日算，跨 8 天长假后窗口内只剩 9 根 K 线，
        低于技术面因子 10 根门槛 → 全池评分集体缺失。改为按交易日窗口。
    """
    end_date = date.today() if end is None else date.fromisoformat(end)
    end_iso = end_date.isoformat()
    if market_db:
        try:
            with sqlite3.connect(market_db, timeout=5) as conn:
                rows = conn.execute(
                    "SELECT date FROM trade_calendar "
                    "WHERE is_trading=1 AND date<=? ORDER BY date DESC LIMIT ?",
                    (end_iso, days_back),
                ).fetchall()
            if len(rows) == days_back:
                return rows[-1][0], end_iso
        except Exception:
            pass
    return (end_date - timedelta(days=days_back)).isoformat(), end_iso
