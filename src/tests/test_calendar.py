"""
三角闭环选股体系 — 交易日历测试（P0-1）

运行:
    pytest src/tests/test_calendar.py -v

覆盖:
    - 交易日历初始化（全年365天 / 交易日数量）
    - 周末/节假日标记
    - 真实缺失统计（节假日不应算缺失）
"""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.db import get_connection
from src.data_source import KlineBar
from src.pipeline.calendar import (
    init_trade_calendar, trading_days_between, count_missing_trading_days,
)
from src.pipeline.collector import Collector


@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_trade_calendar(path)
    yield path
    try:
        os.unlink(path)
    except PermissionError:
        pass


class TestTradeCalendar:
    def test_total_entries(self, db):
        with get_connection(db, readonly=True) as conn:
            total = conn.execute("SELECT COUNT(*) c FROM trade_calendar").fetchone()["c"]
            trading = conn.execute(
                "SELECT COUNT(*) c FROM trade_calendar WHERE is_trading=1").fetchone()["c"]
        assert total == 365  # 2026 全年
        assert trading < 250  # 交易日应明显少于全年（排除周末+节假日）

    def test_weekend_marked(self, db):
        # 2026-01-03 是周六
        with get_connection(db, readonly=True) as conn:
            r = conn.execute(
                "SELECT is_trading FROM trade_calendar WHERE date='2026-01-03'").fetchone()
        assert r["is_trading"] == 0

    def test_spring_festival_marked(self, db):
        # 2026-02-18 春节休市
        with get_connection(db, readonly=True) as conn:
            r = conn.execute(
                "SELECT is_trading, note FROM trade_calendar WHERE date='2026-02-18'").fetchone()
        assert r["is_trading"] == 0
        assert r["note"] == "春节"

    def test_oct_8_is_trading_day(self, db):
        """#111：2026-10-08 是交易日（国庆节休至 10-07，10-08 开市）"""
        with get_connection(db, readonly=True) as conn:
            r = conn.execute(
                "SELECT is_trading FROM trade_calendar WHERE date='2026-10-08'").fetchone()
        assert r["is_trading"] == 1

    def test_spring_festival_reopens_feb_24(self, db):
        """春节休市 2/15~2/23，2026-02-24（周二）起照常开市（深交所公告）"""
        with get_connection(db, readonly=True) as conn:
            prev = conn.execute(
                "SELECT is_trading, note FROM trade_calendar WHERE date='2026-02-23'").fetchone()
            first = conn.execute(
                "SELECT is_trading FROM trade_calendar WHERE date='2026-02-24'").fetchone()
        assert prev["is_trading"] == 0 and prev["note"] == "春节"
        assert first["is_trading"] == 1

    def test_mid_autumn_sep_25_closed(self, db):
        """中秋休市 9/25（周五）~9/27，9/28（周一）开市（上交所公告）"""
        with get_connection(db, readonly=True) as conn:
            close = conn.execute(
                "SELECT is_trading, note FROM trade_calendar WHERE date='2026-09-25'").fetchone()
            reopen = conn.execute(
                "SELECT is_trading FROM trade_calendar WHERE date='2026-09-28'").fetchone()
        assert close["is_trading"] == 0 and close["note"] == "中秋节"
        assert reopen["is_trading"] == 1

    def test_trading_days_between(self, db):
        # 2026-01-05(周一) ~ 2026-01-09(周五)：5 个交易日
        n = trading_days_between(db, "2026-01-05", "2026-01-09")
        assert n == 5

    def test_trading_days_span_weekend(self, db):
        # 2026-01-05(周一) ~ 2026-01-12(下周一)：6 个交易日（周末排除）
        n = trading_days_between(db, "2026-01-05", "2026-01-12")
        assert n == 6


class TestMissingDetection:
    def _collect(self, db, symbol, bars):
        src = type("S", (), {"daily_kline": lambda self, s, start=None, end=None: bars})()
        Collector(src, db_path=db).collect(symbol, force=True)

    def test_full_week_no_missing(self, db):
        """完整周数据 → 0 缺失"""
        bars = [
            KlineBar("2026-01-05", 10, 10, 10.2, 9.8),
            KlineBar("2026-01-06", 10, 10, 10.2, 9.8),
            KlineBar("2026-01-07", 10, 10, 10.2, 9.8),
            KlineBar("2026-01-08", 10, 10, 10.2, 9.8),
            KlineBar("2026-01-09", 10, 10, 10.2, 9.8),
        ]
        self._collect(db, "sh600535", bars)
        missing = count_missing_trading_days(db, "sh600535", "2026-01-05", "2026-01-09")
        assert missing == 0

    def test_weekend_not_counted(self, db):
        """缺周六周日数据 → 0 缺失（周末非交易日）"""
        bars = [
            KlineBar("2026-01-05", 10, 10, 10.2, 9.8),  # 周一
            KlineBar("2026-01-06", 10, 10, 10.2, 9.8),  # 周二
            KlineBar("2026-01-07", 10, 10, 10.2, 9.8),  # 周三
            KlineBar("2026-01-08", 10, 10, 10.2, 9.8),  # 周四
            # 缺 01-09 周五
        ]
        self._collect(db, "sh600535", bars)
        missing = count_missing_trading_days(db, "sh600535", "2026-01-05", "2026-01-09")
        assert missing == 1  # 只缺周五，周六日不算

    def test_spring_festival_not_counted(self, db):
        """春节假期缺口 → 0 缺失（节假日前后的数据完整）"""
        # 春节前最后交易日 02-13(周五)，节后 02-24(周二)
        bars = [
            KlineBar("2026-02-13", 10, 10, 10.2, 9.8),
            KlineBar("2026-02-24", 10, 10, 10.2, 9.8),
            KlineBar("2026-02-25", 10, 10, 10.2, 9.8),
            KlineBar("2026-02-26", 10, 10, 10.2, 9.8),
            KlineBar("2026-02-27", 10, 10, 10.2, 9.8),
        ]
        self._collect(db, "sh600535", bars)
        missing = count_missing_trading_days(db, "sh600535", "2026-02-13", "2026-02-27")
        # 区间交易日：02-13 + 02-24~27 = 5 天，数据 5 天 → 0 缺失
        assert missing == 0

    def test_actual_missing_detected(self, db):
        """真正的交易日缺口应检出"""
        bars = [
            KlineBar("2026-01-05", 10, 10, 10.2, 9.8),
            KlineBar("2026-01-06", 10, 10, 10.2, 9.8),
            KlineBar("2026-01-07", 10, 10, 10.2, 9.8),
            KlineBar("2026-01-09", 10, 10, 10.2, 9.8),  # 缺 01-08 周四
        ]
        self._collect(db, "sh600535", bars)
        missing = count_missing_trading_days(db, "sh600535", "2026-01-05", "2026-01-09")
        assert missing == 1
