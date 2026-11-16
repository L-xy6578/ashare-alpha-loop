"""数据库每日备份测试（VACUUM INTO + 7 份轮转 + 源库不可用不影响主流程）"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))) 

from src.backup_db import backup_one, run_backup


def _make_db(path):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")


def test_backup_creates_dated_file(tmp_path):
    src = tmp_path / "market.db"
    out = tmp_path / "backups"
    _make_db(src)

    dst = backup_one(src, out_dir=out, day="2026-11-06")

    assert dst is not None
    assert dst.exists()
    assert dst.name == "market_2026-11-06.db"
    with sqlite3.connect(dst) as conn:
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1


def test_rotation_keeps_latest_seven(tmp_path):
    src = tmp_path / "market.db"
    out = tmp_path / "backups"
    _make_db(src)
    for day in range(1, 9):                     # 8 份
        backup_one(src, out_dir=out, day=f"2026-11-{day:02d}", keep=7)

    names = sorted(p.name for p in out.glob("market_*.db"))
    assert len(names) == 7
    assert "market_2026-11-01.db" not in names  # 最旧的一份被轮转删除
    assert "market_2026-11-08.db" in names


def test_same_day_backup_is_overwritten(tmp_path):
    """同日重复执行覆盖当日份（幂等），不会堆积多份"""
    src = tmp_path / "market.db"
    out = tmp_path / "backups"
    _make_db(src)
    backup_one(src, out_dir=out, day="2026-11-06")
    with sqlite3.connect(src) as conn:
        conn.execute("INSERT INTO t VALUES (2)")
    backup_one(src, out_dir=out, day="2026-11-06")

    files = list(out.glob("market_*.db"))
    assert len(files) == 1
    with sqlite3.connect(files[0]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2


def test_missing_source_returns_none_without_raising(tmp_path):
    """源库不可用：返回 None（调用方只告警），不影响主流程"""
    out = tmp_path / "backups"
    assert backup_one(tmp_path / "missing.db", out_dir=out) is None
    assert run_backup((str(tmp_path / "missing.db"),), out_dir=out) == 1


def test_run_backup_continues_after_one_failure(tmp_path):
    good = tmp_path / "knowledge.db"
    _make_db(good)
    out = tmp_path / "backups"

    rc = run_backup((str(tmp_path / "missing.db"), str(good)), out_dir=out, keep=7)

    assert rc == 1                             # 有一个失败 → 非 0（只告警）
    assert (out / "knowledge_2026-11-06.db").exists() or \
        len(list(out.glob("knowledge_*.db"))) == 1   # 另一个库照常备份


def test_backup_file_is_readable_sqlite(tmp_path):
    """备份文件必须是可独立打开的 SQLite（VACUUM INTO 产物）"""
    src = tmp_path / "market.db"
    out = tmp_path / "backups"
    _make_db(src)
    dst = backup_one(src, out_dir=out, day="2026-11-06")
    with sqlite3.connect(dst) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_rotation_ignores_manual_dir_and_odd_names(tmp_path):
    """#119 P3：只轮转 {库名}_YYYY-MM-DD.db；manual/ 与自定义命名一律保留"""
    src = tmp_path / "market.db"
    out = tmp_path / "backups"
    _make_db(src)
    out.mkdir()
    manual = out / "manual"
    manual.mkdir()
    manual_snap = manual / "market_2026-11-06_before_d20fix.db"
    manual_snap.write_bytes(b"manual snapshot")     # 内容不重要，只看是否被删
    odd = out / "market_2026-11-01_snapshot.db"
    odd.write_bytes(b"odd name")

    for day in range(1, 10):
        (out / f"market_2026-10-{day:02d}.db").write_bytes(b"auto")

    backup_one(src, out_dir=out, day="2026-11-07", keep=7)

    auto = sorted(p.name for p in out.glob("market_*.db") if p.is_file())
    assert "market_2026-10-01.db" not in auto    # 最旧自动备份被轮转
    assert "market_2026-10-04.db" in auto        # 保留最近 7 份（10-04 ~ 11-07）
    assert manual_snap.exists(), "manual/ 下手工快照不得被轮转删除"
    assert odd.exists(), "非日期命名文件不得参与轮转"


def test_no_sidecar_after_backup(tmp_path):
    """#119 P3：备份目录只剩 .db（无 -wal/-shm）"""
    src = tmp_path / "market.db"
    out = tmp_path / "backups"
    _make_db(src)
    # 让源库处于 WAL 模式（模拟生产库）
    with sqlite3.connect(src) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
    dst = backup_one(src, out_dir=out, day="2026-11-06")
    assert dst is not None
    assert [p.name for p in out.iterdir()] == ["market_2026-11-06.db"]
    with sqlite3.connect(dst) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1


def test_degraded_copy_path_cleans_sidecars(tmp_path, monkeypatch):
    """降级路径（checkpoint+复制）也要把副本规范成单文件"""
    src = tmp_path / "market.db"
    out = tmp_path / "backups"
    _make_db(src)
    with sqlite3.connect(src) as conn:
        conn.execute("PRAGMA journal_mode=WAL")

    import src.backup_db as backup_db
    real_connect = sqlite3.connect

    class _NoVacuum:
        def __init__(self, conn):
            self._c = conn

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._c.__exit__(*exc)

        def execute(self, sql, params=()):
            if str(sql).strip().upper().startswith("VACUUM INTO"):
                raise sqlite3.Error("simulated vacuum failure")
            return self._c.execute(sql, params)

        def close(self):
            self._c.close()

    monkeypatch.setattr(backup_db.sqlite3, "connect",
                        lambda *a, **kw: _NoVacuum(real_connect(*a, **kw)))

    dst = backup_one(src, out_dir=out, day="2026-11-06")

    assert dst is not None and dst.exists()
    assert sorted(p.name for p in out.iterdir()) == ["market_2026-11-06.db"]
    with real_connect(dst) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
