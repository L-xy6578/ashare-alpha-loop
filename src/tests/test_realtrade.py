"""真实交易记录（real 口径）测试"""

import sqlite3

import pytest

from src.portfolio import PortfolioStore


def test_real_buy_gates_rejects_no_research(tmp_path):
    """调研库无业绩证据 → 真实开仓被拒绝（#9）"""
    from src.data_source import KlineBar, StaticDataSource
    from src.realtrade import real_buy_gates
    from src.research import ResearchStore, init_research_db

    db = str(tmp_path / "r.db")
    init_research_db(db)
    src = StaticDataSource()
    src.set_symbol("sz002202", [KlineBar("2026-08-04", 19.0, 19.5, 20.0, 18.5)])
    ok, reasons = real_buy_gates(
        "sz002202", "002202", "金风科技", 19.5, 100,
        research_store=ResearchStore(db), source=src,
        valuation_provider=lambda s: {"pe": 20, "pb": 2},
    )
    assert ok is False
    assert any("调研库无业绩证据" in r for r in reasons)


def test_real_buy_gates_rejects_position_cap(tmp_path):
    """单票市值超过风险预算 → 拒绝（#8）"""
    from src.data_source import KlineBar, StaticDataSource
    from src.realtrade import real_buy_gates
    from src.research import ResearchStore, init_research_db

    db = str(tmp_path / "r2.db")
    init_research_db(db)
    src = StaticDataSource()
    src.set_symbol("sh600000", [KlineBar("2026-08-04", 10.0, 10.0, 10.2, 9.8)])
    ok, reasons = real_buy_gates(
        "sh600000", "600000", "示例银行", 10.0, 3000,
        research_store=ResearchStore(db), source=src,
        valuation_provider=lambda s: {"pe": 20, "pb": 2},
    )
    assert ok is False
    assert any("单票市值" in r for r in reasons)


def test_real_buy_gates_rejects_aggregate_cap(tmp_path):
    """#85：已有 real 持仓 + 本次买入合计市值超 20% → 拒绝"""
    from src.data_source import KlineBar, StaticDataSource
    from src.realtrade import real_buy_gates
    from src.research import ResearchStore, init_research_db

    db = str(tmp_path / "r3.db")
    init_research_db(db)
    store = PortfolioStore(str(tmp_path / "p3.db"))
    # 现有 1500 股@15（市值 22500）+ 本次 300 股@15（4500）= 27000 > 20000（20%×100000）
    store.set("sh600000", "示例银行", 1500, 15.0, 15.0, "已有持仓", account="real")
    src = StaticDataSource()
    src.set_symbol("sh600000", [KlineBar("2026-08-18", 15.0, 15.0, 15.5, 14.8)])
    ok, reasons = real_buy_gates(
        "sh600000", "600000", "示例银行", 15.0, 300,
        research_store=ResearchStore(db), source=src,
        valuation_provider=lambda s: {"pe": 20, "pb": 2},
        store=store,
    )
    assert ok is False
    assert any("加仓后单票市值" in r for r in reasons)


def test_real_buy_accumulates_weighted_cost(tmp_path):
    """#85：二次买入累计股数 + 加权平均成本，不覆盖原持仓"""
    from src.realtrade import real_buy

    store = PortfolioStore(str(tmp_path / "p4.db"))
    store.set("sh600000", "示例银行", 100, 10.0, 10.0, "已有持仓",
              account="real", entry_date="2026-07-01")
    real_buy(store, "sh600000", "示例银行", 12.0, 100, "2026-08-18", "加仓")
    p = store.get("sh600000", account="real")
    assert p is not None
    assert p.shares == 200
    assert p.cost_price == pytest.approx((100 * 10.0 + 100 * 12.0) / 200)
    assert p.last_price == 12.0
    assert "加仓" in p.note
    assert p.entry_date == "2026-07-01"   # 加仓保留原建仓日


def test_real_buy_creates_new_position(tmp_path):
    """#85：无持仓时正常新建"""
    from src.realtrade import real_buy

    store = PortfolioStore(str(tmp_path / "p5.db"))
    real_buy(store, "sh600019", "宝钢股份", 5.95, 100, "2026-08-18")
    p = store.get("sh600019", account="real")
    assert p is not None
    assert p.shares == 100
    assert p.cost_price == 5.95
    assert p.entry_date == "2026-08-18"


def test_real_stop_loss_alerts(tmp_path):
    """真实持仓 -7% 硬止损告警（#10）"""
    from src.realtrade import real_stop_loss_alerts

    store = PortfolioStore(str(tmp_path / "p.db"))
    store.set("sh600000", "示例银行", 700, 10.0, 7.0, "真实持仓", account="real")
    mkt = str(tmp_path / "m.db")
    with sqlite3.connect(mkt) as conn:
        from src.pipeline.schema import init_market_db

        init_market_db(conn)
        conn.execute(
            """INSERT INTO daily_bars
               (symbol, date, open, close, high, low, volume, amount, rows_source)
               VALUES ('sh600000','2026-08-05',7.1,7.0,7.2,6.9,0,0,'test')"""
        )
    alerts = real_stop_loss_alerts(store=store, market_db=mkt)
    assert len(alerts) == 1
    assert alerts[0]["loss_pct"] == pytest.approx(-30.0, abs=0.1)
    assert "硬止损" in alerts[0]["action"]
