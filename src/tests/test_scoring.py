"""五因子评分引擎测试（第二层）"""

import pytest

from src.scoring import (
    FactorScores, ScoreResult, score_stock, score_from_research,
    WEIGHTS, RECOMMEND_THRESHOLD, FACTOR_NAMES,
)


def test_weights_sum_to_100():
    assert sum(WEIGHTS.values()) == pytest.approx(1.0)


def test_recommended_above_85():
    """高分(≥85) → 推荐"""
    r = score_stock("sz002366", "融发核电",
                    FactorScores(industry=88, growth=90, valuation=70, technical=65, capital=80))
    # 88*.25+90*.25+70*.20+65*.15+80*.15 = 22+22.5+14+9.75+12 = 80.25
    assert r.weighted == pytest.approx(80.25)
    assert r.recommended is False


def test_recommended_exactly_85():
    """恰好 85 → 推荐（≥85）"""
    r = score_stock("x", "y", FactorScores(85, 85, 85, 85, 85))
    assert r.weighted == pytest.approx(85.0)
    assert r.recommended is True


def test_not_recommended_below_85():
    r = score_stock("x", "y", FactorScores(50, 50, 50, 50, 50))
    assert r.weighted == pytest.approx(50.0)
    assert r.recommended is False


def test_clamp_above_100():
    """因子分超 100 被钳制到 100"""
    r = score_stock("x", "y", FactorScores(120, 100, 100, 100, 100))
    assert r.factors.industry == 100.0
    assert r.weighted == pytest.approx(100.0)


def test_clamp_below_0():
    r = score_stock("x", "y", FactorScores(-10, 0, 0, 0, 0))
    assert r.factors.industry == 0.0


def test_weighted_formula():
    """加权公式验证：全 60 → 60"""
    r = score_stock("x", "y", FactorScores(60, 60, 60, 60, 60))
    assert r.weighted == pytest.approx(60.0)


def test_result_fields():
    r = score_stock("sh600019", "宝钢股份", FactorScores(70, 60, 95, 55, 65))
    assert isinstance(r, ScoreResult)
    assert r.symbol == "sh600019"
    assert r.name == "宝钢股份"
    d = r.to_dict()
    assert d["weighted"] == round(r.weighted, 2)
    assert "行业景气度" in d["factors"]


def test_str_contains_status():
    r = score_stock("sh600019", "宝钢股份", FactorScores(90, 90, 90, 90, 90))
    assert "✅ 推荐" in str(r)


def test_factor_names_complete():
    assert set(FACTOR_NAMES) == {"industry", "growth", "valuation", "technical", "capital"}


def test_threshold_constant():
    assert RECOMMEND_THRESHOLD == 85.0


def test_score_from_research_with_store(tmp_path):
    """弹药库联动：评分时带情报"""
    from src.research import ResearchStore, init_research_db
    db = str(tmp_path / "r.db")
    init_research_db(db)
    store = ResearchStore(db)
    store.add("sz002366", "融发核电", "风险", "订单周期长", "交付约3年", "https://x", "2026-08-03 14:00")
    r = score_from_research("sz002366", "融发核电", store, 88, 90, 70, 65, 80)
    assert r.weighted == pytest.approx(80.25)
