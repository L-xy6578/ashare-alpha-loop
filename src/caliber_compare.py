"""
行业景气口径对比（只做对比，不切默认）

口径 A（当前默认）：当期主赛道 TOP7 名称匹配（sector_ctx）
口径 B（可选）    ：所属板块 20 日涨幅在全量板块中的分位（0-100）

用途：用同一批观察池标的、同一交易日窗口跑两套口径，
输出行业分/总分/是否达 85 的差异，作为是否切换默认的依据。
默认值不改动——切换需用户确认后另行落地。

注意：估值因子取当前实时 PE/PB（历史日期无历史估值），两套口径相同，
      因此不影响"口径差异"的方向性比较；业绩证据取调研库最新情报。

用法:
    python -m src.caliber_compare                 # 默认 5 个交易日
    python -m src.caliber_compare --dates 2026-09-28,2026-10-29
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date
from pathlib import Path

from src.config import DB_PATH, MARKET_DB_PATH
from src.factors import growth_from_research, score_candidate
from src.logger import get_logger
from src.research import ResearchStore
from src.sector_analysis import SectorAnalyzer
from src.watchlist import WatchlistStore

log = get_logger(__name__)

DEFAULT_DATES = ("2026-09-28", "2026-10-09", "2026-10-16", "2026-10-26", "2026-10-29")


def _load_bars(market_db: str, symbol: str, day: str, n: int = 20) -> list:
    """库内截至 day 的最近 n 根日K（技术面因子用）"""
    from src.data_source import KlineBar

    with sqlite3.connect(market_db, timeout=5) as conn:
        rows = conn.execute(
            "SELECT date, open, close, high, low, volume, amount FROM daily_bars "
            "WHERE symbol=? AND date<=? ORDER BY date DESC LIMIT ?",
            (symbol, day, n),
        ).fetchall()
    bars = [KlineBar(r[0], r[1], r[2], r[3], r[4], volume=r[5], amount=r[6])
            for r in reversed(rows)]
    return bars


def compare(dates: list[str] | None = None,
            today: str | None = None,
            *,
            watch: WatchlistStore | None = None,
            research: ResearchStore | None = None,
            analyzer: SectorAnalyzer | None = None,
            valuation_provider=None,
            market_db: str = MARKET_DB_PATH) -> dict:
    """两套行业口径对比结果（纯计算，不写库）"""
    dates = list(dates or DEFAULT_DATES)
    today = today or date.today().isoformat()
    watch = watch or WatchlistStore()
    research = research or ResearchStore(DB_PATH)
    analyzer = analyzer or SectorAnalyzer(market_db)
    if valuation_provider is None:
        from src.data_source import fetch_valuation_pe_pb
        valuation_provider = fetch_valuation_pe_pb

    items = watch.all()
    valuation_map = {}
    for it in items:
        try:
            valuation_map[it.symbol] = valuation_provider(it.symbol) or {}
        except Exception:
            valuation_map[it.symbol] = {}

    out_dates = []
    for day in dates:
        plates = analyzer.load_date(day, kind="plate") + analyzer.load_date(day, kind="concept")
        plate_n, fund_n = len(plates), sum(1 for p in plates if p.zljlr_d20 is not None)
        r = analyzer.analyze(day)
        sector_ctx = {t.name: t.score for t in r["monthly_tracks"]}
        sector_ctx.update({t.name: t.score for t in r["quarterly_tracks"]})
        rows = []
        for it in items:
            bars = _load_bars(market_db, it.symbol, day)
            growth, evidence = growth_from_research(
                research.query(symbol=it.symbol, category="基本面", limit=5))
            row = {"symbol": it.symbol, "name": it.name, "sector": it.sector}
            if not bars:
                row.update({"note": "无行情数据"})
                rows.append(row)
                continue
            if growth is None:
                row.update({"note": "无业绩证据", "a": None, "b": None})
                rows.append(row)
                continue
            common = dict(growth=growth, growth_evidence=evidence, bars=bars,
                          valuation=valuation_map.get(it.symbol) or {})
            a = score_candidate(it.symbol, it.name, it.sector,
                                sector_ctx=sector_ctx, plates=plates,
                                industry_mode="tracks", **common)
            b = score_candidate(it.symbol, it.name, it.sector,
                                sector_ctx=sector_ctx, plates=plates,
                                industry_mode="percentile", **common)
            row.update({"a": a, "b": b})
            rows.append(row)
        a_hits = sum(1 for x in rows if x.get("a") and x["a"].recommended)
        b_hits = sum(1 for x in rows if x.get("b") and x["b"].recommended)
        out_dates.append({"date": day, "plate_n": plate_n, "fund_n": fund_n,
                          "rows": rows, "a_hits": a_hits, "b_hits": b_hits})
    return {"today": today, "dates": out_dates, "items": len(items)}


def _cell(res) -> tuple[str, str, str]:
    """(行业分, 总分, 是否达标)"""
    if not res:
        return "—", "—", "—"
    ind = "—" if "行业景气" in res.missing else f"{res.factors.industry:.1f}"
    return ind, f"{res.weighted:.1f}", "✅" if res.recommended else "⛔"


def render_markdown(result: dict) -> str:
    today = result["today"]
    lines = [
        f"# 行业景气口径对比（{today}）",
        "",
        "> **待用户确认后才切换默认**：本次只做对比，`r47` 默认口径仍是"
        "「当期主赛道 TOP7 名称匹配」（`--industry-mode tracks`）。",
        "> 口径 A：所属板块命中当期主赛道（月度 TOP5 + 季度 TOP3）名称，取赛道分。",
        "> 口径 B：所属板块 20 日涨幅在当日**全量板块**中的分位（0-100）。",
        "> 达标 = 五因子无缺失且加权 ≥85（与 KPI 登记门槛一致）；两口径共用同一批"
        "观察池标的、同一业绩证据与当前估值（估值无历史值，两口径相同）。",
        "",
        "## 一、汇总",
        "",
        "| 交易日 | 板块快照 | 20日资金非0 | 口径A 达标 | 口径B 达标 | 变化 |",
        "|------|:---:|:---:|:---:|:---:|:---:|",
    ]
    for d in result["dates"]:
        delta = d["b_hits"] - d["a_hits"]
        lines.append(f"| {d['date']} | {d['plate_n']} | {d['fund_n']} | "
                     f"{d['a_hits']}/{result['items']} | {d['b_hits']}/{result['items']} | "
                     f"{delta:+d} |")
    if any(d["plate_n"] == 0 for d in result["dates"]):
        missing = [d["date"] for d in result["dates"] if d["plate_n"] == 0]
        lines += ["", f"⚠️ 数据缺口：{', '.join(missing)} 无板块快照，两套口径均无法计算行业分"
                 "（强制不推荐），当日对比不具结论性。"]

    for d in result["dates"]:
        lines += ["", f"## {d['date']}（板块快照 {d['plate_n']} / 资金非0 {d['fund_n']}）", "",
                  "| 标的 | 板块 | A行业分 | A总分 | A达标 | B行业分 | B总分 | B达标 | 差异 |",
                  "|------|------|:---:|:---:|:---:|:---:|:---:|:---:|------|"]
        for x in d["rows"]:
            a = x.get("a")
            b = x.get("b")
            if not a and not b:
                lines.append(f"| {x['name']} | {x['sector'] or '—'} | — | — | — | — | — | — | {x.get('note', '—')} |")
                continue
            ai, aw, ah = _cell(a)
            bi, bw, bh = _cell(b)
            diff = []
            if a and b:
                if ah != bh:
                    diff.append("达标翻转")
                dw = round(b.weighted - a.weighted, 1)
                if dw:
                    diff.append(f"总分{dw:+.1f}")
                di = round(b.factors.industry - a.factors.industry, 1) \
                    if "行业景气" not in b.missing and "行业景气" not in a.missing else None
                if di:
                    diff.append(f"行业{di:+.1f}")
            lines.append(f"| {x['name']} | {x['sector'] or '—'} | {ai} | {aw} | {ah} | "
                         f"{bi} | {bw} | {bh} | {'；'.join(diff) or '无差异'} |")
    lines += ["", "---", f"*生成于 {today}（口径对比演练）。不构成投资建议。*"]
    return "\n".join(lines) + "\n"


def write_report(result: dict, out_dir: Path | None = None) -> Path:
    out_dir = out_dir or (Path("outputs") / "选股")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"口径对比_{result['today']}.md"
    path.write_text(render_markdown(result), encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="行业景气两套口径对比（只对比不切默认）")
    parser.add_argument("--dates", default=None, help="逗号分隔 YYYY-MM-DD；默认 5 个交易日")
    parser.add_argument("--today", default=None, help="报告日期（默认今天）")
    args = parser.parse_args()
    dates = [s.strip() for s in args.dates.split(",")] if args.dates else None
    result = compare(dates, args.today)
    path = write_report(result)
    print(render_markdown(result))
    print(f"📄 已写入 {path}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
