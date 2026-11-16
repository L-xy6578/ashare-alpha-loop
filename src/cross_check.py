"""
三角闭环选股体系 — 数据源交叉校验层

背景：2026-07-31 实测发现腾讯HTTP数据源对宝钢(sh600019)缺 07-31 数据，
而 westock-mcp 有——不同数据源存在同步/口径差异，单源采集会踩坑。
本模块提供多源交叉比对：对同一标的同一日期区间，从两个源分别拉数据，
比对 OHLC，输出差异清单与报警信号。

用法:
    from src.cross_check import cross_validate, CrossCheckReport

    report = cross_validate(
        source_a=TencentHttpDataSource(),
        source_b=SomeOtherSource(),
        symbol="sh600019",
        start="2026-07-01",
        end="2026-07-31",
    )
    print(report.summary())
    if report.has_conflict:
        for issue in report.issues:
            print(issue)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .data_source import DataSource
from .logger import get_logger

log = get_logger(__name__)

# 默认容差：OHLC 相对差异超过 0.5% 记为冲突
DEFAULT_TOLERANCE_PCT = 0.5


@dataclass(frozen=True)
class CrossCheckIssue:
    """一条 OHLC 冲突记录"""

    date: str
    field: str          # open / close / high / low
    value_a: float      # 源 A 的值
    value_b: float      # 源 B 的值
    diff_pct: float     # 相对差异百分比（相对 A）

    def __str__(self) -> str:
        return (f"{self.date} {self.field}: A={self.value_a} vs B={self.value_b} "
                f"(diff {self.diff_pct:+.2f}%)")


@dataclass
class CrossCheckReport:
    """一次交叉校验的完整结果"""

    symbol: str
    source_a_name: str
    source_b_name: str
    total_days: int                 # 双方共有交易日数
    issues: list[CrossCheckIssue] = field(default_factory=list)
    missing_in_b: list[str] = field(default_factory=list)   # A 有而 B 缺的日期
    missing_in_a: list[str] = field(default_factory=list)   # B 有而 A 缺的日期

    @property
    def has_conflict(self) -> bool:
        """存在 OHLC 冲突或数据缺失（视为需要人工关注的信号）"""
        return bool(self.issues) or bool(self.missing_in_b) or bool(self.missing_in_a)

    def summary(self) -> str:
        status = "❌ 存在冲突" if self.has_conflict else "✅ 一致"
        lines = [
            f"[{status}] {self.symbol} {self.source_a_name} vs {self.source_b_name}",
            f"  共有交易日 {self.total_days} 天 | OHLC 冲突 {len(self.issues)} 条"
            f" | B 缺 {len(self.missing_in_b)} 天 | A 缺 {len(self.missing_in_a)} 天",
        ]
        if self.missing_in_b:
            lines.append(f"  ⚠️ A有B缺: {', '.join(self.missing_in_b[:8])}")
        if self.missing_in_a:
            lines.append(f"  ⚠️ B有A缺: {', '.join(self.missing_in_a[:8])}")
        if self.issues:
            for issue in self.issues[:5]:
                lines.append(f"  🔴 {issue}")
            if len(self.issues) > 5:
                lines.append(f"  ... 其余 {len(self.issues) - 5} 条")
        return "\n".join(lines)


def _pct_diff(a: float, b: float) -> float:
    """相对差异百分比（相对 a），a 为 0 时回退到绝对差异"""
    if a == 0:
        return abs(b - a) * 100.0
    return (b - a) / a * 100.0


def cross_validate(
    source_a: DataSource,
    source_b: DataSource,
    symbol: str,
    start: str | None = None,
    end: str | None = None,
    tolerance_pct: float = DEFAULT_TOLERANCE_PCT,
) -> CrossCheckReport:
    """对同一标的执行两源交叉校验

    Args:
        source_a: 主数据源（基准）
        source_b: 对照数据源
        symbol: 标的代码，如 "sh600019"
        start/end: 日期区间（含）
        tolerance_pct: OHLC 相对差异容差（%），超过记为冲突

    Returns:
        CrossCheckReport 完整结果
    """
    bars_a = source_a.daily_kline(symbol, start, end)
    bars_b = source_b.daily_kline(symbol, start, end)

    name_a = type(source_a).__name__
    name_b = type(source_b).__name__
    log.info("交叉校验 %s: A(%s)=%d根, B(%s)=%d根",
             symbol, name_a, len(bars_a), name_b, len(bars_b))

    by_date_a = {b.date: b for b in bars_a}
    by_date_b = {b.date: b for b in bars_b}

    common_dates = sorted(set(by_date_a) & set(by_date_b))
    missing_in_b = sorted(set(by_date_a) - set(by_date_b))
    missing_in_a = sorted(set(by_date_b) - set(by_date_a))

    issues: list[CrossCheckIssue] = []
    for date in common_dates:
        a, b = by_date_a[date], by_date_b[date]
        for f in ("open", "close", "high", "low"):
            va, vb = getattr(a, f), getattr(b, f)
            if va is None or vb is None:
                continue
            diff = _pct_diff(va, vb)
            if abs(diff) > tolerance_pct:
                issues.append(CrossCheckIssue(date, f, va, vb, round(diff, 2)))

    report = CrossCheckReport(
        symbol=symbol,
        source_a_name=name_a,
        source_b_name=name_b,
        total_days=len(common_dates),
        issues=issues,
        missing_in_b=missing_in_b,
        missing_in_a=missing_in_a,
    )

    if report.has_conflict:
        log.warning("交叉校验 %s: %s", symbol, report.summary().replace("\n", " | "))
    else:
        log.info("交叉校验 %s: 一致（%d 天）", symbol, len(common_dates))
    return report


def cross_validate_many(
    sources: tuple[DataSource, DataSource],
    symbols: list[str],
    start: str | None = None,
    end: str | None = None,
    tolerance_pct: float = DEFAULT_TOLERANCE_PCT,
) -> list[CrossCheckReport]:
    """批量交叉校验多个标的

    Args:
        sources: (主源, 对照源)
        symbols: 标的列表
    """
    reports = [
        cross_validate(sources[0], sources[1], sym, start, end, tolerance_pct)
        for sym in symbols
    ]
    return reports
