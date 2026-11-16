"""
三角闭环选股体系 — 真实交易记录（real 账户 + real 口径知识库）

背景：真实持仓与模拟盘口径混淆，复盘曾把亏损误记成盈利。
本模块是"真实成交"的唯一录入入口：
    - 买入 → 更新 real 账户持仓
    - 卖出 → 按实际成本/股数结算盈亏 → kb.record(source="real") 自动路由对/错题本
    - KPI-07（选股准确率）只统计 real 口径，模拟盘推演不得混入

用法:
    python -m src.realtrade buy  sh600000 600000 示例银行 10.00 200 2026-01-05 "示例买入"
    python -m src.realtrade sell sh600000 9.20 200 2026-02-05 "示例卖出" \
        --root-cause "示例根因" --lesson "示例教训" --mistake-type 止损不及时
    python -m src.realtrade list          # 查看 real 账户
    python -m src.realtrade stop-alert    # 检查真实持仓 -7% 硬止损告警
"""

from __future__ import annotations

import argparse
import sys

from src.knowledge import KnowledgeBase
from src.logger import get_logger
from src.portfolio import PortfolioStore, Position

log = get_logger(__name__)


def real_buy_gates(symbol: str, code: str, name: str, price: float,
                   shares: int, *, research_store=None, source=None,
                   valuation_provider=None, store=None) -> tuple[bool, list[str]]:
    """真实开仓门禁（#9）：仓位上限 + 数据质量 + 五因子评分

    Returns:
        (是否通过, 拒绝原因列表)
    """
    from src.config import REAL_MAX_POSITION_PCT, REAL_TOTAL_ASSETS

    reasons: list[str] = []
    # 1) 单票仓位上限
    pos_value = shares * price
    cap = REAL_TOTAL_ASSETS * REAL_MAX_POSITION_PCT / 100.0
    if pos_value > cap:
        reasons.append(
            f"单票市值 {pos_value:.0f} 元 > 上限 {REAL_MAX_POSITION_PCT:.0f}%"
            f"（{cap:.0f} 元，总资产 {REAL_TOTAL_ASSETS:.0f}）——禁止满仓单票"
        )
    # 1.5) 加仓后单票合计市值上限（#85：防止多次买入累计超限）
    if store is not None:
        cur = store.get(symbol, account="real")
        if cur is not None and cur.shares > 0:
            total_value = (cur.shares + shares) * price
            if total_value > cap:
                reasons.append(
                    f"加仓后单票市值 {total_value:.0f} 元（现有 {cur.shares} 股 + 本次 {shares} 股）"
                    f" > 上限 {REAL_MAX_POSITION_PCT:.0f}%（{cap:.0f} 元）——禁止满仓单票"
                )
    # 2) 数据质量：market.db quality_snapshot 最新等级须 A/B
    from src.config import MARKET_DB_PATH
    from src.db import get_connection

    try:
        with get_connection(MARKET_DB_PATH, readonly=True) as conn:
            row = conn.execute(
                "SELECT grade FROM quality_snapshot WHERE symbol=? "
                "ORDER BY checked_at DESC LIMIT 1",
                (symbol,),
            ).fetchone()
        grade = row[0] if row else None
        if grade not in ("A", "B"):
            reasons.append(f"数据质量 {grade or '无快照'}（须 A/B 才允许真实开仓）")
    except Exception:
        reasons.append("数据质量检查不可用")
    # 3) 五因子评分 ≥85 且数据完整（业绩证据须来自调研库）
    from src.data_source import TencentHttpDataSource, fetch_valuation_pe_pb
    from src.dates import recent_window
    from src.factors import growth_from_research, score_candidate
    from src.research import ResearchStore
    from src.sector_analysis import SectorAnalyzer
    from src.watchlist import WatchlistStore

    try:
        research = research_store or ResearchStore()
        item = WatchlistStore().get(symbol)
        sector = item.sector if item else ""
        start, end = recent_window(days_back=20)
        src = source or TencentHttpDataSource()
        bars = src.daily_kline(symbol, start, end)
        growth, evidence = growth_from_research(
            research.query(symbol=symbol, category="基本面", limit=5)
        )
        if growth is None:
            reasons.append("调研库无业绩证据（基本面情报缺失）——评分无法通过")
        else:
            sector_ctx, plates = {}, []
            try:
                sa = SectorAnalyzer()
                r = sa.analyze()
                sector_ctx = {t.name: t.score for t in r["monthly_tracks"][:5]}
                sector_ctx.update({t.name: t.score for t in r["quarterly_tracks"][:3]})
                plates = sa.load_date(kind="plate") + sa.load_date(kind="concept")
            except Exception:
                pass
            val = valuation_provider(symbol) if valuation_provider else fetch_valuation_pe_pb(symbol)
            result = score_candidate(
                symbol, name, sector,
                growth=growth, growth_evidence=evidence,
                bars=bars, sector_ctx=sector_ctx, plates=plates,
                valuation=val,
            )
            if not result.recommended:
                reasons.append(
                    f"五因子评分 {result.weighted:.1f} 分（<85 或数据缺失），禁止真实开仓"
                )
    except Exception as e:
        reasons.append(f"五因子评分不可用: {e}")
    return not reasons, reasons


def real_buy(store: PortfolioStore, symbol: str, name: str, price: float,
             shares: int, trade_date: str, note: str = "") -> Position | None:
    """真实买入：已有持仓则累计股数 + 加权平均成本，否则新建（#85）

    修复：原 buy 分支 store.set 整体覆盖 shares/cost_price，
    同标的二次买入会丢失原成本基准；改为累计加仓。
    """
    cur = store.get(symbol, account="real")
    if cur is not None and cur.shares > 0:
        new_shares = cur.shares + shares
        new_cost = (cur.shares * cur.cost_price + shares * price) / new_shares
        note_text = f"{trade_date} 真实加仓 {shares}股@{price} {note}".strip()
        # 加仓保留原建仓日（entry_date=None → set 内保留）
        store.set(symbol, name, new_shares, new_cost, price, note_text, account="real")
        log.info("真实加仓 %s(%s): %d→%d股 成本%.4f",
                 name, symbol, cur.shares, new_shares, new_cost)
    else:
        note_text = f"{trade_date} 真实买入 {shares}股@{price} {note}".strip()
        store.set(symbol, name, shares, price, price, note_text, account="real",
                  entry_date=trade_date)
        log.info("真实买入 %s(%s): %d股@%.2f", name, symbol, shares, price)
    return store.get(symbol, account="real")


def real_stop_loss_alerts(store: PortfolioStore | None = None,
                          market_db: str | None = None,
                          threshold_pct: float = 7.0) -> list[dict]:
    """真实持仓 -7% 硬止损告警（#10）：收盘后检查 real 持仓"""
    from src.config import MARKET_DB_PATH
    from src.db import get_connection

    store = store or PortfolioStore()
    market_db = market_db or MARKET_DB_PATH
    latest = {}
    try:
        with get_connection(market_db, readonly=True) as conn:
            for row in conn.execute(
                "SELECT symbol, date, close FROM daily_bars "
                "WHERE (symbol, date) IN "
                "(SELECT symbol, MAX(date) FROM daily_bars GROUP BY symbol)"
            ).fetchall():
                latest[row[0]] = (row[1], row[2])
    except Exception as exc:
        # 新克隆的仓库还没有行情库：没有行情就不产生止损告警
        log.warning("行情库不可用（%s），跳过真实持仓止损检查", exc)
        return []
    alerts = []
    for p in store.all(account="real"):
        if p.shares <= 0:
            continue
        dt, cur = latest.get(p.symbol, (None, p.last_price or p.cost_price))
        loss_pct = (cur - p.cost_price) / p.cost_price * 100
        if loss_pct <= -threshold_pct:
            alerts.append({
                "symbol": p.symbol, "name": p.name, "shares": p.shares,
                "cost": p.cost_price, "price": cur,
                "loss_pct": round(loss_pct, 2),
                "action": f"🔴 真实持仓 -{threshold_pct:.0f}% 硬止损触发，建议清仓",
            })
    return alerts


def _symbol_prefix(code: str) -> str:
    """6位代码 → sh/sz/bj 前缀"""
    if code.startswith("6"):
        return "sh"
    if code.startswith(("4", "8")):
        return "bj"
    return "sz"


def _main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="真实交易记录（real 账户 + real 口径知识库）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    buy = sub.add_parser("buy", help="真实买入 → 更新 real 持仓")
    buy.add_argument("symbol", help="标的代码（如 sz002366）")
    buy.add_argument("code", help="6位代码（如 002366）")
    buy.add_argument("name", help="股票名称")
    buy.add_argument("price", type=float, help="成交价")
    buy.add_argument("shares", type=int, help="股数")
    buy.add_argument("date", help="成交日期 YYYY-MM-DD")
    buy.add_argument("note", nargs="?", default="", help="备注（买入理由）")
    buy.add_argument("--force", action="store_true",
                     help="跳过开仓门禁（仓位上限/数据质量/五因子），仅明确知情时使用")

    sell = sub.add_parser("sell", help="真实卖出 → 结算盈亏并入知识库 real 口径")
    sell.add_argument("symbol", help="标的代码")
    sell.add_argument("price", type=float, help="成交价")
    sell.add_argument("shares", type=int, help="卖出股数（0=全部）")
    sell.add_argument("date", help="成交日期 YYYY-MM-DD")
    sell.add_argument("reason", nargs="?", default="", help="卖出理由")
    sell.add_argument("--round", default="RREAL", help="轮次编号（默认 RREAL）")
    sell.add_argument("--root-cause", default="", help="亏损根因（亏损时建议填写）")
    sell.add_argument("--lesson", default="", help="教训（亏损时建议填写）")
    sell.add_argument("--mistake-type", default="", help="错误分类（亏损时建议填写）")

    sub.add_parser("list", help="查看 real 账户持仓")
    sub.add_parser("stop-alert", help="检查真实持仓 -7%% 硬止损告警")
    args = parser.parse_args()

    kb = KnowledgeBase()
    store = PortfolioStore()

    if args.cmd == "list":
        for p in store.all(account="real"):
            print(f"  {p.symbol} {p.name}: {p.shares}股 成本{p.cost_price} 现{p.last_price} — {p.note}")
        return
    if args.cmd == "stop-alert":
        alerts = real_stop_loss_alerts()
        if alerts:
            for a in alerts:
                print(f"  {a['action']} | {a['name']}({a['symbol']}) "
                      f"{a['shares']}股 成本{a['cost']} 现{a['price']} 浮亏{a['loss_pct']:.2f}%")
        else:
            print("✅ 无真实持仓触发 -7% 硬止损")
        return
    if args.cmd == "buy":
        sym = args.symbol if "." not in args.symbol else args.symbol
        if not sym.startswith(("sh", "sz", "bj")):
            sym = _symbol_prefix(args.code) + args.code
        ok, reasons = real_buy_gates(
            sym, args.code, args.name, args.price, args.shares, store=store)
        if not ok and not args.force:
            print("⛔ 真实开仓被门禁拒绝：")
            for r in reasons:
                print(f"    - {r}")
            print("  如需强行买入：--force（仅在明确知情时使用）")
            sys.exit(1)
        if reasons:
            print("⚠️ 门禁告警（--force 已放行）：")
            for r in reasons:
                print(f"    - {r}")
        real_buy(store, sym, args.name, args.price, args.shares,
                 args.date, args.note)
        pos = store.get(sym, account="real")
        print(f"✅ real 账户买入: {args.name}({sym}) "
              f"{args.shares}股@{args.price}（累计 {pos.shares if pos else 0} 股）")
        return
    if args.cmd == "sell":
        cur = store.get(args.symbol, account="real")
        if cur is None or cur.shares <= 0:
            print(f"⚠️ real 账户无 {args.symbol} 持仓，先 --buy")
            sys.exit(1)
        sell_shares = args.shares if args.shares > 0 else cur.shares
        sell_shares = min(sell_shares, cur.shares)
        r = kb.record(
            stock_code=args.symbol[2:], stock_name=cur.name,
            entry_price=cur.cost_price, exit_price=args.price,
            buy_reason=cur.note or "真实持仓",
            sell_reason=f"{args.date} 真实卖出 {sell_shares}股@{args.price} {args.reason}".strip(),
            strategy_tag="个人真实持仓", round_number=args.round, holding_days=0,
            source="real", entry_date=cur.entry_date, exit_date=args.date,
            loss_root_cause=args.root_cause,
            lesson_learned=args.lesson,
            mistake_type=args.mistake_type,
        )
        store.reduce(args.symbol, sell_shares, args.price, f"真实卖出 {args.date}",
                     account="real")
        book = "对题本" if r["book"] == "correct_book" else "错题本"
        if r["book"] == "mistake_book" and not args.mistake_type:
            print("⚠️ 亏损记录 mistake_type 为'未分类'，建议补充 --mistake-type "
                  "（如 止损不及时/追高/抄底过早）便于错误归因（#99）")
        print(f"✅ 真实卖出结算: {cur.name} {sell_shares}股@{args.price} "
              f"收益{r['return_pct']:+.2f}% → {book} [{args.round}]")


if __name__ == "__main__":
    _main()
