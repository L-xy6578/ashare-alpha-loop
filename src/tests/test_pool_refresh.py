"""观察池换血（#59 演练版）：硬门槛 / 单轮上限 / 演练不写生产池"""

import json
from datetime import date, timedelta

import pytest

from src import pool_refresh
from src.data_source import KlineBar
from src.pool_refresh import (
    MAX_NEW_PER_RUN,
    MAX_SCAN_PER_RUN,
    BarSeries,
    apply_new,
    board_plan,
    eligible_boards,
    plan,
    write_report,
)
from src.sector_analysis import SectorSnapshot
from src.watchlist import WatchlistStore


class _Track:
    def __init__(self, name, score, zdf20=10.0, zdf60=20.0):
        self.name = name
        self.score = score
        self.zdf20 = zdf20
        self.zdf60 = zdf60


class _Analyzer:
    """注入式赛道分析器：固定两条主赛道 + 板块快照"""

    def __init__(self, tracks, plates):
        self._tracks = tracks
        self._plates = plates

    def analyze(self):
        return {"date": "2026-10-29", "monthly_tracks": self._tracks[:2],
                "quarterly_tracks": self._tracks[2:],
                "all_tracks": self._tracks, "sub_sectors": {}}

    def load_date(self, trade_date=None, kind="plate", exclude_suspicious=False):
        return self._plates if kind == "plate" else []

    def snapshot_info(self, trade_date=None):
        return {"date": "2026-10-29", "source": "eastmoney",
                "plate_count": len(self._plates), "concept_count": 0,
                "complete": True, "note": ""}


def _bars(n=25, base=10.0, rising=True):
    d = date(2026, 9, 20)
    out = []
    for i in range(n):
        c = base + (i * 0.5 if rising else 0)
        out.append(KlineBar((d + timedelta(days=i)).isoformat(), c, c, c, c))
    return out


@pytest.fixture()
def env(tmp_path):
    market_db = str(tmp_path / "market.db")
    watch = WatchlistStore(market_db)
    tracks = [_Track("电机Ⅱ", 95.0), _Track("数字媒体", 90.0)]
    plates = [
        SectorSnapshot(code="bk_a", name="电机Ⅱ", kind="plate", date="2026-10-29",
                       zdf20=20.0, zljlr_d20=5000.0),
        SectorSnapshot(code="bk_b", name="数字媒体", kind="plate", date="2026-10-29",
                       zdf20=10.0, zljlr_d20=-2000.0),
    ]
    # 分位基准至少 30 个板块（2026-10-30 新口径）
    plates += [
        SectorSnapshot(code=f"bk_f{i}", name=f"填充板块{i}", kind="plate",
                       date="2026-10-29", zdf20=-20.0 + i, zljlr_d20=-1000.0 + i * 10)
        for i in range(30)
    ]
    analyzer = _Analyzer(tracks, plates)
    members = {
        "电机Ⅱ": [
            {"code": "600001", "name": "候选甲"},
            {"code": "600002", "name": "候选乙"},
            {"code": "600003", "name": "候选丙"},
            {"code": "600004", "name": "候选丁"},
            {"code": "600005", "name": "K线不足股"},
            {"code": "600006", "name": "无业绩股"},
            {"code": "600007", "name": "缺估值股"},
            {"code": "600008", "name": "ST测试股"},
        ],
        "数字媒体": [{"code": "600009", "name": "候选戊"}],
    }
    earnings = {
        "600001": {"report_date": "2026-09-30", "revenue": 1e8, "revenue_yoy": 20.0,
                   "net_profit": 1e8, "net_yoy": 60.0},
        "600002": {"report_date": "2026-09-30", "revenue": 1e8, "revenue_yoy": 10.0,
                   "net_profit": 1e8, "net_yoy": 60.0},
        "600003": {"report_date": "2026-09-30", "revenue": 1e8, "revenue_yoy": 10.0,
                   "net_profit": 1e8, "net_yoy": 60.0},
        "600004": {"report_date": "2026-09-30", "revenue": 1e8, "revenue_yoy": 10.0,
                   "net_profit": 1e8, "net_yoy": 60.0},
        "600005": {"report_date": "2026-09-30", "revenue": 1e8, "revenue_yoy": 10.0,
                   "net_profit": 1e8, "net_yoy": 60.0},
        "600006": {"report_date": "2026-09-30", "revenue": 1e8, "revenue_yoy": 10.0,
                   "net_profit": 1e8, "net_yoy": None},
        "600007": {"report_date": "2026-09-30", "revenue": 1e8, "revenue_yoy": 10.0,
                   "net_profit": 1e8, "net_yoy": 60.0},
        "600009": {"report_date": "2026-09-30", "revenue": 1e8, "revenue_yoy": 10.0,
                   "net_profit": 1e8, "net_yoy": 20.0},
    }

    def bars_provider(symbol, start, end):
        code = symbol[2:]
        if code == "600005":
            return _bars(5)      # 门槛：K线 < 10
        return _bars(25)

    def valuation_provider(symbol):
        if symbol == "sh600007":
            return {}            # 门槛：估值缺失
        return {"pe": 15.0, "pb": 2.0}

    return dict(
        tmp_path=tmp_path, watch=watch, analyzer=analyzer,
        constituents=lambda board: members.get(board, []),
        bars=bars_provider, earnings=lambda code: earnings.get(code),
        valuation=valuation_provider,
    )


def _plan(env, **kw):
    params = dict(
        scope="tracks",
        watch_store=env["watch"], analyzer=env["analyzer"],
        constituents_provider=env["constituents"], bars_provider=env["bars"],
        earnings_provider=env["earnings"], valuation_provider=env["valuation"],
        throttle=0,
    )
    params.update(kw)
    return plan("2026-10-29", **params)


def test_gates_and_max_new(env):
    result = _plan(env)
    by_code = {c.code: c for c in result["scanned"]}

    # 门槛拦截（README 三条硬门槛，逐条可验证）
    assert "K线5根<10" in by_code["600005"].status
    assert "无业绩证据" in by_code["600006"].status
    assert "缺估值" in by_code["600007"].status
    assert "600008" not in by_code                       # ST/退 直接不评估

    # 达标候选按分数排序 + 单轮上限（不足上限时全取）
    assert len([c for c in result["scanned"] if c.status.startswith("候选")]) == 4
    assert len(result["new"]) == min(4, MAX_NEW_PER_RUN)
    assert result["stats"]["new"] == min(4, MAX_NEW_PER_RUN)
    weights = [c.result.weighted for c in result["new"]]
    assert weights == sorted(weights, reverse=True)


def test_plan_does_not_touch_watchlist(env):
    before = {i.symbol for i in env["watch"].all()}
    removed_before = {i.symbol for i in env["watch"].all(status="removed")}
    _plan(env)
    assert {i.symbol for i in env["watch"].all()} == before
    assert {i.symbol for i in env["watch"].all(status="removed")} == removed_before


def test_report_is_idempotent_and_marked_pending(env):
    result = _plan(env)
    out = env["tmp_path"] / "out"
    path1 = write_report(result, out_dir=out)
    text1 = path1.read_text(encoding="utf-8")
    path2 = write_report(result, out_dir=out)
    assert path1 == path2
    assert path2.read_text(encoding="utf-8") == text1          # 覆盖不追加
    assert "待用户确认" in text1
    assert f"观察池换血_{result['today']}.md" == path1.name
    assert "淘汰规则" in text1


def test_apply_new_writes_at_most_new_limit(env):
    result = _plan(env)
    added = apply_new(result, env["watch"])
    assert len(added) == min(4, MAX_NEW_PER_RUN)
    for sym in added:
        item = env["watch"].get(sym)
        assert item.source == "auto"
        assert item.status == "active"
        assert item.sector in ("电机Ⅱ", "数字媒体")


def test_rebalance_exits_long_term_low_score(env):
    """近 20 个评分日 ≥10 次且均分 <70 → 软删；持仓标的豁免"""
    from src.factor_store import FactorSnapshotStore
    from src.pool_refresh import rebalance

    db = str(env["tmp_path"] / "market.db")
    fs = FactorSnapshotStore(db)
    for i in range(10):
        fs.save_many(f"2026-09-{i + 1:02d}", [
            {"symbol": "sz002366", "score": 60.0},
            {"symbol": "sz300880", "score": 62.0},
            {"symbol": "sh600535", "score": 88.0},
        ])
    rb = rebalance({"new": []}, env["watch"], market_db=db, protected={"sz002366"})
    assert {r["symbol"] for r in rb["removed"]} == {"sz300880"}
    assert env["watch"].get("sz300880").status == "removed"
    assert env["watch"].get("sz002366").status == "active"    # 持仓豁免
    assert env["watch"].get("sh600535").status == "active"    # 高分保留


def test_rebalance_respects_capacity(env, monkeypatch):
    """池容量上限：余量不足时只写入余量允许的只数"""
    from src.pool_refresh import rebalance

    monkeypatch.setattr(pool_refresh, "POOL_CAP", 6)   # 池内 5 只 → 余量 1
    rb = rebalance(_plan(env), env["watch"],
                   market_db=str(env["tmp_path"] / "market.db"))
    assert rb["room"] == 1
    assert len(rb["added"]) == 1
    assert len(env["watch"].all()) == 6


def test_sina_bars_fallback_parses_and_filters(monkeypatch):
    """腾讯限流（HTTP 501）时新浪兜底可用，且只保留窗口内 K 线"""
    payload = json.dumps([
        {"day": "2026-09-20", "open": "9.0", "high": "9.2", "low": "8.8",
         "close": "9.1", "volume": "1000"},
        {"day": "2026-10-28", "open": "10.0", "high": "10.5", "low": "9.9",
         "close": "10.3", "volume": "2000"},
    ])
    monkeypatch.setattr(pool_refresh, "fetch_bytes", lambda *a, **k: payload.encode())
    bars = pool_refresh.fetch_sina_bars("sz000858", "2026-10-01", "2026-10-29")
    assert [b.date for b in bars] == ["2026-10-28"]
    assert bars[0].close == 10.3


def test_raw_fallback_marks_technical_missing(env):
    """回退新浪不复权时：技术因子必须缺失并告警，不能用 raw K 线算出降级分数"""
    def bars_provider(symbol, start, end):
        if symbol == "sh600001":
            return BarSeries(_bars(25), source="sina", adjust="raw")
        return BarSeries(_bars(25), source="tencent", adjust="qfq")

    result = _plan(env, bars_provider=bars_provider)
    row = next(c for c in result["scanned"] if c.code == "600001")
    assert row.result is not None
    assert row.result.incomplete
    assert "技术面" in row.result.missing
    assert row.status.startswith("门槛未过：缺技术面")
    assert result["data_sources"].get("sina/raw") == 1
    assert any("非前复权" in g for g in result["gaps"])


# ============================================================
# 扩围（全行业板块分层配额）
# ============================================================

def test_board_plan_covers_all_tiers():
    boards = [{"name": f"板块{i}", "code": f"bk_{i}", "zdf20": 100 - i}
              for i in range(100)]
    planned = board_plan(boards)
    assert len(planned) == 60                    # 热10 + 中20 + 广25 + 冷5
    assert all(b["quota"] >= 1 for b in planned)
    assert sum(b["quota"] for b in planned) == 100
    tiers = {b["tier"] for b in planned}
    assert tiers == {"hot", "mid", "broad", "cold"}
    assert [b["quota"] for b in planned[:10]] == [3] * 10
    assert [b["quota"] for b in planned[10:30]] == [2] * 20


def test_board_plan_small_universe():
    boards = [{"name": f"板块{i}", "code": f"bk_{i}", "zdf20": 10 - i}
              for i in range(3)]
    planned = board_plan(boards)
    assert len(planned) == 3
    assert all(b["quota"] == 3 for b in planned)


def test_eligible_boards_excludes_aggregate():
    class _A:
        def load_date(self, trade_date=None, kind="plate", exclude_suspicious=False):
            assert exclude_suspicious is True
            return [SectorSnapshot(code="bk_a", name="银行", kind="plate", zdf20=5),
                    SectorSnapshot(code="bk_b", name="基金增仓", kind="plate", zdf20=9),
                    SectorSnapshot(code="bk_c", name="半导体", kind="plate", zdf20=-3)]

    boards = eligible_boards(_A())
    assert [b["name"] for b in boards] == ["银行", "半导体"]   # 聚合板块被剔除 + 按涨幅降序


def test_fetch_board_stocks_falls_back_to_name(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("clist down")
    monkeypatch.setattr(pool_refresh, "fetch_board_constituents",
                        lambda name: [{"code": "600001", "name": "甲"}])
    stocks, src = pool_refresh.fetch_board_stocks("bk_BK1", "银行", 2, fetcher=boom)
    assert src == "name_fallback"
    assert stocks[0]["code"] == "600001"


class _AllAnalyzer:
    """扩围测试用：70 个板块 + 每板块 2 只成分"""

    def __init__(self, plates):
        self._plates = plates

    def analyze(self):
        return {"date": "2026-10-30",
                "monthly_tracks": [], "quarterly_tracks": [],
                "all_tracks": [], "sub_sectors": {}}

    def load_date(self, trade_date=None, kind="plate", exclude_suspicious=False):
        return self._plates if kind == "plate" else []

    def snapshot_info(self, trade_date=None):
        return {"date": "2026-10-30", "source": "eastmoney",
                "plate_count": len(self._plates), "concept_count": 200,
                "complete": False, "note": ""}


def _all_fixture(tmp_path, n_boards=70, per_board=2):
    plates = [SectorSnapshot(code=f"bk_{i}", name=f"板块{i}", kind="plate",
                             zdf20=100 - i, zljlr_d20=1000 - i)
              for i in range(n_boards)]
    members = {}
    for i in range(n_boards):
        members[f"板块{i}"] = [
            {"code": str(600000 + i * 10 + j), "name": f"股{i}-{j}"}
            for j in range(per_board)]

    def stocks_provider(code, name, limit):
        if name == "板块5":
            raise RuntimeError("拉取失败")
        if name == "板块6":
            return [], "empty"
        return members[name][:limit], "clist_amount"

    def bars(symbol, start, end):
        return BarSeries(_bars(25), source="tencent", adjust="qfq")

    earnings = lambda code: {"report_date": "2026-09-30", "revenue": 1e8,
                             "revenue_yoy": 10, "net_profit": 1e8, "net_yoy": 30}
    valuation = lambda sym: {"pe": 15.0, "pb": 2.0}
    return dict(watch=WatchlistStore(str(tmp_path / "market.db")),
                analyzer=_AllAnalyzer(plates), stocks=stocks_provider,
                bars=bars, earnings=earnings, valuation=valuation)


def _plan_all(env, **kw):
    params = dict(
        scope="all", watch_store=env["watch"], analyzer=env["analyzer"],
        stocks_provider=env["stocks"], bars_provider=env["bars"],
        earnings_provider=env["earnings"], valuation_provider=env["valuation"],
        throttle=0,
    )
    params.update(kw)
    return plan("2026-10-30", **params)


def test_all_scope_coverage_and_limits(tmp_path):
    env = _all_fixture(tmp_path)
    result = _plan_all(env)
    assert result["scope"] == "all"
    assert result["boards_total"] == 70
    assert len(result["scan_boards"]) == 60          # 热10+中20+广25+冷5
    assert 60 <= result["stats"]["scanned"] <= MAX_SCAN_PER_RUN
    assert result["unique_symbols"] >= result["stats"]["scanned"]
    # 每个成功板块至少 1 只进样本（失败板块除外）
    assert result["board_distribution"]
    assert all(cnt >= 1 for _, cnt in result["board_distribution"])


def test_all_scope_failed_boards_counted(tmp_path):
    env = _all_fixture(tmp_path)
    result = _plan_all(env)
    failed = {f["board"] for f in result["boards_failed"]}
    assert "板块5" in failed          # 抛异常
    assert "板块6" in failed          # 成分 0 只
    assert result["boards_scanned"] == len(result["scan_boards"]) - 2


def test_all_scope_dedup(tmp_path):
    env = _all_fixture(tmp_path, n_boards=40, per_board=1)
    # 让前两个板块返回同一只股票
    env["stocks"] = lambda code, name, limit: (
        ([{"code": "600001", "name": "重复股"}], "clist_amount")
        if name in ("板块0", "板块1") else
        ([{"code": "600002", "name": f"{name}股"}], "clist_amount"))
    result = _plan_all(env)
    assert result["dedup_removed"] >= 1
    codes = [c.code for c in result["scanned"]]
    assert codes.count("600001") == 1


def test_all_scope_respects_max_scan(tmp_path):
    env = _all_fixture(tmp_path, n_boards=80, per_board=3)
    result = _plan_all(env, max_scan=20)
    assert result["stats"]["scanned"] <= 20
