"""预案减仓线 / 清仓线自动执行 + 统一出场口径测试"""

from src.data_source import KlineBar
from src.portfolio import PortfolioStore
from src.simulation import exit_signal, simulate_exit
from src.trim_rules import (EXECUTED_MARK, SELL_LINE_MARK, apply_sell_line_rules,
                            apply_trim_rules)


def _bar(day, open_px, close_px):
    return KlineBar(day, open_px, close_px, max(open_px, close_px) * 1.01,
                    min(open_px, close_px) * 0.99)


def _store(tmp_path):
    s = PortfolioStore(str(tmp_path / "trim.db"))
    s.set("sh600019", "宝钢股份", 1600, 5.95, 6.11)
    s.set("sh600535", "天士力", 600, 15.78, 15.90)
    return s


RULES = {
    "sh600019": {"price": 6.00, "shares": 800, "note": "R45 预案减仓线"},
    "sh600535": {"price": 15.00, "shares": 300, "note": "R45 预案减仓线"},
}


def test_trigger_reduces_and_marks(tmp_path):
    store = _store(tmp_path)
    klines = {
        "sh600019": [_bar("2026-08-05", 5.94, 5.86)],   # 盘中最低 5.80 ≤ 6.00 → 触发
        "sh600535": [_bar("2026-08-05", 15.72, 15.72)], # 盘中最低 15.56 > 15.00 → 不触发
    }
    actions = apply_trim_rules(store, klines, today="2026-08-05", rules=RULES)
    assert len(actions) == 1
    assert actions[0].symbol == "sh600019"
    assert actions[0].shares == 800
    assert actions[0].price == 6.00            # 统一口径：按线价成交（不再按收盘）
    assert "盘中最低" in actions[0].reason
    p = store.get("sh600019")
    assert p.shares == 800                      # 1600 - 800
    assert p.last_price == 6.00
    assert EXECUTED_MARK in p.note
    # 天士力未触发，持仓不变
    assert store.get("sh600535").shares == 600


def test_idempotent_after_mark(tmp_path):
    store = _store(tmp_path)
    klines = {"sh600019": [_bar("2026-08-05", 5.94, 5.86)]}
    assert len(apply_trim_rules(store, klines, today="2026-08-05", rules=RULES)) == 1
    assert apply_trim_rules(store, klines, today="2026-08-05", rules=RULES) == []  # 已打标记
    assert store.get("sh600019").shares == 800


def test_no_trigger_when_intraday_low_above(tmp_path):
    store = _store(tmp_path)
    klines = {"sh600019": [_bar("2026-08-05", 6.10, 6.12)]}   # 盘中最低 6.06 > 6.00
    assert apply_trim_rules(store, klines, today="2026-08-05", rules=RULES) == []
    assert store.get("sh600019").shares == 1600


def test_intraday_touch_triggers_despite_open_above(tmp_path):
    """统一口径的关键差异：开盘价在线上方，但盘中回落触线 → 必须减仓"""
    store = _store(tmp_path)
    klines = {"sh600019": [_bar("2026-08-05", 6.10, 6.05)]}   # 盘中最低 5.99 ≤ 6.00
    actions = apply_trim_rules(store, klines, today="2026-08-05", rules=RULES)
    assert len(actions) == 1
    assert actions[0].price == 6.00


def test_stale_bar_not_executed(tmp_path):
    store = _store(tmp_path)
    klines = {"sh600019": [_bar("2026-08-03", 5.80, 5.75)]}   # 非当日K线
    assert apply_trim_rules(store, klines, today="2026-08-05", rules=RULES) == []
    assert store.get("sh600019").shares == 1600


def test_shares_capped_by_holding(tmp_path):
    store = _store(tmp_path)
    store.set("sh600019", "宝钢股份", 300, 5.95, 5.86)   # 持仓少于规则 800
    klines = {"sh600019": [_bar("2026-08-05", 5.94, 5.86)]}
    actions = apply_trim_rules(store, klines, today="2026-08-05", rules=RULES)
    assert len(actions) == 1
    assert actions[0].shares == 300
    assert store.get("sh600019").shares == 0


# ---------------------------------------------------------------
# 统一出场口径：盘中触线、按线价成交（simulation 与 trim_rules 共用 exit_signal）
# ---------------------------------------------------------------

def test_exit_signal_stop_loss_priority_when_both_hit():
    bar = KlineBar("2026-08-12", 15.80, 15.90, 16.60, 15.20)   # 同根既破止损也达止盈
    assert exit_signal(bar, take_profit=16.50, stop_loss=15.30) == (15.30, "止损")


def test_simulation_intraday_touch_fills_at_line():
    bars = [KlineBar("2026-08-05", 6.10, 6.05, 6.12, 5.99)]    # 开盘在线上，盘中触线
    assert simulate_exit(bars, take_profit=6.20, stop_loss=6.00) == (6.00, "2026-08-05", "止损")


def test_sell_line_stop_loss_fills_at_line(tmp_path):
    store = _store(tmp_path)
    bar = KlineBar("2026-08-05", 15.60, 15.10, 15.80, 15.05)   # low 15.05 ≤ 15.30
    lines = {"sh600535": {"sl": 15.30, "tp": 16.50, "note": "测试"}}
    actions = apply_sell_line_rules(store, {"sh600535": [bar]}, today="2026-08-05",
                                    lines=lines)
    assert len(actions) == 1
    assert actions[0].price == 15.30           # 按线价成交，而非收盘 15.10
    assert actions[0].shares == 600
    p = store.get("sh600535")
    assert p.shares == 0 and p.last_price == 15.30
    assert SELL_LINE_MARK in p.note


def test_sell_line_take_profit_fills_at_line(tmp_path):
    store = _store(tmp_path)
    bar = KlineBar("2026-08-07", 16.00, 16.60, 16.70, 15.95)   # high 16.70 ≥ 16.50
    lines = {"sh600535": {"sl": 15.30, "tp": 16.50, "note": "测试"}}
    actions = apply_sell_line_rules(store, {"sh600535": [bar]}, today="2026-08-07",
                                    lines=lines)
    assert len(actions) == 1
    assert actions[0].price == 16.50
    assert "止盈" in actions[0].reason


def test_sell_line_stop_priority_fills_stop_line(tmp_path):
    store = _store(tmp_path)
    bar = KlineBar("2026-08-05", 16.00, 16.00, 16.80, 15.00)   # 止盈与止损同根触发
    lines = {"sh600535": {"sl": 15.30, "tp": 16.50, "note": "测试"}}
    actions = apply_sell_line_rules(store, {"sh600535": [bar]}, today="2026-08-05",
                                    lines=lines)
    assert len(actions) == 1
    assert actions[0].price == 15.30
    assert "止损" in actions[0].reason
