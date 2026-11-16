"""游资提示（只读观察层）测试：H-01/02/04/05/06/08/09 判定、缺失、落库幂等、不写产物"""

import sqlite3

import pytest

import r47_closed_loop as r47
from src import hot_money_watch as hm
from src.watchlist import WatchItem

ASOF = "2026-11-10"


def _flow(*rows):
    """rows: (date, main_wan, dade_wan)"""
    return [{"date": d, "main_wan": m, "dade_wan": g} for d, m, g in rows]


def _bundle(flow=None, quote=None, margin=None, lhb=None):
    return {"flow": flow or [], "quote": quote or {}, "margin": margin or {}, "lhb": lhb or {}}


def _item(symbol="sh600535", name="天士力"):
    return WatchItem(symbol=symbol, code=symbol[2:], name=name)


def test_no_signal_when_all_clean():
    b = _bundle(flow=_flow(("2026-11-06", 100.0, 50.0), ("2026-11-07", 80.0, 60.0),
                          ("2026-11-09", 90.0, 70.0)),
                quote={"name": "天士力", "turnover_pct": 1.2, "float_cap_yi": 211.0},
                margin={"date": "2026-11-09", "rqyl_wan": 10.0, "rq_ratio_pct": 0.1,
                        "rzye_ratio_pct": 2.0},
                lhb={"latest_date": "", "days_30d": 0})
    row = hm.assess("sh600535", "天士力", b, ASOF)
    assert row["signals"] == [] and row["risk_level"] == hm.LEVEL_NONE
    assert row["missing"] == []


def test_h01_requires_three_consecutive_days():
    """近3日每日净流出>1亿才触发；只有2天不触发"""
    three = _bundle(flow=_flow(("2026-11-06", -12000.0, 0), ("2026-11-07", -11000.0, 0),
                               ("2026-11-09", -10500.0, 0)),
                    quote={"turnover_pct": 1.0, "float_cap_yi": 200.0},
                    margin={"date": "2026-11-09", "rqyl_wan": 1.0, "rq_ratio_pct": 0.1,
                            "rzye_ratio_pct": 1.0})
    row = hm.assess("sh600535", "天士力", three, ASOF)
    assert "H-01" in row["signals"] and row["risk_level"] == hm.LEVEL_HIGH
    assert row["main_out1e_days"] == 3

    two = _bundle(flow=_flow(("2026-11-06", -12000.0, 0), ("2026-11-07", -9999.0, 0),
                             ("2026-11-09", -10500.0, 0)),
                  quote={"turnover_pct": 1.0, "float_cap_yi": 200.0},
                  margin={"date": "2026-11-09", "rqyl_wan": 1.0, "rq_ratio_pct": 0.1,
                          "rzye_ratio_pct": 1.0})
    assert "H-01" not in hm.assess("sh600535", "天士力", two, ASOF)["signals"]


def test_h01_missing_when_less_than_three_days():
    b = _bundle(flow=_flow(("2026-11-09", -20000.0, 0)),
                quote={"turnover_pct": 1.0, "float_cap_yi": 200.0})
    row = hm.assess("sh600535", "天士力", b, ASOF)
    assert "主力资金近3日" in row["missing"]
    assert "H-01" not in row["signals"]


def test_h05_dade_panic_ratio():
    """大单净流出 5 亿 / 流通市值 200 亿 = -2.5% → 触发"""
    b = _bundle(flow=_flow(("2026-11-06", 0, 0), ("2026-11-07", 0, 0), ("2026-11-09", 0, -50000.0)),
                quote={"turnover_pct": 1.0, "float_cap_yi": 200.0},
                margin={"date": "2026-11-09", "rqyl_wan": 1.0, "rq_ratio_pct": 0.1,
                        "rzye_ratio_pct": 1.0})
    row = hm.assess("sh600535", "天士力", b, ASOF)
    assert row["dade_ratio_pct"] == -2.5
    assert "H-05" in row["signals"] and row["risk_level"] == hm.LEVEL_HIGH


def test_h05_missing_cap():
    b = _bundle(flow=_flow(("2026-11-06", 0, 0), ("2026-11-07", 0, 0), ("2026-11-09", 0, -90000.0)),
                quote={}, margin={})
    row = hm.assess("sh600535", "天士力", b, ASOF)
    assert "流通市值（大单砸盘分母）" in row["missing"]
    assert "H-05" not in row["signals"]


def test_h06_turnover():
    b = _bundle(flow=_flow(("2026-11-06", 0, 0), ("2026-11-07", 0, 0), ("2026-11-09", 0, 0)),
                quote={"turnover_pct": 25.5, "float_cap_yi": 100.0},
                margin={"date": "2026-11-09", "rqyl_wan": 1.0, "rq_ratio_pct": 0.1,
                        "rzye_ratio_pct": 1.0})
    row = hm.assess("sh600535", "天士力", b, ASOF)
    assert row["signals"] == ["H-06"] and row["risk_level"] == hm.LEVEL_MID


def test_h02_h04_lhb_seats():
    b = _bundle(flow=_flow(("2026-11-06", 0, 0), ("2026-11-07", 0, 0), ("2026-11-09", 0, 0)),
                quote={"turnover_pct": 1.0, "float_cap_yi": 100.0},
                margin={"date": "2026-11-09", "rqyl_wan": 1.0, "rq_ratio_pct": 0.1,
                        "rzye_ratio_pct": 1.0},
                lhb={"latest_date": "2026-11-09", "days_30d": 2,
                     "sell_buy_ratio": 2.4, "inst_sell_wan": 6200.0})
    row = hm.assess("sh600535", "天士力", b, ASOF)
    assert row["signals"] == ["H-02", "H-04"]
    assert row["risk_level"] == hm.LEVEL_HIGH   # H-02 为🔴高风险


def test_h02_not_triggered_at_exactly_two():
    b = _bundle(flow=_flow(("2026-11-06", 0, 0), ("2026-11-07", 0, 0), ("2026-11-09", 0, 0)),
                quote={"turnover_pct": 1.0, "float_cap_yi": 100.0},
                margin={"date": "2026-11-09", "rqyl_wan": 1.0, "rq_ratio_pct": 0.1,
                        "rzye_ratio_pct": 1.0},
                lhb={"latest_date": "2026-11-09", "days_30d": 1,
                     "sell_buy_ratio": 2.0, "inst_sell_wan": 5000.0})
    assert hm.assess("sh600535", "天士力", b, ASOF)["signals"] == []


@pytest.mark.parametrize("rqyl,rq_ratio,hit", [
    (60.0, 0.04, True),    # 只靠 50 万股腿触发（天士力 600800 股案例）
    (10.0, 1.5, True),     # 只靠占流通市值 1% 腿触发
    (10.0, 0.5, False),
])
def test_h08_margin_short(rqyl, rq_ratio, hit):
    b = _bundle(flow=_flow(("2026-11-06", 0, 0), ("2026-11-07", 0, 0), ("2026-11-09", 0, 0)),
                quote={"turnover_pct": 1.0, "float_cap_yi": 211.0},
                margin={"date": "2026-11-09", "rqyl_wan": rqyl, "rq_ratio_pct": rq_ratio,
                        "rzye_ratio_pct": 1.0})
    assert ("H-08" in hm.assess("sh600535", "天士力", b, ASOF)["signals"]) is hit


def test_h09_margin_finance_ratio():
    b = _bundle(flow=_flow(("2026-11-06", 0, 0), ("2026-11-07", 0, 0), ("2026-11-09", 0, 0)),
                quote={"turnover_pct": 1.0, "float_cap_yi": 100.0},
                margin={"date": "2026-11-09", "rqyl_wan": 1.0, "rq_ratio_pct": 0.1,
                        "rzye_ratio_pct": 3.01})
    row = hm.assess("sh600535", "天士力", b, ASOF)
    assert row["signals"] == ["H-09"] and row["risk_level"] == hm.LEVEL_MID


def test_missing_margin_marks_not_risky():
    b = _bundle(flow=_flow(("2026-11-06", 0, 0), ("2026-11-07", 0, 0), ("2026-11-09", 0, 0)),
                quote={"turnover_pct": 1.0, "float_cap_yi": 100.0}, margin={})
    row = hm.assess("sh600535", "天士力", b, ASOF)
    assert "融资融券" in row["missing"] and row["risk_level"] == hm.LEVEL_MISSING


def test_fetch_flow_parses_and_filters(monkeypatch):
    payload = {"data": {"klines": [
        "2026-11-09,-12526050.0,3256612.0,9269438.0,-13766035.0,1239985.0",
        "2026-11-10,100.0,0,0,50.0,60.0",
    ]}}
    monkeypatch.setattr(hm, "fetch_json", lambda url, **kw: payload)
    rows = hm.fetch_flow("sh600535", "2026-11-09")
    assert len(rows) == 1, "超过 asof 的行必须剔除"
    assert rows[0]["date"] == "2026-11-09"
    assert rows[0]["main_wan"] == pytest.approx(-1252.605)
    assert rows[0]["dade_wan"] == pytest.approx((-13766035.0 + 1239985.0) / 1e4)


def test_fetch_margin_parses(monkeypatch):
    payload = {"result": {"data": [{
        "DATE": "2026-11-09 00:00:00", "RQYL": 600800, "RQYE": 8585432,
        "RZYEZB": 2.85364541, "SZ": 21348545571.45}]}}
    monkeypatch.setattr(hm, "fetch_json", lambda url, **kw: payload)
    m = hm.fetch_margin("sh600535", ASOF)
    assert m["date"] == "2026-11-09" and m["rqyl_wan"] == 60.08
    assert m["rq_ratio_pct"] == pytest.approx(0.0402, abs=1e-3)
    assert m["rzye_ratio_pct"] == pytest.approx(2.854, abs=1e-3)


def test_fetch_lhb_computes_ratios(monkeypatch):
    buy = [{"TRADE_DATE": "2026-11-09 00:00:00", "OPERATEDEPT_NAME": "甲营业部", "BUY": 1e8},
           {"TRADE_DATE": "2026-11-09 00:00:00", "OPERATEDEPT_NAME": "乙营业部", "BUY": 1e8}]
    sell = [{"TRADE_DATE": "2026-11-09 00:00:00", "OPERATEDEPT_NAME": "机构专用", "SELL": 6e7},
            {"TRADE_DATE": "2026-11-09 00:00:00", "OPERATEDEPT_NAME": "丙营业部", "SELL": 4.6e8}]

    def fake(url, **kw):
        return {"result": {"data": buy if "DETAILSBUY" in url else sell}}

    monkeypatch.setattr(hm, "fetch_json", fake)
    r = hm.fetch_lhb("sz002639", "2026-10-11", ASOF)
    assert r["latest_date"] == "2026-11-09" and r["days_30d"] == 1
    assert r["sell_buy_ratio"] == pytest.approx(2.6)
    assert r["inst_sell_wan"] == pytest.approx(6000.0)


def test_fetch_lhb_empty_means_not_listed(monkeypatch):
    monkeypatch.setattr(hm, "fetch_json", lambda url, **kw: {"result": None})
    r = hm.fetch_lhb("sh600535", "2026-10-11", ASOF)
    assert r == {"latest_date": "", "days_30d": 0, "sell_buy_ratio": None,
                 "inst_sell_wan": None}


def test_collect_failure_marks_insufficient():
    def boom(symbol, asof):
        raise OSError("getaddrinfo failed")

    warnings: list[str] = []
    rows = hm.collect([_item()], ASOF, provider=boom, warnings=warnings)
    assert rows[0]["risk_level"] == hm.LEVEL_MISSING
    assert rows[0]["signals"] == []
    assert any("游资数据缺失" in w for w in warnings)


def test_save_rows_idempotent(tmp_path):
    db = str(tmp_path / "market.db")
    b = _bundle(flow=_flow(("2026-11-06", 0, 0), ("2026-11-07", 0, 0), ("2026-11-09", 0, 0)),
                quote={"name": "天士力", "turnover_pct": 1.0, "float_cap_yi": 100.0},
                margin={"date": "2026-11-09", "rqyl_wan": 60.0, "rq_ratio_pct": 0.04,
                        "rzye_ratio_pct": 2.8})
    rows = hm.collect([_item()], ASOF, provider=lambda s, a: b)
    hm.save_rows(rows, ASOF, db_path=db, fetched_at="2026-11-10 15:35:00")
    hm.save_rows(rows, ASOF, db_path=db, fetched_at="2026-11-10 15:40:00")
    with sqlite3.connect(db) as conn:
        got = conn.execute("SELECT date, symbol, signals, risk_level, rqyl_wan, fetched_at "
                           "FROM hotmoney_watch").fetchall()
    assert len(got) == 1, "同日同标的必须覆盖"
    assert got[0][0] == ASOF and got[0][2] == "H-08" and got[0][3] == "高"
    assert got[0][4] == 60.0 and got[0][5] == "2026-11-10 15:40:00"


def test_render_markdown_states_prompt_only_and_unsupported():
    b = _bundle(flow=_flow(("2026-11-06", 0, 0), ("2026-11-07", 0, 0), ("2026-11-09", 0, 0)),
                quote={"turnover_pct": 1.0, "float_cap_yi": 100.0},
                margin={"date": "2026-11-09", "rqyl_wan": 60.0, "rq_ratio_pct": 0.04,
                        "rzye_ratio_pct": 2.8})
    rows = [hm.assess("sh600535", "天士力", b, ASOF)]
    text = hm.render_markdown(ASOF, rows, fetched_at="2026-11-10 15:35:00")
    assert "仅提示，不影响决策" in text
    assert "H-03" in text and "不可实现" not in text.split("未实现")[0]
    assert "是没检查" in text            # 明确区分「未检查」与「已检查未触发」
    assert "天士力" in text and "60.00" in text


def test_run_dry_writes_nothing(tmp_path):
    out = tmp_path / "收盘报表"
    res = hm.run([_item()], ASOF, is_dry=True, provider=lambda s, a: _bundle(),
                 market_db=str(tmp_path / "market.db"), out_dir=out)
    assert res["written"] == 0 and res["path"] is None
    assert not (tmp_path / "market.db").exists()
    assert not out.exists()


def test_run_real_writes_db_and_report(tmp_path):
    out = tmp_path / "收盘报表"
    db = str(tmp_path / "market.db")
    b = _bundle(flow=_flow(("2026-11-06", 0, 0), ("2026-11-07", 0, 0), ("2026-11-09", 0, 0)),
                quote={"turnover_pct": 21.0, "float_cap_yi": 100.0},
                margin={"date": "2026-11-09", "rqyl_wan": 1.0, "rq_ratio_pct": 0.1,
                        "rzye_ratio_pct": 1.0})
    res = hm.run([_item()], ASOF, is_dry=False, provider=lambda s, a: b,
                 market_db=db, out_dir=out)
    assert res["path"] == out / f"游资提示_{ASOF}.md" and res["path"].exists()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM hotmoney_watch").fetchone()[0] == 1


def test_r47_step_dry_run_skips_without_provider(monkeypatch):
    monkeypatch.setattr(hm, "collect", lambda *a, **kw: pytest.fail("不应访问游资接口"))
    warnings: list[str] = []
    r47._run_hotmoney_watch([_item()], True, warnings)
    assert warnings == []


def test_r47_step_failure_only_warns(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("接口异常")

    monkeypatch.setattr(hm, "run", boom)
    warnings: list[str] = []
    r47._run_hotmoney_watch([_item()], False, warnings, provider=lambda s, a: _bundle())
    assert any("游资提示失败" in w for w in warnings)


def test_r47_step_dry_with_provider_prints_only(monkeypatch, capsys):
    monkeypatch.setattr(hm, "save_rows", lambda *a, **kw: pytest.fail("dry-run 不应落库"))
    monkeypatch.setattr(hm, "write_report", lambda *a, **kw: pytest.fail("dry-run 不应写报表"))
    warnings: list[str] = []
    b = _bundle(flow=_flow(("2026-11-06", 0, 0), ("2026-11-07", 0, 0), ("2026-11-09", 0, 0)),
                quote={"turnover_pct": 21.0, "float_cap_yi": 100.0},
                margin={"date": "2026-11-09", "rqyl_wan": 1.0, "rq_ratio_pct": 0.1,
                        "rzye_ratio_pct": 1.0})
    r47._run_hotmoney_watch([_item()], True, warnings, provider=lambda s, a: b)
    out = capsys.readouterr().out
    assert "触发风险信号 1 只" in out
    assert warnings == []
