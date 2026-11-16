"""财报自动采集测试 — note 格式 + 五因子业绩解析集成"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.factors import growth_from_research
from src.finance_feed import build_note
from src.research import ResearchNote


def test_build_note_full_fields():
    e = {"report_date": "2026-06-30 00:00:00", "revenue": 9.2278e10, "revenue_yoy": 1.30,
         "net_profit": 4.4517e10, "net_yoy": -1.95}
    title, content, url = build_note("600519", "贵州茅台", e)
    assert "财报自动采集" in title
    assert "营收922.78亿(+1.30%)" in content
    assert "归母445.17亿(-1.95%)" in content
    assert "600519" in url


def test_build_note_minimal():
    e = {"report_date": "2026-06-30", "revenue": None, "net_profit": 1.0e8, "net_yoy": 5.0}
    title, content, url = build_note("000001", "平安银行", e)
    assert "归母1.00亿(+5.00%)" in content


def test_note_feeds_growth_score():
    """#109：自动采集 note 可被五因子业绩解析（归母增速）"""
    e = {"report_date": "2026-06-30", "revenue": 9.2278e10, "revenue_yoy": 1.30,
         "net_profit": 4.4517e10, "net_yoy": 25.0}
    _, content, _ = build_note("600519", "贵州茅台", e)
    note = ResearchNote(symbol="sh600519", name="贵州茅台", category="基本面",
                        title="t", content=content, source_url="u")
    score, evidence = growth_from_research([note])
    assert score == 80.0          # 归母 +25% → 80 分档
    assert evidence
