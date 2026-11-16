"""
三角闭环选股体系 — 观察池（单一数据源）

背景：R47 之前观察池硬编码在 r47_closed_loop.py 的 WATCHLIST 字典里，
而"动态股票池.md"又各自维护一份，两套真相会漂移（文档池停在 7-21，代码池到 8-03）。
本模块把观察池落库为唯一真相，闭环脚本、手动选股、自动任务读写同一张表，
Markdown 只做展示/导出。

用法:
    from src.watchlist import WatchlistStore

    store = WatchlistStore()
    store.add("sz002202", "002202", "金风科技", sector="电力设备/风电",
              strategy_tag="风电龙头观察", source="manual")
    items = store.all()                    # 全部 active 标的
    store.remove("sz002202", "评分不达标")

CLI:
    python -m src.watchlist --list
    python -m src.watchlist --add sz002202 002202 金风科技 --sector 电力设备/风电 --tag 风电龙头观察
    python -m src.watchlist --remove sz002202 --reason 评分不达标
    python -m src.watchlist --export        # 导出 Markdown 视图
    python -m src.watchlist --import-md watchlist.md  # 批量导入 Markdown 表格
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.config import MARKET_DB_PATH
from src.db import get_connection
from src.executable import is_buyable_code
from src.logger import get_logger

log = get_logger(__name__)


def _symbol_prefix(code: str) -> str:
    """6位代码 → sh/sz/bj 前缀"""
    if code.startswith("6"):
        return "sh"
    if code.startswith(("4", "8", "920")):
        return "bj"
    return "sz"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
    symbol       TEXT PRIMARY KEY,
    code         TEXT NOT NULL,
    name         TEXT NOT NULL,
    cost_price   REAL,
    take_profit  REAL,
    stop_loss    REAL,
    strategy_tag TEXT NOT NULL DEFAULT '',
    sector       TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'active',   -- active / removed
    source       TEXT NOT NULL DEFAULT 'manual',   -- legacy / manual / research / auto
    note         TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class WatchItem:
    """一条观察池标的"""

    symbol: str
    code: str
    name: str
    cost_price: float | None = None
    take_profit: float | None = None
    stop_loss: float | None = None
    strategy_tag: str = ""
    sector: str = ""
    status: str = "active"
    source: str = "manual"
    note: str = ""
    updated_at: str = ""


# 首次运行时的演示观察池（不含真实持仓与个人记录，可随时用 CLI 删除）
LEGACY_WATCHLIST: list[WatchItem] = [
    WatchItem("sh600535", "600535", "天士力", None, None, None,
              "示例观察", "医药", "active", "demo", "演示数据，可删除"),
    WatchItem("sh600019", "600019", "宝钢股份", None, None, None,
              "示例观察", "钢铁", "active", "demo", "演示数据，可删除"),
    WatchItem("sz002366", "002366", "融发核电", None, None, None,
              "示例观察", "电力设备/核电", "active", "demo", "演示数据，可删除"),
    WatchItem("sz002202", "002202", "金风科技", None, None, None,
              "示例观察", "电力设备/风电", "active", "demo", "演示数据，可删除"),
    WatchItem("sz300880", "300880", "迦南智能", None, None, None,
              "示例观察", "电力设备/电网", "active", "demo", "演示数据，可删除"),
]


def init_watchlist_db(db_path: str = MARKET_DB_PATH) -> str:
    """建表（幂等）"""
    with get_connection(db_path) as conn:
        conn.executescript(_SCHEMA)
    return db_path


class WatchlistStore:
    """观察池读写（唯一数据源）"""

    def __init__(self, db_path: str = MARKET_DB_PATH):
        self.db_path = db_path
        init_watchlist_db(db_path)
        self.seed_defaults()

    def seed_defaults(self) -> int:
        """首次运行时把历史观察池迁入；表非空则不覆盖"""
        with get_connection(self.db_path, readonly=True) as conn:
            n = conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0]
        if n > 0:
            return 0
        with get_connection(self.db_path) as conn:
            for item in LEGACY_WATCHLIST:
                conn.execute(
                    """INSERT OR IGNORE INTO watchlist
                       (symbol, code, name, cost_price, take_profit, stop_loss,
                        strategy_tag, sector, status, source, note, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (item.symbol, item.code, item.name, item.cost_price,
                     item.take_profit, item.stop_loss, item.strategy_tag,
                     item.sector, item.status, item.source, item.note,
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                )
        log.info("观察池种子初始化完成：%d 条", len(LEGACY_WATCHLIST))
        return len(LEGACY_WATCHLIST)

    def _row_to_item(self, row) -> WatchItem:
        return WatchItem(*row)

    def get(self, symbol: str) -> WatchItem | None:
        with get_connection(self.db_path, readonly=True) as conn:
            row = conn.execute(
                """SELECT symbol, code, name, cost_price, take_profit, stop_loss,
                          strategy_tag, sector, status, source, note, updated_at
                   FROM watchlist WHERE symbol=?""",
                (symbol,),
            ).fetchone()
        return self._row_to_item(row) if row else None

    def all(self, status: str = "active") -> list[WatchItem]:
        with get_connection(self.db_path, readonly=True) as conn:
            rows = conn.execute(
                """SELECT symbol, code, name, cost_price, take_profit, stop_loss,
                          strategy_tag, sector, status, source, note, updated_at
                   FROM watchlist WHERE status=? ORDER BY symbol""",
                (status,),
            ).fetchall()
        return [self._row_to_item(r) for r in rows]

    def add(
        self,
        symbol: str,
        code: str,
        name: str,
        cost_price: float | None = None,
        take_profit: float | None = None,
        stop_loss: float | None = None,
        strategy_tag: str = "",
        sector: str = "",
        source: str = "manual",
        note: str = "",
        allow_analysis_only: bool = False,
    ) -> WatchItem | None:
        """新增/覆盖观察池标的

        交易权限硬约束：只能买主板（600/601/603/605、000/001/002/003）。
        创业板/科创板/北交所默认拒绝入池；analysis-only 分析清单可显式放行。
        """
        ok, reason = is_buyable_code(code, symbol)
        if not ok and not allow_analysis_only:
            log.warning("观察池拒绝入池 %s(%s)：%s", name, code, reason)
            return None
        item = WatchItem(
            symbol=symbol, code=code, name=name,
            cost_price=cost_price, take_profit=take_profit, stop_loss=stop_loss,
            strategy_tag=strategy_tag, sector=sector, status="active",
            source=source, note=note,
            updated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        with get_connection(self.db_path) as conn:
            conn.execute(
                """INSERT INTO watchlist
                   (symbol, code, name, cost_price, take_profit, stop_loss,
                    strategy_tag, sector, status, source, note, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(symbol) DO UPDATE SET
                     code=excluded.code, name=excluded.name,
                     cost_price=excluded.cost_price, take_profit=excluded.take_profit,
                     stop_loss=excluded.stop_loss, strategy_tag=excluded.strategy_tag,
                     sector=excluded.sector, status=excluded.status,
                     source=excluded.source, note=excluded.note,
                     updated_at=excluded.updated_at""",
                (item.symbol, item.code, item.name, item.cost_price,
                 item.take_profit, item.stop_loss, item.strategy_tag,
                 item.sector, item.status, item.source, item.note, item.updated_at),
            )
        log.info("观察池更新 %s(%s): tag=%s sector=%s source=%s",
                 name, symbol, strategy_tag, sector, source)
        return item

    def remove(self, symbol: str, reason: str = "") -> bool:
        """移出观察池（软删除，保留审计）"""
        with get_connection(self.db_path) as conn:
            cur = conn.execute(
                """UPDATE watchlist SET status='removed', note=?,
                       updated_at=datetime('now', 'localtime')
                   WHERE symbol=? AND status='active'""",
                (reason, symbol),
            )
        if cur.rowcount:
            log.info("观察池移除 %s：%s", symbol, reason)
        return cur.rowcount > 0

    def restore(self, symbol: str, note: str = "") -> bool:
        """恢复被软删的标的（回补误删，保留审计备注）"""
        with get_connection(self.db_path) as conn:
            cur = conn.execute(
                """UPDATE watchlist SET status='active', note=?,
                       updated_at=datetime('now', 'localtime')
                   WHERE symbol=? AND status='removed'""",
                (note, symbol),
            )
        if cur.rowcount:
            log.info("观察池恢复 %s：%s", symbol, note)
        return cur.rowcount > 0

    def export_markdown(self) -> str:
        """导出当前观察池 Markdown 视图（用于同步动态股票池.md 展示）"""
        items = self.all()
        lines = [f"# 观察池（{datetime.now().strftime('%Y-%m-%d %H:%M')} 自动导出）", ""]
        lines.append("| 标的 | 代码 | 所属板块 | 策略标签 | 成本 | 止盈 | 止损 | 来源 |")
        lines.append("|------|------|---------|---------|:---:|:---:|:---:|------|")
        for it in items:
            cost = f"{it.cost_price:.2f}" if it.cost_price else "—"
            tp = f"{it.take_profit:.2f}" if it.take_profit else "—"
            sl = f"{it.stop_loss:.2f}" if it.stop_loss else "—"
            lines.append(f"| {it.name} | {it.code} | {it.sector or '—'} | "
                         f"{it.strategy_tag or '—'} | {cost} | {tp} | {sl} | {it.source} |")
        return "\n".join(lines) + "\n"

    def import_markdown(self, md_path: str, source: str = "pool") -> tuple[int, int]:
        """从动态股票池.md 导入推荐组合+候补（仅 A/B 节），跳过已存在标的

        Returns:
            (新增数, 跳过数)
        """
        text = Path(md_path).read_text(encoding="utf-8")
        added = skipped = 0
        section = None
        idx_name = idx_code = idx_sector = None
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("### "):
                head = line[4:].lstrip(" #")
                if head.startswith(("A.", "A ")):
                    section, idx_name, idx_code, idx_sector = "A", None, None, None
                elif head.startswith(("B.", "B ")):
                    section, idx_name, idx_code, idx_sector = "B", None, None, None
                elif head.startswith(("C.", "C ")):
                    section = "C"
                continue
            if section not in ("A", "B") or not (line.startswith("|") and line.endswith("|")):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if "股票名称" in cells and "代码" in cells:
                idx_name = cells.index("股票名称")
                idx_code = cells.index("代码")
                idx_sector = cells.index("行业") if "行业" in cells else None
                continue
            if idx_name is None or idx_code is None or len(cells) <= max(idx_name, idx_code):
                continue
            name, code = cells[idx_name], cells[idx_code]
            if not (code.isdigit() and len(code) == 6):
                continue
            sym = _symbol_prefix(code) + code
            if self.get(sym):
                skipped += 1
                continue
            sector = cells[idx_sector] if idx_sector is not None and len(cells) > idx_sector else ""
            item = self.add(sym, code, name, sector=sector, source=source,
                            note="动态股票池导入")
            if item is None:
                skipped += 1   # 板块权限硬过滤：仅分析不可买
            else:
                added += 1
        log.info("观察池导入 %s: 新增%d 跳过%d", md_path, added, skipped)
        return added, skipped

    def monthly_check(self, today: str | None = None,
                      valuation_provider=None, source=None) -> dict:
        """观察池月度淘汰检查（#16/#20）：价格区间/PE 硬约束 + 数据新鲜度

        Returns:
            {"today", "suggestions": [{symbol,name,close,flags,hard}], "removed": [...]}
            hard=True 的条目可被 --apply 自动移除（软删+reason 审计）
        """
        from datetime import date, timedelta

        from src.data_source import TencentHttpDataSource, fetch_valuation_pe_pb

        today = today or date.today().isoformat()
        today_d = date.fromisoformat(today)
        cutoff_10 = (today_d - timedelta(days=10)).isoformat()
        cutoff_30 = (today_d - timedelta(days=30)).isoformat()
        latest = {}
        with get_connection(self.db_path, readonly=True) as conn:
            for row in conn.execute(
                "SELECT symbol, date, close FROM daily_bars "
                "WHERE (symbol, date) IN "
                "(SELECT symbol, MAX(date) FROM daily_bars GROUP BY symbol)"
            ).fetchall():
                latest[row[0]] = (row[1], row[2])
        net = source or TencentHttpDataSource()
        val_provider = valuation_provider or fetch_valuation_pe_pb
        suggestions = []
        for item in self.all():
            flags = []
            hard = False
            dt, close = latest.get(item.symbol, (None, None))
            if close is None:
                try:
                    bars = net.daily_kline(item.symbol, cutoff_10, today)
                    if bars:
                        close, dt = bars[-1].close, bars[-1].date
                except Exception:
                    pass
            if close is None:
                flags.append("无行情数据")
            else:
                if close < 5 or close > 20:
                    # 08-08 扫描 #1：入池标准（PE5-60/市值≥50亿）无价格上限，
                    # 主赛道大票普遍 >20 元，价格区间若进 hard 会误杀达标候选
                    # （8/5 视源股份 86.1 分被误删）→ 价格仅作提示，不自动删除
                    flags.append(f"价格 {close:.2f} 超出 5-20 元区间（提示，不自动删除）")
                if dt and dt < cutoff_30:
                    flags.append(f"数据长期陈旧（>30天，最新 {dt}）")
                    hard = True
                elif dt and dt < cutoff_10:
                    flags.append(f"数据陈旧（最新 {dt}）")
            try:
                pe = (val_provider(item.symbol) or {}).get("pe")
                if pe is not None and (pe > 80 or pe < 0):
                    flags.append(f"PE {pe:.1f}（>80 或亏损）")
                    hard = True
            except Exception:
                pass
            if flags:
                suggestions.append({
                    "symbol": item.symbol, "name": item.name,
                    "close": close, "flags": flags, "hard": hard,
                })
        return {"today": today, "suggestions": suggestions}

    def monthly_apply(self, today: str | None = None,
                      valuation_provider=None, source=None) -> dict:
        """月度淘汰自动执行（#20）：仅移除 hard 规则的条目（软删+reason 审计）"""
        result = self.monthly_check(today, valuation_provider, source)
        removed = []
        for s in result["suggestions"]:
            if not s["hard"]:
                continue
            reason = "月度淘汰自动执行：" + "；".join(s["flags"])
            if self.remove(s["symbol"], reason):
                removed.append({**s, "reason": reason})
        result["removed"] = removed
        return result


def _main() -> None:
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="观察池管理（单一数据源）")
    parser.add_argument("--list", action="store_true", help="列出当前观察池")
    parser.add_argument("--add", nargs=3, metavar=("SYMBOL", "CODE", "NAME"),
                        help="新增/更新标的")
    parser.add_argument("--sector", default="", help="所属板块")
    parser.add_argument("--tag", default="", help="策略标签")
    parser.add_argument("--cost", type=float, default=None, help="成本价")
    parser.add_argument("--tp", type=float, default=None, help="止盈价")
    parser.add_argument("--sl", type=float, default=None, help="止损价")
    parser.add_argument("--source", default="manual", help="来源 legacy/manual/research/auto")
    parser.add_argument("--remove", metavar="SYMBOL", help="移出观察池")
    parser.add_argument("--reason", default="", help="移除原因")
    parser.add_argument("--restore", metavar="SYMBOL", help="恢复被软删的标的")
    parser.add_argument("--note", default="", help="恢复备注")
    parser.add_argument("--export", action="store_true", help="导出 Markdown 视图")
    parser.add_argument("--import-md", metavar="PATH", help="从动态股票池.md 批量导入")
    parser.add_argument("--monthly-check", action="store_true",
                        help="观察池月度淘汰检查（出建议，不自动移除）")
    parser.add_argument("--monthly-apply", action="store_true",
                        help="月度淘汰自动执行（仅移除 PE>80或亏损/数据长期陈旧；价格区间仅提示，软删+审计）")
    parser.add_argument("--db", default=None, help="market.db 路径（测试用）")
    args = parser.parse_args()

    store = WatchlistStore(args.db) if args.db else WatchlistStore()
    if args.list:
        for it in store.all():
            print(f"  {it.symbol} {it.name}({it.code}) [{it.sector}] {it.strategy_tag} "
                  f"成本{it.cost_price or '—'} 来源{it.source}")
    elif args.add:
        sym, code, name = args.add
        item = store.add(sym, code, name, cost_price=args.cost, take_profit=args.tp,
                         stop_loss=args.sl, strategy_tag=args.tag, sector=args.sector,
                         source=args.source, note=args.note)
        if item is None:
            print(f"⛔ 拒绝入池：{name}({sym}) 属于创业板/科创板/北交所，仅分析不可买")
        else:
            print(f"✅ 观察池已更新：{name}({sym})")
    elif args.remove:
        ok = store.remove(args.remove, args.reason)
        print("✅ 已移除" if ok else "⚠️ 未找到或已移除")
    elif args.restore:
        ok = store.restore(args.restore, args.note or "恢复观察（回补误删）")
        print("✅ 已恢复" if ok else "⚠️ 未找到或不是 removed 状态")
    elif args.export:
        print(store.export_markdown())
    elif args.import_md:
        added, skipped = store.import_markdown(args.import_md)
        print(f"✅ 导入完成: 新增 {added} 只，跳过已存在 {skipped} 只")
    elif args.monthly_check:
        result = store.monthly_check()
        lines = [f"# 观察池月度淘汰建议（{result['today']}）", "",
                 "> 规则：PE≤80 且盈利 / 数据不陈旧；价格区间仅提示（8/8 修订，避免误杀达标候选）。仅建议，不自动移除。", ""]
        if result["suggestions"]:
            lines.append("| 标的 | 现价 | 问题 |")
            lines.append("|------|:---:|------|")
            for s in result["suggestions"]:
                lines.append(f"| {s['name']}({s['symbol']}) | "
                             f"{s['close'] if s['close'] is not None else '—'} | "
                             f"{'；'.join(s['flags'])} |")
        else:
            lines.append("（无淘汰建议）")
        md = "\n".join(lines) + "\n"
        out = Path("outputs") / "选股" / f"观察池淘汰建议_{result['today']}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        print(md)
        print(f"📄 已写入 {out}")
    elif args.monthly_apply:
        result = store.monthly_apply()
        lines = [f"# 观察池月度淘汰执行（{result['today']}）", ""]
        if result["removed"]:
            lines.append("已移除（软删，reason 留审计）：")
            for s in result["removed"]:
                lines.append(f"- {s['name']}({s['symbol']}): {s['reason']}")
        else:
            lines.append("（无 hard 规则命中，未移除）")
        if result["suggestions"]:
            kept = [s for s in result["suggestions"] if not s["hard"]]
            if kept:
                lines.append("")
                lines.append("保留（仅建议，未自动移除）：")
                for s in kept:
                    lines.append(f"- {s['name']}({s['symbol']}): {'；'.join(s['flags'])}")
        md = "\n".join(lines) + "\n"
        out = Path("outputs") / "选股" / f"观察池淘汰执行_{result['today']}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        print(md)
        print(f"📄 已写入 {out}")
    else:
        parser.print_help()


if __name__ == "__main__":
    _main()
