"""五因子数据化引擎测试"""

import pytest

from src.data_source import KlineBar
from src.factors import (
    capital_factor,
    factor_benchmark,
    growth_from_earnings,
    growth_from_research,
    growth_score_from_rate,
    industry_factor,
    industry_percentile_factor,
    industry_pool,
    score_candidate,
    technical_factor,
    valuation_factor,
)
from src.research import ResearchNote
from src.sector_analysis import SectorSnapshot


def _bars(closes, start="2026-07-01"):
    """收盘价序列 → KlineBar 列表（日期递增）"""
    from datetime import date, timedelta

    d = date.fromisoformat(start)
    out = []
    for c in closes:
        iso = d.isoformat()
        out.append(KlineBar(iso, c, c, c * 1.01, c * 0.99))
        d += timedelta(days=1)
    return out


def _plate(name, zljlr_d20, code=None):
    return SectorSnapshot(code=code or f"p_{name}", name=name, kind="plate",
                          zljlr_d20=zljlr_d20, zdf20=0, zdf60=0)


def _plate20(name, zdf20):
    return SectorSnapshot(code=f"p_{name}", name=name, kind="plate",
                          zdf20=zdf20, zdf60=0)


def _cap_pool(extra=None, zdf20_pool=None):
    """31 个基础板块（资金从 -1000 递增，行业涨幅从 -30 递增）"""
    base = [SectorSnapshot(code=f"b{i}", name=f"板块{i}", kind="plate",
                           zljlr_d20=-1000 + i * 10,
                           zdf20=(-30 + i) if zdf20_pool else 0)
            for i in range(31)]
    return base + list(extra or [])


class TestTechnicalFactor:
    def test_uptrend_scores_higher_than_downtrend(self):
        up = _bars([10 + i * 0.05 for i in range(25)])   # 上升
        down = _bars([20 - i * 0.05 for i in range(25)])  # 下降
        assert technical_factor(up) > technical_factor(down)

    def test_insufficient_bars_returns_none(self):
        assert technical_factor(_bars([10, 11, 12])) is None
        assert technical_factor([]) is None


class TestValuationFactor:
    def test_pe_bands(self):
        assert valuation_factor(15, None) == 84
        assert valuation_factor(-5, None) == 20      # 亏损
        assert valuation_factor(0, None) == 20       # PE=0 视为亏损
        assert valuation_factor(100, None) == 30     # 高估

    def test_pb_bands(self):
        assert valuation_factor(None, 0.8) == 86
        assert valuation_factor(None, 0.5) == 92     # 深度破净
        assert valuation_factor(None, 3.5) == 60
        assert valuation_factor(None, 4.0) == 50

    def test_combined_average(self):
        assert valuation_factor(15, 0.8) == pytest.approx((84 + 86) / 2)

    def test_pe_monotonic_decreasing(self):
        """PE 越高分越低（亏损 20 为下限）"""
        points = [1, 9, 12, 18, 22, 28, 35, 45, 55, 70, 90]
        scores = [valuation_factor(p, None) for p in points]
        assert all(a >= b for a, b in zip(scores, scores[1:]))

    def test_pb_monotonic_decreasing(self):
        points = [0.5, 1.0, 1.5, 2.5, 3.5, 5, 8, 20]
        scores = [valuation_factor(None, p) for p in points]
        assert all(a >= b for a, b in zip(scores, scores[1:]))

    def test_missing_returns_none(self):
        assert valuation_factor(None, None) is None


class TestCapitalFactor:
    """资金面：非聚合板块内分位（2026-10-30 口径）"""

    def test_top_fund_is_top_percentile(self):
        pool = _cap_pool([_plate("风电设备", 5000)])
        assert capital_factor(pool, "风电设备") == pytest.approx(100.0)

    def test_no_match_returns_none(self):
        assert capital_factor(_cap_pool(), "风电设备") is None

    def test_empty_returns_none(self):
        assert capital_factor([], "风电设备") is None

    def test_exact_board_not_max_keyword(self):
        """'银行' 精确命中自身板块，不再取 '国有大型银行Ⅱ' 的最大值"""
        pool = _cap_pool([
            _plate("银行", -500),
            SectorSnapshot(code="bk_big", name="国有大型银行Ⅱ", kind="plate",
                           zljlr_d20=500),
        ])
        exact = capital_factor(pool, "银行")
        max_keyword = capital_factor(pool, "国有大型银行Ⅱ")
        assert exact is not None and max_keyword is not None
        assert exact < max_keyword

    def test_ambiguous_keyword_returns_none(self):
        """关键词命中多个板块且无法唯一定位 → 缺失，不拿别的板块顶替"""
        pool = _cap_pool([_plate("医药生物", 100), _plate("医药商业", -100)])
        assert capital_factor(pool, "医药") is None

    def test_suffix_unique_match(self):
        """去后缀匹配要求层级一致：'航空装备' 不得落到 '航空装备Ⅱ'"""
        pool = _cap_pool([_plate("航空装备Ⅱ", 100)])
        assert capital_factor(pool, "航空装备") is None
        assert capital_factor(pool, "航空装备Ⅱ") is not None

    def test_aggregate_concept_excluded_from_pool(self):
        """全市场聚合概念板块（基金增仓）不进分位基准"""
        pool = _cap_pool([
            _plate("银行", 900),
            SectorSnapshot(code="c1", name="基金增仓", kind="concept",
                           zljlr_d20=999999),
            SectorSnapshot(code="c2", name="转融券标的", kind="concept",
                           zljlr_d20=-999999),
        ])
        # 银行在非聚合板块中资金最高 → 100 分位（聚合板块不拉低/抬高它）
        assert capital_factor(pool, "银行") == pytest.approx(100.0)

    def test_cross_level_fallback_banned(self):
        """'银行' 无分级，不得落到 '国有大型银行Ⅲ'"""
        pool = _cap_pool([
            SectorSnapshot(code="bk_bank3", name="国有大型银行Ⅲ", kind="plate",
                           zljlr_d20=500),
        ])
        assert capital_factor(pool, "银行") is None


class TestIndustryFactor:
    def test_match_track_score(self):
        ctx = {"风电设备": 92.0, "白酒": 80.0}
        assert industry_factor(ctx, "风电设备") == 92.0

    def test_no_match_returns_none(self):
        assert industry_factor({"白酒": 80.0}, "风电设备") is None
        assert industry_factor(None, "风电设备") is None

    def test_exact_priority_over_keyword_max(self):
        """精确名优先：'银行' 取自身 88.6，而不是 '国有大型银行Ⅱ' 的 94.2"""
        ctx = {"银行": 88.6, "国有大型银行Ⅱ": 94.2, "股份制银行Ⅱ": 88.5}
        assert industry_factor(ctx, "银行") == 88.6

    def test_first_segment_only(self):
        """watchlist.sector 只取第一段主板块，不拿细分板块顶替"""
        ctx = {"电力设备": 90.0, "风电设备": 99.0}
        assert industry_factor(ctx, "电力设备/风电") == 90.0

    def test_cross_level_fallback_banned_in_tracks(self):
        ctx = {"国有大型银行Ⅲ": 63.2}
        assert industry_factor(ctx, "银行") is None


class TestIndustryPercentileFactor:
    """行业分：自身板块 20 日涨幅在全部板块中的分位（默认口径）"""

    def _pool(self, zdf20):
        base = [SectorSnapshot(code=f"b{i}", name=f"板块{i}", kind="plate",
                               zdf20=-20 + i) for i in range(31)]
        return base + [SectorSnapshot(code=f"x_{n}", name=n, kind="plate", zdf20=v)
                       for n, v in zdf20.items()]

    def test_top_sector_is_top_percentile(self):
        pool = self._pool({"风电设备": 30.0, "白酒": 8.0, "银行": 3.0})
        assert industry_percentile_factor(pool, "风电设备") == 100.0

    def test_middle_sector_gets_middle_percentile(self):
        pool = self._pool({"银行": 3.0})
        # 基础板块 -20..10 共 31 个；3.0 落在偏上分位
        score = industry_percentile_factor(pool, "银行")
        assert 70 <= score <= 85

    def test_no_match_or_no_data_returns_none(self):
        assert industry_percentile_factor(self._pool({"白酒": 8.0}), "风电设备") is None
        assert industry_percentile_factor([], "白酒") is None
        assert industry_percentile_factor(self._pool({"白酒": 0.0}), "白酒") is None

    def test_too_few_boards_returns_none(self):
        """样本 < 30 时分位无意义 → 缺失"""
        assert industry_percentile_factor([_plate20("白酒", 8.0)], "白酒") is None

    def test_cross_level_fallback_banned(self):
        pool = self._pool({"国有大型银行Ⅲ": 30.0})
        assert industry_percentile_factor(pool, "银行") is None


class TestIndustryPoolScope:
    """#160（2026-11-10 冻结期起算前口径修正）：行业景气分位只按行业板块排序

    概念板块照常采集落库、仍参与资金面分位，但不进行业景气排序池。
    """

    def _mixed_pool(self):
        """32 个行业板块（-31..-1 与 -10.5）+ 20 个概念板块（50..69）"""
        plates = [SectorSnapshot(code=f"b{i}", name=f"板块{i}", kind="plate",
                                 zdf20=-31 + i, zljlr_d20=-1000 + i * 10)
                  for i in range(31)]
        plates.append(SectorSnapshot(code="p_wind", name="风电设备", kind="plate",
                                     zdf20=-10.5, zljlr_d20=900))
        concepts = [SectorSnapshot(code=f"c{i}", name=f"概念{i}", kind="concept",
                                   zdf20=50 + i, zljlr_d20=-500 + i)
                    for i in range(20)]
        return plates + concepts

    def _score(self, plates):
        bars, ctx, _ = TestScoreCandidate()._full_inputs()
        return score_candidate(
            "sz002202", "金风科技", "风电设备",
            growth=90, growth_evidence="https://example.com/evidence",
            bars=bars, sector_ctx=ctx, plates=plates,
            valuation={"pe": 15.0, "pb": 2.0},
        )

    def test_pool_contains_industry_boards_only(self):
        pool = self._mixed_pool()
        sub = industry_pool(pool)
        assert len(pool) == 52
        assert len(sub) == 32
        assert all(p.kind == "plate" for p in sub)
        assert "仅行业板块 n=32" in factor_benchmark(pool)

    def test_percentile_depends_on_pool_composition(self):
        """同一板块：混合池（旧口径）42.3 → 仅行业池（新口径）68.8"""
        pool = self._mixed_pool()
        assert industry_percentile_factor(pool, "风电设备") == 42.3       # 旧：概念抬高样本量
        assert industry_percentile_factor(industry_pool(pool), "风电设备") == 68.8
        assert self._score(pool).factors.industry == 68.8                 # 评分走新口径

    def test_concept_only_pool_falls_back_to_missing(self):
        """池内没有行业板块 → 行业景气判缺失（沿用既有缺失逻辑，不新造行为）"""
        pool = [SectorSnapshot(code=f"c{i}", name=f"概念{i}", kind="concept",
                               zdf20=50 + i, zljlr_d20=-500 + i)
                for i in range(40)]
        assert industry_pool(pool) == []
        r = self._score(pool)
        assert "行业景气" in r.missing
        assert r.recommended is False

    def test_too_few_industry_boards_falls_back_to_missing(self):
        """只有 20 个行业板块（< MIN_BOARD_POOL=30）→ 缺失；概念再多也不补足"""
        pool = [SectorSnapshot(code=f"b{i}", name=f"板块{i}", kind="plate",
                               zdf20=-31 + i, zljlr_d20=-1000 + i * 10)
                for i in range(20)]
        pool += [SectorSnapshot(code=f"c{i}", name=f"概念{i}", kind="concept",
                                zdf20=50 + i, zljlr_d20=-500 + i)
                 for i in range(40)]
        r = self._score(pool)
        assert "行业景气" in r.missing
        assert r.recommended is False


class TestScoreCandidate:
    def _full_inputs(self):
        bars = _bars([10 + i * 0.05 for i in range(25)])
        plates = [SectorSnapshot(code=f"p{i}", name=f"板块{i}", kind="plate",
                                 zdf20=-20 + i, zljlr_d20=-1000 + i * 10)
                  for i in range(31)]
        plates.append(SectorSnapshot(code="p_wind", name="风电设备", kind="plate",
                                     zdf20=30.0, zljlr_d20=900))
        ctx = {"风电设备": 95.0}
        return bars, ctx, plates

    def test_recommended_when_all_factors_high(self):
        bars, ctx, plates = self._full_inputs()
        r = score_candidate(
            "sz002202", "金风科技", "风电设备",
            growth=90, growth_evidence="https://example.com/evidence",
            bars=bars, sector_ctx=ctx, plates=plates,
            valuation={"pe": 15.0, "pb": 2.0},
        )
        assert not r.incomplete
        assert r.weighted >= 85
        assert r.recommended is True

    def test_missing_evidence_forces_not_recommended(self):
        bars, ctx, plates = self._full_inputs()
        r = score_candidate(
            "sz002202", "金风科技", "风电设备",
            growth=90, growth_evidence="",
            bars=bars, sector_ctx=ctx, plates=plates,
            valuation={"pe": 15.0, "pb": 2.0},
        )
        assert r.incomplete
        assert "业绩证据" in r.missing
        assert r.recommended is False

    def test_missing_valuation_forces_not_recommended(self):
        bars, ctx, plates = self._full_inputs()
        r = score_candidate(
            "sz002202", "金风科技", "风电设备",
            growth=90, growth_evidence="https://example.com/evidence",
            bars=bars, sector_ctx=ctx, plates=plates,
            valuation={},  # 估值接口失败 → 数据缺失
        )
        assert r.incomplete
        assert "估值" in r.missing
        assert r.recommended is False

    def test_non_eastmoney_source_forces_missing(self):
        """A 方案门禁：腾讯兜底快照/无快照 → 行业与资金直接缺失"""
        bars, ctx, plates = self._full_inputs()
        r = score_candidate(
            "sz002202", "金风科技", "风电设备",
            growth=90, growth_evidence="https://example.com/evidence",
            bars=bars, sector_ctx=ctx, plates=plates,
            valuation={"pe": 15.0, "pb": 2.0},
            snapshot_source="tencent_fallback",
        )
        assert r.incomplete
        assert "行业景气" in r.missing and "资金面" in r.missing
        assert r.recommended is False

    def test_incomplete_snapshot_only_warns(self):
        """概念不全只告警，不判缺失（行业/资金依赖完整行业板块）"""
        bars, ctx, plates = self._full_inputs()
        r = score_candidate(
            "sz002202", "金风科技", "风电设备",
            growth=90, growth_evidence="https://example.com/evidence",
            bars=bars, sector_ctx=ctx, plates=plates,
            valuation={"pe": 15.0, "pb": 2.0},
            snapshot_source="eastmoney", snapshot_complete=False,
        )
        assert "行业景气" not in r.missing and "资金面" not in r.missing


class TestDataFreshness:
    """2026-11-06 数据新鲜度门禁：旧数据不得算"正常分" """

    def test_stale_bar_makes_technical_missing(self):
        bars = _bars([10 + i * 0.05 for i in range(25)], start="2026-07-01")
        assert technical_factor(bars) is not None            # 不传基准：旧行为
        assert technical_factor(bars, expected_date="2026-11-06") is None

    def test_fresh_bar_still_scores(self):
        bars = _bars([10 + i * 0.05 for i in range(25)], start="2026-10-13")
        assert bars[-1].date == "2026-11-06"
        assert technical_factor(bars, expected_date="2026-11-06") is not None

    def test_stale_sector_snapshot_forces_industry_and_capital_missing(self):
        bars, ctx, plates = TestScoreCandidate()._full_inputs()
        r = score_candidate(
            "sz002202", "金风科技", "风电设备",
            growth=90, growth_evidence="https://example.com/evidence",
            bars=bars, sector_ctx=ctx, plates=plates,
            valuation={"pe": 15.0, "pb": 2.0},
            expected_date="2026-11-06", snapshot_date="2026-11-05",
        )
        assert "行业景气" in r.missing
        assert "资金面" in r.missing
        assert r.recommended is False

    def test_fresh_snapshot_scores_normally(self):
        bars, ctx, plates = TestScoreCandidate()._full_inputs()
        r = score_candidate(
            "sz002202", "金风科技", "风电设备",
            growth=90, growth_evidence="https://example.com/evidence",
            bars=bars, sector_ctx=ctx, plates=plates,
            valuation={"pe": 15.0, "pb": 2.0},
            expected_date="", snapshot_date="",
        )
        assert "行业景气" not in r.missing
        assert "资金面" not in r.missing


class TestCapitalZeroPool:
    """资金分位池：zljlr_d20 为 NULL=无数据（不进池）；0=真实零净流入（有效样本）"""

    def test_all_null_pool_returns_none(self):
        pool = [SectorSnapshot(code=f"b{i}", name=f"板块{i}", kind="plate",
                               zljlr_d20=None) for i in range(32)]
        assert capital_factor(pool, "板块3") is None

    def test_zero_is_valid_sample(self):
        """全 0 池 = 真实零净流入，参与分位（不再是"无数据"）"""
        pool = [SectorSnapshot(code=f"b{i}", name=f"板块{i}", kind="plate",
                               zljlr_d20=0) for i in range(32)]
        assert capital_factor(pool, "板块3") is not None

    def test_low_coverage_returns_none(self):
        """仅 5/31 板块有资金数据（覆盖率 16%）→ 分位不可信 → 缺失"""
        pool = [SectorSnapshot(code=f"b{i}", name=f"板块{i}", kind="plate",
                               zljlr_d20=(10 if i < 5 else None)) for i in range(31)]
        assert capital_factor(pool, "板块0") is None

    def test_chosen_sector_without_data_returns_none(self):
        """自身板块资金为 NULL（当日无数据）→ 缺失，不能拿别的板块顶替"""
        pool = _cap_pool([_plate("风电设备", None)])
        assert capital_factor(pool, "风电设备") is None


class TestGrowthFromEarnings:
    """业绩因子唯一口径（2026-11-10）：东财归母同比 → 固定桶表"""

    def test_net_yoy_passes_bucket(self):
        score, evidence = growth_from_earnings(
            {"net_yoy": 36.01, "net_profit": 8.71e8}, code="600535")
        assert score == 84.0
        assert evidence == "https://data.eastmoney.com/bbsj/600535.html"

    def test_missing_rate_is_missing_not_zero(self):
        """拉不到同比 → 缺失（None），不得折 0 当有值"""
        assert growth_from_earnings(None, "600535") == (None, "")
        assert growth_from_earnings({"net_yoy": None, "net_profit": 1e8},
                                    "600535") == (None, "")

    def test_loss_capped_even_with_high_growth(self):
        score, _ = growth_from_earnings(
            {"net_yoy": 80.0, "net_profit": -1.2e8}, code="000001")
        assert score == 25.0

    def test_amount_unknown_keeps_bucket(self):
        score, _ = growth_from_earnings({"net_yoy": 60.0, "net_profit": None})
        assert score == 90.0


class TestGrowthFromResearch:
    def test_parses_koufei_growth_with_evidence(self):
        notes = [ResearchNote(
            "sh600535", "天士力", "基本面", "2026H1扣非+36.01%",
            "营收42.01亿(-2.03%)，归母8.93亿(+15.23%)，扣非8.71亿(+36.01%)",
            "https://stcn.com/article/detail/4018343.html",
        )]
        score, evidence = growth_from_research(notes)
        assert score == 84.0                      # 扣非 +36% → 30–50 桶
        assert evidence == "https://stcn.com/article/detail/4018343.html"

    def test_parses_guimu_net_profit_with_unit_words(self):
        """归母净利1009万(+224.19%) 这类带单位写法也能解析"""
        notes = [ResearchNote(
            "sz002366", "融发核电", "基本面", "2026Q1净利+224%",
            "营收2.14亿(+38.26%)，归母净利1009万(+224.19%)",
            "https://stcn.com/article/detail/3822552.html",
        )]
        score, evidence = growth_from_research(notes)
        assert score == 90.0                      # +224% → 高增长桶
        assert evidence == "https://stcn.com/article/detail/3822552.html"

    def test_loss_making_capped(self):
        """归母金额为负 → 亏损封顶 25，不因低基数高增速而虚高"""
        notes = [ResearchNote(
            "sz000001", "某公司", "基本面", "扭亏但净利仍为负",
            "营收10.2亿(+5%)，归母净利-1.20亿(+80.00%)",
            "http://example.com",
        )]
        score, _ = growth_from_research(notes)
        assert score == 25.0

    def test_no_match_returns_none(self):
        notes = [ResearchNote("sz000001", "平安", "基本面", "业绩平稳",
                              "经营稳健，无增速数据", "http://example.com")]
        assert growth_from_research(notes) == (None, "")
        assert growth_from_research([]) == (None, "")

    def test_growth_score_bands(self):
        assert growth_score_from_rate(60) == 90
        assert growth_score_from_rate(50) == 90
        assert growth_score_from_rate(2000) == 90    # 极端高增速仍落在高增长桶
        assert growth_score_from_rate(30) == 84
        assert growth_score_from_rate(20) == 80
        assert growth_score_from_rate(10) == 75
        assert growth_score_from_rate(5) == 72
        assert growth_score_from_rate(0) == 70
        assert growth_score_from_rate(-1) == 60      # 小幅下滑
        assert growth_score_from_rate(-5) == 60      # 边界：-5 归入小幅下滑桶
        assert growth_score_from_rate(-15) == 50     # 边界：-15 归入明显下滑桶
        assert growth_score_from_rate(-20) == 40
        assert growth_score_from_rate(-31) == 30

    def test_growth_score_monotonic(self):
        rates = [-60, -31, -30, -20, -15, -6, -5, -1, 0, 5, 10, 20, 30, 50, 80]
        scores = [growth_score_from_rate(r) for r in rates]
        assert all(a <= b for a, b in zip(scores, scores[1:]))

    def test_loss_cap(self):
        assert growth_score_from_rate(200, profitable=False) == 25
        assert growth_score_from_rate(-40, profitable=False) == 25
        assert growth_score_from_rate(200, profitable=True) == 90
        assert growth_score_from_rate(200, profitable=None) == 90
