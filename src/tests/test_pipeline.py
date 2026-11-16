"""
三角闭环选股体系 — 数据管道测试

运行:
    pytest src/tests/test_pipeline.py -v

覆盖:
    - schema 建表（幂等）
    - Collector 增量采集（首次全量、二次增量跳过）
    - upsert 幂等（重复写入不新增行）
    - 强制全量（force=True）
    - 采集日志记录
    - 质量检查（OHLC 一致性、空库 D 级、正常数据 A 级）
"""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.data_source import KlineBar, StaticDataSource
from src.pipeline.collector import Collector, _next_day
from src.pipeline.quality import check_data_quality
from src.pipeline.schema import init_market_db


@pytest.fixture
def market_db():
    """临时 market.db 路径"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    try:
        os.unlink(path)
    except PermissionError:
        pass


def _bars(symbol: str, days: list[str]) -> list[KlineBar]:
    # 平稳价格（10 附近，±0.5 波动），避免被判定为大跳空
    return [
        KlineBar(d, 10.0 + i * 0.1, 10.0 + i * 0.1, 10.2 + i * 0.1, 9.8 + i * 0.1)
        for i, d in enumerate(days)
    ]


def _make_source(symbol: str, bars: list[KlineBar]) -> StaticDataSource:
    src = StaticDataSource()
    src.set_symbol(symbol, bars)
    return src


# ============================================================
# schema
# ============================================================

class TestSchema:
    def test_init_idempotent(self, market_db):
        from src.db import get_connection

        with get_connection(market_db) as conn:
            init_market_db(conn)
        # 再执行一次不报错
        with get_connection(market_db) as conn:
            init_market_db(conn)
        with get_connection(market_db, readonly=True) as conn:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        assert "daily_bars" in tables
        assert "collect_log" in tables
        assert "quality_snapshot" in tables


# ============================================================
# Collector
# ============================================================

class TestCollector:
    def test_first_collect_full_range(self, market_db):
        src = _make_source("sh600535", _bars("sh600535", ["2026-07-28", "2026-07-29", "2026-07-30"]))
        col = Collector(src, db_path=market_db)
        res = col.collect("sh600535")
        assert res.status == "ok"
        assert res.upserted == 3
        # 库内应有 3 行
        from src.db import get_connection
        with get_connection(market_db, readonly=True) as conn:
            n = conn.execute("SELECT COUNT(*) AS c FROM daily_bars WHERE symbol='sh600535'").fetchone()["c"]
        assert n == 3

    def test_incremental_skip_existing(self, market_db):
        """二次采集应只拉增量（静态源只返回原数据，但 upsert 不重复）"""
        bars = _bars("sh600535", ["2026-07-28", "2026-07-29", "2026-07-30"])
        src = _make_source("sh600535", bars)
        col = Collector(src, db_path=market_db)
        col.collect("sh600535")
        res2 = col.collect("sh600535")  # 静态源仍返回 3 根，但 upsert 幂等
        assert res2.status == "ok"
        from src.db import get_connection
        with get_connection(market_db, readonly=True) as conn:
            n = conn.execute("SELECT COUNT(*) AS c FROM daily_bars WHERE symbol='sh600535'").fetchone()["c"]
        assert n == 3  # 不重复

    def test_upsert_updates_in_place(self, market_db):
        """同一天重复写入应更新而非新增"""
        bars1 = _bars("sh600019", ["2026-07-29"])
        src = _make_source("sh600019", bars1)
        col = Collector(src, db_path=market_db)
        col.collect("sh600019")

        bars2 = [KlineBar("2026-07-29", 99.0, 99.0, 99.0, 99.0)]
        src2 = _make_source("sh600019", bars2)
        col2 = Collector(src2, db_path=market_db)
        col2.collect("sh600019", force=True)

        from src.db import get_connection
        with get_connection(market_db, readonly=True) as conn:
            row = conn.execute(
                "SELECT close FROM daily_bars WHERE symbol='sh600019' AND date='2026-07-29'"
            ).fetchone()
            n = conn.execute("SELECT COUNT(*) AS c FROM daily_bars WHERE symbol='sh600019'").fetchone()["c"]
        assert row["close"] == 99.0
        assert n == 1

    def test_collect_log_written(self, market_db):
        src = _make_source("sh600535", _bars("sh600535", ["2026-07-28"]))
        col = Collector(src, db_path=market_db)
        col.collect("sh600535")
        from src.db import get_connection
        with get_connection(market_db, readonly=True) as conn:
            log_row = conn.execute("SELECT * FROM collect_log ORDER BY id DESC LIMIT 1").fetchone()
        assert log_row["symbol"] == "sh600535"
        assert log_row["rows_upserted"] == 1
        assert log_row["status"] == "ok"

    def test_error_handling(self, market_db):
        """数据源抛异常时应返回 error 状态而非崩溃"""
        class BadSource:
            def daily_kline(self, symbol, start=None, end=None):
                raise RuntimeError("network down")

        col = Collector(BadSource(), db_path=market_db)
        res = col.collect("sh600535")
        assert res.status == "error"
        assert "network down" in res.message

    def test_next_day(self):
        assert _next_day("2026-07-31") == "2026-08-01"
        assert _next_day("2026-12-31") == "2027-01-01"


# ============================================================
# Quality
# ============================================================

class TestQuality:
    def test_empty_db_grade_d(self, market_db):
        rep = check_data_quality("sh600535", db_path=market_db)
        assert rep.grade == "D"
        assert rep.total_rows == 0

    def test_good_data_grade_a(self, market_db):
        src = _make_source("sh600535", _bars("sh600535", ["2026-07-28", "2026-07-29"]))
        Collector(src, db_path=market_db).collect("sh600535")
        rep = check_data_quality("sh600535", db_path=market_db)
        assert rep.grade == "A"
        assert rep.ohlc_errors == 0
        assert rep.total_rows == 2

    def test_ohlc_error_detected(self, market_db):
        """high < close 时应检出 OHLC 错误 → C 级"""
        bad = [KlineBar("2026-07-28", 10.0, 12.0, 11.0, 9.0)]  # high=11 < close=12
        src = _make_source("sh600535", bad)
        Collector(src, db_path=market_db).collect("sh600535")
        rep = check_data_quality("sh600535", db_path=market_db)
        assert rep.ohlc_errors >= 1
        assert rep.grade in ("C", "D")

    def test_quality_snapshot_written(self, market_db):
        src = _make_source("sh600535", _bars("sh600535", ["2026-07-28"]))
        Collector(src, db_path=market_db).collect("sh600535")
        check_data_quality("sh600535", db_path=market_db)
        from src.db import get_connection
        with get_connection(market_db, readonly=True) as conn:
            n = conn.execute("SELECT COUNT(*) AS c FROM quality_snapshot").fetchone()["c"]
        assert n >= 1

    def test_adjust_column_migration_and_backfill(self, market_db):
        """#84：旧库自动补 adjust 列并按 rows_source 回填口径"""
        from src.db import get_connection

        with get_connection(market_db) as conn:
            conn.execute(
                """CREATE TABLE daily_bars (
                    symbol TEXT NOT NULL, date TEXT NOT NULL,
                    open REAL NOT NULL, close REAL NOT NULL,
                    high REAL NOT NULL, low REAL NOT NULL,
                    volume REAL, amount REAL,
                    rows_source TEXT NOT NULL DEFAULT 'unknown',
                    collected_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                    PRIMARY KEY (symbol, date))"""
            )
            conn.execute(
                """INSERT INTO daily_bars (symbol, date, open, close, high, low, rows_source)
                   VALUES ('sh600019','2026-08-17',5.7,5.74,5.8,5.6,'tencenthttp'),
                          ('sz002414','2026-08-03',12.5,12.76,12.9,12.4,'tdx')"""
            )
        with get_connection(market_db) as conn:
            init_market_db(conn)
        with get_connection(market_db, readonly=True) as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(daily_bars)")]
            rows = {r["symbol"]: r["adjust"] for r in conn.execute(
                "SELECT symbol, adjust FROM daily_bars")}
        assert "adjust" in cols
        assert rows["sh600019"] == "qfq"
        assert rows["sz002414"] == "raw"

    def test_collect_writes_adjust(self, market_db):
        """#84：采集落库时记录数据源复权口径"""
        src = _make_source("sh600535", _bars("sh600535", ["2026-07-28"]))
        Collector(src, db_path=market_db).collect("sh600535")
        from src.db import get_connection

        with get_connection(market_db, readonly=True) as conn:
            adj = conn.execute(
                "SELECT adjust FROM daily_bars WHERE symbol='sh600535'").fetchone()[0]
        assert adj == "unknown"   # StaticDataSource 无口径

    def test_sources_expose_adjust(self):
        """#84：数据源暴露复权口径属性"""
        from src.data_source import (AkshareDataSource, TdxDataSource,
                                     TencentHttpDataSource)

        assert TencentHttpDataSource.adjust == "qfq"
        assert TdxDataSource.adjust == "raw"
        assert AkshareDataSource.adjust == "raw"

    def test_gem_large_gap_grade_a(self, market_db):
        """#91：创业板/科创板 ±20%，单日 14% 合法波动不降级"""
        bars = [KlineBar("2026-07-28", 10.0, 10.0, 10.2, 9.8),
                KlineBar("2026-07-29", 11.4, 11.4, 11.6, 11.2)]
        Collector(_make_source("sh688396", bars), db_path=market_db).collect("sh688396")
        rep = check_data_quality("sh688396", db_path=market_db)
        assert rep.max_gap_pct > 0.11
        assert rep.grade == "A"

    def test_non_gem_large_gap_grade_c(self, market_db):
        """#91：主板 ±10%，14% 跳空仍应降 C 级（疑似数据错误）"""
        bars = [KlineBar("2026-07-28", 10.0, 10.0, 10.2, 9.8),
                KlineBar("2026-07-29", 11.4, 11.4, 11.6, 11.2)]
        Collector(_make_source("sh600535", bars), db_path=market_db).collect("sh600535")
        rep = check_data_quality("sh600535", db_path=market_db)
        assert rep.grade == "C"


class TestCliSymbols:
    """#89：pipeline 默认采集集跟随观察池（单一数据源）"""

    def test_default_symbols_indexes_plus_watchlist(self, monkeypatch):
        from src.pipeline import cli as pipeline_cli

        class FakeStore:
            def all(self):
                return [type("W", (), {"symbol": "sh600019"})()]

        monkeypatch.setattr(pipeline_cli, "WatchlistStore", lambda: FakeStore())
        syms = pipeline_cli._default_symbols(with_indexes=True)
        assert syms[:3] == ["sh000001", "sz399001", "sz399006"]
        assert "sh600019" in syms
        assert "sh000300" in syms          # 沪深300 基准纳入每日采集
        assert syms.count("sh000001") == 1

    def test_watchlist_only_no_indexes(self, monkeypatch):
        from src.pipeline import cli as pipeline_cli

        class FakeStore:
            def all(self):
                return [type("W", (), {"symbol": "sh600019"})()]

        monkeypatch.setattr(pipeline_cli, "WatchlistStore", lambda: FakeStore())
        assert pipeline_cli._default_symbols(with_indexes=False) == ["sh600019"]


class TestIndexCollect:
    """沪深300 等指数：无复权，落库 adjust=none / source 带 _index 后缀"""

    def test_is_index_symbol(self):
        from src.data_source import is_index_symbol

        assert is_index_symbol("sh000300") is True
        assert is_index_symbol("sh000001") is True
        assert is_index_symbol("sz399006") is True
        assert is_index_symbol("sh600019") is False
        assert is_index_symbol("sz002366") is False
        assert is_index_symbol("bj830799") is False

    def test_adjust_for_index_is_none(self):
        from src.data_source import TencentHttpDataSource

        src = TencentHttpDataSource()
        assert src.adjust_for("sh600019") == "qfq"
        assert src.adjust_for("sh000300") == "none"

    def test_collector_marks_index_rows(self, market_db):
        src = _make_source("sh000300", _bars("sh000300", ["2026-01-05", "2026-01-06"]))
        c = Collector(src, db_path=market_db)
        res = c.collect("sh000300", start="2026-01-05", end="2026-01-06")
        assert res.upserted == 2
        import sqlite3

        with sqlite3.connect(market_db) as conn:
            rows = conn.execute(
                "SELECT DISTINCT adjust, rows_source FROM daily_bars "
                "WHERE symbol='sh000300'").fetchall()
        assert rows == [("none", "static_index")]   # 指数与个股（qfq/tencenthttp）区分

    def test_stock_collector_keeps_qfq_label(self, market_db):
        src = StaticDataSource()
        src.adjust = "qfq"
        src.set_symbol("sh600019", _bars("sh600019", ["2026-01-05"]))
        c = Collector(src, db_path=market_db)
        c.collect("sh600019", start="2026-01-05", end="2026-01-05")
        import sqlite3

        with sqlite3.connect(market_db) as conn:
            row = conn.execute(
                "SELECT adjust, rows_source FROM daily_bars WHERE symbol='sh600019'"
            ).fetchone()
        assert row == ("qfq", "static")


class TestTdxIndex:
    """#95：通达信指数判定（get_index_bars 与个股走不同接口）"""

    def test_index_codes(self):
        from src.data_source import TdxDataSource

        assert TdxDataSource._is_index("sh000001") is True
        assert TdxDataSource._is_index("sh000688") is True
        assert TdxDataSource._is_index("sz399001") is True
        assert TdxDataSource._is_index("sz399006") is True

    def test_stock_codes(self):
        from src.data_source import TdxDataSource

        assert TdxDataSource._is_index("sh600019") is False
        assert TdxDataSource._is_index("sz002366") is False
        assert TdxDataSource._is_index("bj830799") is False
