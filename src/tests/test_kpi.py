"""KPI-07 自动检票测试"""

import pytest

from src.db import get_connection
from src.kpi import KpiTracker, deadline_date
from src.pipeline.calendar import init_trade_calendar
from src.pipeline.schema import init_market_db


@pytest.fixture()
def env(tmp_path):
    kb = str(tmp_path / "kb.db")
    mkt = str(tmp_path / "market.db")
    with get_connection(mkt) as conn:
        init_market_db(conn)
    init_trade_calendar(mkt)
    return kb, mkt


def test_deadline_is_22nd_trading_day(env):
    _, mkt = env
    assert deadline_date("2026-07-24", n=22, market_db=mkt) == "2026-08-24"
    assert deadline_date("2026-07-24", n=23, market_db=mkt) == "2026-08-25"


def test_seed_manual_idempotent(env):
    kb, mkt = env
    k = KpiTracker(kb, mkt)
    assert k.seed_manual() == 1
    assert k.seed_manual() == 0
    items = k.all()
    assert len(items) == 1
    assert all(i["deadline"] == "2026-08-24" for i in items)


def test_evaluate_hit_and_miss(env):
    kb, mkt = env
    k = KpiTracker(kb, mkt)
    # 真实盘样本两条，验证 hit/miss 判定（不依赖具体个股）
    with get_connection(kb) as conn:
        for code, name, close, target in (("600019", "宝钢股份", 5.91, 15.0),
                                          ("600535", "天士力", 15.78, 20.0)):
            conn.execute(
                """INSERT INTO kpi_tracking
                   (round_number, stock_code, stock_name, rec_date, rec_close,
                    target_pct, target_price, deadline, status, source, note, created_at)
                   VALUES ('R48-R52',?,?,?,?,?,?,'2026-08-24','tracking','real','test','2026-08-05')""",
                (code, name, "2026-07-24", close, target,
                 round(close * (1 + target / 100), 2)),
            )
    with get_connection(mkt) as conn:
        for code, close in (("600535", 19.00), ("600019", 6.30)):
            symbol = ("sh" if code.startswith("6") else "sz") + code
            conn.execute(
                """INSERT INTO daily_bars
                   (symbol, date, open, close, high, low, volume, amount, rows_source)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (symbol, "2026-08-24", close, close, close * 1.01,
                 close * 0.99, 0, 0, "test"),
            )
    results = k.evaluate_due(today="2026-08-24", close_providers=[])
    status = {r["stock_code"]: r["status"] for r in results}
    assert status["600535"] == "hit"     # 15.78→19.00 = +20.41% ≥ 20%
    assert status["600019"] == "miss"    # 5.91→6.30 = +6.6% < 15%
    acc = k.accuracy()
    assert acc["evaluated"] == 2 and acc["hits"] == 1
    assert acc["accuracy"] == 50.0
    # #106：成绩单 eval_close 列统一两位小数
    md = k.render_report(today="2026-08-24")
    assert "| 6.30 |" in md
    assert "| 19.00 |" in md


def test_accuracy_only_counts_real(env):
    """KPI-07 口径：模拟盘推演不计入真实盘准确率"""
    kb, mkt = env
    k = KpiTracker(kb, mkt)
    with get_connection(kb) as conn:
        conn.execute(
            """INSERT INTO kpi_tracking
               (round_number, stock_code, stock_name, rec_date, rec_close,
                target_pct, target_price, deadline, status, source, note, created_at)
               VALUES ('R47','600019','宝钢股份','2026-08-05',5.86,20.0,7.03,
                       '2026-09-03','hit','sim','test','2026-08-05')"""
        )
        conn.execute(
            """INSERT INTO kpi_tracking
               (round_number, stock_code, stock_name, rec_date, rec_close,
                target_pct, target_price, deadline, status, source, note, created_at)
               VALUES ('R47','600535','天士力','2026-08-05',15.72,20.0,18.86,
                       '2026-09-03','miss','real','test','2026-08-05')"""
        )
    acc = k.accuracy()
    assert acc["evaluated"] == 1 and acc["hits"] == 0        # 只认真实盘
    assert acc["accuracy"] == 0.0
    assert acc["sim_total"] == 1 and acc["sim_evaluated"] == 1
    md = k.render_report(today="2026-09-03")
    assert "真实盘" in md and "不计入真实口径" in md
    assert "1/1 = 100.0%" not in md                          # 模拟盘命中不得冒充真实命中


def test_migrate_source_column_idempotent(tmp_path):
    """迁移脚本：旧表加 source 列并把历史样本标注 sim，可重复执行"""
    from src.kpi import migrate_source_column

    db = str(tmp_path / "old_kpi.db")
    with get_connection(db) as conn:
        conn.execute(
            """CREATE TABLE kpi_tracking (
               id INTEGER PRIMARY KEY AUTOINCREMENT, round_number TEXT NOT NULL,
               stock_code TEXT NOT NULL, stock_name TEXT NOT NULL, rec_date TEXT NOT NULL,
               rec_close REAL NOT NULL, target_pct REAL NOT NULL, target_price REAL NOT NULL,
               deadline TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'tracking',
               eval_date TEXT, eval_close REAL, actual_pct REAL,
               note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)"""
        )
        conn.execute(
            """INSERT INTO kpi_tracking
               (round_number, stock_code, stock_name, rec_date, rec_close,
                target_pct, target_price, deadline, status, actual_pct, note, created_at)
               VALUES ('R48-R52','600019','宝钢股份','2026-07-24',5.91,15.0,6.80,
                       '2026-08-24','miss',-0.85,'手工跟踪表迁移','2026-07-24')"""
        )
    assert migrate_source_column(db) == 1          # 首次：历史 1 条标注 sim
    assert migrate_source_column(db) == 0          # 再跑：幂等
    with get_connection(db, readonly=True) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(kpi_tracking)")]
        assert "source" in cols
        assert conn.execute("SELECT source FROM kpi_tracking").fetchone()[0] == "sim"


def test_pending_data_not_counted(env):
    kb, mkt = env
    k = KpiTracker(kb, mkt)
    k.seed_manual()   # 模拟盘样本（不计入真实口径）
    with get_connection(kb) as conn:
        conn.execute(
            """INSERT INTO kpi_tracking
               (round_number, stock_code, stock_name, rec_date, rec_close,
                target_pct, target_price, deadline, status, source, note, created_at)
               VALUES ('R47','600535','天士力','2026-07-24',15.78,20.0,18.94,
                       '2026-08-24','tracking','real','test','2026-08-05')"""
        )
    # 截止日无行情数据 → pending_data，不计入分母
    results = k.evaluate_due(today="2026-08-24", close_providers=[])
    real = [r for r in results if r.get("source") == "real"]
    assert all(r["status"] == "pending_data" for r in real)
    acc = k.accuracy()
    assert acc["evaluated"] == 0
    assert acc["pending_data"] == 1          # 真实盘数据缺失 1 笔
    assert acc["accuracy"] is None
    assert acc["sim_total"] == 1


def test_render_report(env):
    kb, mkt = env
    k = KpiTracker(kb, mkt)
    k.seed_manual()
    md = k.render_report(today="2026-08-24")
    assert "KPI-07" in md
    assert "中兵红箭" not in md and "宝钢股份" in md
    assert "待评估" in md


def test_close_on_bare_code_finds_prefixed_bar(env):
    """P0 修复：裸代码自动补 sh/sz 前缀查库（此前查不到 → 08-24 首次评估会失败）"""
    kb, mkt = env
    k = KpiTracker(kb, mkt)
    with get_connection(mkt) as conn:
        conn.execute(
            """INSERT INTO daily_bars
               (symbol, date, open, close, high, low, volume, amount, rows_source)
               VALUES ('sh600535','2026-08-24',19.0,19.0,19.2,18.9,0,0,'test')"""
        )
    assert k._close_on("600535", "2026-08-24", providers=[]) == 19.0
    # 无数据不抛异常，返回 None（走 pending_data 而非崩溃）
    assert k._close_on("600019", "2026-08-24", providers=[]) is None


def test_close_on_only_compares_same_caliber(env):
    """#84：不复权源（raw）不参与前复权库比对，避免口径混比误判不一致"""
    from src.data_source import KlineBar

    kb, mkt = env
    k = KpiTracker(kb, mkt)
    with get_connection(mkt) as conn:
        conn.execute(
            """INSERT INTO daily_bars
               (symbol, date, open, close, high, low, volume, amount, rows_source, adjust)
               VALUES ('sh600535','2026-08-24',19.0,19.0,19.2,18.9,0,0,'test','qfq')"""
        )

    class FakeQfq:
        adjust = "qfq"

        def daily_kline(self, symbol, start=None, end=None):
            return [KlineBar("2026-08-24", 19.0, 19.05, 19.2, 18.9)]

    class FakeRaw:
        adjust = "raw"

        def daily_kline(self, symbol, start=None, end=None):
            # 与库内价差 52%：若被参与比对必判不一致 → 验证它被跳过
            return [KlineBar("2026-08-24", 9.0, 9.0, 9.2, 8.9)]

    assert k._close_on("600535", "2026-08-24",
                       providers=[FakeQfq, FakeRaw]) == 19.0


def test_resolve_close_multi_source():
    """多源核对：一致采用，不一致视为数据不可信（双源防单源抽风）"""
    assert KpiTracker._resolve_close([("tencent", 5.86), ("tdx", 5.86)]) == 5.86
    assert KpiTracker._resolve_close([("market", 5.86), ("tdx", 5.861)]) == 5.86
    assert KpiTracker._resolve_close([("tencent", 5.86), ("tdx", 6.10)]) is None
    assert KpiTracker._resolve_close([]) is None


def test_pending_data_retried_next_run(env):
    """P2 修复：pending_data 到期后自动重试，数据补齐后恢复评估"""
    kb, mkt = env
    k = KpiTracker(kb, mkt)
    k.seed_manual()
    # 第一次：无收盘价 → pending_data
    k.evaluate_due(today="2026-08-24", close_providers=[])
    assert all(r["status"] == "pending_data" for r in k.all())
    # 补收盘价后重试 → 恢复评估
    with get_connection(kb) as conn:
        conn.execute(
            """INSERT INTO kpi_tracking
               (round_number, stock_code, stock_name, rec_date, rec_close,
                target_pct, target_price, deadline, status, note, created_at)
               VALUES ('R48-R52','600535','天士力','2026-07-24',15.78,20.0,18.94,
                       '2026-08-24','tracking','test','2026-08-05')"""
        )
    with get_connection(mkt) as conn:
        for code, close in (("600535", 19.00), ("600019", 6.30)):
            symbol = ("sh" if code.startswith("6") else "sz") + code
            conn.execute(
                """INSERT INTO daily_bars
                   (symbol, date, open, close, high, low, volume, amount, rows_source)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (symbol, "2026-08-24", close, close, close * 1.01,
                 close * 0.99, 0, 0, "test"),
            )
    results = k.evaluate_due(today="2026-08-24", close_providers=[])
    status = {r["stock_code"]: r["status"] for r in results}
    assert status["600535"] == "hit"     # 15.78→19.00
    assert status["600019"] == "miss"    # 5.91→6.30


def test_register_recommendation(env):
    """洞1：推荐自动登记 — 截止日=推荐日起第 22 个交易日，幂等"""
    kb, mkt = env
    k = KpiTracker(kb, mkt)
    dl = k.register_recommendation("R47", "600019", "宝钢股份", "2026-08-05", 5.86)
    assert dl == "2026-09-03"                     # 08-05 起第 22 个交易日
    items = k.all()
    assert len(items) == 1
    assert items[0]["status"] == "tracking"
    assert items[0]["target_price"] == round(5.86 * 1.2, 2)
    assert items[0]["source"] == "sim"           # 闭环默认模拟盘口径
    # 幂等：同轮次同标的同推荐日不重复登记
    assert k.register_recommendation("R47", "600019", "宝钢股份",
                                     "2026-08-05", 5.86) is None
    assert len(k.all()) == 1


def test_register_multiple_candidates(env):
    """不同候选各自登记，互不覆盖"""
    kb, mkt = env
    k = KpiTracker(kb, mkt)
    k.register_recommendation("R47", "600019", "宝钢股份", "2026-08-05", 5.86)
    k.register_recommendation("R47", "600535", "天士力", "2026-08-05", 15.72)
    assert len(k.all()) == 2


def test_first_eval_rehearsal_miss(env):
    """08-24 首评预演：宝钢收 5.76（现价水平）→ 未达标，实际 -2.54%"""
    kb, mkt = env
    k = KpiTracker(kb, mkt)
    k.seed_manual()
    with get_connection(mkt) as conn:
        conn.execute(
            """INSERT INTO daily_bars
               (symbol, date, open, close, high, low, volume, amount, rows_source)
               VALUES ('sh600019','2026-08-24',5.76,5.76,5.82,5.70,0,0,'test')"""
        )
    results = k.evaluate_due(today="2026-08-24", close_providers=[])
    r = results[0]
    assert r["status"] == "miss"
    assert r["actual_pct"] == -2.54        # (5.76/5.91-1)*100
    assert r["eval_close"] == 5.76


def test_first_eval_rehearsal_hit(env):
    """08-24 首评预演：宝钢收 6.80（目标价）→ 达标 +15.06%"""
    kb, mkt = env
    k = KpiTracker(kb, mkt)
    k.seed_manual()
    with get_connection(mkt) as conn:
        conn.execute(
            """INSERT INTO daily_bars
               (symbol, date, open, close, high, low, volume, amount, rows_source)
               VALUES ('sh600019','2026-08-24',6.80,6.80,6.87,6.73,0,0,'test')"""
        )
    results = k.evaluate_due(today="2026-08-24", close_providers=[])
    r = results[0]
    assert r["status"] == "hit"
    assert r["actual_pct"] == 15.06        # (6.80/5.91-1)*100


def test_render_report_sample_insufficiency(env):
    """首评保障：真实盘已评估 1 笔 → 只报数字、不作体系结论，标注单票风险"""
    kb, mkt = env
    k = KpiTracker(kb, mkt)
    with get_connection(kb) as conn:
        conn.execute(
            """INSERT INTO kpi_tracking
               (round_number, stock_code, stock_name, rec_date, rec_close,
                target_pct, target_price, deadline, status, source, note, created_at)
               VALUES ('R48-R52','600019','宝钢股份','2026-07-24',5.91,15.0,6.80,
                       '2026-08-24','tracking','real','test','2026-07-24')"""
        )
    with get_connection(mkt) as conn:
        conn.execute(
            """INSERT INTO daily_bars
               (symbol, date, open, close, high, low, volume, amount, rows_source)
               VALUES ('sh600019','2026-08-24',5.76,5.76,5.82,5.70,0,0,'test')"""
        )
    k.evaluate_due(today="2026-08-24", close_providers=[])
    md = k.render_report(today="2026-08-24")
    assert "0/1 = 0.0%" in md
    assert "样本不足 1 笔" in md
    assert "暂不作体系结论" in md
    assert "不代表体系能力" in md
    assert "（目标 >60.0%，❌未达标）" not in md   # 不做体系级结论


def test_render_report_normal_verdict_at_3(env):
    """达到 3 笔真实盘样本后恢复体系结论（含达标/未达标判定）"""
    kb, mkt = env
    k = KpiTracker(kb, mkt)
    with get_connection(kb) as conn:
        for code, name, rec, target in (
            ("600019", "宝钢股份", 5.91, 15.0),
            ("600535", "天士力", 15.78, 20.0),
            ("002366", "融发核电", 6.74, 20.0),
        ):
            conn.execute(
                """INSERT INTO kpi_tracking
                   (round_number, stock_code, stock_name, rec_date, rec_close,
                    target_pct, target_price, deadline, status, source, note, created_at)
                   VALUES ('R48-R52',?,?,?,?,?,?,'2026-08-24','tracking','real','test','2026-07-24')""",
                (code, name, "2026-07-24", rec, target,
                 round(rec * (1 + target / 100), 2)),
            )
    with get_connection(mkt) as conn:
        for code, close in (("600019", 5.76), ("600535", 19.00), ("002366", 6.50)):
            symbol = ("sh" if code.startswith("6") else "sz") + code
            conn.execute(
                """INSERT INTO daily_bars
                   (symbol, date, open, close, high, low, volume, amount, rows_source)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (symbol, "2026-08-24", close, close, close * 1.01,
                 close * 0.99, 0, 0, "test"),
            )
    k.evaluate_due(today="2026-08-24", close_providers=[])
    md = k.render_report(today="2026-08-24")
    assert "1/3 = 33.3%" in md
    assert "❌未达标" in md                # 3 笔恢复体系结论
    assert "样本不足" not in md
