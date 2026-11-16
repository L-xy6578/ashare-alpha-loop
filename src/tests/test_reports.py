"""多周期收益测量报表测试"""

from datetime import date

import pytest

from src.db import get_connection
from src.knowledge import CorrectBook, MistakeBook, init_db
from src.pipeline.schema import init_market_db
from src.portfolio import PortfolioStore
from src.reports import (
    aggregate,
    build_report,
    dedupe_trades,
    load_floating,
    load_trades,
    period_ranges,
    render_markdown,
)


class TestPeriodRanges:
    def test_weekly_boundaries(self):
        r = period_ranges(date(2026, 8, 5))["weekly"]  # 周三
        assert r["start"] == "2026-08-03"   # 周一
        assert r["end"] == "2026-08-09"     # 周日
        assert r["prev_start"] == "2026-07-27"
        assert r["prev_end"] == "2026-08-02"

    def test_monthly_boundaries(self):
        r = period_ranges(date(2026, 8, 5))["monthly"]
        assert r["start"] == "2026-08-01"
        assert r["end"] == "2026-08-31"
        assert r["prev_start"] == "2026-07-01"
        assert r["prev_end"] == "2026-07-31"

    def test_quarterly_boundaries(self):
        r = period_ranges(date(2026, 8, 5))["quarterly"]
        assert r["start"] == "2026-07-01"
        assert r["end"] == "2026-09-30"
        assert r["prev_start"] == "2026-04-01"
        assert r["prev_end"] == "2026-06-30"

    def test_quarterly_q4_boundaries(self):
        """#110：Q4（10-12 月）季度计算不得越界（month 13 崩溃）"""
        r = period_ranges(date(2026, 10, 14))["quarterly"]
        assert r["start"] == "2026-10-01"
        assert r["end"] == "2026-12-31"
        assert r["prev_start"] == "2026-07-01"
        assert r["prev_end"] == "2026-09-30"

    def test_yearly_boundaries(self):
        r = period_ranges(date(2026, 8, 5))["yearly"]
        assert r["start"] == "2026-01-01"
        assert r["end"] == "2026-08-05"
        assert r["prev_start"] == "2025-01-01"
        assert r["prev_end"] == "2025-12-31"


class TestAggregate:
    def test_empty(self):
        m = aggregate([], "2026-01-01", "2026-12-31")
        assert m["n"] == 0
        assert m["total_pnl"] == 0.0

    def test_mixed(self):
        trades = [
            {"date": "2026-08-01", "pnl": 10.0, "source": "sim", "book": "correct"},
            {"date": "2026-08-02", "pnl": -5.0, "source": "sim", "book": "mistake"},
            {"date": "2026-08-03", "pnl": 15.0, "source": "sim", "book": "correct"},
        ]
        m = aggregate(trades, "2026-08-01", "2026-08-31")
        assert m["n"] == 3
        assert m["win_rate"] == pytest.approx(66.7, abs=0.1)
        assert m["total_pnl"] == 20.0
        assert m["avg_pnl"] == pytest.approx(6.67, abs=0.01)

    def test_zero_pnl_mistake_not_a_win(self):
        """#56：0% 错题记录不算胜（pnl>=0 旧口径会虚高胜率）"""
        trades = [
            {"date": "2026-08-01", "pnl": 0.0, "source": "sim", "book": "mistake"},
            {"date": "2026-08-02", "pnl": 3.0, "source": "sim", "book": "correct"},
        ]
        m = aggregate(trades, "2026-08-01", "2026-08-31")
        assert m["n"] == 2
        assert m["win_rate"] == 50.0

    def test_dedupe_repeated_trades(self):
        """同（代码, 入场价, 出场价）只保留最早一轮（R45/R46 同价重复）"""
        trades = [
            {"code": "600535", "entry": 15.78, "exit": 16.12, "pnl": 2.15,
             "date": "2026-07-31", "round": "R45"},
            {"code": "600535", "entry": 15.78, "exit": 16.12, "pnl": 2.15,
             "date": "2026-08-03", "round": "R46"},
            {"code": "600019", "entry": 5.95, "exit": 6.11, "pnl": 2.69,
             "date": "2026-08-03", "round": "R47"},
        ]
        deduped, dupes = dedupe_trades(trades)
        assert dupes == 1
        assert len(deduped) == 2
        assert {t["round"] for t in deduped} == {"R45", "R47"}


@pytest.fixture()
def measurement_env(tmp_path):
    kb = str(tmp_path / "kb.db")
    mkt = str(tmp_path / "market.db")
    init_db(kb)

    # 本期（8月）：sim 盈利 +10%
    CorrectBook.create(
        kb, stock_code="600535", stock_name="天士力",
        entry_price=10, exit_price=11, profit_pct=10.0,
        buy_reason="x", sell_reason="y", strategy_tag="测试策略",
        round_number="RT1", source="sim", created_at="2026-08-05 15:00:00",
    )
    # 上期（7月）：sim 亏损 -10%
    MistakeBook.create(
        kb, stock_code="600019", stock_name="宝钢",
        entry_price=10, exit_price=9, loss_pct=-10.0,
        buy_reason="x", loss_root_cause="r", lesson_learned="l",
        mistake_type="测试", round_number="RT0", source="sim",
        created_at="2026-07-20 15:00:00",
    )
    # 上季（Q2）：real 亏损
    MistakeBook.create(
        kb, stock_code="600000", stock_name="示例银行",
        entry_price=10.0, exit_price=8.0, loss_pct=-20.0,
        buy_reason="x", loss_root_cause="r", lesson_learned="l",
        mistake_type="止损不及时", round_number="RREAL", source="real",
        created_at="2026-05-05 09:00:00",
    )

    with get_connection(mkt) as conn:
        init_market_db(conn)
        conn.execute(
            """INSERT INTO daily_bars
               (symbol, date, open, close, high, low, volume, amount, rows_source)
               VALUES ('sh600535','2026-08-04',15.0,16.0,16.5,14.5,1000,160000,'test')"""
        )

    store = PortfolioStore(kb)
    store.set("sh600535", "天士力", 100, 15.0, 15.0)   # sim 持仓，最新收盘 16.0
    return kb, mkt


class TestReport:
    def test_load_trades_both_books(self, measurement_env):
        kb, _ = measurement_env
        trades = load_trades(kb)
        assert len(trades) == 3
        assert {t["source"] for t in trades} == {"sim", "real"}

    def test_floating_uses_latest_close(self, measurement_env):
        kb, mkt = measurement_env
        store = PortfolioStore(kb)
        floats = load_floating(store.all(), mkt)
        assert len(floats) == 1
        assert floats[0]["price"] == 16.0
        assert floats[0]["pnl_pct"] == pytest.approx(6.67, abs=0.01)
        assert floats[0]["pnl_amount"] == pytest.approx(100.0)

    def test_build_report_periods(self, measurement_env):
        kb, mkt = measurement_env
        r = build_report(kb_db=kb, market_db=mkt, today=date(2026, 8, 5))
        # 累计
        assert r["cumulative"]["sim"]["n"] == 2
        assert r["cumulative"]["sim"]["total_pnl"] == 0.0   # +10 -10
        assert r["cumulative"]["real"]["n"] == 1
        # 本周（8/3~8/9）：只有 8/5 的 sim 盈利
        w = r["periods"]["weekly"]["sim"]
        assert w["cur"]["n"] == 1
        assert w["cur"]["total_pnl"] == 10.0
        assert w["prev"]["n"] == 0
        # 上月（7月）：sim 亏损 -10
        m = r["periods"]["monthly"]["sim"]
        assert m["cur"]["n"] == 1 and m["cur"]["total_pnl"] == 10.0
        assert m["prev"]["n"] == 1 and m["prev"]["total_pnl"] == -10.0
        # 上季（Q2）：real
        q = r["periods"]["quarterly"]["real"]
        assert q["prev"]["n"] == 1
        assert q["prev"]["total_pnl"] == pytest.approx(-20.0)
        # 策略贡献
        assert any(s["tag"] == "测试策略" for s in r["strategies"]["sim"])

    def test_render_markdown(self, measurement_env):
        kb, mkt = measurement_env
        r = build_report(kb_db=kb, market_db=mkt, today=date(2026, 8, 5))
        md = render_markdown(r)
        assert "收益测量报表" in md
        assert "多周期已实现收益（模拟盘 sim）" in md
        assert "多周期已实现收益（真实盘 real）" in md
        assert "当前持仓浮盈" in md
        assert "策略贡献" in md
