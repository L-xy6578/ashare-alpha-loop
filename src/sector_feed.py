"""
板块快照每日注入 — 东财公开接口（替代手工 inject_sector.py）

问题（2026-08-05 扫描 #21）：生产库 sector_daily 仅 08-03 一天 47 条且资金字段多为 0，
行业景气/资金面因子无真实输入 → 候选评分永远缺因子。

2026-10-30 口径统一（A 方案）：
    - 板块因子**只认东财 bk_ 体系**；腾讯仅保留个股行情兜底，不再作为板块因子输入
      （腾讯兜底快照与东财口径量级差 50–100 倍，混用会让同一只票跨日不可比）。
    - 行业/概念必须分页拉全；2026-11-06 起改为**高覆盖率容忍**：单类缺失板块
      ≤ MAX_MISSING_BOARDS 且条数达标仍落库（complete=1，note 记缺失页与缺口）；
      整页丢失/条数明显不足才整日不落库并告警。
    - 落库时写 source='eastmoney'，并回算 20/60 日涨幅（按交易日历窗口）。

用法:
    python -m src.sector_feed --inject      # 注入今日快照（早盘自动调用）
    python -m src.sector_feed --dry-run     # 预览不落库
    python -m src.sector_feed --date 2026-08-05
"""

from __future__ import annotations

import argparse
import sys
import time

from src.config import MARKET_DB_PATH
from src.logger import get_logger
from src.sector_analysis import (
    MIN_CONCEPT_BOARDS,
    MIN_PLATE_BOARDS,
    SectorAnalyzer,
    SectorSnapshot,
)

log = get_logger(__name__)

CLIST_HOSTS = [
    "https://push2delay.eastmoney.com/api/qt/clist/get",   # delay 镜像（实测可用）
    "https://push2.eastmoney.com/api/qt/clist/get",        # 主域（可能被限流）
]
# 板块日频资金流（主力净流入）；f52 为当日主力净流入（元）
FFLOW_URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
# 2026-11-06 资金口径修复：f170 实测为“5日中单净流入”（不是 20 日主力，含义错误），
# 已从字段列表移除；zljlr_d20 改为按交易日历回溯 20 个交易日、由日频主力累加得到。
# f164 实测等于 daykline 的 5 日主力合计，口径正确，保留。
CLIST_FIELDS = "f12,f14,f2,f3,f8,f62,f109,f164"
FUND_DAYS = 20          # 20 日主力累计口径的交易日数
FUND_CONCURRENCY = 6    # 板块日频资金并发数（1027 板块单线程过慢，6 并发约 1 分钟）
PAGE_MAX = 20           # 分页上限（每页 100 条，足够覆盖东财全量板块）
PAGE_SIZE = 100         # 每页条数（与请求 pz 一致）
PAGE_RETRY = 2          # 每页失败后的重试次数（含首次共 2 轮）
MAX_MISSING_BOARDS = 5  # 高覆盖率容忍：单类缺失板块数 ≤5 仍视为可用快照（2026-11-06 B1）


def _get_json(url: str, timeout: int = 12) -> dict:
    """GET JSON（#117：内置秒级重试，覆盖 DNS/连接短抖动）"""
    from src.net import fetch_json

    return fetch_json(url, timeout=timeout)


def fetch_board_list(kind: str) -> tuple[list[dict], list[int], int]:
    """拉行业(plate)/概念(concept)板块列表（多 host 兜底 + 每页重试）

    字段: f12 代码, f14 名称, f3 当日%, f8 换手%, f62 当日主力净流入,
          f109 5日%, f164 5日主力净流入（实测与 daykline 5 日主力合计一致）
    20 日主力不再取接口窗口字段（f170 实测为 5 日中单），由
    fetch_board_main_flows 按交易日历累加，见 build_snapshots。

    Returns:
        (boards, failed_pages, total)；failed_pages 非空表示有页请求失败，
        是否可用由 build_snapshots 按缺口大小判定（B1 高覆盖率容忍）
    """
    fs = "m:90+t:2" if kind == "plate" else "m:90+t:3"
    boards: list[dict] = []
    failed_pages: list[int] = []
    total = 0
    # #50：不能只拉前 2 页（涨幅榜外的板块会整体缺失 → 资金面因子失配）。
    # 2026-10-30：任一页重试后仍失败 → 记缺失页并停止，当日快照判不完整。
    for pn in range(1, PAGE_MAX + 1):
        qs = (f"?pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3&fs={fs}"
              f"&fields={CLIST_FIELDS}")
        data = None
        last_exc: Exception | None = None
        for _ in range(PAGE_RETRY):
            for host in CLIST_HOSTS:
                try:
                    payload = _get_json(host + qs).get("data") or {}
                    if payload.get("diff"):
                        data = payload
                        break
                except Exception as exc:      # 记录最后一次异常，供失败分类区分 DNS/网络
                    last_exc = exc
                    continue
            if data:
                break
            time.sleep(0.5)
        if not data:
            failed_pages.append(pn)
            log.warning("板块列表拉取失败(%s pn=%d)：%s；重试后仍不可用，判快照不完整",
                        kind, pn, last_exc or "接口无返回内容")
            break
        diff = data.get("diff") or []
        total = max(total, int(data.get("total") or 0))
        for item in diff:
            if not any(b["code"] == item.get("f12") for b in boards):
                boards.append({
                    "code": item.get("f12"),
                    "name": item.get("f14"),
                    "level": item.get("f2"),          # 板块指数点位（自积累）
                    "zdf": item.get("f3"),
                    "hsl": item.get("f8"),
                    "zljlr": item.get("f62"),
                    "zdf5": item.get("f109"),
                    "zljlr_d5": item.get("f164"),
                })
        # B2（2026-11-06）：返回条数 < 每页条数即最后一页，不再追请求越界页
        # （越界页最容易被东财限流/空返回，避免把"正常结束"误判成缺页）。
        # 末页恰好整页时用接口 total 兜底，防止多请求一页。
        if len(diff) < PAGE_SIZE or (total and len(boards) >= total):
            break
    log.info("板块列表拉取 %s: %d 条（缺失页 %s / total=%s）",
            kind, len(boards), failed_pages or "无", total or "未知")
    return boards, failed_pages, total


def _missing_boards(count: int, total: int, failed_pages: list[int]) -> int:
    """估算缺失板块数：优先用接口 total 与实收之差；无 total 时按缺失页 × 每页条数保守估算"""
    if total:
        return max(0, total - count)
    return len(failed_pages) * PAGE_SIZE


def capital_window(trade_date: str, n: int = FUND_DAYS) -> list[str]:
    """按交易日历取最近 n 个交易日的日期窗口（升序）

    采集时点规则：
      - 盘前（当日 15:00 前）或历史日期：当日无完整日频主力 → 窗口截至前一交易日；
      - 收盘后补采当日：窗口含当日。
    交易日来自 trade_calendar（is_trading=1），不按行数回溯。
    """
    from datetime import date, datetime

    from src.db import get_connection

    today = date.today().isoformat()
    if trade_date != today or datetime.now().hour >= 15:
        end_clause, params = "date<=?", (trade_date, n)
    else:
        end_clause, params = "date<?", (trade_date, n)
    try:
        with get_connection(MARKET_DB_PATH, readonly=True) as conn:
            rows = conn.execute(
                f"SELECT date FROM trade_calendar WHERE is_trading=1 AND {end_clause} "
                "ORDER BY date DESC LIMIT ?", params
            ).fetchall()
        return [r[0] for r in reversed(rows)]
    except Exception as exc:
        log.warning("交易日历读取失败（资金 20 日窗口不可用）: %s", exc)
        return []


def fetch_board_main_history(code: str, timeout: int = 6) -> dict[str, float] | None:
    """单个板块的日频主力净流入（万元）：{date: 主力净流入}

    东财 fflow/daykline 的 f52 为当日主力净流入（元），等价于收盘后的 f62。
    失败返回 None（由调用方记为缺失，不做 0 填充）。
    """
    qs = (f"?lmt={FUND_DAYS + 10}&klt=101&secid=90.{code}&fields1=f1,f2,f3,f7"
          "&fields2=f51,f52")
    try:
        data = _get_json(FFLOW_URL + qs, timeout=timeout).get("data") or {}
        klines = data.get("klines") or []
        out: dict[str, float] = {}
        for k in klines:
            parts = k.split(",")
            if len(parts) >= 2 and parts[1] not in ("", "-"):
                out[parts[0]] = float(parts[1]) / 10000   # 元 → 万元
        return out or None
    except Exception:
        return None


def fetch_board_main_flows(codes: list[str]) -> tuple[dict[str, dict[str, float]], list[str]]:
    """批量拉取板块日频主力（并发 + 失败重试一次）

    Returns:
        ({code: {date: 万元}}, failed_codes)
    """
    from concurrent.futures import ThreadPoolExecutor

    def one(code: str) -> tuple[str, dict[str, float] | None]:
        for _ in range(2):
            hist = fetch_board_main_history(code)
            if hist:
                return code, hist
            time.sleep(0.2)
        return code, None

    flows: dict[str, dict[str, float]] = {}
    failed: list[str] = []
    with ThreadPoolExecutor(max_workers=FUND_CONCURRENCY) as pool:
        for code, hist in pool.map(one, codes):
            if hist:
                flows[code] = hist
            else:
                failed.append(code)
    return flows, failed


def calc_zljlr_d20(history: dict[str, float] | None, window: list[str]) -> float | None:
    """20 日主力累计净流入（万元）；窗口内任一交易日缺失 → None（无数据）

    None 表示“无数据/窗口不完整”，由上游写 NULL；0 只表示真实零净流入。
    绝不用相邻日/部分窗口凑数，避免“数据越少、分位越高”的失真。
    """
    if not history or not window:
        return None
    if any(d not in history for d in window):
        return None
    return round(sum(history[d] for d in window), 4)


def _pct(chg) -> float:
    try:
        return float(chg) if chg is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def build_snapshots(trade_date: str | None = None,
                    persist_history: bool = True) -> dict:
    """构建当日东财板块快照（不含腾讯源）

    2026-11-10 冻结期 bug 修复（扫描发现 #158/#159）：
        20/60 日涨幅不再"现拉前 21 个板块 K 线 + 其余继承上一次的值"，
        改为把当日已采到的板块收盘点位落进 `sector_price_history`，
        再用历史表按交易日历回看 20/60 个交易日计算。算不出来写 NULL
        （计入缺失），不再用继承值冒充当日真值。

    Returns:
        {"snaps","plate_count","concept_count","failed_pages","complete","source",
         "asof","hist_written","zdf20_missing"}
        complete=False 时注入层不落库（见 inject）。
    """
    from datetime import date as date_cls

    from src import sector_history as sh

    trade_date = trade_date or date_cls.today().isoformat()
    snaps: list[SectorSnapshot] = []

    plates, plate_failed, plate_total = fetch_board_list("plate")
    concepts, concept_failed, concept_total = fetch_board_list("concept")

    # 2026-11-06 资金口径修复：zljlr_d20 不再取 f170（实测为 5 日中单，含义错误），
    # 改为按交易日历回溯 FUND_DAYS 个交易日、由日频主力（fflow f52，等价收盘 f62）累加。
    window = capital_window(trade_date)
    all_codes = [b["code"] for b in plates + concepts if b.get("code")]
    flows, fund_failed = fetch_board_main_flows(all_codes) if window else ({}, all_codes)
    d20_map = {c: calc_zljlr_d20(flows.get(c), window) for c in all_codes}
    d20_valid = sum(1 for v in d20_map.values() if v is not None)
    log.info("资金 20 日累计: 窗口 %s~%s（%d 个交易日）有效板块 %d/%d，拉取失败 %d 个",
             window[0] if window else "-", window[-1] if window else "-", len(window),
             d20_valid, len(all_codes), len(fund_failed))
    if fund_failed:
        log.warning("板块日频资金拉取失败（按缺失处理，不填充）: %s%s",
                    fund_failed[:10], "…" if len(fund_failed) > 10 else "")

    # 20/60 日涨幅：先落历史表，再从历史表算（零额外请求）
    asof = sh.effective_close_date(trade_date)
    hist_written = 0
    if (persist_history and asof and trade_date == date_cls.today().isoformat()
            and sh.is_close_price_time()):
        hist_written = sh.upsert_closes(asof, [
            {"code": f"bk_{b['code']}", "name": b["name"], "kind": kind,
             "close": b.get("level"), "pct": b.get("zdf")}
            for kind, boards in (("plate", plates), ("concept", concepts))
            for b in boards
        ])
    z20, base20 = sh.zdf_by_code(asof, 20)
    z60, base60 = sh.zdf_by_code(asof, 60)
    log.info("板块涨幅历史：归属收盘日 %s，写入 %d 条；20 日基准 %s 可算 %d 个，"
             "60 日基准 %s 可算 %d 个", asof or "-", hist_written, base20 or "-",
             len(z20), base60 or "-", len(z60))
    if asof and (not z20 or len(z20) < 0.9 * len(all_codes)):
        log.warning("板块 20 日涨幅可算板块仅 %d/%d（历史表未覆盖），缺失板块写 NULL",
                    len(z20), len(all_codes))

    for b in plates:
        code = f"bk_{b['code']}"
        snaps.append(SectorSnapshot(
            code=code, name=b["name"], kind="plate", date=trade_date,
            zdf=_pct(b["zdf"]), zdf5=_pct(b["zdf5"]),
            zdf20=z20.get(code), zdf60=z60.get(code),
            # #35：东财资金字段单位是"元"，库内约定"万元"，需 /10000
            zljlr=_pct(b["zljlr"]) / 10000,
            zljlr_d5=_pct(b["zljlr_d5"]) / 10000,
            zljlr_d20=d20_map.get(b["code"]),
            hsl=_pct(b["hsl"]),
            level=_pct(b.get("level")),
            source="eastmoney",
        ))

    for b in concepts:
        code = f"bk_{b['code']}"
        snaps.append(SectorSnapshot(
            code=code, name=b["name"], kind="concept", date=trade_date,
            zdf=_pct(b["zdf"]), zdf5=_pct(b["zdf5"]),
            zdf20=z20.get(code), zdf60=z60.get(code),
            zljlr=_pct(b["zljlr"]) / 10000,
            zljlr_d5=_pct(b["zljlr_d5"]) / 10000,
            zljlr_d20=d20_map.get(b["code"]),
            hsl=_pct(b["hsl"]),
            level=_pct(b.get("level")),
            source="eastmoney",
        ))

    plate_count = sum(1 for s in snaps if s.kind == "plate")
    concept_count = sum(1 for s in snaps if s.kind == "concept")
    zdf20_missing = sum(1 for s in snaps if s.zdf20 is None)
    # B1（2026-11-06）：缺页零容忍 → 高覆盖率容忍。
    # 条数达标且单类缺口 ≤ MAX_MISSING_BOARDS 时仍视为可用快照；
    # 缺口过大（整页丢失）或条数明显不足才整日拒绝，避免 1025/1027 却整天丢弃。
    plate_missing = _missing_boards(plate_count, plate_total, plate_failed)
    concept_missing = _missing_boards(concept_count, concept_total, concept_failed)
    count_ok = (plate_count >= MIN_PLATE_BOARDS
                and concept_count >= MIN_CONCEPT_BOARDS)
    complete = (count_ok
                and plate_missing <= MAX_MISSING_BOARDS
                and concept_missing <= MAX_MISSING_BOARDS)
    log.info("板块快照构建完成: %s plate=%d/%s concept=%d/%s 缺失页=%s 缺失板块=%d/%d 完整=%s",
             trade_date, plate_count, plate_total or "?", concept_count,
             concept_total or "?", (plate_failed + concept_failed) or "无",
             plate_missing, concept_missing, complete)
    return {"snaps": snaps, "plate_count": plate_count,
            "concept_count": concept_count,
            "failed_pages": plate_failed + concept_failed,
            "plate_total": plate_total, "concept_total": concept_total,
            "plate_missing": plate_missing, "concept_missing": concept_missing,
            "complete": complete, "source": "eastmoney",
            "asof": asof, "hist_written": hist_written,
            "zdf20_missing": zdf20_missing}


def _fund_data_ok(snaps: list[SectorSnapshot]) -> bool:
    """快照必须带真实资金数据：20 日主力净流入不能全为 NULL/0（否则资金面因子被中性化）"""
    return bool(snaps) and any(
        s.zljlr_d20 is not None and abs(s.zljlr_d20) > 0 for s in snaps)


def inject(trade_date: str | None = None, dry_run: bool = False) -> int:
    """注入当日板块快照；数据不可用时宁可不落库（让守护重试）

    门禁（2026-10-30 A 方案 + 2026-11-06 B1 放宽）：
      1. 资金字段全 0 → 不落库；
      2. plate/concept 条数不足阈值 → 不落库，写 meta(complete=0) 并告警；
      3. 有缺页但缺口 ≤ MAX_MISSING_BOARDS 且条数达标 → 高覆盖率容忍落库，
         meta(complete=1) 的 note 记录缺失页与缺口数量；
      4. 腾讯源不再兜底板块因子（只保留个股行情兜底）。
    """
    from datetime import date

    day = trade_date or date.today().isoformat()
    built: dict = {}
    snaps: list[SectorSnapshot] = []
    for attempt in range(2):        # 东财多 host + fetch_json 内部短重试，外层再兜一轮
        try:
            built = build_snapshots(day, persist_history=not dry_run)
            snaps = built.get("snaps") or []
        except Exception as exc:
            log.warning("东财板块快照构建异常（第%d轮）: %s", attempt + 1, exc)
            built, snaps = {}, []
        if _fund_data_ok(snaps):
            break
        if attempt == 0:
            time.sleep(2)

    complete = bool(built.get("complete")) and _fund_data_ok(snaps)
    source = built.get("source") or ""
    plate_count = built.get("plate_count", 0)
    concept_count = built.get("concept_count", 0)
    plate_total = built.get("plate_total", 0)
    concept_total = built.get("concept_total", 0)
    plate_missing = built.get("plate_missing", 0)
    concept_missing = built.get("concept_missing", 0)
    failed_pages = built.get("failed_pages", [])
    zdf20_missing = built.get("zdf20_missing", 0)
    zdf20_note = (f"20日涨幅缺失 {zdf20_missing}/{len(snaps)}（历史表不足，已写 NULL）"
                  if snaps and zdf20_missing else "")
    note = ""
    if not _fund_data_ok(snaps):
        note = "资金字段全 NULL/0 或板块列表不可用"
    elif plate_count < MIN_PLATE_BOARDS or concept_count < MIN_CONCEPT_BOARDS:
        note = (f"快照不完整：条数不足 plate={plate_count}（阈值 {MIN_PLATE_BOARDS}）/ "
                f"concept={concept_count}（阈值 {MIN_CONCEPT_BOARDS}）")
    elif not complete:
        note = (f"快照不完整：缺口过大 plate 缺 {plate_missing} 个 / "
                f"concept 缺 {concept_missing} 个 / 缺失页={failed_pages or '无'}")
    elif failed_pages:
        # B1：高覆盖率容忍落库，但必须把缺页与缺口写进 meta，便于事后追溯
        note = (f"东财高覆盖：plate={plate_count}/{plate_total or '?'}（缺 {plate_missing} 个）/ "
                f"concept={concept_count}/{concept_total or '?'}（缺 {concept_missing} 个）/ "
                f"缺失页={failed_pages}")
    else:
        note = f"东财全量：plate={plate_count} / concept={concept_count}"
    if zdf20_note:
        note = f"{note}；{zdf20_note}"

    if dry_run:
        if complete:
            print(f"（dry-run）将注入 {len(snaps)} 条板块快照（{day}，东财）")
        else:
            print(f"（dry-run）快照不完整，不会落库（{day}）：{note}")
        return 0

    analyzer = SectorAnalyzer(MARKET_DB_PATH)
    if not complete:
        analyzer.save_meta(day, source or "eastmoney", plate_count, concept_count,
                           False, note)
        print(f"❌ 板块快照不完整（{day}）：{note}；本次不落库，稍后自动重试")
        return 1

    n = analyzer.save_snapshot(snaps)
    # B1：落库 note 必须与实际缺口一致（不能写死"东财全量"，否则缺页信息丢失）
    analyzer.save_meta(day, source, plate_count, concept_count, True, note)
    # 20/60 日涨幅已由 sector_history 在 build_snapshots 内按交易日历算好，
    # 不再调用 refresh_zdf_from_levels（它的输入是 sector_daily 行数口径，会覆盖 NULL）。
    print(f"✅ 板块快照注入 {n} 条（{day}，东财 plate={plate_count} / concept={concept_count}）")
    if zdf20_note:
        print(f"  ⚠️ {zdf20_note}")
    return 0


def migrate_d20_null(trade_date: str, dry_run: bool = False) -> int:
    """按当日 20 日窗口完整性判定，把“无数据”板块的 zljlr_d20 置 NULL（幂等）

    判定依据是“该板块当日的交易日历窗口是否逐日都有日频主力”，
    不是“现有值是不是 0”——真实零净流入（窗口完整、合计恰为 0）继续保持 0。
    只把“拉取成功但窗口不完整”的行写 NULL；完整窗口保留原值（不重写），
    拉取失败的板块无法判定完整性，跳过不动（绝不凭猜测置 NULL）。
    重复执行结果一致。
    """
    import sqlite3

    window = capital_window(trade_date)
    if len(window) < FUND_DAYS:
        print(f"[migrate] 交易日历不足 {FUND_DAYS} 日（{trade_date}，实际 {len(window)}），中止")
        return 1

    with sqlite3.connect(MARKET_DB_PATH) as conn:
        info = [r for r in conn.execute("PRAGMA table_info(sector_daily)") if r[1] == "zljlr_d20"]
        if info and info[0][3]:   # notnull=1
            print("[migrate] zljlr_d20 仍为 NOT NULL，无法写入 NULL，请先重建表")
            return 1
        rows = conn.execute(
            "SELECT code, zljlr_d20 FROM sector_daily WHERE date=? ORDER BY code",
            (trade_date,)).fetchall()

    codes = [r[0].replace("bk_", "") for r in rows]
    flows, failed = fetch_board_main_flows(codes)
    failed_set = set(failed)
    if failed_set and len(failed_set) > max(5, int(0.05 * len(codes))):
        print(f"[migrate] 日频资金拉取失败 {len(failed_set)}/{len(codes)}（>5%），"
              f"数据源不可用，中止且未写库")
        return 1

    new_vals: dict[str, float | None] = {}
    skipped = 0
    for full_code in [r[0] for r in rows]:
        code = full_code.replace("bk_", "")
        if code in failed_set:
            skipped += 1
            continue
        new_vals[full_code] = calc_zljlr_d20(flows.get(code), window)

    null_codes = [c for c, v in new_vals.items() if v is None]
    kept_n = len(new_vals) - len(null_codes)
    diff_n = sum(1 for c, old in rows
                 if c in new_vals and new_vals[c] is not None and new_vals[c] != old)
    print(f"[migrate] {trade_date} 窗口 {window[0]}~{window[-1]}（{len(window)} 日），"
          f"板块 {len(rows)}，拉取失败 {len(failed_set)}")
    print(f"[migrate] 结果：置 NULL={len(null_codes)} / 完整保留={kept_n} / "
          f"拉取失败跳过={skipped}（参考：完整板块重算值与现值不同 {diff_n} 条，不写库）")
    if null_codes:
        print("[migrate] NULL 板块示例:", ", ".join(c.replace("bk_", "") for c in null_codes[:10]))
    if dry_run:
        print("[migrate] dry-run：未写库")
        return 0

    with sqlite3.connect(MARKET_DB_PATH) as conn:
        conn.executemany("UPDATE sector_daily SET zljlr_d20=NULL WHERE date=? AND code=?",
                         [(trade_date, c) for c in null_codes])
        conn.commit()
    print(f"[migrate] 已写库：{len(null_codes)} 条置 NULL（重复执行结果一致）")
    return 0


def _main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="板块快照每日注入（东财公开接口）")
    parser.add_argument("--inject", action="store_true", help="注入今日快照（默认）")
    parser.add_argument("--dry-run", action="store_true", help="预览不落库")
    parser.add_argument("--date", default=None, help="交易日 YYYY-MM-DD（默认今天）")
    parser.add_argument("--null-d20", action="store_true",
                        help="按 20 日窗口完整性判定，把无数据板块的 zljlr_d20 置 NULL（幂等）")
    args = parser.parse_args()
    if args.null_d20:
        if not args.date:
            parser.error("--null-d20 需配合 --date YYYY-MM-DD")
        sys.exit(migrate_d20_null(args.date, dry_run=args.dry_run))
    sys.exit(inject(args.date, dry_run=args.dry_run))


if __name__ == "__main__":
    _main()
