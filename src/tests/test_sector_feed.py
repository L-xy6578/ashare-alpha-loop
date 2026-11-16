"""板块快照注入：东财专属 + 完整性硬门禁（2026-10-30 A 方案）"""

import sqlite3

from src import sector_feed
from src.sector_analysis import SectorAnalyzer, SectorSnapshot


def _snap(name, d20, kind="plate", source="eastmoney"):
    return SectorSnapshot(code=f"bk_{name}", name=name, kind=kind,
                          date="2026-10-29", zdf20=1.0, zljlr_d20=d20,
                          source=source)


def _built(plates, concepts, failed=(), complete=True,
           plate_total=None, concept_total=None, plate_missing=0, concept_missing=0):
    return {"snaps": list(plates) + list(concepts),
            "plate_count": len(plates), "concept_count": len(concepts),
            "failed_pages": list(failed), "complete": complete,
            "plate_total": len(plates) if plate_total is None else plate_total,
            "concept_total": len(concepts) if concept_total is None else concept_total,
            "plate_missing": plate_missing, "concept_missing": concept_missing,
            "source": "eastmoney"}


def test_fund_data_ok_rejects_all_zero():
    assert sector_feed._fund_data_ok([]) is False
    assert sector_feed._fund_data_ok([_snap("A", 0.0), _snap("B", 0.0)]) is False
    assert sector_feed._fund_data_ok([_snap("A", None), _snap("B", 0.0)]) is False
    assert sector_feed._fund_data_ok([_snap("A", None), _snap("B", -1200.0)]) is True


def test_no_tencent_board_fallback():
    """腾讯不得再作为板块因子输入（防回退到旧兜底路径）"""
    assert not hasattr(sector_feed, "build_snapshots_from_tencent")
    assert not hasattr(sector_feed, "fetch_tencent_boards")


def test_calc_zljlr_d20_sums_full_window():
    """20 日主力 = 交易日历窗口内逐日主力累加（万元）"""
    window = [f"d{i}" for i in range(20)]
    hist = {d: 100.0 for d in window}
    assert sector_feed.calc_zljlr_d20(hist, window) == 2000.0


def test_calc_zljlr_d20_missing_day_returns_none():
    """窗口内缺任一交易日 → None（无数据，落库写 NULL），不得用部分窗口凑数"""
    window = [f"d{i}" for i in range(20)]
    hist = {d: 100.0 for d in window[:-1]}          # 少最后一天
    assert sector_feed.calc_zljlr_d20(hist, window) is None
    assert sector_feed.calc_zljlr_d20(None, window) is None
    assert sector_feed.calc_zljlr_d20({}, []) is None


def test_calc_zljlr_d20_real_zero_is_zero():
    """窗口完整但合计为 0 → 0（真实零净流入，不是缺失）"""
    window = [f"d{i}" for i in range(20)]
    hist = {d: 0.0 for d in window}
    assert sector_feed.calc_zljlr_d20(hist, window) == 0.0


def test_migrate_d20_null_is_idempotent(monkeypatch, tmp_path, capsys):
    """11-06 迁移：仅“窗口不完整”置 NULL；完整保留原值；拉取失败跳过不动；重复执行不变"""
    db = str(tmp_path / "m.db")
    SectorAnalyzer(db)
    window = [f"d{i}" for i in range(20)]
    with sqlite3.connect(db) as conn:
        for code in ("bk_BK0001", "bk_BK0002", "bk_BK0003"):
            conn.execute(
                "INSERT INTO sector_daily (date, code, name, kind, source) "
                "VALUES ('2026-11-06', ?, ?, 'plate', 'eastmoney')",
                (code, code))
        conn.execute("UPDATE sector_daily SET zljlr_d20=0 "
                     "WHERE date='2026-11-06' AND code IN ('bk_BK0001','bk_BK0002')")
        conn.execute("UPDATE sector_daily SET zljlr_d20=-50 "
                     "WHERE date='2026-11-06' AND code='bk_BK0003'")

    full = {d: 100.0 for d in window}                 # 完整窗口（即使重算=2000 也不覆盖原值）
    missing = {d: 100.0 for d in window[:-1]}         # 缺一天 → NULL
    monkeypatch.setattr(sector_feed, "MARKET_DB_PATH", db)
    monkeypatch.setattr(sector_feed, "capital_window", lambda day, n=20: window)
    monkeypatch.setattr(
        sector_feed, "fetch_board_main_flows",
        lambda codes: ({"BK0001": full, "BK0002": missing}, ["BK0003"]))

    assert sector_feed.migrate_d20_null("2026-11-06", dry_run=True) == 0
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT zljlr_d20 FROM sector_daily "
                            "WHERE date='2026-11-06' AND code='bk_BK0002'").fetchone()[0] == 0

    assert sector_feed.migrate_d20_null("2026-11-06") == 0
    with sqlite3.connect(db) as conn:
        got = dict(conn.execute("SELECT code, zljlr_d20 FROM sector_daily "
                                "WHERE date='2026-11-06'").fetchall())
    assert got["bk_BK0001"] == 0.0          # 完整窗口：保留原值
    assert got["bk_BK0002"] is None         # 缺日：置 NULL
    assert got["bk_BK0003"] == -50.0        # 拉取失败：无法判定，跳过不动

    # 重跑一次：结果一致（幂等）
    assert sector_feed.migrate_d20_null("2026-11-06") == 0
    with sqlite3.connect(db) as conn:
        again = dict(conn.execute("SELECT code, zljlr_d20 FROM sector_daily "
                                 "WHERE date='2026-11-06'").fetchall())
    assert again == got
    out = capsys.readouterr().out
    assert "置 NULL=1" in out and "拉取失败跳过=1" in out


def test_migrate_d20_null_aborts_on_source_outage(monkeypatch, tmp_path, capsys):
    """数据源大面积不可用 → 中止且不写库（防止把全部板块误置 NULL）"""
    db = str(tmp_path / "m.db")
    SectorAnalyzer(db)
    window = [f"d{i}" for i in range(20)]
    with sqlite3.connect(db) as conn:
        for i in range(10):
            conn.execute(
                "INSERT INTO sector_daily (date, code, name, kind, source) "
                "VALUES ('2026-11-06', ?, ?, 'plate', 'eastmoney')",
                (f"bk_BK{i:04d}", f"P{i}"))
        conn.execute("UPDATE sector_daily SET zljlr_d20=7 WHERE date='2026-11-06'")

    monkeypatch.setattr(sector_feed, "MARKET_DB_PATH", db)
    monkeypatch.setattr(sector_feed, "capital_window", lambda day, n=20: window)
    monkeypatch.setattr(
        sector_feed, "fetch_board_main_flows",
        lambda codes: ({}, list(codes)))          # 全部失败

    assert sector_feed.migrate_d20_null("2026-11-06") == 1
    with sqlite3.connect(db) as conn:
        vals = {r[0] for r in conn.execute(
            "SELECT zljlr_d20 FROM sector_daily WHERE date='2026-11-06'")}
    assert vals == {7}                              # 一条都没改
    assert "中止且未写库" in capsys.readouterr().out


def test_calc_zljlr_d20_ignores_extra_history_dates():
    """窗口外更早的日期不参与累加"""
    window = [f"d{i}" for i in range(10, 30)]
    hist = {f"d{i}": 100.0 for i in range(40)}
    assert sector_feed.calc_zljlr_d20(hist, window) == 2000.0


def test_inject_skips_when_no_fund_data(monkeypatch, tmp_path):
    """资金全 0 → 不写库，写 meta(complete=0)"""
    db = str(tmp_path / "m.db")
    SectorAnalyzer(db)
    monkeypatch.setattr(sector_feed.time, "sleep", lambda s: None)
    monkeypatch.setattr(sector_feed, "build_snapshots",
                        lambda day, **kw: _built([], [], complete=False))
    monkeypatch.setattr(sector_feed, "MARKET_DB_PATH", db)
    assert sector_feed.inject(trade_date="2026-10-29") == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sector_daily").fetchone()[0] == 0
        meta = conn.execute("SELECT complete, note FROM sector_snapshot_meta "
                            "WHERE date='2026-10-29'").fetchone()
    assert meta[0] == 0
    assert "资金" in meta[1]


def test_inject_rejects_incomplete_concept(monkeypatch, tmp_path):
    """概念板块只拉到 200 条（<阈值）→ 整日不落库"""
    db = str(tmp_path / "m.db")
    SectorAnalyzer(db)
    plates = [_snap(f"P{i}", -1000 + i) for i in range(450)]
    concepts = [_snap(f"C{i}", -100 + i, kind="concept") for i in range(200)]
    monkeypatch.setattr(sector_feed.time, "sleep", lambda s: None)
    monkeypatch.setattr(sector_feed, "build_snapshots",
                        lambda day, **kw: _built(plates, concepts, failed=[3], complete=False))
    monkeypatch.setattr(sector_feed, "MARKET_DB_PATH", db)
    assert sector_feed.inject(trade_date="2026-10-29") == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sector_daily").fetchone()[0] == 0
        meta = conn.execute("SELECT plate_count, concept_count, complete, note "
                            "FROM sector_snapshot_meta WHERE date='2026-10-29'").fetchone()
    assert meta[0] == 450 and meta[1] == 200 and meta[2] == 0
    assert "概念" in meta[3] or "不完整" in meta[3]


def test_inject_saves_complete_snapshot(monkeypatch, tmp_path):
    """东财全量 → 落库并写 source/meta"""
    db = str(tmp_path / "m.db")
    plates = [_snap(f"P{i}", -1000 + i) for i in range(450)]
    concepts = [_snap(f"C{i}", -100 + i, kind="concept") for i in range(450)]
    monkeypatch.setattr(sector_feed.time, "sleep", lambda s: None)
    monkeypatch.setattr(sector_feed, "build_snapshots",
                        lambda day, **kw: _built(plates, concepts))
    monkeypatch.setattr(sector_feed, "MARKET_DB_PATH", db)
    assert sector_feed.inject(trade_date="2026-10-29") == 0
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sector_daily").fetchone()[0] == 900
        srcs = {r[0] for r in conn.execute("SELECT DISTINCT source FROM sector_daily")}
        meta = conn.execute("SELECT complete FROM sector_snapshot_meta "
                            "WHERE date='2026-10-29'").fetchone()
    assert srcs == {"eastmoney"}
    assert meta[0] == 1


def test_inject_dry_run_does_not_write(monkeypatch, tmp_path):
    monkeypatch.setattr(sector_feed.time, "sleep", lambda s: None)
    monkeypatch.setattr(sector_feed, "build_snapshots",
                        lambda day, **kw: _built([_snap("A", -3000.0)], []))
    monkeypatch.setattr(sector_feed, "MARKET_DB_PATH", str(tmp_path / "m.db"))
    assert sector_feed.inject(trade_date="2026-10-29", dry_run=True) == 0
    assert (tmp_path / "m.db").exists() is False


def test_fetch_board_list_stops_at_short_page(monkeypatch):
    """B2：返回条数 < pz 即最后一页，不再追请求越界页（2026-11-06）"""
    calls = []

    def fake_get(url, timeout=10):
        pn = int(url.split("pn=")[1].split("&")[0])
        calls.append(pn)
        size = 96 if pn == 5 else 100
        start = (pn - 1) * 100
        return {"data": {"total": 496, "diff": [
            {"f12": f"bk_{start + i}", "f14": f"B{start + i}",
             "f3": 1.0, "f170": -1000.0} for i in range(size)]}}

    monkeypatch.setattr(sector_feed, "_get_json", fake_get)
    monkeypatch.setattr(sector_feed.time, "sleep", lambda s: None)
    boards, failed, total = sector_feed.fetch_board_list("plate")
    assert len(boards) == 496
    assert failed == [] and total == 496
    assert max(calls) == 5, "末页已返回 96 条，不应再请求越界第 6 页"


def test_fetch_board_list_still_records_real_failure(monkeypatch):
    """B2：真实失败仍要记录缺失页，不能被终止条件吞掉"""
    def fake_get(url, timeout=10):
        pn = int(url.split("pn=")[1].split("&")[0])
        if pn == 1:
            raise RuntimeError("Remote end closed connection")
        return {"data": {"total": 496, "diff": [
            {"f12": f"bk_{i}", "f14": f"B{i}", "f3": 1.0, "f170": -1000.0}
            for i in range(100)]}}

    monkeypatch.setattr(sector_feed, "_get_json", fake_get)
    monkeypatch.setattr(sector_feed.time, "sleep", lambda s: None)
    boards, failed, _ = sector_feed.fetch_board_list("plate")
    assert boards == [] and failed == [1]


def test_inject_accepts_high_coverage_gap(monkeypatch, tmp_path):
    """B1：1025/1027 高覆盖率仍落库，complete=1 且 note 记缺失页/缺口"""
    db = str(tmp_path / "m.db")
    plates = [_snap(f"P{i}", -1000 + i) for i in range(494)]
    concepts = [_snap(f"C{i}", -100 + i, kind="concept") for i in range(531)]
    monkeypatch.setattr(sector_feed.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        sector_feed, "build_snapshots",
        lambda day, **kw: _built(plates, concepts, failed=[6], complete=True,
                           plate_total=496, concept_total=531, plate_missing=2))
    monkeypatch.setattr(sector_feed, "MARKET_DB_PATH", db)
    assert sector_feed.inject(trade_date="2026-10-29") == 0
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sector_daily").fetchone()[0] == 1025
        meta = conn.execute("SELECT complete, note FROM sector_snapshot_meta "
                            "WHERE date='2026-10-29'").fetchone()
    assert meta[0] == 1
    assert "高覆盖" in meta[1] and "缺失页=[6]" in meta[1] and "缺 2 个" in meta[1]


def test_inject_rejects_large_gap(monkeypatch, tmp_path):
    """B1：整页丢失（缺口 > 5）仍整日拒绝落库并记不完整"""
    db = str(tmp_path / "m.db")
    plates = [_snap(f"P{i}", -1000 + i) for i in range(490)]
    concepts = [_snap(f"C{i}", -100 + i, kind="concept") for i in range(531)]
    monkeypatch.setattr(sector_feed.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        sector_feed, "build_snapshots",
        lambda day, **kw: _built(plates, concepts, failed=[6], complete=False,
                           plate_total=590, concept_total=531, plate_missing=100))
    monkeypatch.setattr(sector_feed, "MARKET_DB_PATH", db)
    assert sector_feed.inject(trade_date="2026-10-29") == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sector_daily").fetchone()[0] == 0
        meta = conn.execute("SELECT complete, note FROM sector_snapshot_meta "
                            "WHERE date='2026-10-29'").fetchone()
    assert meta[0] == 0 and "不完整" in meta[1] and "缺口过大" in meta[1]
