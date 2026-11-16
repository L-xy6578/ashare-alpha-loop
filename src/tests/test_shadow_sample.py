"""分档抽样固定名单：四档各 50、确定性、过滤规则、抽不满说明"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest

import src.shadow_sample as ss


@pytest.fixture(autouse=True)
def _isolate_out_dir(tmp_path, monkeypatch):
    """CSV 只写测试临时目录，不污染 outputs/"""
    monkeypatch.setattr(ss, "OUT_DIR", tmp_path / "out")


def _days(n: int) -> list[str]:
    d0 = date(2026, 1, 5)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(n)]


def _db(tmp_path, *, low=60, mid=10, high=60, top=60):
    """构造 market.db（板块历史）与 research.db（候选池）"""
    days = _days(21)
    base, day = days[0], days[-1]
    market = str(tmp_path / "market.db")
    conn = sqlite3.connect(market)
    conn.executescript(
        """
        CREATE TABLE trade_calendar (date TEXT PRIMARY KEY, is_trading INTEGER, note TEXT);
        CREATE TABLE sector_price_history (date TEXT, code TEXT, name TEXT, kind TEXT,
                                           close REAL, pct REAL, source TEXT,
                                           PRIMARY KEY (date, code));
        """)
    conn.executemany("INSERT INTO trade_calendar VALUES (?,1,'')", [(d,) for d in days])
    for i in range(100):                       # zdf=1..100 → 分位 1%..100%
        name = f"行业{i:02d}"
        conn.execute("INSERT INTO sector_price_history VALUES (?,?,?,?,?,?,?)",
                     (base, f"bk{i:03d}", name, "plate", 100.0, 0.0, "t"))
        conn.execute("INSERT INTO sector_price_history VALUES (?,?,?,?,?,?,?)",
                     (day, f"bk{i:03d}", name, "plate", 101.0 + i, 1.0, "t"))
    conn.commit()
    conn.close()

    research = str(tmp_path / "research.db")
    r = sqlite3.connect(research)
    r.executescript(
        """
        CREATE TABLE research_symbols (symbol TEXT PRIMARY KEY, code TEXT, name TEXT,
            industry TEXT, is_st INTEGER, is_main_board INTEGER, bar_count INTEGER,
            last_date TEXT);
        CREATE TABLE research_bars (symbol TEXT, date TEXT, close REAL,
            PRIMARY KEY (symbol, date));
        """)
    n = 0

    def add(industry, count, *, close=10.0, bar_count=200, is_st=0):
        nonlocal n
        for _ in range(count):
            symbol = f"sh{600000 + n:06d}"
            n += 1
            r.execute("INSERT INTO research_symbols VALUES (?,?,?,?,?,1,?,?)",
                      (symbol, symbol[2:], f"票{n}", industry, is_st, bar_count, day))
            r.execute("INSERT INTO research_bars VALUES (?,?,?)", (symbol, day, close))

    add("行业00", low)
    add("行业55", mid)
    add("行业75", high)
    add("行业95", top)
    add("行业00", 1, is_st=1)                    # ST 必须剔除
    add("行业00", 1, bar_count=50)               # 次新必须剔除
    add("行业00", 1, close=3.0)                  # 低于 5 元剔除
    add("行业00", 1, close=70.0)                 # 高于 60 元剔除
    r.commit()
    r.close()
    return market, research, day


def test_four_buckets_50_each_and_filters(tmp_path):
    market, research, day = _db(tmp_path)
    res = ss.build(market_db=market, research_db=research, day=day, per_bucket=50)
    assert res["written"] == 160                 # 50+10+50+50（低/中/高/顶）
    assert res["counts"] == {"<50": 50, "50~65": 10, "65~85": 50, "≥85": 50}
    assert "50~65" in res["missing"] and "仅 10 只" in res["missing"]["50~65"]
    with sqlite3.connect(market) as conn:
        rows = conn.execute("SELECT symbol FROM shadow_sample").fetchall()
    assert len(rows) == 160


def test_filters_exclude_st_new_and_price_outliers(tmp_path):
    market, research, day = _db(tmp_path, low=60)
    ss.build(market_db=market, research_db=research, day=day)
    with sqlite3.connect(market) as conn:
        prices = [r[0] for r in conn.execute("SELECT base_close FROM shadow_sample")]
    assert min(prices) >= 5 and max(prices) <= 60
    with sqlite3.connect(market) as conn:
        bad = conn.execute(
            "SELECT COUNT(*) FROM shadow_sample WHERE industry='行业00'").fetchone()[0]
    assert bad == 50                             # ST/次新/越界价都被剔除


def test_deterministic_and_fixed(tmp_path):
    market, research, day = _db(tmp_path)
    ss.build(market_db=market, research_db=research, day=day)
    with sqlite3.connect(market) as conn:
        first = sorted(r[0] for r in conn.execute("SELECT symbol FROM shadow_sample"))
    again = ss.build(market_db=market, research_db=research, day=day)
    assert again["skipped"] is True and again["written"] == len(first)
    ss.build(market_db=market, research_db=research, day=day, rebuild=True)
    with sqlite3.connect(market) as conn:
        second = sorted(r[0] for r in conn.execute("SELECT symbol FROM shadow_sample"))
    assert first == second                       # 同种子重抽结果一致


def test_dry_run_writes_nothing(tmp_path):
    market, research, day = _db(tmp_path)
    res = ss.build(market_db=market, research_db=research, day=day, dry_run=True)
    assert res["skipped"] is True
    with sqlite3.connect(market) as conn:
        n = conn.execute("SELECT COUNT(*) FROM shadow_sample").fetchone()[0]
    assert n == 0


def test_missing_research_db_is_empty_not_crash(tmp_path):
    market, _, day = _db(tmp_path)
    res = ss.build(market_db=market, research_db=str(tmp_path / "nope.db"), day=day)
    assert res["written"] == 0
