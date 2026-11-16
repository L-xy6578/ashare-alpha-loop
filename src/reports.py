"""
多周期收益测量报表 — "先测量，再谈提升"

原则（2026-08-05 定）：当前样本不足（约 11 笔），不再叠加新功能，
先建立收益测量体系——用真实数据回答"周/月/季/年赚了多少、胜率多少、
哪个策略在贡献"。

口径说明（防止误读）：
    - 已实现收益：来自知识库对题本/错题本（每笔收益率%，累计=百分点合计，
      非资金收益率——因为历史记录未存每笔投入金额）
    - 当前浮盈：positions 表 × market.db 最新收盘（资金口径，可算金额）
    - 分口径：sim（模拟盘推演）与 real（真实成交）独立统计；KPI-07 只认 real

用法:
    python -m src.reports                     # 生成 outputs/收盘报表/收益报表_YYYY-MM-DD.md
    python -m src.reports --today 2026-08-05  # 指定测量日（测试/补跑）
    python -m src.reports --out my.md --quiet # 自定义输出/静默
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from src.config import DB_PATH, MARKET_DB_PATH
from src.db import get_connection
from src.logger import get_logger

log = get_logger(__name__)

SIM_TOTAL_ASSETS = 30000.0   # 模拟盘总资产基准（与 TradeExecutor 一致）


# ============================================================
# 周期边界
# ============================================================

def _iso(d: date) -> str:
    return d.isoformat()


def _kpi_names(kb_db: str = DB_PATH) -> str:
    """KPI-07 跟踪中的推荐标的（kpi_tracking 表，逗号分隔）"""
    try:
        with get_connection(kb_db, readonly=True) as conn:
            rows = conn.execute(
                "SELECT stock_name FROM kpi_tracking ORDER BY id"
            ).fetchall()
        return "、".join(r[0] for r in rows)
    except Exception:
        return ""


def period_ranges(today: date) -> dict[str, dict]:
    """返回各周期的 (label, 本期起, 本期止, 上期起, 上期止)

    - 周：周一~周日
    - 月：自然月
    - 季：自然季（Q1=1-3月）
    - 年：自然年（本期截至 today，上期为全年）
    """
    out = {}

    # 周
    monday = today - timedelta(days=today.weekday())
    out["weekly"] = {
        "label": "本周",
        "start": _iso(monday), "end": _iso(monday + timedelta(days=6)),
        "prev_start": _iso(monday - timedelta(days=7)),
        "prev_end": _iso(monday - timedelta(days=1)),
    }

    # 月
    month_start = today.replace(day=1)
    next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    month_end = next_month - timedelta(days=1)
    prev_month_end = month_start - timedelta(days=1)
    prev_month_start = prev_month_end.replace(day=1)
    out["monthly"] = {
        "label": "本月",
        "start": _iso(month_start), "end": _iso(month_end),
        "prev_start": _iso(prev_month_start), "prev_end": _iso(prev_month_end),
    }

    # 季
    q = (today.month - 1) // 3
    q_start = date(today.year, q * 3 + 1, 1)
    q_end = (date(today.year, 12, 31) if q == 3
             else date(today.year, q * 3 + 4, 1) - timedelta(days=1))
    prev_q_end = q_start - timedelta(days=1)
    prev_q_start = date(prev_q_end.year, (prev_q_end.month - 1) // 3 * 3 + 1, 1)
    out["quarterly"] = {
        "label": "本季",
        "start": _iso(q_start), "end": _iso(q_end),
        "prev_start": _iso(prev_q_start), "prev_end": _iso(prev_q_end),
    }

    # 年
    out["yearly"] = {
        "label": "今年",
        "start": _iso(date(today.year, 1, 1)), "end": _iso(today),
        "prev_start": _iso(date(today.year - 1, 1, 1)),
        "prev_end": _iso(date(today.year - 1, 12, 31)),
    }
    return out


# ============================================================
# 数据读取
# ============================================================

def load_trades(kb_db: str) -> list[dict]:
    """从对题本/错题本读取全部已实现交易（含 source 口径）"""
    trades = []
    with get_connection(kb_db, readonly=True) as conn:
        for row in conn.execute(
            """SELECT stock_code, stock_name, strategy_tag, round_number,
                      entry_price, exit_price,
                      profit_pct AS pnl, source, created_at, 'correct' AS book
               FROM correct_book
               UNION ALL
               SELECT stock_code, stock_name, strategy_tag, round_number,
                      entry_price, exit_price,
                      loss_pct AS pnl, source, created_at, 'mistake' AS book
               FROM mistake_book
               ORDER BY created_at"""
        ).fetchall():
            trades.append({
                "code": row[0], "name": row[1], "tag": row[2], "round": row[3],
                "entry": row[4], "exit": row[5],
                "pnl": float(row[6]), "source": row[7],
                "date": (row[8] or "")[:10],
                "book": row[9],
            })
    return trades


def dedupe_trades(trades: list[dict]) -> tuple[list[dict], int]:
    """去重：同（代码, 入场价, 出场价）保留最早一笔（R45/R46 同价重复轮次）

    Returns:
        (去重后列表, 去除条数)
    """
    seen: dict[tuple, dict] = {}
    dupes = 0
    for t in sorted(trades, key=lambda x: (x["date"], x["round"])):
        key = (t["code"], t["entry"], t["exit"])
        if key in seen:
            dupes += 1
            continue
        seen[key] = t
    return list(seen.values()), dupes


def load_floating(positions, market_db: str) -> list[dict]:
    """持仓浮盈（按 market.db 最新收盘）"""
    latest = {}
    with get_connection(market_db, readonly=True) as conn:
        for row in conn.execute(
            "SELECT symbol, date, close FROM daily_bars "
            "WHERE (symbol, date) IN "
            "(SELECT symbol, MAX(date) FROM daily_bars GROUP BY symbol)"
        ).fetchall():
            latest[row[0]] = (row[1], row[2])
    out = []
    for p in positions:
        if p.shares <= 0:
            continue
        dt, cur = latest.get(p.symbol, (None, p.last_price or p.cost_price))
        out.append({
            "account": p.account, "symbol": p.symbol, "name": p.name,
            "shares": p.shares, "cost": p.cost_price, "price": cur,
            "date": dt, "pnl_pct": (cur - p.cost_price) / p.cost_price * 100,
            "pnl_amount": p.shares * (cur - p.cost_price),
        })
    return out


# ============================================================
# 聚合
# ============================================================

def aggregate(trades: list[dict], start: str, end: str) -> dict:
    """区间聚合：笔数/胜率/合计收益%/平均单笔%"""
    picked = [t for t in trades if start <= t["date"] <= end]
    n = len(picked)
    if n == 0:
        return {"n": 0, "win_rate": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0}
    # #56：胜率按账本语义（对题本=胜、错题本=负）——pnl>=0 会把 0% 的
    # 错题记录（如拉卡拉"交易员正确拒绝"）和占位记录误计为胜，虚高胜率
    wins = sum(1 for t in picked if t.get("book", "correct") == "correct")
    total = round(sum(t["pnl"] for t in picked), 2)
    return {
        "n": n,
        "win_rate": round(wins / n * 100, 1),
        "total_pnl": total,
        "avg_pnl": round(total / n, 2),
    }


def strategy_contributions(trades: list[dict], days: int = 90,
                           source: str = "sim", today: date | None = None) -> list[dict]:
    """策略贡献（近 N 天，某口径）：笔数/胜率/合计收益/平均收益

    today：统计基准日（默认今天）。补跑历史报表必须传，否则会按"当前时间"截断。
    """
    ref = today or date.today()
    cutoff = (ref - timedelta(days=days)).isoformat()
    picked = [t for t in trades if t["source"] == source and t["date"] >= cutoff]
    groups: dict[str, list[tuple[float, str]]] = {}
    for t in picked:
        groups.setdefault(t["tag"], []).append((t["pnl"], t.get("book", "correct")))
    out = []
    for tag, pnls in groups.items():
        if not tag:
            continue
        n = len(pnls)
        wins = sum(1 for _, book in pnls if book == "correct")
        out.append({
            "tag": tag, "n": n,
            "win_rate": round(wins / n * 100, 1),
            "total": round(sum(p for p, _ in pnls), 2),
            "avg": round(sum(p for p, _ in pnls) / n, 2),
            "insufficient": n < 3,
        })
    out.sort(key=lambda x: -x["total"])
    return out


def build_report(kb_db: str = DB_PATH, market_db: str = MARKET_DB_PATH,
                 today: date | None = None) -> dict:
    """生成完整测量报表数据"""
    from src.portfolio import PortfolioStore

    today = today or date.today()
    trades = load_trades(kb_db)
    trades, dupes = dedupe_trades(trades)
    ranges = period_ranges(today)
    report = {"date": _iso(today), "ranges": ranges, "trades": trades,
              "dupes_removed": dupes}

    # 各周期 × 各口径
    report["periods"] = {}
    for key, r in ranges.items():
        report["periods"][key] = {
            "label": r["label"],
            "sim": {
                "cur": aggregate([t for t in trades if t["source"] == "sim"],
                                 r["start"], r["end"]),
                "prev": aggregate([t for t in trades if t["source"] == "sim"],
                                  r["prev_start"], r["prev_end"]),
            },
            "real": {
                "cur": aggregate([t for t in trades if t["source"] == "real"],
                                 r["start"], r["end"]),
                "prev": aggregate([t for t in trades if t["source"] == "real"],
                                  r["prev_start"], r["prev_end"]),
            },
        }

    # 累计（全部历史）
    report["cumulative"] = {
        "sim": aggregate([t for t in trades if t["source"] == "sim"], "1970-01-01", "2099-12-31"),
        "real": aggregate([t for t in trades if t["source"] == "real"], "1970-01-01", "2099-12-31"),
    }

    # 当前浮盈
    store = PortfolioStore(kb_db)
    report["floating"] = load_floating(store.all(), market_db)

    # 策略贡献（sim，近90天；real 样本单独列）
    report["strategies"] = {
        "sim": strategy_contributions(trades, days=90, source="sim", today=today),
        "real": strategy_contributions(trades, days=365, source="real", today=today),
    }
    # 真实持仓 -7% 硬止损告警（#10）
    from src.realtrade import real_stop_loss_alerts

    report["real_stop_alerts"] = real_stop_loss_alerts()

    # 指数化组合（2026-11-11 策略路线变更后的主口径：sim 记账、真实 ETF 收盘价）
    try:
        from src.index_portfolio import summary as index_summary

        report["index_portfolio"] = index_summary(market_db)
    except Exception as exc:   # 组合库异常不阻断历史测量报表
        log.warning("指数化组合概况读取失败: %s", exc)
        report["index_portfolio"] = None
    return report


# ============================================================
# 渲染
# ============================================================

def _index_portfolio_lines(st: dict | None) -> list[str]:
    """指数化组合小节：KPI 三项 + 持仓明细 + 再平衡提示 + 兜底标注"""
    lines = ["### 3.1 指数化组合（sim 记账 · 真实 ETF 收盘价）", ""]
    if not st:
        lines.append("（尚未建仓：待收盘闭环首次采集到 ETF 日K 后生成）")
        return lines
    bench = st["benchmark_symbol"][-3:]
    bench_txt = ("—" if st["benchmark_return_pct"] is None
                 else f"{st['benchmark_return_pct']:+.2f}%")
    excess_txt = ("—" if st["excess_pct"] is None
                  else f"{st['excess_pct']:+.2f}%")
    ratio_txt = ("—" if st["outperform_ratio"] is None
                 else f"{st['outperform_ratio']:.1f}%")
    lines.append("| 项目 | 数值 |")
    lines.append("|------|------|")
    lines.append(f"| 数据日 / 建仓日 | {st['date']} / {st['inception_date']} |")
    lines.append(f"| 总资产（本金 {st['initial_cash']:.0f}） | "
                 f"{st['total_value']:.0f} 元 |")
    lines.append(f"| 现金 / 持仓市值 | {st['cash']:.0f} / {st['market_value']:.0f} 元 |")
    lines.append(f"| ① 累计收益（vs 沪深{bench}） | "
                 f"{st['total_return_pct']:+.2f}% / {bench_txt}（超额 {excess_txt}） |")
    lines.append(f"| ② 胜率（跑赢基准的交易日占比） | {ratio_txt}"
                 f"（{st['outperform_days']}/{st['compare_days']} 个交易日） |")
    lines.append("| ② 交易胜率 | 不适用（指数化无平仓交易） |")
    lines.append(f"| ③ 最大回撤 | {st['max_drawdown_pct']:.2f}% |")
    lines.append("")
    if st["holdings"]:
        name_by = {t["symbol"]: t for t in st["targets"]}
        lines.append("| 标的 | 角色 | 目标金额 | 份额 | 收盘价 | 市值 | 权重 |")
        lines.append("|------|------|:---:|:---:|:---:|:---:|:---:|")
        for h in st["holdings"]:
            t = name_by.get(h["symbol"], {})
            lines.append(
                f"| {t.get('name', h['symbol'])}({h['symbol']}) | {t.get('role', '—')} | "
                f"{h['cost_amount']:.0f} | {h['shares']} | {h['close']:.3f} | "
                f"{h['market_value']:.0f} | {h['weight']:.2f}% |"
            )
        lines.append("")
    lines.append(f"- 再平衡：每季度第一个交易日拉回等权（各 6,000）；"
                 f"下次窗口 {st['next_rebalance']}")
    if st["rebuilds"]:
        latest = st["rebuilds"][0]
        lines.append(f"- 最近一次：{latest['date']} {latest['kind']}（{latest['note']}）")
    lines.append(f"- 正式净值序列：{st['official_days']} 个交易日（真实 ETF 收盘价）")
    if st["proxy_dates"]:
        lines.append(f"- ⚠️ 指数代打兜底（ETF 行情缺失，**不计入正式净值序列**）："
                     f"{'、'.join(st['proxy_dates'][:10])}"
                     f"{' 等' if len(st['proxy_dates']) > 10 else ''}")
    if st["bar_coverage"]:
        cov = "；".join(
            f"{c['symbol']} {c['n']} 根（{c['first_date']}~{c['last_date']}，{c['adjust']}）"
            for c in st["bar_coverage"])
        lines.append(f"- ETF 日K覆盖：{cov}")
    lines.append("- 记账口径：sim 模拟记录；真实下单由用户自己在券商操作，"
                 "不写入 knowledge.db 的 real 口径。")
    return lines


def render_markdown(report: dict) -> str:
    lines = [f"# 收益测量报表（{report['date']}）", ""]
    note = "> 口径：2026-11-11 起策略路线为指数化（五因子选股退役），"
    note += "净值/持仓市值/盈亏一律用真实 ETF 收盘价（sim 记账，真实下单由用户自办）；"
    note += "历史已实现收益来自知识库对题本/错题本（每笔收益率%，累计为百分点合计），仅存档不参与新统计。"
    if report.get("dupes_removed"):
        note += f" 已去重 {report['dupes_removed']} 笔重复记录（同代码同价，保留最早一轮）。"
    lines.append(note)
    lines.append("")

    def _table(src_label: str, data: dict) -> list[str]:
        t = [
            "| 周期 | 区间 | 笔数 | 胜率 | 合计收益% | 平均单笔% | 上期合计% |",
            "|------|------|:---:|:---:|:---:|:---:|:---:|",
        ]
        for key in ("weekly", "monthly", "quarterly", "yearly"):
            r = report["ranges"][key]
            d = data[key][src_label.lower()]
            cur, prev = d["cur"], d["prev"]
            if cur["n"] == 0 and prev["n"] == 0:
                continue
            t.append(
                f"| {r['label']} | {r['start']}~{r['end']} | {cur['n']} | "
                f"{cur['win_rate']}% | {cur['total_pnl']:+.2f} | "
                f"{cur['avg_pnl']:+.2f} | {prev['total_pnl']:+.2f} |"
            )
        t.append(
            f"| 累计 | 全部历史 | {report['cumulative'][src_label.lower()]['n']} | "
            f"{report['cumulative'][src_label.lower()]['win_rate']}% | "
            f"{report['cumulative'][src_label.lower()]['total_pnl']:+.2f} | "
            f"{report['cumulative'][src_label.lower()]['avg_pnl']:+.2f} | — |"
        )
        t.append("")
        return t

    lines.append("## 一、多周期已实现收益（模拟盘 sim）")
    lines.extend(_table("sim", report["periods"]))
    lines.append("## 二、多周期已实现收益（真实盘 real）")
    lines.extend(_table("real", report["periods"]))

    lines.append("## 三、当前持仓浮盈")
    lines.append("")
    lines.extend(_index_portfolio_lines(report.get("index_portfolio")))
    lines.append("")
    lines.append("### 3.2 历史遗留个股持仓（五因子已退役 · 仅存档）")
    lines.append("")
    sim_pos = [f for f in report["floating"] if f["account"] == "sim"]
    real_pos = [f for f in report["floating"] if f["account"] == "real"]
    if sim_pos:
        lines.append("| 标的 | 股数 | 成本 | 现价 | 浮盈% | 浮盈金额 | 数据日 |")
        lines.append("|------|:---:|:---:|:---:|:---:|:---:|:---:|")
        total_val = sum(f["shares"] * f["price"] for f in sim_pos)
        total_cost = sum(f["shares"] * f["cost"] for f in sim_pos)
        for f in sim_pos:
            lines.append(
                f"| {f['name']} | {f['shares']} | {f['cost']:.2f} | {f['price']:.2f} | "
                f"{f['pnl_pct']:+.2f}% | {f['pnl_amount']:+.0f} | {f['date'] or '—'} |"
            )
        lines.append(
            f"| **合计** | — | — | — | **{(total_val-total_cost)/total_cost*100:+.2f}%** | "
            f"**{total_val-total_cost:+.0f}** | 市值 {total_val:.0f}/{SIM_TOTAL_ASSETS:.0f} "
            f"({total_val/SIM_TOTAL_ASSETS*100:.1f}%) |"
        )
    else:
        lines.append("（无模拟盘持仓）")
    if real_pos:
        for f in real_pos:
            lines.append(f"- real {f['name']}: {f['pnl_pct']:+.2f}%")
    else:
        lines.append("")
        lines.append("真实盘：无在持（见第二节）")
    lines.append("")

    lines.append("## 四、策略贡献（近90天 · sim）")
    lines.append("")
    sim_strat = report["strategies"]["sim"]
    if sim_strat:
        lines.append("| 策略 | 笔数 | 胜率 | 合计收益% | 平均% | 样本 |")
        lines.append("|------|:---:|:---:|:---:|:---:|:---:|")
        for s in sim_strat:
            note = "⚠️不足" if s["insufficient"] else "✅"
            lines.append(
                f"| {s['tag']} | {s['n']} | {s['win_rate']}% | {s['total']:+.2f} | "
                f"{s['avg']:+.2f} | {note} |"
            )
    else:
        lines.append("（近90天无 sim 交易记录）")
    lines.append("")
    lines.append("## 五、测量日历")
    lines.append("")
    names = _kpi_names() or "（无）"
    lines.append(f"- KPI-07 首次评估：**2026-08-24**（R48-R52 推荐：{names}，"
                 "推荐日起第 22 个交易日跟踪截止，休市日不计）")
    lines.append("- 报表生成：每日收盘后自动产出本文件并归档推送")
    lines.append("")
    lines.append("## 六、真实持仓硬止损告警（-7%）")
    lines.append("")
    if report["real_stop_alerts"]:
        for a in report["real_stop_alerts"]:
            lines.append(f"- {a['action']} | {a['name']}({a['symbol']}) "
                         f"{a['shares']}股 成本{a['cost']} 现{a['price']} "
                         f"浮亏{a['loss_pct']:.2f}%")
    else:
        lines.append("（无真实持仓触发 -7% 硬止损）")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="多周期收益测量报表")
    parser.add_argument("--today", default=None, help="测量日 YYYY-MM-DD（默认今天）")
    parser.add_argument("--out", default=None, help="输出路径（默认 outputs/收盘报表/收益报表_YYYY-MM-DD.md）")
    parser.add_argument("--quiet", action="store_true", help="不打印，只写文件")
    args = parser.parse_args()

    today = date.fromisoformat(args.today) if args.today else date.today()
    report = build_report(today=today)
    md = render_markdown(report)

    out = Path(args.out) if args.out else (
        Path("outputs") / "收盘报表" / f"收益报表_{_iso(today)}.md"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    if not args.quiet:
        print(md)
    log.info("收益测量报表已生成: %s", out)


if __name__ == "__main__":
    main()
