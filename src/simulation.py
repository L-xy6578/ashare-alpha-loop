"""
三角闭环选股体系 — 模拟引擎

基于真实K线 + 止盈/止损规则，自动计算模拟交易结果。
与数据源解耦：只消费 DataSource.daily_kline() 返回的 KlineBar。

用法:
    from src.simulation import simulate_trade
    from src.data_source import StaticDataSource

    src = StaticDataSource({...})
    r = simulate_trade(src, "sh600535", "2026-07-29", take_profit=16.50, stop_loss=15.30)
    # -> {"entry_price":..., "exit_price":..., "exit_date":..., "exit_reason":..., "return_pct":...}
"""

from __future__ import annotations


def exit_signal(bar, take_profit: float | None = None,
                stop_loss: float | None = None):
    """单根K线触线判定（全系统统一出场口径）

    口径：盘中触线、按线价成交——
    - 某日 low  <= stop_loss  → 止损（成交价 stop_loss）
    - 某日 high >= take_profit → 止盈（成交价 take_profit）
    - 同一根K线同时触及两条线 → 止损优先（保守假设，避免高估收益）
    - 均未触发 → (None, None)

    Returns:
        (price, reason) 或 (None, None)
    """
    if stop_loss is not None and bar.low <= stop_loss:
        return stop_loss, "止损"
    if take_profit is not None and bar.high >= take_profit:
        return take_profit, "止盈"
    return None, None


def simulate_exit(bars, take_profit: float | None = None, stop_loss: float | None = None):
    """按日序扫描真实K线，决定退出价与原因

    - 止损：low <= stop_loss，成交价 stop_loss
    - 止盈：high >= take_profit，成交价 take_profit
    - 同日双触发时止损优先（见 exit_signal）
    - 均未触发 → 持有到期末收盘价

    Returns:
        (exit_price, exit_date, reason)
    """
    for bar in bars:
        price, reason = exit_signal(bar, take_profit, stop_loss)
        if price is not None:
            return price, bar.date, reason
    last = bars[-1]
    return last.close, last.date, "持有至期末"


def simulate_trade(
    source,
    symbol: str,
    entry_date: str,
    take_profit: float | None = None,
    stop_loss: float | None = None,
) -> dict:
    """在 entry_date 开盘价建仓，按止盈/止损规则模拟退出

    Args:
        source: 实现 daily_kline(symbol, start) 的数据源
        symbol: 标的代码，如 "sh600535"
        entry_date: 建仓日 YYYY-MM-DD（以当日开盘价为买入价）
        take_profit: 止盈价（触发即卖出）
        stop_loss:   止损价（触发即卖出）

    Returns:
        {"entry_price", "exit_price", "exit_date", "exit_reason", "return_pct"}
    """
    bars = source.daily_kline(symbol, start=entry_date)
    if not bars:
        raise ValueError(f"{symbol} 无K线数据，无法模拟")

    entry = bars[0].open
    exit_price, exit_date, reason = simulate_exit(bars, take_profit, stop_loss)
    return {
        "entry_price": entry,
        "exit_price": exit_price,
        "exit_date": exit_date,
        "exit_reason": reason,
        "return_pct": round((exit_price - entry) / entry * 100, 2),
    }
