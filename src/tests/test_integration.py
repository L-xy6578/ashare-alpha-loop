"""
三角闭环整体集成测试 — 数据源 → 模拟引擎 → 知识库 4 环节

验证 B/A 工程化后的完整链路：
1. 数据源接口层：StaticDataSource 注入 07-29~07-31 真实K线（westock-mcp）
2. 模拟引擎：开盘价建仓 + 止盈/止损规则 → 基于真实K线自动计算盈亏
3. 知识库 4 环节：preflight / record / round_summary / cross_check

运行:
    pytest src/tests/test_integration.py -v
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest

from src.data_source import KlineBar, StaticDataSource
from src.knowledge import KnowledgeBase, init_db
from src.simulation import simulate_trade


# ================================================================
# 真实K线数据（westock-mcp · 2026-07-29~07-31）
# ================================================================

REAL_KLINE = {
    "sh600535": [  # 天士力
        KlineBar("2026-07-29", 15.78, 15.95, 16.10, 15.78),
        KlineBar("2026-07-30", 15.91, 15.88, 16.09, 15.70),
        KlineBar("2026-07-31", 15.72, 16.12, 16.13, 15.68),
    ],
    "sh600019": [  # 宝钢股份
        KlineBar("2026-07-29", 5.95, 6.11, 6.12, 5.94),
        KlineBar("2026-07-30", 6.11, 6.26, 6.28, 6.09),
        KlineBar("2026-07-31", 6.17, 6.13, 6.25, 6.12),
    ],
    "sz002414": [  # 高德红外
        KlineBar("2026-07-29", 13.69, 13.91, 13.99, 13.35),
        KlineBar("2026-07-30", 13.65, 12.52, 13.75, 12.52),
        KlineBar("2026-07-31", 12.82, 12.61, 13.03, 12.56),
    ],
    "sz002366": [  # 融发核电
        KlineBar("2026-07-29", 5.67, 5.78, 5.83, 5.55),
        KlineBar("2026-07-30", 5.68, 5.67, 5.84, 5.65),
        KlineBar("2026-07-31", 5.64, 5.74, 5.77, 5.64),
    ],
}


@pytest.fixture
def db():
    """每次测试使用独立临时数据库"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    yield path
    try:
        os.unlink(path)
    except PermissionError:
        pass  # Windows 文件锁，清理失败不影响测试


# ================================================================
# 模拟引擎（实现在 src/simulation.py，此处复用）
# ================================================================


# ================================================================
# 1. 数据源接口层测试
# ================================================================

class TestDataSource:
    def test_static_kline_load_and_filter(self):
        src = StaticDataSource(REAL_KLINE)
        bars = src.daily_kline("sh600535", "2026-07-29", "2026-07-31")
        assert len(bars) == 3
        assert bars[0].date == "2026-07-29"
        assert bars[0].open == 15.78
        # 日期过滤
        bars2 = src.daily_kline("sh600535", start="2026-07-30")
        assert len(bars2) == 2
        assert bars2[0].date == "2026-07-30"

    def test_unknown_symbol_returns_empty(self):
        src = StaticDataSource(REAL_KLINE)
        assert src.daily_kline("sh999999") == []

    def test_set_symbol_incremental(self):
        src = StaticDataSource()
        src.set_symbol("sh600535", REAL_KLINE["sh600535"])
        assert len(src.daily_kline("sh600535")) == 3

    def test_kline_bar_is_frozen(self):
        bar = KlineBar("2026-07-29", 1, 2, 3, 4)
        with pytest.raises(Exception):
            bar.open = 99  # type: ignore[misc]


# ================================================================
# 2. 模拟引擎测试（真实K线 → 盈亏）
# ================================================================

class TestSimulateEngine:
    def test_tianshili_hold_to_end(self):
        """天士力：15.78建仓，止盈16.50/止损15.30均未触发 → 期末16.12 → +2.15%"""
        src = StaticDataSource(REAL_KLINE)
        r = simulate_trade(src, "sh600535", "2026-07-29", take_profit=16.50, stop_loss=15.30)
        assert r["entry_price"] == pytest.approx(15.78)
        assert r["exit_price"] == pytest.approx(16.12)
        assert r["exit_reason"] == "持有至期末"
        assert r["return_pct"] == pytest.approx(2.15, abs=0.01)

    def test_baogang_take_profit(self):
        """宝钢：5.95建仓，止盈6.20 → 07-30 触发止盈 → +4.20%"""
        src = StaticDataSource(REAL_KLINE)
        r = simulate_trade(src, "sh600019", "2026-07-29", take_profit=6.20, stop_loss=5.85)
        assert r["exit_price"] == pytest.approx(6.20)
        assert r["exit_date"] == "2026-07-30"
        assert r["exit_reason"] == "止盈"
        assert r["return_pct"] == pytest.approx(4.20, abs=0.01)

    def test_gaode_observation_correct(self):
        """高德红外：若 13.69 建仓且无止损 → 期末 12.61 → 亏 -7.89%（观望正确）"""
        src = StaticDataSource(REAL_KLINE)
        r = simulate_trade(src, "sz002414", "2026-07-29")
        assert r["return_pct"] < -7
        assert r["exit_reason"] == "持有至期末"


# ================================================================
# 3. 全链路测试（数据源 → 模拟 → 知识库 4 环节）
# ================================================================

class TestFullLoop:
    def test_r45_full_loop(self, db):
        """R45 全链路：空库预检 → 模拟2笔 → 记录 → 复盘 → 交叉验证"""
        src = StaticDataSource(REAL_KLINE)
        kb = KnowledgeBase(db, market_db=tempfile.mkdtemp() + "/no_market.db")

        # 环节1：预检（空库）
        pre = kb.preflight()
        assert pre.total_trades == 0
        assert len(pre.danger_signals) == 0

        # 环节2：模拟引擎计算
        tianshili = simulate_trade(src, "sh600535", "2026-07-29", take_profit=16.50, stop_loss=15.30)
        baogang = simulate_trade(src, "sh600019", "2026-07-29", take_profit=6.20, stop_loss=5.85)

        # 环节2：记录（盈亏路由）
        r1 = kb.record(
            stock_code="600535", stock_name="天士力",
            entry_price=tianshili["entry_price"], exit_price=tianshili["exit_price"],
            buy_reason="竞价高开+委比92%强买", sell_reason=tianshili["exit_reason"],
            strategy_tag="竞价高开强买", round_number="R45", holding_days=3,
        )
        assert r1["book"] == "correct_book"
        assert r1["return_pct"] == pytest.approx(2.15, abs=0.01)

        r2 = kb.record(
            stock_code="600019", stock_name="宝钢股份",
            entry_price=baogang["entry_price"], exit_price=baogang["exit_price"],
            buy_reason="低开+PB0.63破净", sell_reason=baogang["exit_reason"],
            strategy_tag="破净低吸", round_number="R45", holding_days=2,
        )
        assert r2["book"] == "correct_book"
        assert r2["return_pct"] == pytest.approx(4.20, abs=0.01)

        # 环节3：复盘
        report = kb.round_summary("R45")
        assert report.correct_count == 2
        assert report.mistake_count == 0
        assert report.total_return == pytest.approx(6.35, abs=0.05)
        assert "正确 2 笔" in report.summary

        # 环节4：交叉验证
        cc = kb.cross_check("破净低吸")
        assert cc["sample_count"] == 1
        assert cc["historical_return"] == pytest.approx(4.20, abs=0.01)
        assert cc["safe"] is False  # 样本不足

    def test_datasource_driven_engine_consistency(self, db):
        """数据源驱动的模拟结果与人工核验一致（防回归锚点）"""
        src = StaticDataSource(REAL_KLINE)
        # 天士力人工核验：(16.12-15.78)/15.78
        r = simulate_trade(src, "sh600535", "2026-07-29", take_profit=16.50, stop_loss=15.30)
        assert r["return_pct"] == pytest.approx((16.12 - 15.78) / 15.78 * 100, abs=0.01)
