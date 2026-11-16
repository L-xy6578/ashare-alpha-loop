"""数据源交叉校验层测试"""

from src.cross_check import cross_validate, CrossCheckReport
from src.data_source import KlineBar, StaticDataSource


def _src_with(name: str, bars: dict[str, list[KlineBar]]) -> StaticDataSource:
    src = StaticDataSource()
    for sym, bs in bars.items():
        src.set_symbol(sym, bs)
    src.name = name  # type: ignore[attr-defined]
    return src


def _bars(days: list[tuple[str, float, float, float, float]]) -> list[KlineBar]:
    return [KlineBar(d, o, c, h, l) for d, o, c, h, l in days]


# ----------------------------------------------------------------
# 基础场景
# ----------------------------------------------------------------

def test_identical_sources_no_conflict():
    """两源数据完全一致 → 无冲突"""
    days = [("2026-07-29", 15.78, 15.95, 16.10, 15.78),
            ("2026-07-30", 15.95, 16.05, 16.13, 15.68),
            ("2026-07-31", 16.05, 16.12, 16.13, 16.00)]
    a = _src_with("A", {"sh600535": _bars(days)})
    b = _src_with("B", {"sh600535": _bars(days)})

    report = cross_validate(a, b, "sh600535", "2026-07-29", "2026-07-31")

    assert not report.has_conflict
    assert report.total_days == 3
    assert len(report.issues) == 0
    assert report.missing_in_a == []
    assert report.missing_in_b == []


def test_price_diff_detected():
    """收盘价差异超过容差 → 冲突"""
    days_a = [("2026-07-29", 15.78, 15.95, 16.10, 15.78)]
    days_b = [("2026-07-29", 15.78, 16.30, 16.10, 15.78)]  # close 差 2.2%
    a = _src_with("A", {"sh600535": _bars(days_a)})
    b = _src_with("B", {"sh600535": _bars(days_b)})

    report = cross_validate(a, b, "sh600535")

    assert report.has_conflict
    assert len(report.issues) == 1
    issue = report.issues[0]
    assert issue.field == "close"
    assert issue.date == "2026-07-29"
    assert issue.value_a == 15.95
    assert issue.value_b == 16.30


def test_small_diff_within_tolerance():
    """微小差异在容差内 → 不算冲突"""
    days_a = [("2026-07-29", 15.78, 15.95, 16.10, 15.78)]
    days_b = [("2026-07-29", 15.78, 15.96, 16.10, 15.78)]  # close 差 0.06%
    a = _src_with("A", {"sh600535": _bars(days_a)})
    b = _src_with("B", {"sh600535": _bars(days_b)})

    report = cross_validate(a, b, "sh600535")

    assert not report.has_conflict


def test_custom_tolerance():
    """自定义容差生效"""
    days_a = [("2026-07-29", 10.00, 10.00, 10.00, 10.00)]
    days_b = [("2026-07-29", 10.00, 10.20, 10.00, 10.00)]  # close 差 2%
    a = _src_with("A", {"sh600019": _bars(days_a)})
    b = _src_with("B", {"sh600019": _bars(days_b)})

    assert cross_validate(a, b, "sh600019", tolerance_pct=3.0).has_conflict is False
    assert cross_validate(a, b, "sh600019", tolerance_pct=1.0).has_conflict is True


# ----------------------------------------------------------------
# 数据缺失（同步差异）场景 —— 宝钢缺 07-31 教训
# ----------------------------------------------------------------

def test_missing_day_in_b_detected():
    """B 源缺某天（如宝钢缺 07-31）→ 记录 missing_in_b"""
    days_a = [("2026-07-29", 1, 1, 1, 1), ("2026-07-30", 1, 1, 1, 1), ("2026-07-31", 1, 1, 1, 1)]
    days_b = [("2026-07-29", 1, 1, 1, 1), ("2026-07-30", 1, 1, 1, 1)]
    a = _src_with("A", {"sh600019": _bars(days_a)})
    b = _src_with("B", {"sh600019": _bars(days_b)})

    report = cross_validate(a, b, "sh600019")

    assert report.has_conflict
    assert report.missing_in_b == ["2026-07-31"]
    assert "07-31" in report.summary()


def test_extra_day_in_b_detected():
    """B 源多出某天 → 记录 missing_in_a"""
    days_a = [("2026-07-29", 1, 1, 1, 1)]
    days_b = [("2026-07-29", 1, 1, 1, 1), ("2026-07-30", 1, 1, 1, 1)]
    a = _src_with("A", {"sh600019": _bars(days_a)})
    b = _src_with("B", {"sh600019": _bars(days_b)})

    report = cross_validate(a, b, "sh600019")

    assert report.missing_in_a == ["2026-07-30"]


def test_empty_source_a():
    """A 源无数据 → 全部记录为 missing_in_b 之外的空报告（不崩溃）"""
    a = _src_with("A", {})
    b = _src_with("B", {"sh600535": _bars([("2026-07-29", 1, 1, 1, 1)])})

    report = cross_validate(a, b, "sh600535")

    assert report.total_days == 0
    assert report.missing_in_a == ["2026-07-29"]
    assert report.has_conflict


# ----------------------------------------------------------------
# 其他行为
# ----------------------------------------------------------------

def test_volume_ignored():
    """volume/amount 不参与 OHLC 比对（只比 open/close/high/low）"""
    days_a = [("2026-07-29", 15.78, 15.95, 16.10, 15.78, 1000.0, 2000.0)]
    days_b = [("2026-07-29", 15.78, 15.95, 16.10, 15.78, 8888.0, 9999.0)]
    a = _src_with("A", {"sh600535": [KlineBar(*days_a[0])]})
    b = _src_with("B", {"sh600535": [KlineBar(*days_b[0])]})

    report = cross_validate(a, b, "sh600535")

    assert not report.has_conflict


def test_date_range_filter_applied():
    """start/end 过滤传递给两个源"""
    days = [("2026-07-28", 1, 1, 1, 1), ("2026-07-29", 1, 1, 1, 1), ("2026-07-31", 1, 1, 1, 1)]
    a = _src_with("A", {"sh600535": _bars(days)})
    b = _src_with("B", {"sh600535": _bars(days)})

    report = cross_validate(a, b, "sh600535", start="2026-07-29", end="2026-07-31")

    assert report.total_days == 2  # 07-28 被过滤
    assert "2026-07-28" not in report.missing_in_a


def test_report_type_and_summary():
    """返回类型与 summary 字符串格式"""
    days = [("2026-07-29", 15.78, 15.95, 16.10, 15.78)]
    a = _src_with("A", {"sh600535": _bars(days)})
    b = _src_with("B", {"sh600535": _bars(days)})

    report = cross_validate(a, b, "sh600535")

    assert isinstance(report, CrossCheckReport)
    s = report.summary()
    assert "✅ 一致" in s
    assert "sh600535" in s
