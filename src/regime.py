"""
三角闭环选股体系 — 市场状态判定 + 自适应策略

中心思想「不预测、只应对」的落地：
    先判定当前市场处于什么状态（趋势/震荡/防御），
    再自动选择匹配的策略——趋势市用趋势策略，震荡市用均值回归，防御市空仓。

判定逻辑（基于历史K线，天然无前视偏差）：
    - 趋势市（上涨）：MA20 > MA60 且 价格 > MA20（均线多头）
    - 趋势市（下跌）：MA20 < MA60 且 价格 < MA20（均线空头）
    - 震荡市：价格围绕 MA20 上下缠绕（用 |价格/MA20 - 1| 和 均线距离判断）
    - 防御市：均线空头 + 价格远离 MA20（深跌）
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from src.data_source import KlineBar
from src.logger import get_logger

from src.strategy import Strategy, Position, Signal

log = get_logger(__name__)


class Regime(str, Enum):
    TREND_UP = "trend_up"      # 上升趋势 → 趋势策略（做多）
    TREND_DOWN = "trend_down"  # 下降趋势 → 趋势策略（做空/回避，A股只做多则空仓）
    RANGE = "range"            # 震荡 → 均值回归
    DEFENSIVE = "defensive"    # 防御 → 空仓

    def label(self) -> str:
        return {
            Regime.TREND_UP: "🟢上升趋势",
            Regime.TREND_DOWN: "🔴下降趋势",
            Regime.RANGE: "🟡震荡市",
            Regime.DEFENSIVE: "🟠防御市",
        }[self]


@dataclass
class RegimeResult:
    regime: Regime
    ma20: float
    ma60: float
    price_ma20_gap: float   # 价格偏离 MA20 的比例
    detail: str


def detect_regime(bars: list[KlineBar], ma_short: int = 20, ma_long: int = 60) -> RegimeResult | None:
    """判定市场状态

    Returns:
        RegimeResult；数据不足返回 None
    """
    if len(bars) < ma_long + 1:
        return None

    closes = [b.close for b in bars]
    price = closes[-1]
    ma20 = sum(closes[-ma_short:]) / ma_short
    ma60 = sum(closes[-ma_long:]) / ma_long

    # 价格偏离 MA20 比例
    gap = (price - ma20) / ma20 if ma20 else 0.0

    # 均线关系
    bullish = ma20 > ma60
    bearish = ma20 < ma60

    # 震荡判定：均线距离近（<3%）且价格贴近 MA20（<5%）
    ma_distance = abs(ma20 - ma60) / ma60 if ma60 else 0.0
    price_close_to_ma = abs(gap) < 0.05

    if price_close_to_ma and ma_distance < 0.03:
        regime = Regime.RANGE
        detail = f"MA20={ma20:.2f} MA60={ma60:.2f} 缠绕，价格偏离{abs(gap):.2%}"
    elif bearish:
        # 空头排列：深度偏离 → 防御；否则下降趋势
        if gap < -0.08:
            regime = Regime.DEFENSIVE
            detail = f"空头+深跌（偏离-{abs(gap):.1%}）→ 防御"
        else:
            regime = Regime.TREND_DOWN
            detail = f"MA20={ma20:.2f} < MA60={ma60:.2f} 空头排列"
    elif bullish:
        regime = Regime.TREND_UP
        detail = f"MA20={ma20:.2f} > MA60={ma60:.2f} 多头排列"
    else:
        regime = Regime.RANGE
        detail = "均线接近，方向不明 → 按震荡处理"

    return RegimeResult(regime=regime, ma20=ma20, ma60=ma60,
                        price_ma20_gap=gap, detail=detail)


class AdaptiveStrategy(Strategy):
    """自适应策略：按市场状态自动切换内部策略

    - 上升趋势 → trend_strategy（如 MA交叉/突破）
    - 震荡市 → range_strategy（如 RSI 均值回归）
    - 下降趋势/防御 → 不建仓（A股无做空，只回避）
    """

    name = "adaptive"

    def __init__(self, trend_strategy: Strategy, range_strategy: Strategy,
                 ma_short: int = 20, ma_long: int = 60, **kwargs):
        super().__init__(**kwargs)
        self.trend_strategy = trend_strategy
        self.range_strategy = range_strategy
        self.ma_short = ma_short
        self.ma_long = ma_long

    def current_regime(self, bars: list[KlineBar]) -> Regime | None:
        r = detect_regime(bars, self.ma_short, self.ma_long)
        return r.regime if r else None

    def generate(self, bars: list[KlineBar], position: Position) -> Signal:
        r = detect_regime(bars, self.ma_short, self.ma_long)
        if r is None:
            return Signal("hold", "数据不足")

        regime = r.regime
        log.debug("adaptive[%s] %s", regime.value, r.detail)

        if regime == Regime.TREND_UP:
            return self.trend_strategy.generate(bars, position)
        if regime == Regime.RANGE:
            return self.range_strategy.generate(bars, position)

        # TREND_DOWN / DEFENSIVE：不建仓；若已持仓，交给趋势策略处理（可能触发卖出）
        if regime == Regime.TREND_DOWN and position.shares > 0:
            return self.trend_strategy.generate(bars, position)
        if position.shares > 0:
            # 防御市/下降趋势且有持仓 → 清仓
            return Signal("sell", f"市场进入{r.regime.label()}，减仓回避")
        return Signal("hold", f"市场{r.regime.label()}，观望")
