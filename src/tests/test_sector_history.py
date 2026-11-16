"""板块日收盘价历史：20 日涨幅真值化（2026-11-10 冻结期 bug 修复 #158/#159）"""

import sqlite3
from datetime import date, datetime, timedelta

import pytest

from src import sector_feed
from src import sector_history as sh
from src.sector_analysis import SectorAnalyzer, SectorSnapshot


def _calendar(db, days):
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS trade_calendar "
                     "(date TEXT PRIMARY KEY, is_trading INTEGER NOT NULL DEFAULT 1, "
                     "note TEXT)")
        for d in days:
            conn.execute("INSERT OR REPLACE INTO trade_calendar VALUES (?,1,'')", (d,))


def _days(n=25, start=date(2026, 1, 5)):
    return [(start + timedelta(days=i)).isoformat() for i in range(n)]


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "m.db")
    _calendar(path, _days())
    monkeypatch.setattr(sh, "MARKET_DB_PATH", path)
    monkeypatch.setattr(sector_feed, "MARKET_DB_PATH", path)
    return path


def test_effective_close_date_premarket_uses_previous_day(tmp_path, monkeypatch):
    """08:50 采集到的点位是上一交易日收盘 → 历史表归属上一交易日（防整条序列错位一天）"""
    today = date.today().isoformat()
    prev = (date.today() - timedelta(days=1)).isoformat()
    path = str(tmp_path / "cal.db")
    _calendar(path, [prev, today])
    monkeypatch.setattr(sh, "MARKET_DB_PATH", path)

    assert sh.effective_close_date(today, datetime.now().replace(hour=8, minute=50)) == prev
    # 15:35 收盘闭环采集 → 归属当日
    assert sh.effective_close_date(today, datetime.now().replace(hour=15, minute=35)) == today
    # 手工注入历史日期：信任调用方
    assert sh.effective_close_date("2026-01-06") == "2026-01-06"


def test_zdf_from_history_not_inherited(db):
    """历史完整 → 按交易日历回看 20 个交易日算真值"""
    days = _days()
    for i, d in enumerate(days):
        sh.upsert_closes(d, [{"code": "bk_BK1", "name": "钢铁", "kind": "plate",
                              "close": 100 + i}])
    z, base = sh.zdf_by_code(days[20], 20)
    assert base == days[0]
    assert z["bk_BK1"] == pytest.approx(20.0)     # 120/100-1


def test_zdf_missing_base_is_absent_not_zero(db):
    """基准日无该板块数据 → 不在结果里（调用方写 NULL），绝不继承或补 0"""
    days = _days()
    sh.upsert_closes(days[20], [{"code": "bk_BK1", "close": 120.0}])
    z, base = sh.zdf_by_code(days[20], 20)
    assert base == days[0]
    assert "bk_BK1" not in z


def test_zdf_insufficient_calendar_returns_empty(db):
    """交易日历不足 21 天 → 整体不可算"""
    z, base = sh.zdf_by_code(_days()[3], 20)
    assert z == {} and base is None


def test_upsert_closes_idempotent(db):
    """同日同板块重复写入 → 覆盖不重复（主键 date+code）"""
    sh.upsert_closes("2026-01-05", [{"code": "bk_BK1", "close": 100.0}])
    sh.upsert_closes("2026-01-05", [{"code": "bk_BK1", "close": 101.0}])
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT date, close FROM sector_price_history").fetchall()
    assert rows == [("2026-01-05", 101.0)]


def test_build_snapshots_uses_history_and_writes_null(db, monkeypatch):
    """采集侧：zdf20 只来自历史表；算不出的板块写 None 并计入缺失（不继承旧值）"""
    days = _days()
    for i, d in enumerate(days):
        sh.upsert_closes(d, [{"code": "bk_BK1", "name": "P0", "kind": "plate",
                              "close": 100 + i}])

    def fake_list(kind):
        if kind == "plate":
            return ([{"code": "BK1", "name": "P0", "zdf": 1.0, "zdf5": 1.0,
                      "level": 120.0, "zljlr": 100.0, "zljlr_d5": 50.0, "hsl": 1.0},
                     {"code": "BK2", "name": "P1", "zdf": 2.0, "zdf5": 2.0,
                      "level": 88.0, "zljlr": 100.0, "zljlr_d5": 50.0, "hsl": 1.0}],
                    [], 2)
        return ([], [], 0)

    monkeypatch.setattr(sector_feed, "fetch_board_list", fake_list)
    monkeypatch.setattr(sector_feed, "capital_window", lambda day, n=20: [])
    monkeypatch.setattr(sector_feed, "fetch_board_main_flows",
                        lambda codes: ({}, list(codes)))
    monkeypatch.setattr(sh, "effective_close_date", lambda *a, **k: days[20])
    monkeypatch.setattr(sh, "is_close_price_time", lambda *a, **k: False)

    built = sector_feed.build_snapshots("2026-01-25")   # 非今日 → 不写历史
    snaps = {s.name: s for s in built["snaps"]}
    assert snaps["P0"].zdf20 == pytest.approx(20.0)
    assert snaps["P1"].zdf20 is None
    assert built["zdf20_missing"] == 1
    assert built["hist_written"] == 0


def test_build_snapshots_writes_history_for_today(db, monkeypatch):
    """当日采集：把已采到的板块收盘点位落进历史表（零额外请求）"""
    today = date.today().isoformat()
    monkeypatch.setattr(sh, "effective_close_date", lambda *a, **k: "2026-01-06")
    monkeypatch.setattr(sh, "is_close_price_time", lambda *a, **k: True)

    def fake_list(kind):
        if kind == "plate":
            return ([{"code": "BK1", "name": "P0", "zdf": 1.0, "zdf5": 1.0,
                      "level": 123.4, "zljlr": 100.0, "zljlr_d5": 50.0, "hsl": 1.0}],
                    [], 1)
        return ([], [], 0)

    monkeypatch.setattr(sector_feed, "fetch_board_list", fake_list)
    monkeypatch.setattr(sector_feed, "capital_window", lambda day, n=20: [])
    monkeypatch.setattr(sector_feed, "fetch_board_main_flows",
                        lambda codes: ({}, list(codes)))
    built = sector_feed.build_snapshots(today)
    assert built["hist_written"] == 1
    with sqlite3.connect(db) as conn:
        got = conn.execute("SELECT date, code, close FROM sector_price_history").fetchall()
    assert got == [("2026-01-06", "bk_BK1", 123.4)]


def test_snapshot_null_roundtrip(tmp_path):
    """zdf20 缺失必须落 NULL，读回来仍是 None（不能变成 0 冒充低分/中性）"""
    analyzer = SectorAnalyzer(db_path=str(tmp_path / "s.db"))
    analyzer.save_snapshot([SectorSnapshot(code="bk_A", name="A", date="2026-01-05",
                                           zdf20=None, zdf60=None)])
    loaded = analyzer.load_date("2026-01-05")
    assert loaded[0].zdf20 is None and loaded[0].zdf60 is None
