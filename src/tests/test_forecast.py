"""每日预判 + 到期对账 + 历史回溯 测试

覆盖：append-only、作废排除、唯一键迁移幂等、命中率表格口径、
回放生成不读未来数据、回溯统计函数。
"""

import sqlite3

import pytest

import src.forecast as fc
import src.forecast_backtest as fb
from src.pipeline.schema import init_market_db


# ============================================================
# 夹具
# ============================================================
@pytest.fixture()
def kb(tmp_path, monkeypatch):
    path = str(tmp_path / "knowledge.db")
    monkeypatch.setattr(fc, "DB_PATH", path)
    fc.init_forecast_tables(path)
    return path


@pytest.fixture()
def mkt(tmp_path):
    path = str(tmp_path / "market.db")
    with sqlite3.connect(path) as conn:
        init_market_db(conn)
        conn.commit()
    return path


def _bar(conn, symbol, date, close, volume=1000.0):
    conn.execute(
        "INSERT OR REPLACE INTO daily_bars (symbol, date, open, close, high, low, volume, amount) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (symbol, date, close, close, close, close, volume, close * volume),
    )


def _make_market(mkt, closes, symbol="sh000300"):
    """写入连续交易日的行情（日期从 2026-01-05 起逐日递增，跳过周末不做精细处理）。"""
    from datetime import date, timedelta

    d = date(2026, 1, 5)
    dates = []
    with sqlite3.connect(mkt) as conn:
        for c in closes:
            while d.weekday() >= 5:
                d += timedelta(days=1)
            iso = d.isoformat()
            dates.append(iso)
            _bar(conn, symbol, iso, c)
            _bar(conn, "sh000001", iso, c * 30)
            conn.execute(
                "INSERT OR REPLACE INTO trade_calendar (date, is_trading) VALUES (?, 1)", (iso,)
            )
            d += timedelta(days=1)
        conn.commit()
    return dates


def _pred(predict_date="2026-06-01", horizon=1, direction="上涨", run_date=None,
          scope="market", target="sh000300", name="沪深300"):
    return {
        "mode": "live", "predict_date": predict_date,
        "run_date": run_date or predict_date, "scope": scope,
        "target": target, "target_name": name, "horizon": horizon,
        "direction": direction, "strength": "弱",
        "statement": f"{scope} 判定", "rationale": "依据",
        "score": None, "target_date": "2026-06-08", "data_date": predict_date,
    }


# ============================================================
# append-only 与作废
# ============================================================
class TestAppendOnly:
    def test_update_content_aborted(self, kb):
        fc.save_predictions([_pred()], kb)
        with sqlite3.connect(kb) as conn:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("UPDATE forecast_ledger SET direction='下跌'")
            conn.rollback()

    def test_delete_aborted(self, kb):
        fc.save_predictions([_pred()], kb)
        with sqlite3.connect(kb) as conn:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM forecast_ledger")
            conn.rollback()

    def test_settlement_columns_updatable(self, kb):
        fc.save_predictions([_pred()], kb)
        with sqlite3.connect(kb) as conn:
            conn.execute(
                "UPDATE forecast_ledger SET status='HIT', actual_return=1.23, settled_at='now'"
            )
            conn.commit()
        with sqlite3.connect(kb) as conn:
            assert conn.execute("SELECT status FROM forecast_ledger").fetchone()[0] == "HIT"


class TestVoid:
    def test_mark_void_keeps_rows(self, kb):
        n = fc.save_predictions([_pred(predict_date="2026-11-06", run_date="2026-11-07")], kb)
        assert n == 1
        marked = fc.mark_void("2026-11-06", "生成日与落盘日不一致",
                              run_date="2026-11-07", db_path=kb)
        assert marked == 1
        with sqlite3.connect(kb) as conn:
            row = conn.execute(
                "SELECT status, voided, void_reason, direction FROM forecast_ledger"
            ).fetchone()
        assert row[0] == "VOID" and row[1] == 1
        assert row[2] == "生成日与落盘日不一致"
        assert row[3] == "上涨"          # 判断内容未被改动

    def test_void_not_in_hit_rate(self, kb):
        fc.save_predictions([_pred(run_date="2026-06-01")], kb)
        fc.save_predictions([_pred(run_date="2026-06-02")], kb)
        with sqlite3.connect(kb) as conn:
            conn.execute(
                "UPDATE forecast_ledger SET status='HIT' WHERE run_date='2026-06-01'"
            )
            conn.commit()
        fc.mark_void("2026-06-01", "作废测试", run_date="2026-06-02", db_path=kb)
        stats = fc.collect_hit_rate(kb)
        g = stats["groups"][("market", 1)]
        assert g["void"] == 1
        assert g["hit"] == 1
        denom = g["hit"] + g["miss"]
        assert denom == 1                 # 作废样本不进分母
        assert stats["void"] == 1

    def test_void_rows_in_report(self, kb):
        fc.save_predictions([_pred(run_date="2026-06-02")], kb)
        fc.mark_void("2026-06-01", "作废测试", run_date="2026-06-02", db_path=kb)
        md = fc.render_hit_rate_markdown(fc.collect_hit_rate(kb))
        assert "作废留痕" in md and "作废测试" in md
        rows = [ln for ln in md.splitlines() if ln.startswith("| 大盘 | 1日 |")]
        assert len(rows) == 1
        assert rows[0].count("|") == 9   # 8 列 → 9 个分隔符


class TestMigration:
    def test_unique_key_allows_same_predict_date_different_run_date(self, kb):
        a = fc.save_predictions([_pred(run_date="2026-11-09")], kb)
        b = fc.save_predictions([_pred(run_date="2026-11-10")], kb)
        assert a == 1 and b == 1
        with sqlite3.connect(kb) as conn:
            assert conn.execute("SELECT COUNT(*) FROM forecast_ledger").fetchone()[0] == 2

    def test_init_idempotent(self, kb):
        fc.init_forecast_tables(kb)
        fc.init_forecast_tables(kb)
        with sqlite3.connect(kb) as conn:
            ddl = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='forecast_ledger'"
            ).fetchone()[0]
            trig = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'forecast_ledger_immutable%'"
            ).fetchone()[0]
        assert "UNIQUE(mode, predict_date, run_date" in " ".join(ddl.split())
        assert trig == 2


class TestNonCloseBatch:
    """判重键含 predict_date + 非收盘批次（run_date ≠ data_date）一律 VOID"""

    @staticmethod
    def _init_aux_tables(mkt):
        from src.factor_store import init_factor_db
        from src.sector_analysis import SECTOR_META_TABLE, SECTOR_TABLE
        with sqlite3.connect(mkt) as conn:
            conn.executescript(SECTOR_TABLE)
            conn.executescript(SECTOR_META_TABLE)
        init_factor_db(mkt)

    def test_same_run_date_two_data_dates_not_deduped(self, mkt, kb, tmp_path, monkeypatch):
        monkeypatch.setattr(fc, "FORECAST_DIR", tmp_path / "预判")
        self._init_aux_tables(mkt)
        dates = _make_market(mkt, [100.0 + i for i in range(30)])
        run = dates[21]
        a = fc.generate_daily(mkt, kb, data_date=dates[20], run_date=run)
        b = fc.generate_daily(mkt, kb, data_date=run, run_date=run)
        assert a["skipped"] is False and b["skipped"] is False
        with sqlite3.connect(kb) as conn:
            n_dates = conn.execute(
                "SELECT COUNT(DISTINCT predict_date) FROM forecast_ledger WHERE run_date=?", (run,)
            ).fetchone()[0]
        assert n_dates == 2          # 两个数据截止日并存，互不覆盖

    def test_non_close_batch_voided_and_excluded(self, mkt, kb, tmp_path, monkeypatch):
        monkeypatch.setattr(fc, "FORECAST_DIR", tmp_path / "预判")
        self._init_aux_tables(mkt)
        dates = _make_market(mkt, [100.0 + i for i in range(30)])
        run = dates[21]
        a = fc.generate_daily(mkt, kb, data_date=dates[20], run_date=run)
        assert a["voided"] > 0 and "非收盘批次" in a["void_reason"]
        with sqlite3.connect(kb) as conn:
            status, voided = conn.execute(
                "SELECT status, voided FROM forecast_ledger WHERE predict_date=?",
                (dates[20],),
            ).fetchone()
        assert (status, voided) == ("VOID", 1)
        day_file = tmp_path / "预判" / f"预判_{run}.md"
        assert "本批次已作废" in day_file.read_text(encoding="utf-8")

        b = fc.generate_daily(mkt, kb, data_date=run, run_date=run)
        assert b["voided"] == 0      # 收盘批次不作废
        stats = fc.collect_hit_rate(kb)
        g = stats["groups"][("market", 1)]
        assert g["void"] > 0 and g["pending"] == 1     # 作废批次不进分母
        assert g["hit"] == 0 and g["miss"] == 0
        assert fc.void_non_close_batches(kb) == 0      # 幂等，不重复作废

    def test_report_shows_void_batch_without_close_batch(self, mkt, kb, tmp_path, monkeypatch):
        monkeypatch.setattr(fc, "FORECAST_DIR", tmp_path / "预判")
        self._init_aux_tables(mkt)
        dates = _make_market(mkt, [100.0 + i for i in range(30)])
        fc.generate_daily(mkt, kb, data_date=dates[20], run_date=dates[22])
        md = fc.render_hit_rate_markdown(fc.collect_hit_rate(kb))
        assert "作废批次（按生成日）" in md
        rows = [ln for ln in md.splitlines() if f"| {dates[22]} | {dates[20]} |" in ln]
        assert rows and "否（当日无有效收盘批次）" in rows[0]
        assert "非收盘批次" in rows[0]


# ============================================================
# 回放不读未来
# ============================================================
class TestNoLookahead:
    def test_market_generation_truncated_at_as_of(self, mkt):
        closes = [100.0 + i for i in range(30)]      # 单调上涨
        dates = _make_market(mkt, closes)
        as_of = dates[21]
        preds_before = fc.build_market_predictions(mkt, as_of, mode="backtest")
        # 追加暴涨的未来行情，重新生成必须完全不变
        with sqlite3.connect(mkt) as conn:
            from datetime import date, timedelta
            d = date.fromisoformat(dates[-1]) + timedelta(days=1)
            for i, extra in enumerate([50.0, 60.0, 70.0, 80.0, 90.0]):
                while d.weekday() >= 5:
                    d += timedelta(days=1)
                _bar(conn, "sh000300", d.isoformat(), extra)
                _bar(conn, "sh000001", d.isoformat(), extra * 30)
                conn.execute("INSERT OR REPLACE INTO trade_calendar (date,is_trading) VALUES (?,'1')",
                             (d.isoformat(),))
                d += timedelta(days=1)
            conn.commit()
        preds_after = fc.build_market_predictions(mkt, as_of, mode="backtest")
        key = lambda p: (p["direction"], p["strength"], p["rationale"], p["target_date"])
        assert [key(p) for p in preds_before] == [key(p) for p in preds_after]

    def test_load_bars_respects_end_date(self, mkt):
        dates = _make_market(mkt, [100.0 + i for i in range(10)])
        bars = fc.load_bars(mkt, "sh000300", dates[4])
        assert len(bars) == 5
        assert bars[-1].date == dates[4]


# ============================================================
# 个股评分：复用决策侧结果 + 缺数据不出分
# ============================================================
def _seed_live_market(mkt, valuation, score):
    """构造一只可买标的的当日决策侧快照，返回数据截止日（留 10 个后续交易日）。"""
    from datetime import date, timedelta

    from src.factor_store import FactorSnapshotStore
    from src.watchlist import init_watchlist_db

    d = date(2026, 1, 5)
    dates = []
    init_watchlist_db(mkt)
    FactorSnapshotStore(mkt)
    with sqlite3.connect(mkt) as conn:
        init_market_db(conn)
        for i in range(90):
            while d.weekday() >= 5:
                d += timedelta(days=1)
            iso = d.isoformat()
            dates.append(iso)
            _bar(conn, "sh000300", iso, 4000.0 + i)
            _bar(conn, "sh600019", iso, 5.0 + i * 0.01)
            conn.execute(
                "INSERT OR REPLACE INTO trade_calendar (date, is_trading) VALUES (?, 1)", (iso,)
            )
            d += timedelta(days=1)
        conn.execute(
            "INSERT OR REPLACE INTO watchlist "
            "(symbol, code, name, sector, status, updated_at) "
            "VALUES ('sh600019','600019','宝钢股份','钢铁','active','2026-01-05')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO factor_snapshot "
            "(date, symbol, industry, growth, valuation, technical, capital, score, source) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (dates[79], "sh600019", 67.3, 40.0, valuation, 90.0, 55.0, score, "eastmoney"),
        )
        conn.commit()
    return dates[79]


class TestStockScoreSource:
    # 注：本类中的 83.4 / 66.4 为合成夹具（因子齐全 vs 缺估值），
    # 不是 11-06 的真实分。当日留档真实值为视源 66.4 / 天士力 63.5 / 海尔 55.4；
    # 83.4/73.1/66.8 系跨日混拼、已作废。

    def test_complete_factors_reuse_decision_score(self):
        score, missing = fc._decision_score({
            "industry": 80.0, "growth": 90.0, "valuation": 85.0,
            "technical": 75.0, "capital": 85.0, "score": 83.4,
        })
        assert score == 83.4 and missing == []

    def test_missing_valuation_no_score(self):
        score, missing = fc._decision_score({
            "industry": 80.0, "growth": 90.0, "valuation": None,
            "technical": 75.0, "capital": 85.0, "score": 66.4,
        })
        assert score is None            # 不拿 66.4（缺失按 0 折损后的数）当预判分
        assert missing == ["估值"]

    def test_zero_factor_is_not_missing(self):
        score, missing = fc._decision_score({
            "industry": 0.0, "growth": 0.0, "valuation": 0.0,
            "technical": 0.0, "capital": 0.0, "score": 0.0,
        })
        assert score == 0.0 and missing == []

    def test_live_prediction_missing_valuation_has_no_score(self, mkt):
        as_of = _seed_live_market(mkt, valuation=None, score=66.4)
        preds, _ = fc.build_stock_predictions_live(mkt, as_of)
        assert len(preds) == 1
        assert preds[0]["score"] is None
        assert "不出分" in preds[0]["strength"]
        assert "缺估值" in preds[0]["rationale"]

    def test_live_prediction_complete_uses_snapshot_score(self, mkt):
        as_of = _seed_live_market(mkt, valuation=85.0, score=83.4)
        preds, _ = fc.build_stock_predictions_live(mkt, as_of)
        assert preds[0]["score"] == 83.4
        assert "不出分" not in preds[0]["strength"]


# ============================================================
# 回溯统计
# ============================================================
class TestBacktestStats:
    def test_sample_label(self):
        assert "严重不足" in fb._sample_label(5)
        assert "偏小" in fb._sample_label(20)
        assert "初步结论" in fb._sample_label(40)

    def test_verdict_small_sample_no_conclusion(self):
        assert "不作结论" in fb._verdict(8, 8, 0.5)

    def test_verdict_close_to_random(self):
        assert "接近随机" in fb._verdict(50, 100, 0.5)

    def test_summarize_counts(self):
        rows = [
            {"scope": "market", "prediction": {"horizon": 1}, "status": "HIT"},
            {"scope": "market", "prediction": {"horizon": 1}, "status": "MISS"},
            {"scope": "market", "prediction": {"horizon": 1}, "status": "UNVERIFIABLE"},
            {"scope": "market", "prediction": {"horizon": 5}, "status": "HIT"},
        ]
        s = fb._summarize(rows, "market", "horizon", 1)
        assert s["hit"] == 1 and s["miss"] == 1 and s["unverifiable"] == 1
        assert s["n"] == 2 and s["rate"] == 50.0

    def test_market_baseline_frequency(self):
        rows = [
            {"scope": "market", "prediction": {"horizon": 1}, "status": "HIT", "actual_class": "上涨"},
            {"scope": "market", "prediction": {"horizon": 1}, "status": "HIT", "actual_class": "上涨"},
            {"scope": "market", "prediction": {"horizon": 1}, "status": "MISS", "actual_class": "下跌"},
            {"scope": "market", "prediction": {"horizon": 1}, "status": "MISS", "actual_class": "震荡"},
        ]
        b = fb._market_baseline(rows, 1)
        assert b["n"] == 4
        assert b["p_majority"] == pytest.approx(0.5)
        assert b["p_freq"] == pytest.approx((0.25 + 0.0625 + 0.0625))


# ============================================================
# 整月 0 命中归因 + 报告渲染
# ============================================================
class TestZeroHitAttribution:
    def test_direction_correct_ignores_breadth(self):
        assert fb._direction_correct("上涨", 2.49) is True
        assert fb._direction_correct("上涨", -1.50) is False
        assert fb._direction_correct("下跌", -0.82) is True
        assert fb._direction_correct("震荡", 0.20) is True
        assert fb._direction_correct("上涨", None) is None

    def test_zero_hit_month_split(self):
        """5 月 18 条全不中：5 条方向对（宽度否决）+ 13 条方向错。"""
        rows = []
        for i in range(18):
            ret = 2.49 if i < 5 else -1.50
            rows.append({
                "scope": "market", "status": "MISS", "actual_return": ret,
                "actual_class": "上涨" if ret > 0 else "下跌",
                "prediction": {"scope": "market", "horizon": 5, "direction": "上涨",
                               "predict_date": f"2026-05-{i + 1:02d}"},
            })
        seg = fb._zero_hit_segments(rows, 5)[0]
        assert seg["months"] == ["2026-05"]
        assert seg["n"] == 18 and seg["veto"] == 5 and seg["wrong"] == 13
        assert fb._month_direction_split(rows, "2026-05", 5) == (13, 5)

    def test_month_with_hits_not_a_segment(self):
        rows = [{
            "scope": "market", "status": "HIT", "actual_return": 2.0, "actual_class": "上涨",
            "prediction": {"scope": "market", "horizon": 5, "direction": "上涨",
                           "predict_date": "2026-09-01"},
        }]
        assert fb._zero_hit_segments(rows, 5) == []


class TestReportNoScore:
    def test_stock_row_without_score_renders_dash(self):
        row = {
            "scope": "stock", "target": "sh600690", "target_name": "海尔智家",
            "direction": "上涨", "target_date": "2026-11-13", "score": None,
            "statement": "个股 海尔智家 上涨", "rationale": "估值 —（缺失）",
        }
        md = fc.render_forecast_markdown([row], "2026-11-06", "2026-11-09")
        assert "五因子加权分" in md
        assert "| 海尔智家（sh600690） | — | 上涨 |" in md
        assert "不用 0 分占位" in md

    def test_complete_stock_row_renders_score(self):
        row = {
            "scope": "stock", "target": "sh600690", "target_name": "海尔智家",
            "direction": "上涨", "target_date": "2026-11-13", "score": 83.4,
            "statement": "个股 海尔智家 上涨", "rationale": "五因子加权分 83.4",
        }
        md = fc.render_forecast_markdown([row], "2026-11-06", "2026-11-09")
        assert "| 海尔智家（sh600690） | 83.4 | 上涨 |" in md
