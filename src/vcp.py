"""
VCP 波动收缩识别 + 伏击区间 / 止损止盈计划

用户拍板口径（2026-11-06）：
- 首仓触发：五因子 ≥70 且 VCP 至少两次收缩，最后一次收缩量能 ≤ 箱体整体均量的 50%
  （2026-11-09 冻结期 bug 修复：原为「前 5 日均量」，分母与收缩期高度重叠导致零通过，
  详见 docs/ARCHITECTURE.md），
  回撤逐次变浅（box 高度递减），且板块 20 日涨幅处于中上位置（非最热头部）
- 伏击区间：由箱体下沿与 20 日均线推导（见 derive_zone 的说明）
- 止损：结构止损（箱体下沿或近 20 日低点下方 2%）与硬止损（入场价 -6%）取更近者，
  且单只最大亏损不超过本金 1.5%（450 元）
- 止盈：+8% 卖 1/3；+15% 再卖 1/3；余仓跌破 10 日线或自最高点回撤 8% 离场

本模块只做识别与计划计算，不读写数据库、不臆造行情。

用法:
    from src.vcp import detect_vcp, derive_zone, build_risk_plan

    v = detect_vcp(bars)
    if v.is_vcp:
        zone = derive_zone(v, bars)
        risk = build_risk_plan(zone["zone_high"], zone, shares=200, bars=bars)
"""

from __future__ import annotations

from dataclasses import dataclass

MIN_CONTRACTIONS = 2
VOL_RATIO_MAX = 0.5          # 最后一次收缩均量 / 箱体整体均量（2026-11-09 起）
MIN_DEPTH_PCT = 0.5          # 单次收缩最小回撤幅度，过滤噪声
SECTOR_PERCENTILE_MIN = 50.0  # 板块 20 日涨幅分位下限（中位以上）
SECTOR_PERCENTILE_MAX = 85.0  # 上限（剔除最热头部 15%）
STOP_LOSS_PCT = 0.06         # 硬止损 -6%
MAX_LOSS_PER_STOCK = 450.0   # 本金 1.5%
TP1_PCT, TP2_PCT = 8.0, 15.0
TRAIL_MA_DAYS = 10
TRAIL_DRAWDOWN_PCT = 8.0


@dataclass(frozen=True)
class VcpResult:
    """VCP 识别结果（所有字段可审计）"""

    is_vcp: bool
    contractions: int                 # 有效收缩次数
    depths: tuple[float, ...]         # 各次收缩幅度（%）
    box_high: float
    box_low: float
    last_vol_ratio: float | None      # 最后一次收缩均量 / 箱体整体均量（2026-11-09 起）
    ma20: float
    low20: float
    reasons: tuple[str, ...] = ()     # 未满足条件的具体原因
    bar_count: int = 0

    @property
    def box_height_pct(self) -> float:
        return round((self.box_high - self.box_low) / self.box_high * 100, 2) \
            if self.box_high else 0.0


def _mean(values) -> float | None:
    vals = [float(v) for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _find_pivot_highs(bars, span: int = 2) -> list[int]:
    """局部高点索引：high[i] 为 ±span 窗口内唯一最高点"""
    n = len(bars)
    pivots: list[int] = []
    for i in range(span, n - span):
        window = [bars[j].high for j in range(i - span, i + span + 1)]
        h = bars[i].high
        if h == max(window) and sum(1 for v in window if v == h) == 1:
            # 避免连续相邻的伪双顶：与前一个 pivot 至少间隔 2 根
            if not pivots or i - pivots[-1] >= 2:
                pivots.append(i)
    return pivots


def detect_vcp(
    bars,
    *,
    lookback: int = 60,
    pivot_span: int = 2,
    min_contractions: int = MIN_CONTRACTIONS,
    vol_ratio_max: float = VOL_RATIO_MAX,
) -> VcpResult:
    """识别 VCP 波动收缩（输入日 K，输出可单测的结构化结果）

    算法：
    1. 取最近 lookback 根 K 线，找出局部高点（pivot）。
    2. 相邻两个 pivot 之间为一次收缩：回撤幅度 = (pivot 高点 - 区间最低) / pivot 高点。
    3. 最后一个 pivot 之后若仍有 ≥3 根 K 线，视为"当前收缩"（最后一段）。
    4. 至少 2 次有效收缩（幅度 ≥0.5%），且回撤逐次变浅（后一次 < 前一次）。
    5. 最后一次收缩的日均量 ≤ 箱体整体均量（首个有效收缩段起点至今）的 50%。
       箱体左边界被 lookback 截断时判缺失，不回退到可见部分。
    """
    n_all = len(bars)
    bars = list(bars)[-lookback:] if n_all > lookback else list(bars)
    n = len(bars)
    reasons: list[str] = []

    if n < 10:
        return VcpResult(False, 0, (), 0.0, 0.0, None, 0.0, 0.0,
                         ("K线不足10根，无法识别VCP",), n)

    closes = [b.close for b in bars]
    ma20 = round(_mean(closes[-20:]) or closes[-1], 2)
    low20 = round(min(b.low for b in bars[-20:]), 2)
    pivots = _find_pivot_highs(bars, pivot_span)

    segments: list[tuple[int, int, float, float, float]] = []
    # (start_idx, end_idx(不含), high, low, depth_pct)
    for a, b in zip(pivots, pivots[1:]):
        seg = bars[a:b + 1]
        hi = max(x.high for x in seg)
        lo = min(x.low for x in seg)
        depth = (hi - lo) / hi * 100 if hi else 0.0
        if depth >= MIN_DEPTH_PCT:
            segments.append((a, b + 1, hi, lo, depth))

    # 最后一段：最后一个 pivot 到末尾（要求至少 3 根，避免单根噪声）
    if pivots:
        start = pivots[-1]
        if n - start >= 3:
            seg = bars[start:]
            hi = max(x.high for x in seg)
            lo = min(x.low for x in seg)
            depth = (hi - lo) / hi * 100 if hi else 0.0
            if depth >= MIN_DEPTH_PCT:
                segments.append((start, n, hi, lo, depth))

    depths = tuple(round(s[4], 2) for s in segments)
    contractions = len(segments)
    if contractions < min_contractions:
        reasons.append(f"收缩次数不足（{contractions} < {min_contractions}）")

    if segments:
        box_high, box_low = round(segments[-1][2], 2), round(segments[-1][3], 2)
    else:
        box_high = round(max(b.high for b in bars[-20:]), 2)
        box_low = round(min(b.low for b in bars[-20:]), 2)

    # 回撤逐次变浅：最后 3 段（或全部）严格递减
    check = segments[-3:] if len(segments) >= 3 else segments
    shrinking = len(check) >= 2 and all(
        check[i + 1][4] < check[i][4] for i in range(len(check) - 1)
    )
    if contractions >= min_contractions and not shrinking:
        reasons.append("回撤未逐次变浅（box 高度未递减）")

    last_ratio: float | None = None
    if segments:
        start, end = segments[-1][0], segments[-1][1]
        vol_last = _mean(b.volume for b in bars[start:end])
        box_start = segments[0][0]          # 箱体左边界 = 首个有效收缩段起点
        vol_box = _mean(b.volume for b in bars[box_start:])
        # 2026-11-09 冻结期 bug 修复：分母由「前 5 日均量」改为「箱体整体均量」。
        # 旧口径的问题：收缩段前的 5 根往往仍处在前一次收缩/横盘里，与收缩期高度重叠，
        # 比值被系统性压向 1.0 —— 实测 1532 个历史样本中位 0.96、p1 仅 0.56，
        # 8 个月（2026-04~11）三条全过 0/1532、通过率 0.00%；换成箱体整体后同阈值
        # 三条全过的比例回到该判据应有的量级。
        if vol_last is None or not vol_box:
            reasons.append("量能数据缺失，无法判断缩量")
        elif box_start == 0 and n_all > lookback:
            # 箱体左边界被 lookback 窗口截断（还有更早的历史没进来）：判缺失，不静默回退。
            # 理由：回退到「可见部分均量」会让比值随窗口长度漂移——正是旧口径的病根；
            # 判缺失则走「VCP 不成立 → ABSTAIN」的管住手路径，不会产生虚假通过。
            reasons.append("箱体左边界触及回看窗口起点，箱体均量不完整（判缺失）")
        else:
            last_ratio = round(vol_last / vol_box, 2)
            if last_ratio > vol_ratio_max:
                reasons.append(
                    f"最后一次收缩量能比 {last_ratio:.2f} > {vol_ratio_max:.2f}"
                    "（未缩量，放量破坏）")
    else:
        reasons.append("未找到有效收缩段，无法判断量能")

    ok = (contractions >= min_contractions and shrinking
          and last_ratio is not None and last_ratio <= vol_ratio_max)
    return VcpResult(
        is_vcp=ok, contractions=contractions, depths=depths,
        box_high=box_high, box_low=box_low, last_vol_ratio=last_ratio,
        ma20=ma20, low20=low20, reasons=tuple(reasons), bar_count=n,
    )


def sector_position_ok(percentile: float | None) -> tuple[bool, str]:
    """板块 20 日涨幅分位：中上位置（50%~85%），不是最热头部"""
    if percentile is None:
        return False, "板块 20 日涨幅分位缺失"
    if percentile < SECTOR_PERCENTILE_MIN:
        return False, f"板块分位 {percentile:.1f}% < {SECTOR_PERCENTILE_MIN:.0f}%（偏弱）"
    if percentile > SECTOR_PERCENTILE_MAX:
        return False, f"板块分位 {percentile:.1f}% > {SECTOR_PERCENTILE_MAX:.0f}%（最热头部，不追）"
    return True, f"板块分位 {percentile:.1f}%（中上位置）"


def derive_zone(vcp: VcpResult, bars) -> dict:
    """伏击区间 = [箱体下沿, min(20 日均线, 箱体上沿)]

    - 下沿：箱体下沿（结构支撑）
    - 上沿：20 日均线（回踩均线）与箱体上沿取低者
    - 若 20 日线低于箱体下沿（区间倒挂），上沿 = 下沿 × 1.02（保底 2% 宽度）
    - 若上沿与下沿过近，抬高到 下沿 × 1.01（至少 1% 宽度，避免零宽区间）
    """
    closes = [b.close for b in bars]
    ma20 = round(_mean(closes[-20:]) or closes[-1], 2)
    lower = round(vcp.box_low, 2)
    upper = round(min(ma20, vcp.box_high), 2)
    if upper < lower:
        upper = round(lower * 1.02, 2)
    if upper <= lower * 1.005:
        upper = round(lower * 1.01, 2)
    return {
        "zone_low": lower,
        "zone_high": upper,
        "ma20": ma20,
        "box_high": round(vcp.box_high, 2),
        "box_low": lower,
        "method": ("伏击下沿=箱体下沿；上沿=min(20日均线, 箱体上沿)；"
                   "倒挂时上沿=下沿×1.02；确保区间宽度≥1%"),
    }


def trim_shares(shares: int) -> dict:
    """分批止盈股数（100 股整数倍）

    +8% 卖 1/3（向下取整到 100 股，至少 100 股）；
    +15% 再卖 1/3（同样取整，且不超过剩余股数）；
    剩余为移动止损仓。100 股仓位无法三等分，按"能卖则卖 1 手"降级处理。
    """
    if shares <= 0:
        return {"tier1": 0, "tier2": 0, "trail": 0}
    t1 = (shares // 3 // 100) * 100
    if t1 == 0:
        t1 = 100 if shares >= 100 else 0
    t1 = min(t1, shares)
    remaining = shares - t1
    t2 = (shares // 3 // 100) * 100
    if t2 == 0:
        t2 = 100 if remaining >= 100 else 0
    t2 = min(t2, remaining)
    return {"tier1": t1, "tier2": t2, "trail": shares - t1 - t2}


def build_risk_plan(entry_ref: float, zone: dict, shares: int, bars,
                    max_loss: float = MAX_LOSS_PER_STOCK) -> dict:
    """止损/止盈计划（决策时以伏击区间上沿为保守入场参考价）

    - 结构止损 = min(箱体下沿, 近20日低点) × 0.98
    - 硬止损 = 入场参考价 × 0.94
    - 取更近者（更高的止损价），并用单只最大亏损 450 元做上限约束
    """
    closes = [b.close for b in bars]
    low20 = min(b.low for b in bars[-20:]) if bars else zone["box_low"]
    structural = round(min(zone["box_low"], low20) * 0.98, 2)
    hard = round(entry_ref * (1 - STOP_LOSS_PCT), 2)
    stop = max(structural, hard)          # 更近者 = 更高止损价
    # 单只最大亏损约束：shares × (entry - stop) ≤ 450 → stop ≥ entry - 450/shares
    if shares > 0:
        max_stop = round(entry_ref - max_loss / shares, 2)
        stop = max(stop, max_stop)
    stop = max(stop, 0.01)
    tiers = trim_shares(shares)
    return {
        "entry_ref": round(entry_ref, 2),
        "stop_price": round(stop, 2),
        "structural_stop": structural,
        "hard_stop": hard,
        "stop_basis": "结构止损与-6%硬止损取更近者，并受单只450元上限约束",
        "tp1": round(entry_ref * (1 + TP1_PCT / 100), 2),
        "tp2": round(entry_ref * (1 + TP2_PCT / 100), 2),
        "tp1_shares": tiers["tier1"],
        "tp2_shares": tiers["tier2"],
        "trail_shares": tiers["trail"],
    }
