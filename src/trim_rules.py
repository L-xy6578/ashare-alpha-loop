"""
预案减仓线 / 清仓线 — 规则自动执行（方案 A，2026-08-05 落地，08-07 补清仓线）

背景：9:25 竞价预案输出减仓线（如"宝钢 ≤6.00 减 800 股"）与清仓线
（止损线：开盘 ≤ 止损 清仓；止盈线：开盘 ≥ 止盈 清仓），但此前只有减仓线
有执行通道，止损/止盈清仓出现"预案说清仓、收盘却没卖"的脱节（08-07 宝钢）。
本模块：
    - TRIM_RULES 为预案减仓线唯一规则源（morning_flow 提示 与 r47 收盘执行共用）
    - 清仓线（止损/止盈）与 morning_flow 共用 watchlist.take_profit/stop_loss，
      由 r47 收盘时执行（apply_sell_line_rules）
    - 触发/成交口径统一（与 src/simulation.py 的 exit_signal 一致）：
      **盘中触线、按线价成交**——减仓线 low ≤ 线价，止损 low ≤ 止损线、
      止盈 high ≥ 止盈线，成交价即线价；同一根K线止损优先（保守）
    - 幂等：减仓线看持仓 note「预案减仓已执行」；清仓线持仓已清（shares=0）
      或 note 含「清仓线已执行」后不再重复执行

用法:
    from src.trim_rules import TRIM_RULES, apply_trim_rules
    from src.trim_rules import apply_sell_line_rules, SELL_LINE_MARK
    actions = apply_trim_rules(store, klines, today="2026-08-05")
    sells = apply_sell_line_rules(store, klines, today="2026-08-07", lines={...})
"""

from __future__ import annotations

from src.logger import get_logger
from src.trader import TradeAction

log = get_logger(__name__)

# 手动预案沉淀的减仓线（symbol -> 规则，可维护；morning_flow 与 r47 共用）
# #107：R45 预案减仓线标的（宝钢/天士力）均已清仓，2026-08-25 移除陈旧规则，
# 避免每日收盘"预案减仓线触发但无持仓"重复告警；新预案按需加入。
TRIM_RULES: dict[str, dict] = {}

EXECUTED_MARK = "预案减仓已执行"
SELL_LINE_MARK = "清仓线已执行"
MIRROR_MARK = "待真实跟单(次日9:25)"   # 模拟盘建仓标记 → 竞价预案真实跟单指令


def apply_trim_rules(store, klines: dict, today: str,
                     rules: dict | None = None) -> list[TradeAction]:
    """对模拟盘持仓执行预案减仓线（一次/标的）

    Args:
        store: PortfolioStore（sim 账户）
        klines: {symbol: [KlineBar, ...]}，取最后一根作为当日K线
        today: 当日 YYYY-MM-DD（新鲜度校验：非当日K线不执行，防陈旧数据触发）
        rules: 规则源，默认 TRIM_RULES

    Returns:
        触发并回写的 TradeAction 列表
    """
    rules = TRIM_RULES if rules is None else rules
    actions = []
    for p in store.holding(account="sim"):
        rule = rules.get(p.symbol)
        if not rule:
            continue
        if p.note and EXECUTED_MARK in p.note:
            continue
        bars = klines.get(p.symbol, [])
        if not bars:
            continue
        bar = bars[-1]
        if bar.date != today:      # 当日K线缺失 → 不执行（防陈旧数据误触发）
            log.warning("预案减仓跳过 %s：当日K线缺失（最新 %s）", p.symbol, bar.date)
            continue
        if bar.low > rule["price"]:
            continue
        shares = min(int(rule["shares"]), p.shares)
        if shares <= 0:
            continue
        price = rule["price"]
        act = TradeAction(
            action="reduce", symbol=p.symbol, name=p.name,
            shares=shares, price=price,
            reason=f"预案减仓线执行（{rule['note']}：盘中最低{bar.low:.2f}≤{rule['price']:.2f}，按线价成交）",
        )
        marker = f"{EXECUTED_MARK}({bar.date}@{price})"
        note = " | ".join(x for x in (p.note, marker) if x)
        store.set(p.symbol, p.name, p.shares - shares, p.cost_price, price, note)
        actions.append(act)
        log.info("预案减仓线执行 %s(%s): 减%d股@%.2f", p.name, p.symbol, shares, price)
    return actions


def apply_sell_line_rules(store, klines: dict, today: str,
                          lines: dict[str, dict],
                          account: str = "sim") -> list[TradeAction]:
    """止损/止盈清仓线自动执行（方案：9:25 预案触发 → 收盘落实）

    Args:
        store: PortfolioStore（sim 账户）
        klines: {symbol: [KlineBar, ...]}，取最后一根作为当日K线
        today: 当日 YYYY-MM-DD（新鲜度校验：非当日K线不执行，防陈旧数据触发）
        lines: {symbol: {"sl": float|None, "tp": float|None, "note": str}}
            sl=止损线（盘中 low ≤ sl 触发），tp=止盈线（盘中 high ≥ tp 触发）
            ——与 morning_flow._judge_position 同源（watchlist.take_profit/stop_loss）
        account: 默认 sim 模拟盘

    Returns:
        触发并全清回写的 TradeAction 列表（按线价成交，幂等）
    """
    from src.simulation import exit_signal

    actions = []
    for p in store.holding(account=account):
        rule = lines.get(p.symbol)
        if not rule:
            continue
        sl, tp = rule.get("sl"), rule.get("tp")
        if not sl and not tp:
            continue
        if p.note and SELL_LINE_MARK in p.note:
            continue
        bars = klines.get(p.symbol, [])
        if not bars:
            continue
        bar = bars[-1]
        if bar.date != today:      # 当日K线缺失 → 不执行（防陈旧数据误触发）
            log.warning("清仓线跳过 %s：当日K线缺失（最新 %s）", p.symbol, bar.date)
            continue
        price, kind = exit_signal(bar, tp, sl)
        if price is None:
            continue
        line = sl if kind == "止损" else tp
        trigger = (f"盘中最低{bar.low:.2f}≤{line:.2f}" if kind == "止损"
                   else f"盘中最高{bar.high:.2f}≥{line:.2f}")
        act = TradeAction(
            action="reduce", symbol=p.symbol, name=p.name,
            shares=p.shares, price=price,
            reason=f"{kind}清仓线执行（{rule.get('note', '')}：{trigger}，按线价成交）",
        )
        marker = f"{SELL_LINE_MARK}({bar.date}@{price:.2f})"
        note = " | ".join(x for x in (p.note, marker) if x)
        store.set(p.symbol, p.name, 0, p.cost_price, price, note, account=account)
        actions.append(act)
        log.info("清仓线执行 %s(%s): 清%d股@%.2f（%s）", p.name, p.symbol,
                 p.shares, price, kind)
    return actions
