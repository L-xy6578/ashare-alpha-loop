"""
财报自动采集 — 东财业绩报表（B1a 探测落地，2026-08-25）

来源：datacenter.eastmoney.com RPT_LICO_FN_CPD（业绩报表）
能力：按观察池标的拉取最新一期 营收/归母/扣非（金额 + 同比），
      写入 research_notes（基本面类别），内容格式可被 growth_from_research 解析
      （扣非优先 → 归母 → 净利），从而直接支撑五因子"业绩趋势"。
去重：同一标题（标的名 + 报告期）不重复写入。

用法:
    python -m src.finance_feed --dry-run     # 拉取预览，不写入
    python -m src.finance_feed --inject      # 拉取并写入调研库
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request

from src.logger import get_logger
from src.research import ResearchStore
from src.watchlist import WatchlistStore

log = get_logger(__name__)

API = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
REPORT = "RPT_LICO_FN_CPD"


def _get_json(url: str, timeout: int = 10) -> dict:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _secucode(code: str) -> str:
    prefix = "SH" if code.startswith("6") else (
        "BJ" if code.startswith(("4", "8")) else "SZ")
    return f"{code}.{prefix}"


def fetch_earnings(code: str) -> dict | None:
    """东财业绩报表：最新一期 营收/归母（金额与同比，按 REPORTDATE 倒序），失败返回 None

    字段口径（RPT_LICO_FN_CPD）：TOTAL_OPERATE_INCOME 营收(元)、YSTZ 营收同比(%数值)、
    PARENT_NETPROFIT 归母(元)、SJLTZ 归母同比(%数值)；扣非金额本报表不含 → 归母兜底。
    """
    url = (f"{API}?reportName={REPORT}&columns=ALL"
           f"&filter=(SECUCODE%3D%22{_secucode(code)}%22)"
           f"&pageNumber=1&pageSize=1&sortColumns=REPORTDATE&sortTypes=-1"
           f"&source=WEB&client=WEB")
    try:
        rows = ((_get_json(url).get("result") or {}).get("data") or [])
    except Exception as exc:
        log.warning("财报拉取失败 %s: %s", code, exc)
        return None
    if not rows:
        return None
    r = rows[0]

    def _num(x):
        try:
            return float(x) if x is not None else None
        except (TypeError, ValueError):
            return None

    return {
        "report_date": r.get("REPORTDATE"),
        "revenue": _num(r.get("TOTAL_OPERATE_INCOME")),
        "revenue_yoy": _num(r.get("YSTZ")),
        "net_profit": _num(r.get("PARENT_NETPROFIT")),
        "net_yoy": _num(r.get("SJLTZ")),
    }


def build_note(code: str, name: str, e: dict) -> tuple[str, str, str] | None:
    """生成 (title, content, url)；content 含 归母 增速（百分数）供评分解析"""
    period = str(e.get("report_date") or "最新一期")[:10]
    parts = []
    if e.get("revenue") is not None:
        rev = e["revenue"] / 1e8
        parts.append(f"营收{rev:.2f}亿({e['revenue_yoy']:+.2f}%)"
                     if e.get("revenue_yoy") is not None else f"营收{rev:.2f}亿")
    if e.get("net_profit") is not None:
        np_ = e["net_profit"] / 1e8
        parts.append(f"归母{np_:.2f}亿({e['net_yoy']:+.2f}%)"
                     if e.get("net_yoy") is not None else f"归母{np_:.2f}亿")
    if not parts:
        return None
    return (
        f"{name} 财报自动采集（{period}）",
        "；".join(parts) + f"（东财业绩报表自动采集，{period}）",
        f"https://data.eastmoney.com/bbsj/{code}.html",
    )


def inject(dry_run: bool = False, symbols: list[str] | None = None) -> tuple[int, int]:
    """拉取观察池标的最新财报并写入调研库（按标题去重）"""
    research = ResearchStore()
    items = WatchlistStore().all()
    if symbols:
        items = [w for w in items if w.symbol in symbols]
    added = skipped = 0
    for item in items:
        e = fetch_earnings(item.code)
        if not e:
            skipped += 1
            continue
        note = build_note(item.code, item.name, e)
        if not note:
            skipped += 1
            continue
        title, content, url = note
        existing = research.query(symbol=item.symbol, category="基本面", limit=50)
        if any(n.title == title for n in existing):
            skipped += 1
            continue
        if dry_run:
            print(f"  [dry] {item.name}: {content}")
            added += 1
            continue
        research.add(item.symbol, item.name, "基本面", title, content, url)
        added += 1
    return added, skipped


def _main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="财报自动采集（东财业绩报表）")
    parser.add_argument("--dry-run", action="store_true", help="拉取预览，不写入")
    parser.add_argument("--inject", action="store_true", help="拉取并写入调研库")
    parser.add_argument("--symbols", default=None, help="限定标的（逗号分隔，默认观察池全部）")
    args = parser.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] if args.symbols else None
    added, skipped = inject(dry_run=not args.inject, symbols=symbols)
    print(f"完成：新增 {added} 条，跳过 {skipped} 条（已存在/无数据）"
          + ("（dry-run，未写入）" if not args.inject else ""))


if __name__ == "__main__":
    _main()
