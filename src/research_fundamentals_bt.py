"""三项基本面因子（估值 20% / 业绩 25% / 行业景气 25%）扩样本回测

只读 data/research.db，不改生产代码 / 权重 / 桶表 / 门槛，不写 data/market.db。

时点还原（2026-11-11 用户更正后的口径）：
    - 估值：只取 TRADE_DATE == d 的当日 PE_TTM / PB_MRQ（东财逐日值）；
    - 业绩：报告期的可得日 = **NOTICE_DATE（首次公告日）**，d 日只能用 NOTICE_DATE<=d
      的最新一期；UPDATE_DATE 是"最后修改日"（更正会改到未来），仅做对照，不作时点；
    - 行业景气：d 日板块 20 日涨幅在其所属行业板块（kind='plate'）当日横截面中的分位。

买入与前向收益沿用技术面回测口径：
    买入价 = open(d+1)；前向 = close(d+h)/open(d+1)-1，h=5/10/20；
    剔除 d+1 一字涨停（open>=close(d)*1.095）、前向不足、价格不在 5~60 元；
    同日超额 = 个股收益 − 当日全池（同口径可交易样本）均值，消除大盘同涨同跌；
    20 日窗口在相邻交易日重叠 → 同时给「每 20 个交易日取 1 天」的非重叠结果。

用法:
    python -m src.research_fundamentals_bt --limit 200      # 小样本自测
    python -m src.research_fundamentals_bt
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from array import array
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import research_store as rs                                    # noqa: E402
from src.factors import (growth_score_from_rate, valuation_factor,     # noqa: E402
                         _resolve_board)
from src.research_backtest import (Bar, describe, load_bars, _norm_cdf,  # noqa: E402
                                   mannwhitney_z, two_prop_z)

HORIZONS = (5, 10, 20)
START_DATE = "2024-03-19"
PRICE_MIN, PRICE_MAX = 5.0, 60.0
LIMIT_UP_OPEN = 1.095
MIN_BOARD_POOL = 30              # 与生产 MIN_BOARD_POOL 一致
W_IND, W_GROW, W_VAL = 25 / 70, 25 / 70, 20 / 70   # 三项按 25/25/20 归一

VAL_BANDS = [("<60", 0, 60), ("60-70", 60, 70), ("70-80", 70, 80),
             ("80-90", 80, 90), ("≥90", 90, 101)]
GROW_BANDS = [("<50", 0, 50), ("50-70", 50, 70), ("70-75", 70, 75),
              ("75-84", 75, 84), ("≥84", 84, 101)]
IND_BANDS = [("<50", -1, 50), ("50-65", 50, 65), ("65-85", 65, 85), ("≥85", 85, 101)]
COMP_BANDS = [("<60", 0, 60), ("60-70", 60, 70), ("70-75", 70, 75),
              ("75-80", 75, 80), ("≥80", 80, 101)]

FACTOR_BANDS = {
    "valuation": VAL_BANDS,
    "growth_notice": GROW_BANDS,
    "growth_update": GROW_BANDS,
    "growth_report": GROW_BANDS,
    "industry": IND_BANDS,
    "composite": COMP_BANDS,
}
HIGH_LOW = {
    "valuation": ("≥90", "<60"),
    "growth_notice": ("≥84", "<50"),
    "growth_update": ("≥84", "<50"),
    "growth_report": ("≥84", "<50"),
    "industry": ("65-85", "<50"),
    "composite": ("≥80", "<60"),
}


def band_of(bands, v: float) -> str | None:
    for label, lo, hi in bands:
        if lo <= v < hi:
            return label
    return None


def _t(vals: list[float]) -> dict:
    """均值 t 检验（正态近似）"""
    n = len(vals)
    if n < 20:
        return {"days": n}
    m, sd = statistics.fmean(vals), statistics.pstdev(vals)
    se = sd / math.sqrt(n) if n else 0.0
    t = m / se if se else 0.0
    return {
        "days": n,
        "mean_pct": round(m * 100, 3),
        "median_day_pct": round(statistics.median(vals) * 100, 3),
        "t_stat": round(t, 2),
        "p_two_sided": round(2 * (1 - _norm_cdf(abs(t))), 4),
        "positive_day_pct": round(sum(1 for v in vals if v > 0) / n * 100, 1),
    }


# ============================================================
# 行业景气分位（复刻生产口径：仅 kind='plate'，20 日涨幅，当日横截面分位）
# ============================================================

def build_industry_percentile(conn) -> dict[str, dict[str, float]]:
    names = {r[0]: r[1] for r in conn.execute(
        "SELECT code, name FROM research_boards WHERE kind='plate'")}
    series: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for code, date, close in conn.execute(
            "SELECT code, date, close FROM research_board_bars ORDER BY code, date"):
        if code in names:
            series[code].append((date, close))

    by_date: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for code, pts in series.items():
        for i in range(20, len(pts)):
            prev = pts[i - 20][1]
            if prev:
                by_date[pts[i][0]].append((names[code], (pts[i][1] / prev - 1) * 100))

    out: dict[str, dict[str, float]] = {}
    for date, pairs in by_date.items():
        pairs = [(n, z) for n, z in pairs if z != 0]
        if len(pairs) < MIN_BOARD_POOL:
            continue
        vals = sorted(z for _, z in pairs)
        n = len(vals)
        out[date] = {name: round(sum(1 for v in vals if v <= z) / n * 100, 1)
                     for name, z in pairs}
    return out


def load_valuation(conn, symbol: str) -> dict[str, tuple]:
    return {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT date, pe_ttm, pb_mrq FROM research_valuation WHERE symbol=?",
        (symbol,))}


def load_earnings(conn, symbol: str) -> list[tuple]:
    return [tuple(r) for r in conn.execute(
        "SELECT report_date, notice_date, update_date, sjltz, parent_netprofit "
        "FROM research_earnings WHERE symbol=? ORDER BY report_date", (symbol,))]


# ============================================================
# 估值时点自查：PE 大幅跳变日是否落在 NOTICE_DATE 附近
# ============================================================

def _days_between(a: str, b: str) -> int:
    from datetime import date
    ya, ma, da = map(int, a.split("-"))
    yb, mb, db = map(int, b.split("-"))
    return (date(ya, ma, da) - date(yb, mb, db)).days


def valuation_timing_check(conn, plate_rows, limit: int = 300) -> dict:
    """抽 N 只：PE 跳变（价格几乎不动）的日期，是否贴着财报公告日

    若 PE 序列是"按报告期回填"的历史值，跳变应集中在季度末；
    若它是真实的逐日点时值，跳变应跟着 NOTICE_DATE（公告后 1~2 个交易日）。
    """
    events = matched = 0
    base_days = base_hit = 0
    checked = 0
    for row in plate_rows[:limit]:
        symbol = row["symbol"]
        bars = load_bars(conn, symbol)
        if len(bars) < 40:
            continue
        close = {b.date: b.close for b in bars if b.close}
        val = [(d, v) for d, v in sorted(load_valuation(conn, symbol).items())
               if v[0] and v[0] > 0]
        if len(val) < 40:
            continue
        notices = [r[1] for r in load_earnings(conn, symbol) if r[1]]
        if not notices:
            continue
        checked += 1
        for k in range(1, len(val)):
            d0, d1 = val[k - 1][0], val[k][0]
            pe0, pe1 = val[k - 1][1][0], val[k][1][0]
            c0, c1 = close.get(d0), close.get(d1)
            if not c0 or not c1:
                continue
            px_chg = c1 / c0 - 1
            pe_chg = pe1 / pe0 - 1
            near = any(-7 <= _days_between(d1, nd) <= 3 for nd in notices)
            base_days += 1
            base_hit += 1 if near else 0
            if abs(px_chg) < 0.03 and abs(pe_chg) > 0.15:
                events += 1
                matched += 1 if near else 0
    return {
        "symbols_checked": checked,
        "pe_jump_events": events,
        "pe_jump_near_notice_pct": round(matched / events * 100, 1) if events else None,
        "all_days_near_notice_pct": round(base_hit / base_days * 100, 1) if base_days else None,
        "window": "公告日 −7 ~ +3 个自然日",
    }


# ============================================================
# 主回测
# ============================================================

def run(limit: int = 0, out_path: str | None = None, selfcheck: bool = True) -> dict:
    t0 = time.time()
    per_band = {f: {label: {h: array("f") for h in HORIZONS} for label, _, _ in bands}
                for f, bands in FACTOR_BANDS.items()}
    daily = {f: {label: {h: defaultdict(lambda: [0.0, 0]) for h in HORIZONS}
                 for label, _, _ in bands} for f, bands in FACTOR_BANDS.items()}
    syms_band = {f: {label: set() for label, _, _ in bands}
                 for f, bands in FACTOR_BANDS.items()}
    pool_daily = {h: defaultdict(lambda: [0.0, 0]) for h in HORIZONS}
    counters = defaultdict(int)

    with rs.open_research(readonly=True) as conn:
        plate_rows = [dict(r) for r in conn.execute(
            "SELECT symbol, name, industry, last_date FROM research_symbols "
            "WHERE is_main_board=1 AND bar_count>0 AND is_st=0 "
            "AND last_date >= ? ORDER BY symbol", (START_DATE,))]
        if limit:
            plate_rows = plate_rows[:limit]
        plate_names = [r[0] for r in conn.execute(
            "SELECT name FROM research_boards WHERE kind='plate'")]
        log_every = max(50, len(plate_rows) // 20)

        print(f"标的 {len(plate_rows)}，载入行业分位...", flush=True)
        ind_pct = build_industry_percentile(conn)
        print(f"行业分位覆盖 {len(ind_pct)} 个交易日", flush=True)

        for si, row in enumerate(plate_rows, 1):
            symbol, industry = row["symbol"], row["industry"] or ""
            bars = load_bars(conn, symbol)
            if len(bars) < 3:
                counters["skip_short"] += 1
                continue
            val = load_valuation(conn, symbol)
            ern = load_earnings(conn, symbol)
            board = _resolve_board(plate_names, industry) if industry else None
            e_notice = sorted([(r[1], r[3], r[4]) for r in ern if r[1] and r[3] is not None])
            e_update = sorted([(r[2], r[3], r[4]) for r in ern if r[2] and r[3] is not None])
            e_report = sorted([(r[0], r[3], r[4]) for r in ern if r[0] and r[3] is not None])
            p1 = p2 = p3 = 0
            cur_n = cur_u = cur_r = None

            for i in range(len(bars) - 1):
                d = bars[i].date
                while p1 < len(e_notice) and e_notice[p1][0] <= d:
                    cur_n = e_notice[p1]
                    p1 += 1
                while p2 < len(e_update) and e_update[p2][0] <= d:
                    cur_u = e_update[p2]
                    p2 += 1
                while p3 < len(e_report) and e_report[p3][0] <= d:
                    cur_r = e_report[p3]
                    p3 += 1
                if d < START_DATE:
                    continue
                close = bars[i].close
                if not close or not (PRICE_MIN <= close <= PRICE_MAX):
                    counters["skip_price"] += 1
                    continue
                entry = bars[i + 1].open
                if entry <= 0:
                    counters["skip_bad_open"] += 1
                    continue
                if entry >= close * LIMIT_UP_OPEN:
                    counters["skip_limit_up_open"] += 1
                    continue
                if i + max(HORIZONS) >= len(bars):
                    break
                fwd = {h: bars[i + h].close / entry - 1 for h in HORIZONS
                       if bars[i + h].close > 0}
                if len(fwd) < len(HORIZONS):
                    counters["skip_forward_short"] += 1
                    continue

                counters["observations"] += 1
                for h in HORIZONS:
                    a = pool_daily[h][d]
                    a[0] += fwd[h]
                    a[1] += 1

                # 估值（当日值）
                v = val.get(d)
                v_score = None
                if v and (v[0] is not None or v[1] is not None):
                    v_score = valuation_factor(v[0], v[1])
                # 业绩：NOTICE_DATE 时点（主口径）
                g_score = None
                if cur_n is not None:
                    pn = cur_n[2]
                    g_score = growth_score_from_rate(
                        float(cur_n[1]), profitable=None if pn is None else pn >= 0)
                else:
                    counters["growth_missing"] += 1
                # 行业景气
                i_score = None
                if board:
                    i_score = ind_pct.get(d, {}).get(board)
                if i_score is None:
                    counters["industry_missing"] += 1

                def _add(factor: str, score: float | None) -> None:
                    if score is None:
                        return
                    label = band_of(FACTOR_BANDS[factor], score)
                    if label is None:
                        return
                    for h in HORIZONS:
                        per_band[factor][label][h].append(fwd[h])
                        a = daily[factor][label][h][d]
                        a[0] += fwd[h]
                        a[1] += 1
                    syms_band[factor][label].add(symbol)

                _add("valuation", v_score)
                _add("growth_notice", g_score)
                _add("industry", i_score)
                if cur_u is not None:      # 对照：用 UPDATE_DATE（错误口径）
                    pn = cur_u[2]
                    _add("growth_update", growth_score_from_rate(
                        float(cur_u[1]), profitable=None if pn is None else pn >= 0))
                if cur_r is not None:      # 对照：用 REPORTDATE（更错误的回填口径）
                    pn = cur_r[2]
                    _add("growth_report", growth_score_from_rate(
                        float(cur_r[1]), profitable=None if pn is None else pn >= 0))
                if v_score is not None and g_score is not None and i_score is not None:
                    comp = v_score * W_VAL + g_score * W_GROW + i_score * W_IND
                    _add("composite", comp)
                    counters["composite_ok"] += 1

            if si % log_every == 0 or si == len(plate_rows):
                print(f"进度 {si}/{len(plate_rows)} 观察点 {counters['observations']} "
                      f"用时 {(time.time() - t0) / 60:.1f}min", flush=True)

        res: dict = {
            "meta": {
                "symbols": len(plate_rows),
                "start": START_DATE,
                "price_filter": [PRICE_MIN, PRICE_MAX],
                "entry": "open(d+1)",
                "growth_timing": "NOTICE_DATE（主口径）；UPDATE_DATE / REPORTDATE 仅作对照",
                "weights_normalized": {"industry": round(W_IND, 4),
                                       "growth": round(W_GROW, 4),
                                       "valuation": round(W_VAL, 4)},
                "counters": dict(counters),
                "industry_pct_days": len(ind_pct),
                "runtime_min": round((time.time() - t0) / 60, 1),
            },
            "pool": {},
        }
        # 池基准：用逐日均值的均值，避免大样本日主导
        for h in HORIZONS:
            day_means = [a[0] / a[1] for a in pool_daily[h].values() if a[1]]
            res["pool"][f"{h}d"] = {
                "days": len(day_means),
                "mean_of_daily_mean_pct": round(statistics.fmean(day_means) * 100, 3),
                "median_of_daily_mean_pct": round(statistics.median(day_means) * 100, 3),
            }

        # 分组 + 价差 + 同日超额
        for factor, bands in FACTOR_BANDS.items():
            block: dict = {
                "groups": {},
                "spread": {},
                "same_day_excess": {},
            }
            for label, _, _ in bands:
                block["groups"][label] = {
                    "symbols": len(syms_band[factor][label]),
                    **{f"{h}d": describe(per_band[factor][label][h]) for h in HORIZONS},
                }
            hi_label, lo_label = HIGH_LOW[factor]
            for h in HORIZONS:
                diffs = []
                for d in sorted(daily[factor][hi_label][h]):
                    a = daily[factor][hi_label][h][d]
                    b = daily[factor][lo_label][h].get(d)
                    if a[1] and b and b[1]:
                        diffs.append(a[0] / a[1] - b[0] / b[1])
                block["spread"][f"{h}d"] = {
                    "high": hi_label, "low": lo_label,
                    "overlapping": _t(diffs),
                    "non_overlapping_every20d": _t(diffs[::20]),
                }
                # 同日超额（各分组 − 当日全池均值）
                exc: dict = {}
                for label, _, _ in bands:
                    vals = []
                    for d in sorted(daily[factor][label][h]):
                        a = daily[factor][label][h][d]
                        p = pool_daily[h].get(d)
                        if a[1] and p and p[1]:
                            vals.append(a[0] / a[1] - p[0] / p[1])
                    exc[label] = {"overlapping": _t(vals),
                                  "non_overlapping_every20d": _t(vals[::20])}
                block["same_day_excess"][f"{h}d"] = exc
            res[factor] = block

        # 顶组 vs 底组：池化分布检验（含 Mann-Whitney / 胜率两比例 z）
        for factor in FACTOR_BANDS:
            hi_label, lo_label = HIGH_LOW[factor]
            hi = per_band[factor][hi_label][20]
            lo = per_band[factor][lo_label][20]
            if len(hi) >= 30 and len(lo) >= 30:
                k1 = sum(1 for x in hi if x > 0)
                k2 = sum(1 for x in lo if x > 0)
                z, p = two_prop_z(k1, len(hi), k2, len(lo))
                mz, mp = mannwhitney_z(hi, lo)
                res[factor]["top_vs_bottom_20d"] = {
                    "high_n": len(hi), "low_n": len(lo),
                    "median_diff_pct": round(describe(hi)["median_pct"]
                                             - describe(lo)["median_pct"], 2),
                    "win_rate_z": round(z, 2), "win_rate_p": round(p, 5),
                    "dist_z": round(mz, 2), "dist_p": round(mp, 5),
                }

        if selfcheck:
            res["valuation_timing_check"] = valuation_timing_check(conn, plate_rows)

    if out_path:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(json.dumps(res, ensure_ascii=False, indent=2,
                                             default=str), encoding="utf-8")
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="outputs/研究/fundamentals_bt_2026-11-11.json")
    ap.add_argument("--no-selfcheck", action="store_true")
    a = ap.parse_args()
    res = run(a.limit, a.out, not a.no_selfcheck)
    print(json.dumps({k: v for k, v in res.items() if k in
                      ("meta", "valuation_timing_check")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
