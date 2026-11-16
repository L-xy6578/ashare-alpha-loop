"""板块反转影子台账：分档抽样固定名单（2026-11-11 新增观察层，不改口径）

问题（首日暴露）：生产库只采观察池那二三十只票，池外标的日K 停在 2026-08，
「各档位实际表现」缺池外对照样本。本模块按行业板块 20 日涨幅分位四档各抽 50 只
主板非 ST 标的，**一次性生成后固定**，供每日补采（src.shadow_collect）与影子台账使用。

抽样规则（写死，报表同步展示，保证可复现）：
    1. 抽样基准日 = 板块历史最新交易日（默认 2026-11-10）；
    2. 候选池 = 沪深主板、非 ST、上市满 `MIN_BAR_COUNT` 根日K（剔除次新）、
       基准日前 10 个自然日内有成交、收盘价 ∈ [5, 60] 元；
    3. 板块分位 = `sector_price_history` 当日行业板块（kind='plate'）20 日涨幅分位，
       口径与生产 `industry_percentile_factor` 完全一致；
    4. 每个档位（<50 / 50~65 / 65~85 / ≥85）内按 `md5(f"{SEED}:{symbol}")` 升序取前
       `PER_BUCKET` 只 —— 等价于固定种子的确定性随机抽样，不掺任何主观挑选；
    5. 名单写入 `market.db.shadow_sample`（symbol 主键），已存在则**不重抽**
       （除非显式 `--rebuild`）；同时导出 CSV 便于审计。

用法:
    python -m src.shadow_sample            # 生成名单（已存在则跳过）
    python -m src.shadow_sample --rebuild  # 强制重抽（会改变名单，慎用）
    python -m src.shadow_sample --status   # 只看现有名单分档计数
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from src.config import MARKET_DB_PATH
from src.db import get_connection
from src.factors import industry_percentile_factor, industry_pool
from src.logger import get_logger
from src.sector_reversal_shadow import BUCKETS, OUT_DIR, bucket_of, load_plates

log = get_logger(__name__)

SEED = 20261111
PER_BUCKET = 50
MIN_BAR_COUNT = 120          # 上市不足约半年视为次新
PRICE_MIN, PRICE_MAX = 5.0, 60.0
STALE_DAYS = 10              # 基准日前 10 个自然日内必须还有成交（停牌/退市剔除）
RULE = ("主板非ST · 上市≥120根日K · 收盘5~60元 · 按行业板块20日涨幅分位四档，"
        f"每档 md5(seed:symbol) 升序取前 {PER_BUCKET} 只，seed={SEED}")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS shadow_sample (
    symbol      TEXT PRIMARY KEY,
    code        TEXT NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    industry    TEXT NOT NULL,
    bucket      TEXT NOT NULL,
    sector_pct  REAL NOT NULL,
    board_zdf20 REAL,
    base_close  REAL NOT NULL,
    bar_count   INTEGER NOT NULL DEFAULT 0,
    seed        INTEGER NOT NULL,
    rule        TEXT NOT NULL DEFAULT '',
    sampled_for TEXT NOT NULL,               -- 抽样基准日
    created_at  TEXT NOT NULL
)
"""


def _research_path() -> str:
    from src.research_store import RESEARCH_DB_PATH

    return RESEARCH_DB_PATH


def _universe(research_db: str, day: str) -> list[dict]:
    """候选池：主板、非ST、非次新、价格区间内、基准日附近有成交"""
    if not Path(research_db).exists():
        log.warning("研究库不存在：%s（先跑 scripts/research_collect.py）", research_db)
        return []
    conn = sqlite3.connect(f"file:{research_db}?mode=ro", uri=True, timeout=10)
    try:
        rows = conn.execute(
            """SELECT s.symbol, s.code, s.name, s.industry, b.close, s.bar_count
               FROM research_symbols s
               JOIN research_bars b ON b.symbol = s.symbol AND b.date = s.last_date
               WHERE s.is_main_board = 1 AND s.is_st = 0
                 AND s.bar_count >= ? AND s.last_date >= date(?, ?)""",
            (MIN_BAR_COUNT, day, f"-{STALE_DAYS} days")).fetchall()
    finally:
        conn.close()
    out = []
    for symbol, code, name, industry, close, bar_count in rows:
        if not close or not (PRICE_MIN <= close <= PRICE_MAX) or not industry:
            continue
        out.append({"symbol": symbol, "code": code, "name": name or "",
                    "industry": industry, "close": round(close, 2),
                    "bar_count": bar_count})
    return out


def _order_key(symbol: str) -> str:
    return hashlib.md5(f"{SEED}:{symbol}".encode("utf-8")).hexdigest()


def build(*, market_db: str = MARKET_DB_PATH, research_db: str | None = None,
          day: str | None = None, rebuild: bool = False, dry_run: bool = False,
          per_bucket: int = PER_BUCKET) -> dict:
    """生成固定名单；已有名单且未指定 rebuild 时直接返回现状（不重抽）"""
    research_db = research_db or _research_path()
    day = day or _latest_plate_date(market_db)
    result = {"day": day, "seed": SEED, "per_bucket": per_bucket,
              "counts": {b: 0 for b in BUCKETS}, "universe": {}, "missing": {},
              "written": 0, "skipped": False, "reason": ""}
    if not day:
        result.update(skipped=True, reason="板块历史为空，无法确定抽样基准日")
        return result

    with get_connection(market_db) as conn:
        conn.execute(SCHEMA_SQL)
        existing = conn.execute("SELECT COUNT(*) FROM shadow_sample").fetchone()[0]
    if existing and not rebuild:
        counts = {b: 0 for b in BUCKETS}
        with get_connection(market_db, readonly=True) as conn:
            for b, n in conn.execute(
                    "SELECT bucket, COUNT(*) FROM shadow_sample GROUP BY bucket"):
                counts[b] = n
        result.update(counts=counts, written=existing, skipped=True,
                      reason="名单已存在，保持固定（如需重抽用 --rebuild）")
        return result

    plates, _ = load_plates(market_db, day)
    pool = industry_pool(plates)
    if not pool:
        result.update(skipped=True, reason=f"{day} 行业板块池不可用（历史不足或全缺失）")
        return result
    zdf_by_name = {p.name: p.zdf20 for p in pool}

    groups: dict[str, list[dict]] = {b: [] for b in BUCKETS}
    for s in _universe(research_db, day):
        pct = industry_percentile_factor(pool, s["industry"])
        b = bucket_of(pct)
        if b is None:
            continue
        groups[b].append({**s, "sector_pct": pct,
                          "board_zdf20": zdf_by_name.get(s["industry"])})
    for b in BUCKETS:
        result["universe"][b] = len(groups[b])

    picked = []
    for b in BUCKETS:
        ordered = sorted(groups[b], key=lambda x: _order_key(x["symbol"]))
        take = ordered[:per_bucket]
        result["counts"][b] = len(take)
        if len(take) < per_bucket:
            result["missing"][b] = (f"该档候选池仅 {len(groups[b])} 只"
                                    f"（已按非ST/非次新/5~60元/板块可解析过滤）")
        picked.extend({**x, "bucket": b} for x in take)

    if dry_run:
        result.update(skipped=True, reason="dry-run：只计算不落库")
        return result

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection(market_db) as conn:
        conn.execute(SCHEMA_SQL)
        if rebuild:
            conn.execute("DELETE FROM shadow_sample")
        conn.executemany(
            """INSERT OR REPLACE INTO shadow_sample
                 (symbol, code, name, industry, bucket, sector_pct, board_zdf20,
                  base_close, bar_count, seed, rule, sampled_for, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(x["symbol"], x["code"], x["name"], x["industry"], x["bucket"],
              x["sector_pct"], x["board_zdf20"], x["close"], x["bar_count"],
              SEED, RULE, day, now) for x in picked])
    result["written"] = len(picked)
    export_csv(picked, day)
    return result


def _latest_plate_date(market_db: str) -> str | None:
    try:
        with get_connection(market_db, readonly=True) as conn:
            row = conn.execute(
                "SELECT MAX(date) FROM sector_price_history WHERE kind='plate'").fetchone()
        return row[0] if row and row[0] else None
    except Exception as exc:
        log.warning("板块历史日期探测失败：%s", exc)
        return None


def export_csv(rows: list[dict], day: str, out_dir: Path | None = None) -> Path:
    out_dir = out_dir or OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"抽样名单_{day}.csv"
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["symbol", "code", "name", "industry", "bucket", "sector_pct",
                    "board_zdf20", "base_close", "bar_count"])
        for x in sorted(rows, key=lambda r: (BUCKETS.index(r["bucket"]), r["symbol"])):
            w.writerow([x["symbol"], x["code"], x["name"], x["industry"], x["bucket"],
                        x["sector_pct"], x["board_zdf20"], x["close"], x["bar_count"]])
    return path


def status(market_db: str = MARKET_DB_PATH) -> dict:
    try:
        with get_connection(market_db, readonly=True) as conn:
            rows = list(conn.execute(
                "SELECT bucket, COUNT(*) FROM shadow_sample GROUP BY bucket"))
            seed = conn.execute("SELECT seed, sampled_for, rule, COUNT(*) "
                                "FROM shadow_sample LIMIT 1").fetchone()
    except Exception as exc:
        return {"error": str(exc), "counts": {}}
    return {"counts": {b: n for b, n in rows},
            "seed": seed[0] if seed else None,
            "sampled_for": seed[1] if seed else None,
            "rule": seed[2] if seed else None,
            "total": seed[3] if seed else 0}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="板块反转影子台账：分档抽样固定名单")
    p.add_argument("--rebuild", action="store_true", help="强制重抽（默认保持既有名单）")
    p.add_argument("--dry-run", action="store_true", help="只计算不落库")
    p.add_argument("--status", action="store_true", help="查看现有名单分档计数")
    p.add_argument("--per-bucket", type=int, default=PER_BUCKET)
    p.add_argument("--date", default=None, help="抽样基准日 YYYY-MM-DD")
    p.add_argument("--market-db", default=MARKET_DB_PATH)
    args = p.parse_args()

    if args.status:
        print(status(args.market_db))
        return
    res = build(market_db=args.market_db, day=args.date, rebuild=args.rebuild,
                dry_run=args.dry_run, per_bucket=args.per_bucket)
    print(f"基准日 {res['day']}  seed={res['seed']}  写入 {res['written']} 只")
    for b in BUCKETS:
        print(f"  {b:<6} 抽到 {res['counts'][b]:>3} / 候选池 {res['universe'].get(b, 0):>4}"
              + (f"  ⚠️ {res['missing'][b]}" if b in res["missing"] else ""))
    if res.get("reason"):
        print(f"  说明：{res['reason']}")


if __name__ == "__main__":
    main()
