"""扩样本回测引擎（只读 research.db，不碰生产库、不改任何生产口径）

回答四个问题：
    1. VCP 信号在「全部沪深主板 × 近两年」里成立多少次、后续 5/10/20 日表现如何、
       是否显著优于同池全部交易日；
    2. 技术分（现口径 technical_factor，权重/桶表不动）分组后，后续收益是否单调、是否真有区分度；
    3. 板块 20 日涨幅分位 50%~85% 这个筛选条件，两年里筛掉了多少机会、被筛掉的那部分表现如何；
    4. 随机基准：同池随机持有 1/3/5 只的收益分布。

反前视口径（写死在代码里，自查见 self_check 段）：
    - 信号判定只用「当日及之前」的 K 线（第 d 根收盘为止）；
    - 买入价 = 第 d+1 根的开盘价（模拟次日竞价成交），前向收益 = close(d+h)/open(d+1)-1；
    - 第 d+1 根不存在 / 开盘一字涨停（open(d+1) ≥ close(d)×1.095）/ 前向不足 h 根 → 该观察点剔除；
    - 相邻交易日信号高度相关，VCP 结论同时给「全部信号」与「独立事件（同一只票连续信号归并为一段）」。

用法:
    python -m src.research_backtest --out outputs/研究/backtest_2026-11-10.json
    python -m src.research_backtest --limit 200        # 小样本自测
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import random
import statistics
import sys
import time
from array import array
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import research_store as rs                     # noqa: E402
from src.factors import technical_factor                 # noqa: E402
from src.vcp import detect_vcp                           # noqa: E402

log = logging.getLogger("research_backtest")
log.setLevel(logging.INFO)
log.propagate = False                                    # 不写生产 app.log
_h = logging.StreamHandler(sys.stdout)
_h.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
log.addHandler(_h)

HORIZONS = (5, 10, 20)
MIN_HISTORY = 60            # 与 VCP lookback 一致；不足 60 根不计观察点
START_DATE = "2024-03-19"   # 研究窗口起点（腾讯 640 根上限的最早一天）；更早的历史只用来做前置窗口，不产生观察点
VCP_WINDOW_BARS = 80        # 传给 detect_vcp 的 K 线根数（>60，保持"箱体触窗判缺失"语义）
PRICE_MIN, PRICE_MAX = 5.0, 60.0   # 生产可执行价格区间（executable.MAX_PRICE=60）
LIMIT_UP_OPEN = 1.095       # 开盘价 ≥ 前收 ×1.095 视为一字涨停买不到
EPISODE_GAP = 10            # 同一只票两次信号间隔 ≤10 个交易日 → 同一段（独立事件归并）

TECH_BUCKETS = [
    ("<55", 0, 55), ("55-65", 55, 65), ("65-75", 65, 75),
    ("75-85", 75, 85), ("≥85", 85, 101),
]
TECH_HIGH, TECH_LOW = 85.0, 65.0   # 日频横截面价差：≥85（推荐门槛） vs <65（低分区）
SECTOR_BUCKETS = [("<50%（偏弱）", -1.0, 50.0),
                  ("50~85%（生产放行）", 50.0, 85.0),
                  (">85%（最热头部）", 85.0, 101.0)]


# ============================================================
# 轻量 K 线（duck-typed，字段与 src.data_source.KlineBar 一致）
# ============================================================

@dataclass(frozen=True, slots=True)
class Bar:
    date: str
    open: float
    close: float
    high: float
    low: float
    volume: float | None = None


def load_bars(conn, symbol: str) -> list[Bar]:
    rows = conn.execute(
        "SELECT date, open, close, high, low, volume FROM research_bars "
        "WHERE symbol=? ORDER BY date", (symbol,)).fetchall()
    return [Bar(r[0], r[1] or 0.0, r[2] or 0.0, r[3] or 0.0, r[4] or 0.0, r[5])
            for r in rows if r[2]]


# ============================================================
# 板块分位（东财板块指数 20 日涨幅的当日横截面分位，复刻生产 industry_percentile_factor 口径）
# ============================================================

def build_sector_percentile(conn) -> dict[str, dict[str, float]]:
    """返回 {date: {板块名: 分位 0-100}}

    口径对齐生产：分位池 = 当日全部板块（行业+概念）中 zdf20 ≠ 0 者；
    zdf20 = close/close[-21]-1（与 sector_feed.build_snapshots 一致）。
    """
    board_names = {r[0]: r[1] for r in conn.execute("SELECT code, name FROM research_boards")}
    series: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for code, date, close in conn.execute(
            "SELECT code, date, close FROM research_board_bars ORDER BY code, date"):
        series[code].append((date, close))

    zdf_by_date: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for code, pts in series.items():
        name = board_names.get(code)
        if not name or len(pts) < 21:
            continue
        for i in range(20, len(pts)):
            prev = pts[i - 20][1]
            if not prev:
                continue
            zdf = (pts[i][1] / prev - 1) * 100
            zdf_by_date[pts[i][0]].append((name, zdf))

    out: dict[str, dict[str, float]] = {}
    for date, pairs in zdf_by_date.items():
        pairs = [(n, z) for n, z in pairs if z != 0]
        if len(pairs) < 30:          # 与生产 MIN_BOARD_POOL 一致
            continue
        vals = sorted(z for _, z in pairs)
        n = len(vals)
        out[date] = {name: sum(1 for v in vals if v <= z) / n * 100 for name, z in pairs}
    return out


# ============================================================
# 统计工具（纯标准库）
# ============================================================

def _pct(xs: array, p: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    k = (len(s) - 1) * p
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    return s[lo] if lo == hi else s[lo] + (s[hi] - s[lo]) * (k - lo)


def describe(xs: array) -> dict:
    if not xs:
        return {"n": 0}
    n = len(xs)
    win = sum(1 for x in xs if x > 0)
    return {
        "n": n,
        "median_pct": round(_pct(xs, 0.5) * 100, 2),
        "mean_pct": round(statistics.fmean(xs) * 100, 2),
        "win_rate_pct": round(win / n * 100, 2),
        "p25_pct": round(_pct(xs, 0.25) * 100, 2),
        "p75_pct": round(_pct(xs, 0.75) * 100, 2),
    }


def two_prop_z(k1: int, n1: int, k2: int, n2: int) -> tuple[float, float]:
    """两比例 z 检验（双侧）：返回 (z, p)"""
    if not n1 or not n2:
        return 0.0, 1.0
    p1, p2 = k1 / n1, k2 / n2
    p = (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return 0.0, 1.0
    z = (p1 - p2) / se
    return z, 2 * (1 - _norm_cdf(abs(z)))


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def mannwhitney_z(x: array, y: array) -> tuple[float, float]:
    """Mann-Whitney U 正态近似（双侧 p），用于两组收益分布位置差异"""
    n1, n2 = len(x), len(y)
    if n1 < 10 or n2 < 10:
        return 0.0, 1.0
    combined = [(v, 0) for v in x] + [(v, 1) for v in y]
    combined.sort(key=lambda t: t[0])
    ranks = [0.0] * len(combined)
    i = 0
    while i < len(combined):
        j = i
        while j + 1 < len(combined) and combined[j + 1][0] == combined[i][0]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[k] = avg
        i = j + 1
    r1 = sum(ranks[k] for k, (_, g) in enumerate(combined) if g == 0)
    u1 = r1 - n1 * (n1 + 1) / 2
    mu = n1 * n2 / 2
    sd = math.sqrt(n1 * n2 * (n1 + n2 + 1) / 12)
    if sd == 0:
        return 0.0, 1.0
    z = (u1 - mu) / sd
    return z, 2 * (1 - _norm_cdf(abs(z)))


def bootstrap_median_diff_ci(clusters: list[list[float]], base: array,
                             iters: int = 2000, seed: int = 20261110) -> tuple[float, float]:
    """按「段（episode）」重采样，给出信号中位数 − 基准中位数的 95% 置信区间"""
    rng = random.Random(seed)
    if not clusters or not base:
        return float("nan"), float("nan")
    base_med = _pct(base, 0.5)
    diffs = []
    for _ in range(iters):
        pick = [clusters[rng.randrange(len(clusters))] for _ in range(len(clusters))]
        flat = [v for c in pick for v in c]
        if flat:
            diffs.append(_pct(array("d", flat), 0.5) - base_med)
    diffs.sort()
    return round(diffs[int(len(diffs) * 0.025)] * 100, 2), round(diffs[int(len(diffs) * 0.975)] * 100, 2)


def day_level_spread(series: dict[str, list[float]]) -> dict:
    """日频横截面价差序列（每日 高分组均值 − 低分组均值）的 t 检验

    20 日前向窗口在相邻日期上重叠 → 日频序列自相关，t 值会虚高；
    因此同时给「每 20 个交易日取 1 天」的非重叠子样本结果（更保守）。
    """
    xs = []
    for _, agg in sorted(series.items()):
        if agg[1] and agg[3]:
            xs.append(agg[0] / agg[1] - agg[2] / agg[3])

    def _t(vals: list[float]) -> dict:
        if len(vals) < 20:
            return {"days": len(vals)}
        m, sd = statistics.fmean(vals), statistics.pstdev(vals)
        se = sd / math.sqrt(len(vals))
        t = m / se if se else 0.0
        return {
            "days": len(vals),
            "mean_spread_pct": round(m * 100, 3),
            "t_stat": round(t, 2),
            "p_two_sided": round(2 * (1 - _norm_cdf(abs(t))), 4),
            "positive_day_pct": round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1),
        }

    return {"overlapping": _t(xs), "non_overlapping_every20d": _t(xs[::20])}


# ============================================================
# 主回测
# ============================================================

def run(limit: int = 0, workers_ignored: int = 0) -> dict:
    t0 = time.time()
    with rs.open_research(readonly=True) as conn:
        sym_rows = conn.execute(
            "SELECT symbol, name, industry, is_st FROM research_symbols "
            "WHERE is_main_board=1 AND bar_count>0 AND is_st=0 ORDER BY symbol").fetchall()
        if limit:
            sym_rows = sym_rows[:limit]
        log.info("载入板块分位（%d 只标的）...", len(sym_rows))
        sector_pct = build_sector_percentile(conn)
        log.info("板块分位覆盖 %d 个交易日", len(sector_pct))
        idx_series = [(r[0], r[1]) for r in conn.execute(
            "SELECT date, close FROM research_bars WHERE symbol='sh000300' ORDER BY date")
            if r[1]]

        base = {h: array("d") for h in HORIZONS}
        tech_groups = {label: {h: array("d") for h in HORIZONS} for label, _, _ in TECH_BUCKETS}
        tech_hi_lo = defaultdict(lambda: [0.0, 0, 0.0, 0])   # date -> [sum_hi,n_hi,sum_lo,n_lo]
        sector_groups = {label: {h: array("d") for h in HORIZONS} for label, _, _ in SECTOR_BUCKETS}
        sector_seen = array("d")
        per_date = {h: defaultdict(lambda: array("d")) for h in HORIZONS}
        sector_date_sum = {label: defaultdict(lambda: [0.0, 0]) for label, _, _ in SECTOR_BUCKETS}
        vcp_sig: list[dict] = []
        vcp_episodes: list[dict] = []
        counters = defaultdict(int)

        for si, row in enumerate(sym_rows, 1):
            symbol, name, industry, is_st = row[0], row[1], row[2] or "", row[3]
            bars = load_bars(conn, symbol)
            n = len(bars)
            if n < MIN_HISTORY + 2:
                counters["skip_short_history"] += 1
                continue
            last_sig_i = -10**9
            cur_ep: dict | None = None
            for i in range(MIN_HISTORY, n):
                d = bars[i].date
                if d < START_DATE:      # 窗口外的历史（含退市票早年被截断的 K 线）只做前置窗口，不产生观察点
                    counters["skip_before_window"] += 1
                    continue
                close = bars[i].close
                if not (PRICE_MIN <= close <= PRICE_MAX):
                    counters["skip_price"] += 1
                    continue
                if i + 1 >= n:
                    break
                entry = bars[i + 1].open
                if entry <= 0:
                    counters["skip_bad_open"] += 1
                    continue
                if entry >= close * LIMIT_UP_OPEN:
                    counters["skip_limit_up_open"] += 1
                    continue
                if i + max(HORIZONS) >= n:
                    break
                fwd = {h: bars[i + h].close / entry - 1 for h in HORIZONS
                       if bars[i + h].close > 0}
                if len(fwd) < len(HORIZONS):
                    counters["skip_forward_short"] += 1
                    continue

                counters["observations"] += 1
                for h in HORIZONS:
                    base[h].append(fwd[h])
                    per_date[h][d].append(fwd[h])

                window = bars[max(0, i + 1 - VCP_WINDOW_BARS): i + 1]
                tech = technical_factor(window)
                if tech is not None:
                    for label, lo, hi in TECH_BUCKETS:
                        if lo <= tech < hi:
                            for h in HORIZONS:
                                tech_groups[label][h].append(fwd[h])
                            break
                    if tech >= TECH_HIGH:
                        tech_hi_lo[d][0] += fwd[20]
                        tech_hi_lo[d][1] += 1
                    elif tech < TECH_LOW:
                        tech_hi_lo[d][2] += fwd[20]
                        tech_hi_lo[d][3] += 1

                pct = sector_pct.get(d, {}).get(industry)
                if pct is not None:
                    sector_seen.append(pct)
                    for label, lo, hi in SECTOR_BUCKETS:
                        if lo <= pct < hi:
                            for h in HORIZONS:
                                sector_groups[label][h].append(fwd[h])
                            agg = sector_date_sum[label][d]
                            agg[0] += fwd[20]
                            agg[1] += 1
                            break

                rec = detect_vcp(window)
                if rec.is_vcp:
                    counters["vcp_signals"] += 1
                    item = {
                        "symbol": symbol, "name": name, "date": d, "i": i,
                        "tech": tech, "pct": pct,
                        "ret5": fwd[5], "ret10": fwd[10], "ret20": fwd[20],
                    }
                    vcp_sig.append(item)
                    if cur_ep is None or i - last_sig_i > EPISODE_GAP:
                        cur_ep = {"symbol": symbol, "name": name, "start": d, "first": item,
                                  "count": 1}
                        vcp_episodes.append(cur_ep)
                    else:
                        cur_ep["count"] += 1
                    last_sig_i = i

            if si % 200 == 0 or si == len(sym_rows):
                log.info("进度 %d/%d 标的，观察点 %d，VCP 信号 %d，用时 %.1f 分钟",
                         si, len(sym_rows), counters["observations"],
                         counters["vcp_signals"], (time.time() - t0) / 60)

    # ---------------- 汇总 ----------------
    res: dict = {
        "meta": {
            "symbols": len(sym_rows),
            "sector_days": len(sector_pct),
            "sector_start": min(sector_pct) if sector_pct else "",
            "sector_end": max(sector_pct) if sector_pct else "",
            "counters": dict(counters),
            "runtime_min": round((time.time() - t0) / 60, 1),
            "price_filter": [PRICE_MIN, PRICE_MAX],
            "entry": "次日开盘价（open(d+1)），信号只用 d 及之前 K 线",
            "limit_up_open_excluded": LIMIT_UP_OPEN,
        },
        "baseline": {f"{h}d": describe(base[h]) for h in HORIZONS},
    }

    # VCP：全部信号
    vcp_all = {}
    for h in HORIZONS:
        arr = array("d", [s[f"ret{h}"] for s in vcp_sig])
        vcp_all[f"{h}d"] = describe(arr)
        k1 = sum(1 for x in arr if x > 0)
        k2 = sum(1 for x in base[h] if x > 0)
        z, p = two_prop_z(k1, len(arr), k2, len(base[h]))
        mz, mp = mannwhitney_z(arr, base[h])
        vcp_all[f"{h}d"].update({
            "excess_median_pct": round(vcp_all[f"{h}d"]["median_pct"]
                                       - describe(base[h])["median_pct"], 2),
            "win_z": round(z, 2), "win_p": round(p, 5),
            "dist_z": round(mz, 2), "dist_p": round(mp, 5),
        })
    res["vcp_all_signals"] = vcp_all

    # VCP：独立事件（每段只取第一个信号）
    ep_first = [e["first"] for e in vcp_episodes]
    vcp_ep = {"episodes": len(vcp_episodes),
              "episode_len_median": (statistics.median([e["count"] for e in vcp_episodes])
                                     if vcp_episodes else 0)}
    for h in HORIZONS:
        arr = array("d", [s[f"ret{h}"] for s in ep_first])
        d0 = describe(arr)
        k1 = sum(1 for x in arr if x > 0)
        z, p = two_prop_z(k1, len(arr), sum(1 for x in base[h] if x > 0), len(base[h]))
        ci_lo, ci_hi = bootstrap_median_diff_ci([[s[f"ret{h}"]] for s in ep_first], base[h])
        d0.update({"win_z": round(z, 2), "win_p": round(p, 5),
                   "excess_median_ci95": [ci_lo, ci_hi],
                   "excess_median_pct": round(d0["median_pct"] - describe(base[h])["median_pct"], 2)})
        vcp_ep[f"{h}d"] = d0
    res["vcp_episodes"] = vcp_ep
    res["vcp_episode_samples"] = [
        {k: (round(v, 4) if isinstance(v, float) else v)
         for k, v in e["first"].items() if k in ("symbol", "name", "date", "ret20")}
        for e in vcp_episodes[:20]
    ]
    year_count: dict[str, int] = defaultdict(int)
    for e in vcp_episodes:
        year_count[e["start"][:4]] += 1
    res["vcp_episodes_by_year"] = dict(sorted(year_count.items()))

    # VCP：同日横截面超额（每个信号先减掉当日全池平均，消除大盘同涨同跌）
    same_day = {}
    for h in HORIZONS:
        date_mean = {d: statistics.fmean(a) for d, a in per_date[h].items() if a}
        excess_by_date: dict[str, list[float]] = defaultdict(list)
        for s in ep_first:
            dm = date_mean.get(s["date"])
            if dm is not None:
                excess_by_date[s["date"]].append(s[f"ret{h}"] - dm)
        daily_excess = [statistics.fmean(v) for v in excess_by_date.values()]
        if len(daily_excess) >= 30:
            m = statistics.fmean(daily_excess)
            sd = statistics.pstdev(daily_excess)
            se = sd / math.sqrt(len(daily_excess))
            t = m / se if se else 0.0
            same_day[f"{h}d"] = {
                "episodes_used": sum(len(v) for v in excess_by_date.values()),
                "days": len(daily_excess),
                "mean_excess_pct": round(m * 100, 3),
                "median_daily_excess_pct": round(statistics.median(daily_excess) * 100, 3),
                "t_stat": round(t, 2),
                "p_two_sided": round(2 * (1 - _norm_cdf(abs(t))), 4),
                "positive_day_pct": round(sum(1 for v in daily_excess if v > 0)
                                          / len(daily_excess) * 100, 1),
            }
    res["vcp_same_day_excess"] = same_day

    # 沪深300 同期 20 日收益（基准参照，不是策略收益）
    idx_rets = [idx_series[i + 20][1] / idx_series[i][1] - 1
                for i in range(len(idx_series) - 20) if idx_series[i][0] >= START_DATE]
    if idx_rets:
        res["index_20d"] = {
            "days": len(idx_rets),
            "mean_pct": round(statistics.fmean(idx_rets) * 100, 2),
            "median_pct": round(statistics.median(idx_rets) * 100, 2),
            "win_rate_pct": round(sum(1 for x in idx_rets if x > 0) / len(idx_rets) * 100, 2),
        }

    # 技术分分组
    tech_tbl = {label: {f"{h}d": describe(tech_groups[label][h]) for h in HORIZONS}
                for label, _, _ in TECH_BUCKETS}
    hi = tech_groups["≥85"][20]
    lo = tech_groups["<55"][20]
    res["technical_buckets"] = tech_tbl
    res["technical_spread_20d"] = {
        "high_minus_low_median_pct": round(describe(hi)["median_pct"] - describe(lo)["median_pct"], 2)
        if hi and lo else None,
        "dist_z": round(mannwhitney_z(hi, lo)[0], 2) if hi and lo else None,
        "dist_p": round(mannwhitney_z(hi, lo)[1], 5) if hi and lo else None,
        "day_level": day_level_spread(tech_hi_lo),
    }

    # 板块分位
    res["sector"] = {
        "observed": len(sector_seen),
        "coverage_pct": round(len(sector_seen) / max(1, counters["observations"]) * 100, 1),
        "groups": {label: {f"{h}d": describe(sector_groups[label][h]) for h in HORIZONS}
                   for label, _, _ in SECTOR_BUCKETS},
        "share_pct": {label: round(len(sector_groups[label][20])
                                   / max(1, len(sector_seen)) * 100, 2)
                      for label, _, _ in SECTOR_BUCKETS},
    }
    acc = sector_groups["50~85%（生产放行）"][20]
    rej_hot = sector_groups[">85%（最热头部）"][20]
    rej_weak = sector_groups["<50%（偏弱）"][20]
    if acc and rej_hot and rej_weak:
        res["sector"]["accepted_vs_rejected_20d"] = {
            "accepted_median_pct": describe(acc)["median_pct"],
            "hot_median_pct": describe(rej_hot)["median_pct"],
            "weak_median_pct": describe(rej_weak)["median_pct"],
            "accepted_minus_hot_z": round(mannwhitney_z(acc, rej_hot)[0], 2),
            "accepted_minus_hot_p": round(mannwhitney_z(acc, rej_hot)[1], 5),
            "accepted_minus_weak_z": round(mannwhitney_z(acc, rej_weak)[0], 2),
            "accepted_minus_weak_p": round(mannwhitney_z(acc, rej_weak)[1], 5),
        }

    # 板块分位：同日横截面价差（放行组 − 偏弱组 / 放行组 − 最热组），按日做 t 检验
    sector_daily = {}
    acc_label = "50~85%（生产放行）"
    for cmp_label in ("<50%（偏弱）", ">85%（最热头部）"):
        diffs = []
        for d, a in sector_date_sum[acc_label].items():
            b = sector_date_sum[cmp_label].get(d)
            if a[1] and b and b[1]:
                diffs.append(a[0] / a[1] - b[0] / b[1])
        if len(diffs) >= 30:
            m = statistics.fmean(diffs)
            sd = statistics.pstdev(diffs)
            se = sd / math.sqrt(len(diffs))
            t = m / se if se else 0.0
            sector_daily[f"accepted_minus_{cmp_label}"] = {
                "days": len(diffs),
                "mean_diff_pct": round(m * 100, 3),
                "t_stat": round(t, 2),
                "p_two_sided": round(2 * (1 - _norm_cdf(abs(t))), 4),
                "positive_day_pct": round(sum(1 for v in diffs if v > 0) / len(diffs) * 100, 1),
            }
    res["sector"]["same_day_tests"] = sector_daily

    # VCP ∩ 板块放行
    both = array("d", [s["ret20"] for s in vcp_sig
                       if s["pct"] is not None and 50 <= s["pct"] <= 85])
    res["vcp_with_sector_pass_20d"] = {
        "n": len(both), **{k: v for k, v in describe(both).items() if k != "n"},
        "vs_baseline_median_pct": describe(base[20])["median_pct"],
    }

    # 随机基准：同池随机持有 1/3/5 只（20 日）
    rng = random.Random(20261110)
    dates = [d for d, arr in per_date[20].items() if len(arr) >= 5]
    rand_out = {}
    for k in (1, 3, 5):
        ports = array("d")
        for _ in range(5000):
            arr = per_date[20][dates[rng.randrange(len(dates))]]
            picks = [arr[rng.randrange(len(arr))] for _ in range(k)]
            ports.append(sum(picks) / k)
        rand_out[f"random_{k}_stocks_20d"] = {
            **describe(ports),
            "p05_pct": round(_pct(ports, 0.05) * 100, 2),
            "p95_pct": round(_pct(ports, 0.95) * 100, 2),
        }
    res["random_baseline_20d"] = rand_out
    res["pool_20d"] = describe(base[20])

    # 独立事件 & 显著性结论
    ep20 = vcp_ep.get("20d", {})
    res["verdict"] = {
        "vcp_signals_raw": counters["vcp_signals"],
        "vcp_episodes": len(vcp_episodes),
        "baseline_obs": len(base[20]),
        "vcp_episode_win_p_20d": ep20.get("win_p"),
        "vcp_episode_excess_median_ci95_20d": ep20.get("excess_median_ci95"),
        "vcp_same_day_excess_20d_t": same_day.get("20d", {}).get("t_stat"),
        "vcp_same_day_excess_20d_p": same_day.get("20d", {}).get("p_two_sided"),
    }
    res["runtime_min"] = round((time.time() - t0) / 60, 1)
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    res = run(limit=args.limit)
    text = json.dumps(res, ensure_ascii=False, indent=2, default=str)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        log.info("结果已写入 %s", out)
    print(text)


if __name__ == "__main__":
    main()
