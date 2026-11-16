"""策略信号接入闭环测试"""

from datetime import date, timedelta

from src.regime import Regime
from src.strategy import MovingAverageCrossStrategy, RsiMeanReversionStrategy
from src.data_source import KlineBar
from src.portfolio import PortfolioStore
from src.strategy_loop import (
    apply_strategy_actions,
    candidate_buy_allowed,
    select_strategy,
)
from src.trader import Position, TradeExecutor


def _bars(closes, start="2026-07-01"):
    d = date.fromisoformat(start)
    out = []
    for c in closes:
        out.append(KlineBar(d.isoformat(), c, c, c * 1.01, c * 0.99))
        d += timedelta(days=1)
    return out


class TestSelectStrategy:
    def test_regime_to_strategy(self):
        assert isinstance(select_strategy("TREND_UP"), MovingAverageCrossStrategy)
        assert isinstance(select_strategy(Regime.RANGE), RsiMeanReversionStrategy)
        assert isinstance(select_strategy("TREND_DOWN"), MovingAverageCrossStrategy)


class TestCandidateBuy:
    def test_golden_cross_in_trend_up(self):
        closes = [10.0] * 19 + [10.0, 11.0]   # 今日金叉（prev short<=long, today short>long）
        sig = candidate_buy_allowed("TREND_UP", _bars(closes))
        assert sig is not None and sig.action == "buy"

    def test_rsi_oversold_in_range(self):
        closes = [10 - i * 0.2 for i in range(25)]  # 持续下跌 → RSI 超卖
        sig = candidate_buy_allowed("RANGE", _bars(closes))
        assert sig is not None and sig.action == "buy"

    def test_no_buy_in_downtrend_or_defensive(self):
        bars = _bars([20 - i * 0.1 for i in range(25)])
        assert candidate_buy_allowed("TREND_DOWN", bars) is None
        assert candidate_buy_allowed("DEFENSIVE", bars) is None

    def test_insufficient_bars_returns_none(self):
        assert candidate_buy_allowed("TREND_UP", _bars([10, 11, 12])) is None


class TestApplyActions:
    def test_sell_signal_reduces_half(self, tmp_path):
        store = PortfolioStore(str(tmp_path / "p.db"))
        store.set("sh600535", "天士力", 600, 15.78, 15.9)
        held = [Position("sh600535", "天士力", 600, 15.78, 15.9)]
        closes = [10 + i * 0.2 for i in range(25)]  # 持续上涨 → RSI 超买 → 卖出信号
        trader = TradeExecutor(total_assets=30000, macro_cap_pct=50)

        actions = apply_strategy_actions(trader, store, "RANGE", held,
                                         {"sh600535": _bars(closes)})
        assert len(actions) == 1
        assert actions[0].action == "reduce"
        assert actions[0].shares == 300            # 600 × 50%
        assert "RSI" in actions[0].reason
        assert store.get("sh600535").shares == 300

    def test_no_signal_no_action(self, tmp_path):
        store = PortfolioStore(str(tmp_path / "p2.db"))
        store.set("sh600019", "宝钢股份", 1600, 5.95, 6.11)
        held = [Position("sh600019", "宝钢股份", 1600, 5.95, 6.11)]
        trader = TradeExecutor(total_assets=30000, macro_cap_pct=50)
        actions = apply_strategy_actions(trader, store, "RANGE", held,
                                         {"sh600019": _bars([10.0] * 25)})
        assert actions == []
