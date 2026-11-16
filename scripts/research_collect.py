"""研究样本采集器（独立于生产链路）

把「全部沪深主板个股 + 沪深300 + 东财板块指数」的近两年日K写入 data/research.db。
**不写 data/market.db，不接入 r47_closed_loop / run_daily。**

用法:
    python scripts/research_collect.py --stage all
    python scripts/research_collect.py --stage symbols
    python scripts/research_collect.py --stage bars --limit 50 --workers 4
    python scripts/research_collect.py --stage boards
    python scripts/research_collect.py --stage status

设计要点:
    - 断点续采：research_symbols.collect_status='ok' 且 bar_count>0 的标的默认跳过（--force 重采）；
    - 限流：线程池 + 每请求小间隔 + 多 host 轮换 + 指数退避重试；
    - 失败不静默：单个标的失败写入 research_collect_log，标的记 error:<原因>。
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import research_store as rs  # noqa: E402

LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
log = logging.getLogger("research_collect")
log.setLevel(logging.INFO)
_fh = logging.FileHandler(LOG_DIR / "research_collect.log", encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(_fh)
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
log.addHandler(_sh)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

MAIN_BOARD_PREFIXES = ("600", "601", "603", "605", "000", "001", "002", "003")

CLIST_HOSTS = (
    "https://push2delay.eastmoney.com/api/qt/clist/get",
    "https://push2.eastmoney.com/api/qt/clist/get",
)
TENCENT_HOSTS = (
    "https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get",
    "https://ifzq.gtimg.cn/appstock/app/newfqkline/get",
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
)
BOARD_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

KLINE_COUNT = 640          # 腾讯单次上限，约 2.6 年日K
BOARD_KLINE_COUNT = 700


def _get(url: str, timeout: int = 15) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ============================================================
# 阶段 1：股票列表
# ============================================================

def stage_symbols(limit: int = 0) -> int:
    rows: list[dict] = []
    total = None
    seen: set[str] = set()
    for pn in range(1, 80):
        url = (f"{CLIST_HOSTS[0]}?pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3"
               "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23&fields=f12,f13,f14,f100")
        payload = None
        for host in CLIST_HOSTS:
            try:
                payload = json.loads(_get(url.replace(CLIST_HOSTS[0], host)).decode())["data"]
                break
            except Exception as exc:  # 换 host 重试
                log.warning("股票列表第 %d 页失败（%s）: %s", pn, host, exc)
                time.sleep(0.5)
        if not payload or not payload.get("diff"):
            if total is None:
                raise RuntimeError(f"股票列表第 {pn} 页完全拉不到，中止")
            break
        total = payload.get("total") or total
        for item in payload["diff"]:
            code = str(item.get("f12") or "")
            if code in seen:
                continue
            seen.add(code)
            market = "sh" if int(item.get("f13") or 0) == 1 else "sz"
            name = str(item.get("f14") or "")
            is_main = code.startswith(MAIN_BOARD_PREFIXES)
            up = name.upper()
            rows.append({
                "symbol": market + code,
                "code": code,
                "market": market,
                "name": name,
                "industry": item.get("f100") or "",
                "is_st": 1 if ("ST" in up or "退" in name) else 0,
                "is_main_board": 1 if is_main else 0,
            })
        if len(rows) >= payload.get("total", 0):
            break
        if limit and len(rows) >= limit:
            break
    main_rows = [r for r in rows if r["is_main_board"]]
    with rs.open_research() as conn:
        rs.init_db(conn)
        rs.upsert_symbols(conn, main_rows)
        rs.log_collect(conn, "symbols", "all", "ok", len(main_rows),
                       f"全A {len(rows)} 只，主板 {len(main_rows)} 只（接口 total={total}）")
    log.info("股票列表完成：全A %d 只，沪深主板 %d 只（接口 total=%s）", len(rows), len(main_rows), total)
    return len(main_rows)


# ============================================================
# 阶段 2：个股日K（腾讯前复权）
# ============================================================

def _fetch_tencent(symbol: str, start: str, end: str) -> list[tuple]:
    """返回 [(date, open, close, high, low, volume, amount), ...]（升序）"""
    params = f"{symbol},day,{start},{end},{KLINE_COUNT},qfq"
    last_exc: Exception | None = None
    for attempt in range(3):
        for base in TENCENT_HOSTS:
            try:
                payload = json.loads(_get(f"{base}?param={params}").decode())
                node = (payload.get("data") or {}).get(symbol) or {}
                if isinstance(node, list):
                    rows = node
                else:
                    rows = node.get("qfqday") or node.get("day") or []
                out = []
                for r in rows:
                    if len(r) < 5:
                        continue
                    vol = float(r[5]) if len(r) > 5 and r[5] not in (None, "") else None
                    amt = None
                    if len(r) > 6 and r[6] not in (None, ""):
                        try:
                            amt = float(r[6])
                        except (TypeError, ValueError):
                            amt = None
                    out.append((str(r[0]), float(r[1]), float(r[2]), float(r[3]),
                                float(r[4]), vol, amt))
                if out:
                    return out
                last_exc = RuntimeError("返回空K线")
            except Exception as exc:
                last_exc = exc
        time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(f"腾讯K线失败: {last_exc}")


def _collect_one(symbol: str, start: str, end: str) -> dict:
    try:
        bars = _fetch_tencent(symbol, start, end)
        return {"symbol": symbol, "bars": bars, "error": ""}
    except Exception as exc:
        return {"symbol": symbol, "bars": [], "error": str(exc)[:200]}


def stage_bars(limit: int = 0, workers: int = 4, force: bool = False,
               start: str = "2024-01-01") -> None:
    end = date.today().isoformat()
    with rs.open_research() as conn:
        rs.init_db(conn)
        sql = "SELECT symbol FROM research_symbols WHERE is_main_board=1"
        if not force:
            sql += " AND (collect_status<>'ok' OR bar_count=0)"
        sql += " ORDER BY symbol"
        todo = [r[0] for r in conn.execute(sql).fetchall()]
    if limit:
        todo = todo[:limit]
    log.info("待采个股 %d 只（workers=%d，force=%s）", len(todo), workers, force)
    if not todo:
        return

    t0 = time.time()
    done = err = 0
    with ThreadPoolExecutor(max_workers=workers) as pool, rs.open_research() as conn:
        rs.init_db(conn)
        futures = {pool.submit(_collect_one, s, start, end): s for s in todo}
        for idx, fut in enumerate(as_completed(futures), 1):
            res = fut.result()
            sym = res["symbol"]
            if res["bars"]:
                rs.upsert_bars(conn, sym, res["bars"], "tencent", "qfq")
                rs.mark_symbol(conn, sym, "ok", len(res["bars"]),
                               res["bars"][0][0], res["bars"][-1][0])
                done += 1
            else:
                rs.mark_symbol(conn, sym, f"error:{res['error'][:80]}", 0, "", "")
                rs.log_collect(conn, "bars", sym, "error", 0, res["error"])
                err += 1
            if idx % 100 == 0 or idx == len(todo):
                conn.commit()
                el = time.time() - t0
                rate = idx / el if el else 0
                log.info("个股进度 %d/%d（ok=%d err=%d）%.1f 只/秒，预计剩余 %.1f 分钟",
                         idx, len(todo), done, err, rate,
                         (len(todo) - idx) / rate / 60 if rate else -1)
        rs.log_collect(conn, "bars", "all", "ok", done, f"失败 {err} 只")
    log.info("个股采集完成：成功 %d，失败 %d，用时 %.1f 分钟",
             done, err, (time.time() - t0) / 60)


# ============================================================
# 阶段 3：沪深300 基准
# ============================================================

def stage_index() -> None:
    end = date.today().isoformat()
    bars = _fetch_tencent("sh000300", "2024-01-01", end)
    with rs.open_research() as conn:
        rs.init_db(conn)
        rs.upsert_bars(conn, "sh000300", bars, "tencent", "none")
        rs.log_collect(conn, "index", "sh000300", "ok", len(bars), "")
    log.info("沪深300：%d 根（%s ~ %s）", len(bars), bars[0][0], bars[-1][0])


# ============================================================
# 阶段 4：板块指数（东财 BK，历史收盘价）
# ============================================================

def _fetch_board_list() -> list[dict]:
    boards: list[dict] = []
    for kind, fs in (("plate", "m:90+t:2"), ("concept", "m:90+t:3")):
        seen = set()
        for pn in range(1, 30):
            url = (f"{CLIST_HOSTS[0]}?pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3"
                   f"&fs={fs}&fields=f12,f14")
            payload = None
            for host in CLIST_HOSTS:
                try:
                    payload = json.loads(_get(url.replace(CLIST_HOSTS[0], host)).decode())["data"]
                    break
                except Exception:
                    time.sleep(0.5)
            if not payload or not payload.get("diff"):
                break
            for item in payload["diff"]:
                code = str(item.get("f12") or "")
                if code in seen:
                    continue
                seen.add(code)
                boards.append({"code": code, "name": str(item.get("f14") or ""), "kind": kind})
            if len(payload["diff"]) < 100:
                break
        log.info("板块列表 %s：%d 个", kind, len(seen))
    return boards


def _fetch_board_closes(code: str) -> list[tuple[str, float]]:
    qs = (f"?secid=90.{code}&klt=101&fqt=1&lmt={BOARD_KLINE_COUNT}&end=20500101"
          "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56")
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            data = json.loads(_get(BOARD_KLINE_URL + qs).decode()).get("data") or {}
            out = []
            for k in data.get("klines") or []:
                p = k.split(",")
                if len(p) >= 3 and p[2] not in ("", "-"):
                    out.append((p[0], float(p[2])))
            if out:
                return out
            last_exc = RuntimeError("空K线")
        except Exception as exc:
            last_exc = exc
        time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"板块K线失败: {last_exc}")


def stage_boards(workers: int = 3) -> None:
    boards = _fetch_board_list()
    with rs.open_research() as conn:
        rs.init_db(conn)
        rs.upsert_boards(conn, boards)
    log.info("板块元数据入库 %d 个，开始拉历史收盘（workers=%d）", len(boards), workers)

    t0 = time.time()
    ok = err = 0
    with ThreadPoolExecutor(max_workers=workers) as pool, rs.open_research() as conn:
        futures = {pool.submit(_fetch_board_closes, b["code"]): b for b in boards}
        for idx, fut in enumerate(as_completed(futures), 1):
            b = futures[fut]
            try:
                closes = fut.result()
                rs.upsert_board_bars(conn, b["code"], closes)
                ok += 1
            except Exception as exc:
                conn.execute("UPDATE research_boards SET status=? WHERE code=?",
                             (f"error:{str(exc)[:60]}", b["code"]))
                rs.log_collect(conn, "boards", b["code"], "error", 0, str(exc)[:150])
                err += 1
            if idx % 50 == 0 or idx == len(boards):
                conn.commit()
                log.info("板块进度 %d/%d（ok=%d err=%d）%.1f 分钟",
                         idx, len(boards), ok, err, (time.time() - t0) / 60)
            time.sleep(0.05 + random.random() * 0.05)
    log.info("板块采集完成：ok=%d err=%d", ok, err)


def stage_status() -> None:
    with rs.open_research(readonly=True) as conn:
        s = rs.stats(conn)
        for k, v in s.items():
            print(f"{k:>14}: {v}")
        rows = conn.execute(
            "SELECT symbol,name,industry,bar_count,first_date,last_date,collect_status "
            "FROM research_symbols ORDER BY RANDOM() LIMIT 5").fetchall()
        print("\n随机抽查:")
        for r in rows:
            print("  ", dict(r))
        bad = conn.execute(
            "SELECT COUNT(*) FROM research_symbols WHERE collect_status LIKE 'error%'").fetchone()[0]
        print("\n失败标的数:", bad)
        if bad:
            for r in conn.execute(
                    "SELECT symbol,collect_status FROM research_symbols "
                    "WHERE collect_status LIKE 'error%' LIMIT 10"):
                print("  ", r[0], r[1])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="all",
                    choices=["all", "symbols", "bars", "index", "boards", "status"])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--start", default="2024-01-01")
    args = ap.parse_args()

    if args.stage in ("all", "symbols"):
        stage_symbols(limit=args.limit)
    if args.stage in ("all", "bars"):
        stage_bars(limit=args.limit, workers=args.workers, force=args.force, start=args.start)
    if args.stage in ("all", "index"):
        stage_index()
    if args.stage in ("all", "boards"):
        stage_boards(workers=max(2, args.workers - 1))
    if args.stage == "status":
        stage_status()
    if args.stage == "all":
        stage_status()


if __name__ == "__main__":
    main()
