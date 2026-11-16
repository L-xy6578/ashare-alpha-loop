"""调研情报弹药库测试（第一层）"""

import pytest

from src.research import ResearchStore, ResearchNote, init_research_db


@pytest.fixture()
def store(tmp_path):
    db = str(tmp_path / "research_test.db")
    init_research_db(db)
    s = ResearchStore(db)
    s.add("sz002366", "融发核电", "基本面", "Q1净利+224%", "归母1009万(+224%)",
          "https://stcn.com/article/detail/3822552.html", "2026-08-03 14:00:00")
    s.add("sz002366", "融发核电", "风险", "订单周期长", "交付约3年",
          "https://cls.cn/detail/x", "2026-08-03 14:10:00")
    s.add("sh600019", "宝钢股份", "估值", "破净0.64", "PB≈0.64",
          "http://lxjzb.qianlong.com.cn/cpbd/600019.html", "2026-08-03 14:20:00")
    return s


def test_add_and_count(store):
    assert store.count() == 3


def test_query_by_symbol(store):
    notes = store.query(symbol="sz002366")
    assert len(notes) == 2
    assert all(n.symbol == "sz002366" for n in notes)
    # 按时间倒序：风险(14:10)在前
    assert notes[0].category == "风险"


def test_query_by_category(store):
    notes = store.query(category="估值")
    assert len(notes) == 1
    assert notes[0].symbol == "sh600019"


def test_query_both_filters(store):
    notes = store.query(symbol="sz002366", category="基本面")
    assert len(notes) == 1
    assert notes[0].title == "Q1净利+224%"


def test_latest(store):
    latest = store.latest("sz002366")
    assert latest is not None
    assert latest.category == "风险"  # 时间最新的一条
    latest_fund = store.latest("sz002366", category="基本面")
    assert latest_fund is not None
    assert latest_fund.title == "Q1净利+224%"


def test_latest_none_for_unknown(store):
    assert store.latest("sh999999") is None


def test_invalid_category_raises(store):
    with pytest.raises(ValueError):
        store.add("sh600535", "天士力", "非法类别", "t", "c")


def test_source_url_preserved(store):
    """AH-08 证据要求：来源 URL 必须可追溯"""
    notes = store.query(symbol="sz002366", category="基本面")
    assert notes[0].source_url == "https://stcn.com/article/detail/3822552.html"


def test_note_dataclass(store):
    notes = store.query(symbol="sh600019")
    assert isinstance(notes[0], ResearchNote)
    assert notes[0].name == "宝钢股份"
