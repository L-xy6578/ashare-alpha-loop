"""
决策单模拟成交与结算（15:35 收盘执行）

口径（2026-11-06 用户拍板）：
- 次日若价格进入伏击区间，按区间成交价模拟买入；未进入则挂单等待
- 3 个交易日未成交 → 决策作废
- 止损：结构止损 / -6% 硬止损取更近者；跳空低开按开盘价（风险如实记录）
- +8% 卖 1/3；+15% 再卖 1/3；余仓跌破 10 日线或自最高点回撤 8% 离场
- 卖出所得回到现金池；后续成交按"已在区间内优先、评分高优先"排序
- 费用：佣金万三（最低 5 元）、卖出印花税 0.05%、过户费 0.001% 双边
- 同日重复运行不重复成交/平仓（决策状态机 + trade 唯一键）
- dry-run 不写库

与现有 sim/trim 规则的关系：
- 决策单持仓在 positions.note 中带 "VCP决策单" 标记；r47 的旧 -7% 止损、
  策略减仓、止盈线等只作用于无标记的历史持仓，避免双重出场。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date

from src.config import MARKET_DB_PATH
from src.decision_log import (DECISION_MARK, MAX_POSITIONS, MAX_TOTAL_AMOUNT,
                              MIN_CASH, DecisionStore, calc_fees)
from src.executable import MAX_POSITION_AMOUNT, max_shares_for_position
from src.portfolio import PortfolioStore
from src.vcp import TRAIL_DRAWDOWN_PCT, TRAIL_MA_DAYS, trim_shares

ADD_VOLUME_RATIO = 1.5  # 加仓放量阈值：≥ 5 日均量 1.5 倍


@dataclass
class SettleResult:
    buys: list[dict] = field(default_factory=list)
    sells: list[dict] = field(default_factory=list)
    expired: list[dict] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)

    @property
    def has_actions(self) -> bool:
        return bool(self.buys or self.sells or self.expired)


def is_managed(position) -> bool:
    return DECISION_MARK in ((position.note if position else "") or "")


def _note(plan: dict, action: str, price: float, extra: str = "") -> str:
    return f"{DECISION_MARK} {action}@{price:.2f} {extra}".strip()


def _holding_days(entry_date: str, exit_date: str,
                  market_db: str = MARKET_DB_PATH) -> int:
    try:
        with sqlite3.connect(market_db, timeout=5) as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM trade_calendar WHERE is_trading=1 "
                "AND date>? AND date<=?", (entry_date, exit_date),
            ).fetchone()[0]
        return int(n)
    except Exception:
        return max(0, (date.fromisoformat(exit_date) - date.fromisoformat(entry_date)).days)


def _avg(values) -> float:
    vals = [float(v) for v in values if v]
    return sum(vals) / len(vals) if vals else 0.0


def _position_count(portfolio: PortfolioStore) -> int:
    return len([p for p in portfolio.holding("sim") if p.shares > 0])


def _total_cost(portfolio: PortfolioStore) -> float:
    return sum(p.shares * p.cost_price for p in portfolio.holding("sim"))


def _sell(
    store: DecisionStore,
    portfolio: PortfolioStore,
    plan: dict,
    shares: int,
    price: float,
    today: str,
    reason: str,
) -> dict | None:
    shares = min(shares, int(plan["shares_remaining"]))
    if shares <= 0:
        return None
    amount = round(shares * price, 2)
    fees = calc_fees("SELL", amount)
    remaining = int(plan["shares_remaining"]) - shares
    holding_days = _holding_days(plan["entry_date"], today)
    ret = round((price - plan["entry_price"]) / plan["entry_price"] * 100, 2) \
        if plan["entry_price"] else 0.0
    pnl = round(amount - fees["total_fee"] - shares * plan["entry_price"], 2)
    store.add_trade(
        decision_date=plan["decision_date"], symbol=plan["symbol"], name=plan["name"],
        trade_date=today, side="SELL", shares=shares, price=round(price, 2),
        amount=amount, **fees, reason=reason, holding_days=holding_days,
        return_pct=ret, pnl=pnl,
    )
    note = _note(plan, "清仓" if remaining == 0 else f"减仓{shares}股", price,
                 reason)
    portfolio.set(plan["symbol"], plan["name"], remaining, plan["entry_price"],
                  price, note, account="sim", entry_date=plan["entry_date"])
    _, cash = store.cash()
    store.set_cash(cash + amount - fees["total_fee"])
    plan["shares_remaining"] = remaining
    return {"symbol": plan["symbol"], "name": plan["name"], "shares": shares,
            "price": round(price, 2), "amount": amount, "fee": fees["total_fee"],
            "reason": reason, "pnl": pnl, "return_pct": ret,
            "holding_days": holding_days,
            # 成本基准（加仓后为摊薄成本）与实际入场日，供知识库回写复算收益率
            "cost": plan["entry_price"], "entry_date": plan["entry_date"]}


def _save_plan(store: DecisionStore, plan: dict, today: str,
               status: str | None = None) -> None:
    store.upsert_plan(
        symbol=plan["symbol"], decision_date=plan["decision_date"],
        name=plan["name"], entry_date=plan["entry_date"],
        entry_price=plan["entry_price"], shares_total=plan["shares_total"],
        shares_remaining=plan["shares_remaining"],
        tier1_done=plan.get("tier1_done", 0), tier2_done=plan.get("tier2_done", 0),
        tp1=plan.get("tp1"), tp2=plan.get("tp2"), stop_price=plan["stop_price"],
        box_high=plan.get("box_high"), box_low=plan.get("box_low"),
        peak_price=plan.get("peak_price", plan["entry_price"]),
        status=status or plan.get("status", "OPEN"),
        close_reason=plan.get("close_reason", ""),
    )
    plan["updated_at"] = today


def _close_if_flat(store: DecisionStore, plan: dict, today: str, reason: str) -> bool:
    """股数归零 → plan/log 双双置 CLOSED 并写明平仓原因（防样本消失/仓位复活）"""
    if int(plan["shares_remaining"]) > 0:
        return False
    plan["status"] = "CLOSED"
    plan["close_reason"] = reason
    _save_plan(store, plan, today, status="CLOSED")
    store.update_log_status(plan["decision_date"], plan["symbol"], "CLOSED")
    return True


def _process_open_plan(store: DecisionStore, portfolio: PortfolioStore,
                       plan: dict, bar, bars, today: str,
                       result: SettleResult) -> None:
    # 已 CLOSED 的计划绝不再结算/加仓（防止清空后又被放量突破"复活"）
    if plan.get("status") != "OPEN":
        return
    if bar is None or bar.date != today:
        result.skipped.append(f"{plan['name']}({plan['symbol']}) 当日无行情，跳过结算")
        return
    # T+1：建仓当日不能卖出
    if plan["entry_date"] == today:
        plan["peak_price"] = max(plan.get("peak_price", bar.high), bar.high)
        _save_plan(store, plan, today)
        return

    entry = float(plan["entry_price"])
    peak_before = max(float(plan.get("peak_price", entry)), bar.high)

    # 1) 止损优先（保守：同日既触止损又触止盈时按止损处理）
    stop = float(plan["stop_price"])
    if bar.low <= stop:
        fill = stop if bar.open >= stop else bar.open
        reason = "止损（结构/硬止损更近者）" if bar.open >= stop else "止损（跳空低开按开盘价）"
        sold = _sell(store, portfolio, plan, plan["shares_remaining"], fill, today, reason)
        if sold:
            result.sells.append(sold)
            plan["status"] = "CLOSED"
            plan["close_reason"] = reason
            plan["peak_price"] = peak_before
            _save_plan(store, plan, today, status="CLOSED")
            store.update_log_status(plan["decision_date"], plan["symbol"], "CLOSED")
        return

    # 2) 分批止盈 +8% / +15%
    tiers = trim_shares(int(plan["shares_total"]))
    if not plan.get("tier1_done") and tiers["tier1"] > 0 and bar.high >= float(plan["tp1"]):
        sold = _sell(store, portfolio, plan, tiers["tier1"], float(plan["tp1"]),
                     today, "止盈+8%（卖1/3）")
        if sold:
            result.sells.append(sold)
            plan["tier1_done"] = 1
            plan["peak_price"] = peak_before
            if _close_if_flat(store, plan, today, "分批止盈卖光（+8% 卖1/3 后无剩余仓位）"):
                return
    if (plan.get("tier1_done") and not plan.get("tier2_done")
            and tiers["tier2"] > 0 and bar.high >= float(plan["tp2"])):
        sold = _sell(store, portfolio, plan, tiers["tier2"], float(plan["tp2"]),
                     today, "止盈+15%（再卖1/3）")
        if sold:
            result.sells.append(sold)
            plan["tier2_done"] = 1
            plan["peak_price"] = peak_before
            if _close_if_flat(store, plan, today, "分批止盈卖光（+15% 再卖1/3 后无剩余仓位）"):
                return

    # 3) 剩余仓位移动止损：跌破 10 日线，或自最高点回撤 8%
    if plan["shares_remaining"] > 0 and plan.get("tier1_done") and plan.get("tier2_done"):
        closes = [b.close for b in bars if b.date <= today]
        ma10 = _avg(closes[-TRAIL_MA_DAYS:]) if len(closes) >= TRAIL_MA_DAYS else None
        drawdown = ((peak_before - bar.close) / peak_before * 100) if peak_before else 0.0
        hit_ma = ma10 is not None and bar.close < ma10
        hit_dd = drawdown >= TRAIL_DRAWDOWN_PCT
        if hit_ma or hit_dd:
            why = []
            if hit_ma:
                why.append(f"跌破{TRAIL_MA_DAYS}日线({ma10:.2f})")
            if hit_dd:
                why.append(f"自高点回撤{drawdown:.1f}%")
            sold = _sell(store, portfolio, plan, plan["shares_remaining"], bar.close,
                         today, "移动止损：" + "；".join(why))
            if sold:
                result.sells.append(sold)
                plan["status"] = "CLOSED"
                plan["close_reason"] = "移动止损：" + "；".join(why)
                plan["peak_price"] = peak_before
                _save_plan(store, plan, today, status="CLOSED")
                store.update_log_status(plan["decision_date"], plan["symbol"], "CLOSED")
                return

    # 4) 放量突破箱体上沿：每次加仓 1 手，累计不超 6,000 元
    if plan["status"] == "OPEN" and plan.get("box_high"):
        vol5 = _avg([b.volume for b in bars if b.date <= today][-6:-1])
        max_shares = max_shares_for_position(bar.close)
        can_add = (int(plan["shares_total"]) + 100 <= max_shares
                   and int(plan["shares_remaining"]) + 100 <= max_shares
                   and bar.close > float(plan["box_high"])
                   and vol5 > 0 and bar.volume >= ADD_VOLUME_RATIO * vol5)
        if can_add:
            amount = round(100 * bar.close, 2)
            fees = calc_fees("BUY", amount)
            _, cash = store.cash()
            if amount <= MAX_POSITION_AMOUNT and cash - amount - fees["total_fee"] >= MIN_CASH:
                store.add_trade(
                    decision_date=plan["decision_date"], symbol=plan["symbol"],
                    name=plan["name"], trade_date=today, side="BUY", shares=100,
                    price=round(bar.close, 2), amount=amount, **fees,
                    reason="放量突破箱体上沿加仓1手",
                )
                old_shares = int(plan["shares_total"])
                new_cost = round((old_shares * plan["entry_price"] + amount
                                  + fees["total_fee"]) / (old_shares + 100), 3)
                # 成本基准回写摊薄成本：后续 ret/pnl 与组合成本都以它为准。
                # tp1/tp2/stop_price 仍锚首次建仓价，加仓不重算（冻结期口径，未获批不改）。
                plan["entry_price"] = new_cost
                plan["shares_total"] = old_shares + 100
                plan["shares_remaining"] = int(plan["shares_remaining"]) + 100
                store.set_cash(cash - amount - fees["total_fee"])
                portfolio.set(plan["symbol"], plan["name"],
                              plan["shares_remaining"], new_cost, bar.close,
                              _note(plan, "加仓100股", bar.close, "放量突破箱体上沿"),
                              account="sim", entry_date=plan["entry_date"])
                result.buys.append({"symbol": plan["symbol"], "name": plan["name"],
                                    "shares": 100, "price": round(bar.close, 2),
                                    "amount": amount, "fee": fees["total_fee"],
                                    "reason": "加仓（放量突破箱体上沿）"})

    plan["peak_price"] = peak_before
    _save_plan(store, plan, today)


def _try_fill(store: DecisionStore, portfolio: PortfolioStore, d: dict,
              bar, today: str, result: SettleResult) -> bool:
    """尝试按伏击区间成交；返回是否成交"""
    if bar is None or bar.date != today:
        result.skipped.append(f"{d['name']}({d['symbol']}) 当日无行情，挂单顺延")
        return False
    low, high = float(d["zone_low"]), float(d["zone_high"])
    if bar.open < low:
        result.skipped.append(
            f"{d['name']}({d['symbol']}) 开盘 {bar.open:.2f} 跌破伏击下沿 {low:.2f}，不追")
        return False
    if bar.open <= high:
        fill = bar.open
    elif bar.low <= high:
        fill = high
    else:
        result.skipped.append(
            f"{d['name']}({d['symbol']}) 最低 {bar.low:.2f} 未进入区间 {low:.2f}~{high:.2f}，继续等待")
        return False
    shares = int(d["shares"])
    if shares <= 0:
        result.skipped.append(f"{d['name']}({d['symbol']}) 计划股数为0，无法成交")
        return False

    # 容量检查：最多 3 只 / 单只 ≤6,000 / 总仓 ≤18,000 / 现金 ≥12,000
    amount = round(shares * fill, 2)
    fees = calc_fees("BUY", amount)
    _, cash = store.cash()
    count = _position_count(portfolio)
    total = _total_cost(portfolio)
    reasons = []
    if portfolio.get(d["symbol"]) is not None and portfolio.get(d["symbol"]).shares > 0:
        reasons.append("已持仓")
    if amount > MAX_POSITION_AMOUNT:
        reasons.append(f"单只金额 {amount:.0f} > {MAX_POSITION_AMOUNT:.0f}")
    if count >= MAX_POSITIONS:
        reasons.append(f"持仓已达 {MAX_POSITIONS} 只")
    if total + amount > MAX_TOTAL_AMOUNT:
        reasons.append(f"总仓位将超过 {MAX_TOTAL_AMOUNT:.0f} 元")
    if cash - amount - fees["total_fee"] < MIN_CASH:
        reasons.append(f"成交后现金将低于 {MIN_CASH:.0f} 元")
    if reasons:
        result.skipped.append(f"{d['name']}({d['symbol']}) 容量不足：{'；'.join(reasons)}")
        return False

    # 实际成交后按成交价重算止损/止盈（结构止损沿用决策时的箱体下沿计算）
    structural = float(d.get("stop_structural") or (low * 0.98))
    stop = max(structural, round(fill * 0.94, 2))
    max_stop = round(fill - 450.0 / shares, 2)
    stop = max(stop, max_stop)
    tp1 = round(fill * 1.08, 2)
    tp2 = round(fill * 1.15, 2)

    store.add_trade(
        decision_date=d["decision_date"], symbol=d["symbol"], name=d["name"],
        trade_date=today, side="BUY", shares=shares, price=round(fill, 2),
        amount=amount, **fees, reason="伏击区间成交（首仓）",
    )
    store.set_cash(cash - amount - fees["total_fee"])
    portfolio.set(d["symbol"], d["name"], shares, round(fill, 2), round(fill, 2),
                  _note({"decision_date": d["decision_date"], "symbol": d["symbol"]},
                        "建仓", fill, f"伏击区间{low:.2f}~{high:.2f}"),
                  account="sim", entry_date=today)
    store.upsert_plan(
        symbol=d["symbol"], decision_date=d["decision_date"], name=d["name"],
        entry_date=today, entry_price=round(fill, 2), shares_total=shares,
        shares_remaining=shares, tier1_done=0, tier2_done=0, tp1=tp1, tp2=tp2,
        stop_price=stop, box_high=d.get("box_high"), box_low=d.get("box_low"),
        peak_price=round(fill, 2), status="OPEN",
    )
    store.update_log_status(d["decision_date"], d["symbol"], "FILLED",
                            fill_date=today, fill_price=round(fill, 2),
                            filled_shares=shares)
    result.buys.append({"symbol": d["symbol"], "name": d["name"], "shares": shares,
                        "price": round(fill, 2), "amount": amount,
                        "fee": fees["total_fee"],
                        "reason": f"伏击区间成交 {low:.2f}~{high:.2f}"})
    return True


def settle(
    store: DecisionStore,
    portfolio: PortfolioStore,
    klines: dict,
    today: str,
    *,
    dry_run: bool = False,
) -> SettleResult:
    """执行当日结算（幂等）；dry_run 只返回提示，不写库"""
    result = SettleResult()
    if dry_run:
        result.lines.append("dry-run：跳过模拟成交与结算（不写库）")
        return result

    # 1) 已有持仓：止损 / 分批止盈 / 移动止损 / 加仓
    for plan in store.open_plans():
        bars = klines.get(plan["symbol"]) or []
        bar = next((b for b in bars if b.date == today), None)
        _process_open_plan(store, portfolio, plan, bar, bars, today, result)

    # 2) 待成交决策：按"已在区间内优先、评分高优先、决策日期早优先"排序
    pending = store.pending(today)
    def _priority(d):
        bars = klines.get(d["symbol"]) or []
        bar = next((b for b in bars if b.date == today), None)
        in_zone = bool(bar and float(d["zone_low"]) <= bar.open <= float(d["zone_high"]))
        return (0 if in_zone else 1, -(d.get("score") or 0), d["decision_date"])
    for d in sorted(pending, key=_priority):
        bars = klines.get(d["symbol"]) or []
        bar = next((b for b in bars if b.date == today), None)
        filled = _try_fill(store, portfolio, d, bar, today, result)
        if (not filled and d.get("expire_date") and today >= d["expire_date"]
                and bar is not None and bar.date == today):
            store.update_log_status(d["decision_date"], d["symbol"], "EXPIRED")
            result.expired.append({"symbol": d["symbol"], "name": d["name"],
                                   "reason": f"{d['expire_date']} 前未进入伏击区间"})

    # 3) 汇总文本
    for b in result.buys:
        result.lines.append(
            f"- 买入 {b['name']}({b['symbol']}) {b['shares']}股 @{b['price']:.2f}"
            f"（费用 {b['fee']:.2f}）— {b['reason']}")
    for s in result.sells:
        result.lines.append(
            f"- 卖出 {s['name']}({s['symbol']}) {s['shares']}股 @{s['price']:.2f}"
            f"（费用 {s['fee']:.2f}，持有{s['holding_days']}日，"
            f"本笔 {s['pnl']:+.2f} 元 / {s['return_pct']:+.2f}%）— {s['reason']}")
    for e in result.expired:
        result.lines.append(f"- 作废 {e['name']}({e['symbol']})：{e['reason']}")
    for s in result.skipped:
        result.lines.append(f"- 等待/跳过：{s}")
    return result
