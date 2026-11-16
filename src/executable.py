"""
可执行性前置过滤（价格上限 / 板块类型 / ST / 次新 / 手数）

2026-11-06 用户拍板口径：
- 股价 ≤ 60 元/股（一手 100 股 ≤ 6,000 元 = 单只 20% 仓位上限）
- 单只总仓上限 6,000 元；最多同时持有 3 只；总仓位 ≤ 18,000 元；常留现金 ≥ 12,000
- 首仓 1 手；若一手金额 < 1,500 元则首仓 2 手；加仓每次 1 手，累计不超 6,000
- 排除科创板（688）、北交所（8/920 开头）、ST、停牌、上市不足 60 个交易日

本模块只做"能不能买、买多少手"的判定，不参与评分与排序；
筛选与决策记录共用同一实现，避免两套口径漂移。

用法:
    from src.executable import check_eligibility, filter_universe

    e = check_eligibility("sh600019", "600019", "宝钢股份", price=6.11,
                          listed_days=200, bars=bars, as_of="2026-11-06")
    if e.ok:
        print(e.first_shares, e.max_shares, e.lot_amount)
"""

from __future__ import annotations

from dataclasses import dataclass

# ----- 用户拍板参数（冻结期硬约束，见 AGENTS.md 第 6 节） -----
MAX_PRICE = 60.0                 # 元/股
LOT_SIZE = 100                   # 一手 100 股
MAX_POSITION_AMOUNT = 6000.0     # 单只总仓上限
MAX_POSITIONS = 3                # 最多同时持有
MAX_TOTAL_AMOUNT = 18000.0       # 总仓位上限（60% × 30,000）
MIN_CASH = 12000.0               # 常留现金下限
FIRST_LOT_AMOUNT_THRESHOLD = 1500.0   # 一手金额 < 1,500 → 首仓 2 手
MIN_LISTED_TRADING_DAYS = 60     # 上市不足 60 个交易日排除

# 交易权限硬约束：只能买主板（2026-11-06 用户明确授权范围）
BUYABLE_BOARDS = {"沪主板", "深主板"}
ANALYSIS_ONLY_BOARDS = {"创业板", "科创板", "北交所"}


@dataclass(frozen=True)
class Eligibility:
    """单只标的的可执行性判定结果"""

    symbol: str
    code: str
    name: str
    ok: bool
    reason: str = ""
    board: str = ""              # 沪主板 / 深主板 / 创业板 / 科创板 / 北交所
    price: float = 0.0
    lot_amount: float = 0.0      # 一手金额
    first_shares: int = 0        # 首仓股数（1~2 手）
    max_shares: int = 0          # 单只上限内最多股数
    analysis_only: bool = False  # True = 仅分析不可买（板块权限限制）

    @property
    def excluded(self) -> bool:
        return not self.ok


def board_of(code: str, symbol: str = "") -> str:
    """按代码判定板块（不依赖名称，避免简称误判）

    可买：沪主板 600/601/603/605、深主板 000/001/002/003
    不可买：创业板 300/301、科创板 688/689、北交所 8xx/920
    """
    c = (code or "").strip()
    if not c and symbol:
        c = symbol[2:]
    if c.startswith(("688", "689")):
        return "科创板"
    if c.startswith(("8", "920", "4")):
        return "北交所"
    if c.startswith(("300", "301")):
        return "创业板"
    if c.startswith(("600", "601", "603", "605")):
        return "沪主板"
    if c.startswith(("000", "001", "002", "003")):
        return "深主板"
    return "未知"


def is_buyable_code(code: str, symbol: str = "") -> tuple[bool, str]:
    """板块权限硬过滤：返回 (是否可买, 原因)

    这是"可买入候选"的唯一入口判定；分析用途可以保留，但不得进入买入候选。
    """
    board = board_of(code, symbol)
    if board in ANALYSIS_ONLY_BOARDS:
        return False, f"{board}（{code}）仅分析不可买（用户只能买主板）"
    if board not in BUYABLE_BOARDS:
        return True, ""   # 未知代码不主动拦截，交由 ST/价格/次新等规则判定
    return True, ""


def is_st_name(name: str) -> bool:
    """ST / *ST / 退市整理（名称含 ST 或 退）"""
    n = (name or "").upper()
    return "ST" in n or "退" in n


def first_lot_shares(price: float) -> int:
    """首仓股数：默认 1 手；一手金额 < 1,500 元 → 2 手（仍受单只 6,000 上限约束）"""
    if price <= 0:
        return 0
    lot_amount = price * LOT_SIZE
    shares = LOT_SIZE * 2 if lot_amount < FIRST_LOT_AMOUNT_THRESHOLD else LOT_SIZE
    return min(shares, max_shares_for_position(price))


def max_shares_for_position(price: float) -> int:
    """单只 6,000 元上限内可持有的最大股数（100 股整数倍）"""
    if price <= 0:
        return 0
    lots = int(MAX_POSITION_AMOUNT // (price * LOT_SIZE))
    return max(0, lots * LOT_SIZE)


def check_eligibility(
    symbol: str,
    code: str,
    name: str,
    price: float,
    *,
    bars=None,
    listed_days: int | None = None,
    as_of: str | None = None,
    suspended: bool = False,
) -> Eligibility:
    """单只标的可执行性判定（不通过时给出可读原因，绝不静默丢弃）

    Args:
        symbol: 带市场前缀代码（sh600019）
        code: 6 位裸代码
        name: 证券简称
        price: 拟参考价（通常为最新收盘价）
        bars: 日K列表（用于停牌/替换校验，可为 None）
        listed_days: 已上市交易日数（来自本地历史K线总数）；None 时按 bars 长度推断
        as_of: 决策数据截止日；提供时要求最新K线日期等于 as_of（否则视为停牌/数据缺失）
        suspended: 外部停牌标记
    """
    board = board_of(code, symbol)
    base = dict(symbol=symbol, code=code, name=name, board=board, price=float(price or 0))

    if board in ANALYSIS_ONLY_BOARDS:
        return Eligibility(
            ok=False,
            reason=f"{board}（{code}）仅分析不可买（用户只能买主板）",
            analysis_only=True,
            **base,
        )

    if is_st_name(name):
        return Eligibility(ok=False, reason="ST/退市整理标的", **base)

    if suspended:
        return Eligibility(ok=False, reason="停牌", **base)

    if not price or price <= 0:
        return Eligibility(ok=False, reason="无有效价格（停牌或行情缺失）", **base)

    if bars is not None:
        if not bars:
            return Eligibility(ok=False, reason="无日K数据（停牌或行情缺失）", **base)
        if as_of and bars[-1].date != as_of:
            return Eligibility(
                ok=False,
                reason=f"停牌/当日无行情（最新K线 {bars[-1].date}，数据截止 {as_of}）",
                **base,
            )

    if price > MAX_PRICE:
        lot_amount = price * LOT_SIZE
        return Eligibility(
            ok=False,
            reason=(f"股价 {price:.2f} > {MAX_PRICE:.0f} 元"
                    f"（一手 {lot_amount:.0f} 元 > 单只上限 {MAX_POSITION_AMOUNT:.0f} 元）"),
            **base,
        )

    lots_available = max_shares_for_position(price)
    if lots_available <= 0:
        return Eligibility(
            ok=False,
            reason=f"一手 {price * LOT_SIZE:.0f} 元 > 单只上限 {MAX_POSITION_AMOUNT:.0f} 元",
            **base,
        )

    days = listed_days
    if days is None:
        days = len(bars) if bars is not None else None
    if days is not None and days < MIN_LISTED_TRADING_DAYS:
        return Eligibility(
            ok=False,
            reason=(f"上市不足 {MIN_LISTED_TRADING_DAYS} 个交易日"
                    f"（本地历史仅 {days} 根，无法确认非次新）"),
            **base,
        )

    return Eligibility(
        symbol=symbol, code=code, name=name, ok=True, board=board,
        price=float(price), lot_amount=round(price * LOT_SIZE, 2),
        first_shares=first_lot_shares(price), max_shares=lots_available,
    )


def filter_universe(
    items,
    bars_by_symbol: dict,
    *,
    as_of: str,
    listed_days_by_symbol: dict[str, int] | None = None,
    suspended_symbols: set[str] | None = None,
) -> tuple[list[tuple[object, Eligibility]], list[Eligibility]]:
    """对观察池做前置过滤，返回 (可执行列表, 排除列表)

    Args:
        items: 具备 symbol/code/name 属性的观察池对象（WatchItem）
        bars_by_symbol: {symbol: [KlineBar, ...]}
        as_of: 决策数据截止日
        listed_days_by_symbol: {symbol: 上市交易日数}
        suspended_symbols: 外部停牌集合
    """
    listed_days_by_symbol = listed_days_by_symbol or {}
    suspended_symbols = suspended_symbols or set()
    eligible: list[tuple[object, Eligibility]] = []
    excluded: list[Eligibility] = []
    for item in items:
        bars = bars_by_symbol.get(item.symbol, [])
        price = bars[-1].close if bars else 0.0
        e = check_eligibility(
            item.symbol, item.code, item.name, price,
            bars=bars, listed_days=listed_days_by_symbol.get(item.symbol),
            as_of=as_of, suspended=item.symbol in suspended_symbols,
        )
        (eligible if e.ok else excluded).append(
            (item, e) if e.ok else e
        )
    return eligible, excluded
