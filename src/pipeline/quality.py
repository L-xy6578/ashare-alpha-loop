"""
三角闭环选股体系 — 数据质量检查

对 market.db 中某标的的 daily_bars 做一致性校验，输出质量等级：

检查项：
    1. OHLC 一致性：high >= max(open, close)，low <= min(open, close)
    2. 价格有效性：open/close/high/low 均为正数
    3. 缺失交易日：相邻记录日期跳跃（跳过交易日需在允许范围）
    4. 最大单日跳空：相邻收盘价变化率（涨停/跌停极端值提醒）

等级：
    A 级：无错误，缺失合理
    B 级：轻微警告（如少量缺失日）
    C 级：OHLC 错误或大跳空，需人工复核
    D 级：数据严重不可用（大量缺失/大量错误）
"""

from __future__ import annotations

from dataclasses import dataclass

from src.db import get_connection
from src.logger import get_logger

from .schema import init_market_db

log = get_logger(__name__)


@dataclass
class QualityReport:
    """单个标的的质量检查报告"""

    symbol: str
    total_rows: int = 0
    missing_days: int = 0
    ohlc_errors: int = 0
    zero_price: int = 0
    max_gap_pct: float = 0.0
    grade: str = "A"
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "total_rows": self.total_rows,
            "missing_days": self.missing_days,
            "ohlc_errors": self.ohlc_errors,
            "zero_price": self.zero_price,
            "max_gap_pct": round(self.max_gap_pct, 4),
            "grade": self.grade,
            "detail": self.detail,
        }


def _grade(report: QualityReport) -> str:
    """等级判定；20% 涨跌幅板块（创业板 30x/科创板 688）跳空阈值放宽（#91）"""
    if report.total_rows == 0:
        return "D"
    if report.ohlc_errors > 0 or report.zero_price > 0:
        return "C" if report.ohlc_errors <= 3 else "D"
    gap_limit = 0.21 if _is_gem(report.symbol) else 0.11
    if report.missing_days > 10 or report.max_gap_pct > gap_limit:
        return "C"
    if report.missing_days > 3:
        return "B"
    return "A"


def _is_gem(symbol: str) -> bool:
    """创业板(30xxxx)/科创板(688xxx) 涨跌停 ±20%"""
    code = symbol[2:] if symbol[:2] in ("sh", "sz", "bj") else symbol
    return code.startswith("30") or code.startswith("688")


def check_data_quality(symbol: str, db_path: str | None = None) -> QualityReport:
    """检查某标的数据质量

    Args:
        symbol: 标的代码
        db_path: market.db 路径，None 用 config.MARKET_DB_PATH
    """
    from src.config import MARKET_DB_PATH

    path = db_path or MARKET_DB_PATH
    report = QualityReport(symbol=symbol)

    # 确保表结构存在（用写连接；空库时 readonly 无法建表）
    with get_connection(path) as conn:
        init_market_db(conn)

    with get_connection(path, readonly=True) as conn:
        rows = conn.execute(
            """SELECT date, open, close, high, low FROM daily_bars
               WHERE symbol = ? ORDER BY date""",
            (symbol,),
        ).fetchall()

    report.total_rows = len(rows)
    if report.total_rows == 0:
        report.grade = "D"
        report.detail = "无数据"
        log.warning("质量检查 %s: 无数据", symbol)
        return report

    # OHLC 一致性 + 价格有效性 + 跳空
    prev_close = None
    for r in rows:
        o, c, h, l = r["open"], r["close"], r["high"], r["low"]
        if min(o, c, h, l) <= 0:
            report.zero_price += 1
        if h < max(o, c) - 1e-9 or l > min(o, c) + 1e-9:
            report.ohlc_errors += 1
        if prev_close is not None and prev_close > 0:
            gap = abs(c - prev_close) / prev_close
            if gap > report.max_gap_pct:
                report.max_gap_pct = gap
        prev_close = c

    # 缺失交易日：基于交易日历（P0-1，消除节假日误报）
    dates = [r["date"] for r in rows]
    if dates:
        # 先确保日历存在（幂等），再统计真实缺失
        from .calendar import init_trade_calendar, count_missing_trading_days

        init_trade_calendar(path)
        report.missing_days = count_missing_trading_days(
            path, symbol, dates[0], dates[-1])
    else:
        report.missing_days = 0

    report.grade = _grade(report)
    report.detail = (
        f"rows={report.total_rows}, missing={report.missing_days}, "
        f"ohlc_err={report.ohlc_errors}, zero={report.zero_price}, "
        f"max_gap={report.max_gap_pct:.2%}"
    )
    log.info("质量检查 %s: %s 级 (%s)", symbol, report.grade, report.detail)

    # 写快照
    try:
        with get_connection(path) as conn:
            init_market_db(conn)
            conn.execute(
                """INSERT INTO quality_snapshot
                   (symbol, total_rows, missing_days, ohlc_errors, zero_price,
                    max_gap_pct, grade, detail)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (symbol, report.total_rows, report.missing_days, report.ohlc_errors,
                 report.zero_price, report.max_gap_pct, report.grade, report.detail),
            )
    except Exception:
        log.exception("质量快照写入失败 %s", symbol)

    return report


def _estimate_missing(dates: list[str]) -> int:
    """（已废弃，改用交易日历）保留供兼容"""
    from datetime import date

    missing = 0
    for i in range(1, len(dates)):
        y1, m1, d1 = (int(x) for x in dates[i - 1].split("-"))
        y2, m2, d2 = (int(x) for x in dates[i].split("-"))
        gap = (date(y2, m2, d2) - date(y1, m1, d1)).days
        if gap > 4:  # 超过周末（3天）+1缓冲
            missing += gap - 3
    return missing
