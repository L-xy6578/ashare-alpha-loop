"""每日硬检查测试（三条硬指标 + DEGRADED 判定 + 日报落点）"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import src.daily_audit as audit

DAY = "2026-11-06"


def _market_db(tmp_path, *, last_bar=DAY, complete=1, sector_rows=3,
               trade=1, watchlist=("sh600019",)):
    db = tmp_path / "market.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE daily_bars (symbol TEXT, date TEXT, close REAL,"
                     " volume REAL)")
        conn.execute("CREATE TABLE watchlist (symbol TEXT, name TEXT, note TEXT,"
                     " status TEXT)")
        conn.execute("CREATE TABLE trade_calendar (date TEXT, is_trading INTEGER)")
        conn.execute("CREATE TABLE sector_daily (date TEXT, name TEXT)")
        conn.execute(
            "CREATE TABLE sector_snapshot_meta (date TEXT, source TEXT, plate_count INTEGER,"
            " concept_count INTEGER, complete INTEGER, note TEXT)")
        if last_bar:
            conn.execute("INSERT INTO daily_bars VALUES (?,?,?,?)",
                         (audit.BENCHMARK_SYMBOL, last_bar, 4000.0, 1000))
        conn.executemany("INSERT INTO watchlist VALUES (?,?,?,?)",
                         [(s, s, "", "active") for s in watchlist])
        conn.execute("INSERT INTO trade_calendar VALUES (?,?)", (DAY, trade))
        conn.execute("INSERT INTO trade_calendar VALUES (?,?)", ("2026-11-05", 1))
        if complete is not None:
            conn.execute("INSERT INTO sector_snapshot_meta VALUES (?,?,?,?,?,?)",
                         (DAY, "eastmoney", 496, 531, complete, ""))
        for i in range(sector_rows):
            conn.execute("INSERT INTO sector_daily VALUES (?,?)", (DAY, f"板块{i}"))
    return str(db)


def _watch_db(tmp_path, active: list[tuple[str, str, str]],
              prev_bars: tuple[str, ...] = (), today_bars: dict[str, float] | None = None,
              removed: tuple[str, ...] = ()):
    """构造带 active/removed 观察池与前一交易日/当日 K 线的行情库

    active: [(symbol, name, note)]；today_bars: {symbol: volume}（volume=0 表示停牌零成交）
    """
    db = tmp_path / "market.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE daily_bars (symbol TEXT, date TEXT, close REAL,"
                     " volume REAL)")
        conn.execute("CREATE TABLE watchlist (symbol TEXT, name TEXT, note TEXT,"
                     " status TEXT)")
        conn.execute("CREATE TABLE trade_calendar (date TEXT, is_trading INTEGER)")
        conn.execute("CREATE TABLE sector_daily (date TEXT, name TEXT)")
        conn.execute(
            "CREATE TABLE sector_snapshot_meta (date TEXT, source TEXT, plate_count INTEGER,"
            " concept_count INTEGER, complete INTEGER, note TEXT)")
        conn.execute("INSERT INTO daily_bars VALUES (?,?,?,?)",
                     (audit.BENCHMARK_SYMBOL, DAY, 4000.0, 1000))   # 指数层正常
        conn.execute("INSERT INTO trade_calendar VALUES (?,?)", (DAY, 1))
        conn.execute("INSERT INTO trade_calendar VALUES (?,?)", ("2026-11-05", 1))
        conn.execute("INSERT INTO sector_snapshot_meta VALUES (?,?,?,?,?,?)",
                     (DAY, "eastmoney", 496, 531, 1, ""))
        conn.execute("INSERT INTO sector_daily VALUES (?,?)", (DAY, "板块0"))
        conn.executemany("INSERT INTO watchlist VALUES (?,?,?,?)",
                         [(s, n, note, "active") for s, n, note in active]
                         + [(s, s, "", "removed") for s in removed])
        for sym in prev_bars:
            conn.execute("INSERT INTO daily_bars VALUES (?,?,?,?)",
                         (sym, "2026-11-05", 10.0, 1000))
        for sym, vol in (today_bars or {}).items():
            conn.execute("INSERT INTO daily_bars VALUES (?,?,?,?)",
                         (sym, DAY, 10.0, vol))
    return str(db)


def _knowledge_db(tmp_path, rows: list[dict]):
    from src.decision_log import DecisionStore

    db = str(tmp_path / "knowledge.db")
    store = DecisionStore(db)
    if rows:
        store.save_rows(rows)
    return db


def _abstain(reason: str = "可执行性过滤后 0 只可买候选"):
    from src.decision_log import make_no_candidate_row

    return make_no_candidate_row(
        decision_date=DAY, data_date=DAY, reason=reason,
        snapshot_date=DAY, snapshot_complete=True,
        git_head="abc1234", git_dirty=False,
    )


def _run(tmp_path, market_db=None, knowledge_db=None):
    return audit.run_audit(
        DAY, market_db=market_db or _market_db(tmp_path),
        knowledge_db=knowledge_db or _knowledge_db(tmp_path, [_abstain()]),
        out_dir=tmp_path, write=True,
    )


def test_all_three_pass(tmp_path):
    res = _run(tmp_path)
    assert res.ok is True
    assert [c.key for c in res.checks] == ["market", "sector", "decision"]
    report = Path(res.report_path).read_text(encoding="utf-8")
    assert "PASS" in report and "沪深300" in report
    assert (tmp_path / f"硬检查_{DAY}.md").exists()


def test_market_stale_degrades(tmp_path):
    res = _run(tmp_path, market_db=_market_db(tmp_path, last_bar="2026-10-31"))
    assert res.ok is False
    assert res.checks[0].ok is False
    assert "2026-10-31" in res.checks[0].detail


def test_benchmark_missing_degrades(tmp_path):
    res = _run(tmp_path, market_db=_market_db(tmp_path, last_bar=None))
    assert res.ok is False
    assert res.checks[0].ok is False
    assert "无数据" in res.checks[0].detail


def test_sector_incomplete_degrades(tmp_path):
    res = _run(tmp_path, market_db=_market_db(tmp_path, complete=0))
    assert res.ok is False
    assert res.checks[1].ok is False
    report = Path(res.report_path).read_text(encoding="utf-8")
    assert "DEGRADED" in report


def test_sector_meta_missing_degrades(tmp_path):
    res = _run(tmp_path, market_db=_market_db(tmp_path, complete=None))
    assert res.ok is False
    assert "无 sector_snapshot_meta" in res.checks[1].detail


def test_decision_missing_degrades(tmp_path):
    res = _run(tmp_path, knowledge_db=_knowledge_db(tmp_path, []))
    assert res.ok is False
    assert res.checks[2].ok is False
    assert "无 decision_log 记录" in res.checks[2].detail


def test_abstain_without_reason_degrades(tmp_path):
    res = _run(tmp_path, knowledge_db=_knowledge_db(tmp_path, [_abstain(reason="")]))
    assert res.ok is False
    assert "弃权未写原因" in res.checks[2].detail


def test_non_trading_day_skipped_not_failed(tmp_path):
    res = audit.run_audit(DAY, market_db=_market_db(tmp_path, trade=0),
                          knowledge_db=_knowledge_db(tmp_path, []),
                          out_dir=tmp_path)
    assert res.ok is True
    assert "非交易日" in res.skipped
    assert "非交易日" in Path(res.report_path).read_text(encoding="utf-8")


def test_cli_exit_code_2_on_failure(tmp_path, capsys):
    rc = audit.main(["--date", DAY, "--market-db", _market_db(tmp_path, last_bar=None),
                     "--knowledge-db", _knowledge_db(tmp_path, []),
                     "--out", str(tmp_path / "out.md")])
    assert rc == 2
    assert "DEGRADED" in capsys.readouterr().out


def test_cli_exit_code_0_on_pass(tmp_path, capsys):
    rc = audit.main(["--date", DAY, "--market-db", _market_db(tmp_path),
                     "--knowledge-db", _knowledge_db(tmp_path, [_abstain()]),
                     "--no-write"])
    assert rc == 0
    assert "PASS" in capsys.readouterr().out


def test_cli_crash_exit_code_3(monkeypatch, capsys):
    """未捕获异常必须落到独立的 3（崩溃），不能与 0/2 混淆导致 close fail-open"""
    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(audit, "run_audit", boom)
    rc = audit.main(["--date", DAY, "--no-write"])
    assert rc == audit.EXIT_CRASH == 3
    assert "崩溃" in capsys.readouterr().err


def test_watchlist_outage_fails_even_when_index_fresh(tmp_path):
    """指数层正常但 active 观察池整体停在昨天 → 必须 FAIL（原 fail-open 漏洞）"""
    db = _watch_db(
        tmp_path,
        active=[("sh600019", "宝钢股份", ""), ("sz002366", "融发核电", "")],
        prev_bars=("sh600019", "sz002366"),      # 昨日有
        today_bars={},                            # 今日全无
    )
    res = _run(tmp_path, market_db=db)
    assert res.checks[0].ok is False
    assert res.ok is False
    detail = res.checks[0].detail
    assert "指数层：沪深300" in detail and "PASS" in detail
    assert "观察池层" in detail and "FAIL" in detail
    assert "断供" in res.checks[0].evidence
    assert "宝钢股份(sh600019)" in res.checks[0].evidence


def test_suspended_stock_does_not_fail(tmp_path):
    """停牌证据：当日 0 成交 bar / 无 bar 但备注含停牌 → 不计入断供，不误报 FAIL"""
    db = _watch_db(
        tmp_path,
        active=[("sh600019", "宝钢股份", ""),      # 正常
                ("sz002366", "融发核电", "停牌核查"),  # 无当日 bar 但有停牌备注
                ("sh600535", "天士力", "")],       # 当日 0 成交 bar（有 bar 无成交）
        prev_bars=("sh600019", "sz002366", "sh600535"),
        today_bars={"sh600019": 1000, "sh600535": 0},
    )
    res = _run(tmp_path, market_db=db)
    assert res.checks[0].ok is True
    assert res.ok is True
    assert "停牌 2 只" in res.checks[0].detail
    assert "停牌：" in res.checks[0].evidence


def test_removed_symbols_not_counted_in_coverage(tmp_path):
    """软删（removed）符号不得计入覆盖分母/断供数"""
    db = _watch_db(
        tmp_path,
        active=[("sh600019", "宝钢股份", "")],
        prev_bars=("sh600019",),
        today_bars={"sh600019": 1000},
        removed=("sh600100", "sz000001", "sh600887"),   # 均无当日 bar
    )
    res = _run(tmp_path, market_db=db)
    assert res.checks[0].ok is True
    assert "覆盖率 1/1" in res.checks[0].detail
    assert "断供 0 只" in res.checks[0].detail


def test_empty_active_watchlist_skipped_explicitly(tmp_path):
    """active 观察池为空：明确写明"跳过、仅指数锚点判定"，不静默 PASS"""
    db = _watch_db(tmp_path, active=[], removed=("sh600100",))
    res = _run(tmp_path, market_db=db)
    assert res.ok is True
    assert "观察池为空" in res.checks[0].detail
    assert "仅指数锚点判定" in res.checks[0].detail
