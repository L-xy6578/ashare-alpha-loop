"""
三角闭环选股体系 — 五因子数据化引擎（评分输入不再依赖人工打分）

中心思想落地：KPI-08 数据真实率=100%。五因子中四个因子改为由真实数据计算：
    - 行业景气度：赛道分析模块的月度/季度主赛道评分（sector_daily 数据驱动）
    - 技术面：标的真实日K（MA5/MA20、RSI14、20日动能）
    - 资金面：所属板块主力 20 日净流入（sector_daily.zljlr_d20，跨板块归一化）
    - 估值水平：东财公开接口实时 PE(TTM)/PB
    - 业绩趋势：保留 AI 调研评估，但必须附证据 URL（AH-08），无证据视为数据缺失

任一因子缺失 → incomplete=True，推荐判定强制为否（"管住手"，不猜不补）。

用法:
    from src.factors import score_candidate, technical_factor, valuation_factor
    result = score_candidate(
        symbol="sz002202", name="金风科技", sector="电力设备/风电",
        growth=60, growth_evidence="https://...",
        bars=bars, sector_ctx=sector_ctx, plates=plates,
        valuation={"pe": 22.0, "pb": 2.1},
    )
    print(result)   # DataScoreResult(weighted=62.3, recommended=False, ...)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.scoring import FactorScores, WEIGHTS, RECOMMEND_THRESHOLD, FACTOR_NAMES
from src.logger import get_logger

log = get_logger(__name__)

# 业绩趋势证据解析：匹配 净利/归母/扣非 增速（如 "归母8.93亿(+15.23%)"、"扣非+36.01%"）
_GROWTH_PATTERNS = [
    re.compile(r"扣非[^%]*?([+-]?\d+(?:\.\d+)?)%"),   # 扣非优先（主业质量）
    re.compile(r"归母[^%]*?([+-]?\d+(?:\.\d+)?)%"),
    re.compile(r"净利[^%]*?([+-]?\d+(?:\.\d+)?)%"),
]

# 归母净利润金额（用于识别亏损：金额为负，而不是增速为负）
_PROFIT_AMOUNT = re.compile(
    r"归母(?:净利|净利润)?[^0-9\-]*?(-?\d+(?:\.\d+)?)(?:亿|万)"
)


def _is_loss(content: str) -> bool | None:
    """从情报正文判断是否亏损（归母净利金额为负）

    Returns:
        True=亏损 / False=盈利 / None=正文未给出金额（不做质量调整）
    """
    m = _PROFIT_AMOUNT.search(content or "")
    if not m:
        return None
    try:
        return float(m.group(1)) < 0
    except ValueError:
        return None


# 业绩趋势桶（2026-10-30 细化）：绝对增速分档 + 亏损质量下限。
# 设计原则：区间来自增速的经济含义（高增长/稳健/停滞/小幅下滑/明显下滑/深度下滑），
# 映射对增速单调不递减；亏损（净利金额<0）一律封顶 25，不因低基数高增速而虚高。
GROWTH_BUCKETS: list[tuple[float, float, float]] = [
    (50, 1e9, 90.0),     # 高增长
    (30, 50, 84.0),      # 较快增长
    (20, 30, 80.0),      # 稳健增长
    (10, 20, 75.0),      # 温和增长
    (5, 10, 72.0),       # 低速增长
    (0, 5, 70.0),        # 增长停滞（盈利稳定）
    (-5, 0, 60.0),       # 小幅下滑（盈利稳定）
    (-15, -5, 50.0),     # 明显下滑
    (-30, -15, 40.0),    # 大幅下滑
    (-1000, -30, 30.0),  # 深度下滑
]
LOSS_CAP = 25.0


def growth_score_from_rate(rate: float, *, profitable: bool | None = None) -> float:
    """净利增速 → 业绩趋势分（0-100，单调不递减）

    Args:
        rate: 净利增速 %
        profitable: False=亏损（净利金额为负）→ 封顶 25；None=未知，按增速给分
    """
    score = 30.0
    for lo, hi, bucket_score in GROWTH_BUCKETS:
        if lo <= rate < hi:
            score = bucket_score
            break
    if profitable is False:
        score = min(score, LOSS_CAP)
    return score


def growth_from_earnings(earnings: dict | None, code: str = "") -> tuple[float | None, str]:
    """东财业绩报表 → (业绩趋势分, 证据 URL)

    2026-11-10 起业绩因子唯一口径：东财 RPT_LICO_FN_CPD 的归母净利同比（SJLTZ，
    `src.finance_feed.fetch_earnings` 的 net_yoy）→ growth_score_from_rate，桶表不变。
    拉不到 net_yoy 一律返回 (None, "")，调用方按数据缺失（incomplete，不推荐）处理，
    不得折 0 当有值。

    Args:
        earnings: fetch_earnings 返回的 dict（net_yoy/net_profit/...）
        code: 6 位代码，用于生成可追溯的证据 URL
    """
    if not earnings:
        return None, ""
    rate = earnings.get("net_yoy")
    if rate is None:
        return None, ""
    net_profit = earnings.get("net_profit")
    profitable = None if net_profit is None else float(net_profit) >= 0
    score = growth_score_from_rate(float(rate), profitable=profitable)
    url = f"https://data.eastmoney.com/bbsj/{code}.html" if code else ""
    return score, url


def growth_from_research(notes) -> tuple[float | None, str]:
    """从调研情报（基本面类别）提取业绩趋势分 + 证据来源（AH-08）

    2026-11-10 起**不再参与收盘闭环评分**（业绩因子统一走 growth_from_earnings），
    仅保留给预判/实盘辅助/口径对照等旁路模块使用。

    Returns:
        (score, evidence)；无匹配情报返回 (None, "")
    """
    for note in notes:
        content = note.content or ""
        for pat in _GROWTH_PATTERNS:
            m = pat.search(content)
            if m:
                loss = _is_loss(content)
                score = growth_score_from_rate(
                    float(m.group(1)),
                    profitable=None if loss is None else (not loss))
                evidence = note.source_url or note.title or content[:50]
                log.info("业绩趋势证据命中 %s: %s → %s分", note.symbol, note.title, score)
                return score, evidence
    return None, ""


# ============================================================
# 因子计算（全部基于真实数据，规则确定性）
# ============================================================

def _rsi14(closes: list[float]) -> float:
    """RSI14（收盘价序列，最后 14 个差分）"""
    if len(closes) < 15:
        return 50.0
    gains = losses = 0.0
    for i in range(-14, 0):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_g, avg_l = gains / 14, losses / 14
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - 100.0 / (1.0 + rs)


def technical_factor(bars, window: int = 20, expected_date: str | None = None) -> float | None:
    """技术面 0-100：MA 多空(25) + RSI 健康度(45) + 20日动能(30)

    - 趋势：MA5>MA20 多头 +25，空头 +10
    - RSI：<30 超卖反弹 +30；30~45 偏弱 +30；45~70 中性偏强 +45；
          70~85 过热 +25；>85 极端过热 +15
    - 20日动能：>10% +30；0~10% +20；-10~0 +12；<-10% +5

    Args:
        expected_date: 当日交易日（trade_calendar 最近交易日）。给定且最后一根
            bar 的日期不等于它时返回 None（缺失），不得用前一交易日的旧 bar 计分。
    """
    if not bars or len(bars) < 10:
        return None
    if expected_date and bars[-1].date != expected_date:
        log.warning("技术面缺失：最新K线 %s ≠ 当日交易日 %s", bars[-1].date, expected_date)
        return None
    closes = [b.close for b in bars]
    price = closes[-1]
    w = min(window, len(closes))
    ma5 = sum(closes[-5:]) / 5
    ma20 = sum(closes[-w:]) / w
    rsi = _rsi14(closes)
    chg20 = (price / closes[-w] - 1) * 100 if closes[-w] else 0.0

    score = 25.0 if ma5 > ma20 else 10.0
    if rsi < 30:
        score += 30.0
    elif rsi < 45:
        score += 30.0
    elif rsi <= 70:
        score += 45.0
    elif rsi <= 85:
        score += 25.0
    else:
        score += 15.0
    if chg20 > 10:
        score += 30.0
    elif chg20 > 0:
        score += 20.0
    elif chg20 > -10:
        score += 12.0
    else:
        score += 5.0
    return round(min(score, 100.0), 1)


_BOARD_SUFFIXES = ("概念", "产业", "板块", "行业", "指数", "Ⅱ", "Ⅲ")

# 全市场聚合类概念板块：主力资金是"全市场加总"，不是单个行业/题材的资金，
# 2026-10-30 起从资金分位基准中剔除（否则会绑架整条归一化曲线）。
AGGREGATE_BOARD_MARKERS = (
    "转融券", "融资融券", "沪股通", "深股通", "大盘股", "中盘股", "小盘股",
    "基金增仓", "基金减仓", "昨日", "MSCI", "标普", "富时", "机构重仓",
    "社保", "QFII", "预增", "预减", "百元股", "低价股", "高送转", "次新股",
    "新股", "同股同权", "AH股", "AB股", "破净", "高股息", "红利", "风格",
)

# 分位基准最少板块数：样本太少时分位无意义 → 因子缺失
MIN_BOARD_POOL = 30

# 资金分位有效样本覆盖率下限：zljlr_d20 为 NULL 表示"当日无资金数据/20 日窗口不完整"，
# 不进分位池；0 是真实零净流入，属于有效样本。若有效样本占池内板块比例过低（<30%），
# 分位被"缺数据"主导 → 判缺失。
# 30% 依据：正常交易日东财板块资金覆盖率约 80%+；低于 30% 说明采集大面积失败。
MIN_CAPITAL_COVERAGE = 0.3


def _strip_board_suffix(name: str) -> str:
    """板块名去后缀（Ⅱ/Ⅲ/概念/产业/板块/行业/指数），可连续去除"""
    n = (name or "").strip()
    changed = True
    while changed and n:
        changed = False
        for suf in _BOARD_SUFFIXES:
            if n.endswith(suf):
                n = n[: -len(suf)].strip()
                changed = True
    return n


def _level_tag(name: str) -> str:
    """板块层级标记：Ⅱ / Ⅲ / ''（无分级）

    用于禁止跨层级回退：'银行'（无分级）不得命中 '国有大型银行Ⅲ'。
    """
    n = (name or "").strip()
    if n.endswith("Ⅲ"):
        return "Ⅲ"
    if n.endswith("Ⅱ"):
        return "Ⅱ"
    return ""


def _keywords(sector: str) -> list[str]:
    """板块字段拆关键词：只取第一段（watchlist.sector 的主板块）

    '电力设备/核电' → ['电力设备']。细分方向不参与跨板块匹配，
    避免主板块被细分板块的资金/赛道分顶替。
    """
    primary = (sector or "").split("/")[0].strip()
    primary = _strip_board_suffix(primary)
    return [primary] if len(primary) >= 2 else []


def _resolve_board(names, sector: str) -> str | None:
    """板块匹配（唯一解析，不跨板块取最大值，不跨层级回退）

    顺序：精确名 → 去后缀唯一 → 唯一关键词命中；仍不唯一/匹配不到返回 None。
    2026-10-30：关键词步骤要求分级标记一致（银行不落到"国有大型银行Ⅲ"）。
    """
    primary = (sector or "").split("/")[0].strip()
    if not primary:
        return None
    names = list(names)
    if primary in names:                     # 1) 精确名
        return primary
    tag = _level_tag(primary)
    stripped = _strip_board_suffix(primary)
    same = [n for n in names
            if _strip_board_suffix(n) == stripped and _level_tag(n) == tag]
    if len(same) == 1:                       # 2) 去后缀后唯一（层级一致）
        return same[0]
    if len(same) > 1:
        return None                          # 同名多板块 → 歧义
    if len(stripped) >= 2:                   # 3) 唯一关键词命中（层级一致）
        hits = [n for n in names
                if _level_tag(n) == tag and stripped in _strip_board_suffix(n)]
        if len(hits) == 1:
            return hits[0]
    return None


def _resolve_plate(plates, sector: str):
    """解析到唯一板块快照（同名板块优先 plate，其次 concept）"""
    target = _resolve_board([p.name for p in plates], sector)
    if target is None:
        return None
    matched = [p for p in plates if p.name == target]
    if not matched:
        return None
    plate_only = [p for p in matched if getattr(p, "kind", "") == "plate"]
    chosen = plate_only or matched
    return chosen[0] if len(chosen) == 1 else None


def industry_factor(sector_ctx: dict[str, float] | None, sector: str) -> float | None:
    """行业景气度：所属主板块命中的当期主赛道评分（不跨板块取 max）"""
    if not sector_ctx:
        return None
    target = _resolve_board(sector_ctx.keys(), sector)
    if target is None:
        return None
    return round(sector_ctx[target], 1)


def industry_pool(plates):
    """行业景气分位的排序池：**只含行业板块**（kind='plate'），概念板块不参与排序

    2026-11-10（#160，冻结期起算前的口径修正）：此前行业+概念混排进同一个分位池，
    11-10 池内 1019 个板块里概念 526 个（51.6%），等于拿概念题材当"同行"比强弱。
    概念板块的数据照常采集、照常落库（资金面因子口径不变），只是不进这里的分位池。
    """
    return [p for p in plates if getattr(p, "kind", "plate") == "plate"]


def industry_percentile_factor(plates, sector: str) -> float | None:
    """行业景气（默认口径）：候选自身板块 20 日涨幅在**当日行业板块**中的分位 0-100

    2026-10-30 口径固定：不再随当日 TOP 榜漂移（旧口径只认 TOP7 赛道名，
    同一只票掉出 TOP7 就直接判缺失）。分位基准 = 当日全部有效行业板块
    （2026-11-10 起概念板块不入池，见 `industry_pool`），
    天然固定在 0-100，跨日可比。
    """
    if not plates:
        return None
    # zdf20 为 NULL（板块历史不足，无真值）或 0 时不进分位池，也不作为候选自身值
    vals = [p.zdf20 for p in plates if p.zdf20 is not None and p.zdf20 != 0]
    if len(vals) < MIN_BOARD_POOL:
        return None
    chosen = _resolve_plate(plates, sector)
    if chosen is None or chosen.zdf20 is None or chosen.zdf20 == 0:
        return None
    below = sum(1 for v in vals if v <= chosen.zdf20)
    return round(below / len(vals) * 100, 1)


def is_aggregate_board(name: str) -> bool:
    """是否全市场聚合类概念板块（资金口径不可比，不进分位基准）"""
    n = name or ""
    return any(marker in n for marker in AGGREGATE_BOARD_MARKERS)


def capital_factor(plates, sector: str) -> float | None:
    """资金面：所属板块 20 日主力净流入在**非聚合板块**中的分位 0-100

    2026-10-30 口径修正：
      - 剔除"转融券标的/融资融券/大盘股/基金增仓"等全市场聚合概念板块；
      - 改为板块内分位，不再用全市场 min-max（min-max 被极端板块绑架，
        10-29 曾出现"所有板块资金分 90-99"的失真）。
    2026-11-06 数据新鲜度门禁：
      - zljlr_d20 为 NULL 表示"当日无资金数据/20 日窗口不完整"，一律不进分位池；
        0 表示真实零净流入，是有效样本（2026-11-07 可空化后口径）；
      - 有效样本 < MIN_BOARD_POOL 或覆盖率 < MIN_CAPITAL_COVERAGE → 返回 None（缺失）。
        修正前全 0 池会把自身计为"低于自身"从而返回 100 分。
    """
    if not plates:
        return None
    pool = [s for s in plates if not is_aggregate_board(s.name)]
    if len(pool) < MIN_BOARD_POOL:
        return None
    chosen = _resolve_plate(pool, sector)
    if chosen is None or chosen.zljlr_d20 is None:
        return None
    vals = [s.zljlr_d20 for s in pool if s.zljlr_d20 is not None]
    if len(vals) < MIN_BOARD_POOL:
        return None
    if len(vals) / len(pool) < MIN_CAPITAL_COVERAGE:
        return None
    below = sum(1 for v in vals if v <= chosen.zljlr_d20)
    return round(below / len(vals) * 100, 1)


def factor_benchmark(plates) -> str:
    """分位基准描述（写入 factor_snapshot，便于回溯当时的分位样本量）"""
    if not plates:
        return "无板块快照"
    ind_n = sum(1 for p in industry_pool(plates)
                if p.zdf20 is not None and p.zdf20 != 0)
    cap_pool = [s for s in plates if not is_aggregate_board(s.name)]
    cap_n = sum(1 for s in cap_pool if s.zljlr_d20 is not None)
    return (f"industry=行业板块内分位(仅行业板块 n={ind_n}); "
            f"capital=板块内分位(非聚合板块 n={cap_n})")


# 估值桶（2026-10-30 细化）：PE 主桶 + PB 辅助，均为"估值越高分越低"的单调映射。
# PE<=0 视为亏损（20 分）；PB<=0.8 视为深度破净（92 分）。
PE_BUCKETS: list[tuple[float, float, float]] = [
    (-1000, 0, 20.0),    # 亏损
    (0, 10, 92.0),       # 低估
    (10, 15, 88.0),
    (15, 20, 84.0),
    (20, 25, 78.0),      # 合理偏上
    (25, 30, 72.0),
    (30, 40, 64.0),      # 偏高
    (40, 50, 56.0),
    (50, 60, 48.0),
    (60, 80, 40.0),      # 极高
    (80, 100000, 30.0),
]

PB_BUCKETS: list[tuple[float, float, float]] = [
    (-1000, 0.8, 92.0),  # 深度破净
    (0.8, 1.2, 86.0),
    (1.2, 2, 78.0),
    (2, 3, 70.0),
    (3, 4, 60.0),
    (4, 6, 50.0),
    (6, 10, 40.0),
    (10, 100000, 30.0),
]


def _bucket_score(value: float, buckets: list[tuple[float, float, float]],
                  default: float) -> float:
    for lo, hi, score in buckets:
        if lo <= value < hi:
            return score
    return default


def valuation_factor(pe: float | None, pb: float | None) -> float | None:
    """估值水平 0-100：PE 主桶 + PB 辅助（有则取均值）

    PE：<=0 亏损 20；0-10 低估 92；10-15 88；15-20 84；20-25 78；25-30 72；
        30-40 偏高 64；40-50 56；50-60 48；60-80 极高 40；>80 30。
    PB：<=0.8 深度破净 92；0.8-1.2 86；1.2-2 78；2-3 70；3-4 60；4-6 50；
        6-10 40；>10 30。
    """
    scores = []
    if pe is not None:
        pe = float(pe)
        # PE<=0 为亏损，单独判；其余按桶（PE_BUCKETS 第一桶仅作说明）
        scores.append(20.0 if pe <= 0 else _bucket_score(pe, PE_BUCKETS[1:], 30.0))
    if pb is not None:
        scores.append(_bucket_score(float(pb), PB_BUCKETS, 30.0))
    if not scores:
        return None
    return round(sum(scores) / len(scores), 1)


# ============================================================
# 评分结果 & 汇总
# ============================================================

@dataclass(frozen=True)
class DataScoreResult:
    """数据化五因子评分结果"""

    symbol: str
    name: str
    factors: FactorScores
    weighted: float
    recommended: bool
    incomplete: bool          # True=有因子数据缺失，强制不推荐
    missing: tuple[str, ...]  # 缺失因子
    evidence: str = ""        # 业绩趋势证据 URL

    @property
    def summary(self) -> str:
        status = "✅推荐" if self.recommended else "⛔不推荐"
        tag = "（数据缺失：" + "、".join(self.missing) + "）" if self.missing else ""
        return (f"{self.name}({self.symbol}) {self.weighted:.1f}分 [{status}]{tag} "
                f"行业{self.factors.industry:.0f} 业绩{self.factors.growth:.0f} "
                f"估值{self.factors.valuation:.0f} 技术{self.factors.technical:.0f} "
                f"资金{self.factors.capital:.0f}")


def score_candidate(
    symbol: str,
    name: str,
    sector: str,
    *,
    growth: float,
    growth_evidence: str,
    bars,
    sector_ctx: dict[str, float] | None,
    plates,
    valuation: dict | None,
    industry_mode: str = "percentile",
    snapshot_source: str = "eastmoney",
    snapshot_complete: bool = True,
    expected_date: str = "",
    snapshot_date: str = "",
) -> DataScoreResult:
    """数据化五因子评分：四个因子来自真实数据，growth 来自调研评估（须附证据）

    Args:
        sector: 所属板块（如 "电力设备/风电"），用于行业/资金因子匹配
        growth: 业绩趋势评估分 0-100（调研评估）
        growth_evidence: 业绩评估证据 URL（AH-08），缺失视为数据不足
        bars: 标的日K（KlineBar 列表）
        sector_ctx: 赛道评分字典（仅展示/解析用；percentile 口径不参与评分）
        plates: 板块快照列表（SectorSnapshot，含 zdf20 / zljlr_d20）；
            行业景气分位只用其中的行业板块（kind='plate'），资金面用全量表
        valuation: {"pe": float|None, "pb": float|None}，来自实时行情接口
        industry_mode: "percentile"（默认：自身板块 20 日涨幅分位）/
                       "tracks"（旧口径：TOP7 赛道名匹配，仅对比用）
        snapshot_source: 当日板块快照数据源；只认 eastmoney，其余一律缺失
        snapshot_complete: 概念板块是否拉全（不完整只告警，不判缺失——
            行业/资金只依赖完整行业板块，概念聚合板块已被资金口径剔除）
        expected_date: 当日交易日（trade_calendar 最近交易日）；给定且最新K线
            日期不等于它时，技术面判缺失（不用旧 bar 计分）
        snapshot_date: 板块快照最大日期；与 expected_date 不一致时，
            行业/资金判缺失（不用旧快照计分）
    """
    missing: list[str] = []

    # 数据新鲜度门禁：板块快照最大日期 != 当日交易日 → 行业/资金判缺失
    sector_stale = bool(expected_date and snapshot_date
                        and snapshot_date < expected_date)

    # A 方案门禁：腾讯兜底快照/无快照不得参与板块因子评分
    source_ok = (snapshot_source or "").strip().lower() == "eastmoney"
    if not source_ok:
        industry = None
        capital = None
        log.warning("板块快照数据源=%s（非东财），行业/资金因子按缺失处理",
                    snapshot_source or "空")
    elif sector_stale:
        industry = None
        capital = None
        log.warning("板块快照滞后：快照日期=%s ≠ 当日交易日=%s，行业/资金因子按缺失处理",
                    snapshot_date, expected_date)
    else:
        if industry_mode == "tracks":
            industry = industry_factor(sector_ctx, sector)
        else:
            # 分位池只含行业板块（#160 · 2026-11-10）：概念板块不进行业景气排序；
            # 资金面仍用全量 plates（含概念），本次不动资金口径。
            industry = industry_percentile_factor(industry_pool(plates), sector)
        capital = capital_factor(plates, sector)
        if not snapshot_complete:
            log.warning("板块快照概念板块不完整，已按行业板块分位计算（概念聚合板块已剔除）")
    if industry is None:
        missing.append("行业景气")
    technical = technical_factor(bars, expected_date=expected_date or None)
    if technical is None:
        missing.append("技术面")
    if capital is None:
        missing.append("资金面")
    val = valuation_factor(
        (valuation or {}).get("pe"), (valuation or {}).get("pb"),
    )
    if val is None:
        missing.append("估值")
    if not growth_evidence.strip():
        missing.append("业绩证据")

    factors = FactorScores(
        industry=industry or 0.0,
        growth=float(growth),
        valuation=val or 0.0,
        technical=technical or 0.0,
        capital=capital or 0.0,
    )
    weighted = (
        factors.industry * WEIGHTS["industry"]
        + factors.growth * WEIGHTS["growth"]
        + factors.valuation * WEIGHTS["valuation"]
        + factors.technical * WEIGHTS["technical"]
        + factors.capital * WEIGHTS["capital"]
    )
    incomplete = bool(missing)
    recommended = (not incomplete) and weighted >= RECOMMEND_THRESHOLD
    result = DataScoreResult(
        symbol=symbol, name=name, factors=factors,
        weighted=round(weighted, 1), recommended=recommended,
        incomplete=incomplete, missing=tuple(missing),
        evidence=growth_evidence,
    )
    log.info("数据化五因子评分 %s(%s): %.1f分 推荐=%s 缺失=%s",
             name, symbol, weighted, recommended, missing)
    return result


def factor_legend() -> str:
    """打印权重说明（调试用）"""
    return " | ".join(f"{FACTOR_NAMES[k]} {v*100:.0f}%" for k, v in WEIGHTS.items())
