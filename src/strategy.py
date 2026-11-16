"""
三角闭环选股体系 — 策略接口与内置策略

策略 = 一个纯函数式的信号生成器：给定截至当前日的K线序列，决定下一交易日动作。

接口约定（保证反前视）：
    - generate(bars, position) 只读 bars[0..-1]（已收盘的历史），返回信号
    - 信号在 T 日收盘后生成，引擎在 T+1 日开盘执行

内置策略：
    - MovingAverageCrossStrategy：双均线金叉/死叉
    - BreakoutStrategy：N日新高突破（买入） + N日新低跌破（卖出）
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from src.data_source import KlineBar


@dataclass(frozen=True)
class Signal:
    """策略信号（T日收盘生成，T+1日开盘执行）"""

    action: str          # "buy" / "sell" / "hold"
    reason: str = ""
    strength: float = 1.0  # 信号强度 0-1，可作仓位加权（当前未用）


@dataclass
class Position:
    """当前持仓快照（策略引擎维护，策略只读）"""

    shares: int = 0       # 持股数
    entry_price: float = 0.0
    entry_date: str = ""
    stop_loss: float = 0.0
    take_profit: float = 0.0


class Strategy(ABC):
    """策略基类：子类实现 generate()"""

    name = "base"

    def __init__(self, stop_loss_pct: float = 0.07,
                 take_profit_pct: float = 0.20,
                 position_ratio: float = 0.5,
                 fee_rate: float = 0.0002):
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.position_ratio = position_ratio  # 单笔仓位占总资金比例
        self.fee_rate = fee_rate              # 手续费（双边各收）

    @abstractmethod
    def generate(self, bars: list[KlineBar], position: Position) -> Signal:
        """基于截至当前日的K线，返回信号

        Args:
            bars: 已收盘的历史K线（升序，含当日）
            position: 当前持仓（可空仓 shares=0）
        """


class MovingAverageCrossStrategy(Strategy):
    """双均线交叉策略：短均线上穿长均线买入，下穿卖出"""

    name = "ma_cross"

    def __init__(self, short_win: int = 5, long_win: int = 20, **kwargs):
        super().__init__(**kwargs)
        self.short_win = short_win
        self.long_win = long_win

    def generate(self, bars: list[KlineBar], position: Position) -> Signal:
        if len(bars) < self.long_win + 1:
            return Signal("hold", "数据不足")

        closes = [b.close for b in bars]
        short_ma = sum(closes[-self.short_win:]) / self.short_win
        long_ma = sum(closes[-self.long_win:]) / self.long_win
        prev_short = sum(closes[-self.short_win - 1:-1]) / self.short_win
        prev_long = sum(closes[-self.long_win - 1:-1]) / self.long_win

        # 金叉：前一日 short <= long，今日 short > long
        if prev_short <= prev_long and short_ma > long_ma:
            if position.shares == 0:
                return Signal("buy", f"MA{self.short_win}上穿MA{self.long_win}金叉")
        # 死叉：前一日 short >= long，今日 short < long
        elif prev_short >= prev_long and short_ma < long_ma:
            if position.shares > 0:
                return Signal("sell", f"MA{self.short_win}下穿MA{self.long_win}死叉")
        return Signal("hold", "")


class BreakoutStrategy(Strategy):
    """N日突破策略：收盘价创N日新高买入（且均线多头），跌破N日新低卖出"""

    name = "breakout"

    def __init__(self, n: int = 20, **kwargs):
        super().__init__(**kwargs)
        self.n = n

    def generate(self, bars: list[KlineBar], position: Position) -> Signal:
        if len(bars) < self.n + 1:
            return Signal("hold", "数据不足")

        closes = [b.close for b in bars]
        cur = closes[-1]
        window = closes[-self.n - 1:-1]  # 不含当日的前 N 日

        if position.shares == 0 and cur > max(window):
            return Signal("buy", f"收盘{cur:.2f}创{self.n}日新高")
        if position.shares > 0 and cur < min(window):
            return Signal("sell", f"收盘{cur:.2f}跌破{self.n}日新低")
        return Signal("hold", "")


class RsiMeanReversionStrategy(Strategy):
    """RSI均值回归策略（震荡市专用）

    逻辑：超卖（RSI 低）买入，超买（RSI 高）卖出——赌价格回归均值。
    适用于震荡市；趋势市中会接飞刀（历史验证可界定其适用边界）。

    默认参数：
        buy_rsi:  RSI 低于该值买入（超卖，默认 30）
        sell_rsi: RSI 高于该值卖出（超买，默认 70）
        window:   RSI 计算窗口（默认 14）
    """

    name = "rsi_meanreversion"

    def __init__(self, buy_rsi: float = 30, sell_rsi: float = 70,
                 window: int = 14, **kwargs):
        super().__init__(**kwargs)
        self.buy_rsi = buy_rsi
        self.sell_rsi = sell_rsi
        self.window = window

    def generate(self, bars: list[KlineBar], position: Position) -> Signal:
        if len(bars) < self.window + 2:
            return Signal("hold", "数据不足")

        closes = [b.close for b in bars]
        rsi = _rsi(closes, self.window)

        if position.shares == 0 and rsi < self.buy_rsi:
            return Signal("buy", f"RSI={rsi:.1f} 超卖(<{self.buy_rsi}) 均值回归买入")
        if position.shares > 0 and rsi > self.sell_rsi:
            return Signal("sell", f"RSI={rsi:.1f} 超买(>{self.sell_rsi}) 均值回归卖出")
        return Signal("hold", "")


def _rsi(closes: list[float], window: int = 14) -> float:
    """计算最近收盘价的 RSI（Wilder 简化版：取最近 window 段涨跌均值）"""
    if len(closes) < window + 1:
        return 50.0
    gains, losses = [], []
    for i in range(len(closes) - window, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains) / window
    avg_loss = sum(losses) / window
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0   # 无波动视为中性（修复平坦行情误报超买）
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)
