"""每日补采：独立限速接口可注入、失败计数落日志、幂等、dry-run 跳过"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import src.shadow_collect as sc


def _db(tmp_path, symbols=("sh600000", "sh600001", "sh600002")) -> str:
    path = str(tmp_path / "market.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE shadow_sample (symbol TEXT PRIMARY KEY)")
    conn.executemany("INSERT INTO shadow_sample VALUES (?)", [(s,) for s in symbols])
    conn.commit()
    conn.close()
    return path


def _bars(n=3):
    return [SimpleNamespace(date=f"2026-11-0{i + 1}", open=10.0, close=10.0 + i,
                            high=11.0, low=9.0, volume=100.0) for i in range(n)]


def test_collect_ok_failed_and_log(tmp_path):
    path = _db(tmp_path)

    def fetcher(symbol, start, end):
        if symbol == "sh600002":
            raise RuntimeError("接口超时")
        return _bars()

    log = tmp_path / "shadow_collect.log"
    st = sc.collect("2026-11-03", market_db=path, fetcher=fetcher,
                    workers=1, gap=0, log_path=log)
    assert st["requests"] == 3 and st["ok"] == 2 and st["failed"] == 1
    assert st["rows"] == 6
    with sqlite3.connect(path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM shadow_daily_bars").fetchone()[0]
    assert n == 6
    text = log.read_text(encoding="utf-8")
    assert "sh600002" in text and "error" in text


def test_collect_idempotent(tmp_path):
    path = _db(tmp_path)
    kw = dict(fetcher=lambda s, a, b: _bars(), workers=1, gap=0,
              log_path=tmp_path / "l.log")
    sc.collect("2026-11-03", market_db=path, **kw)
    sc.collect("2026-11-03", market_db=path, **kw)
    with sqlite3.connect(path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM shadow_daily_bars").fetchone()[0]
    assert n == 9                                   # 3 只 × 3 天，重跑不重复


def test_collect_dry_run_skips(tmp_path):
    path = _db(tmp_path)
    st = sc.collect("2026-11-03", market_db=path, dry_run=True,
                    fetcher=lambda s, a, b: _bars(), gap=0)
    assert st["skipped"] is True
    with sqlite3.connect(path) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='shadow_daily_bars'").fetchone()[0]
    assert n == 0


def test_collect_empty_sample_skips(tmp_path):
    path = _db(tmp_path, symbols=())
    st = sc.collect("2026-11-03", market_db=path, fetcher=lambda s, a, b: _bars())
    assert st["skipped"] is True and "名单为空" in st["reason"]


def test_collect_empty_response_counts_empty(tmp_path):
    path = _db(tmp_path, symbols=("sh600000",))
    st = sc.collect("2026-11-03", market_db=path, fetcher=lambda s, a, b: [],
                    workers=1, gap=0, log_path=tmp_path / "l.log")
    assert st["empty"] == 1 and st["rows"] == 0 and st["failed"] == 0
