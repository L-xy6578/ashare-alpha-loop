"""决策记录：幂等 / append-only / 无候选留痕 / 费用 / KPI 口径"""

import json
import sqlite3
from datetime import date, timedelta

import pytest

from src.data_source import KlineBar
from src.decision_log import (CandidateInput, DecisionStore, append_csv, calc_fees,
                              decision_kpi, init_decision_db, make_decision_row,
                              make_no_candidate_row, render_day_markdown,
                              trading_days_after, write_day_markdown)
from src.vcp import detect_vcp


_LEGACY_PLAN_DDL = """
CREATE TABLE decision_plans (
    symbol TEXT PRIMARY KEY,
    decision_date TEXT NOT NULL,
    name TEXT NOT NULL,
    entry_date TEXT,
    entry_price REAL,
    shares_total INTEGER,
    shares_remaining INTEGER,
    tier1_done INTEGER DEFAULT 0,
    tier2_done INTEGER DEFAULT 0,
    tp1 REAL, tp2 REAL, stop_price REAL,
    box_high REAL, box_low REAL,
    peak_price REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN',
    updated_at TEXT NOT NULL
)
"""


def test_close_reason_migration_from_legacy_17col_db(tmp_path):
    """17 列旧库 → ALTER 加 close_reason：旧行保留、新列默认 ''、重复迁移幂等、18 列写入正常"""
    db = str(tmp_path / "legacy.db")
    with sqlite3.connect(db) as conn:
        conn.execute(_LEGACY_PLAN_DDL)
        conn.execute(
            "INSERT INTO decision_plans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("sh600019", "2026-11-05", "宝钢股份", "2026-11-06", 10.0, 200, 100,
             1, 0, 10.8, 11.5, 9.4, 10.2, 10.0, 10.9, "OPEN", "2026-11-06 15:35:00"),
        )

    init_decision_db(db)

    with sqlite3.connect(db) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(decision_plans)")]
        old = conn.execute(
            "SELECT symbol, shares_remaining, status, close_reason FROM decision_plans"
        ).fetchone()
    assert "close_reason" in cols and len(cols) == 18
    assert old == ("sh600019", 100, "OPEN", "")      # 旧行保留，新列为 ''

    init_decision_db(db)                              # 重复迁移不报错、不加第二列
    with sqlite3.connect(db) as conn:
        assert len([r[1] for r in conn.execute("PRAGMA table_info(decision_plans)")]) == 18

    store = DecisionStore(db)
    store.upsert_plan(
        symbol="sh600019", decision_date="2026-11-05", name="宝钢股份",
        entry_date="2026-11-06", entry_price=10.0, shares_total=200,
        shares_remaining=0, tier1_done=1, tier2_done=1, tp1=10.8, tp2=11.5,
        stop_price=9.4, box_high=10.2, box_low=10.0, peak_price=11.6,
        status="CLOSED", close_reason="分批止盈卖光（测试）",
    )
    plan = store.get_plan("sh600019")
    assert plan["status"] == "CLOSED"
    assert plan["close_reason"] == "分批止盈卖光（测试）"
    assert plan["shares_remaining"] == 0


def _vcp_bars():
    up = [88 + i * 1.2 for i in range(11)]
    down = [98, 96, 94, 92, 90]
    up2 = [91, 93, 95, 97]
    down2 = [95, 94, 93]
    up3 = [93.5, 94, 94.5]
    down3 = [94.2, 93.8, 93.5]
    closes = up + down + up2 + down2 + up3 + down3
    vols = [12000] * len(closes)
    last_start = len(up) + len(down) + len(up2) + len(down2) + len(up3) - 1
    vols[last_start:] = [3000] * (len(closes) - last_start)
    return [KlineBar(f"d{i:02d}", c, c, c * 1.01, c * 0.99, volume=v)
            for i, (c, v) in enumerate(zip(closes, vols))]


def _entry_row(store, decision_date="2026-11-06"):
    bars = _vcp_bars()
    vcp = detect_vcp(bars)
    cand = CandidateInput(
        symbol="sh600019", code="600019", name="宝钢股份", sector="钢铁",
        price=bars[-1].close, bars=bars, score=80.0,
        factors={"industry": 60, "growth": 80, "valuation": 70,
                 "technical": 90, "capital": 75},
        missing=(), vcp=vcp, sector_pct=60.0, first_shares=200,
    )
    return make_decision_row(
        decision_date=decision_date, data_date=decision_date,
        cand=cand, snapshot_date=decision_date, snapshot_complete=True,
        git_head="8dde356", git_dirty=False,
    )


def test_calc_fees():
    buy = calc_fees("BUY", 10000)
    assert buy["commission"] == 5.0       # 万三=3 元，最低 5 元
    assert buy["stamp_tax"] == 0.0
    assert buy["transfer_fee"] == 0.1
    assert buy["total_fee"] == 5.1
    sell = calc_fees("SELL", 10000)
    assert sell["stamp_tax"] == 5.0       # 0.05%
    assert sell["total_fee"] == 10.1
    small = calc_fees("BUY", 1000)
    assert small["commission"] == 5.0


def test_trading_days_after_uses_calendar():
    assert trading_days_after("2026-11-06", 3) == "2026-11-11"


def test_decision_is_observe_only(tmp_path):
    """2026-11-11 策略路线变更：评分段一律观察记录，不再产出建仓计划"""
    store = DecisionStore(str(tmp_path / "k.db"))
    row = _entry_row(store)
    assert row["decision"] == "OBSERVE"
    assert row["status"] == "OBSERVE"
    assert row["reason"].startswith("观察记录｜")
    assert row.get("shares") is None and row.get("zone_low") is None
    assert row["git_head"] == "8dde356"

    # 条件不满足：原因仍然逐条记录
    bars = _vcp_bars()
    vcp = detect_vcp(bars)
    cand = CandidateInput(
        symbol="sh600019", code="600019", name="宝钢股份", sector="钢铁",
        price=bars[-1].close, bars=bars, score=65.0, factors={},
        missing=(), vcp=vcp, sector_pct=95.0, first_shares=200,
    )
    ab = make_decision_row(
        decision_date="2026-11-06", data_date="2026-11-06", cand=cand,
        snapshot_date="2026-11-06", snapshot_complete=True,
        git_head="8dde356", git_dirty=True,
    )
    assert ab["decision"] == "OBSERVE"
    assert "65.0" in ab["reason"]
    assert "最热头部" in ab["reason"]


def test_store_is_idempotent(tmp_path):
    store = DecisionStore(str(tmp_path / "k.db"))
    row = _entry_row(store)
    assert store.save_rows([row]) == (1, 0)
    assert store.save_rows([row]) == (0, 1)
    assert len(store.rows_by_date("2026-11-06")) == 1


def test_markdown_and_csv_append_only(tmp_path):
    store = DecisionStore(str(tmp_path / "k.db"))
    row = _entry_row(store)
    md = render_day_markdown(
        decision_date="2026-11-06", data_date="2026-11-06",
        rows=[row], excluded=[], snapshot_date="2026-11-06",
        snapshot_complete=True, git_head="8dde356", git_dirty=False,
    )
    path, created = write_day_markdown("2026-11-06", md, out_dir=tmp_path)
    assert created and path.exists()
    path2, created2 = write_day_markdown("2026-11-06", "覆盖内容", out_dir=tmp_path)
    assert not created2
    assert path2.read_text(encoding="utf-8") == md   # 不被覆盖
    assert append_csv([row], out_dir=tmp_path) == 1
    assert append_csv([row], out_dir=tmp_path) == 0


def test_no_candidate_still_recorded(tmp_path):
    store = DecisionStore(str(tmp_path / "k.db"))
    row = make_no_candidate_row(
        decision_date="2026-11-06", data_date="2026-11-06",
        reason="可执行性过滤后 0 只可买候选", snapshot_date="2026-11-06",
        snapshot_complete=True, git_head="8dde356", git_dirty=False,
    )
    store.save_rows([row])
    rows = store.rows_by_date("2026-11-06")
    assert len(rows) == 1
    assert rows[0]["decision"] == "NO_CANDIDATE"
    assert "0 只" in rows[0]["reason"]


def test_kpi_only_counts_closed_entries(tmp_path):
    """KPI 只认历史 ENTRY（改造前建仓计划）；新产生的 OBSERVE 不计入"""
    store = DecisionStore(str(tmp_path / "k.db"))
    row = _entry_row(store)
    row["decision"] = "ENTRY"      # 模拟改造前的历史建仓计划
    row["status"] = "PENDING"
    store.save_rows([row])
    observe = _entry_row(store, decision_date="2026-11-11")
    observe["symbol"], observe["code"], observe["name"] = "sh600000", "600000", "浦发银行"
    store.save_rows([observe])
    store.update_log_status("2026-11-06", "sh600019", "CLOSED")
    fees_buy = calc_fees("BUY", 2020.0)
    fees_sell = calc_fees("SELL", 2181.6)
    store.add_trade(
        decision_date="2026-11-06", symbol="sh600019", name="宝钢股份",
        trade_date="2026-11-07", side="BUY", shares=200, price=10.1,
        amount=2020.0, **fees_buy, reason="首仓",
    )
    store.add_trade(
        decision_date="2026-11-06", symbol="sh600019", name="宝钢股份",
        trade_date="2026-11-10", side="SELL", shares=200, price=10.908,
        amount=2181.6, **fees_sell, reason="清仓", return_pct=8.0, pnl=146.4,
    )
    kpi = decision_kpi(str(tmp_path / "k.db"))
    assert kpi["closed"] == 1 and kpi["wins"] == 1
    assert kpi["win_rate"] == 100.0
    assert kpi["total_pnl"] > 0


def test_markdown_lists_analysis_only_excluded(tmp_path):
    """仅分析不可买（创业板/科创板/北交所）必须在产物中单列，不能静默消失"""
    store = DecisionStore(str(tmp_path / "k.db"))
    row = _entry_row(store)
    excluded = [
        {"code": "301510", "name": "固高科技",
         "reason": "创业板（301510）仅分析不可买（用户只能买主板）",
         "analysis_only": True},
        {"code": "688396", "name": "华润微",
         "reason": "科创板（688396）仅分析不可买（用户只能买主板）",
         "analysis_only": True},
        {"code": "603259", "name": "药明康德",
         "reason": "股价 147.00 > 60 元（一手 14700 元 > 单只上限 6000 元）",
         "analysis_only": False},
    ]
    md = render_day_markdown(
        decision_date="2026-11-06", data_date="2026-11-06",
        rows=[row], excluded=excluded, snapshot_date="2026-11-06",
        snapshot_complete=True, git_head="8dde356", git_dirty=True,
    )
    only_section = md.split("## 二、仅分析不可买")[1].split("## 三、")[0]
    assert "固高科技" in only_section and "301510" in only_section
    assert "华润微" in only_section and "科创板" in only_section
    assert "药明康德" not in only_section            # 价格排除不混入"仅分析"节
    assert "药明康德" in md.split("## 三、其他可执行性排除")[1].split("## 四、")[0]

