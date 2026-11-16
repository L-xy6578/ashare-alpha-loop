"""
观察池换血（#59 最小可执行版 · 演练优先）

背景：观察池从 08-05 的 22 只衰减到 10 只，且三个月没有新增候选。
本模块按 #59 草案做"最小可执行版"，只做两件事：
  1. 从**主赛道 TOP 板块成分**里选候选（不追涨，不用近期涨幅做入池理由）；
  2. 用**硬数据门槛**过滤（缺一不可），再用五因子评分分层。

选入门槛（硬性，缺一不可）：
  1. 近 20 个交易日窗口内 K 线 ≥10 根（技术面可算）；
  2. 有业绩证据（东财业绩报表 归母同比，业绩趋势可算）；
  3. 板块可映射（候选来自主赛道成分，行业/资金因子可算）。
评分分层：五因子 ≥80 入池候补；≥85 才推荐并登记 KPI（门槛保持 85 不动）。
单轮上限：新增 ≤15（2026-11-10 由 3 提到 15）；池容量上限 80 只；
退出：近 20 个评分日 ≥10 次且均分 <70 → 软删（持仓豁免）。
接入：收盘闭环 2.6 步（评分前）自动执行，失败只进 warnings。

安全约定：
  - 默认 dry-run：只输出 outputs/选股/观察池换血_YYYY-MM-DD.md（标注"待用户确认"），
    绝不写生产 watchlist；
  - --apply 才写入（用户确认后手动执行，source=auto，软记录评分与理由）；
  - 同日重跑覆盖同一文件（幂等），不追加。

用法:
    python -m src.pool_refresh              # 演练（默认）
    python -m src.pool_refresh --apply      # 确认后写入观察池（≤3 只）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from src.config import MARKET_DB_PATH
from src.dates import recent_window
from src.executable import is_buyable_code
from src.factors import (
    growth_from_earnings,
    is_aggregate_board,
    score_candidate,
)
from src.logger import get_logger
from src.net import fetch_bytes, fetch_json
from src.sector_analysis import SectorAnalyzer
from src.watchlist import WatchlistStore, _symbol_prefix

log = get_logger(__name__)

# 东财数据中心（业绩报表/核心题材板块映射均在此域名，实测可用；push2 行情域可能被限流）
DATACENTER = "https://datacenter-web.eastmoney.com/api/data/v1/get"
THEME_REPORT = "RPT_F10_CORETHEME_BOARDTYPE"

POOL_ENTRY_THRESHOLD = 80.0    # 入池候补门槛（#59，冻结门槛，不动）
MAX_NEW_PER_RUN = 15           # 单轮新增上限（2026-11-10 由 3 提到 15：池子两周内成型）
POOL_CAP = 80                  # active 池容量上限（超限按评分保留，85 只已演练过）
POOL_HISTORY_DAYS = 20         # 淘汰判据回看窗口（交易日）
POOL_EXIT_MIN_DAYS = 10        # 窗口内至少被评分 N 次才判淘汰（样本不足不淘汰）
POOL_EXIT_MIN_AVG = 70.0       # 窗口内平均分低于此值 → 长期低分移出
MAX_SCAN_PER_RUN = 100         # 单轮最多评估标的（扩围后 60–100，控制请求量）
MIN_SAMPLE_PER_RUN = 60        # 扩围后的最少样本数
MAX_SCAN_PER_TRACK = 8         # 单赛道最多取前 N 只成分
BAD_NAME_MARKERS = ("ST", "退")  # 风险警示/退市整理不计入候选

# 全板块分层配额：热 10×3 / 中 20×2 / 广 25×1 / 冷 5×1（合计 100；去重后不足 60 由 reserve 补足）
HOT_BOARDS, HOT_QUOTA = 10, 3
MID_BOARDS, MID_QUOTA = 20, 2
BROAD_BOARDS, BROAD_QUOTA = 25, 1
COLD_SAMPLES, COLD_QUOTA = 5, 1

# 东财板块成分行情域（按成交额降序；push2delay 实测可用）
BOARD_CLIST_HOSTS = (
    "https://push2delay.eastmoney.com",
    "https://push2.eastmoney.com",
)


@dataclass(frozen=True)
class CandidateRow:
    """单个候选的评估结果（含各门槛结论）"""

    symbol: str
    code: str
    name: str
    board: str
    status: str                  # 候选 / 门槛未过 / 评分不足 / 已持仓 / 已在池
    close: float | None = None
    result: object | None = None  # DataScoreResult（未过门槛时为 None）
    growth: float | None = None
    evidence: str = ""

    @property
    def weighted(self) -> float | None:
        return getattr(self.result, "weighted", None)


def fetch_board_constituents(board: str, *, page_size: int = 100,
                             max_pages: int = 3,
                             fetcher=fetch_json) -> list[dict]:
    """按板块名称拉成分股（东财核心题材板块映射，返回 [{code,name}]）

    说明：不走 push2 clist（该域在本机常被 RemoteDisconnected），
    改用 datacenter 报表按 BOARD_NAME 精确过滤，实测稳定。
    """
    flt = urllib.parse.quote(f'(BOARD_NAME="{board}")')
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        url = (f"{DATACENTER}?reportName={THEME_REPORT}"
               f"&columns=SECURITY_CODE,SECURITY_NAME_ABBR,BOARD_NAME"
               f"&filter={flt}&pageSize={page_size}&pageNumber={page}"
               f"&sortColumns=SECURITY_CODE&sortTypes=1")
        try:
            data = fetcher(url, timeout=12)
        except Exception as exc:
            log.warning("板块成分拉取失败 %s: %s", board, exc)
            break
        rows = ((data.get("result") or {}).get("data") or [])
        if not rows:
            break
        for r in rows:
            code = str(r.get("SECURITY_CODE") or "")
            name = str(r.get("SECURITY_NAME_ABBR") or "")
            if len(code) == 6 and code.isdigit() and name:
                out.append({"code": code, "name": name})
        pages = (data.get("result") or {}).get("pages") or 1
        if page >= pages:
            break
    log.info("板块成分 %s: %d 只", board, len(out))
    return out


def fetch_board_stocks(board_code: str, board_name: str, limit: int = 3,
                       fetcher=fetch_json) -> tuple[list[dict], str]:
    """按成交额降序拉板块成分股（扩围用）

    Returns:
        (stocks, sort_source)；sort_source = clist_amount / name_fallback / empty
    """
    fs_code = board_code.split("_", 1)[-1] if "_" in board_code else board_code
    qs = (f"/api/qt/clist/get?pn=1&pz={max(limit, 10)}&po=1&np=1&fltt=2&invt=2"
          f"&fid=f6&fs=b:{fs_code}&fields=f12,f14,f6")
    for host in BOARD_CLIST_HOSTS:
        try:
            data = fetcher(host + qs, timeout=10).get("data") or {}
        except Exception:
            continue
        diff = data.get("diff") or []
        out = []
        for it in diff:
            code = str(it.get("f12") or "")
            name = str(it.get("f14") or "")
            if len(code) == 6 and code.isdigit() and name:
                out.append({"code": code, "name": name})
        if out:
            return out[:limit], "clist_amount"
    members = fetch_board_constituents(board_name)
    return members[:limit], ("name_fallback" if members else "empty")


def eligible_boards(analyzer) -> list[dict]:
    """全部行业板块（剔除可疑板块与聚合板块），按 20 日涨幅降序"""
    plates = analyzer.load_date(kind="plate", exclude_suspicious=True)
    # zdf20 为 NULL（板块历史不足）或 0 的板块无法排序，直接不参与候选板块分层
    boards = [{"name": p.name, "code": p.code, "zdf20": round(float(p.zdf20), 2)}
              for p in plates
              if not is_aggregate_board(p.name)
              and p.zdf20 is not None and p.zdf20 != 0]
    boards.sort(key=lambda b: (-b["zdf20"], b["name"]))
    return boards


def board_plan(boards: list[dict]) -> list[dict]:
    """分层配额：热 10×3 / 中 20×2 / 广 25×1 / 冷 5×1

    冷样本从第 55 名之后等距抽取，保证非榜单板块也有机会进样本；
    每个被扫板块至少取 1 只（quota>=1）。
    """
    n = len(boards)
    out: list[dict] = []
    for i, b in enumerate(boards):
        if i < HOT_BOARDS:
            q, tier = HOT_QUOTA, "hot"
        elif i < HOT_BOARDS + MID_BOARDS:
            q, tier = MID_QUOTA, "mid"
        elif i < HOT_BOARDS + MID_BOARDS + BROAD_BOARDS:
            q, tier = BROAD_QUOTA, "broad"
        else:
            break
        out.append({**b, "quota": q, "tier": tier})
    tail_start = HOT_BOARDS + MID_BOARDS + BROAD_BOARDS
    if n > tail_start:
        span = n - tail_start
        for j in range(COLD_SAMPLES):
            idx = tail_start + int(span * (j + 0.5) / COLD_SAMPLES)
            idx = min(idx, n - 1)
            if boards[idx]["name"] not in {x["name"] for x in out}:
                out.append({**boards[idx], "quota": COLD_QUOTA, "tier": "cold"})
    return out


SINA_KLINE = ("https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
              "?symbol={symbol}&scale=240&ma=no&datalen=60")
REQUEST_GAP = 0.25             # 单轮评估的请求间隔（防触发行情源限流）


class BarSeries(list):
    """K线序列 + 来源/复权口径（技术因子只接受前复权 qfq）"""

    def __init__(self, bars=(), source: str = "injected", adjust: str = "qfq"):
        super().__init__(bars)
        self.source = source          # tencent / sina / injected / none
        self.adjust = adjust          # qfq / raw / unknown


def _default_bars_provider(symbol: str, start: str, end: str):
    """K线：腾讯前复权优先；新浪不复权仅作展示兜底，技术因子不使用"""
    from src.data_source import TencentHttpDataSource

    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            bars = TencentHttpDataSource().daily_kline(symbol, start, end)
            if bars:
                return BarSeries(bars, source="tencent", adjust="qfq")
            break
        except Exception as exc:      # 501/限流/网络抖动 → 短退避重试一次
            last_exc = exc
            time.sleep(1.0 + attempt)
    try:
        bars = fetch_sina_bars(symbol, start, end)
        if bars:
            log.warning("行情源回退新浪（不复权）%s：技术因子按缺失处理", symbol)
        return BarSeries(bars, source="sina", adjust="raw")
    except Exception as exc:
        log.warning("K线双源失败 %s: %s", symbol, last_exc or exc)
        return BarSeries([], source="none", adjust="unknown")


def fetch_sina_bars(symbol: str, start: str, end: str) -> list:
    """新浪日K兜底（不复权；仅用于收盘价展示，技术因子不使用）"""
    from src.data_source import KlineBar

    raw = fetch_bytes(SINA_KLINE.format(symbol=symbol), timeout=12,
                      headers={"User-Agent": "Mozilla/5.0",
                               "Referer": "https://finance.sina.com.cn/"})
    rows = json.loads(raw.decode("utf-8", "replace"))
    out = []
    for r in rows or []:
        day = str(r.get("day") or "")[:10]
        if not (start <= day <= end):
            continue
        try:
            out.append(KlineBar(day, float(r["open"]), float(r["close"]),
                                float(r["high"]), float(r["low"]),
                                volume=float(r.get("volume") or 0)))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out, key=lambda b: b.date)


def _default_earnings_provider(code: str) -> dict | None:
    from src.finance_feed import fetch_earnings

    return fetch_earnings(code)


def _default_valuation_provider(symbol: str) -> dict:
    from src.data_source import fetch_valuation_pe_pb

    return fetch_valuation_pe_pb(symbol)


def plan(
    today: str | None = None,
    *,
    scope: str = "all",
    watch_store: WatchlistStore | None = None,
    analyzer: SectorAnalyzer | None = None,
    constituents_provider=None,
    stocks_provider=None,
    bars_provider=None,
    earnings_provider=None,
    valuation_provider=None,
    max_new: int = MAX_NEW_PER_RUN,
    max_scan: int = MAX_SCAN_PER_RUN,
    max_per_track: int = MAX_SCAN_PER_TRACK,
    throttle: float = REQUEST_GAP,
) -> dict:
    """生成观察池换血方案（纯计算，不写 watchlist；供演练与测试注入）

    Args:
        scope: "all"=全行业板块分层配额（扩围，默认）/"tracks"=旧 TOP7 口径
        stocks_provider: 测试注入 (board_code, board_name, limit) -> ([{code,name}], sort_source)

    Returns:
        {"today","scope","tracks","scanned","new","gaps","stats","boards_*"}
    """
    today = today or date.today().isoformat()
    watch = watch_store or WatchlistStore()
    analyzer = analyzer or SectorAnalyzer(MARKET_DB_PATH)
    constituents_provider = constituents_provider or fetch_board_constituents
    bars_provider = bars_provider or _default_bars_provider
    earnings_provider = earnings_provider or _default_earnings_provider
    valuation_provider = valuation_provider or _default_valuation_provider

    start, end = recent_window(days_back=20, end=today, market_db=MARKET_DB_PATH)
    # 用库内最新板块快照（非交易日/当日未注入时仍能出方案，避免空池）
    r = analyzer.analyze()
    plates = analyzer.load_date(kind="plate", exclude_suspicious=True)
    plates += analyzer.load_date(kind="concept", exclude_suspicious=True)
    info = analyzer.snapshot_info(r.get("date") or None)
    # C 方案：全板块字典只用于解析/展示，评分用板块内分位
    sector_ctx: dict[str, float] = {
        t.name: t.score for t in r.get("all_tracks", r["monthly_tracks"])}
    gaps: list[str] = []
    if info.get("source") and info["source"] != "eastmoney":
        gaps.append(f"板块快照数据源={info['source']}（非东财），行业/资金因子按缺失处理")
    if not info.get("complete"):
        gaps.append("板块快照不完整：" + (info.get("note") or "概念板块未拉全"))

    tracks: list[dict] = []
    seen_tracks: set[str] = set()
    for t in list(r["monthly_tracks"]) + list(r["quarterly_tracks"]):
        if t.name in seen_tracks:
            continue
        seen_tracks.add(t.name)
        tracks.append({"name": t.name, "score": round(t.score, 1),
                       "zdf20": round(t.zdf20, 1), "zdf60": round(t.zdf60, 1),
                       "constituents": 0, "scanned": 0})

    # 已在池（active + 历史软删）不重复提议，避免"换血换来换去还是旧池"
    in_pool = {i.symbol for i in watch.all()} | {
        i.symbol for i in watch.all(status="removed")}

    scanned: list[CandidateRow] = []
    considered: set[str] = set()       # 去重后的候选池（含 reserve）
    data_sources: dict[str, int] = {}
    board_dist: dict[str, int] = {}
    failed_boards: list[dict] = []
    sort_sources: dict[str, int] = {}
    dedup_removed = 0

    def evaluate(sym: str, code: str, name: str, board: str) -> CandidateRow:
        """单只候选评估（硬门槛与五因子均沿用原规则）"""
        try:
            bars = bars_provider(sym, start, end) or []
        except Exception as exc:
            gaps.append(f"{name}({sym}) K线拉取失败: {exc}")
            bars = []
        source_tag = f"{getattr(bars, 'source', 'injected')}/{getattr(bars, 'adjust', 'qfq')}"
        data_sources[source_tag] = data_sources.get(source_tag, 0) + 1
        if getattr(bars, "adjust", "qfq") != "qfq" and len(bars) > 0:
            gaps.append(f"{name}({sym}) 行情源 {source_tag} 非前复权：技术因子按缺失处理")
        score_bars = bars if getattr(bars, "adjust", "qfq") == "qfq" else []
        if throttle:
            time.sleep(throttle)      # 单轮节流：防行情源限流（腾讯 501）
        if len(bars) < 10:
            return CandidateRow(sym, code, name, board, f"门槛未过：K线{len(bars)}根<10")
        close = bars[-1].close

        try:
            earnings = earnings_provider(code)
        except Exception as exc:
            gaps.append(f"{name}({code}) 财报拉取失败: {exc}")
            earnings = None
        growth, evidence = growth_from_earnings(earnings, code)
        if growth is None:
            return CandidateRow(sym, code, name, board,
                                "门槛未过：无业绩证据（东财业绩报表缺失/同比为空）",
                                close=close)

        try:
            valuation = valuation_provider(sym) or {}
        except Exception:
            valuation = {}
        result = score_candidate(
            sym, name, board, growth=growth, growth_evidence=evidence,
            bars=score_bars, sector_ctx=sector_ctx, plates=plates,
            valuation=valuation,
            snapshot_source=info.get("source") or "",
            snapshot_complete=bool(info.get("complete")),
        )
        if result.incomplete:
            status = "门槛未过：缺" + "、".join(result.missing)
        elif result.weighted < POOL_ENTRY_THRESHOLD:
            status = f"评分不足：{result.weighted:.1f}<{POOL_ENTRY_THRESHOLD:.0f}"
        else:
            status = "候选（≥80）"
        return CandidateRow(sym, code, name, board, status,
                            close=close, result=result,
                            growth=growth, evidence=evidence)

    def add(sym: str, code: str, name: str, board: str) -> None:
        row = evaluate(sym, code, name, board)
        scanned.append(row)
        board_dist[board] = board_dist.get(board, 0) + 1

    def acceptable(name: str) -> bool:
        return not any(marker in name for marker in BAD_NAME_MARKERS)

    if scope == "tracks":
        for track in tracks:
            board = track["name"]
            members = constituents_provider(board)
            track["constituents"] = len(members)
            if not members:
                gaps.append(f"「{board}」成分股接口无数据（板块名称未被东财核心题材收录）")
                continue
            taken = 0
            for m in members:
                if len(scanned) >= max_scan or taken >= max_per_track:
                    break
                code, name = m["code"], m["name"]
                if not acceptable(name):
                    continue
                sym = _symbol_prefix(code) + code
                if sym in in_pool or sym in considered:
                    continue
                considered.add(sym)
                taken += 1
                track["scanned"] += 1
                add(sym, code, name, board)
            if len(scanned) >= max_scan:
                gaps.append(f"已达单轮评估上限 {max_scan} 只，后续赛道未展开")
                break
        planned = []
        boards_total = 0
        boards_scanned = len([t for t in tracks if t["scanned"] > 0])
    else:
        boards = eligible_boards(analyzer)
        planned = board_plan(boards)
        boards_total = len(boards)
        boards_scanned = 0
        reserve: list[tuple[str, dict]] = []
        for b in planned:
            if len(scanned) >= max_scan:
                break
            limit = b["quota"] + 3
            try:
                if stocks_provider is not None:
                    members, src = stocks_provider(b["code"], b["name"], limit)
                else:
                    members, src = fetch_board_stocks(b["code"], b["name"], limit)
            except Exception as exc:
                failed_boards.append({"board": b["name"], "reason": str(exc)[:80]})
                continue
            sort_sources[src] = sort_sources.get(src, 0) + 1
            if not members:
                failed_boards.append({"board": b["name"], "reason": "成分股 0 只"})
                continue
            boards_scanned += 1
            taken = 0
            for m in members:
                code, name = str(m.get("code") or ""), str(m.get("name") or "")
                if not (len(code) == 6 and code.isdigit()) or not acceptable(name):
                    continue
                sym = _symbol_prefix(code) + code
                if sym in in_pool:
                    continue
                if sym in considered:
                    dedup_removed += 1
                    continue
                considered.add(sym)
                if taken < b["quota"] and len(scanned) < max_scan:
                    taken += 1
                    add(sym, code, name, b["name"])
                else:
                    reserve.append((b["name"], m))
        # 去重后不足 60 → 从 reserve 补足（保持 60–100 的样本区间）
        target_min = min(MIN_SAMPLE_PER_RUN, max_scan)
        for board, m in reserve:
            if len(scanned) >= target_min:
                break
            add(_symbol_prefix(m["code"]) + m["code"], m["code"], m["name"], board)

    pool_candidates = [c for c in scanned if c.status.startswith("候选")]
    pool_candidates.sort(key=lambda c: -(c.result.weighted if c.result else 0))
    # 交易权限硬约束：只能买主板；创业板/科创板/北交所只进"仅分析不可买"清单
    buyable_candidates, analysis_only = [], []
    for c in pool_candidates:
        ok, reason = is_buyable_code(c.code, c.symbol)
        if ok:
            buyable_candidates.append(c)
        else:
            analysis_only.append({
                "symbol": c.symbol, "code": c.code, "name": c.name,
                "board": c.board, "score": c.weighted, "reason": reason,
            })
    new = buyable_candidates[:max_new]
    stats = {
        "scanned": len(scanned),
        "candidates": len(pool_candidates),
        "new": len(new),
        "ge85": sum(1 for c in scanned if c.weighted is not None and c.weighted >= 85),
        "ge80": sum(1 for c in scanned if c.weighted is not None and c.weighted >= 80),
        "range70_80": sum(1 for c in scanned if c.weighted is not None and 70 <= c.weighted < 80),
        "lt70": sum(1 for c in scanned if c.weighted is not None and c.weighted < 70),
        "bar_gate": sum(1 for c in scanned if "K线" in c.status),
        "evidence_gate": sum(1 for c in scanned if "无业绩证据" in c.status),
        "factor_gate": sum(1 for c in scanned if c.status.startswith("门槛未过：缺")),
    }
    return {"today": today, "scope": scope, "tracks": tracks, "scanned": scanned,
            "new": new, "gaps": gaps, "stats": stats,
            "analysis_only": analysis_only,
            "data_sources": data_sources,
            "boards_total": boards_total,
            "boards_scanned": boards_scanned,
            "boards_failed": failed_boards,
            "scan_boards": planned,
            "sort_sources": sort_sources,
            "board_distribution": sorted(board_dist.items(), key=lambda x: -x[1]),
            "dedup_removed": dedup_removed,
            "unique_symbols": len(considered),
            "snapshot": {"date": r.get("date"), "source": info.get("source"),
                         "complete": info.get("complete"),
                         "plate_count": info.get("plate_count"),
                         "concept_count": info.get("concept_count")}}


def render_markdown(result: dict) -> str:
    """换血清单 Markdown（固定标注"待用户确认"，演练产物）"""
    today = result["today"]
    st = result["stats"]
    is_all = result.get("scope") == "all"
    snap = result.get("snapshot") or {}
    lines = [
        f"# 观察池换血（{today}）",
        "",
        "> **状态：待用户确认（演练产物，未写入生产 watchlist）**",
    ]
    if is_all:
        lines += [
            "> 扫描范围：全行业板块分层配额（热 10×3 / 中 20×2 / 广 25×1 / 冷 5×1），"
            "剔除可疑板块与聚合板块，不再只看 TOP7。",
        ]
    else:
        lines += [
            "> 依据：主赛道 TOP 板块成分 + 硬数据门槛。",
        ]
    lines += [
        f"> 选入门槛：近 20 交易日 K 线 ≥10 根 / 有业绩数据（东财业绩报表归母同比）/ 板块可映射。",
        f"> 业绩口径：东财归母净利同比 → 固定桶表（与收盘闭环同一把尺子，2026-11-10 起）。",
        f"> 评分分层：五因子 ≥{POOL_ENTRY_THRESHOLD:.0f} 入池候补；≥85 才推荐并登记 KPI（门槛不动）。"
        f"单轮新增 ≤{MAX_NEW_PER_RUN} 只；池容量上限 {POOL_CAP} 只；"
        f"近{POOL_HISTORY_DAYS}个评分日 ≥{POOL_EXIT_MIN_DAYS} 次且均分 <{POOL_EXIT_MIN_AVG:.0f} 自动移出。",
        "",
    ]
    if is_all:
        lines += [
            "## 一、扫描覆盖",
            "",
            f"- 快照：{snap.get('date')} · source={snap.get('source')} · "
            f"plate={snap.get('plate_count')} / concept={snap.get('concept_count')} · "
            f"完整={'是' if snap.get('complete') else '否'}",
            f"- 全行业板块（plate）{result.get('boards_total', 0)} 个（已剔除可疑板块与聚合板块）；"
            f"本次配额扫描 {len(result.get('scan_boards') or [])} 个，"
            f"成功 {result.get('boards_scanned', 0)} 个 / 失败 {len(result.get('boards_failed') or [])} 个",
            f"- 成分排序源：{result.get('sort_sources') or {}}",
            f"- 选取样本 {st['scanned']} 只；覆盖到的去重候选 {result.get('unique_symbols', 0)} 只"
            f"（去重剔除 {result.get('dedup_removed', 0)} 只，其余为补位备用未评估）",
            "",
            "样本分布前 10（板块：只数）：",
            "",
            "| 板块 | 只数 |",
            "|------|:---:|",
        ]
        for name, cnt in (result.get("board_distribution") or [])[:10]:
            lines.append(f"| {name} | {cnt} |")
        failed = result.get("boards_failed") or []
        if failed:
            lines += ["", "失败板块："]
            lines += [f"- {f['board']}：{f['reason']}" for f in failed[:20]]
        lines += [
            "",
            "参考：当期主赛道（仅展示，不参与评分）",
            "",
            "| 主赛道 | 赛道分 | 20日涨幅 | 60日涨幅 |",
            "|------|:---:|:---:|:---:|",
        ]
        for t in result["tracks"]:
            lines.append(f"| {t['name']} | {t['score']:.1f} | {t['zdf20']:+.1f}% | "
                         f"{t['zdf60']:+.1f}% |")
    else:
        lines += [
            "## 一、主赛道与成分来源",
            "",
            "| 主赛道 | 赛道分 | 20日涨幅 | 60日涨幅 | 成分股数 | 本轮评估 |",
            "|------|:---:|:---:|:---:|:---:|:---:|",
        ]
        for t in result["tracks"]:
            lines.append(f"| {t['name']} | {t['score']:.1f} | {t['zdf20']:+.1f}% | "
                         f"{t['zdf60']:+.1f}% | {t['constituents']} | {t['scanned']} |")
    src = result.get("data_sources") or {}
    if src:
        txt = "、".join(f"{k} {v} 只" for k, v in sorted(src.items()))
        lines += ["", f"行情源口径：{txt}（技术因子只接受 tencent/qfq；"
                  "新浪 raw 仅用于收盘价展示，技术因子按缺失处理）。"]
    if result["gaps"]:
        lines += ["", "来源缺口："]
        lines += [f"- {g}" for g in result["gaps"]]

    lines += ["", "## 二、拟新增候选（待确认后写入观察池）", ""]
    if result["new"]:
        lines += ["| 标的 | 代码 | 来源赛道 | 总分 | 行业 | 业绩 | 估值 | 技术 | 资金 | 现价 | 业绩证据 |",
                  "|------|------|------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|------|"]
        for c in result["new"]:
            f = c.result.factors
            lines.append(
                f"| {c.name} | {c.code} | {c.board} | {c.result.weighted:.1f} | "
                f"{f.industry:.0f} | {f.growth:.0f} | {f.valuation:.0f} | "
                f"{f.technical:.0f} | {f.capital:.0f} | "
                f"{c.close if c.close is not None else '—'} | {c.evidence or '—'} |")
    else:
        lines.append("（本轮无 ≥80 分候选，观察池维持现状）")

    new_syms = {c.symbol for c in result["new"]}
    analysis_syms = {c["symbol"] for c in result.get("analysis_only", [])}
    others = [c for c in result["scanned"]
              if c.symbol not in new_syms and c.symbol not in analysis_syms]
    lines += ["", "## 三、仅分析不可买（板块权限限制）", ""]
    if result.get("analysis_only"):
        lines += ["| 标的 | 代码 | 板块 | 总分 | 说明 |",
                  "|------|------|------|:---:|------|"]
        for c in result["analysis_only"]:
            score = f"{c['score']:.1f}" if c.get("score") is not None else "—"
            lines.append(f"| {c['name']} | {c['code']} | {c['board']} | {score} | {c['reason']} |")
    else:
        lines.append("（无）")
    lines += ["", "## 四、未入选明细（含 ≥80 但受单轮新增上限限制）", ""]
    if others:
        lines += ["| 标的 | 代码 | 来源赛道 | 总分 | 行业 | 业绩 | 估值 | 技术 | 资金 | 状态/缺口 |",
                  "|------|------|------|:---:|:---:|:---:|:---:|:---:|:---:|------|"]
        for c in sorted(others, key=lambda x: -(x.weighted or 0)):
            if c.result is None:
                lines.append(f"| {c.name} | {c.code} | {c.board} | — | — | — | — | — | — | {c.status} |")
                continue
            f = c.result.factors
            miss = c.result.missing
            cell = lambda v, label: "—" if label in miss else f"{v:.0f}"  # noqa: E731
            lines.append(
                f"| {c.name} | {c.code} | {c.board} | {c.result.weighted:.1f} | "
                f"{cell(f.industry, '行业景气')} | {cell(f.growth, '业绩证据')} | "
                f"{cell(f.valuation, '估值')} | {cell(f.technical, '技术面')} | "
                f"{cell(f.capital, '资金面')} | {c.status} |")
    else:
        lines.append("（无）")

    lines += [
        "",
        "## 四、统计与口径",
        "",
        f"- 评估 {st['scanned']} 只；≥85 分 {st['ge85']} 只；80-85 分 {st['ge80'] - st['ge85']} 只；"
        f"70-80 分 {st['range70_80']} 只；<70 分 {st['lt70']} 只；",
        f"- 门槛拦截：K线不足 {st['bar_gate']} / 无业绩证据 {st['evidence_gate']} / 其它因子缺失 {st['factor_gate']}；",
        f"- 本轮建议新增 {st['new']} 只；**淘汰规则**：近{POOL_HISTORY_DAYS}个评分日 ≥{POOL_EXIT_MIN_DAYS} 次"
        f"且均分 <{POOL_EXIT_MIN_AVG:.0f} → 软删（持仓标的豁免），池容量上限 {POOL_CAP} 只。",
        f"- 交易权限：只能买主板；仅分析不可买 {len(result.get('analysis_only', []))} 只（创业板/科创板/北交所）。",
        "",
        "## 五、待用户确认",
        "",
        f"- 确认后执行：`python -m src.pool_refresh --apply`（最多写入 {MAX_NEW_PER_RUN} 只，source=auto，"
        "保留评分与来源赛道备注，可 `--restore`/软删回滚）；",
        "- 未确认前不改变生产观察池；持仓/KPI 口径不受影响。",
        "",
        "---",
        f"*生成于 {today}（#59 演练版）。不构成投资建议。*",
    ]
    return "\n".join(lines) + "\n"


def write_report(result: dict, out_dir: Path | None = None,
                 path: str | None = None) -> Path:
    """写入换血清单（同日覆盖，幂等）；path 可指定完整输出路径"""
    if path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
    else:
        out_dir = out_dir or (Path("outputs") / "选股")
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / f"观察池换血_{result['today']}.md"
    p.write_text(render_markdown(result), encoding="utf-8")
    return p


def apply_new(result: dict, watch_store: WatchlistStore | None = None) -> list[str]:
    """把已确认的候选写入观察池（≤MAX_NEW_PER_RUN 只，source=auto；不选股、不登记 KPI）"""
    watch = watch_store or WatchlistStore()
    added: list[str] = []
    for c in result["new"]:
        item = watch.add(
            c.symbol, c.code, c.name, sector=c.board,
            strategy_tag="主赛道候选（#59）", source="auto",
            note=(f"{result['today']} #59 换血：{c.board} 五因子 "
                  f"{c.result.weighted:.1f} 分（行业{c.result.factors.industry:.0f}/"
                  f"业绩{c.result.factors.growth:.0f}/估值{c.result.factors.valuation:.0f}/"
                  f"技术{c.result.factors.technical:.0f}/资金{c.result.factors.capital:.0f}）"),
        )
        if item is None:
            log.warning("换血写入被板块权限拒绝：%s(%s)", c.name, c.code)
            continue
        added.append(c.symbol)
    return added


def score_history(market_db: str = MARKET_DB_PATH,
                  days: int = POOL_HISTORY_DAYS) -> dict[str, tuple[float, int]]:
    """近 N 个评分日的因子快照均分 → {symbol: (avg_score, 评分次数)}

    淘汰判据的唯一数据源（factor_snapshot 落档分，可复算）。
    """
    from src.db import get_connection

    with get_connection(market_db, readonly=True) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name='factor_snapshot'").fetchone():
            return {}
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM factor_snapshot ORDER BY date DESC LIMIT ?",
            (days,))]
        if not dates:
            return {}
        marks = ",".join("?" * len(dates))
        rows = conn.execute(
            f"SELECT symbol, AVG(score), COUNT(*) FROM factor_snapshot "
            f"WHERE date IN ({marks}) AND score IS NOT NULL GROUP BY symbol",
            dates).fetchall()
    return {r[0]: (float(r[1]), int(r[2])) for r in rows}


def rebalance(result: dict, watch_store: WatchlistStore | None = None,
              market_db: str = MARKET_DB_PATH,
              protected: set[str] | None = None) -> dict:
    """把 plan() 结果落到观察池：低分淘汰 → 容量裁剪 → 写入新候选

    规则：
      1. 淘汰：近 20 个评分日中至少 10 次评分、平均分 <70 → 软删（watch.remove，留审计）；
         持仓标的不淘汰（protected），避免把在持仓的观察项删掉。
      2. 容量：active 上限 80；已超限时按历史均分升序移出（无评分历史的排最后、不动）。
      3. 入池：plan() 已按分数降序取前 MAX_NEW_PER_RUN=15，再按剩余容量裁剪。

    Returns:
        {"removed": [{symbol,name,reason}], "added": [symbol], "room": int}
    """
    watch = watch_store or WatchlistStore(market_db)
    protected = protected or set()
    hist = score_history(market_db)
    removed: list[dict] = []

    # 1) 长期低分淘汰
    for item in watch.all():
        if item.symbol in protected:
            continue
        avg, n = hist.get(item.symbol, (None, 0))
        if avg is None or n < POOL_EXIT_MIN_DAYS or avg >= POOL_EXIT_MIN_AVG:
            continue
        reason = (f"池内长期低分：近{POOL_HISTORY_DAYS}个评分日 {n} 次均分 "
                  f"{avg:.1f}<{POOL_EXIT_MIN_AVG:.0f}")
        if watch.remove(item.symbol, reason):
            removed.append({"symbol": item.symbol, "name": item.name, "reason": reason})

    # 2) 容量裁剪（超上限时按历史均分升序移出）
    active = watch.all()
    overflow = len(active) - POOL_CAP
    if overflow > 0:
        ranked = sorted((i for i in active if i.symbol in hist and i.symbol not in protected),
                        key=lambda i: hist[i.symbol][0])
        for item in ranked[:overflow]:
            reason = f"池容量超限（>{POOL_CAP}）按评分移出：均分 {hist[item.symbol][0]:.1f}"
            if watch.remove(item.symbol, reason):
                removed.append({"symbol": item.symbol, "name": item.name, "reason": reason})
        active = watch.all()

    # 3) 写入新候选（单轮上限 + 剩余容量双重裁剪）
    room = max(0, POOL_CAP - len(active))
    limited = {**result, "new": list(result.get("new") or [])[:min(MAX_NEW_PER_RUN, room)]}
    added = apply_new(limited, watch)
    return {"removed": removed, "added": added, "room": room}


def main() -> None:
    parser = argparse.ArgumentParser(description="观察池换血（#59 最小可执行版，默认演练）")
    parser.add_argument("--apply", action="store_true",
                        help=f"确认后写入生产 watchlist（最多 {MAX_NEW_PER_RUN} 只）")
    parser.add_argument("--dry-run", action="store_true",
                        help="演练（默认行为，显式指定亦可）")
    parser.add_argument("--date", default=None, help="基准日期 YYYY-MM-DD（默认今天）")
    parser.add_argument("--scope", choices=("all", "tracks"), default="all",
                        help="候选发现口径：all=全行业板块分层配额（默认）/ tracks=旧 TOP7")
    parser.add_argument("--out", default=None,
                        help="输出路径（默认 outputs/选股/观察池换血_YYYY-MM-DD.md）")
    args = parser.parse_args()

    result = plan(args.date, scope=args.scope)
    path = write_report(result, path=args.out)
    print(render_markdown(result))
    print(f"📄 已写入 {path}")
    if args.apply:
        if not result["new"]:
            print("ℹ️ 无候选可写入")
            return
        added = apply_new(result)
        print(f"✅ 已写入观察池 {len(added)} 只：{', '.join(added)}")
    else:
        print("（演练模式）未写入生产观察池；确认后加 --apply 执行")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
