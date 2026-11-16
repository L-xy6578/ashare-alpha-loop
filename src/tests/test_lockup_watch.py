"""解禁提示（只读观察层）测试：分档、信号、缺失、落库幂等、不写产物"""

import sqlite3

import pytest

import r47_closed_loop as r47
from src import lockup_watch as lw
from src.watchlist import WatchItem

ASOF = "2026-11-10"


def _ev(day, ratio=5.0, shares=100.0, cap=1000.0, stype="定向增发机构配售股份"):
    return {"date": day, "shares_wan": shares, "market_cap_wan": cap,
            "ratio_pct": ratio, "share_type": stype, "holders": 1}


def _item(symbol="sh600535", name="天士力"):
    return WatchItem(symbol=symbol, code=lw.code_of(symbol), name=name)


def test_code_of():
    assert lw.code_of("sh600535") == "600535"
    assert lw.code_of("sz002366") == "002366"
    assert lw.code_of("600535") == "600535"


@pytest.mark.parametrize("events,level", [
    ([], "无"),
    ([_ev("2026-11-20", 3.9)], "低"),
    ([_ev("2026-11-20", 10.0)], "中"),
    ([_ev("2026-11-20", 20.0)], "高"),
    ([_ev("2026-11-20", 40.0)], "极高"),
    # 30 日内两次解禁累计 12%（5+7）→ 按累计比例落"中"档
    ([_ev("2026-11-20", 5.0), _ev("2026-11-25", 7.0)], "中"),
])
def test_assess_level_buckets(events, level):
    """分档阈值照 SKILL.md：<5 低 / 5-15 中 / 15-30 高 / >30 极高"""
    row = lw.assess("sh600535", "天士力", events, ASOF)
    assert row["risk_level"] == level
    assert row["has_upcoming"] is (level != "无")


def test_event_outside_horizon_is_not_upcoming():
    row = lw.assess("sh600535", "天士力", [_ev("2026-12-31", 50.0)], ASOF)
    assert row["has_upcoming"] is False
    assert row["risk_level"] == "无"
    assert row["signals"] == []


def test_signals_r01_r02():
    row = lw.assess("sh600535", "天士力", [_ev("2026-11-20", 3.0)], ASOF)
    assert row["signals"] == ["R-01"]
    row = lw.assess("sh600535", "天士力", [_ev("2026-11-20", 20.0)], ASOF)
    assert "R-02" in row["signals"]


def test_signal_r07_stack_within_6m():
    """R-07：6 个月内 ≥2 次且累计 >30%（两次都在 30 日窗口之外，排除 R-01/R-02）"""
    events = [_ev("2026-12-25", 20.0), _ev("2027-01-25", 15.0)]
    row = lw.assess("sh600535", "天士力", events, ASOF)
    assert row["has_upcoming"] is False
    assert row["signals"] == ["R-07"]
    assert row["stack_count"] == 2 and row["stack_ratio_pct"] == 35.0


def test_past_events_ignored():
    row = lw.assess("sh600535", "天士力", [_ev("2026-10-01", 90.0)], ASOF)
    assert row["has_upcoming"] is False and row["risk_level"] == "无"


def test_fetch_events_parses_and_sorts(monkeypatch):
    payload = {"result": {"pages": 1, "data": [
        {"FREE_DATE": "2026-12-01 00:00:00", "CURRENT_FREE_SHARES": 731.0222,
         "LIFT_MARKET_CAP": 18034.317674, "FREE_RATIO": 0.038724,
         "FREE_SHARES_TYPE": "定向增发机构配售股份", "BATCH_HOLDER_NUM": 2},
        {"FREE_DATE": "2026-11-20 00:00:00", "CURRENT_FREE_SHARES": 100.0,
         "LIFT_MARKET_CAP": 1000.0, "FREE_RATIO": 0.01,
         "FREE_SHARES_TYPE": "股权激励限售股份", "BATCH_HOLDER_NUM": 1},
    ]}}

    def fake_fetch_json(url, **kwargs):
        assert "RPT_LIFT_STAGE" in url
        return payload

    monkeypatch.setattr(lw, "fetch_json", fake_fetch_json)
    events = lw.fetch_events("sh688226", ASOF, "2027-05-09")
    assert [e["date"] for e in events] == ["2026-11-20", "2026-12-01"]
    assert events[1]["ratio_pct"] == pytest.approx(3.8724, abs=1e-3)
    assert events[1]["share_type"] == "定向增发机构配售股份"


def test_fetch_events_empty_result_is_no_event(monkeypatch):
    monkeypatch.setattr(lw, "fetch_json",
                        lambda url, **kw: {"code": 9201, "message": "返回数据为空",
                                           "result": None})
    assert lw.fetch_events("sh600535", ASOF, "2027-05-09") == []


def test_collect_missing_on_error():
    """单只失败 → 数据缺失档（不得判低风险），并记告警"""
    def boom(symbol, start, end):
        raise OSError("getaddrinfo failed")

    warnings: list[str] = []
    rows = lw.collect([_item()], ASOF, provider=boom, warnings=warnings)
    assert rows[0]["risk_level"] == "数据缺失"
    assert rows[0]["has_upcoming"] is None
    assert rows[0]["signals"] == []
    assert any("解禁数据缺失" in w for w in warnings)


def test_save_rows_idempotent(tmp_path):
    db = str(tmp_path / "market.db")
    rows = lw.collect([_item()], ASOF, provider=lambda s, a, b: [_ev("2026-11-20", 20.0)])
    assert lw.save_rows(rows, ASOF, db_path=db, fetched_at="2026-11-10 15:35:00") == 1
    lw.save_rows(rows, ASOF, db_path=db, fetched_at="2026-11-10 15:40:00")
    with sqlite3.connect(db) as conn:
        got = conn.execute(
            "SELECT date, symbol, has_upcoming, sum_ratio_pct, risk_level, signals, fetched_at "
            "FROM lockup_watch").fetchall()
    assert len(got) == 1, "同日同标的必须覆盖，不得追加"
    assert got[0][0] == ASOF and got[0][2] == 1
    assert got[0][3] == 20.0 and got[0][4] == "高"
    assert got[0][5] == "R-01,R-02"
    assert got[0][6] == "2026-11-10 15:40:00"


def test_render_markdown_marks_prompt_only():
    rows = [
        lw.assess("sh688226", "威腾电气", [_ev("2026-12-01", 3.87)], ASOF),
        lw.assess("sh600019", "宝钢股份", [], ASOF),
        lw.missing("sz002366", "融发核电", "OSError: boom"),
    ]
    text = lw.render_markdown(ASOF, rows, fetched_at="2026-11-10 15:35:00")
    assert "仅提示，不影响决策" in text
    assert "威腾电气" in text and "3.87" in text
    assert "数据缺失" in text and "[数据缺失: 解禁规模]" in text
    assert "R-01" in text


def test_run_dry_writes_nothing(tmp_path):
    out = tmp_path / "收盘报表"
    res = lw.run([_item()], ASOF, is_dry=True, provider=lambda s, a, b: [],
                 market_db=str(tmp_path / "market.db"), out_dir=out)
    assert res["written"] == 0 and res["path"] is None
    assert not (tmp_path / "market.db").exists(), "dry-run 不应建表/落库"
    assert not out.exists(), "dry-run 不应写报表"


def test_run_real_writes_db_and_report(tmp_path):
    out = tmp_path / "收盘报表"
    db = str(tmp_path / "market.db")
    res = lw.run([_item()], ASOF, is_dry=False,
                 provider=lambda s, a, b: [_ev("2026-11-20", 20.0)],
                 market_db=db, out_dir=out)
    assert res["path"] == out / f"解禁提示_{ASOF}.md"
    assert res["path"].exists()
    assert "天士力" in res["path"].read_text(encoding="utf-8")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM lockup_watch").fetchone()[0] == 1


def test_r47_step_dry_run_skips_without_provider(monkeypatch):
    def boom(*a, **kw):
        pytest.fail("dry-run 未注入 provider 时不应访问解禁接口")

    monkeypatch.setattr(lw, "collect", boom)
    warnings: list[str] = []
    r47._run_lockup_watch([_item()], True, warnings)
    assert warnings == []


def test_r47_step_failure_only_warns(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("接口异常")

    monkeypatch.setattr(lw, "run", boom)
    warnings: list[str] = []
    r47._run_lockup_watch([_item()], False, warnings, provider=lambda s, a, b: [])
    assert any("解禁提示失败" in w for w in warnings)


def test_r47_step_dry_with_provider_prints_only(tmp_path, monkeypatch, capsys):
    def boom(*a, **kw):
        pytest.fail("dry-run 不应写库/写报表")

    monkeypatch.setattr(lw, "save_rows", boom)
    monkeypatch.setattr(lw, "write_report", boom)
    warnings: list[str] = []
    r47._run_lockup_watch([_item()], True, warnings,
                          provider=lambda s, a, b: [_ev("2026-11-20", 20.0)])
    out = capsys.readouterr().out
    assert "未来 30 日内有解禁 1 只" in out
    assert warnings == []
