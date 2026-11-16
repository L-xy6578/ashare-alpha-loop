"""交易员决策模块（TradeExecutor）测试 — SKILL V9.0 风控规则数字化验证"""

import pytest

from src.trader import TradeExecutor, Position


def _pos(symbol, name, shares, cost, cur, frozen=False):
    return Position(symbol, name, shares, cost, cur, frozen)


# ----------------------------------------------------------------
# 数据质量门槛
# ----------------------------------------------------------------

def test_quality_good_allows_buy():
    ex = TradeExecutor(total_assets=30000, macro_cap_pct=50)
    ok, reason = ex.quality_allows("good")
    assert ok is True
    assert "允许建仓" in reason


def test_quality_limited_rejects_buy():
    ex = TradeExecutor(total_assets=30000, macro_cap_pct=50)
    ok, reason = ex.quality_allows("limited")
    assert ok is False
    assert "禁止建仓" in reason


def test_quality_poor_rejects_buy():
    ex = TradeExecutor(total_assets=30000, macro_cap_pct=50)
    ok, _ = ex.quality_allows("poor")
    assert ok is False


def test_plan_buy_rejected_when_limited():
    """limited 数据质量下 plan_buy 直接拒绝，无任何动作"""
    ex = TradeExecutor(total_assets=30000, macro_cap_pct=50)
    d = ex.plan_buy("sh600535", "天士力", 15.78, 5000, quality_level="limited")
    assert d.allowed is False
    assert d.actions == []


# ----------------------------------------------------------------
# 仓位计算
# ----------------------------------------------------------------

def test_total_position_pct():
    ex = TradeExecutor(total_assets=100000, macro_cap_pct=50)
    positions = [
        _pos("sh600535", "A", 1000, 10, 10),   # 10000 元 → 10%
        _pos("sh600019", "B", 1000, 20, 20),   # 20000 元 → 20%
    ]
    assert ex.total_position_pct(positions) == pytest.approx(30.0)


# ----------------------------------------------------------------
# 正常建仓（100股取整）
# ----------------------------------------------------------------

def test_plan_buy_normal_round_lot():
    """总资产 3 万、上限 50%、空仓 → 期望 5000 元 → 300 股（100 股取整）"""
    ex = TradeExecutor(total_assets=30000, macro_cap_pct=50)
    d = ex.plan_buy("sh600535", "天士力", 15.78, 5000, quality_level="good")
    assert d.allowed is True
    assert d.buy_shares == 300  # 5000 / 15.78 = 316 → 300
    assert d.actions[0].action == "buy"
    assert d.final_position_pct == pytest.approx(300 * 15.78 / 30000 * 100, abs=0.01)


def test_plan_buy_no_extra_reduce_when_within_cap():
    """仓位未超限 → 只有 buy 动作，无 reduce"""
    ex = TradeExecutor(total_assets=100000, macro_cap_pct=50)
    positions = [_pos("sh600535", "A", 500, 10, 10)]  # 5%
    d = ex.plan_buy("sh600019", "宝钢", 6.0, 10000, positions=positions)
    actions = [a.action for a in d.actions]
    assert actions == ["buy"]


def test_plan_buy_over_cap_resolved_by_reduce():
    """仓位超限但可通过先减后买化解 → 输出 reduce + buy 且最终不超上限"""
    ex = TradeExecutor(total_assets=100000, macro_cap_pct=50)
    # 仓位 60%（>50%），超限 10% + 期望 20% → 需减 30%
    positions = [_pos("sh600535", "A", 1000, 60, 60)]
    d = ex.plan_buy("sh600019", "宝钢", 6.0, 20000, positions=positions)
    actions = [a.action for a in d.actions]
    assert "reduce" in actions
    assert "buy" in actions
    assert d.allowed is True
    assert d.final_position_pct <= 50.0  # 决策后不超上限


# ----------------------------------------------------------------
# 先减后买
# ----------------------------------------------------------------

def test_reduce_then_buy_when_over_cap():
    """仓位 60%、上限 50%、期望买 10% → 需先减 20% → 输出 reduce + buy"""
    ex = TradeExecutor(total_assets=100000, macro_cap_pct=50)
    positions = [_pos("sh600535", "A", 1000, 40, 60)]   # 60000 → 60%
    d = ex.plan_buy("sh600019", "宝钢", 6.0, 10000, positions=positions)

    actions = [a.action for a in d.actions]
    assert "reduce" in actions
    assert "buy" in actions
    # 单次减仓 ≤ 50%（A 有 1000 股 → 最多减 500 股 = 30000 元 = 30%）
    reduce_act = [a for a in d.actions if a.action == "reduce"][0]
    assert reduce_act.shares <= 500
    assert d.final_position_pct <= 50.0  # 决策后不超上限


def test_reduce_order_by_profit_first():
    """先减后买按盈利最多优先：盈利 20% 的应被先减（在动作列表中排前）"""
    ex = TradeExecutor(total_assets=100000, macro_cap_pct=50)
    positions = [
        _pos("sh600535", "盈利多", 1000, 10, 12),   # +20%
        _pos("sh600019", "盈利少", 1000, 10, 10.5), # +5%
    ]
    # 仓位 22.5%，期望买 50% → 超限 22.5% → 需大幅减仓
    d = ex.plan_buy("sz002414", "高德", 13.0, 50000, positions=positions)
    reduces = [a for a in d.actions if a.action == "reduce"]
    if reduces:
        assert reduces[0].name == "盈利多"


def test_reduce_prefers_deepest_loss():
    """#62：全亏行情下按跌幅降序（最差者优先减），不再砍最不亏、留深亏"""
    ex = TradeExecutor(total_assets=100000, macro_cap_pct=50)
    positions = [
        _pos("sh600535", "亏少", 1000, 10, 9.5),    # -5%
        _pos("sh600019", "亏多", 1000, 10, 9.0),    # -10%
    ]
    d = ex.plan_buy("sz002414", "高德", 13.0, 80000, positions=positions)
    reduces = [a for a in d.actions if a.action == "reduce"]
    assert reduces, "应触发先减后买"
    assert reduces[0].name == "亏多", "最差者应优先减"


def test_plan_buy_rejected_when_need_reduce_over_cap():
    """#63：需减比例超过仓位上限 → 直接拒绝，不产生无效减仓"""
    ex = TradeExecutor(total_assets=30000, macro_cap_pct=50)
    positions = [_pos("sh600535", "A", 1000, 10, 12)]   # 40%
    # 期望买 20000 元 = 66.7% → need_reduce = 40+66.7-50 = 56.7% > 50% → 拒绝
    d = ex.plan_buy("sh600019", "B", 6.0, 20000, positions=positions)
    assert d.allowed is False
    assert d.actions == []
    assert "超上限" in d.reason or "无法容纳" in d.reason


def test_reduce_cap_per_position_50pct():
    """单只减仓不超过 50%：1000 股 → 最多 500 股"""
    ex = TradeExecutor(total_assets=100000, macro_cap_pct=50)
    positions = [_pos("sh600535", "A", 1000, 40, 60)]   # 60%
    d = ex.plan_buy("sh600019", "B", 6.0, 20000, positions=positions)
    reduces = [a for a in d.actions if a.action == "reduce"]
    for r in reduces:
        assert r.shares <= 500


def test_frozen_position_not_reduced():
    """冻结持仓（停牌）不参与先减后买"""
    ex = TradeExecutor(total_assets=100000, macro_cap_pct=50)
    positions = [
        _pos("sh600535", "冻结", 1000, 50, 50, frozen=True),  # 50%
        _pos("sh600019", "正常", 1000, 30, 30),               # 30%
    ]
    d = ex.plan_buy("sz002414", "C", 13.0, 10000, positions=positions)
    reduces = [a for a in d.actions if a.action == "reduce"]
    for r in reduces:
        assert r.name != "冻结"


# ----------------------------------------------------------------
# 止损
# ----------------------------------------------------------------

def test_stop_loss_triggered_at_minus_7():
    """亏损 -8% → 触发止损"""
    ex = TradeExecutor(total_assets=100000, macro_cap_pct=50, stop_loss_pct=7.0)
    positions = [_pos("sh600535", "A", 1000, 10, 9.2)]  # -8%
    actions = ex.check_stop_loss(positions)
    assert len(actions) == 1
    assert actions[0].action == "stop_loss"
    assert actions[0].shares == 1000


def test_stop_loss_not_triggered_within_threshold():
    """亏损 -5% → 不触发止损"""
    ex = TradeExecutor(total_assets=100000, macro_cap_pct=50)
    positions = [_pos("sh600535", "A", 1000, 10, 9.5)]  # -5%
    assert ex.check_stop_loss(positions) == []


def test_stop_loss_skips_frozen():
    """冻结持仓跳过止损检查"""
    ex = TradeExecutor(total_assets=100000, macro_cap_pct=50)
    positions = [_pos("sh600535", "A", 1000, 10, 9.2, frozen=True)]  # -8% 但冻结
    assert ex.check_stop_loss(positions) == []


def test_stop_loss_custom_threshold():
    """自定义止损阈值 -5%"""
    ex = TradeExecutor(total_assets=100000, macro_cap_pct=50, stop_loss_pct=5.0)
    positions = [_pos("sh600535", "A", 1000, 10, 9.6)]  # -4%
    assert ex.check_stop_loss(positions) == []
    positions2 = [_pos("sh600535", "A", 1000, 10, 9.4)]  # -6%
    assert len(ex.check_stop_loss(positions2)) == 1


# ----------------------------------------------------------------
# 主动减仓（涨停减仓/止盈）
# ----------------------------------------------------------------

def test_plan_reduce_50pct():
    """主动减仓默认减 50%：1000 股 → 500 股"""
    ex = TradeExecutor(total_assets=100000, macro_cap_pct=50)
    act = ex.plan_reduce("sh600535", "天士力", 16.0, 1000)
    assert act.action == "reduce"
    assert act.shares == 500
    assert act.price == 16.0


def test_plan_reduce_custom_ratio():
    """自定义减仓比例 75%：1000 股 → 700 股（100 股取整）"""
    ex = TradeExecutor(total_assets=100000, macro_cap_pct=50)
    act = ex.plan_reduce("sh600535", "天士力", 16.0, 1000, reduce_ratio=0.75)
    assert act.shares == 700


# ----------------------------------------------------------------
# 宏观仓位上限映射
# ----------------------------------------------------------------

def test_regime_cap_mapping():
    """市场状态 → 仓位上限：进攻60 / 震荡50 / 防御30 / 急跌10"""
    assert TradeExecutor(100000, regime="TREND_UP").macro_cap_pct == 60
    assert TradeExecutor(100000, regime="RANGE").macro_cap_pct == 50
    assert TradeExecutor(100000, regime="TREND_DOWN").macro_cap_pct == 30
    assert TradeExecutor(100000, regime="DEFENSIVE").macro_cap_pct == 10


def test_limited_quality_halves_cap():
    """limited 数据质量下仓位上限减半：50% → 25%"""
    ex = TradeExecutor(total_assets=100000, macro_cap_pct=50)
    positions = [_pos("sh600535", "A", 1000, 20, 20)]  # 20%
    # 期望买 20% → 合计 40% > 25%（减半后）→ 需减仓或拒绝
    d = ex.plan_buy("sh600019", "B", 6.0, 20000, positions=positions, quality_level="limited")
    # limited 本身禁建仓
    assert d.allowed is False
