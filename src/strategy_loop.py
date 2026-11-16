"""
三角闭环选股体系 — 策略信号接入闭环（"什么环境用什么策略"落地）

市场状态 → 策略 → 信号 → 动作：
    TREND_UP      → 双均线策略：金叉买入 / 死叉减仓
    RANGE         → RSI 均值回归：超卖(RSI<30)买入 / 超买(RSI>70)减仓
    TREND_DOWN / DEFENSIVE → 不加仓；持仓按策略卖出信号减仓 50%，硬止损由 -7% 兜底

设计约束（保守、可测试）：
    - 策略信号只触发"减仓 50%"或"候选建仓"，不清仓、不追单
    - 建仓必须同时满足：五因子 ≥85（数据化评分）+ 策略买入信号
    - 所有函数不依赖网络，纯 K 线驱动

用法:
    from src.strategy_loop import select_strategy, apply_strategy_actions, candidate_buy_allowed
    strategy = select_strategy(regime)
    actions = apply_strategy_actions(trader, store, regime, held, klines)
    sig = candidate_buy_allowed(regime, bars, strategy)
"""

from __future__ import annotations

from src.regime import Regime
from src.strategy import (
    MovingAverageCrossStrategy,
    Position as StrategyPosition,
    RsiMeanReversionStrategy,
    Signal,
    Strategy,
)
from src.logger import get_logger

log = get_logger(__name__)


def _norm(regime) -> str:
    """Regime 枚举 / 字符串 → 大写字符串（TREND_UP/RANGE/TREND_DOWN/DEFENSIVE）"""
    if isinstance(regime, Regime):
        return regime.value.upper()
    return str(regime).upper()


def select_strategy(regime, stop_loss_pct: float = 7.0) -> Strategy:
    """按市场状态选择策略

    TREND_UP → 双均线交叉；RANGE → RSI 均值回归；
    TREND_DOWN/DEFENSIVE → 仍用双均线（只出减仓信号，禁止买入）
    """
    r = _norm(regime)
    if r == "RANGE":
        return RsiMeanReversionStrategy(buy_rsi=30, sell_rsi=70)
    return MovingAverageCrossStrategy(
        short_win=5, long_win=20, stop_loss_pct=stop_loss_pct / 100.0,
    )


def _to_strategy_position(p, stop_loss_pct: float = 7.0) -> StrategyPosition:
    return StrategyPosition(
        shares=p.shares,
        entry_price=p.cost_price,
        stop_loss=p.cost_price * (1 - stop_loss_pct / 100.0),
    )


def apply_strategy_actions(
    trader,
    store,
    regime,
    held_positions: list,
    klines: dict,
    stop_loss_pct: float = 7.0,
) -> list:
    """对已持仓标的执行策略信号：sell → 减仓 50% 并回写持仓表

    Returns:
        触发并执行的 TradeAction 列表
    """
    strategy = select_strategy(regime, stop_loss_pct)
    actions = []
    for p in held_positions:
        bars = klines.get(p.symbol, [])
        if len(bars) < 21:
            continue
        sig = strategy.generate(bars, _to_strategy_position(p, stop_loss_pct))
        if sig.action != "sell" or p.shares <= 0:
            continue
        price = bars[-1].close
        act = trader.plan_reduce(
            p.symbol, p.name, price, p.shares, 0.5,
            f"策略信号: {sig.reason}",
        )
        store.reduce(p.symbol, act.shares, price, f"策略减仓50%: {sig.reason}")
        actions.append(act)
        log.info("策略减仓 %s(%s): %s", p.name, p.symbol, sig.reason)
    return actions


def candidate_buy_allowed(regime, bars: list, strategy: Strategy | None = None) -> Signal | None:
    """候选建仓前置检查：返回策略买入信号（无信号/下跌防御市 → None）

    TREND_DOWN / DEFENSIVE 一律禁止新买入。
    """
    r = _norm(regime)
    if r in ("TREND_DOWN", "DEFENSIVE"):
        return None
    if len(bars) < 21:
        return None
    strategy = strategy or select_strategy(regime)
    sig = strategy.generate(bars, StrategyPosition(shares=0))
    return sig if sig.action == "buy" else None
