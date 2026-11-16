"""R47 闭环脚本端到端冒烟测试（dry-run：临时库 + 静态数据源，完全离线）"""

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

import r47_closed_loop as r47
from src.data_source import KlineBar, StaticDataSource
from src.config import DB_PATH, MARKET_DB_PATH


def _bars(closes, start, symbol):
    d = date.fromisoformat(start)
    out = []
    for c in closes:
        iso = d.isoformat()
        out.append(KlineBar(iso, c, c, c * 1.01, c * 0.99))
        d += timedelta(days=1)
    return out


def _count(db, sql, *args):
    try:
        with sqlite3.connect(db) as conn:
            return conn.execute(sql, args).fetchone()[0]
    except sqlite3.OperationalError:
        # 新克隆的仓库还没有生产库/表，按 0 处理
        return 0


def _no_earnings(code: str):
    """离线业绩桩：东财业绩接口不打网络，标的按"业绩缺失"处理（fail-closed）"""
    return None


@pytest.fixture()
def offline_source():
    """离线静态数据源（#108：动态对齐近 20 日窗口，避免跨日后固定日期失效）"""
    from src.dates import recent_window

    _, end = recent_window(days_back=20)
    start = (date.fromisoformat(end) - timedelta(days=69)).isoformat()
    src = StaticDataSource()
    # 5 只观察池标的：70 根（满足上市 ≥60 交易日，避免被可执行性过滤误判为次新）
    src.set_symbol("sh600535", _bars([15.90] * 70, start, "sh600535"))
    src.set_symbol("sh600019", _bars([6.11] * 70, start, "sh600019"))
    src.set_symbol("sz002366", _bars([6.31] * 70, start, "sz002366"))
    src.set_symbol("sz002202", _bars([19.30] * 70, start, "sz002202"))
    src.set_symbol("sz300880", _bars([21.00] * 70, start, "sz300880"))
    # 创业板指：70 个自然日（止于 END，≥61 根满足市场状态判定）
    src.set_symbol("sz399006", _bars([3300 + i * 0.5 for i in range(70)],
                                     (date.fromisoformat(end) - timedelta(days=69)).isoformat(),
                                     "sz399006"))
    return src


def test_dry_run_end_to_end(tmp_path, offline_source):
    """dry-run 全流程跑通：不写生产库、空库隔离、临时库完整落库"""
    kb_before = _count(DB_PATH, "SELECT COUNT(*) FROM correct_book")
    bars_before = _count(MARKET_DB_PATH, "SELECT COUNT(*) FROM daily_bars")

    db_dir = str(tmp_path / "dbs")
    Path(db_dir).mkdir()
    # 预置空库：跳过生产库复制，保证测试完全隔离、确定性（静态数据源全量驱动）
    (Path(db_dir) / "knowledge.db").touch()
    (Path(db_dir) / "market.db").touch()
    # 演示持仓 + 止盈线：验证 9:25 预案线在收盘落实
    from src.portfolio import PortfolioStore
    from src.watchlist import WatchlistStore

    kb_db = f"{db_dir}/knowledge.db"
    mkt_db = f"{db_dir}/market.db"
    PortfolioStore(kb_db).set(
        "sz002366", "融发核电", 250, 5.75, 6.31, "演示底仓",
        account="sim", entry_date="2026-07-28",
    )
    WatchlistStore(mkt_db).add(
        "sz002366", "002366", "融发核电", cost_price=5.75,
        take_profit=6.30, stop_loss=5.30, note="演示止盈线",
    )
    r47.main(
        dry_run=True,
        db_dir=db_dir,
        source=offline_source,
        valuation_provider=lambda sym: {"pe": 20.0, "pb": 2.0},
        earnings_provider=_no_earnings,
    )

    # 生产库零写入
    assert _count(DB_PATH, "SELECT COUNT(*) FROM correct_book") == kb_before
    assert _count(MARKET_DB_PATH, "SELECT COUNT(*) FROM daily_bars") == bars_before

    # 临时库：观察池种子 + R47 记录 + 指数落库
    # 方案 A（#26）：只记录真实动作。演示持仓触发止盈清仓 → 对题本 1 笔
    assert _count(kb_db, "SELECT COUNT(*) FROM correct_book WHERE round_number='R47'") == 1
    assert _count(kb_db, "SELECT COUNT(*) FROM mistake_book WHERE round_number='R47'") == 0
    assert _count(mkt_db, "SELECT COUNT(*) FROM watchlist") == 5
    assert _count(mkt_db, "SELECT COUNT(*) FROM daily_bars WHERE symbol='sz399006'") >= 61
    # 洞1 接线：无 ≥85 推荐时不登记 KPI-07（表存在且为 0 行）
    assert _count(kb_db, "SELECT COUNT(*) FROM kpi_tracking") == 0


def test_dry_run_keeps_source_column_in_tmp_only(tmp_path, offline_source):
    """dry-run 不污染生产库：生产库记录数与真实口径均不变"""
    kb_before = _count(DB_PATH, "SELECT COUNT(*) FROM correct_book")
    real_before = _count(DB_PATH, "SELECT COUNT(*) FROM correct_book WHERE source='real'")
    r47.main(
        dry_run=True,
        db_dir=str(tmp_path / "dbs2"),
        source=offline_source,
        valuation_provider=lambda sym: {"pe": 20.0, "pb": 2.0},
        earnings_provider=_no_earnings,
    )
    with sqlite3.connect(f"{tmp_path}/dbs2/knowledge.db") as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(correct_book)").fetchall()]
        assert "source" in cols
    # 生产库数据零改动（行数不变、无新增 real 记录）
    assert _count(DB_PATH, "SELECT COUNT(*) FROM correct_book") == kb_before
    assert _count(DB_PATH, "SELECT COUNT(*) FROM correct_book WHERE source='real'") == real_before


def test_stop_loss_fires_same_day(tmp_path):
    """#39：止损检查必须用当日收盘价——宝钢当日跌 -9.24%（5.95→5.40）应当天触发止损"""
    from src.dates import recent_window

    _, end = recent_window(days_back=20)
    # 70 根自然日 K 线，最后一根必须落在 END（动态，否则止损用昨收不触发）
    start = (date.fromisoformat(end) - timedelta(days=69)).isoformat()
    db_dir = str(tmp_path / "dbs_stop")
    Path(db_dir).mkdir()
    (Path(db_dir) / "knowledge.db").touch()
    (Path(db_dir) / "market.db").touch()
    # 演示持仓：宝钢 1600 股 @5.95
    from src.portfolio import PortfolioStore

    kb_db = f"{db_dir}/knowledge.db"
    PortfolioStore(kb_db).set(
        "sh600019", "宝钢股份", 1600, 5.95, 6.11, "演示底仓",
        account="sim", entry_date="2026-07-29",
    )

    src = StaticDataSource()
    # 宝钢：横盘后 3 日急跌至 5.40（-9.24% vs 成本 5.95）
    # 最新一根落在 END（与 END 一致，止损用当日收盘）
    src.set_symbol("sh600019", _bars([6.11] * 67 + [5.80, 5.60, 5.40],
                                     start, "sh600019"))
    src.set_symbol("sh600535", _bars([15.90] * 70, start, "sh600535"))
    src.set_symbol("sz002366", _bars([6.31] * 70, start, "sz002366"))
    src.set_symbol("sz002202", _bars([19.50] * 70, start, "sz002202"))
    src.set_symbol("sz300880", _bars([21.00] * 70, start, "sz300880"))
    src.set_symbol("sz399006", _bars([3300 + i * 0.5 for i in range(70)],
                                     start, "sz399006"))

    r47.main(
        dry_run=True, db_dir=db_dir, source=src,
        valuation_provider=lambda sym: {"pe": 20.0, "pb": 2.0},
        earnings_provider=_no_earnings,
    )
    # 宝钢当天被止损清仓
    with sqlite3.connect(kb_db) as conn:
        pos = conn.execute(
            "SELECT shares FROM positions WHERE symbol='sh600019' AND account='sim'"
        ).fetchone()
        assert pos[0] == 0, "宝钢应在当日触发止损清仓"
        # R47：止损 → 错题本（本例无额外止盈动作）
        assert _count(kb_db, "SELECT COUNT(*) FROM mistake_book WHERE round_number='R47'") == 1
        assert _count(kb_db, "SELECT COUNT(*) FROM correct_book WHERE round_number='R47'") == 0
        row = conn.execute(
            "SELECT entry_price, exit_price, loss_pct FROM mistake_book WHERE round_number='R47'"
        ).fetchone()
        assert row[0] == 5.95 and row[1] == 5.40


def test_r47_keeps_previous_day_records(tmp_path, offline_source):
    """#49：跨日再跑不能清掉历史 R47 交易（幂等只限当日记录）"""
    import sqlite3
    from datetime import date, timedelta

    from src.knowledge.models import init_db

    db_dir = str(tmp_path / "dbs_keep")
    Path(db_dir).mkdir()
    kb_db = f"{db_dir}/knowledge.db"
    (Path(f"{db_dir}/market.db")).touch()
    init_db(kb_db)

    yesterday = (date.today() - timedelta(days=1)).isoformat()
    with sqlite3.connect(kb_db) as conn:
        conn.execute(
            """INSERT INTO mistake_book
               (stock_code, stock_name, entry_price, exit_price, loss_pct,
                buy_reason, loss_root_cause, lesson_learned, mistake_type,
                round_number, holding_days, created_at, source)
               VALUES ('600019','宝钢股份',5.95,5.76,-3.19,
                       'R47 前一日交易','测试','测试','止损不及时',
                       'R47',0,?,'sim')""",
            (f"{yesterday} 15:00",),
        )

    r47.main(
        dry_run=True, db_dir=db_dir, source=offline_source,
        valuation_provider=lambda sym: {"pe": 20.0, "pb": 2.0},
        earnings_provider=_no_earnings,
    )
    with sqlite3.connect(kb_db) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM mistake_book "
            "WHERE round_number='R47' AND stock_code='600019'"
        ).fetchone()[0]
    assert n == 1, "前一日 R47 交易记录不应被当日运行清除"


def test_dry_run_never_writes_candidates_report(tmp_path, offline_source, monkeypatch):
    """候选落盘只允许正式运行：dry-run 既不写选股结果，也不写调研库"""
    def _boom(*args, **kwargs):
        pytest.fail("dry-run 不应写选股结果/调研库")

    monkeypatch.setattr(r47, "_write_candidates_report", _boom)
    monkeypatch.setattr(r47, "_refresh_finance_evidence", _boom)

    db_dir = str(tmp_path / "dbs_report")
    Path(db_dir).mkdir()
    (Path(db_dir) / "knowledge.db").touch()
    (Path(db_dir) / "market.db").touch()
    r47.main(
        dry_run=True, db_dir=db_dir, source=offline_source,
        valuation_provider=lambda sym: {"pe": 20.0, "pb": 2.0},
        earnings_provider=_no_earnings,
    )


def test_candidates_report_overwrites_same_day(tmp_path):
    """正式运行落盘：同日重跑覆盖而非追加，缺失因子与强制不推荐原因可读"""
    from src.factors import DataScoreResult
    from src.scoring import FactorScores

    res = DataScoreResult(
        symbol="sh600001", name="测试股",
        factors=FactorScores(industry=90, growth=80, valuation=0, technical=60, capital=50),
        weighted=61.0, recommended=False, incomplete=True,
        missing=("估值",), evidence="https://example.com/e1",
    )
    rows = [
        {"code": "600001", "name": "测试股", "sector": "电机", "result": res,
         "status": "强制不推荐：缺估值"},
        {"code": "600002", "name": "休眠股", "sector": "电机", "result": None,
         "status": "强制不推荐：无业绩证据（调研库基本面缺失）"},
    ]
    path1 = r47._write_candidates_report("2026-10-29", rows, tmp_path)
    text = path1.read_text(encoding="utf-8")
    path2 = r47._write_candidates_report("2026-10-29", rows, tmp_path)
    assert path1 == path2
    assert path2.read_text(encoding="utf-8") == text           # 覆盖，不追加
    assert path1.name == "选股结果_2026-10-29.md"
    assert "| 测试股 | 600001 | 电机 | 61.0 |" in text
    assert "| — | 60 |" in text            # 缺失因子列显示 —，不编造估值分
    assert "无业绩证据" in text


class _FakeSector:
    def __init__(self, dates):
        # 持有同一列表引用：补拉成功后 fake_inject 的 insert 才能反映到快照日期
        self._dates = dates

    def available_dates(self):
        return list(self._dates)


def test_ensure_snapshot_refetch_success(monkeypatch):
    """B3：当日快照缺失 → 自动补拉成功，返回新日期"""
    dates = ["2026-11-03"]
    calls = []

    def fake_inject(trade_date=None, dry_run=False):
        calls.append((trade_date, dry_run))
        dates.insert(0, trade_date)
        return 0

    monkeypatch.setattr("src.sector_feed.inject", fake_inject)
    out = r47._ensure_current_sector_snapshot(_FakeSector(dates), "2026-11-06", is_dry=False)
    assert calls == [("2026-11-06", False)]
    assert out[0] == "2026-11-06"


def test_ensure_snapshot_refetch_failure_degrades(monkeypatch):
    """B3：补拉失败不抛异常、不改退出路径，沿用旧快照"""
    def fake_inject(trade_date=None, dry_run=False):
        return 1

    monkeypatch.setattr("src.sector_feed.inject", fake_inject)
    out = r47._ensure_current_sector_snapshot(_FakeSector(["2026-11-03"]), "2026-11-06", is_dry=False)
    assert out == ["2026-11-03"]


def test_ensure_snapshot_refetch_exception_degrades(monkeypatch):
    """B3：补拉异常同样只降级，不允许中断收盘"""
    def boom(trade_date=None, dry_run=False):
        raise RuntimeError("getaddrinfo failed")

    monkeypatch.setattr("src.sector_feed.inject", boom)
    out = r47._ensure_current_sector_snapshot(_FakeSector(["2026-11-03"]), "2026-11-06", is_dry=False)
    assert out == ["2026-11-03"]


def test_ensure_snapshot_dry_run_never_writes(monkeypatch):
    """B3：dry-run 不触发补拉（不写库）"""
    def boom(*args, **kwargs):
        pytest.fail("dry-run 不应触发板块补拉")

    monkeypatch.setattr("src.sector_feed.inject", boom)
    out = r47._ensure_current_sector_snapshot(_FakeSector(["2026-11-03"]), "2026-11-06", is_dry=True)
    assert out == ["2026-11-03"]


def test_ensure_snapshot_noop_when_current(monkeypatch):
    """B3：已有当日快照时不补拉"""
    def boom(*args, **kwargs):
        pytest.fail("已有当日快照不应补拉")

    monkeypatch.setattr("src.sector_feed.inject", boom)
    out = r47._ensure_current_sector_snapshot(_FakeSector(["2026-11-06", "2026-11-05"]),
                                              "2026-11-06", is_dry=False)
    assert out[0] == "2026-11-06"


def test_snapshot_quality_note_has_date_and_source():
    """B5：质量提示必须带快照日期与 source，不能把旧快照条数当成当日全量"""
    info = {"date": "2026-11-03", "source": "eastmoney", "complete": False,
            "plate_count": 496, "concept_count": 531,
            "note": "东财全量：plate=496 / concept=531"}
    note = r47._snapshot_quality_note(info)
    assert "2026-11-03" in note and "source=eastmoney" in note and "496" in note
    assert r47._snapshot_quality_note({}) == ""


def test_candidates_markdown_shows_snapshot_date():
    """B5：表头质量提示落盘后带日期"""
    note = r47._snapshot_quality_note({"date": "2026-11-06", "source": "eastmoney",
                                       "plate_count": 496, "concept_count": 531,
                                       "note": "东财全量：plate=496 / concept=531"})
    text = r47._candidates_markdown("2026-11-06", [], notes=[note])
    assert "板块快照 2026-11-06 · source=eastmoney" in text
