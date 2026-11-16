"""
板块日收盘价历史 — 20/60 日涨幅的唯一可信来源（2026-11-10 冻结期 bug 修复）

背景（扫描发现 #158 / #159）：
    - 旧采集每轮只给前 21 个行业板块现拉历史 K 线，其余板块与全部概念板块
      `closes=[]` → 走 `last_known` 继承上一次的值冒充"当日 20 日涨幅"。
      实测 11-09→11-10 共 1027 个板块，zdf20 未变 1006 个（98.0%），真更新仅 21 个。
    - 行业景气因子（占评分 25%）用 zdf20 在当日横截面里算分位，输入大面积陈旧
      → 算出来的是"某个过去时点的排序"。

本模块的口径：
    1. **独立历史表** `sector_price_history`，按真实收盘日存板块收盘价，
       不写入 `sector_daily`（那张表是当日快照，混入历史会破坏语义）；
    2. 当日采集只做两件事：把当日已采到的收盘价落进历史表（1027 条，零额外请求），
       再用历史表按交易日历回看 20/60 个交易日计算涨幅；
    3. **拿不到真值就写 NULL**，由上游计入缺失，绝不用继承值冒充当日值。

采集时点 → 真实收盘日（关键）：
    实测 08:50 盘前采集到的板块点位 f2 是**上一交易日收盘价**（11-10 行存的是
    11-09 的收盘 7238.8，而 11-10 真实收盘 7244.42）。因此历史表按
    `effective_close_date()` 判定归属日，避免整条序列错位一天。

用法:
    python -m src.sector_history --backfill --start 2025-12-01   # 回补历史（一次性）
    python -m src.sector_history --report --start 2026-01-05     # 回补后可用率自检
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from datetime import datetime

from src.config import MARKET_DB_PATH
from src.db import get_connection
from src.logger import get_logger

log = get_logger(__name__)

# 板块指数日K（单板块一次请求即可拿全，lmt 与 beg=0 同时给时以东财返回为准）
KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
# 回补条数：320 根日K 覆盖到 2025-07 前后，足够 2026-01-05 起回看 60 个交易日
BACKFILL_BARS = 320
BACKFILL_WORKERS = 5
BACKFILL_RETRY = 2

TABLE = """
CREATE TABLE IF NOT EXISTS sector_price_history (
    date TEXT NOT NULL,        -- 真实收盘日（不是采集日）
    code TEXT NOT NULL,        -- bk_BK0479
    name TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '',   -- plate(行业) | concept(概念)
    close REAL NOT NULL,       -- 板块指数收盘点位
    pct REAL,                  -- 当日涨跌幅 %（可空）
    source TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (date, code)
)
"""
INDEX = ("CREATE INDEX IF NOT EXISTS idx_sector_hist_code_date "
         "ON sector_price_history(code, date)")


def ensure_schema(conn) -> None:
    conn.execute(TABLE)
    conn.execute(INDEX)


# ---------- 交易日历 / 收盘日归属 ----------
def trading_days_upto(day: str, n: int) -> list[str]:
    """day（含）往前 n 个交易日，升序；日历不足返回实际条数"""
    try:
        with get_connection(MARKET_DB_PATH, readonly=True) as conn:
            rows = conn.execute(
                "SELECT date FROM trade_calendar WHERE is_trading=1 AND date<=? "
                "ORDER BY date DESC LIMIT ?", (day, n)).fetchall()
    except Exception as exc:
        log.warning("交易日历读取失败: %s", exc)
        return []
    return [r[0] for r in reversed(rows)]


def effective_close_date(run_date: str | None = None,
                         now: datetime | None = None) -> str | None:
    """采集日 → 该批点位对应的真实收盘日

    - 15:00 之后采集：点位是当日收盘 → 归属当日（非交易日则上一个交易日）；
    - 15:00 之前采集（自动任务在 08:50）：点位仍是上一交易日收盘 → 归属上一交易日；
    - 手工注入历史日期（run_date != 今天）：信任调用方，按该日归属。
    """
    from datetime import date as date_cls

    now = now or datetime.now()
    run_date = run_date or date_cls.today().isoformat()
    if run_date != date_cls.today().isoformat():
        # 手工注入历史日期：信任调用方，按该日归属（无须查日历）
        return run_date
    days = trading_days_upto(run_date, 2)
    if not days:
        return None
    if now.hour >= 15:
        return days[-1]
    prev = [d for d in days if d < run_date]
    return prev[-1] if prev else None


def is_close_price_time(now: datetime | None = None) -> bool:
    """当前时刻拿到的板块点位是否可当"收盘价"落库

    09:00–15:00 之间是盘中价（或未开盘的隔夜价之外的盘中值），
    落进历史表会污染序列，一律不写；只做计算不写库。
    """
    now = now or datetime.now()
    return now.hour < 9 or now.hour >= 15


def ensure_calendar(start: str = "2025-12-01", end: str = "2025-12-31") -> int:
    """补 2026 年之前的交易日历（`trade_calendar` 原来只生成 2026 全年）

    2026-01-05 起算 20 日涨幅需要 2025-12 的交易日作基准；2025 年 12 月
    无法定休市（元旦在 2026-01-01），按工作日判定即可。幂等：已有行不动。
    """
    from datetime import date as date_cls, timedelta

    d, rows = date_cls.fromisoformat(start), []
    while d.isoformat() <= end:
        if d.weekday() < 5:
            rows.append((d.isoformat(), 1, "扩展（2025-12 无休市）"))
        d += timedelta(days=1)
    with get_connection(MARKET_DB_PATH) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS trade_calendar "
                     "(date TEXT PRIMARY KEY, is_trading INTEGER NOT NULL DEFAULT 1, "
                     "note TEXT)")
        conn.executemany(
            "INSERT OR IGNORE INTO trade_calendar (date, is_trading, note) VALUES (?,?,?)",
            rows)
    return len(rows)


# ---------- 写入 ----------
def upsert_closes(close_date: str, rows: list[dict], source: str = "eastmoney") -> int:
    """按真实收盘日批量写入板块收盘价（幂等）

    Args:
        rows: [{"code","name","kind","close","pct"}]
    """
    vals = []
    for r in rows:
        try:
            close = float(r.get("close") or 0)
        except (TypeError, ValueError):
            continue
        if close <= 0 or not r.get("code"):
            continue
        pct = r.get("pct")
        try:
            pct = float(pct) if pct is not None else None
        except (TypeError, ValueError):
            pct = None
        vals.append((close_date, r["code"], r.get("name") or "",
                     r.get("kind") or "", round(close, 4), pct, source))
    if not vals:
        return 0
    with get_connection(MARKET_DB_PATH) as conn:
        ensure_schema(conn)
        conn.executemany(
            """INSERT INTO sector_price_history
               (date, code, name, kind, close, pct, source)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(date, code) DO UPDATE SET
                 name=excluded.name, kind=excluded.kind, close=excluded.close,
                 pct=COALESCE(excluded.pct, pct), source=excluded.source""", vals)
    return len(vals)


# ---------- 读取 / 计算 ----------
def zdf_by_code(asof: str | None, n: int) -> tuple[dict[str, float], str | None]:
    """按真实收盘日 asof 回看 n 个交易日，返回 {code: 涨幅%} 与基准日

    Returns:
        (map, base_date)；历史不足 / 基准日无该板块数据时该板块不在 map 里
        （调用方必须按"缺失"处理，不得用 0 或继承值顶替）。
    """
    if not asof:
        return {}, None
    window = trading_days_upto(asof, n + 1)
    if len(window) < n + 1:
        return {}, None
    base = window[0]
    try:
        with get_connection(MARKET_DB_PATH, readonly=True) as conn:
            cur = {r[0]: r[1] for r in conn.execute(
                "SELECT code, close FROM sector_price_history WHERE date=?", (asof,))}
            old = {r[0]: r[1] for r in conn.execute(
                "SELECT code, close FROM sector_price_history WHERE date=?", (base,))}
    except Exception as exc:
        log.warning("板块历史读取失败（%s）：%s", asof, exc)
        return {}, None
    out: dict[str, float] = {}
    for code, close in cur.items():
        prev = old.get(code)
        if not close or not prev or prev <= 0:
            continue
        out[code] = round((close / prev - 1) * 100, 2)
    return out, base


# ---------- 历史回补（一次性，不进日常链路） ----------
def _board_codes() -> list[dict]:
    """从 sector_daily 最近一天取全部板块（含概念），无需再请求列表接口"""
    try:
        with get_connection(MARKET_DB_PATH, readonly=True) as conn:
            day = conn.execute("SELECT MAX(date) FROM sector_daily").fetchone()[0]
            if not day:
                return []
            return [{"code": r[0], "name": r[1], "kind": r[2]} for r in conn.execute(
                "SELECT code, name, kind FROM sector_daily WHERE date=?", (day,))]
    except Exception as exc:
        log.warning("板块清单读取失败: %s", exc)
        return []


def fetch_board_history(code: str, bars: int = BACKFILL_BARS,
                        timeout: int = 8) -> dict[str, float] | None:
    """单个板块的历史收盘价 {date: close}；失败返回 None"""
    from src.net import fetch_json

    secid = code.replace("bk_", "")
    qs = (f"?secid=90.{secid}&klt=101&fqt=1&lmt={bars}&end=20500101"
          "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56")
    last: Exception | None = None
    for attempt in range(BACKFILL_RETRY):
        try:
            data = fetch_json(KLINE_URL + qs, timeout=timeout).get("data") or {}
            out: dict[str, float] = {}
            for k in data.get("klines") or []:
                parts = k.split(",")
                if len(parts) >= 3 and parts[2] not in ("", "-"):
                    try:
                        out[parts[0]] = float(parts[2])
                    except ValueError:
                        continue
            if out:
                return out
            last = RuntimeError("空K线")
        except Exception as exc:
            last = exc
        time.sleep(0.5 * (attempt + 1))
    log.warning("板块历史拉取失败 %s: %s", code, last)
    return None


def backfill(start: str = "2025-12-01", workers: int = BACKFILL_WORKERS,
             limit: int = 0, force: bool = False) -> dict:
    """回补板块历史收盘价（幂等；默认跳过已覆盖 start 的板块）

    Returns: {"total","pulled","skipped","failed","rows","failed_codes"}
    """
    from concurrent.futures import ThreadPoolExecutor

    boards = _board_codes()
    if not boards:
        return {"total": 0, "pulled": 0, "skipped": 0, "failed": 0,
                "rows": 0, "failed_codes": []}

    covered: dict[str, str] = {}
    if not force:
        try:
            with get_connection(MARKET_DB_PATH, readonly=True) as conn:
                covered = {r[0]: r[1] for r in conn.execute(
                    "SELECT code, MIN(date) FROM sector_price_history GROUP BY code")}
        except Exception:
            covered = {}
    todo = [b for b in boards if force or covered.get(b["code"], "9999") > start]
    if limit:
        todo = todo[:limit]

    log.info("板块历史回补：共 %d 个板块，待补 %d 个（已覆盖 %d 个），起始 %s",
             len(boards), len(todo), len(boards) - len(todo), start)

    stats = {"total": len(boards), "pulled": 0, "skipped": len(boards) - len(todo),
             "failed": 0, "rows": 0, "failed_codes": []}
    buf: list[dict] = []
    t0 = time.time()

    def one(b):
        return b, fetch_board_history(b["code"])

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for idx, (b, hist) in enumerate(pool.map(one, todo), 1):
            if not hist:
                stats["failed"] += 1
                stats["failed_codes"].append(b["code"])
            else:
                stats["pulled"] += 1
                for d, close in hist.items():
                    if d >= start:
                        buf.append({"code": b["code"], "name": b["name"],
                                    "kind": b["kind"], "close": close,
                                    "pct": None, "date": d})
            if len(buf) >= 20000 or idx == len(todo):
                stats["rows"] += _write_buffered(buf)
                buf = []
            if idx % 100 == 0 or idx == len(todo):
                log.info("回补进度 %d/%d（失败 %d）%.1fs", idx, len(todo),
                         stats["failed"], time.time() - t0)
            time.sleep(0.05 + random.random() * 0.05)

    log.info("板块历史回补完成：成功 %d / 失败 %d / 写入 %d 行，耗时 %.1fs",
             stats["pulled"], stats["failed"], stats["rows"], time.time() - t0)
    return stats


def _write_buffered(buf: list[dict]) -> int:
    """按日期分组批量 upsert（一次连接写多天）"""
    if not buf:
        return 0
    by_date: dict[str, list[dict]] = {}
    for r in buf:
        by_date.setdefault(r["date"], []).append(r)
    n = 0
    with get_connection(MARKET_DB_PATH) as conn:
        ensure_schema(conn)
        for day, rows in by_date.items():
            n += upsert_closes_rows(conn, day, rows, source="eastmoney_kline")
    return n


def upsert_closes_rows(conn, close_date: str, rows: list[dict], source: str) -> int:
    vals = [(close_date, r["code"], r.get("name") or "", r.get("kind") or "",
             round(float(r["close"]), 4), r.get("pct"), source)
            for r in rows if r.get("close")]
    conn.executemany(
        """INSERT INTO sector_price_history
           (date, code, name, kind, close, pct, source)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(date, code) DO UPDATE SET
             name=excluded.name, kind=excluded.kind, close=excluded.close,
             pct=COALESCE(excluded.pct, pct), source=excluded.source""", vals)
    return len(vals)


def coverage(start: str = "2026-01-05", end: str | None = None) -> list[dict]:
    """逐交易日统计 20 日涨幅可算率（回补自检用）"""
    end = end or trading_days_upto(datetime.now().date().isoformat(), 1)[-1]
    days = [d for d in trading_days_upto(end, 400) if d >= start]
    out = []
    for d in days:
        z20, base20 = zdf_by_code(d, 20)
        total = len(_boards_on(d))
        out.append({"date": d, "boards": total, "zdf20": len(z20), "base": base20})
    return out


def _boards_on(day: str) -> set[str]:
    try:
        with get_connection(MARKET_DB_PATH, readonly=True) as conn:
            return {r[0] for r in conn.execute(
                "SELECT DISTINCT code FROM sector_price_history WHERE date=?", (day,))}
    except Exception:
        return set()


def _main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="板块日收盘价历史（20/60 日涨幅唯一来源）")
    p.add_argument("--backfill", action="store_true", help="回补历史收盘价（一次性）")
    p.add_argument("--start", default="2025-12-01", help="回补起始日期")
    p.add_argument("--workers", type=int, default=BACKFILL_WORKERS)
    p.add_argument("--limit", type=int, default=0, help="本次最多补多少个板块（0=全部）")
    p.add_argument("--force", action="store_true", help="忽略已覆盖判定，全部重拉")
    p.add_argument("--report", action="store_true", help="逐交易日可算率自检")
    p.add_argument("--extend-calendar", action="store_true",
                   help="补 2025-12 交易日历（2026-01-05 起算 20 日涨幅的基准日需要）")
    args = p.parse_args()
    if args.extend_calendar:
        n = ensure_calendar()
        print(f"交易日历已扩展 2025-12 工作日 {n} 条（幂等）")
        return
    if args.backfill:
        s = backfill(args.start, workers=args.workers, limit=args.limit, force=args.force)
        print(f"回补：板块 {s['total']} / 成功 {s['pulled']} / 跳过 {s['skipped']} / "
              f"失败 {s['failed']} / 写入 {s['rows']} 行")
        if s["failed_codes"]:
            print("失败板块（可重跑续补）:", ", ".join(s["failed_codes"][:20]))
        return
    if args.report:
        rows = coverage(max(args.start, "2026-01-05"))
        ok = [r for r in rows if r["boards"] and r["zdf20"] / r["boards"] >= 0.9]
        print(f"交易日 {len(rows)} 个，20 日涨幅可算率 ≥90% 的 {len(ok)} 个")
        for r in rows[:5] + rows[-5:]:
            rate = (r["zdf20"] / r["boards"] * 100) if r["boards"] else 0
            print(f"  {r['date']}  板块 {r['boards']:4d}  可算 {r['zdf20']:4d} "
                  f"({rate:5.1f}%)  基准日 {r['base'] or '-'}")
        return
    p.print_help()


if __name__ == "__main__":
    _main()
