"""2019-2023 历史样本外采集（东财日K / 前复权）

只写 data/research.db 的三张新表，**不写 data/market.db**，不接入生产链路：
    - research_bars_hist(symbol, date, open, close, high, low, volume, amount, source)
    - research_board_bars_hist(code, date, close)
    - research_hist_status(kind, code, status, bar_count, first_date, last_date, updated_at)

为什么单独建表：现有 research_bars 是腾讯 640 根（起点 2024-03），
本次要 2019-2023 的完整历史；混写会悄悄改掉现有 2024-2026 回测的样本边界。

用法:
    python scripts/research_collect_hist.py --stage bars
    python scripts/research_collect_hist.py --stage boards
    python scripts/research_collect_hist.py --stage status
    python scripts/research_collect_hist.py --stage all --workers 6
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import research_store as rs  # noqa: E402

LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
log = logging.getLogger("research_collect_hist")
log.setLevel(logging.INFO)
_fh = logging.FileHandler(LOG_DIR / "research_collect_hist.log", encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(_fh)
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
log.addHandler(_sh)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
KLINE_HOSTS = (
    "https://push2his.eastmoney.com/api/qt/stock/kline/get",
    "https://push2his.eastmoney.com/api/qt/stock/kline/get",
)
STOCK_BEG, STOCK_END = "20190101", "20240201"
BOARD_BEG, BOARD_END = "20180601", "20240201"
LMT = 10000

SCHEMA = """
CREATE TABLE IF NOT EXISTS research_bars_hist (
    symbol  TEXT NOT NULL,
    date    TEXT NOT NULL,
    open    REAL, close REAL, high REAL, low REAL,
    volume  REAL, amount REAL,
    source  TEXT,
    PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS research_board_bars_hist (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    close REAL,
    PRIMARY KEY (code, date)
);
CREATE TABLE IF NOT EXISTS research_hist_status (
    kind TEXT NOT NULL,                 -- bars / board
    code TEXT NOT NULL,
    status TEXT,
    bar_count INTEGER,
    first_date TEXT,
    last_date TEXT,
    updated_at TEXT,
    PRIMARY KEY (kind, code)
);
"""

def _open() -> sqlite3.Connection:
    """独立写连接（不走 open_research 上下文，避免生成器被回收时提前 close）"""
    conn = sqlite3.connect(rs.RESEARCH_DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _kline(secid: str, beg: str, end: str) -> list[tuple]:
    qs = (f"?secid={secid}&klt=101&fqt=1&beg={beg}&end={end}&lmt={LMT}"
          "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57")
    last: Exception | None = None
    for attempt in range(3):
        for host in KLINE_HOSTS:
            try:
                data = json.loads(_get(host + qs).decode()).get("data") or {}
                rows = []
                for k in data.get("klines") or []:
                    p = k.split(",")
                    if len(p) < 6 or p[2] in ("", "-"):
                        continue
                    amt = None
                    if len(p) > 6 and p[6] not in ("", "-"):
                        try:
                            amt = float(p[6])
                        except ValueError:
                            amt = None
                    rows.append((p[0], float(p[1]), float(p[2]), float(p[3]),
                                 float(p[4]), float(p[5]) if p[5] not in ("", "-") else 0.0, amt))
                if rows:
                    return rows
                last = RuntimeError("空K线")
            except Exception as exc:
                last = exc
        time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(f"东财K线失败: {last}")


def _secid(symbol: str) -> str:
    market, code = symbol[:2], symbol[2:]
    return ("1." if market == "sh" else "0.") + code


def init(conn) -> None:
    conn.executescript(SCHEMA)


def stage_bars(workers: int = 6, gap: float = 0.25, limit: int = 0,
               force: bool = False) -> None:
    with rs.open_research() as conn:
        init(conn)
        sql = "SELECT symbol FROM research_symbols WHERE is_main_board=1"
        if not force:
            sql += (" AND symbol NOT IN (SELECT code FROM research_hist_status"
                    " WHERE kind='bars' AND status='ok' AND bar_count>0)")
        sql += " ORDER BY symbol"
        todo = [r[0] for r in conn.execute(sql).fetchall()]
    if limit:
        todo = todo[:limit]
    log.info("待采个股 %d 只（workers=%d gap=%.2fs）", len(todo), workers, gap)
    if not todo:
        return

    t0 = time.time()
    done = err = rows_total = 0
    conn = _open()
    try:
        init(conn)

        def work(sym: str) -> dict:
            time.sleep(gap + 0.05 * ((hash(sym) % 7) / 7))
            try:
                return {"symbol": sym, "rows": _kline(_secid(sym), STOCK_BEG, STOCK_END), "err": ""}
            except Exception as exc:
                return {"symbol": sym, "rows": [], "err": str(exc)[:120]}

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(work, s) for s in todo]
            for idx, fut in enumerate(as_completed(futures), 1):
                res = fut.result()
                sym, rows = res["symbol"], res["rows"]
                if rows:
                    conn.executemany(
                        "INSERT OR REPLACE INTO research_bars_hist "
                        "(symbol,date,open,close,high,low,volume,amount,source) "
                        "VALUES (?,?,?,?,?,?,?,?,'eastmoney')",
                        [(sym, *r) for r in rows])
                    conn.execute(
                        "INSERT OR REPLACE INTO research_hist_status VALUES "
                        "('bars',?,?,?,?,?,datetime('now','localtime'))",
                        (sym, "ok", len(rows), rows[0][0], rows[-1][0]))
                    done += 1
                    rows_total += len(rows)
                else:
                    conn.execute(
                        "INSERT OR REPLACE INTO research_hist_status VALUES "
                        "('bars',?,?,?,?,?,datetime('now','localtime'))",
                        (sym, f"error:{res['err'][:60]}", 0, "", ""))
                    err += 1
                if idx % 200 == 0 or idx == len(todo):
                    conn.commit()
                    el = max(time.time() - t0, 1e-6)
                    rate = idx / el
                    log.info("个股 %d/%d ok=%d err=%d 行=%d %.1f 只/秒 剩余 %.1f 分钟",
                             idx, len(todo), done, err, rows_total, rate,
                             (len(todo) - idx) / rate / 60 if rate else -1)
    finally:
        conn.commit()
        conn.close()
    log.info("个股采集完成：ok=%d err=%d 行=%d 用时 %.1f 分钟",
             done, err, rows_total, (time.time() - t0) / 60)


def stage_boards(workers: int = 4, gap: float = 0.25, limit: int = 0) -> None:
    with rs.open_research() as conn:
        init(conn)
        todo = [r[0] for r in conn.execute(
            "SELECT code FROM research_boards WHERE kind='plate' ORDER BY code").fetchall()]
    if limit:
        todo = todo[:limit]
    log.info("待采行业板块 %d 个", len(todo))

    t0 = time.time()
    done = err = 0
    conn = _open()
    try:
        init(conn)

        def work(code: str) -> dict:
            time.sleep(gap)
            try:
                rows = _kline(f"90.{code}", BOARD_BEG, BOARD_END)
                return {"code": code, "rows": rows, "err": ""}
            except Exception as exc:
                return {"code": code, "rows": [], "err": str(exc)[:120]}

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(work, c) for c in todo]
            for idx, fut in enumerate(as_completed(futures), 1):
                res = fut.result()
                code, rows = res["code"], res["rows"]
                if rows:
                    conn.executemany(
                        "INSERT OR REPLACE INTO research_board_bars_hist (code,date,close) "
                        "VALUES (?,?,?)", [(code, r[0], r[2]) for r in rows])
                    conn.execute(
                        "INSERT OR REPLACE INTO research_hist_status VALUES "
                        "('board',?,?,?,?,?,datetime('now','localtime'))",
                        (code, "ok", len(rows), rows[0][0], rows[-1][0]))
                    done += 1
                else:
                    conn.execute(
                        "INSERT OR REPLACE INTO research_hist_status VALUES "
                        "('board',?,?,?,?,?,datetime('now','localtime'))",
                        (code, f"error:{res['err'][:60]}", 0, "", ""))
                    err += 1
                if idx % 50 == 0 or idx == len(todo):
                    conn.commit()
                    log.info("板块 %d/%d ok=%d err=%d 用时 %.1f 分钟",
                             idx, len(todo), done, err, (time.time() - t0) / 60)
    finally:
        conn.commit()
        conn.close()


def stage_status() -> None:
    with rs.open_research(readonly=True) as conn:
        for q, label in (
            ("SELECT COUNT(*), SUM(bar_count), MIN(first_date), MAX(last_date) "
             "FROM research_hist_status WHERE kind='bars' AND status='ok'", "个股"),
            ("SELECT COUNT(*), SUM(bar_count), MIN(first_date), MAX(last_date) "
             "FROM research_hist_status WHERE kind='board' AND status='ok'", "板块"),
            ("SELECT COUNT(*), MIN(date), MAX(date), COUNT(DISTINCT symbol) "
             "FROM research_bars_hist", "个股行"),
            ("SELECT COUNT(*), MIN(date), MAX(date), COUNT(DISTINCT code) "
             "FROM research_board_bars_hist", "板块行"),
        ):
            print(f"{label:>6}: {tuple(conn.execute(q).fetchone())}")
        print("失败清单:")
        for r in conn.execute("SELECT kind, code, status FROM research_hist_status "
                              "WHERE status<>'ok' LIMIT 30"):
            print("   ", tuple(r))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="all",
                    choices=("all", "bars", "boards", "status"))
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--gap", type=float, default=0.25)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    if a.stage in ("all", "bars"):
        stage_bars(a.workers, a.gap, a.limit, a.force)
    if a.stage in ("all", "boards"):
        stage_boards(min(a.workers, 4), a.gap, a.limit)
    if a.stage in ("all", "status"):
        stage_status()


if __name__ == "__main__":
    main()
