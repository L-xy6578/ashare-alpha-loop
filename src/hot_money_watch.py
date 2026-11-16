"""
游资提示（只读观察层 · 不参与任何决策）

背景：`morning_flow.py --lhb` 每天采龙虎榜但只出简报、无阈值判定；
游资风险信号检查（H-01~H-10）在本模块中落地。
本模块只把能从公开接口拿到、定义明确的那几条做成"提示"：

| 信号 | 结论 | 数据源 |
|---|---|---|
| H-01 主力资金连续流出（近3日每日净流出>1亿） | 可做 | 东财个股资金流日线 |
| H-02 龙虎榜卖出席位为主 | 可做（金额口径，见下） | 东财龙虎榜买/卖席位明细 |
| H-03 知名游资出货 | **不可实现**（无游资席位知识库） | — |
| H-04 机构席位大额卖出（>5000万） | 可做 | 东财龙虎榜卖出席位明细（"机构专用"） |
| H-05 大单砸盘（单日大单净流出>流通市值2%） | 可做 | 东财资金流日线（大单+超大单）+ 流通市值 |
| H-06 换手率异常（>20%） | 可做 | 东财快照 f168 |
| H-07 涨停板资金分歧 | **不可实现**（无分钟级/涨停打开次数） | — |
| H-08 融券余额高位（>50万股 或 占流通市值>1%） | 可做 | 东财两融个股明细 RPTA_WEB_RZRQ_GGMX |
| H-09 融资余额异常（占流通市值>3%） | 可做 | 同上（RZYEZB） |
| H-10 停牌+融券双信号 | **不可实现**（无可靠"停牌中"状态源） | — |

H-02 口径说明：SKILL.md 原文"卖出席位>买入席位2倍"若按"席位数量"读则恒不成立
（东财买/卖各只披露前 5 席），故按"卖出前5席位金额合计 > 买入前5席位金额合计 ×2"落地，
报告中明示该口径。

硬约束（模型冻结期）：
- 只提示：不改评分/门槛/筛选/参数，不参与 ABSTAIN/ENTRY，不写 decision_log
- 数据缺失按 SKILL.md V1.1/V1.4 契约标 `[数据缺失: xxx]`，不得默认"无风险"，不得编造

用法:
    python -m src.hot_money_watch --date 2026-11-10         # 打印（不写库）
    python -m src.hot_money_watch --date 2026-11-10 --save   # 落库 + 写报表
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
import urllib.parse
from datetime import date, timedelta
from pathlib import Path

from src.config import MARKET_DB_PATH
from src.logger import get_logger
from src.net import fetch_json

log = get_logger(__name__)

FLOW_API = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
QUOTE_API = "https://push2.eastmoney.com/api/qt/stock/get"
DC_API = "https://datacenter-web.eastmoney.com/api/data/v1/get"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}
SOURCE = "eastmoney:fflow+RPTA_WEB_RZRQ_GGMX+RPT_BILLBOARD_DAILYDETAILS"

REQUEST_INTERVAL = 0.3

# —— SKILL.md「强制风险信号检查」阈值（照原文，不改） ——
H01_DAILY_OUT_WAN = 10000.0    # 1 亿/日
H02_MULTIPLE = 2.0             # 卖出/买入 > 2 倍（金额口径）
H04_INST_SELL_WAN = 5000.0     # 机构席位卖出 > 5000 万
H05_RATIO_PCT = 2.0            # 大单净流出 > 流通市值 2%
H06_TURNOVER_PCT = 20.0        # 换手率 > 20%
H08_RQYL_WAN = 50.0            # 融券余量 > 50 万股
H08_RATIO_PCT = 1.0            # 融券余额/流通市值 > 1%
H09_RATIO_PCT = 3.0            # 融资余额/流通市值 > 3%

LHB_WINDOW_DAYS = 30
HIGH_SIGNALS = ("H-01", "H-02", "H-05", "H-08")
MID_SIGNALS = ("H-04", "H-06", "H-09")

LEVEL_HIGH = "高"
LEVEL_MID = "中"
LEVEL_NONE = "无"
LEVEL_MISSING = "数据不足"

# 不可实现信号（报表中原样列出，避免被误读成"已检查且未触发"）
UNSUPPORTED = {
    "H-03": "无游资席位知识库（赵老哥/章建平等到营业部的映射表），硬编码名单属编造",
    "H-07": "无分钟级行情与涨停打开次数，日线无法判定「反复打开」",
    "H-10": "无可靠「停牌中」状态源（日K缺口只能事后推断，不能判定当日停牌）",
}


def secid_of(symbol: str) -> str:
    market = {"sh": "1", "sz": "0", "bj": "0"}.get(symbol[:2].lower())
    if not market:
        raise ValueError(f"无法识别市场: {symbol}")
    return f"{market}.{symbol[2:]}"


def _num(x):
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _wan(yuan) -> float | None:
    v = _num(yuan)
    return None if v is None else v / 1e4


def fetch_flow(symbol: str, end: str, *, timeout: int = 12,
               attempts: int = 2) -> list[dict]:
    """东财个股资金流日线：主力/大单/超大单净额（元→万元），按日期升序"""
    url = (f"{FLOW_API}?lmt=0&klt=101&fields1=f1,f2,f3,f7"
           f"&fields2=f51,f52,f53,f54,f55,f56&secid={secid_of(symbol)}")
    js = fetch_json(url, headers=HEADERS, timeout=timeout, attempts=attempts)
    klines = (js.get("data") or {}).get("klines") or []
    out = []
    for line in klines:
        parts = str(line).split(",")
        if len(parts) < 6:
            continue
        d = parts[0]
        if d > end:
            continue
        out.append({
            "date": d,
            "main_wan": _wan(parts[1]),
            "dade_wan": _wan((_num(parts[4]) or 0) + (_num(parts[5]) or 0)),
        })
    return sorted(out, key=lambda r: r["date"])


def fetch_quote(symbol: str, *, timeout: int = 12, attempts: int = 2) -> dict:
    """东财快照：换手率 f168(%)、流通市值 f117(元)、名称 f58"""
    url = (f"{QUOTE_API}?secid={secid_of(symbol)}&fields=f43,f58,f117,f168"
           f"&invt=2&fltt=2")
    js = fetch_json(url, headers=HEADERS, timeout=timeout, attempts=attempts)
    data = js.get("data") or {}
    if not data:
        return {}
    cap = _num(data.get("f117"))
    return {
        "name": data.get("f58") or "",
        "turnover_pct": _num(data.get("f168")),
        "float_cap_yi": round(cap / 1e8, 3) if cap else None,
    }


def fetch_margin(symbol: str, end: str, *, timeout: int = 12,
                 attempts: int = 2) -> dict:
    """东财两融个股明细（T+1 发布）：融券余量/融券余额比/融资余额比"""
    flt = f"(scode=\"{symbol[2:]}\")"
    url = (f"{DC_API}?reportName=RPTA_WEB_RZRQ_GGMX&columns=ALL&pageSize=10"
           f"&pageNumber=1&sortColumns=DATE&sortTypes=-1"
           f"&filter={urllib.parse.quote(flt)}&source=WEB&client=WEB")
    js = fetch_json(url, headers=HEADERS, timeout=timeout, attempts=attempts)
    rows = [r for r in ((js.get("result") or {}).get("data") or [])
            if str(r.get("DATE") or "")[:10] <= end]
    if not rows:
        return {}
    r = rows[0]
    rqye, sz = _num(r.get("RQYE")), _num(r.get("SZ"))
    rzyezb = _num(r.get("RZYEZB"))
    return {
        "date": str(r.get("DATE") or "")[:10],
        "rqyl_wan": round((_num(r.get("RQYL")) or 0) / 1e4, 2),
        "rq_ratio_pct": round(rqye / sz * 100, 3) if rqye and sz else None,
        "rzye_ratio_pct": round(rzyezb, 3) if rzyezb is not None else None,
        "float_cap_yi": round(sz / 1e8, 3) if sz else None,
    }


def fetch_lhb(symbol: str, start: str, end: str, *, timeout: int = 12,
              attempts: int = 2) -> dict:
    """东财龙虎榜买/卖席位明细（近 30 日）：最近上榜日 + 卖出/买入金额比 + 机构卖出席位"""
    flt = (f'(SECURITY_CODE="{symbol[2:]}")'
           f"(TRADE_DATE>='{start}')(TRADE_DATE<='{end}')")
    seats = {}
    for kind, report in (("buy", "RPT_BILLBOARD_DAILYDETAILSBUY"),
                         ("sell", "RPT_BILLBOARD_DAILYDETAILSSELL")):
        url = (f"{DC_API}?reportName={report}&columns=ALL&pageSize=200&pageNumber=1"
               f"&filter={urllib.parse.quote(flt)}&source=WEB&client=WEB")
        js = fetch_json(url, headers=HEADERS, timeout=timeout, attempts=attempts)
        seats[kind] = (js.get("result") or {}).get("data") or []
    days = sorted({str(r.get("TRADE_DATE") or "")[:10]
                   for r in seats["buy"] + seats["sell"] if r.get("TRADE_DATE")})
    if not days:
        return {"latest_date": "", "days_30d": 0, "sell_buy_ratio": None,
                "inst_sell_wan": None}
    latest = days[-1]
    buy_amt = sum(_num(r.get("BUY")) or 0 for r in seats["buy"]
                  if str(r.get("TRADE_DATE") or "")[:10] == latest)
    sell_amt = sum(_num(r.get("SELL")) or 0 for r in seats["sell"]
                   if str(r.get("TRADE_DATE") or "")[:10] == latest)
    inst_sell = sum(_num(r.get("SELL")) or 0 for r in seats["sell"]
                    if str(r.get("TRADE_DATE") or "")[:10] == latest
                    and "机构专用" in str(r.get("OPERATEDEPT_NAME") or ""))
    return {
        "latest_date": latest,
        "days_30d": len(days),
        "sell_buy_ratio": round(sell_amt / buy_amt, 2) if buy_amt else None,
        "inst_sell_wan": round(inst_sell / 1e4, 2),
    }


def fetch_bundle(symbol: str, asof: str) -> dict:
    """一只股票的全部原始数据（供 assess；测试可整体替换 provider）"""
    start = (date.fromisoformat(asof) - timedelta(days=LHB_WINDOW_DAYS)).isoformat()
    return {
        "flow": fetch_flow(symbol, asof),
        "quote": fetch_quote(symbol),
        "margin": fetch_margin(symbol, asof),
        "lhb": fetch_lhb(symbol, start, asof),
    }


def assess(symbol: str, name: str, bundle: dict, asof: str) -> dict:
    """按 SKILL.md「强制风险信号检查」逐条判定（只读）"""
    flow = bundle.get("flow") or []
    quote = bundle.get("quote") or {}
    margin = bundle.get("margin") or {}
    lhb = bundle.get("lhb") or {}

    missing: list[str] = []
    signals: list[str] = []
    row = {
        "symbol": symbol, "name": quote.get("name") or name,
        "flow_date": "", "main_3d_wan": None, "main_out1e_days": None,
        "dade_wan": None, "dade_ratio_pct": None,
        "float_cap_yi": quote.get("float_cap_yi"), "turnover_pct": quote.get("turnover_pct"),
        "lhb_date": lhb.get("latest_date") or "", "lhb_days30": lhb.get("days_30d"),
        "lhb_sell_buy_ratio": lhb.get("sell_buy_ratio"),
        "inst_sell_wan": lhb.get("inst_sell_wan"),
        "margin_date": margin.get("date") or "", "rqyl_wan": margin.get("rqyl_wan"),
        "rq_ratio_pct": margin.get("rq_ratio_pct"),
        "rzye_ratio_pct": margin.get("rzye_ratio_pct"),
        "signals": signals, "risk_level": LEVEL_NONE, "missing": missing, "note": "",
    }

    # —— H-01 主力资金连续流出：近 3 个可得交易日每日净流出 > 1 亿 ——
    last3 = flow[-3:]
    if len(last3) < 3:
        missing.append("主力资金近3日")
    else:
        row["flow_date"] = last3[-1]["date"]
        row["main_3d_wan"] = round(sum(d["main_wan"] or 0 for d in last3), 2)
        row["main_out1e_days"] = sum(1 for d in last3
                                     if (d["main_wan"] or 0) < -H01_DAILY_OUT_WAN)
        if row["main_out1e_days"] == 3:
            signals.append("H-01")

    # —— H-05 大单砸盘：单日（大单+超大单）净流出 > 流通市值 2% ——
    if not flow:
        missing.append("当日大单净额")
    else:
        last = flow[-1]
        row["flow_date"] = last["date"]
        row["dade_wan"] = round(last["dade_wan"] or 0, 2)
        cap = row["float_cap_yi"] or margin.get("float_cap_yi")
        if cap and row["dade_wan"] is not None:
            row["float_cap_yi"] = cap
            row["dade_ratio_pct"] = round(row["dade_wan"] / 1e4 / cap * 100, 3)
            if row["dade_ratio_pct"] < -H05_RATIO_PCT:
                signals.append("H-05")
        else:
            missing.append("流通市值（大单砸盘分母）")

    # —— H-06 换手率异常 > 20% ——
    if row["turnover_pct"] is None:
        missing.append("换手率")
    elif row["turnover_pct"] > H06_TURNOVER_PCT:
        signals.append("H-06")

    # —— H-02 龙虎榜卖出席位为主 / H-04 机构席位大额卖出 ——
    if not row["lhb_date"]:
        row["note"] = "近30日未上龙虎榜"
    else:
        ratio = row["lhb_sell_buy_ratio"]
        if ratio is not None and ratio > H02_MULTIPLE:
            signals.append("H-02")
        if (row["inst_sell_wan"] or 0) > H04_INST_SELL_WAN:
            signals.append("H-04")

    # —— H-08 融券余额高位 / H-09 融资余额异常 ——
    if not margin:
        missing.append("融资融券")
    else:
        if (row["rqyl_wan"] or 0) > H08_RQYL_WAN or \
                (row["rq_ratio_pct"] or 0) > H08_RATIO_PCT:
            signals.append("H-08")
        if (row["rzye_ratio_pct"] or 0) > H09_RATIO_PCT:
            signals.append("H-09")

    if any(s in HIGH_SIGNALS for s in signals):
        row["risk_level"] = LEVEL_HIGH
    elif any(s in MID_SIGNALS for s in signals):
        row["risk_level"] = LEVEL_MID
    elif missing:
        row["risk_level"] = LEVEL_MISSING
    return row


def collect(items, asof: str, *, provider=None, warnings: list[str] | None = None) -> list[dict]:
    """逐只采集+判定；单只失败只降级为「数据不足」+ 告警，不影响其他标的"""
    provider = provider or fetch_bundle
    rows = []
    for i, item in enumerate(items):
        symbol = getattr(item, "symbol", item)
        name = getattr(item, "name", "")
        try:
            rows.append(assess(symbol, name, provider(symbol, asof), asof))
        except Exception as exc:                     # noqa: BLE001
            rows.append({
                "symbol": symbol, "name": name, "flow_date": "", "main_3d_wan": None,
                "main_out1e_days": None, "dade_wan": None, "dade_ratio_pct": None,
                "float_cap_yi": None, "turnover_pct": None, "lhb_date": "",
                "lhb_days30": None, "lhb_sell_buy_ratio": None, "inst_sell_wan": None,
                "margin_date": "", "rqyl_wan": None, "rq_ratio_pct": None,
                "rzye_ratio_pct": None, "signals": [], "risk_level": LEVEL_MISSING,
                "missing": [f"全部资金面（{type(exc).__name__}: {exc}）"], "note": "",
            })
            if warnings is not None:
                warnings.append(f"游资数据缺失 {symbol}: {exc}")
            log.warning("游资数据缺失 %s: %s", symbol, exc)
        if i + 1 < len(items):
            time.sleep(REQUEST_INTERVAL)
    return rows


_DDL = """
CREATE TABLE IF NOT EXISTS hotmoney_watch (
    date                TEXT NOT NULL,   -- 判定日（数据截止日）
    symbol              TEXT NOT NULL,
    name                TEXT,
    flow_date           TEXT,            -- 资金流口径日期
    main_3d_wan         REAL,            -- 近3日主力净流入合计（万元）
    main_out1e_days     INTEGER,         -- 近3日单日净流出>1亿的天数
    dade_wan            REAL,            -- 最近一日大单(含超大单)净额（万元）
    dade_ratio_pct      REAL,            -- 大单净额/流通市值 %
    float_cap_yi        REAL,            -- 流通市值（亿元）
    turnover_pct        REAL,            -- 换手率 %
    lhb_date            TEXT,            -- 近30日最近上榜日
    lhb_days30          INTEGER,         -- 近30日上榜天数
    lhb_sell_buy_ratio  REAL,            -- 最近上榜日 卖出/买入 金额比
    inst_sell_wan       REAL,            -- 最近上榜日 机构专用席位卖出（万元）
    margin_date         TEXT,            -- 两融数据日期（T+1）
    rqyl_wan            REAL,            -- 融券余量（万股）
    rq_ratio_pct        REAL,            -- 融券余额/流通市值 %
    rzye_ratio_pct      REAL,            -- 融资余额/流通市值 %
    signals             TEXT,            -- 触发信号，如 H-01,H-08
    risk_level          TEXT,            -- 高/中/无/数据不足
    missing             TEXT,            -- [数据缺失] 清单
    note                TEXT,
    source              TEXT,
    fetched_at          TEXT,
    PRIMARY KEY (date, symbol)
)
"""


def save_rows(rows: list[dict], asof: str, *, db_path: str | None = None,
              fetched_at: str | None = None) -> int:
    """落一行客观记录（同日同标的覆盖，幂等）"""
    path = db_path or MARKET_DB_PATH
    ts = fetched_at or time.strftime("%Y-%m-%d %H:%M:%S")
    cols = ["symbol", "name", "flow_date", "main_3d_wan", "main_out1e_days",
            "dade_wan", "dade_ratio_pct", "float_cap_yi", "turnover_pct",
            "lhb_date", "lhb_days30", "lhb_sell_buy_ratio", "inst_sell_wan",
            "margin_date", "rqyl_wan", "rq_ratio_pct", "rzye_ratio_pct",
            "signals", "risk_level", "missing", "note"]
    with sqlite3.connect(path) as conn:
        conn.execute(_DDL)
        for r in rows:
            vals = [r.get(c) for c in cols]
            vals[cols.index("signals")] = ",".join(r.get("signals") or [])
            vals[cols.index("missing")] = "|".join(r.get("missing") or [])
            conn.execute(
                f"INSERT OR REPLACE INTO hotmoney_watch (date, {','.join(cols)}, source, fetched_at) "
                f"VALUES (?{',?' * len(cols)}, ?, ?)",
                [asof, *vals, SOURCE, ts])
        conn.commit()
    return len(rows)


def _fmt(v, suffix="", nd=2):
    if v is None or v == "":
        return "—"
    if isinstance(v, float):
        return f"{v:.{nd}f}{suffix}"
    return f"{v}{suffix}"


def render_markdown(asof: str, rows: list[dict], fetched_at: str | None = None) -> str:
    """游资提示报表（仅提示，不影响决策）"""
    hit = [r for r in rows if r["signals"]]
    miss = [r for r in rows if r["risk_level"] == LEVEL_MISSING]
    lines = [
        f"# 游资提示（{asof}）",
        "",
        "> **仅提示，不影响决策**：本表不参与五因子评分、ABSTAIN/ENTRY 判定与仓位计算。",
        "> 阈值："
        "H-01 近3日主力每日净流出>1亿；H-02 卖出/买入金额>2倍；H-04 机构席位卖出>5000万；"
        "H-05 单日大单净流出>流通市值2%；H-06 换手率>20%；H-08 融券余量>50万股或融券余额占流通市值>1%；"
        "H-09 融资余额占流通市值>3%。",
        "> 口径说明：H-02 原文「卖出席位>买入席位2倍」若按席位数读恒不成立（东财买卖各只披露前5席），"
        "本表按**卖出前5席位金额合计/买入前5席位金额合计**落地。"
        "资金流为东财日线口径（flow_date 列示实际日期），两融为 T+1 口径（margin_date 列示）。",
        "> 未实现：H-03 知名游资出货（无游资席位知识库）、H-07 涨停板资金分歧（无分钟级/打开次数）、"
        "H-10 停牌+融券双信号（无可靠「停牌中」状态源）——这三条**不是「已检查未触发」**，是没检查。",
        f"> 生成时间：{fetched_at or time.strftime('%Y-%m-%d %H:%M:%S')}；数据源：{SOURCE}",
    ]
    flow_days = {r["flow_date"] for r in rows if r.get("flow_date")}
    margin_days = {r["margin_date"] for r in rows if r.get("margin_date")}
    if flow_days:
        lines.append(f"> 资金流口径日：{'、'.join(sorted(flow_days))}"
                     "（盘后接口未更新时自动取最近一日）；"
                     f"两融口径日：{'、'.join(sorted(margin_days)) or '—'}（T+1 披露）")
    lines += [
        "",
        f"## 一、触发风险信号（{len(hit)} 只）",
        "",
    ]
    if hit:
        lines += [
            "| 标的 | 代码 | 档位 | 信号 | 近3日主力(万) | 大单净额(万) | 大单/流通市值% "
            "| 换手率% | 融券余量(万股) | 融资占比% | 最近上榜日 | 卖/买倍数 | 机构卖出(万) |",
            "|------|------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|",
        ]
        for r in sorted(hit, key=lambda x: (x["risk_level"] != LEVEL_HIGH, x["symbol"])):
            lines.append(
                f"| {r['name']} | {r['symbol']} | {r['risk_level']} | {', '.join(r['signals'])} | "
                f"{_fmt(r['main_3d_wan'])} | {_fmt(r['dade_wan'])} | {_fmt(r['dade_ratio_pct'])} | "
                f"{_fmt(r['turnover_pct'])} | {_fmt(r['rqyl_wan'])} | {_fmt(r['rzye_ratio_pct'])} | "
                f"{r['lhb_date'] or '—'} | {_fmt(r['lhb_sell_buy_ratio'])} | "
                f"{_fmt(r['inst_sell_wan'])} |")
    else:
        lines.append("（无）")

    lines += ["", f"## 二、观察池全量资金面（{len(rows)} 只）", "",
              "| 标的 | 代码 | 档位 | 信号 | 近3日主力(万) | 换手率% | 融券余量(万股) "
              "| 融券占流通% | 融资占比% | 上榜天数 | 数据缺失 |",
              "|------|------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|------|"]
    for r in rows:
        lines.append(
            f"| {r['name']} | {r['symbol']} | {r['risk_level']} | "
            f"{', '.join(r['signals']) if r['signals'] else '—'} | "
            f"{_fmt(r['main_3d_wan'])} | {_fmt(r['turnover_pct'])} | {_fmt(r['rqyl_wan'])} | "
            f"{_fmt(r['rq_ratio_pct'])} | {_fmt(r['rzye_ratio_pct'])} | "
            f"{_fmt(r['lhb_days30'])} | "
            f"{'、'.join(f'[数据缺失: {m}]' for m in r['missing']) if r['missing'] else '—'} |")

    if miss:
        lines += ["", f"## 三、数据不足（{len(miss)} 只）", "",
                  "> 数据不足不得当「无风险」使用，也不得据此下结论，需人工复核。", ""]
        for r in miss:
            lines.append(f"- {r['name']}（{r['symbol']}）："
                         + "、".join(f"[数据缺失: {m}]" for m in r["missing"]))
    return "\n".join(lines) + "\n"


def write_report(asof: str, rows: list[dict], out_dir: Path | None = None,
                 fetched_at: str | None = None) -> Path:
    out = out_dir or (Path("outputs") / "收盘报表")
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"游资提示_{asof}.md"
    path.write_text(render_markdown(asof, rows, fetched_at), encoding="utf-8")
    return path


def run(items, asof: str, *, is_dry: bool = False, warnings: list[str] | None = None,
        provider=None, market_db: str | None = None,
        out_dir: Path | None = None) -> dict:
    """收盘闭环入口：采集 → 判定 → 落库 + 报表（失败只进 warnings）"""
    rows = collect(items, asof, provider=provider, warnings=warnings)
    if is_dry:
        return {"rows": rows, "written": 0, "path": None}
    save_rows(rows, asof, db_path=market_db)
    path = write_report(asof, rows, out_dir=out_dir)
    return {"rows": rows, "written": len(rows), "path": path}


def _main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    from src.dates import last_trading_day
    from src.watchlist import WatchlistStore

    parser = argparse.ArgumentParser(description="游资提示（只读观察层）")
    parser.add_argument("--date", default=None, help="判定日（默认最近交易日）")
    parser.add_argument("--save", action="store_true", help="落库并写报表")
    args = parser.parse_args()
    asof = args.date or last_trading_day(date.today().isoformat())
    rows = collect(WatchlistStore().all(), asof)
    print(render_markdown(asof, rows))
    if args.save:
        save_rows(rows, asof)
        print(f"已落库 {len(rows)} 条；报表 {write_report(asof, rows)}")
    else:
        print("（未落库；--save 落库并写报表）")


if __name__ == "__main__":
    _main()
