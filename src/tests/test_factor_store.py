"""每日因子快照（D 方案）：同日覆盖幂等 + 缺失记 NULL + 缺失清单可复算"""

import sqlite3

from src.factor_store import FactorSnapshotStore, recompute_score


def _row(symbol, score, industry=80.0, capital=60.0, valuation=75.0, missing=""):
    return {"symbol": symbol, "industry": industry, "growth": 70.0,
            "valuation": valuation, "technical": 90.0, "capital": capital,
            "score": score, "missing": missing, "source": "eastmoney",
            "resolved_board": "银行",
            "benchmark": "industry=板块内分位(n=496); capital=板块内分位(n=450)"}


def test_save_and_load(tmp_path):
    store = FactorSnapshotStore(str(tmp_path / "m.db"))
    assert store.save_many("2026-10-30", [_row("sz002807", 65.2)]) == 1
    rows = store.load("2026-10-30")
    assert len(rows) == 1
    assert rows[0]["symbol"] == "sz002807"
    assert rows[0]["score"] == 65.2
    assert rows[0]["source"] == "eastmoney"


def test_same_day_upsert_is_idempotent(tmp_path):
    db = str(tmp_path / "m.db")
    store = FactorSnapshotStore(db)
    store.save_many("2026-10-30", [_row("sz002807", 65.2)])
    store.save_many("2026-10-30", [_row("sz002807", 71.0), _row("sz000858", 72.5)])
    rows = {r["symbol"]: r for r in store.load("2026-10-30")}
    assert len(rows) == 2
    assert rows["sz002807"]["score"] == 71.0     # 同日覆盖，不追加


def test_missing_factor_saved_as_null(tmp_path):
    store = FactorSnapshotStore(str(tmp_path / "m.db"))
    store.save_many("2026-10-30", [_row("sh600535", 42.5, industry=None, capital=None)])
    r = store.load("2026-10-30")[0]
    assert r["industry"] is None and r["capital"] is None
    assert r["score"] == 42.5


def test_empty_rows_noop(tmp_path):
    store = FactorSnapshotStore(str(tmp_path / "m.db"))
    assert store.save_many("2026-10-30", []) == 0
    assert store.load("2026-10-30") == []


# ---------- 缺失显式记录 + 可复现性（2026-11-06 估值全 NULL 事故） ----------

def test_missing_factor_recorded_explicitly(tmp_path):
    """估值缺失：因子列 NULL 之外，missing 必须写明"估值"（区别于低估）"""
    store = FactorSnapshotStore(str(tmp_path / "m.db"))
    store.save_many("2026-11-06", [_row("sh600535", 63.5, valuation=None,
                                       missing=["估值"])])
    r = store.load("2026-11-06")[0]
    assert r["valuation"] is None
    assert r["missing"] == "估值"


def test_recompute_score_reproduces_folded_score(tmp_path):
    """留档单独复算：缺失因子按 0 计，必须还原当日落库分"""
    store = FactorSnapshotStore(str(tmp_path / "m.db"))
    row = _row("sz002841", 66.4, industry=79.6, capital=84.8, valuation=None,
               missing="估值")
    row.update({"growth": 90.0, "technical": 75.0})
    # 0.25*79.6 + 0.25*90 + 0.20*0 (估值缺失) + 0.15*75 + 0.15*84.8 = 66.4
    assert recompute_score(row) == 66.4
    store.save_many("2026-11-06", [row])
    assert store.verify("2026-11-06") == []


def test_verify_flags_score_that_cannot_be_reproduced(tmp_path):
    """落库分与因子对不上（字段丢失/口径不一致）必须被自检抓出"""
    store = FactorSnapshotStore(str(tmp_path / "m.db"))
    row = _row("sz002841", 83.4, industry=79.6, capital=84.8, valuation=None,
               missing="估值")
    row.update({"growth": 90.0, "technical": 75.0})
    store.save_many("2026-11-06", [row])
    bad = store.verify("2026-11-06")
    assert bad and bad[0][0] == "sz002841"
    assert bad[0][1] == 83.4 and bad[0][2] == 66.4


def test_growth_recomputed_from_raw_rate(tmp_path):
    """业绩分必须能用落档原始值（growth_raw）复算，不能只信落库分"""
    store = FactorSnapshotStore(str(tmp_path / "m.db"))
    row = _row("sz002841", 0.0, industry=80.0, capital=65.0, valuation=75.0)
    row.update({"growth": 70.0, "technical": 90.0,
                "growth_raw": 15.0, "growth_profitable": 1,
                "growth_source": "eastmoney"})
    # 0.25*80 + 0.25*75(15%→桶 75) + 0.20*75 + 0.15*90 + 0.15*65 = 77.0
    row["score"] = recompute_score(row)
    assert row["score"] == 77.0
    store.save_many("2026-11-11", [row])
    loaded = store.load("2026-11-11")[0]
    assert loaded["growth_raw"] == 15.0 and loaded["growth_profitable"] == 1
    assert store.verify("2026-11-11") == []

    # 落库分若还是旧口径（业绩 70）→ 复算对不上，自检必须抓出
    store.save_many("2026-11-11", [dict(row, score=75.8)])
    bad = store.verify("2026-11-11")
    assert bad and bad[0][2] == 77.0


def test_growth_profitable_flag_roundtrip(tmp_path):
    """亏损标记 NULL（未给金额）与 0（亏损）必须可区分"""
    store = FactorSnapshotStore(str(tmp_path / "m.db"))
    row = _row("sh600535", 60.0)
    row.update({"growth_raw": -20.0, "growth_profitable": 0})
    store.save_many("2026-11-11", [row])
    loaded = store.load("2026-11-11")[0]
    assert loaded["growth_profitable"] == 0
    assert recompute_score(loaded) == recompute_score(row)   # 亏损封顶 25 可复算


def test_old_schema_gets_missing_column(tmp_path):
    """旧库（无 missing 列）打开即迁移，历史行 missing 为空但复算仍可用 NULL 判缺失"""
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE factor_snapshot (
        date TEXT NOT NULL, symbol TEXT NOT NULL, industry REAL, growth REAL,
        valuation REAL, technical REAL, capital REAL, score REAL,
        source TEXT NOT NULL DEFAULT '', resolved_board TEXT NOT NULL DEFAULT '',
        benchmark TEXT NOT NULL DEFAULT '', PRIMARY KEY (date, symbol))""")
    conn.execute("INSERT INTO factor_snapshot VALUES "
                 "('2026-11-06','sz002841',79.6,90,NULL,75,84.8,66.4,'eastmoney','','')")
    conn.commit()
    conn.close()

    store = FactorSnapshotStore(db)
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(factor_snapshot)")}
    assert "missing" in cols
    assert store.verify("2026-11-06") == []     # NULL 估值仍按 0 复算出 66.4
