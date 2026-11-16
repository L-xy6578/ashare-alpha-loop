"""
三角闭环选股体系 — Gold 层测试

运行:
    pytest src/tests/test_gold.py -v

覆盖:
    - 构建流程（全量/增量）
    - 指标正确性（涨跌幅/MA/RSI/涨跌停/量比/regime）
    - 幂等性（重复构建不重复）
    - 创业板跌停标记（±20%）
"""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.db import get_connection
from src.data_source import KlineBar, StaticDataSource
from src.gold.builder import GoldBuilder, symbol_is_gem
from src.gold.schema import init_gold_db
from src.pipeline.collector import Collector


@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    # 建 Bronze + Gold 表
    from src.pipeline.schema import init_market_db

    with get_connection(path) as conn:
        init_market_db(conn)
        init_gold_db(conn)
    yield path
    try:
        os.unlink(path)
    except PermissionError:
        pass


def _seed(db, symbol, closes, volumes=None):
    """向 Bronze 写入 K 线"""
    bars = []
    from datetime import date, timedelta

    d = date(2026, 1, 5)
    for i, c in enumerate(closes):
        v = volumes[i] if volumes else 1000.0
        bars.append(KlineBar(d.isoformat(), c, c, round(c * 1.01, 2),
                             round(c * 0.99, 2), volume=v))
        d += timedelta(days=1)
    src = StaticDataSource()
    src.set_symbol(symbol, bars)
    Collector(src, db_path=db).collect(symbol, force=True)


class TestGoldBuilder:
    def test_full_build(self, db):
        closes = [10.0 + i * 0.05 for i in range(70)]
        _seed(db, "sh600535", closes)
        builder = GoldBuilder(db_path=db)
        res = builder.build("sh600535")
        assert res.status == "ok"
        assert res.upserted == 70
        with get_connection(db, readonly=True) as conn:
            n = conn.execute("SELECT COUNT(*) c FROM gold_bars WHERE symbol='sh600535'").fetchone()["c"]
        assert n == 70

    def test_metrics_computed(self, db):
        closes = [10.0 + i * 0.1 for i in range(70)]
        _seed(db, "sh600535", closes)
        GoldBuilder(db_path=db).build("sh600535")
        with get_connection(db, readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM gold_bars WHERE symbol='sh600535' ORDER BY date DESC LIMIT 1"
            ).fetchone()
        # 上涨趋势：pct_chg 为正，ma20 存在，rsi 高
        assert row["pct_chg"] > 0
        assert row["ma20"] is not None
        assert row["rsi14"] > 50
        assert row["limit_up"] == 0

    def test_limit_down_detected(self, db):
        """-10% 应标记跌停（主板）"""
        closes = [10.0] * 20 + [9.0]  # 最后一天 -10%
        _seed(db, "sh600535", closes)
        GoldBuilder(db_path=db).build("sh600535")
        with get_connection(db, readonly=True) as conn:
            row = conn.execute(
                "SELECT limit_down FROM gold_bars WHERE symbol='sh600535' ORDER BY date DESC LIMIT 1"
            ).fetchone()
        assert row["limit_down"] == 1

    def test_gem_limit_up_20pct(self, db):
        """创业板 +20% 才涨停（±20% 规则）"""
        # +10% 在创业板不涨停
        closes = [10.0] * 20 + [11.0]  # +10%
        _seed(db, "sz300001", closes)
        GoldBuilder(db_path=db).build("sz300001")
        with get_connection(db, readonly=True) as conn:
            row = conn.execute(
                "SELECT limit_up FROM gold_bars WHERE symbol='sz300001' ORDER BY date DESC LIMIT 1"
            ).fetchone()
        assert row["limit_up"] == 0  # 创业板 +10% 不算涨停

    def test_symbol_is_gem(self):
        assert symbol_is_gem("sz300001") is True
        assert symbol_is_gem("sh688001") is True
        assert symbol_is_gem("sh600535") is False
        assert symbol_is_gem("sz002366") is False

    def test_incremental_build(self, db):
        """增量构建：已有数据只构建新日期"""
        closes = [10.0] * 70
        _seed(db, "sh600535", closes)
        builder = GoldBuilder(db_path=db)
        builder.build("sh600535")  # 全量 70 行

        # 追加新数据（8-03）
        from datetime import date, timedelta
        d = date(2026, 1, 5) + timedelta(days=70)
        bars = [KlineBar(d.isoformat(), 10.0, 10.0, 10.1, 9.9, volume=1000)]
        src = StaticDataSource()
        src.set_symbol("sh600535", bars)
        Collector(src, db_path=db).collect("sh600535", force=True)

        res = builder.build("sh600535")  # 增量
        assert res.status == "ok"
        with get_connection(db, readonly=True) as conn:
            n = conn.execute("SELECT COUNT(*) c FROM gold_bars WHERE symbol='sh600535'").fetchone()["c"]
        assert n == 71  # 70 + 1 新增

    def test_volume_ratio(self, db):
        """量比 = 当日量 / 前5日均量"""
        closes = [10.0] * 30
        volumes = [1000.0] * 29 + [5000.0]  # 最后一天放量5倍
        _seed(db, "sh600535", closes, volumes)
        GoldBuilder(db_path=db).build("sh600535")
        with get_connection(db, readonly=True) as conn:
            row = conn.execute(
                "SELECT volume_ratio FROM gold_bars WHERE symbol='sh600535' ORDER BY date DESC LIMIT 1"
            ).fetchone()
        assert row["volume_ratio"] == pytest.approx(5.0, abs=0.5)

    def test_regime_recorded(self, db):
        """regime 状态应被记录"""
        closes = [10.0 + i * 0.1 for i in range(70)]
        _seed(db, "sh600535", closes)
        GoldBuilder(db_path=db).build("sh600535")
        with get_connection(db, readonly=True) as conn:
            row = conn.execute(
                "SELECT regime FROM gold_bars WHERE symbol='sh600535' ORDER BY date DESC LIMIT 1"
            ).fetchone()
        assert row["regime"] in ("trend_up", "range", "trend_down", "defensive")
