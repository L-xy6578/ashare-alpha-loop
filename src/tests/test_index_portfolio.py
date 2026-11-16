"""指数化组合（sim 记账）测试：写死配置 / 建仓 / 季度再平衡 / 真实收盘价净值 / 兜底标注"""

import sqlite3

import pytest

from src import index_portfolio as ip


def _bars(symbol: str, dates: list[str], px: dict[str, float]) -> list[dict]:
    return [{"symbol": symbol, "date": d, "open": px[d], "close": px[d],
             "high": px[d], "low": px[d], "volume": 1e6, "amount": 1e7,
             "adjust": "qfq", "source": "test"} for d in dates]


DATES = ["2026-06-30", "2026-07-01", "2026-07-02"]
PX = {
    "sh510300": {"2026-06-30": 4.00, "2026-07-01": 4.10, "2026-07-02": 4.20},
    "sh510500": {"2026-06-30": 6.00, "2026-07-01": 5.90, "2026-07-02": 6.10},
    "sh510880": {"2026-06-30": 3.00, "2026-07-01": 3.05, "2026-07-02": 3.10},
}


def _fetcher(symbol, beg, end):
    return _bars(symbol, DATES, PX[symbol])


@pytest.fixture()
def market_db(tmp_path):
    from src.pipeline.schema import init_market_db

    path = str(tmp_path / "market.db")
    with sqlite3.connect(path) as conn:
        init_market_db(conn)
        for i, d in enumerate(DATES):
            conn.execute(
                """INSERT OR REPLACE INTO daily_bars
                   (symbol, date, open, close, high, low, volume, amount)
                   VALUES ('sh000300',?,?,?,?,?,?,?)""",
                (d, 4000 + i * 10, 4000 + i * 10, 4010 + i * 10, 3990 + i * 10,
                 1e8, 1e11),
            )
    return path


def test_targets_are_fixed():
    """配置写死三只 ETF（各 6,000），不留 TBD"""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        db = ip.init_db(str(Path(d) / "idx.db"))
        rows = ip.targets(db)
    assert [r["code"] for r in rows] == ["510300", "510500", "510880"]
    assert [r["role"] for r in rows] == ["core", "mid", "defense"]
    assert all(r["target_amount"] == 6000.0 for r in rows)
    assert sum(r["target_amount"] for r in rows) == ip.ETF_BUDGET == 18000.0


def test_build_rebalance_and_nav(tmp_path, market_db):
    db = ip.init_db(str(tmp_path / "idx.db"))
    added = ip.collect_bars(DATES[-1], db, fetcher=_fetcher)
    assert added == {"sh510300": 3, "sh510500": 3, "sh510880": 3}

    with sqlite3.connect(db) as conn:
        assert ip._ensure_built(conn, DATES[0]) is True
        acc = conn.execute("SELECT * FROM account WHERE id=1").fetchone()
        assert acc[1] == DATES[0] and acc[2] == 30000.0
        assert acc[3] >= ip.CASH_FLOOR            # 单只 ≤6000 → 现金 ≥12000
        assert ip._ensure_built(conn, DATES[0]) is False   # 幂等

        # 7-01 是季度首个交易日（以 ETF 日K 为日历）→ 拉回等权
        assert ip._apply_rebalance(conn, DATES[1]) is True
        assert ip._apply_rebalance(conn, DATES[1]) is False   # 同日重跑不重复
        stat = ip.rebuild_nav(conn, market_db)
    assert stat["days"] == 3 and stat["proxy_days"] == 0

    st = ip.summary(market_db, db)
    assert st["inception_date"] == DATES[0]
    assert len(st["holdings"]) == 3
    assert st["official_days"] == 3
    assert st["max_drawdown_pct"] <= 0
    assert st["proxy_dates"] == []
    # 每季度第一个交易日拉回等权：份额按 6,000 重新取整
    for h in st["holdings"]:
        assert h["shares"] % ip.LOT == 0
        assert abs(h["cost_amount"] - 6000.0) < 1e-6


def test_missing_etf_day_uses_marked_proxy(tmp_path, market_db):
    """某天 ETF 行情缺失 → 指数代打兜底（proxy=1，不计入正式净值序列）"""
    db = ip.init_db(str(tmp_path / "idx.db"))
    ip.collect_bars(DATES[-1], db, fetcher=_fetcher)
    with sqlite3.connect(db) as conn:
        ip._ensure_built(conn, DATES[0])
    with sqlite3.connect(market_db) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO daily_bars
               (symbol, date, open, close, high, low, volume, amount)
               VALUES ('sh000300','2026-07-03',4030,4030,4040,4020,1e8,1e11)"""
        )
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO etf_bars (symbol, date, open, close, high, low,
                                     volume, amount, adjust, source)
               VALUES ('sh510300','2026-07-03',4.2,4.2,4.2,4.2,1,1,'qfq','test'),
                      ('sh510500','2026-07-03',6.1,6.1,6.1,6.1,1,1,'qfq','test')"""
        )
        stat = ip.rebuild_nav(conn, market_db)
        proxies = conn.execute(
            "SELECT date FROM nav WHERE proxy=1 ORDER BY date").fetchall()
    assert stat["proxy_days"] == 1
    assert [r[0] for r in proxies] == ["2026-07-03"]
    st = ip.summary(market_db, db)
    assert st["proxy_dates"] == ["2026-07-03"]
    assert st["official_days"] == 3          # 兜底日不混进正式序列


def test_summary_none_when_not_built(tmp_path):
    assert ip.summary(str(tmp_path / "market.db"), str(tmp_path / "none.db")) is None
