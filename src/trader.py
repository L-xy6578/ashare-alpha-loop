"""
三角闭环选股体系 — 交易员决策模块（TradeExecutor）

把交易员 SKILL.md V9.0 的风控规则数字化，让"交易员"环节真正做决策而非传声筒：

- 步骤0：数据质量门槛（good/usable → 可建仓；limited → 禁建仓仅减仓/止损；poor → 仅止损）
- 步骤1：仓位率检查（总仓位 = Σ持仓市值 / 总资产；超宏观上限 → 禁买）
- 步骤2：先减后买（超限时按"盈利最多→最少"排序减仓，每只≤50%）
- 步骤3：可用空间（上限-当前仓位）× 总资产 → 100股向下取整
- 止损：-7% 硬约束（逐持仓检查）

用法:
    from src.trader import TradeExecutor, Position

    ex = TradeExecutor(total_assets=30000, macro_cap_pct=50, stop_loss_pct=7.0)
    decision = ex.plan_buy("sh600535", price=15.78, desired_amount=5000,
                           positions=[...], quality_level="good")
    for action in decision.actions:
        print(action)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .logger import get_logger

log = get_logger(__name__)

# 数据质量等级与建仓许可
_QUALITY_BUY_ALLOWED = {"good", "usable"}
_QUALITY_REDUCE_ALLOWED = {"good", "usable", "limited"}  # poor 下仅止损

# 宏观仓位上限（%）：震荡50% / 进攻60% / 防御30% / 急跌10%
MACRO_CAP_DEFAULT = 50          # 默认按震荡市
MACRO_CAP_BY_REGIME = {
    "TREND_UP": 60,             # 进攻
    "RANGE": 50,                # 震荡
    "TREND_DOWN": 30,           # 防御
    "DEFENSIVE": 10,            # 急跌/防御
}


@dataclass(frozen=True)
class Position:
    """一条持仓"""

    symbol: str
    name: str
    shares: int
    cost_price: float       # 持仓成本价
    current_price: float    # 当前价
    frozen: bool = False    # 冻结（如停牌）


@dataclass(frozen=True)
class TradeAction:
    """一条交易动作（减仓 / 买入 / 止损）"""

    action: str             # buy / reduce / stop_loss
    symbol: str
    name: str
    shares: int
    price: float
    reason: str

    def __str__(self) -> str:
        return f"[{self.action}] {self.name}({self.symbol}) {self.shares}股@{self.price} — {self.reason}"


@dataclass
class TradeDecision:
    """plan_buy 的完整输出"""

    symbol: str
    name: str
    actions: list[TradeAction] = field(default_factory=list)
    allowed: bool = True
    reason: str = ""
    final_position_pct: float = 0.0   # 决策后预计总仓位

    @property
    def buy_shares(self) -> int:
        return sum(a.shares for a in self.actions if a.action == "buy")


class TradeExecutor:
    """交易员决策引擎（SKILL V9.0 数字化）"""

    def __init__(
        self,
        total_assets: float,
        macro_cap_pct: float | None = None,
        regime: str | None = None,
        stop_loss_pct: float = 7.0,
        max_reduce_per_position: float = 0.5,
    ):
        """
        Args:
            total_assets: 账户总资产（元）
            macro_cap_pct: 宏观仓位上限（%），优先于 regime
            regime: 市场状态（TREND_UP/RANGE/TREND_DOWN/DEFENSIVE），用于推算上限
            stop_loss_pct: 止损幅度（%），默认 -7%
            max_reduce_per_position: 单只持仓单次最大减仓比例（默认 50%）
        """
        self.total_assets = total_assets
        if macro_cap_pct is None:
            macro_cap_pct = MACRO_CAP_BY_REGIME.get(regime or "RANGE", MACRO_CAP_DEFAULT)
        self.macro_cap_pct = float(macro_cap_pct)
        self.stop_loss_pct = float(stop_loss_pct)
        self.max_reduce = max_reduce_per_position

    # ------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------

    def position_value(self, p: Position) -> float:
        return p.shares * p.current_price

    def total_position_pct(self, positions: list[Position]) -> float:
        """总仓位率 = Σ持仓市值 / 总资产 × 100"""
        total = sum(self.position_value(p) for p in positions)
        return total / self.total_assets * 100.0

    def quality_allows(self, level: str) -> tuple[bool, str]:
        """数据质量门槛：返回 (是否允许建仓, 说明)"""
        level = (level or "good").lower()
        if level in _QUALITY_BUY_ALLOWED:
            return True, f"数据质量 {level}：允许建仓"
        if level == "limited":
            return False, "⚠️ 数据质量 limited：禁止建仓，仅允许减仓/止损"
        return False, "⚠️ 数据质量 poor：仅允许止损"

    # ------------------------------------------------------------
    # 先减后买
    # ------------------------------------------------------------

    def _reduce_plan(
        self,
        positions: list[Position],
        need_reduce_pct: float,
    ) -> tuple[list[TradeAction], float]:
        """从盈利最多开始减仓，返回 (动作列表, 释放后的仓位率)

        规则：每只减仓不超过原持仓 50%；盈利优先（cost 相对 current 涨幅大者先减）；
        全亏行情下按跌幅降序（最差者优先减，#62 截断亏损原则）。
        """
        actions: list[TradeAction] = []
        current_pct = self.total_position_pct(positions)

        # #62 排序：盈利组按涨幅降序（盈利优先）；亏损组按跌幅降序（最差者优先减），
        # 避免全亏行情下"砍最不亏、留深亏"的逆向操作；冻结持仓不参与
        def _reduce_key(p):
            ret = (p.current_price - p.cost_price) / p.cost_price * 100
            return (0, ret) if ret >= 0 else (1, abs(ret))

        sorted_pos = sorted(
            [p for p in positions if not p.frozen],
            key=_reduce_key,
            reverse=True,
        )

        for p in sorted_pos:
            if current_pct <= self.macro_cap_pct - need_reduce_pct:
                break
            max_shares = int(p.shares * self.max_reduce)   # 单次最多减 50%
            if max_shares <= 0:
                continue
            # 目标释放金额：剩余需减仓位率 × 总资产
            target_release = max(0.0, current_pct - (self.macro_cap_pct - need_reduce_pct)) / 100 * self.total_assets
            release_by_price = max(1, int(target_release / p.current_price))
            shares = min(max_shares, release_by_price)
            shares = max(1, (shares // 100) * 100 or 100)  # 100 股取整（至少 100）
            shares = min(shares, max_shares)
            profit_pct = (p.current_price - p.cost_price) / p.cost_price * 100
            actions.append(TradeAction(
                action="reduce", symbol=p.symbol, name=p.name,
                shares=shares, price=p.current_price,
                reason=f"仓位超限，先减后买（收益 {profit_pct:+.2f}%，单次≤50%）",
            ))
            # 更新剩余持仓
            new_shares = p.shares - shares
            if new_shares > 0:
                positions = [pos if pos is not p else Position(
                    p.symbol, p.name, new_shares, p.cost_price, p.current_price, p.frozen) for pos in positions]
            else:
                positions = [pos for pos in positions if pos is not p]
            current_pct = self.total_position_pct(positions)

        return actions, current_pct

    # ------------------------------------------------------------
    # 核心决策
    # ------------------------------------------------------------

    def plan_buy(
        self,
        symbol: str,
        name: str,
        price: float,
        desired_amount: float,
        positions: list[Position] | None = None,
        quality_level: str = "good",
    ) -> TradeDecision:
        """建仓决策：质量门槛 → 仓位检查 → 先减后买 → 100股取整

        Args:
            symbol/name: 目标标的
            price: 拟成交价（建仓参考价）
            desired_amount: 期望投入金额（元）
            positions: 当前持仓（不含本次买入）
            quality_level: 数据质量等级 good/usable/limited/poor

        Returns:
            TradeDecision：actions 可能含 reduce（先减后买）+ buy
        """
        positions = positions or []
        decision = TradeDecision(symbol=symbol, name=name)

        # 步骤0：数据质量门槛
        ok, reason = self.quality_allows(quality_level)
        if not ok:
            decision.allowed = False
            decision.reason = reason
            log.warning("交易员拒绝建仓 %s: %s", symbol, reason)
            return decision

        # 步骤1：仓位率检查（limited 时上限×50%）
        cap = self.macro_cap_pct
        if (quality_level or "").lower() == "limited":
            cap = cap * 0.5
        current_pct = self.total_position_pct(positions)
        decision.final_position_pct = current_pct

        # 期望买入占比
        buy_pct = desired_amount / self.total_assets * 100.0

        # 步骤2：超限 → 先减后买
        if current_pct + buy_pct > cap:
            need_reduce = current_pct + buy_pct - cap
            # #63：需减比例超过仓位上限 → 减仓也无法容纳目标买入，直接拒绝
            if need_reduce > cap:
                decision.allowed = False
                decision.reason = (f"目标买入金额超上限：需先减 {need_reduce:.1f}% "
                                   f"（> 仓位上限 {cap:.0f}%），减仓也无法容纳，拒绝建仓")
                log.warning("交易员拒绝建仓 %s: %s", symbol, decision.reason)
                return decision
            reduce_actions, after_pct = self._reduce_plan(list(positions), need_reduce)
            decision.actions.extend(reduce_actions)
            current_pct = after_pct

        # 步骤3：可用空间 → 100 股取整
        avail_amount = max(0.0, (cap - current_pct) / 100.0 * self.total_assets)
        buy_amount = min(desired_amount, avail_amount)
        shares = int(buy_amount // (price * 100) * 100)  # 100 股向下取整
        if shares <= 0:
            decision.allowed = False
            decision.reason = f"可用空间不足（仓位 {current_pct:.1f}% / 上限 {cap:.0f}%）"
            log.warning("交易员拒绝建仓 %s: %s", symbol, decision.reason)
            return decision

        decision.actions.append(TradeAction(
            action="buy", symbol=symbol, name=name, shares=shares, price=price,
            reason=f"可用空间 {avail_amount:.0f} 元，按 100 股取整",
        ))
        decision.final_position_pct = current_pct + shares * price / self.total_assets * 100.0
        decision.reason = f"建仓 {shares} 股（{shares * price:.0f} 元），决策后仓位 {decision.final_position_pct:.1f}%"
        log.info("交易员建仓决策 %s(%s): %d 股 @ %.2f → 仓位 %.1f%%",
                 name, symbol, shares, price, decision.final_position_pct)
        return decision

    # ------------------------------------------------------------
    # 止损检查
    # ------------------------------------------------------------

    def check_stop_loss(self, positions: list[Position]) -> list[TradeAction]:
        """逐持仓检查 -7% 止损，返回触发止损的动作列表"""
        actions = []
        for p in positions:
            if p.frozen:
                continue
            loss_pct = (p.current_price - p.cost_price) / p.cost_price * 100.0
            if loss_pct <= -self.stop_loss_pct:
                actions.append(TradeAction(
                    action="stop_loss", symbol=p.symbol, name=p.name,
                    shares=p.shares, price=p.current_price,
                    reason=f"触发止损（-{self.stop_loss_pct:.0f}%，当前 {loss_pct:+.2f}%）",
                ))
                log.warning("止损触发 %s: %+2.2f%%", p.name, loss_pct)
        return actions

    def plan_reduce(
        self,
        symbol: str,
        name: str,
        price: float,
        current_shares: int,
        reduce_ratio: float = 0.5,
        reason: str = "止盈/调仓减仓",
    ) -> TradeAction:
        """主动减仓（如涨停减仓、止盈），默认减 50%"""
        shares = int(current_shares * reduce_ratio)
        shares = max(100, (shares // 100) * 100)
        shares = min(shares, current_shares)
        return TradeAction(
            action="reduce", symbol=symbol, name=name,
            shares=shares, price=price, reason=reason,
        )
