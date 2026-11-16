"""持仓状态表测试（决策连贯性修复）"""

import pytest

from src.portfolio import (
    PortfolioStore,
    Position,
    init_portfolio_db,
)


@pytest.fixture()
def store(tmp_path):
    db = str(tmp_path / "portfolio_test.db")
    init_portfolio_db(db)
    return PortfolioStore(db)


def test_set_and_get(store):
    store.set("sz002366", "融发核电", 250, 5.50, 6.31, "底仓")
    p = store.get("sz002366")
    assert p is not None
    assert p.shares == 250
    assert p.cost_price == 5.50
    assert p.last_price == 6.31


def test_get_none_for_unknown(store):
    assert store.get("sh999999") is None


def test_reduce_partial(store):
    store.set("sz002366", "融发核电", 250, 5.50, 6.31)
    p = store.reduce("sz002366", 100, 6.50, "止盈")
    assert p is not None
    assert p.shares == 150
    assert "减仓100股" in p.note


def test_reduce_to_zero_clear(store):
    store.set("sz002366", "融发核电", 250, 5.50, 6.31)
    p = store.reduce("sz002366", 250, 6.50, "清仓")
    assert p.shares == 0
    assert "清仓" in p.note
    # 清仓后不进入 holding
    assert store.holding() == []


def test_reduce_no_position(store):
    assert store.reduce("sh999999", 100, 1.0) is None


def test_holding_filters_zero(store):
    store.set("sz002366", "融发核电", 0, 5.50, None, "已清仓")
    store.set("sh600535", "天士力", 600, 15.78, 15.90)
    holdings = store.holding()
    assert len(holdings) == 1
    assert holdings[0].symbol == "sh600535"


def test_set_overwrites(store):
    store.set("sz002366", "融发核电", 250, 5.50)
    store.set("sz002366", "融发核电", 500, 5.60)
    p = store.get("sz002366")
    assert p.shares == 500
    assert p.cost_price == 5.60


def test_position_pct(store):
    store.set("sh600535", "天士力", 600, 15.78, 15.90)   # 9540 元
    store.set("sh600019", "宝钢股份", 1600, 5.95, 6.11)  # 9776 元
    # 总市值 ≈ 19316，总资产 30000 → 64.4%
    pct = store.position_pct(30000)
    assert pct == pytest.approx(19316 / 30000 * 100, abs=0.1)


def test_total_market_value(store):
    store.set("sh600535", "天士力", 600, 15.78, 15.90)
    store.set("sh600019", "宝钢股份", 1600, 5.95, 6.11)
    mv = store.total_market_value()
    assert mv == pytest.approx(600 * 15.90 + 1600 * 6.11, abs=0.01)


def test_dataclass_type(store):
    store.set("sh600535", "天士力", 600, 15.78)
    assert isinstance(store.get("sh600535"), Position)


def test_account_separation(store):
    """模拟盘与真实持仓同标的共存，互不覆盖"""
    store.set("sh600000", "示例银行", 125, 5.75, 6.31, "模拟底仓", account="sim")
    store.set("sh600000", "示例银行", 0, 10.0, 8.0, "真实已清仓", account="real")
    sim = store.get("sh600000")
    real = store.get("sh600000", account="real")
    assert sim is not None and sim.shares == 125
    assert real is not None and real.shares == 0
    assert real.cost_price == 10.0
    # 默认 holding 只含模拟盘；real 账户 0 股不进入 holding
    assert [p.symbol for p in store.holding()] == ["sh600000"]
    assert store.holding("real") == []
    assert len(store.all()) == 2


def test_set_preserves_entry_date_when_not_given(store):
    """#82：set 未传 entry_date 时保留原建仓日（加仓/减仓不覆盖）"""
    store.set("sz002366", "融发核电", 100, 5.0, 5.0, "建仓",
              account="real", entry_date="2026-07-01")
    store.set("sz002366", "融发核电", 200, 5.2, 5.2, "加仓", account="real")
    p = store.get("sz002366", account="real")
    assert p.shares == 200
    assert p.entry_date == "2026-07-01"


def test_legacy_schema_migration(tmp_path):
    """旧表（无 account 列，symbol 主键）自动迁移：存量归入 sim 账户"""
    import sqlite3

    db = str(tmp_path / "legacy.db")
    with sqlite3.connect(db) as conn:
        conn.execute(
            """CREATE TABLE positions (
                symbol TEXT PRIMARY KEY, name TEXT NOT NULL, shares INTEGER NOT NULL,
                cost_price REAL NOT NULL, last_price REAL, note TEXT, updated_at TEXT NOT NULL)"""
        )
        conn.execute(
            """INSERT INTO positions VALUES
               ('sh600535','天士力',600,15.78,15.90,'R45',datetime('now'))"""
        )
    init_portfolio_db(db)
    store = PortfolioStore(db)
    p = store.get("sh600535")
    assert p is not None
    assert p.shares == 600
    assert p.account == "sim"
