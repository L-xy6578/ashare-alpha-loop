"""
三角闭环选股体系 — 五因子评分引擎（第二层）

把市场调查员 SKILL V9.0 的五因子评分规则代码化：
- 行业景气度 25% / 业绩趋势 25% / 估值水平 20% / 技术面 15% / 资金面 15%
- 加权得分 ≥ 85 → 推荐；< 85 → 不推荐
- 每个因子 0-100 分（AI 基于调研情报打分，规则由代码保证确定性）

用法:
    from src.scoring import score_stock, FactorScores, ScoreResult

    result = score_stock(
        symbol="sh600000", name="示例银行",
        factors=FactorScores(industry=88, growth=90, valuation=70, technical=65, capital=80),
    )
    print(result)          # ScoreResult(weighted=80.0, recommended=False, ...)
"""

from __future__ import annotations

from dataclasses import dataclass

from src.logger import get_logger

log = get_logger(__name__)

# 五因子权重（来自 strategy_config.yaml / SKILL V9.0）
WEIGHTS = {
    "industry": 0.25,   # 行业景气度
    "growth": 0.25,     # 业绩趋势
    "valuation": 0.20,  # 估值水平
    "technical": 0.15,  # 技术面
    "capital": 0.15,    # 资金面
}

# 推荐阈值（SKILL V9.0 硬约束）
RECOMMEND_THRESHOLD = 85.0

# 因子中文名
FACTOR_NAMES = {
    "industry": "行业景气度",
    "growth": "业绩趋势",
    "valuation": "估值水平",
    "technical": "技术面",
    "capital": "资金面",
}


@dataclass(frozen=True)
class FactorScores:
    """五因子原始分（每项 0-100）"""

    industry: float
    growth: float
    valuation: float
    technical: float
    capital: float


@dataclass(frozen=True)
class ScoreResult:
    """评分结果"""

    symbol: str
    name: str
    factors: FactorScores
    weighted: float
    recommended: bool

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "weighted": round(self.weighted, 2),
            "recommended": self.recommended,
            "factors": {
                FACTOR_NAMES[k]: getattr(self.factors, k)
                for k in ("industry", "growth", "valuation", "technical", "capital")
            },
        }

    def __str__(self) -> str:
        status = "✅ 推荐" if self.recommended else "⛔ 不推荐"
        return (f"{self.name}({self.symbol}) 加权 {self.weighted:.1f}分 [{status}] "
                f"行业{self.factors.industry:.0f} 业绩{self.factors.growth:.0f} "
                f"估值{self.factors.valuation:.0f} 技术{self.factors.technical:.0f} "
                f"资金{self.factors.capital:.0f}")


def _clamp(v: float) -> float:
    """因子分钳制到 0-100"""
    return max(0.0, min(100.0, float(v)))


def score_stock(symbol: str, name: str, factors: FactorScores) -> ScoreResult:
    """按五因子权重计算加权分并给出推荐判定

    Args:
        symbol/name: 标的
        factors: 五因子原始分（AI 基于调研情报打分）

    Returns:
        ScoreResult
    """
    f = FactorScores(
        industry=_clamp(factors.industry),
        growth=_clamp(factors.growth),
        valuation=_clamp(factors.valuation),
        technical=_clamp(factors.technical),
        capital=_clamp(factors.capital),
    )
    weighted = (
        f.industry * WEIGHTS["industry"]
        + f.growth * WEIGHTS["growth"]
        + f.valuation * WEIGHTS["valuation"]
        + f.technical * WEIGHTS["technical"]
        + f.capital * WEIGHTS["capital"]
    )
    recommended = weighted >= RECOMMEND_THRESHOLD
    result = ScoreResult(symbol, name, f, weighted, recommended)
    log.info("五因子评分 %s(%s): %.1f分 %s", name, symbol, weighted,
             "推荐" if recommended else "不推荐")
    return result


def score_from_research(
    symbol: str,
    name: str,
    store,
    industry: float,
    growth: float,
    valuation: float,
    technical: float,
    capital: float,
) -> ScoreResult:
    """基于调研情报库的评分（工程集成：评分前自动附加情报提醒）

    与 score_stock 相同，但会打印该标的相关调研情报（弹药库联动）。
    """
    result = score_stock(symbol, name, FactorScores(industry, growth, valuation, technical, capital))
    notes = store.query(symbol=symbol, limit=5)
    if notes:
        log.info("弹药库情报 %s: %d 条（含风险提示）", symbol, len(notes))
    return result
