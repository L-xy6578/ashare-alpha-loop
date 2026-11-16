"""宏观仓位提示（只读观察层）测试：打分档位、系数映射、缺失降级、落库幂等、回填、不写产物"""

import sqlite3

import pytest

import r47_closed_loop as r47
from src import macro_regime as mr

ASOF = "2026-11-10"


def _bundle(pmi=49.3, cpi=0.7, ppi=5.7, m2=10.6, lpr=(3.0, 3.0), pmi_date="2026-10-01"):
    return {
        "pmi": {"value": pmi, "date": pmi_date, "prev": None},
        "cpi": {"value": cpi, "date": "2026-10-01", "prev": None},
        "ppi": {"value": ppi, "date": "2026-10-01", "prev": None},
        "m2": {"value": m2, "date": "2026-09-01", "prev": None},
        "lpr": {"value": lpr[0], "date": "2026-10-20", "prev": lpr[1],
                "trend": "down" if lpr[0] < lpr[1] else ("up" if lpr[0] > lpr[1] else "flat")},
    }


# ---------- 打分规则（逐档验证 SKILL.md 2.2） ----------

@pytest.mark.parametrize("v,score", [
    (52.0, 20.0),    # >51 → 18-20（封顶 20）
    (51.0, 18.0),
    (50.5, 16.5),    # >50 → 15-18
    (49.3, 10.6),    # 48-50 → 8-12
    (48.0, 8.0),
    (46.0, 4.0),     # <48 → 0-7
])
def test_score_pmi(v, score):
    assert mr.score_pmi(v) == pytest.approx(score, abs=0.01)


@pytest.mark.parametrize("v,score", [
    (2.5, 16.5),     # 2-3% 最优
    (0.7, 10.0),     # 0-2% 原文空档 → 保守 10
    (3.0, 10.0),     # >=3 先命中空档
    (3.5, 10.0),     # 3-4% 原文空档 → 保守 10
    (4.0, 10.0),     # >4% 起点
    (5.0, 7.5),      # >4% → 5-10
    (-1.0, 8.33),    # <0% 通缩
])
def test_score_cpi(v, score):
    expect = 10.0 if 3 <= v < 4 else score
    assert mr.score_cpi(v) == pytest.approx(expect, abs=0.02)


@pytest.mark.parametrize("v,score", [
    (1.0, 13.0),     # 0-3% → 12-15
    (3.0, 12.0 + 3.0),
    (4.0, 10.0),     # 3-5% 原文空档 → 保守 10
    (5.0, 10.0),     # >5% 起点
    (5.7, 8.83),     # >5% → 5-10
    (-1.0, 8.33),    # <0%
])
def test_score_ppi(v, score):
    expect = 10.0 if 3 <= v <= 5 else score
    assert mr.score_ppi(v) == pytest.approx(expect, abs=0.02)


@pytest.mark.parametrize("v,score", [
    (10.6, 15.36),   # >10% → 15-18
    (9.0, 12.0),     # 8-10% → 10-14
    (8.0, 10.0),
    (7.0, 7.67),     # <8% → 5-9
])
def test_score_m2(v, score):
    assert mr.score_m2(v) == pytest.approx(score, abs=0.02)


@pytest.mark.parametrize("trend,score", [
    ("down", 16.5), ("flat", 12.0), ("up", 7.0), ("unknown", 12.0),
])
def test_score_lpr(trend, score):
    assert mr.score_lpr(trend) == score


# ---------- 评分 → 档位 → 系数（含 100% 档不放大） ----------

@pytest.mark.parametrize("score,level,ratio", [
    (100.0, "进攻", 1.0), (80.0, "进攻", 1.0),
    (79.9, "乐观", 0.8), (65.0, "乐观", 0.8),
    (64.9, "中性", 0.5), (50.0, "中性", 0.5),
    (49.9, "谨慎", 0.3), (35.0, "谨慎", 0.3),
    (34.9, "防御", 0.1), (0.0, "防御", 0.1),
])
def test_level_of(score, level, ratio):
    assert mr.level_of(score) == (level, ratio)


def test_suggest_cap_never_exceeds_18000():
    """100% 档乘的是 18,000 而不是 30,000：宏观只给相对系数，不覆盖硬上限"""
    row = mr.assess(_bundle(pmi=53.0, cpi=2.5, ppi=2.0, m2=12.0, lpr=(3.0, 3.5)), ASOF)
    assert row["level"] == "进攻" and row["cap_ratio"] == 1.0
    assert row["suggest_cap"] == mr.TOTAL_CAP == 18000


def test_assess_today_live_values():
    """用 2026-11-10 实测值复算：58.6 分 → 中性 → 9,000 元"""
    row = mr.assess(_bundle(), ASOF)
    assert row["weight_covered"] == 80, "社融恒缺，覆盖权重应为 80"
    assert row["score"] == pytest.approx(58.6, abs=0.05)
    assert row["level"] == "中性" and row["cap_ratio"] == 0.5
    assert row["suggest_cap"] == 9000
    assert row["sfe"] is None and "社融" in row["missing"][0]


def test_sfe_always_missing_even_if_bundle_has_it():
    """社融无数据源：即使 provider 塞了值也不采信，缺失说明不许消失"""
    b = _bundle()
    b["sfe"] = {"value": 9.5, "date": "2026-09-01"}
    row = mr.assess(b, ASOF)
    assert row["sfe"] is None
    assert any("社融" in m for m in row["missing"])


def test_assess_degrades_when_too_many_missing():
    """缺失 ≥4 项（含社融）→ 数据不足，不给档位/不给建议上限"""
    b = _bundle()
    b["cpi"] = {}
    b["ppi"] = {}
    b["m2"] = {}
    row = mr.assess(b, ASOF)
    assert row["level"] == mr.LEVEL_MISSING
    assert row["score"] is None and row["suggest_cap"] is None
    assert len(row["missing"]) == 4


def test_assess_three_missing_still_scores():
    """缺失 3 项（含社融）→ 仍按剩余两项折算，但权重覆盖低"""
    b = _bundle()
    b["cpi"] = {}
    b["ppi"] = {}
    row = mr.assess(b, ASOF)
    assert row["score"] is not None
    assert row["weight_covered"] == 20 + 20 + 15
    assert set(row["missing"]) >= {"CPI", "PPI"}


# ---------- 落库 ----------

def test_save_rows_idempotent(tmp_path):
    db = str(tmp_path / "market.db")
    row = mr.assess(_bundle(), ASOF)
    assert mr.save_rows([row], db_path=db, fetched_at="2026-11-10 15:35:00") == 1
    mr.save_rows([mr.assess(_bundle(m2=12.0), ASOF)], db_path=db,
                 fetched_at="2026-11-10 15:40:00")
    with sqlite3.connect(db) as conn:
        got = conn.execute(
            "SELECT date, score, level, cap_ratio, suggest_cap, m2, sfe, "
            "weight_covered, missing, fetched_at FROM macro_regime").fetchall()
    assert len(got) == 1, "同一判定日必须覆盖，不得追加"
    assert got[0][0] == ASOF and got[0][3] == 0.5 and got[0][4] == 9000
    assert got[0][5] == 12.0, "覆盖后应写入最新值"
    assert got[0][6] is None and "社融" in got[0][8]
    assert got[0][9] == "2026-11-10 15:40:00"


def test_backfill_returns(tmp_path):
    db = str(tmp_path / "market.db")
    mr.save_rows([mr.assess(_bundle(), "2026-11-03")], db_path=db)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE daily_bars (symbol TEXT, date TEXT, close REAL)")
        # 6 根基准K线：判定日 + 5 日后可算；10/20 日仍为 NULL
        conn.executemany("INSERT INTO daily_bars VALUES (?,?,?)", [
            (mr.BENCH, "2026-11-03", 4000.0),
            (mr.BENCH, "2026-11-04", 4020.0),
            (mr.BENCH, "2026-11-05", 4040.0),
            (mr.BENCH, "2026-11-06", 4060.0),
            (mr.BENCH, "2026-11-09", 4080.0),
            (mr.BENCH, "2026-11-10", 4100.0),
        ])
        conn.commit()
    assert mr.backfill_returns(db) == 1
    with sqlite3.connect(db) as conn:
        got = conn.execute(
            "SELECT bench_close, ret_5d, ret_10d, ret_20d FROM macro_regime").fetchone()
    assert got[0] == 4000.0
    assert got[1] == pytest.approx(0.025, abs=1e-4)   # 4100/4000-1
    assert got[2] is None and got[3] is None, "后向数据不足时不得编造收益"


def test_backfill_returns_no_bench_bars(tmp_path):
    db = str(tmp_path / "market.db")
    mr.save_rows([mr.assess(_bundle(), ASOF)], db_path=db)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE daily_bars (symbol TEXT, date TEXT, close REAL)")
        conn.commit()
    assert mr.backfill_returns(db) == 0


# ---------- 报表与运行 ----------

def test_render_markdown_marks_prompt_only_and_sources():
    text = mr.render_markdown(ASOF, [mr.assess(_bundle(), ASOF)])
    assert "仅提示，不影响决策" in text
    assert "18,000" in text and "9,000" in text
    assert "## 三、数据来源与缺失说明" in text
    assert "社融" in text and "未用其他指标顶替" in text
    assert "58.6" in text and "中性" in text


def test_render_markdown_missing_row():
    row = mr.assess(_bundle(pmi=None), ASOF)
    row.update({"score": None, "level": mr.LEVEL_MISSING, "suggest_cap": None})
    text = mr.render_markdown(ASOF, [row])
    assert "数据不足" in text


def test_run_dry_writes_nothing(tmp_path):
    out = tmp_path / "收盘报表"
    res = mr.run(ASOF, is_dry=True, provider=lambda a: _bundle(),
                 market_db=str(tmp_path / "market.db"), out_dir=out)
    assert res["written"] == 0 and res["path"] is None
    assert not (tmp_path / "market.db").exists(), "dry-run 不应建表/落库"
    assert not out.exists(), "dry-run 不应写报表"


def test_run_real_writes_db_and_report(tmp_path):
    out = tmp_path / "收盘报表"
    db = str(tmp_path / "market.db")
    res = mr.run(ASOF, is_dry=False, provider=lambda a: _bundle(),
                 market_db=db, out_dir=out)
    assert res["path"] == out / f"宏观仓位提示_{ASOF}.md"
    assert res["path"].exists()
    text = res["path"].read_text(encoding="utf-8")
    assert "中性" in text and "数据来源与缺失说明" in text
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM macro_regime").fetchone()[0] == 1


# ---------- 收盘闭环接入 ----------

def test_r47_step_dry_run_skips_without_provider(monkeypatch):
    def boom(*a, **kw):
        pytest.fail("dry-run 未注入 provider 时不应访问宏观接口")

    monkeypatch.setattr(mr, "fetch_bundle", boom)
    warnings: list[str] = []
    r47._run_macro_regime(True, warnings)
    assert warnings == []


def test_r47_step_failure_only_warns(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("接口异常")

    monkeypatch.setattr(mr, "run", boom)
    warnings: list[str] = []
    r47._run_macro_regime(False, warnings, provider=lambda a: _bundle())
    assert any("宏观仓位提示失败" in w for w in warnings)


def test_r47_step_dry_with_provider_prints_only(tmp_path, monkeypatch, capsys):
    def boom(*a, **kw):
        pytest.fail("dry-run 不应写库/写报表")

    monkeypatch.setattr(mr, "save_rows", boom)
    monkeypatch.setattr(mr, "write_report", boom)
    warnings: list[str] = []
    r47._run_macro_regime(True, warnings, provider=lambda a: _bundle())
    out = capsys.readouterr().out
    assert "宏观评分 58.6/100" in out and "中性" in out and "9,000" in out
    assert warnings == []
