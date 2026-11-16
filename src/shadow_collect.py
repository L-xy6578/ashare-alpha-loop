"""板块反转影子台账：每日补采固定 200 只样本的日K（2026-11-11 新增观察层，不改口径）

为什么需要：生产库只采观察池二三十只票，池外对照样本几乎没有。
本模块只采集 `shadow_sample` 名单内 200 只主板标的的日K，落到独立表
`market.db.shadow_daily_bars`，只供影子台账使用，不参与评分、不影响决策。

硬约束：
    - 独立限速（本模块自己的并发与间隔，不与收盘阶段共用），不挤占原有采集；
    - 失败逐只落日志 + 计数，不静默跳过；失败不影响收盘闭环退出码；
    - dry-run 完全跳过（不发请求、不写库）。

用法:
    python -m src.shadow_collect             # 补采到最近交易日
    python -m src.shadow_collect --dry-run
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

from src.config import MARKET_DB_PATH
from src.db import get_connection
from src.logger import get_logger

log = get_logger(__name__)

REQUEST_GAP = 0.30      # 每只标的请求后的休眠（单线程口径）
WORKERS = 3             # 独立并发（3 线程 → 实际间隔约 0.1s，实测不触发腾讯限流）
WINDOW_DAYS = 120       # 每次补采最近 120 个自然日（覆盖回填与回看余量）
LOG_PATH = Path("logs") / "shadow_collect.log"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS shadow_daily_bars (
    symbol     TEXT NOT NULL,
    date       TEXT NOT NULL,
    open       REAL, close REAL, high REAL, low REAL, volume REAL,
    source     TEXT NOT NULL DEFAULT 'tencent',
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (symbol, date)
)
"""


def _default_fetcher(symbol: str, start: str, end: str):
    from src.data_source import TencentHttpDataSource

    return TencentHttpDataSource().daily_kline(symbol, start, end)


def _write_log(lines: list[str], path: Path | None = None) -> None:
    if not lines:
        return
    path = path or LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _symbols(market_db: str) -> list[str]:
    try:
        with get_connection(market_db, readonly=True) as conn:
            return [r[0] for r in conn.execute(
                "SELECT symbol FROM shadow_sample ORDER BY symbol")]
    except Exception as exc:
        log.warning("抽样名单读取失败：%s", exc)
        return []


def collect(day: str | None = None, *, market_db: str = MARKET_DB_PATH,
            dry_run: bool = False, fetcher=None, workers: int = WORKERS,
            gap: float = REQUEST_GAP, log_path: Path | None = None) -> dict:
    """补采名单内 200 只的日K（幂等：按 (symbol,date) 覆盖写，不产生重复行）"""
    stats = {"day": day, "symbols": 0, "requests": 0, "ok": 0, "empty": 0,
             "failed": 0, "rows": 0, "elapsed_sec": 0.0, "skipped": False,
             "reason": "", "failed_symbols": []}
    if dry_run:
        stats.update(skipped=True, reason="dry-run 跳过（不发请求、不写库）")
        return stats
    day = day or date.today().isoformat()
    symbols = _symbols(market_db)
    if not symbols:
        stats.update(skipped=True, reason="抽样名单为空（先跑 python -m src.shadow_sample）")
        return stats
    stats["symbols"] = len(symbols)
    start = (date.fromisoformat(day) - timedelta(days=WINDOW_DAYS)).isoformat()
    fetch = fetcher or _default_fetcher
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _one(symbol: str):
        time.sleep(gap)
        try:
            bars = fetch(symbol, start, day) or []
        except Exception as exc:
            return symbol, None, f"{type(exc).__name__}: {exc}"
        return symbol, bars, ""

    t0 = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for item in pool.map(_one, symbols):
            results.append(item)
    stats["elapsed_sec"] = round(time.time() - t0, 1)
    stats["requests"] = len(symbols)

    rows, log_lines = [], []
    for symbol, bars, err in results:
        if err:
            stats["failed"] += 1
            stats["failed_symbols"].append(symbol)
            log_lines.append(f"{now}\t{symbol}\terror\t0\t{err}")
            continue
        if not bars:
            stats["empty"] += 1
            log_lines.append(f"{now}\t{symbol}\tempty\t0\t")
            continue
        stats["ok"] += 1
        for b in bars:
            rows.append((symbol, b.date, b.open, b.close, b.high, b.low,
                         getattr(b, "volume", None), "tencent", now))
        log_lines.append(f"{now}\t{symbol}\tok\t{len(bars)}\t")

    if rows:
        with get_connection(market_db) as conn:
            conn.execute(SCHEMA_SQL)
            conn.executemany(
                """INSERT OR REPLACE INTO shadow_daily_bars
                     (symbol, date, open, close, high, low, volume, source, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""", rows)
    stats["rows"] = len(rows)
    _write_log(log_lines, log_path)
    if stats["failed"]:
        log.warning("日K补采失败 %d/%d：%s", stats["failed"], stats["symbols"],
                    ", ".join(stats["failed_symbols"][:20]))
    log.info("日K补采完成：%d 只，请求 %d 次，写 %d 行，耗时 %.1fs，失败 %d",
             stats["symbols"], stats["requests"], stats["rows"],
             stats["elapsed_sec"], stats["failed"])
    return stats


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="影子台账样本日K补采（独立限速）")
    p.add_argument("--date", default=None, help="补采截止日 YYYY-MM-DD（默认今天）")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--market-db", default=MARKET_DB_PATH)
    args = p.parse_args()
    print(collect(args.date, market_db=args.market_db, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
