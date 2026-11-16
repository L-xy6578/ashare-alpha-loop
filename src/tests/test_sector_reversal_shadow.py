"""板块反转影子台账：空数据 / 幂等 / 只回填可算 / dry-run 跳过 / 报表"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import src.sector_reversal_shadow as srs


def _days(n: int, start: str = "2026-01-05") -> list[str]:
    """n 个连续自然日（当作交易日，测试不依赖真实日历）"""
    d0 = date.fromisoformat(start)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(n)]


def _fresh_db(tmp_path, *, plates: int = 40, bars: int = 6) -> tuple[str, str, str]:
    """构造最小 market.db：返回 (路径, 信号日, 基准日)"""
    path = str(tmp_path / "market.db")
    days = _days(21 + bars)
    day, base = days[20], days[0]
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE trade_calendar (date TEXT PRIMARY KEY, is_trading INTEGER, note TEXT);
        CREATE TABLE watchlist (symbol TEXT PRIMARY KEY, code TEXT, name TEXT,
                                sector TEXT, status TEXT);
        CREATE TABLE daily_bars (symbol TEXT, date TEXT, open REAL, close REAL,
                                 high REAL, low REAL, volume REAL, amount REAL);
        CREATE TABLE sector_price_history (date TEXT, code TEXT, name TEXT, kind TEXT,
                                           close REAL, pct REAL, source TEXT,
                                           PRIMARY KEY (date, code));
        """)
    conn.executemany("INSERT INTO trade_calendar VALUES (?,1,'')", [(d,) for d in days])
    # 40 个行业板块：基准日 100，信号日涨幅互不相同（保证 zdf20 非 0 且可分位）
    for i in range(plates):
        name = f"行业{i:02d}"
        conn.execute("INSERT INTO sector_price_history VALUES (?,?,?,?,?,?,?)",
                     (base, f"bk_{i:03d}", name, "plate", 100.0, 0.0, "test"))
        conn.execute("INSERT INTO sector_price_history VALUES (?,?,?,?,?,?,?)",
                     (day, f"bk_{i:03d}", name, "plate", 101.0 + i, 1.0, "test"))
    conn.execute("INSERT INTO watchlist VALUES ('sh600000','600000','浦发银行','行业00','active')")
    conn.execute("INSERT INTO watchlist VALUES ('sz000001','000001','平安银行','行业39','removed')")
    # 个股日K：从信号日起 bars 根（bars=6 → 只能算 5 日）
    for sym, start in (("sh600000", 10.0), ("sz000001", 20.0)):
        for i, d in enumerate(days[20:20 + bars]):
            conn.execute("INSERT INTO daily_bars VALUES (?,?,?,?,?,?,?,?)",
                         (sym, d, start, start + i, start, start, 1.0, 1.0))
    for i, d in enumerate(days[20:20 + bars]):
        conn.execute("INSERT INTO daily_bars VALUES (?,?,?,?,?,?,?,?)",
                     ("sh000300", d, 100.0, 100.0 + i, 100.0, 100.0, 1.0, 1.0))
    conn.commit()
    conn.close()
    return path, day, base


def test_empty_db_does_not_raise(tmp_path):
    path = str(tmp_path / "empty.db")
    sqlite3.connect(path).close()
    assert srs.record(None, market_db=path)["skipped"] is True
    assert srs.backfill(market_db=path)["checked"] == 0
    assert srs.write_report(market_db=path, out_dir=tmp_path / "out").exists()


def test_record_rows_and_buckets(tmp_path):
    path, day, _ = _fresh_db(tmp_path)
    res = srs.record(day, market_db=path)
    assert res["inserted"] == 1 and res["skipped"] is False
    with sqlite3.connect(path) as conn:
        rows = [dict(zip(("date", "symbol", "code", "in_pool", "board", "pct", "bucket", "close"),
                         r)) for r in conn.execute(
            "SELECT date, symbol, code, in_pool, board, sector_pct, bucket, close "
            "FROM sector_reversal_shadow ORDER BY symbol")]
    assert {r["symbol"] for r in rows} == {"sh600000"}
    for r in rows:
        assert r["date"] == day
        assert r["pct"] is not None and r["bucket"] in srs.BUCKETS
        assert r["board"] is not None
        assert r["close"] is not None
        assert r["in_pool"] == 1


def test_record_is_idempotent(tmp_path):
    path, day, _ = _fresh_db(tmp_path)
    srs.record(day, market_db=path)
    again = srs.record(day, market_db=path)
    assert again["inserted"] == 0
    with sqlite3.connect(path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM sector_reversal_shadow").fetchone()[0]
    assert n == 1


def test_backfill_only_computable(tmp_path):
    path, day, _ = _fresh_db(tmp_path, bars=6)   # 只有 6 根 → 仅 5 日可算
    srs.record(day, market_db=path)
    stats = srs.backfill(market_db=path)
    assert stats["updated"] == 1
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT ret5, ret10, ret20, excess5, excess10, excess20 "
            "FROM sector_reversal_shadow").fetchall()
    for ret5, ret10, ret20, excess5, excess10, excess20 in rows:
        assert ret5 is not None and excess5 is not None
        assert ret10 is None and ret20 is None
        assert excess10 is None and excess20 is None


def test_dry_run_skips(tmp_path):
    path, day, _ = _fresh_db(tmp_path)
    assert srs.record(day, market_db=path, dry_run=True)["skipped"] is True
    assert srs.backfill(market_db=path, dry_run=True)["skipped"] is True
    with sqlite3.connect(path) as conn:
        exists = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name='sector_reversal_shadow'").fetchone()[0]
    assert exists == 0


def test_report_contains_bucket_table(tmp_path):
    path, day, _ = _fresh_db(tmp_path)
    srs.record(day, market_db=path)
    srs.backfill(market_db=path)
    md = srs.write_report(market_db=path, out_dir=tmp_path / "out", day=day)
    text = md.read_text(encoding="utf-8")
    assert md.name == f"板块反转影子_{day}.md"
    for b in srs.BUCKETS:
        assert b in text
    assert "超额均值%" in text
    assert "样本够不够" in text and "覆盖率" in text


def test_shadow_bars_take_priority_over_daily_bars(tmp_path):
    path, day, _ = _fresh_db(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE shadow_daily_bars (
            symbol TEXT, date TEXT, open REAL, close REAL, high REAL, low REAL,
            volume REAL, source TEXT, fetched_at TEXT, PRIMARY KEY (symbol, date))""")
        conn.execute("INSERT INTO shadow_daily_bars VALUES (?,?,?,?,?,?,?,?,?)",
                     ("sh600000", day, 99.0, 99.0, 99.0, 99.0, 1.0, "t", "now"))
    srs.record(day, market_db=path)
    with sqlite3.connect(path) as conn:
        close = conn.execute(
            "SELECT close FROM sector_reversal_shadow WHERE symbol='sh600000'").fetchone()[0]
    assert close == 99.0                     # 新采集优先，不是 daily_bars 的 10.0


def test_sample_list_included_as_out_of_pool(tmp_path):
    path, day, _ = _fresh_db(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE shadow_sample (
            symbol TEXT PRIMARY KEY, code TEXT, name TEXT, industry TEXT,
            bucket TEXT, sector_pct REAL, board_zdf20 REAL, base_close REAL,
            bar_count INTEGER, seed INTEGER, rule TEXT, sampled_for TEXT, created_at TEXT)""")
        conn.execute("INSERT INTO shadow_sample VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("sh600123", "600123", "样本票", "行业05", "<50", 5.0, 1.0, 10.0,
                      200, 20261111, "r", day, "now"))
        conn.execute("INSERT INTO daily_bars VALUES (?,?,?,?,?,?,?,?)",
                     ("sh600123", day, 10.0, 12.0, 12.0, 10.0, 1.0, 1.0))
    res = srs.record(day, market_db=path)
    assert res["inserted"] == 2
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT in_pool, board FROM sector_reversal_shadow WHERE symbol='sh600123'"
        ).fetchone()
    assert row[0] == 0 and row[1] == "行业05"

