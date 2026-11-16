"""基本面数据采集（研究用，只写 data/research.db）

两个 stage：
    valuation — 东财 RPT_VALUEANALYSIS_DET 逐日 PE_TTM / PB_MRQ（按 TRADE_DATE）
    earnings  — 东财 RPT_LICO_FN_CPD 报告期 / NOTICE_DATE（公告日）/ UPDATE_DATE / SJLTZ

时点还原口径（2026-11-11 用户更正）：
    业绩因子的"可得日" = **NOTICE_DATE（首次公告日）**；
    UPDATE_DATE 是"最后修改日"，财报更正会把它改到未来（例：000001 2025-09-30 期，
    NOTICE_DATE=2025-10-25 而 UPDATE_DATE=2026-10-24），**不能**用作时点。
    本模块把两列都落库，仅 NOTICE_DATE 用于主口径，UPDATE_DATE 只做对照。

用法:
    python -m scripts.research_fundamentals --stage valuation --limit 20   # 小样本自测
    python -m scripts.research_fundamentals --stage valuation
    python -m scripts.research_fundamentals --stage earnings
    python -m scripts.research_fundamentals --stage status
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import research_store as rs  # noqa: E402

HOSTS = ("https://datacenter-web.eastmoney.com",
         "https://datacenter.eastmoney.com")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

VAL_START = "2024-01-01"        # 研究窗口 2024-03-19 起，估值只需当日值，往前留余量
ERN_START = "2022-06-30"        # 最早需要 2022 年报/2023 年报表
VAL_PAGE_SIZE = 500
ERN_PAGE_SIZE = 100
TIMEOUT = 20
RETRIES = 4

_print_lock = threading.Lock()


def _url(host: str, report: str, columns: str, filter_expr: str,
         page: int, page_size: int, sort_col: str, sort_type: int) -> str:
    return (f"{host}/api/data/v1/get?reportName={report}&columns={columns}"
            f"&filter={urllib.parse.quote(filter_expr, safe='()')}"
            f"&pageNumber={page}&pageSize={page_size}"
            f"&sortColumns={sort_col}&sortTypes={sort_type}"
            f"&source=WEB&client=WEB")


def _get_json(url: str) -> dict:
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Referer": "https://data.eastmoney.com/"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def _get_with_retry(report: str, columns: str, filter_expr: str, page: int,
                    page_size: int, sort_col: str, sort_type: int) -> dict:
    last = ""
    for attempt in range(RETRIES):
        host = HOSTS[attempt % len(HOSTS)]
        try:
            return _get_json(_url(host, report, columns, filter_expr,
                                  page, page_size, sort_col, sort_type))
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            time.sleep(0.8 * (attempt + 1) + random.random() * 0.5)
    raise RuntimeError(last)


def _date(v) -> str | None:
    if not v:
        return None
    s = str(v)
    return s[:10] if len(s) >= 10 else s


def _num(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ============================================================
# 估值
# ============================================================

def fetch_valuation(row: dict) -> tuple[list[tuple], str, str]:
    code = row["code"]
    filt = f'(SECURITY_CODE="{code}")(TRADE_DATE>=\'{VAL_START}\')'
    out: dict[str, tuple] = {}
    page, pages = 1, 1
    while page <= pages:
        d = _get_with_retry("RPT_VALUEANALYSIS_DET",
                            "SECURITY_CODE,TRADE_DATE,PE_TTM,PB_MRQ",
                            filt, page, VAL_PAGE_SIZE, "TRADE_DATE", 1)
        res = d.get("result") or {}
        pages = int(res.get("pages") or 0)
        for r in res.get("data") or []:
            dstr = _date(r.get("TRADE_DATE"))
            if dstr:
                out[dstr] = (dstr, _num(r.get("PE_TTM")), _num(r.get("PB_MRQ")))
        page += 1
        time.sleep(0.03 + random.random() * 0.05)
    rows = [out[k] for k in sorted(out)]
    status = "ok" if rows else "empty"
    return rows, status, f"pages={pages}"


# ============================================================
# 业绩
# ============================================================

def fetch_earnings(row: dict) -> tuple[list[tuple], str, str]:
    secucode = f"{row['code']}.{row['market'].upper()}"
    filt = f'(SECUCODE="{secucode}")(REPORTDATE>=\'{ERN_START}\')'
    out: dict[str, tuple] = {}
    page, pages = 1, 1
    while page <= pages:
        d = _get_with_retry("RPT_LICO_FN_CPD",
                            "SECURITY_CODE,REPORTDATE,NOTICE_DATE,UPDATE_DATE,SJLTZ,PARENT_NETPROFIT",
                            filt, page, ERN_PAGE_SIZE, "REPORTDATE", 1)
        res = d.get("result") or {}
        pages = int(res.get("pages") or 0)
        for r in res.get("data") or []:
            rd = _date(r.get("REPORTDATE"))
            if not rd:
                continue
            nd, ud = _date(r.get("NOTICE_DATE")), _date(r.get("UPDATE_DATE"))
            rec = (rd, nd, ud, _num(r.get("SJLTZ")), _num(r.get("PARENT_NETPROFIT")))
            old = out.get(rd)
            # 同一报告期若多行，保留 UPDATE_DATE 最新的一行（更正后的值）
            if old is None or (ud or "") > (old[2] or ""):
                out[rd] = rec
        page += 1
        time.sleep(0.03 + random.random() * 0.05)
    rows = [out[k] for k in sorted(out)]
    status = "ok" if rows else "empty"
    return rows, status, f"pages={pages}"


FETCHERS = {"valuation": fetch_valuation, "earnings": fetch_earnings}
WRITERS = {"valuation": rs.upsert_valuation, "earnings": rs.upsert_earnings}


def collect(stage: str, limit: int, workers: int, force: bool) -> None:
    t0 = time.time()
    fetch, write = FETCHERS[stage], WRITERS[stage]
    with rs.open_research() as conn:
        rs.init_db(conn)
        syms = [dict(r) for r in conn.execute(
            "SELECT symbol, code, market FROM research_symbols "
            "WHERE is_main_board=1 AND bar_count>0 AND is_st=0 ORDER BY symbol")]
        if limit:
            syms = syms[:limit]
        done = {r[0] for r in conn.execute(
            "SELECT DISTINCT target FROM research_collect_log "
            "WHERE stage=? AND status IN ('ok','empty')", (stage,))}
        todo = syms if force else [s for s in syms if s["symbol"] not in done]
        print(f"[{stage}] 总标的 {len(syms)}，已完成 {len(syms) - len(todo)}，"
              f"本轮待采 {len(todo)}，并发 {workers}", flush=True)

        ok = empty = err = rows_written = 0
        n_done = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(fetch, s): s for s in todo}
            for fut in as_completed(futs):
                s = futs[fut]
                try:
                    rows, status, msg = fut.result()
                except Exception as e:  # noqa: BLE001
                    rows, status, msg = [], "error", f"{type(e).__name__}: {e}"
                if rows:
                    rows_written += write(conn, s["symbol"], rows)
                rs.log_collect(conn, stage, s["symbol"], status, len(rows), msg)
                if status == "ok":
                    ok += 1
                elif status == "empty":
                    empty += 1
                else:
                    err += 1
                n_done += 1
                if n_done % 20 == 0:
                    conn.commit()
                if n_done % 50 == 0 or n_done == len(todo):
                    el = time.time() - t0
                    rate = n_done / el if el else 0
                    eta = (len(todo) - n_done) / rate if rate else 0
                    with _print_lock:
                        print(f"[{stage}] {n_done}/{len(todo)} ok={ok} empty={empty} "
                              f"err={err} rows={rows_written} "
                              f"{rate:.1f}只/s ETA {eta / 60:.1f}min", flush=True)
        conn.commit()
        el = time.time() - t0
        print(f"[{stage}] 完成：ok={ok} empty={empty} err={err} 写入 {rows_written} 行，"
              f"耗时 {el / 60:.1f} 分钟", flush=True)


def show_status() -> None:
    with rs.open_research(readonly=True) as conn:
        print(json.dumps(rs.stats(conn), ensure_ascii=False, indent=2))
        for stage in ("valuation", "earnings"):
            rows = conn.execute(
                "SELECT status, COUNT(*), SUM(rows) FROM research_collect_log "
                "WHERE stage=? GROUP BY status", (stage,)).fetchall()
            print(stage, [tuple(r) for r in rows])
        cov = conn.execute(
            "SELECT COUNT(DISTINCT symbol) FROM research_valuation").fetchone()[0]
        print("估值覆盖标的数:", cov)
        cov2 = conn.execute(
            "SELECT COUNT(DISTINCT symbol) FROM research_earnings").fetchone()[0]
        print("业绩覆盖标的数:", cov2)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True,
                    choices=["valuation", "earnings", "status"])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    if a.stage == "status":
        show_status()
    else:
        collect(a.stage, a.limit, a.workers, a.force)


if __name__ == "__main__":
    main()
