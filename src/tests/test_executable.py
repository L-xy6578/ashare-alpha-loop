"""可执行性过滤（板块权限 / 价格 / ST / 次新 / 手数）单元测试"""

from datetime import date, timedelta

import pytest

from src.data_source import KlineBar
from src.executable import (check_eligibility, filter_universe, first_lot_shares,
                            board_of, is_buyable_code, max_shares_for_position)


def _bars(n=70, last="2026-11-06", px=10.0):
    end = date.fromisoformat(last)
    out = []
    for i in range(n):
        d = (end - timedelta(days=n - 1 - i)).isoformat()
        out.append(KlineBar(d, px, px, px * 1.01, px * 0.99))
    return out


class _Item:
    def __init__(self, symbol, code, name):
        self.symbol, self.code, self.name = symbol, code, name


@pytest.mark.parametrize("code,symbol,board", [
    ("600019", "sh600019", "沪主板"),
    ("601398", "sh601398", "沪主板"),
    ("603259", "sh603259", "沪主板"),
    ("605123", "sh605123", "沪主板"),
    ("000001", "sz000001", "深主板"),
    ("001979", "sz001979", "深主板"),
    ("002979", "sz002979", "深主板"),
    ("003816", "sz003816", "深主板"),
    ("300124", "sz300124", "创业板"),
    ("301510", "sz301510", "创业板"),
    ("688320", "sh688320", "科创板"),
    ("689009", "sh689009", "科创板"),
    ("830799", "bj830799", "北交所"),
    ("920001", "bj920001", "北交所"),
])
def test_board_permission_prefixes(code, symbol, board):
    assert board_of(code, symbol) == board
    ok, reason = is_buyable_code(code, symbol)
    if board in ("创业板", "科创板", "北交所"):
        assert not ok and "仅分析不可买" in reason
        e = check_eligibility(symbol, code, "测试", 10.0, bars=_bars())
        assert not e.ok and e.analysis_only
    else:
        assert ok


def test_main_board_eligible_and_lot_sizing():
    e = check_eligibility("sh600019", "600019", "宝钢股份", 6.11,
                          bars=_bars(), listed_days=200, as_of="2026-11-06")
    assert e.ok
    # 611 < 1500 → 首仓 2 手
    assert first_lot_shares(6.11) == 200
    assert e.first_shares == 200
    assert e.max_shares == 900            # 6000/611 → 900 股


def test_first_lot_boundary_1500():
    assert first_lot_shares(14.99) == 200   # 1499 < 1500
    assert first_lot_shares(15.00) == 100   # 1500 不 < 1500


def test_max_shares_rounding():
    assert max_shares_for_position(60.0) == 100
    assert max_shares_for_position(59.01) == 100
    assert max_shares_for_position(6.0) == 1000
    assert max_shares_for_position(0) == 0


def test_price_upper_bound():
    e = check_eligibility("sh600519", "600519", "贵州茅台", 61.0,
                          bars=_bars(), listed_days=200, as_of="2026-11-06")
    assert not e.ok and "60" in e.reason
    e2 = check_eligibility("sh600519", "600519", "贵州茅台", 60.0,
                           bars=_bars(), listed_days=200, as_of="2026-11-06")
    assert e2.ok


def test_st_suspended_and_new_stock():
    st = check_eligibility("sh600001", "600001", "ST测试", 10.0, bars=_bars())
    assert not st.ok and "ST" in st.reason
    susp = check_eligibility("sh600001", "600001", "测试", 10.0,
                             bars=_bars(), as_of="2026-11-07")
    assert not susp.ok and "停牌" in susp.reason
    new = check_eligibility("sh600001", "600001", "测试", 10.0,
                            bars=_bars(n=59), listed_days=59, as_of="2026-11-06")
    assert not new.ok and "60" in new.reason


def test_filter_universe_splits_analysis_only_and_other():
    items = [
        _Item("sh600019", "600019", "宝钢股份"),
        _Item("sz301510", "301510", "固高科技"),
        _Item("sh688320", "688320", "禾川科技"),
        _Item("sz300124", "300124", "汇川技术"),
        _Item("sh600519", "600519", "贵州茅台"),
    ]
    bars = {i.symbol: _bars(px=61.0 if i.code == "600519" else 10.0) for i in items}
    eligible, excluded = filter_universe(
        items, bars, as_of="2026-11-06",
        listed_days_by_symbol={i.symbol: 200 for i in items},
    )
    elig_symbols = {i.symbol for i, _ in eligible}
    assert "sh600019" in elig_symbols
    assert "sz301510" not in elig_symbols
    analysis = [e for e in excluded if e.analysis_only]
    assert {e.code for e in analysis} == {"301510", "688320", "300124"}
    other = [e for e in excluded if not e.analysis_only]
    assert {e.code for e in other} == {"600519"}
