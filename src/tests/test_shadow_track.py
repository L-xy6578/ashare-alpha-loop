"""影子跟踪：基线幂等 / 每日追加 / 缺价标注 / 分组均值 / 自动补齐"""

import sys

import pytest

import src.shadow_track as shadow_track
from src.data_source import KlineBar
from src.shadow_track import (
    BASELINE_DATE,
    BUYABLE_SHADOW_SAMPLE,
    backfill,
    group_stats,
    init,
    read_rows,
    update,
)


def _flat_provider(price=10.0):
    return lambda symbol, day: (price, "test")


def _bench(dates_closes):
    bars = [KlineBar(d, c, c, c, c) for d, c in dates_closes]
    return lambda start, end: ("沪深300", bars)


def _bench_by_date(prices):
    return lambda start, end: ("沪深300", [
        KlineBar(BASELINE_DATE, prices[BASELINE_DATE], prices[BASELINE_DATE],
                 prices[BASELINE_DATE], prices[BASELINE_DATE]),
        KlineBar(end, prices[end], prices[end], prices[end], prices[end]),
    ])


@pytest.fixture()
def paths(tmp_path):
    return tmp_path / "track.csv", tmp_path / "out"


@pytest.fixture()
def days(monkeypatch):
    """固定目标日，避免测试依赖真实交易日历"""
    monkeypatch.setattr(shadow_track, "_latest_closed_day", lambda d=None: d or "2026-11-05")


def test_init_idempotent(paths):
    csv_path, _ = paths
    provider = _flat_provider(10.0)
    assert len(init(BASELINE_DATE, path=csv_path, close_provider=provider)) == len(BUYABLE_SHADOW_SAMPLE)
    first = read_rows(csv_path)
    assert len(first) == len(BUYABLE_SHADOW_SAMPLE)
    assert {r["date"] for r in first} == {BASELINE_DATE}
    init(BASELINE_DATE, path=csv_path, close_provider=provider)   # 重复执行
    assert len(read_rows(csv_path)) == len(BUYABLE_SHADOW_SAMPLE)          # 不产生重复行


def test_update_appends_and_computes(paths):
    csv_path, out_dir = paths
    init(BASELINE_DATE, path=csv_path, close_provider=_flat_provider(10.0))
    rows = update("2026-11-02", path=csv_path, out_dir=out_dir,
                  close_provider=_flat_provider(11.0),
                  benchmark_provider=_bench([("2026-10-30", 100.0), ("2026-11-02", 102.0)]))
    assert len(read_rows(csv_path)) == len(BUYABLE_SHADOW_SAMPLE) * 2
    r = rows[0]
    assert float(r["day_pct"]) == pytest.approx(10.0)
    assert float(r["cum_pct"]) == pytest.approx(10.0)
    assert float(r["bench_cum"]) == pytest.approx(2.0)
    assert float(r["rel_pct"]) == pytest.approx(8.0)
    assert r["benchmark"] == "沪深300"
    md = out_dir / "影子组合_2026-11-02.md"
    assert md.exists() and "model_high" in md.read_text(encoding="utf-8")


def test_missing_price_marked(paths):
    csv_path, out_dir = paths
    init(BASELINE_DATE, path=csv_path, close_provider=_flat_provider(10.0))

    def provider(symbol, day):
        if symbol == "sh603882":
            return None, "none"
        return 10.5, "test"

    rows = update("2026-11-02", path=csv_path, out_dir=out_dir,
                  close_provider=provider,
                  benchmark_provider=_bench([("2026-10-30", 100.0), ("2026-11-02", 101.0)]))
    bad = next(r for r in rows if r["symbol"] == "sh603882")
    assert bad["status"] == "未取到"
    assert bad["close"] == "" and bad["cum_pct"] == "" and bad["rel_pct"] == ""


def test_benchmark_missing_uses_sample_mean(paths):
    csv_path, out_dir = paths
    init(BASELINE_DATE, path=csv_path, close_provider=_flat_provider(10.0))
    rows = update("2026-11-02", path=csv_path, out_dir=out_dir,
                  close_provider=_flat_provider(11.0),
                  benchmark_provider=lambda s, e: ("无基准", []))
    cum = [float(r["cum_pct"]) for r in rows]
    assert all(abs(c - 10.0) < 1e-6 for c in cum)
    assert all(float(r["rel_pct"]) == 0.0 for r in rows)
    assert rows[0]["benchmark"] == "无基准"


def test_group_stats_means():
    rows = [
        {"group": "model_high", "status": "ok", "cum_pct": "12.0"},
        {"group": "model_high", "status": "ok", "cum_pct": "8.0"},
        {"group": "model_high", "status": "未取到", "cum_pct": ""},
        {"group": "control_low_base", "status": "ok", "cum_pct": "5.0"},
    ]
    st = group_stats(rows)
    assert st["model_high"]["count"] == 2
    assert st["model_high"]["missing"] == 1
    assert st["model_high"]["mean_cum"] == pytest.approx(10.0)
    assert st["control_low_base"]["mean_cum"] == pytest.approx(5.0)


def test_update_without_baseline_raises(paths):
    csv_path, out_dir = paths
    with pytest.raises(RuntimeError):
        update("2026-11-02", path=csv_path, out_dir=out_dir,
               close_provider=_flat_provider(10.0))


# ---------- 自动补齐 ----------

def test_backfill_multiple_days_in_order(paths, days):
    csv_path, out_dir = paths
    init(BASELINE_DATE, path=csv_path, close_provider=_flat_provider(10.0))
    prices = {BASELINE_DATE: 10.0, "2026-11-02": 11.0, "2026-11-03": 11.5,
              "2026-11-04": 12.0, "2026-11-05": 12.5}
    trading = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"]
    summary = backfill("2026-11-05", path=csv_path, out_dir=out_dir,
                       close_provider=lambda symbol, day: (prices[day], "test"),
                       benchmark_provider=_bench_by_date(prices),
                       days_provider=lambda start, end: trading)
    assert summary["filled"] == trading
    assert summary["rows"] == len(BUYABLE_SHADOW_SAMPLE) * len(trading)
    rows = read_rows(csv_path)
    assert {r["date"] for r in rows} == {BASELINE_DATE, *trading}
    last = [r for r in rows if r["date"] == "2026-11-05"]
    # 逐日链式计算：11-05 相对 11-04 的当日涨幅 +4.17%（12.5/12.0）
    assert float(last[0]["day_pct"]) == pytest.approx(4.17)
    assert float(last[0]["cum_pct"]) == pytest.approx(25.0)


def test_backfill_idempotent(paths, days):
    csv_path, out_dir = paths
    init(BASELINE_DATE, path=csv_path, close_provider=_flat_provider(10.0))
    trading = ["2026-11-02", "2026-11-03"]
    kw = dict(path=csv_path, out_dir=out_dir, close_provider=_flat_provider(11.0),
              benchmark_provider=_bench([(BASELINE_DATE, 100.0)]),
              days_provider=lambda start, end: trading)
    first = backfill("2026-11-03", **kw)
    second = backfill("2026-11-03", **kw)
    assert first["filled"] == trading and first["rows"] == len(BUYABLE_SHADOW_SAMPLE) * 2
    assert second["filled"] == [] and second["rows"] == 0
    assert len(read_rows(csv_path)) == len(BUYABLE_SHADOW_SAMPLE) * 3   # 无重复行


def test_backfill_failure_day_continues(paths, days):
    csv_path, out_dir = paths
    init(BASELINE_DATE, path=csv_path, close_provider=_flat_provider(10.0))
    prices = {BASELINE_DATE: 10.0, "2026-11-02": 11.0, "2026-11-03": 11.5,
              "2026-11-04": 12.0}
    trading = ["2026-11-02", "2026-11-03", "2026-11-04"]

    def provider(symbol, day):
        if day == "2026-11-03" and symbol == "sh603882":
            return None, "none"          # 单只取价失败
        return prices[day], "test"

    summary = backfill("2026-11-04", path=csv_path, out_dir=out_dir,
                       close_provider=provider,
                       benchmark_provider=_bench_by_date(prices),
                       days_provider=lambda start, end: trading)
    assert summary["filled"] == trading                 # 失败日不中断后续
    assert summary["missing"] == {"2026-11-03": ["金域医学"]}
    rows_1103 = [r for r in read_rows(csv_path) if r["date"] == "2026-11-03"]
    bad = next(r for r in rows_1103 if r["symbol"] == "sh603882")
    assert bad["status"] == "未取到" and bad["close"] == ""
    rows_1104 = [r for r in read_rows(csv_path) if r["date"] == "2026-11-04"]
    recovered = next(r for r in rows_1104 if r["symbol"] == "sh603882")
    assert recovered["status"] == "ok"
    assert float(recovered["day_pct"]) == pytest.approx(9.09)   # 用 11-02 的 11.0 作前收


def test_backfill_retries_failed_recorded_day(paths, days):
    """已记录但全部“未取到”的日期会被重试并覆盖原行"""
    csv_path, out_dir = paths
    init(BASELINE_DATE, path=csv_path, close_provider=_flat_provider(10.0))
    update("2026-11-02", path=csv_path, out_dir=out_dir,
           close_provider=lambda symbol, day: (None, "none"),
           benchmark_provider=_bench([(BASELINE_DATE, 100.0)]))
    assert {r["status"] for r in read_rows(csv_path)
            if r["date"] == "2026-11-02"} == {"未取到"}

    summary = backfill("2026-11-02", path=csv_path, out_dir=out_dir,
                       close_provider=_flat_provider(11.0),
                       benchmark_provider=_bench([(BASELINE_DATE, 100.0),
                                                  ("2026-11-02", 101.0)]),
                       days_provider=lambda start, end: [])
    assert summary["retried"] == ["2026-11-02"]
    assert summary["filled"] == ["2026-11-02"]
    assert summary["rows"] == len(BUYABLE_SHADOW_SAMPLE)
    rows = [r for r in read_rows(csv_path) if r["date"] == "2026-11-02"]
    assert len(rows) == len(BUYABLE_SHADOW_SAMPLE)                  # 覆盖，不产生重复行
    assert {r["status"] for r in rows} == {"ok"}
    assert float(rows[0]["cum_pct"]) == pytest.approx(10.0)


def test_backfill_retry_still_failed_keeps_missing(paths, days):
    csv_path, out_dir = paths
    init(BASELINE_DATE, path=csv_path, close_provider=_flat_provider(10.0))
    update("2026-11-02", path=csv_path, out_dir=out_dir,
           close_provider=lambda symbol, day: (None, "none"),
           benchmark_provider=_bench([(BASELINE_DATE, 100.0)]))
    summary = backfill("2026-11-02", path=csv_path, out_dir=out_dir,
                       close_provider=lambda symbol, day: (None, "none"),
                       benchmark_provider=_bench([(BASELINE_DATE, 100.0)]),
                       days_provider=lambda start, end: [])
    assert summary["filled"] == ["2026-11-02"]
    assert summary["missing"] == {"2026-11-02": [s["name"] for s in BUYABLE_SHADOW_SAMPLE]}
    rows = [r for r in read_rows(csv_path) if r["date"] == "2026-11-02"]
    assert len(rows) == len(BUYABLE_SHADOW_SAMPLE) and all(
        r["status"] == "未取到" for r in rows)


def test_backfill_stale_failure_outside_window_not_retried(paths, days):
    """超出最近 10 个已记录交易日窗口的失败只提示，不自动重试"""
    csv_path, out_dir = paths
    init(BASELINE_DATE, path=csv_path, close_provider=_flat_provider(10.0))
    trading = [f"2026-11-{d:02d}" for d in range(2, 14)]   # 12 个日期全部失败
    for d in trading:
        update(d, path=csv_path, out_dir=out_dir,
               close_provider=lambda symbol, day: (None, "none"),
               benchmark_provider=_bench([(BASELINE_DATE, 100.0)]))
    summary = backfill(trading[-1], path=csv_path, out_dir=out_dir,
                       close_provider=_flat_provider(11.0),
                       benchmark_provider=_bench([(BASELINE_DATE, 100.0)]),
                       days_provider=lambda start, end: [])
    recent = trading[-10:]
    assert summary["stale_failed"] == trading[:-10]
    assert summary["retried"] == recent and summary["filled"] == recent
    stale_rows = [r for r in read_rows(csv_path) if r["date"] in trading[:-10]]
    assert all(r["status"] == "未取到" for r in stale_rows)   # 未被重试


def test_backfill_respects_max_days(paths, days):
    csv_path, out_dir = paths
    init(BASELINE_DATE, path=csv_path, close_provider=_flat_provider(10.0))
    trading = [f"2026-11-{d:02d}" for d in range(2, 12)]   # 10 天
    summary = backfill("2026-11-11", path=csv_path, out_dir=out_dir,
                       close_provider=_flat_provider(10.5),
                       benchmark_provider=_bench([(BASELINE_DATE, 100.0)]),
                       days_provider=lambda start, end: trading, max_days=3)
    assert summary["filled"] == trading[:3]
    assert summary["overflow"] == trading[3:]


def test_no_backfill_updates_only_one_day(paths, days, monkeypatch):
    csv_path, out_dir = paths
    calls = []

    def build(day):
        return [{
            "date": day, "symbol": s["symbol"], "name": s["name"],
            "group": s["group"], "board": s["board"], "close": "10.00",
            "day_pct": "0.00", "cum_pct": "0.00", "trade_days": "0",
            "benchmark": "沪深300", "bench_cum": "0.00", "rel_pct": "0.00",
            "status": "ok", "industry": s["industry"], "growth": s["growth"],
            "valuation": s["valuation"], "technical": s["technical"],
            "capital": s["capital"], "score": s["score"],
        } for s in BUYABLE_SHADOW_SAMPLE]

    def fake_update(day=None, **kw):
        calls.append(day)
        return build(day)

    monkeypatch.setattr(shadow_track, "OUT_DIR", out_dir)
    monkeypatch.setattr(shadow_track, "update", fake_update)
    monkeypatch.setattr(shadow_track, "read_rows", lambda path=None: build("2026-11-02"))
    monkeypatch.setattr(shadow_track, "backfill",
                        lambda *a, **kw: pytest.fail("--no-backfill 不应调用自动补齐"))
    monkeypatch.setattr(sys, "argv", ["shadow", "--no-backfill", "--date", "2026-11-02"])
    shadow_track.main()
    assert calls == ["2026-11-02"]


# ---------- 收盘闭环接入 ----------

def test_closed_loop_shadow_failure_degrades(monkeypatch, capsys):
    import r47_closed_loop as r47

    def boom(**kwargs):
        raise RuntimeError("取价网络中断")

    monkeypatch.setattr(shadow_track, "backfill", boom)
    warnings: list[str] = []
    r47._run_shadow_tracking(False, warnings)        # 不抛异常 → 不改退出码
    assert warnings == ["影子跟踪更新失败（不影响闭环）: 取价网络中断"]
    assert "影子跟踪更新失败" in capsys.readouterr().out


def test_closed_loop_shadow_dry_run_skips(monkeypatch):
    import r47_closed_loop as r47

    called = []
    monkeypatch.setattr(shadow_track, "backfill", lambda **kw: called.append(1))
    warnings: list[str] = []
    r47._run_shadow_tracking(True, warnings)
    assert called == [] and warnings == []


# ---------- 全候选面板 ----------

DAY = "2026-11-02"


def _panel_sources(tmp_path, bars_days=12):
    """造最小 market.db / knowledge.db（factor_snapshot + daily_bars + decision_log）"""
    import sqlite3

    m, k = tmp_path / "market.db", tmp_path / "knowledge.db"
    with sqlite3.connect(m) as c:
        c.execute("""CREATE TABLE factor_snapshot (
            date TEXT, symbol TEXT, resolved_board TEXT, score REAL,
            industry REAL, growth REAL, valuation REAL, technical REAL,
            capital REAL, missing TEXT)""")
        c.execute("""CREATE TABLE daily_bars (
            symbol TEXT, date TEXT, open REAL, close REAL, high REAL, low REAL,
            volume REAL, amount REAL)""")
        c.execute("CREATE TABLE watchlist (symbol TEXT, name TEXT)")
        for i, sym in enumerate(("sz000001", "sz000002")):
            c.execute("INSERT INTO watchlist VALUES (?,?)", (sym, f"自选{i + 1}"))
        for i, sym in enumerate(("sz000001", "sz000002")):
            c.execute("INSERT INTO factor_snapshot VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (DAY, sym, "银行", 80.0 + i, 70.0, 75.0, 85.0, 60.0, 50.0, ""))
            for d in range(bars_days):
                px = 10.0 + i + d * 0.1
                c.execute("INSERT INTO daily_bars VALUES (?,?,?,?,?,?,?,?)",
                          (sym, f"2026-11-{d + 2:02d}", px, px, px, px, 1000.0, px * 1000))
    with sqlite3.connect(k) as c:
        c.execute("""CREATE TABLE decision_log (
            decision_date TEXT, symbol TEXT, name TEXT, depths TEXT, vcp INTEGER,
            contractions INTEGER, vol_ratio REAL, sector_pct REAL, decision TEXT)""")
        for i, sym in enumerate(("sz000001", "sz000002")):
            c.execute("INSERT INTO decision_log VALUES (?,?,?,?,?,?,?,?,?)",
                      (DAY, sym, f"测试{i + 1}", "[3.1, 2.2]", 0, 2, 0.81, 60.0, "ABSTAIN"))
    return str(m), str(k)


def test_record_panel_append_only_and_fields(tmp_path):
    market_db, kb_db = _panel_sources(tmp_path)
    path = tmp_path / "候选面板.csv"

    rows = shadow_track.record_panel(DAY, panel_path=path, market_db=market_db, kb_db=kb_db)
    assert len(rows) == 2
    assert rows[0]["name"] == "测试1" and rows[0]["last_depth"] == "2.20"
    assert rows[0]["vcp"] == "0" and rows[0]["vol_ratio"] == "0.81"
    assert rows[0]["close"] == "10.00"

    again = shadow_track.record_panel(DAY, panel_path=path, market_db=market_db, kb_db=kb_db)
    assert len(again) == 2
    assert len(path.read_text(encoding="utf-8").strip().splitlines()) == 3   # 表头 + 2 行，未重复追加


def test_fill_returns_appends_each_horizon_once(tmp_path):
    market_db, kb_db = _panel_sources(tmp_path)
    path, ret = tmp_path / "候选面板.csv", tmp_path / "面板收益.csv"
    shadow_track.record_panel(DAY, panel_path=path, market_db=market_db, kb_db=kb_db)

    new = shadow_track.fill_returns(panel_path=path, ret_path=ret, market_db=market_db)
    assert len(new) == 4                      # 2 只 × (5 日, 10 日)
    assert {r["horizon"] for r in new} == {5, 10}
    r5 = [r for r in new if r["symbol"] == "sz000001" and r["horizon"] == 5][0]
    assert r5["exit_day"] == "2026-11-07"
    assert r5["ret_pct"] == "5.00"            # 10.5/10.0-1

    assert shadow_track.fill_returns(panel_path=path, ret_path=ret, market_db=market_db) == []


def test_panel_stats_ic_and_spread(tmp_path):
    path, ret = tmp_path / "候选面板.csv", tmp_path / "面板收益.csv"
    path.write_text("day,symbol,score\n", encoding="utf-8")
    with path.open("a", encoding="utf-8") as f:
        f.writelines(["2026-11-02,a,10.0\n", "2026-11-02,b,20.0\n",
                      "2026-11-02,c,30.0\n", "2026-11-02,d,5.0\n"])
    with ret.open("w", encoding="utf-8", newline="") as f:
        f.write("day,symbol,horizon,exit_day,ret_pct\n")
        f.writelines(["2026-11-02,a,20,2026-11-30,1.0\n",
                      "2026-11-02,b,20,2026-11-30,2.0\n",
                      "2026-11-02,c,20,2026-11-30,3.0\n",
                      "2026-11-02,d,20,2026-11-30,0.5\n"])

    st = shadow_track.panel_stats(panel_path=path, ret_path=ret, horizon=20)
    assert st["ic_mean"] == 1.0 and st["ic_days"] == 1 and st["obs"] == 4
    assert st["top_mean"] == 3.0 and st["bottom_mean"] == 0.5 and st["spread"] == 2.5
