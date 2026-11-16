# -*- coding: utf-8 -*-
"""
盘前自动流程 — 龙虎榜简报 + 9:25 竞价终局预案（纯本地，零外部依赖）

设计要点（2026-08-05 按用户质疑落地）：
1. 竞价预案只在 9:25 集合竞价终局后生成；撮合中（9:15-9:25）不落盘、不出执行版；
2. 龙虎榜简报基于前一交易日盘后静态数据（东财公开接口，不会变化）；
3. 幂等由 run_daily.py 守护状态文件控制（同一天同阶段只跑一次）；
4. 数据源：东财 datacenter-web（龙虎榜）+ 腾讯 qt.gtimg.cn（行情），均无需安装依赖。

用法:
    python morning_flow.py                # 全跑：龙虎榜简报 + 竞价预案
    python morning_flow.py --lhb          # 只跑龙虎榜简报
    python morning_flow.py --auction      # 只跑竞价预案（9:25 后有效）
    python morning_flow.py --dry-run      # 演练：抓数据并预览，不写文件
    python morning_flow.py --date 2026-08-05   # 指定交易日（默认今天）
"""

import argparse
import json
import re
import sqlite3
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

from src.config import DB_PATH, MARKET_DB_PATH

ROOT = Path(__file__).resolve().parent
OUTPUTS = ROOT / "outputs"

# ---------------------------------------------------------------
# 数据源配置（公开接口，纯标准库）
# ---------------------------------------------------------------
LHB_API = "https://datacenter-web.eastmoney.com/api/data/v1/get"
LHB_REPORT = "RPT_DAILYBILLBOARD_DETAILSNEW"
QUOTE_API = "https://qt.gtimg.cn/q="

INDEX_SYMBOLS = ["sh000001", "sz399001", "sz399006"]
INDEX_NAMES = {"sh000001": "上证指数", "sz399001": "深证成指", "sz399006": "创业板指"}

# 预案减仓线规则（唯一来源：src/trim_rules.py，morning_flow 与 r47 共用）
from src.trim_rules import MIRROR_MARK, TRIM_RULES  # noqa: E402
from src.net import fetch_bytes  # noqa: E402


# ---------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------
def _get_json(url: str) -> dict:
    """GET JSON（#117：内置秒级重试，覆盖 DNS/连接短抖动）"""
    raw = fetch_bytes(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": "https://data.eastmoney.com/",
        },
        timeout=20,
    )
    return json.loads(raw.decode("utf-8", errors="replace"))


def _query(db_path: str, sql: str, args: tuple = ()) -> list:
    with sqlite3.connect(db_path, timeout=5) as con:
        return con.execute(sql, args).fetchall()


def _fmt_amt(yuan) -> str:
    if yuan is None:
        return "—"
    y = float(yuan)
    if abs(y) >= 1e8:
        return f"{y / 1e8:.2f} 亿"
    if abs(y) >= 1e4:
        return f"{y / 1e4:.0f} 万"
    return f"{y:.0f}"


def _weekday_cn(d: date) -> str:
    return "周一 周二 周三 周四 周五 周六 周日".split()[d.weekday()]


def _is_trading_day(day_str: str) -> bool:
    """交易日判定：优先 market.db trade_calendar，缺表回退工作日"""
    try:
        rows = _query(
            MARKET_DB_PATH,
            "SELECT is_trading FROM trade_calendar WHERE date=?",
            (day_str,),
        )
        if rows:
            return rows[0][0] == 1
    except Exception:
        pass
    return date.fromisoformat(day_str).weekday() < 5


def _prev_trading_day(day_str: str) -> str:
    """前一交易日（跨周末/节假日）"""
    try:
        rows = _query(
            MARKET_DB_PATH,
            "SELECT MAX(date) FROM trade_calendar WHERE date<? AND is_trading=1",
            (day_str,),
        )
        if rows and rows[0][0]:
            return rows[0][0]
    except Exception:
        pass
    d = date.fromisoformat(day_str) - timedelta(days=1)
    for _ in range(10):
        if d.weekday() < 5:
            return d.isoformat()
        d -= timedelta(days=1)
    return d.isoformat()


# ---------------------------------------------------------------
# 数据获取
# ---------------------------------------------------------------
def fetch_lhb_rows(trade_date: str) -> list[dict]:
    """东财龙虎榜个股级汇总（盘后静态数据）"""
    day_after = (date.fromisoformat(trade_date) + timedelta(days=1)).isoformat()
    rows: list[dict] = []
    page = 1
    while True:
        params = {
            "reportName": LHB_REPORT,
            "columns": "ALL",
            "filter": f"(TRADE_DATE>='{trade_date}')(TRADE_DATE<'{day_after}')",
            "pageNumber": str(page),
            "pageSize": "100",
            "source": "WEB",
            "client": "WEB",
        }
        url = LHB_API + "?" + urllib.parse.urlencode(params)
        data = (_get_json(url).get("result") or {}).get("data") or []
        rows.extend(data)
        if len(data) < 100:
            break
        page += 1
    return rows


def fetch_quotes(symbols: list[str]) -> dict[str, dict]:
    """腾讯实时行情快照；9:25 后 open 字段为集合竞价终局开盘价"""
    if not symbols:
        return {}
    url = QUOTE_API + ",".join(symbols)
    raw = fetch_bytes(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15).decode(
        "gbk", errors="replace")
    out: dict[str, dict] = {}
    for line in raw.strip().split(";"):
        m = re.match(r'v_(\w+)="(.*)"', line.strip())
        if not m:
            continue
        sym, f = m.group(1), m.group(2).split("~")

        def g(i: int, default: float = 0.0):
            try:
                v = f[i] if i < len(f) and f[i] else ""
                return float(v) if v else default
            except ValueError:
                return default

        out[sym] = {
            "name": f[1] if len(f) > 1 else sym,
            "price": g(3),
            "prev_close": g(4),
            "open": g(5),
            "change": g(31),
            "pct": g(32),
            "high": g(33),
            "low": g(34),
            "volume": g(36),
            "amount_wan": g(37),
            "time": f[30] if len(f) > 30 else "",
        }
    return out


# ---------------------------------------------------------------
# 本地库读取
# ---------------------------------------------------------------
def load_watchlist() -> list[dict]:
    rows = _query(
        MARKET_DB_PATH,
        "SELECT symbol, code, name, cost_price, take_profit, stop_loss, sector, note "
        "FROM watchlist WHERE status='active'",
    )
    keys = ("symbol", "code", "name", "cost_price", "take_profit", "stop_loss", "sector", "note")
    return [dict(zip(keys, r)) for r in rows]


def load_positions() -> list[dict]:
    rows = _query(
        DB_PATH,
        "SELECT account, symbol, name, shares, cost_price, note FROM positions WHERE shares>0",
    )
    keys = ("account", "symbol", "name", "shares", "cost_price", "note")
    return [dict(zip(keys, r)) for r in rows]


def mirror_buy_orders(positions: list[dict]) -> list[dict]:
    """模拟盘持仓中带'待真实跟单'标记的建仓 → 真实跟单买入清单（不新增文件）"""
    out = []
    for p in positions:
        note = p.get("note") or ""
        if p.get("account") == "sim" and p.get("shares", 0) > 0 and MIRROR_MARK in note:
            out.append({"symbol": p["symbol"], "name": p["name"],
                        "shares": p["shares"], "ref_price": p["cost_price"]})
    return out


def _prev_close_pct(symbols: list[str], prev: str, quotes: dict[str, dict]) -> dict[str, dict]:
    """前一交易日行情：本地日K为权威（真实收盘），腾讯实时昨收仅兜底

    #41：腾讯实时"昨收"可能滞后一日，不能冒充当日；本地滞后时标记 stale。
    """
    if not symbols:
        return {}
    placeholders = ",".join("?" * len(symbols))
    rows = _query(
        MARKET_DB_PATH,
        "SELECT symbol, date, close FROM daily_bars "
        f"WHERE symbol IN ({placeholders}) AND date<=? ORDER BY date DESC",
        (*symbols, prev),
    )
    hist: dict[str, list[tuple[str, float]]] = {}
    for sym, d, close in rows:
        hist.setdefault(sym, []).append((d, close))
    out: dict[str, dict] = {}
    for sym in symbols:
        q = quotes.get(sym, {})
        h = hist.get(sym, [])
        if not h:
            out[sym] = {"close": q.get("prev_close") or 0, "pct": None,
                        "date": prev, "stale": True}
            continue
        d0, c0 = h[0]
        pct = None
        if len(h) >= 2 and h[1][1]:
            pct = (c0 / h[1][1] - 1) * 100
        out[sym] = {"close": c0, "pct": pct, "date": d0, "stale": d0 != prev}
    return out


# ---------------------------------------------------------------
# 产物 1：龙虎榜简报
# ---------------------------------------------------------------
def generate_lhb_report(today: str, out_path: Path, dry_run: bool) -> int:
    prev = _prev_trading_day(today)
    print(f"[lhb] 取数日期：{prev}（前一交易日）")
    rows = fetch_lhb_rows(prev)
    if not rows:
        print(f"[lhb] 东财接口未返回 {prev} 龙虎榜数据（未发布或接口异常），本次跳过")
        return 1

    for r in rows:
        for k in (
            "BILLBOARD_NET_AMT",
            "BILLBOARD_DEAL_AMT",
            "BILLBOARD_BUY_AMT",
            "BILLBOARD_SELL_AMT",
            "CHANGE_RATE",
            "TURNOVERRATE",
            "CLOSE_PRICE",
        ):
            try:
                r[k] = float(r[k] or 0)
            except (TypeError, ValueError):
                r[k] = 0.0
    rows.sort(key=lambda r: r["BILLBOARD_NET_AMT"], reverse=True)
    # 同一股票可能因多个上榜原因出现多行（东财口径），按代码去重，保留净买额最大的一行
    seen: set[str] = set()
    dedup: list[dict] = []
    for r in rows:
        code = str(r.get("SECURITY_CODE") or "")
        if not code or code in seen:
            continue
        seen.add(code)
        dedup.append(r)
    rows = dedup
    total_deal = sum(r["BILLBOARD_DEAL_AMT"] for r in rows)

    wl = load_watchlist()
    wl_codes = {w["code"] for w in wl}
    on_board = [r for r in rows if str(r.get("SECURITY_CODE")) in wl_codes]
    all_syms: list[str] = []
    for s in INDEX_SYMBOLS + [w["symbol"] for w in wl]:
        if s not in all_syms:
            all_syms.append(s)
    quotes = fetch_quotes(all_syms)
    stats = _prev_close_pct(all_syms, prev, quotes)

    L = []
    L.append(f"# 盘前龙虎榜简报（{today} {_weekday_cn(date.fromisoformat(today))}）")
    L.append("")
    L.append(f"> 数据说明：基于 **{prev}（{_weekday_cn(date.fromisoformat(prev))}）盘后龙虎榜公开数据**（盘后静态数据，不会变化）。")
    L.append("> 数据来源：东方财富 datacenter-web 公开接口（RPT_DAILYBILLBOARD_DETAILSNEW）。")
    anchors = []
    for sym in INDEX_SYMBOLS:
        s = stats.get(sym)
        if s and s["close"]:
            txt = f"{INDEX_NAMES[sym]} {s.get('date', prev)} 收 {s['close']:.2f}"
            if s["pct"] is not None:
                txt += f"（{s['pct']:+.2f}%）"
            if s.get("stale"):
                txt += "（本地数据滞后）"
            anchors.append(txt)
    if anchors:
        L.append(f"> 行情锚点：{'；'.join(anchors)}。")
    L.append("> 口径说明：自动版为个股级汇总（净买额/涨跌幅/上榜原因/换手率）；席位级明细（机构/沪深股通/游资）需 westock/tdx 或东财营业部接口，本环境不可用。")
    L.append("")
    L.append("---")
    L.append("")

    L.append(f"## 一、观察池上榜情况（{len(wl)} 只）")
    L.append("")
    L.append("| 标的 | 收盘 | 涨跌 | 上榜情况 | 备注 |")
    L.append("|---|---|---|---|---|")
    for w in wl:
        s = stats.get(w["symbol"])
        code = w["code"]
        hit = "🔴 上榜" if any(str(r.get("SECURITY_CODE")) == code for r in on_board) else "⚪ 未上榜"
        if s:
            pct = f"{s['pct']:+.2f}%" if s["pct"] is not None else "—"
            close = f"{s['close']:.2f}"
        else:
            pct, close = "—", "—"
        note = w["sector"] or w["note"] or ""
        if s and not s["close"]:
            close = "数据缺失"
        L.append(f"| {w['name']} {w['symbol']} | {close} | {pct} | {hit} | {note} |")
    if not on_board:
        L.append("")
        L.append("> 观察池全部未上榜。")
    L.append("")
    L.append("---")
    L.append("")

    L.append(f"## 二、全市场龙虎榜（{prev}）")
    L.append("")
    L.append(f"- 共 **{len(rows)} 只**（含可转债）上榜，总上榜成交额 **{_fmt_amt(total_deal)}**。")
    L.append("")
    L.append("### 净买入 Top10")
    L.append("")
    L.append("| # | 代码 | 名称 | 净买额 | 涨跌幅 | 换手率 | 上榜原因 |")
    L.append("|---|---|---|---|---|---|---|")
    for i, r in enumerate(rows[:10], 1):
        L.append(
            f"| {i} | {r.get('SECURITY_CODE', '')} | {r.get('SECURITY_NAME_ABBR', '')} "
            f"| **{_fmt_amt(r['BILLBOARD_NET_AMT'])}** | {r['CHANGE_RATE']:+.2f}% "
            f"| {r['TURNOVERRATE']:.2f}% | {(r.get('EXPLANATION') or '')[:38]} |"
        )
    L.append("")
    L.append("### 净卖出 Top5")
    L.append("")
    L.append("| # | 代码 | 名称 | 净卖出 | 涨跌幅 | 上榜原因 |")
    L.append("|---|---|---|---|---|---|")
    for i, r in enumerate(rows[-5:][::-1], 1):
        L.append(
            f"| {i} | {r.get('SECURITY_CODE', '')} | {r.get('SECURITY_NAME_ABBR', '')} "
            f"| **{_fmt_amt(r['BILLBOARD_NET_AMT'])}** | {r['CHANGE_RATE']:+.2f}% "
            f"| {(r.get('EXPLANATION') or '')[:38]} |"
        )
    L.append("")
    L.append("---")
    L.append("")

    limit_up = [r for r in rows if r["CHANGE_RATE"] >= 9.8]
    L.append("## 三、异动提示")
    L.append("")
    if limit_up:
        names = "、".join(
            f"{r.get('SECURITY_NAME_ABBR', '')}({r['CHANGE_RATE']:+.1f}%, 换手{r['TURNOVERRATE']:.0f}%)"
            for r in limit_up[:12]
        )
        L.append(f"- 涨停/大幅异动（涨幅≥9.8%，前 12 只）：{names}")
    if on_board:
        names = "、".join(r.get("SECURITY_NAME_ABBR", "") for r in on_board)
        L.append(f"- 🔴 观察池关联：{names} 上榜，需重点核对资金面。")
    else:
        L.append("- 观察池关联：无（观察池与龙虎榜资金主线错位，以观察为主）。")
    L.append("- 净卖出居前需回避：")
    L.append(
        "、".join(
            r.get("SECURITY_NAME_ABBR", "")
            for r in sorted(rows, key=lambda r: r["BILLBOARD_NET_AMT"])[:3]
        )
        + "。"
    )
    L.append("")
    L.append("---")
    L.append("")
    L.append(f"*自动生成于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 数据为盘后静态数据 | 不构成投资建议。*")
    content = "\n".join(L) + "\n"

    if dry_run:
        print(content)
        print(f"[lhb] (dry-run) 未写文件：{out_path}")
    else:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(content, encoding="utf-8")
        print(f"[lhb] ✅ 已生成 {out_path}")
    return 0


# ---------------------------------------------------------------
# 产物 2：9:25 竞价终局预案
# ---------------------------------------------------------------
def _judge_position(pos: dict, wl_row: dict | None, open_px: float, pct: float) -> tuple[str, str]:
    sym = pos["symbol"]
    tp = wl_row["take_profit"] if wl_row else None
    sl = wl_row["stop_loss"] if wl_row else None
    cost = pos.get("cost_price") or 0
    hard_sl = round(cost * 0.93, 2) if cost else None
    action = "✅ 持有"
    if sl and open_px <= sl:
        action = "🔴 清仓"
    elif tp and open_px >= tp:
        action = "🟢 止盈"
    elif sym in TRIM_RULES and open_px <= TRIM_RULES[sym]["price"]:
        tr = TRIM_RULES[sym]
        action = f"🟡 减仓 {tr['shares']} 股（{tr['note']}）"
    detail = f"开盘 {open_px:.2f}（{pct:+.2f}%）"
    if tp:
        detail += f"；止盈 {tp:.2f}"
    if sl:
        detail += f"；止损 {sl:.2f}"
    if hard_sl:
        detail += f"；硬止损 {hard_sl:.2f}"
    return action, detail


def generate_auction_report(today: str, out_path: Path, dry_run: bool) -> int:
    positions = load_positions()
    wl = load_watchlist()
    wl_by_sym = {w["symbol"]: w for w in wl}
    syms: list[str] = []
    for s in INDEX_SYMBOLS + [p["symbol"] for p in positions] + [w["symbol"] for w in wl]:
        if s not in syms:
            syms.append(s)

    quotes = fetch_quotes(syms)
    if not quotes:
        print("[auction] 行情接口无数据，本次跳过")
        return 1

    # 关键标的（指数 + 持仓）必须有 9:25 终局开盘价，否则视为竞价未结束
    key = INDEX_SYMBOLS + [p["symbol"] for p in positions]
    ready = [s for s in key if quotes.get(s, {}).get("open")]
    if not ready:
        print("[auction] 集合竞价未结束或开盘价缺失（今开=0），9:25 后再运行")
        return 1
    if len(ready) < len(key):
        missing = [s for s in key if not quotes.get(s, {}).get("open")]
        print(f"[auction] 部分关键标的无开盘价：{missing}，继续以现有数据生成")

    def q(sym: str) -> dict:
        return quotes.get(sym, {})

    def open_pct(d: dict) -> float:
        """开盘价对昨收的涨跌幅（腾讯 pct 字段在开盘后是现价口径，不可用）"""
        if d.get("open") and d.get("prev_close"):
            return (d["open"] / d["prev_close"] - 1) * 100
        return d.get("pct", 0)

    def verdict(pct: float) -> str:
        if pct >= 1:
            return "🔴 高开"
        if pct <= -1:
            return "🟡 低开"
        return "⚪ 平开"

    L = []
    L.append(f"# 今日开盘预案（{today} {_weekday_cn(date.fromisoformat(today))}）—— 9:25 终局版")
    L.append("")
    L.append(f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 数据源：腾讯行情快照（qt.gtimg.cn）")
    L.append("> ✅ 本版基于 **9:25 集合竞价终局价**，为最终执行依据。")
    L.append("> ⚠️ 撮合中（9:15-9:25）价格会大幅变动，本流程只在终局后生成，撮合中不落盘、非执行依据。")
    L.append("")
    L.append("---")
    L.append("")

    # 竞价结果总览：指数 + 持仓 + 观察池异动
    L.append("## 一、9:25 终局竞价结果")
    L.append("")
    L.append("| 标的 | 代码 | 开盘价 | 昨收 | 幅度 | 判定 |")
    L.append("|---|---|---|---|---|---|")
    for sym in INDEX_SYMBOLS:
        d = q(sym)
        if d.get("open"):
            pct = open_pct(d)
            L.append(
                f"| {d['name']} | {sym} | {d['open']:.2f} | {d['prev_close']:.2f} "
                f"| {pct:+.2f}% | {verdict(pct)} |"
            )
    for p in positions:
        d = q(p["symbol"])
        if d.get("open"):
            pct = open_pct(d)
            L.append(
                f"| {p['name']}（{p['account']}） | {p['symbol']} | {d['open']:.2f} | {d['prev_close']:.2f} "
                f"| {pct:+.2f}% | {verdict(pct)} |"
            )
    moved = [
        w for w in wl
        if w["symbol"] not in {p["symbol"] for p in positions}
        and abs(open_pct(q(w["symbol"]))) >= 1.5
    ]
    for w in moved:
        d = q(w["symbol"])
        pct = open_pct(d)
        L.append(
            f"| {w['name']}（观察） | {w['symbol']} | {d['open']:.2f} | {d['prev_close']:.2f} "
            f"| {pct:+.2f}% | {verdict(pct)} |"
        )
    L.append("")
    L.append("---")
    L.append("")

    # 持仓处理指令
    L.append("## 二、持仓处理指令（9:25 终局判定）")
    L.append("")
    actions: list[str] = []
    if not positions:
        L.append("当前无持仓（真实仓与模拟仓均为空），无强制动作。")
        L.append("")
    for p in positions:
        d = q(p["symbol"])
        open_px = d.get("open", 0)
        action, detail = _judge_position(
            p, wl_by_sym.get(p["symbol"]), open_px, open_pct(d)
        )
        L.append(f"### {p['name']} {p['symbol']}（{p['account']} {int(p['shares'])} 股 @{p['cost_price']:.2f}）")
        L.append("")
        L.append(f"- {action}：{detail}")
        L.append("")
        if not action.startswith("✅"):
            actions.append(f"{p['name']}（{p['account']}）：{action}")
    if actions:
        L.append("**触发动作汇总：** " + "；".join(actions))
        L.append("")

    # 真实跟单·买入（模拟盘昨日收盘建仓 → 今日 9:25-9:30 执行）
    mirror = mirror_buy_orders(positions)
    if mirror:
        L.append("### 真实跟单·买入（模拟盘昨日建仓，今日 9:25-9:30 执行）")
        L.append("")
        L.append("| 标的 | 股数 | 参考价(昨收) |")
        L.append("|---|---|---|")
        for m in mirror:
            L.append(f"| {m['name']} {m['symbol']} | {m['shares']} | {m['ref_price']:.2f} |")
        L.append("")
        L.append("> 按参考价附近执行；成交后用 `python -m src.realtrade --buy` 按实际成交价回填。")
        L.append("")

    # 观察池异动
    L.append("## 三、观察池异动（无持仓）")
    L.append("")
    if moved:
        L.append("| 标的 | 开盘 | 幅度 | 备注 |")
        L.append("|---|---|---|---|")
        for w in moved:
            d = q(w["symbol"])
            pct = open_pct(d)
            note = w["sector"] or ""
            L.append(f"| {w['name']} {w['symbol']} | {d['open']:.2f} | {pct:+.2f}% | {note} |")
    else:
        L.append("无（|幅度| < 1.5% 视为正常波动）。")
    L.append("")
    L.append("---")
    L.append("")

    # 大盘环境
    L.append("## 四、大盘环境")
    L.append("")
    cyb = q("sz399006")
    sh = q("sh000001")
    if cyb.get("open"):
        cyb_pct = open_pct(cyb)
        sh_pct = open_pct(sh)
        env = "防御" if cyb_pct <= -1 else ("进攻" if cyb_pct >= 1 else "中性")
        L.append(
            f"沪指 {sh_pct:+.2f}% / 创业板指 {cyb_pct:+.2f}%"
            f"（昨收 {cyb['prev_close']:.2f}）→ 今日环境判定：**{env}**。"
        )
        L.append("防御：不追高、不抄底，只做持仓纪律管理；进攻：可关注观察池异动标的（需五因子≥85 且调研证据齐全）。")
    L.append("")
    L.append("---")
    L.append("")

    # 执行清单
    L.append("## 五、执行清单")
    L.append("")
    if actions:
        for i, a in enumerate(actions, 1):
            L.append(f"{i}. **{a}**（按 9:25 开盘价附近执行）。")
        next_no = len(actions) + 1
    else:
        L.append("1. 无强制动作，全部持有/观望。")
        next_no = 2
    L.append(f"{next_no}. 收盘 15:35 守护进程自动跑 R47 闭环，核对动作是否落地并归档。")
    L.append("")
    L.append("---")
    L.append("")
    L.append(f"*自动生成于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 数据源：腾讯行情快照（qt.gtimg.cn）| 不构成投资建议。*")
    content = "\n".join(L) + "\n"

    if dry_run:
        print(content)
        print(f"[auction] (dry-run) 未写文件：{out_path}")
    else:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(content, encoding="utf-8")
        print(f"[auction] ✅ 已生成 {out_path}")
    return 0


# ---------------------------------------------------------------
# 入口
# ---------------------------------------------------------------
def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="盘前自动流程：龙虎榜简报 + 9:25 竞价终局预案")
    parser.add_argument("--lhb", action="store_true", help="只生成龙虎榜简报")
    parser.add_argument("--auction", action="store_true", help="只生成竞价预案（9:25 后有效）")
    parser.add_argument("--dry-run", action="store_true", help="演练：抓数据预览，不写文件")
    parser.add_argument("--date", default=date.today().isoformat(), help="交易日 YYYY-MM-DD（默认今天）")
    args = parser.parse_args()

    today = args.date
    if not _is_trading_day(today):
        print(f"{today} 非交易日，盘前流程跳过")
        return

    rc = 0
    if args.lhb:
        rc = generate_lhb_report(today, OUTPUTS / "龙虎榜简报" / f"龙虎榜简报_{today}.md", args.dry_run)
    elif args.auction:
        rc = generate_auction_report(today, OUTPUTS / "竞价预案" / f"竞价预案_{today}.md", args.dry_run)
    else:
        rc = generate_lhb_report(today, OUTPUTS / "龙虎榜简报" / f"龙虎榜简报_{today}.md", args.dry_run)
        if rc == 0:
            rc = generate_auction_report(today, OUTPUTS / "竞价预案" / f"竞价预案_{today}.md", args.dry_run)
    sys.exit(rc)


if __name__ == "__main__":
    main()
