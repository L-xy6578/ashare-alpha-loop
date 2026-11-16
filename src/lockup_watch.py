"""
解禁提示（只读观察层 · 不参与任何决策）

本模块实现限售解禁风险分档。
本模块只做"多看一层、写进报告"：

- 数据源：东方财富数据中心限售解禁 `RPT_LIFT_STAGE`（按 SECURITY_CODE + FREE_DATE 区间过滤）
- 判定：未来 30 个自然日内是否有解禁、解禁股数占流通股本比例（= 解禁市值/流通市值，
  东财 FREE_RATIO 口径）、6 个月内多次解禁累计比例
- 分档：沿用 SKILL.md「解禁规模」维度阈值（<5% 低 / 5-15% 中 / 15-30% 高 / >30% 极高），
  风险信号编号沿用 SKILL.md 白名单 R-01/R-02/R-07

硬约束（模型冻结期）：
- 不改任何评分/门槛/筛选/可执行性参数，不参与 ABSTAIN/ENTRY 判定，不写 decision_log 决策字段
- 数据缺失按 SKILL.md V1.3 规则 3 标注「数据缺失」，不得默认低风险，不得编造

用法:
    python -m src.lockup_watch --date 2026-11-10            # 打印观察池解禁提示（不写库）
    python -m src.lockup_watch --date 2026-11-10 --save     # 打印并落库 market.db
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

API = "https://datacenter-web.eastmoney.com/api/data/v1/get"
REPORT_NAME = "RPT_LIFT_STAGE"
SOURCE = "eastmoney:RPT_LIFT_STAGE"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}

HORIZON_DAYS = 30          # 提示窗口：未来 30 个自然日
STACK_DAYS = 180           # R-07 多次解禁叠加的统计窗口：6 个月
REQUEST_INTERVAL = 0.3     # 东财限流：串行 + 间隔

LEVEL_NONE = "无"
LEVEL_MISSING = "数据缺失"

# SKILL.md「解禁规模」分档（解禁市值/流通市值）：阈值照原文，不自定
_SIZE_BUCKETS = ((5.0, "低"), (15.0, "中"), (30.0, "高"))   # >30% → 极高


def code_of(symbol: str) -> str:
    """sh600535 / sz002366 → 600535"""
    return symbol[2:] if symbol[:2] in ("sh", "sz", "bj") else symbol


def fetch_events(symbol: str, start: str, end: str, *, timeout: int = 12,
                 attempts: int = 2) -> list[dict]:
    """东财限售解禁：取 symbol 在 [start, end] 内的解禁记录（按解禁日升序）

    返回字段（已归一）：
        date / shares_wan / market_cap_wan / ratio_pct / share_type / holders
    接口无数据时返回 `code=9201 返回数据为空`，按"无事件"处理，不是失败。
    """
    flt = (f'(SECURITY_CODE="{code_of(symbol)}")'
           f"(FREE_DATE>='{start}')(FREE_DATE<='{end}')")
    rows: list[dict] = []
    page = 1
    while True:
        url = (f"{API}?reportName={REPORT_NAME}&columns=ALL"
               f"&sortColumns=FREE_DATE&sortTypes=1&pageSize=200&pageNumber={page}"
               f"&filter={urllib.parse.quote(flt)}&source=WEB&client=WEB")
        js = fetch_json(url, headers=HEADERS, timeout=timeout, attempts=attempts)
        result = js.get("result") or {}
        data = result.get("data") or []
        for r in data:
            rows.append({
                "date": str(r.get("FREE_DATE") or "")[:10],
                "shares_wan": _num(r.get("CURRENT_FREE_SHARES")),
                "market_cap_wan": _num(r.get("LIFT_MARKET_CAP")),
                "ratio_pct": (_num(r.get("FREE_RATIO")) or 0.0) * 100,
                "share_type": r.get("FREE_SHARES_TYPE") or "",
                "holders": _num(r.get("BATCH_HOLDER_NUM")),
            })
        pages = result.get("pages") or 0
        if page >= pages or page >= 5:
            break
        page += 1
        time.sleep(REQUEST_INTERVAL)
    return sorted(rows, key=lambda e: e["date"])


def _num(x):
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _level(sum_ratio_pct: float) -> str:
    for limit, name in _SIZE_BUCKETS:
        if sum_ratio_pct < limit:
            return name
    return "极高"


def assess(symbol: str, name: str, events: list[dict], asof: str,
           horizon: int = HORIZON_DAYS) -> dict:
    """按 SKILL.md 分档给出风险提示（只读；不改任何决策）

    Args:
        events: fetch_events 结果（asof 起 STACK_DAYS 内的记录）
    Raises:
        ValueError: asof 非法日期
    """
    base = date.fromisoformat(asof)
    horizon_end = (base + timedelta(days=horizon)).isoformat()
    pending = [e for e in events if e["date"] >= asof]
    near = [e for e in pending if e["date"] <= horizon_end]
    row = {
        "symbol": symbol, "name": name, "horizon_days": horizon,
        "has_upcoming": bool(near),
        "event_count": len(near),
        "nearest_date": "", "nearest_ratio_pct": None,
        "max_ratio_pct": None, "sum_ratio_pct": None,
        "market_cap_yi": None, "share_type": "",
        "risk_level": LEVEL_NONE, "signals": [],
        "stack_count": 0, "stack_ratio_pct": None,
    }
    if near:
        first = near[0]
        ratios = [e["ratio_pct"] for e in near]
        row.update({
            "nearest_date": first["date"],
            "nearest_ratio_pct": round(first["ratio_pct"], 2),
            "max_ratio_pct": round(max(ratios), 2),
            "sum_ratio_pct": round(sum(ratios), 2),
            "market_cap_yi": (round((first["market_cap_wan"] or 0) / 1e4, 2)
                              if first["market_cap_wan"] is not None else None),
            "share_type": first["share_type"],
            "risk_level": _level(sum(ratios)),
        })
    # 风险信号（仅 SKILL.md 白名单里有数据支撑的三条；其余 R-03/04/05/06 无数据源，不输出）
    if near:
        row["signals"].append("R-01")                      # 近期解禁冲击：30 日内有解禁
    if any(e["ratio_pct"] > 15 for e in near):
        row["signals"].append("R-02")                      # 大额解禁：单次 >15% 流通市值
    stack_end = (base + timedelta(days=STACK_DAYS)).isoformat()
    stack = [e for e in pending if e["date"] <= stack_end]
    row["stack_count"] = len(stack)
    row["stack_ratio_pct"] = round(sum(e["ratio_pct"] for e in stack), 2) if stack else None
    if len(stack) >= 2 and (row["stack_ratio_pct"] or 0) > 30:
        row["signals"].append("R-07")                      # 多次解禁叠加
    return row


def missing(symbol: str, name: str, error: str, horizon: int = HORIZON_DAYS) -> dict:
    """数据缺失：按 SKILL.md 不得默认低风险，单列「数据缺失」档"""
    return {
        "symbol": symbol, "name": name, "horizon_days": horizon,
        "has_upcoming": None, "event_count": 0,
        "nearest_date": "", "nearest_ratio_pct": None,
        "max_ratio_pct": None, "sum_ratio_pct": None,
        "market_cap_yi": None, "share_type": "",
        "risk_level": LEVEL_MISSING, "signals": [],
        "stack_count": 0, "stack_ratio_pct": None,
        "error": error,
    }


def collect(items, asof: str, *, provider=None, horizon: int = HORIZON_DAYS,
            warnings: list[str] | None = None) -> list[dict]:
    """对观察池逐只取解禁数据并判定；单只失败只降级为「数据缺失」+ 告警"""
    provider = provider or fetch_events
    end = (date.fromisoformat(asof) + timedelta(days=STACK_DAYS)).isoformat()
    rows: list[dict] = []
    for i, item in enumerate(items):
        symbol = getattr(item, "symbol", item)
        name = getattr(item, "name", "")
        try:
            events = provider(symbol, asof, end)
            rows.append(assess(symbol, name, events, asof, horizon=horizon))
        except Exception as exc:                     # noqa: BLE001 单只失败不影响其他
            rows.append(missing(symbol, name, f"{type(exc).__name__}: {exc}", horizon=horizon))
            if warnings is not None:
                warnings.append(f"解禁数据缺失 {symbol}: {exc}")
            log.warning("解禁数据缺失 %s: %s", symbol, exc)
        if i + 1 < len(items):
            time.sleep(REQUEST_INTERVAL)
    return rows


def save_rows(rows: list[dict], asof: str, *, db_path: str | None = None,
              fetched_at: str | None = None) -> int:
    """落一行客观记录到 market.db.lockup_watch（同日同标的覆盖，幂等）"""
    path = db_path or MARKET_DB_PATH
    ts = fetched_at or time.strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(path) as conn:
        conn.execute(_DDL)
        for r in rows:
            conn.execute(
                """INSERT OR REPLACE INTO lockup_watch
                   (date, symbol, name, horizon_days, has_upcoming, event_count,
                    nearest_date, nearest_ratio_pct, max_ratio_pct, sum_ratio_pct,
                    market_cap_yi, share_type, stack_count, stack_ratio_pct,
                    risk_level, signals, source, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (asof, r["symbol"], r["name"], r["horizon_days"],
                 None if r["has_upcoming"] is None else int(r["has_upcoming"]),
                 r["event_count"], r["nearest_date"], r["nearest_ratio_pct"],
                 r["max_ratio_pct"], r["sum_ratio_pct"], r["market_cap_yi"],
                 r["share_type"], r["stack_count"], r["stack_ratio_pct"],
                 r["risk_level"], ",".join(r["signals"]), SOURCE, ts),
            )
        conn.commit()
    return len(rows)


_DDL = """
CREATE TABLE IF NOT EXISTS lockup_watch (
    date               TEXT NOT NULL,   -- 判定日（数据截止日）
    symbol             TEXT NOT NULL,
    name               TEXT,
    horizon_days       INTEGER,         -- 提示窗口天数
    has_upcoming       INTEGER,         -- 1=窗口内有解禁 0=无 NULL=数据缺失
    event_count        INTEGER,
    nearest_date       TEXT,            -- 窗口内最近解禁日
    nearest_ratio_pct  REAL,            -- 最近一次解禁股数占流通股本 %
    max_ratio_pct      REAL,            -- 窗口内单次最大占比 %
    sum_ratio_pct      REAL,            -- 窗口内累计占比 %
    market_cap_yi      REAL,            -- 最近一次解禁市值（亿元，东财参考价）
    share_type         TEXT,            -- 解禁股份类型
    stack_count        INTEGER,         -- 6 个月内解禁次数
    stack_ratio_pct    REAL,            -- 6 个月内累计占比 %
    risk_level         TEXT,            -- 无/低/中/高/极高/数据缺失
    signals            TEXT,            -- R-01,R-02,R-07
    source             TEXT,
    fetched_at         TEXT,
    PRIMARY KEY (date, symbol)
)
"""


def _fmt(v, suffix="", nd=2):
    if v is None or v == "":
        return "—"
    if isinstance(v, float):
        return f"{v:.{nd}f}{suffix}"
    return f"{v}{suffix}"


def render_markdown(asof: str, rows: list[dict], fetched_at: str | None = None) -> str:
    """解禁提示报表（仅提示，不影响决策）"""
    hit = [r for r in rows if r["has_upcoming"]]
    miss = [r for r in rows if r["has_upcoming"] is None]
    lines = [
        f"# 解禁提示（{asof}）",
        "",
        f"> 口径：解禁数据来自东财限售解禁（RPT_LIFT_STAGE），按 **未来 {HORIZON_DAYS} 个自然日** 判定；"
        "占比 = 解禁股数/流通股本（东财 FREE_RATIO，等价于解禁市值/流通市值）。",
        "> 分档阈值："
        "<5% 低 / 5-15% 中 / 15-30% 高 / >30% 极高；风险信号仅取有数据支撑的 R-01（30 日内解禁）、"
        "R-02（单次 >15%）、R-07（6 个月内累计 >30% 且 ≥2 次）。",
        "> **仅提示，不影响决策**：本表不参与五因子评分、ABSTAIN/ENTRY 判定与仓位计算；"
        "股东类型/历史减持/股权质押三维无数据源，未计算 0-100 综合解禁分。",
        f"> 生成时间：{fetched_at or time.strftime('%Y-%m-%d %H:%M:%S')}；"
        f"数据源：{SOURCE}",
        "",
        f"## 一、未来 {HORIZON_DAYS} 个自然日内有解禁（{len(hit)} 只）",
        "",
    ]
    if hit:
        lines += [
            "| 标的 | 代码 | 最近解禁日 | 单次占流通% | 累计占流通% | 解禁市值(亿) | 类型 | 风险档位 | 信号 |",
            "|------|------|-----------|:---:|:---:|:---:|------|:---:|:---:|",
        ]
        for r in sorted(hit, key=lambda x: (-(x["sum_ratio_pct"] or 0), x["symbol"])):
            lines.append(
                f"| {r['name']} | {r['symbol']} | {r['nearest_date']} | "
                f"{_fmt(r['nearest_ratio_pct'])} | {_fmt(r['sum_ratio_pct'])} | "
                f"{_fmt(r['market_cap_yi'])} | {r['share_type']} | "
                f"{r['risk_level']} | {', '.join(r['signals']) or '—'} |")
    else:
        lines.append("（无）")

    lines += ["", f"## 二、未来 {HORIZON_DAYS} 个自然日内无解禁（{len(rows) - len(hit) - len(miss)} 只）", ""]
    clean = [r for r in rows if r["has_upcoming"] is False]
    if clean:
        lines += ["| 标的 | 代码 |", "|------|------|"]
        for r in clean:
            lines.append(f"| {r['name']} | {r['symbol']} |")
    else:
        lines.append("（无）")

    if miss:
        lines += ["", f"## 三、数据缺失（{len(miss)} 只）", "",
                  "| 标的 | 代码 | 说明 |", "|------|------|------|"]
        for r in miss:
            lines.append(f"| {r['name']} | {r['symbol']} | "
                         f"[数据缺失: 解禁规模]（{r.get('error', '')}） |")
        lines += ["", "> 数据缺失不判低风险、不判高风险，需人工复核。"]
    return "\n".join(lines) + "\n"


def write_report(asof: str, rows: list[dict], out_dir: Path | None = None,
                 fetched_at: str | None = None) -> Path:
    out = out_dir or (Path("outputs") / "收盘报表")
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"解禁提示_{asof}.md"
    path.write_text(render_markdown(asof, rows, fetched_at), encoding="utf-8")
    return path


def run(items, asof: str, *, is_dry: bool = False, warnings: list[str] | None = None,
        provider=None, market_db: str | None = None,
        out_dir: Path | None = None) -> dict:
    """收盘闭环调用入口：采集 → 判定 → 落库 + 报表（失败只进 warnings，不改退出码）"""
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

    parser = argparse.ArgumentParser(description="解禁提示（只读观察层）")
    parser.add_argument("--date", default=None, help="判定日（默认最近交易日）")
    parser.add_argument("--save", action="store_true", help="落库 market.db 并写报表")
    args = parser.parse_args()
    asof = args.date or last_trading_day(date.today().isoformat())
    items = WatchlistStore().all()
    rows = collect(items, asof)
    print(render_markdown(asof, rows))
    if args.save:
        save_rows(rows, asof)
        path = write_report(asof, rows)
        print(f"已落库 {len(rows)} 条；报表 {path}")
    else:
        print("（未落库；--save 落库并写报表）")


if __name__ == "__main__":
    _main()
