"""
指数化组合（sim 记账）— 2026-11-11 策略路线变更落地

背景：五因子选股退役，转指数化。配置写死、一次建满、不分批（分批本质是择时，
择时同样没有证据支持）。真实下单由用户自己在券商操作，本模块只记 sim 账：
"该买什么、值多少钱"，**不写 knowledge.db 的 real 口径**。

配置（用户 2026-11-11 拍板，不留 TBD）：
    510300 沪深300ETF  6,000 元  role=core
    510500 中证500ETF  6,000 元  role=mid
    510880 红利ETF     6,000 元  role=defense
    合计 18,000，留现金 12,000（总资产 30,000，沿用单只 ≤6,000 / 最多 3 只 / 总仓 ≤18,000）
    再平衡：每季度第一个交易日拉回等权（各 6,000）

数据：真实 ETF 日 K（东财，前复权）落 data/index_portfolio.db 的 etf_bars；
净值、持仓市值、盈亏一律用真实 ETF 收盘价。index_proxy（指数点位代打）只在
"某天 ETF 行情缺失"时临时兜底：nav.proxy=1 单独存放、报表显式标注，
**不计入正式净值序列**（回撤/跑赢基准统计只取 proxy=0）。

用法:
    python -m src.index_portfolio --collect     # 采集 ETF 日K + 建仓/再平衡 + 重算净值
    python -m src.index_portfolio --summary     # 只打印当前组合概况（不采集、不写库）
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.request
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path

from src.config import INDEX_DB_PATH, MARKET_DB_PATH
from src.logger import get_logger

log = get_logger(__name__)

# ============================================================
# 配置（写死，冻结：策略路线变更时由用户明确批准后才改）
# ============================================================

ETF_TARGETS: tuple[dict, ...] = (
    {"symbol": "sh510300", "code": "510300", "name": "沪深300ETF",
     "role": "core", "target_amount": 6000.0},
    {"symbol": "sh510500", "code": "510500", "name": "中证500ETF",
     "role": "mid", "target_amount": 6000.0},
    {"symbol": "sh510880", "code": "510880", "name": "红利ETF",
     "role": "defense", "target_amount": 6000.0},
)

INITIAL_CASH = 30000.0      # 模拟账户总资产（与 decision_account 同口径）
ETF_BUDGET = 18000.0        # 组合目标市值合计（单只 6,000 × 3）
CASH_FLOOR = 12000.0        # 常留现金下限
LOT = 100                   # ETF 最小交易单位（100 份）
BENCHMARK_SYMBOL = "sh000300"   # 沪深300：跑赢基准判定与 index_proxy 代打基准

KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "Chrome/120 Safari/537.36")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS targets (
    symbol TEXT PRIMARY KEY,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    target_amount REAL NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS etf_bars (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL, close REAL, high REAL, low REAL,
    volume REAL, amount REAL,
    adjust TEXT NOT NULL DEFAULT 'qfq',
    source TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    inception_date TEXT NOT NULL,
    initial_cash REAL NOT NULL,
    cash REAL NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS holdings (
    date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    shares INTEGER NOT NULL,
    cost_amount REAL NOT NULL DEFAULT 0,
    close REAL,
    market_value REAL,
    weight REAL,
    price_source TEXT NOT NULL DEFAULT 'etf',
    PRIMARY KEY (date, symbol)
);

CREATE TABLE IF NOT EXISTS nav (
    date TEXT PRIMARY KEY,
    cash REAL NOT NULL,
    market_value REAL NOT NULL,
    total_value REAL NOT NULL,
    nav REAL NOT NULL,
    benchmark_close REAL,
    proxy INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rebalance_log (
    date TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
"""


# ============================================================
# 建库 / 配置
# ============================================================

def _open(db_path: str) -> sqlite3.Connection:
    """打开连接（autocommit + 调用方 with closing 确保句柄释放）"""
    conn = sqlite3.connect(db_path, timeout=10)
    conn.isolation_level = None
    return conn


def init_db(db_path: str = INDEX_DB_PATH) -> str:
    """建表 + 写入写死的 ETF 配置（幂等：已存在则按配置覆盖目标金额）"""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with closing(_open(db_path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        for t in ETF_TARGETS:
            conn.execute(
                """INSERT INTO targets (symbol, code, name, role, target_amount, enabled)
                   VALUES (?,?,?,?,?,1)
                   ON CONFLICT(symbol) DO UPDATE SET
                     code=excluded.code, name=excluded.name, role=excluded.role,
                     target_amount=excluded.target_amount, enabled=1""",
                (t["symbol"], t["code"], t["name"], t["role"], t["target_amount"]),
            )
    return db_path


def targets(db_path: str = INDEX_DB_PATH) -> list[dict]:
    """当前启用的配置（写死三只 ETF）"""
    with closing(_open(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM targets WHERE enabled=1 ORDER BY rowid"
        ).fetchall()
    return [dict(r) for r in rows]


# ============================================================
# ETF 日K采集（东财 · 前复权）
# ============================================================

def _secid(symbol: str) -> str:
    """sh510300 → 1.510300；sz159xxx → 0.159xxx"""
    return f"{'1' if symbol.startswith('sh') else '0'}.{symbol[2:]}"


def _get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_etf_bars(symbol: str, beg: str, end: str, retries: int = 3) -> list[dict]:
    """拉 ETF 日K（前复权）。beg/end: YYYY-MM-DD"""
    qs = (f"?secid={_secid(symbol)}&klt=101&fqt=1"
          f"&beg={beg.replace('-', '')}&end={end.replace('-', '')}&lmt=10000"
          "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57")
    last: Exception | None = None
    for _ in range(retries):
        try:
            payload = json.loads(_get(KLINE_URL + qs).decode())
            data = payload.get("data") or {}
            out = []
            for k in data.get("klines") or []:
                p = k.split(",")
                if len(p) < 6 or p[2] in ("", "-"):
                    continue
                out.append({
                    "symbol": symbol, "date": p[0],
                    "open": float(p[1]), "close": float(p[2]),
                    "high": float(p[3]), "low": float(p[4]),
                    "volume": float(p[5] or 0),
                    "amount": float(p[6]) if len(p) > 6 and p[6] not in ("", "-") else None,
                    "adjust": "qfq", "source": "eastmoney",
                })
            return out
        except Exception as exc:   # 网络/解析失败重试
            last = exc
    raise RuntimeError(f"{symbol} ETF 日K拉取失败: {last}")


def save_bars(rows: list[dict], db_path: str = INDEX_DB_PATH) -> int:
    """写入/覆盖 ETF 日K（前复权）"""
    if not rows:
        return 0
    with closing(_open(db_path)) as conn:
        conn.executemany(
            """INSERT INTO etf_bars (symbol, date, open, close, high, low,
                                     volume, amount, adjust, source)
               VALUES (:symbol,:date,:open,:close,:high,:low,:volume,:amount,:adjust,:source)
               ON CONFLICT(symbol, date) DO UPDATE SET
                 open=excluded.open, close=excluded.close, high=excluded.high,
                 low=excluded.low, volume=excluded.volume, amount=excluded.amount,
                 adjust=excluded.adjust, source=excluded.source""",
            rows,
        )
    return len(rows)


def collect_bars(end: str, db_path: str = INDEX_DB_PATH,
                 fetcher=None) -> dict:
    """按"已有最新日期之后"增量采集三只 ETF 日K；返回 {symbol: 新增条数}"""
    fetcher = fetcher or fetch_etf_bars
    out: dict[str, int] = {}
    with closing(_open(db_path)) as conn:
        for t in ETF_TARGETS:
            sym = t["symbol"]
            row = conn.execute(
                "SELECT MAX(date) FROM etf_bars WHERE symbol=?", (sym,)
            ).fetchone()
            last = row[0] if row else None
            if last:
                # 回看 3 天覆盖（收盘前重跑时同日 bar 可能还是盘中值）
                beg = (date.fromisoformat(last) - timedelta(days=3)).isoformat()
            else:
                beg = (date.fromisoformat(end) - timedelta(days=400)).isoformat()
            bars = fetcher(sym, beg, end)
            out[sym] = save_bars(bars, db_path)
    return out


# ============================================================
# 建仓 / 季度再平衡 / 净值
# ============================================================

def _closes(conn: sqlite3.Connection, day: str) -> dict[str, float]:
    """某日三只 ETF 收盘价（缺失的标的不会出现在结果里）"""
    rows = conn.execute(
        "SELECT symbol, close FROM etf_bars WHERE date=?"
        " AND symbol IN (?,?,?)",
        (day, *(t["symbol"] for t in ETF_TARGETS)),
    ).fetchall()
    return {r[0]: r[1] for r in rows if r[1]}


def _lot_shares(amount: float, price: float) -> int:
    """按金额买整手（100 份）"""
    if not price or price <= 0:
        return 0
    return int(amount / price / LOT) * LOT


def _ensure_built(conn: sqlite3.Connection, day: str) -> bool:
    """一次建满：首次运行时按当日真实收盘价买入三只 ETF（不分批）"""
    if conn.execute("SELECT 1 FROM account WHERE id=1").fetchone():
        return False
    closes = _closes(conn, day)
    if len(closes) < len(ETF_TARGETS):
        missing = [t["code"] for t in ETF_TARGETS if t["symbol"] not in closes]
        raise RuntimeError(
            f"{day} ETF 行情缺失（{'、'.join(missing)}），暂不建仓")
    shares = {t["symbol"]: _lot_shares(t["target_amount"], closes[t["symbol"]])
              for t in ETF_TARGETS}
    spent = sum(shares[s] * closes[s] for s in shares)
    cash = round(INITIAL_CASH - spent, 2)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """INSERT INTO account (id, inception_date, initial_cash, cash, updated_at)
           VALUES (1,?,?,?,?)""",
        (day, INITIAL_CASH, cash, now),
    )
    conn.execute(
        """INSERT OR REPLACE INTO rebalance_log (date, kind, note, detail, created_at)
           VALUES (?,?,?,?,?)""",
        (day, "BUILD", "一次建满（不分批）",
         json.dumps({"shares": shares, "cash": cash, "closes": closes},
                    ensure_ascii=False), now),
    )
    return True


def _quarter_first_trading_day(conn: sqlite3.Connection, day: str) -> bool:
    """是否为季度第一个交易日（以 ETF 日K 实际日期为交易日历）"""
    d = date.fromisoformat(day)
    if d.month not in (1, 4, 7, 10):
        return False
    q_start = d.isoformat()
    row = conn.execute(
        "SELECT MIN(date) FROM etf_bars WHERE date>=? AND date<=?", (q_start, day)
    ).fetchone()
    return bool(row and row[0] == day)


def _apply_rebalance(conn: sqlite3.Connection, day: str) -> bool:
    """季度第一个交易日拉回等权（各 6,000 等额），同日幂等"""
    if not _quarter_first_trading_day(conn, day):
        return False
    if conn.execute("SELECT 1 FROM rebalance_log WHERE date=?", (day,)).fetchone():
        return False
    acc = conn.execute("SELECT cash, inception_date FROM account WHERE id=1").fetchone()
    if not acc or day <= acc[1]:
        return False
    closes = _closes(conn, day)
    if len(closes) < len(ETF_TARGETS):
        return False
    # 上一交易日的份额以最近一次建仓/再平衡记录为准（不依赖 holdings 的写入顺序）
    prev_log = conn.execute(
        "SELECT detail FROM rebalance_log WHERE date<? ORDER BY date DESC LIMIT 1",
        (day,),
    ).fetchone()
    old_shares = {}
    if prev_log:
        old_shares = dict(json.loads(prev_log[0] or "{}").get("shares") or {})
    new_shares = {t["symbol"]: _lot_shares(t["target_amount"], closes[t["symbol"]])
                  for t in ETF_TARGETS}
    cash = acc[0]
    for t in ETF_TARGETS:
        sym = t["symbol"]
        cash += (old_shares.get(sym, 0) - new_shares[sym]) * closes[sym]
    cash = round(cash, 2)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE account SET cash=?, updated_at=? WHERE id=1", (cash, now))
    conn.execute(
        """INSERT INTO rebalance_log (date, kind, note, detail, created_at)
           VALUES (?,?,?,?,?)""",
        (day, "QUARTERLY", "季度首个交易日拉回等权（各 6,000）",
         json.dumps({"shares": new_shares, "prev_shares": old_shares,
                     "cash": cash, "closes": closes}, ensure_ascii=False), now),
    )
    return True


def _benchmark_closes(market_db: str, start: str, end: str) -> dict[str, float]:
    """沪深300 收盘（market.db daily_bars，随每日采集落库）"""
    if not Path(market_db).exists():
        return {}
    with closing(_open(market_db)) as conn:
        rows = conn.execute(
            """SELECT date, close FROM daily_bars
               WHERE symbol=? AND date BETWEEN ? AND ? ORDER BY date""",
            (BENCHMARK_SYMBOL, start, end),
        ).fetchall()
    return {r[0]: r[1] for r in rows if r[1]}


def _trading_dates(conn: sqlite3.Connection, start: str, end: str) -> list[str]:
    """交易日历：ETF 日K 的日期并集（指数化组合自身口径）"""
    rows = conn.execute(
        "SELECT DISTINCT date FROM etf_bars WHERE date BETWEEN ? AND ? ORDER BY date",
        (start, end),
    ).fetchall()
    return [r[0] for r in rows]


def rebuild_nav(conn: sqlite3.Connection, market_db: str = MARKET_DB_PATH) -> dict:
    """重放建仓/再平衡，重算全历史净值（同日覆盖，幂等）

    正式净值序列（proxy=0）只取三只 ETF 都有真实收盘价的交易日；
    缺失日若基准可用，写 proxy=1 的指数代打行（仅标注，不参与统计）。
    """
    acc = conn.execute(
        "SELECT inception_date, initial_cash, cash FROM account WHERE id=1"
    ).fetchone()
    if not acc:
        return {"days": 0, "proxy_days": 0}
    inception, initial_cash, cash_now = acc
    logs = conn.execute(
        "SELECT date, detail FROM rebalance_log ORDER BY date"
    ).fetchall()
    schedule = {d: json.loads(detail or "{}") for d, detail in logs}
    if inception not in schedule:
        return {"days": 0, "proxy_days": 0}
    shares = dict(schedule[inception].get("shares") or {})
    cash = float(schedule[inception].get("cash") or cash_now)

    days = _trading_dates(conn, inception,
                          date.today().isoformat())
    bench = _benchmark_closes(market_db, inception, days[-1] if days else inception)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn.execute("DELETE FROM nav")
    conn.execute("DELETE FROM holdings")

    official = 0
    proxy_days = 0
    prev_total: float | None = None
    prev_bench: float | None = None
    for day in days:
        if day in schedule and day != inception:
            info = schedule[day]
            shares = dict(info.get("shares") or shares)
            cash = float(info.get("cash") or cash)
        closes = _closes(conn, day)
        bclose = bench.get(day)
        if len(closes) == len(ETF_TARGETS):
            mv = sum(shares[s] * closes[s] for s in shares)
            total = round(cash + mv, 2)
            nav = round(total / initial_cash, 6)
            _write_holdings(conn, day, shares, closes, mv, "etf")
            conn.execute(
                """INSERT OR REPLACE INTO nav
                   (date, cash, market_value, total_value, nav, benchmark_close,
                    proxy, created_at) VALUES (?,?,?,?,?,?,0,?)""",
                (day, cash, round(mv, 2), total, nav, bclose, now),
            )
            official += 1
            prev_total, prev_bench = total, bclose
        elif prev_total is not None and bclose and prev_bench:
            # 临时兜底：用沪深300 点位变动代打当日组合市值（仅标注，不进正式序列）
            total = round(prev_total * bclose / prev_bench, 2)
            conn.execute(
                """INSERT OR REPLACE INTO nav
                   (date, cash, market_value, total_value, nav, benchmark_close,
                    proxy, created_at) VALUES (?,?,?,?,?,?,1,?)""",
                (day, cash, round(total - cash, 2), total,
                 round(total / initial_cash, 6), bclose, now),
            )
            proxy_days += 1
            prev_total = total
    return {"days": official, "proxy_days": proxy_days}


def _write_holdings(conn: sqlite3.Connection, day: str, shares: dict,
                    closes: dict, market_value: float, price_source: str) -> None:
    target_by_symbol = {t["symbol"]: t["target_amount"] for t in ETF_TARGETS}
    for sym, n in shares.items():
        close = closes.get(sym)
        mv = round(n * close, 2) if close else None
        weight = round(mv / market_value * 100, 2) if (mv and market_value) else None
        conn.execute(
            """INSERT OR REPLACE INTO holdings
               (date, symbol, shares, cost_amount, close, market_value, weight,
                price_source) VALUES (?,?,?,?,?,?,?,?)""",
            (day, sym, n, target_by_symbol.get(sym, 0), close, mv, weight,
             price_source),
        )


# ============================================================
# 概况（供收益报表 / 收盘闭环打印）
# ============================================================

def _max_drawdown(nav_values: list[float]) -> float:
    peak, worst = 0.0, 0.0
    for v in nav_values:
        peak = max(peak, v)
        if peak > 0:
            worst = min(worst, v / peak - 1)
    return round(worst * 100, 2)


def _next_quarter_start(day: str) -> str:
    d = date.fromisoformat(day)
    q = (d.month - 1) // 3
    return date(d.year, q * 3 + 4, 1).isoformat() if q < 3 \
        else date(d.year + 1, 1, 1).isoformat()


def summary(market_db: str = MARKET_DB_PATH,
            db_path: str = INDEX_DB_PATH) -> dict | None:
    """组合概况：净值/回撤/跑赢基准交易日占比/持仓/再平衡提示/兜底标注

    未建仓（库或 account 不存在）时返回 None。
    """
    if not Path(db_path).exists():
        return None
    with closing(_open(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        acc = conn.execute("SELECT * FROM account WHERE id=1").fetchone()
        if not acc:
            return None
        navs = [dict(r) for r in conn.execute(
            "SELECT * FROM nav ORDER BY date").fetchall()]
        official = [r for r in navs if not r["proxy"]]
        if not official:
            return None
        last_date = official[-1]["date"]
        holds = [dict(r) for r in conn.execute(
            "SELECT * FROM holdings WHERE date=? ORDER BY symbol", (last_date,)
        ).fetchall()]
        target_rows = [dict(r) for r in conn.execute(
            "SELECT * FROM targets WHERE enabled=1 ORDER BY rowid").fetchall()]
        logs = [dict(r) for r in conn.execute(
            "SELECT * FROM rebalance_log ORDER BY date DESC LIMIT 5").fetchall()]

    proxies = [r for r in navs if r["proxy"]]
    latest = official[-1]
    initial_cash = acc["initial_cash"]
    total_ret = round((latest["total_value"] / initial_cash - 1) * 100, 2)

    # 跑赢基准的交易日占比（组合日收益 > 沪深300 日收益）
    wins = days = 0
    for prev, cur in zip(official, official[1:]):
        if not (prev["benchmark_close"] and cur["benchmark_close"]):
            continue
        p_ret = cur["total_value"] / prev["total_value"] - 1
        b_ret = cur["benchmark_close"] / prev["benchmark_close"] - 1
        days += 1
        if p_ret > b_ret:
            wins += 1

    bench_first = next((r["benchmark_close"] for r in official
                        if r["benchmark_close"]), None)
    bench_last = next((r["benchmark_close"] for r in reversed(official)
                       if r["benchmark_close"]), None)
    bench_ret = (round((bench_last / bench_first - 1) * 100, 2)
                 if (bench_first and bench_last) else None)

    return {
        "inception_date": acc["inception_date"],
        "initial_cash": initial_cash,
        "cash": acc["cash"],
        "date": last_date,
        "total_value": latest["total_value"],
        "market_value": latest["market_value"],
        "nav": latest["nav"],
        "total_return_pct": total_ret,
        "max_drawdown_pct": _max_drawdown([r["nav"] for r in official]),
        "benchmark_symbol": BENCHMARK_SYMBOL,
        "benchmark_return_pct": bench_ret,
        "excess_pct": (round(total_ret - bench_ret, 2)
                       if bench_ret is not None else None),
        "outperform_days": wins,
        "compare_days": days,
        "outperform_ratio": round(wins / days * 100, 1) if days else None,
        "holdings": holds,
        "targets": target_rows,
        "rebuilds": logs,
        "proxy_dates": [r["date"] for r in proxies],
        "official_days": len(official),
        "next_rebalance": _next_quarter_start(last_date),
        "bar_coverage": _bar_coverage(db_path),
    }


def _bar_coverage(db_path: str) -> list[dict]:
    with closing(_open(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            """SELECT symbol, COUNT(*) AS n, MIN(date) AS first_date,
                      MAX(date) AS last_date, MAX(adjust) AS adjust
               FROM etf_bars GROUP BY symbol ORDER BY symbol""").fetchall()]


# ============================================================
# 收盘闭环入口
# ============================================================

def run(day: str, is_dry: bool = False, warnings: list[str] | None = None,
        market_db: str = MARKET_DB_PATH, db_path: str = INDEX_DB_PATH,
        fetcher=None) -> dict | None:
    """收盘闭环第 6.10 步：采集 ETF 日K → 建仓/再平衡 → 重算净值

    失败只进 warnings，不影响闭环退出码；dry-run 不写生产库。
    """
    warnings = warnings if warnings is not None else []
    print("\n【6.10】指数化组合（sim 记账 · 真实 ETF 收盘价）")
    print("-" * 70)
    if is_dry:
        print("  （dry-run 跳过：不写指数组合库）")
        return None
    try:
        init_db(db_path)
        added = collect_bars(day, db_path, fetcher=fetcher)
        print("  ETF 日K：" + "｜".join(
            f"{t['code']} +{added.get(t['symbol'], 0)}" for t in ETF_TARGETS))
        with closing(_open(db_path)) as conn:
            built = _ensure_built(conn, day)
            rebalanced = _apply_rebalance(conn, day)
            navstat = rebuild_nav(conn, market_db)
        if built:
            print(f"  ✅ 首次建仓（一次建满，不分批）：{day}")
        if rebalanced:
            print(f"  ✅ 季度再平衡（拉回等权各 6,000）：{day}")
        print(f"  净值序列 {navstat['days']} 个交易日；"
              f"指数代打兜底 {navstat['proxy_days']} 个交易日（不计入正式序列）")
        st = summary(market_db, db_path)
        if st:
            print(f"  总资产 {st['total_value']:.0f} 元（现金 {st['cash']:.0f} + "
                  f"持仓 {st['market_value']:.0f}）｜累计 {st['total_return_pct']:+.2f}%"
                  f"｜最大回撤 {st['max_drawdown_pct']:.2f}%")
            if st["proxy_dates"]:
                warnings.append(
                    "指数组合有 ETF 行情缺失日，已用沪深300 代打（不计入正式净值）："
                    + "、".join(st["proxy_dates"][:10]))
        return st
    except Exception as exc:
        warnings.append(f"指数化组合更新失败（不影响闭环）: {exc}")
        print(f"  ⚠️ 指数化组合更新失败: {exc}（后续步骤不受影响）")
        return None


def _main_cli() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="指数化组合（sim 记账）")
    parser.add_argument("--collect", action="store_true",
                        help="采集 ETF 日K + 建仓/再平衡 + 重算净值")
    parser.add_argument("--summary", action="store_true", help="只打印当前概况")
    parser.add_argument("--date", default=None, help="数据日 YYYY-MM-DD（默认今天）")
    args = parser.parse_args()
    day = args.date or date.today().isoformat()
    if args.summary:
        st = summary()
        print(json.dumps(st, ensure_ascii=False, indent=2, default=str)
              if st else "（尚未建仓）")
        return
    run(day, is_dry=False)


if __name__ == "__main__":
    _main_cli()
