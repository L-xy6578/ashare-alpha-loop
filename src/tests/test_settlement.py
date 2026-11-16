"""模拟成交与结算：伏击成交 / 等待 / 3日作废 / 止损 / 分批止盈 / 移动止损 / 隔离"""

import sqlite3

from src.data_source import KlineBar
from src.decision_log import DecisionStore
from src.decision_settle import settle
from src.portfolio import PortfolioStore


def _bar(d, o, c, h, l, v=10000):
    return KlineBar(d, o, c, h, l, volume=v)


def _decision(store, *, decision_date="2026-11-05", zone_low=10.0, zone_high=10.2,
              shares=200, stop_structural=9.0, expire_date="2026-11-10",
              symbol="sh600019", name="宝钢股份", score=80.0):
    row = {
        "decision_date": decision_date, "data_date": decision_date,
        "symbol": symbol, "code": symbol[2:], "name": name, "sector": "钢铁",
        "score": score, "factors": "{}", "vcp": 1, "contractions": 2,
        "vol_ratio": 0.3, "depths": "[10,5]", "box_high": zone_high,
        "box_low": zone_low, "sector_pct": 60.0, "decision": "ENTRY",
        "reason": "", "zone_low": zone_low, "zone_high": zone_high,
        "shares": shares, "amount": round(shares * zone_high, 2),
        "stop_price": stop_structural, "stop_structural": stop_structural,
        "tp1": round(zone_high * 1.08, 2), "tp2": round(zone_high * 1.15, 2),
        "expire_date": expire_date, "snapshot_date": decision_date,
        "snapshot_complete": 1, "git_head": "test", "git_dirty": 0,
        "status": "PENDING", "created_at": "2026-11-05 15:35:00",
    }
    store.save_rows([row])


def _stores(tmp_path):
    kb = str(tmp_path / "knowledge.db")
    return DecisionStore(kb), PortfolioStore(kb)


def test_fill_when_price_enters_zone(tmp_path):
    store, pf = _stores(tmp_path)
    _decision(store)
    klines = {"sh600019": [_bar("2026-11-06", 10.1, 10.3, 10.4, 9.95)]}
    result = settle(store, pf, klines, "2026-11-06")
    assert len(result.buys) == 1
    assert result.buys[0]["price"] == 10.1
    pos = pf.get("sh600019")
    assert pos.shares == 200 and pos.account == "sim"
    plan = store.get_plan("sh600019")
    assert plan["status"] == "OPEN" and plan["shares_remaining"] == 200
    assert store.rows_by_date("2026-11-05")[0]["status"] == "FILLED"


def test_limit_fill_at_zone_high_when_low_touches(tmp_path):
    store, pf = _stores(tmp_path)
    _decision(store)
    klines = {"sh600019": [_bar("2026-11-06", 10.5, 10.4, 10.6, 10.15)]}
    result = settle(store, pf, klines, "2026-11-06")
    assert result.buys[0]["price"] == 10.2   # 开盘高于区间，盘中回踩到上沿


def test_wait_when_not_entered(tmp_path):
    store, pf = _stores(tmp_path)
    _decision(store, expire_date="2026-11-20")
    klines = {"sh600019": [_bar("2026-11-06", 10.6, 10.7, 10.8, 10.5)]}
    result = settle(store, pf, klines, "2026-11-06")
    assert not result.buys
    assert store.get_plan("sh600019") is None
    assert store.rows_by_date("2026-11-05")[0]["status"] == "PENDING"
    assert any("继续等待" in x for x in result.skipped)


def test_expire_after_three_trading_days(tmp_path):
    store, pf = _stores(tmp_path)
    _decision(store, expire_date="2026-11-06")
    klines = {"sh600019": [_bar("2026-11-06", 10.6, 10.7, 10.8, 10.5)]}
    result = settle(store, pf, klines, "2026-11-06")
    assert result.expired
    assert store.rows_by_date("2026-11-05")[0]["status"] == "EXPIRED"


def test_stop_loss_closer_of_two(tmp_path):
    store, pf = _stores(tmp_path)
    _decision(store, zone_low=10.0, zone_high=10.0, stop_structural=9.0, shares=200)
    # 先成交
    settle(store, pf, {"sh600019": [_bar("2026-11-06", 10.0, 10.0, 10.1, 9.9)]},
           "2026-11-06")
    plan = store.get_plan("sh600019")
    # -6% 硬止损 9.40 比结构止损 9.00 更近 → 取 9.40
    assert abs(plan["stop_price"] - 9.4) < 1e-6
    result = settle(store, pf,
                    {"sh600019": [_bar("2026-11-09", 9.5, 9.3, 9.5, 9.25)]},
                    "2026-11-09")
    assert result.sells and result.sells[0]["price"] == 9.4
    assert pf.get("sh600019").shares == 0
    plan = store.get_plan("sh600019")
    assert plan["status"] == "CLOSED"
    assert plan["close_reason"].startswith("止损（")
    assert store.rows_by_date("2026-11-05")[0]["status"] == "CLOSED"


def test_tier_sell_out_sets_closed_with_reason(tmp_path):
    """分批止盈把 200 股卖到 0：plan/log 必须 CLOSED，且写明平仓原因"""
    store, pf = _stores(tmp_path)
    _decision(store, zone_low=10.0, zone_high=10.0, shares=200,
              stop_structural=9.0, expire_date="2026-11-20")
    settle(store, pf, {"sh600019": [_bar("2026-11-06", 10.0, 10.0, 10.1, 9.9)]},
           "2026-11-06")

    day1 = _bar("2026-11-09", 10.5, 10.5, 10.9, 10.4)   # +8% 卖 1/3（100 股）
    settle(store, pf, {"sh600019": [_bar("2026-11-06", 10.0, 10.0, 10.1, 9.9), day1]},
           "2026-11-09")
    assert store.get_plan("sh600019")["shares_remaining"] == 100

    day2 = _bar("2026-11-10", 10.6, 11.5, 11.6, 10.5)   # +15% 再卖 1/3（清空）
    settle(store, pf, {"sh600019": [_bar("2026-11-06", 10.0, 10.0, 10.1, 9.9),
                                    day1, day2]}, "2026-11-10")

    plan = store.get_plan("sh600019")
    assert plan["shares_remaining"] == 0
    assert plan["status"] == "CLOSED"
    assert "分批止盈卖光" in plan["close_reason"]
    assert pf.get("sh600019").shares == 0
    assert store.rows_by_date("2026-11-05")[0]["status"] == "CLOSED"


def test_closed_plan_cannot_be_revived_by_breakout(tmp_path):
    """卖光后次日即使放量突破箱体上沿，也不得再加仓复活"""
    store, pf = _stores(tmp_path)
    _decision(store, zone_low=10.0, zone_high=10.0, shares=200,
              stop_structural=9.0, expire_date="2026-11-20")
    fill = _bar("2026-11-06", 10.0, 10.0, 10.1, 9.9)
    day1 = _bar("2026-11-09", 10.5, 10.5, 10.9, 10.4)
    day2 = _bar("2026-11-10", 10.6, 11.5, 11.6, 10.5)
    settle(store, pf, {"sh600019": [fill]}, "2026-11-06")
    settle(store, pf, {"sh600019": [fill, day1]}, "2026-11-09")
    settle(store, pf, {"sh600019": [fill, day1, day2]}, "2026-11-10")
    assert store.get_plan("sh600019")["status"] == "CLOSED"

    breakout = _bar("2026-11-11", 11.6, 12.2, 12.3, 11.5, v=30000)  # 放量突破 10.0
    result = settle(store, pf, {"sh600019": [fill, day1, day2, breakout]}, "2026-11-11")
    assert not result.buys
    assert pf.get("sh600019").shares == 0
    assert store.get_plan("sh600019")["status"] == "CLOSED"


def test_batch_take_profit_and_trailing(tmp_path):
    store, pf = _stores(tmp_path)
    _decision(store, zone_low=10.0, zone_high=10.0, shares=600,
              stop_structural=9.0, expire_date="2026-11-20")
    # 提供 10 日以上均线所需的收盘序列
    hist = [_bar(f"2026-10-{d:02d}", 10.5, 10.5, 10.6, 10.4) for d in range(26, 31)]
    hist += [_bar(f"2026-11-{d:02d}", 10.5, 10.5, 10.6, 10.4) for d in range(2, 6)]
    fill = _bar("2026-11-06", 10.0, 10.0, 10.1, 9.9)
    day1 = _bar("2026-11-09", 10.5, 10.5, 10.9, 10.4)   # +8% 触发
    day2 = _bar("2026-11-10", 10.6, 11.5, 11.6, 10.5)   # +15% 触发，收盘贴近高点
    klines = {"sh600019": hist + [fill]}
    settle(store, pf, klines, "2026-11-06")
    assert store.get_plan("sh600019")["shares_remaining"] == 600

    r1 = settle(store, pf, {"sh600019": hist + [fill, day1]}, "2026-11-09")
    assert any("+8%" in s["reason"] for s in r1.sells)
    plan = store.get_plan("sh600019")
    assert plan["shares_remaining"] == 400 and plan["tier1_done"] == 1

    r2 = settle(store, pf, {"sh600019": hist + [fill, day1, day2]}, "2026-11-10")
    assert any("+15%" in s["reason"] for s in r2.sells)
    plan = store.get_plan("sh600019")
    assert plan["shares_remaining"] == 200 and plan["tier2_done"] == 1

    drop = _bar("2026-11-11", 10.1, 9.9, 10.1, 9.8)     # 跌破 10 日线
    r3 = settle(store, pf, {"sh600019": hist + [fill, day1, day2, drop]},
                "2026-11-11")
    assert any("移动止损" in s["reason"] for s in r3.sells)
    assert pf.get("sh600019").shares == 0
    assert store.get_plan("sh600019")["status"] == "CLOSED"


def test_dry_run_writes_nothing(tmp_path):
    store, pf = _stores(tmp_path)
    _decision(store)
    result = settle(store, pf, {}, "2026-11-06", dry_run=True)
    assert not result.buys and not result.sells
    assert store.get_plan("sh600019") is None
    assert pf.get("sh600019") is None


def test_sim_real_isolation(tmp_path):
    store, pf = _stores(tmp_path)
    _decision(store)
    settle(store, pf, {"sh600019": [_bar("2026-11-06", 10.1, 10.3, 10.4, 9.95)]},
           "2026-11-06")
    with sqlite3.connect(store.db_path) as conn:
        rows = conn.execute("SELECT account, shares FROM positions").fetchall()
    assert rows == [("sim", 200)]


def test_add_position_writes_blended_cost_back_to_plan(tmp_path):
    """加仓后 plan 成本回写摊薄成本：后续 ret/pnl 与组合成本都不再按首仓价"""
    store, pf = _stores(tmp_path)
    _decision(store, zone_low=10.0, zone_high=10.0, shares=200,
              stop_structural=9.0, expire_date="2026-11-20")
    fill = _bar("2026-11-06", 10.0, 10.0, 10.1, 9.9, v=1000)
    settle(store, pf, {"sh600019": [fill]}, "2026-11-06")
    assert store.get_plan("sh600019")["entry_price"] == 10.0

    add = _bar("2026-11-09", 10.5, 10.7, 10.75, 10.4, v=3000)  # 放量突破箱体上沿
    r = settle(store, pf, {"sh600019": [fill, add]}, "2026-11-09")
    assert any("加仓" in b["reason"] for b in r.buys)

    blended = round((200 * 10.0 + 100 * 10.7 + 5.01) / 300, 3)
    plan = store.get_plan("sh600019")
    assert abs(plan["entry_price"] - blended) < 1e-6
    assert abs(pf.get("sh600019").cost_price - blended) < 1e-6
    assert plan["tp1"] == 10.8 and plan["tp2"] == 11.5   # 止盈位仍锚首仓价，不重算

    tp = _bar("2026-11-10", 10.85, 10.9, 11.0, 10.8, v=500)  # 量能不足，不再触发加仓
    r = settle(store, pf, {"sh600019": [fill, add, tp]}, "2026-11-10")
    sell = next(s for s in r.sells if "+8%" in s["reason"])
    assert abs(sell["return_pct"] - round((10.8 - blended) / blended * 100, 2)) < 0.01
    assert sell["return_pct"] < 6.0        # 不再是按首仓价算出的 8.0
    # 卖出后组合成本仍是摊薄成本，不被重置回 10.0
    assert abs(pf.get("sh600019").cost_price - blended) < 1e-6


def test_sell_result_carries_cost_basis_and_entry_date(tmp_path):
    """卖出明细必须带摊薄成本与入场日，供知识库回写复算收益率"""
    store, pf = _stores(tmp_path)
    _decision(store, zone_low=10.0, zone_high=10.0, shares=200,
              stop_structural=9.0, expire_date="2026-11-20")
    fill = _bar("2026-11-06", 10.0, 10.0, 10.1, 9.9, v=1000)
    settle(store, pf, {"sh600019": [fill]}, "2026-11-06")
    add = _bar("2026-11-09", 10.5, 10.7, 10.75, 10.4, v=3000)
    settle(store, pf, {"sh600019": [fill, add]}, "2026-11-09")

    tp = _bar("2026-11-10", 10.85, 10.9, 11.0, 10.8, v=500)
    r = settle(store, pf, {"sh600019": [fill, add, tp]}, "2026-11-10")
    sell = next(s for s in r.sells if "+8%" in s["reason"])
    assert abs(sell["cost"] - 10.25) < 1e-6
    assert sell["entry_date"] == "2026-11-06"
    assert abs(round((sell["price"] - sell["cost"]) / sell["cost"] * 100, 2)
               - sell["return_pct"]) < 0.01


def test_second_round_overwrites_stale_plan_prices(tmp_path):
    """同一标的第二轮建仓：entry/tp/date 必须换成第二轮自己的值"""
    store, pf = _stores(tmp_path)
    _decision(store, zone_low=10.0, zone_high=10.0, shares=200,
              stop_structural=9.0, expire_date="2026-11-20")
    fill = _bar("2026-11-06", 10.0, 10.0, 10.1, 9.9)
    settle(store, pf, {"sh600019": [fill]}, "2026-11-06")
    stop = _bar("2026-11-09", 9.5, 9.3, 9.5, 9.2)      # 触发 9.4 止损，第一轮清仓
    settle(store, pf, {"sh600019": [fill, stop]}, "2026-11-09")
    assert store.get_plan("sh600019")["status"] == "CLOSED"

    _decision(store, decision_date="2026-11-10", zone_low=12.0, zone_high=12.0,
              shares=200, stop_structural=11.3, expire_date="2026-11-13")
    fill2 = _bar("2026-11-11", 12.0, 12.05, 12.1, 11.95)
    settle(store, pf, {"sh600019": [fill, stop, fill2]}, "2026-11-11")

    plan = store.get_plan("sh600019")
    assert plan["entry_price"] == 12.0
    assert plan["entry_date"] == "2026-11-11"
    assert plan["decision_date"] == "2026-11-10"
    assert plan["tp1"] == 12.96 and plan["tp2"] == 13.8
    assert plan["tier1_done"] == 0 and plan["tier2_done"] == 0

    # 次日 12.30 不得按第一轮遗留的 10.80 / 11.50 卖出
    nxt = _bar("2026-11-12", 12.2, 12.3, 12.4, 12.1)
    r = settle(store, pf, {"sh600019": [fill, stop, fill2, nxt]}, "2026-11-12")
    assert not r.sells
    assert pf.get("sh600019").shares == 200
