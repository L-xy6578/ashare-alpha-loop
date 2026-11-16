"""2019-2023 历史样本外回测（行业景气分位 → 之后 20 日同日超额）

背景：2024-2026 扩样本里唯一有方向的线索是「板块 20 日涨幅分位越高，之后 20 日越弱」
（<50 +0.175% / ≥85 −0.552%），但非重叠独立时点只有 32 个。本模块用**完全没参与发现过程**
的 2019-2023 五年做样本外检验，并按年拆开看——只有某一年成立就是噪声。

口径（与 src.research_fundamentals_bt 完全一致，不得改动）：
    - 信号日 d：收盘价在 5~60 元；买入 = open(d+1)；一字涨停（open ≥ close(d)×1.095）剔除；
    - 前向 = close(d+h)/open(d+1) − 1，h ∈ {5, 20}；前向不足的观察点剔除；
    - 分位 = 当日 zdf20 在「仅行业板块（kind='plate'）」横截面中的分位；池 < 30 记缺失；
    - 同日超额 = 个股前向 − 当日全池（同口径可交易样本）前向均值；
    - t 检验：日频重叠序列 + 每 20 个交易日取 1 天的非重叠子样本。

数据：data/research.db 的 research_bars_hist / research_board_bars_hist（只读）。
反前视：信号只用 d 及以前的数据，买入在 d+1 开盘。

已知偏差（报告必须写明）：
    - 分类前视：行业归属用的是**今天**的东财行业分类回填到 2019-2023；
    - 生存偏差：股票池来自东财当前行情中心快照（含已退市 209 只），但 640 根上限修复后
      仍有极少数长期停牌/代码回收标的缺失；各档同向上偏，只能看**相对差距**。

用法:
    python -m src.research_hist_bt                 # 2019-2023 分年 + 合并
    python -m src.research_hist_bt --five-day-2024 # 2024-2026 的 5 日窗
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import research_store as rs                                    # noqa: E402
from src.factors import MIN_BOARD_POOL, _resolve_board                  # noqa: E402
from src.research_fundamentals_bt import IND_BANDS, _t, band_of, _norm_cdf  # noqa: E402

HORIZONS = (5, 20)
START_DATE, END_DATE = "2019-01-01", "2023-12-31"
PRICE_MIN, PRICE_MAX = 5.0, 60.0
LIMIT_UP_OPEN = 1.095
YEARS = ("2019", "2020", "2021", "2022", "2023")


# ============================================================
# 数据载入（只用 d 及以前的信息）
# ============================================================

def load_bars_hist(conn, symbol: str) -> list[tuple]:
    rows = conn.execute(
        "SELECT date, open, close FROM research_bars_hist WHERE symbol=? ORDER BY date",
        (symbol,)).fetchall()
    return [(r[0], r[1] or 0.0, r[2] or 0.0) for r in rows if r[2]]


def build_industry_percentile_hist(conn) -> dict[str, dict[str, float]]:
    """{date: {板块名: 分位}}——与 research_fundamentals_bt.build_industry_percentile 同口径"""
    names = {r[0]: r[1] for r in conn.execute(
        "SELECT code, name FROM research_boards WHERE kind='plate'")}
    series: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for code, date, close in conn.execute(
            "SELECT code, date, close FROM research_board_bars_hist ORDER BY code, date"):
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


# ============================================================
# 主回测
# ============================================================

def _t_any(vals: list[float], min_n: int = 8) -> dict:
    """均值 t 检验（n≥8 即算；单年独立窗口只有 ~12 个点，务必看 days 再下结论）"""
    n = len(vals)
    if n < min_n:
        return {"days": n}
    m, sd = statistics.fmean(vals), statistics.pstdev(vals)
    se = sd / (n ** 0.5)
    t = m / se if se else 0.0
    return {
        "days": n,
        "mean_pct": round(m * 100, 3),
        "t_stat": round(t, 2),
        "p_two_sided": round(2 * (1 - _norm_cdf(abs(t))), 4),
        "positive_day_pct": round(sum(1 for v in vals if v > 0) / n * 100, 1),
    }


def _empty_bands() -> dict:
    return {label: {h: defaultdict(lambda: [0.0, 0]) for h in HORIZONS}
            for label, _, _ in IND_BANDS}


def run(limit: int = 0, out_path: str | None = None, universe: str = "non_st") -> dict:
    """universe: non_st=与发现过程一致（当前非ST）；all=全部主板（含当前 ST / 已退市，削生存偏差）"""
    daily_year = defaultdict(_empty_bands)          # [年][档][h][日] = [sum, n]
    pool_year = defaultdict(lambda: {h: defaultdict(lambda: [0.0, 0]) for h in HORIZONS})
    syms_year = defaultdict(lambda: {label: set() for label, _, _ in IND_BANDS})
    # 逐观察点计数（不存原始列表，避免内存爆掉）：[obs, sum, win]
    agg_year = defaultdict(lambda: {label: {h: [0, 0.0, 0] for h in HORIZONS}
                                     for label, _, _ in IND_BANDS})
    counters = defaultdict(int)
    with rs.open_research(readonly=True) as conn:
        sql = "SELECT symbol, industry FROM research_symbols WHERE is_main_board=1"
        if universe == "non_st":
            sql += " AND is_st=0"
        rows = [dict(r) for r in conn.execute(sql + " ORDER BY symbol")]
        if limit:
            rows = rows[:limit]
        plate_names = [r[0] for r in conn.execute(
            "SELECT name FROM research_boards WHERE kind='plate'")]
        print(f"标的 {len(rows)}，载入行业分位...", flush=True)
        ind = build_industry_percentile_hist(conn)
        print(f"行业分位覆盖 {len(ind)} 个交易日", flush=True)

        for si, row in enumerate(rows, 1):
            symbol, industry = row["symbol"], row["industry"] or ""
            bars = load_bars_hist(conn, symbol)
            if len(bars) < 3:
                counters["skip_short"] += 1
                continue
            board = _resolve_board(plate_names, industry) if industry else None
            for i in range(len(bars) - 1):
                d, _o, close = bars[i]
                if d < START_DATE or d > END_DATE:
                    continue
                if not close or not (PRICE_MIN <= close <= PRICE_MAX):
                    counters["skip_price"] += 1
                    continue
                entry = bars[i + 1][1]
                if entry <= 0:
                    counters["skip_bad_open"] += 1
                    continue
                if entry >= close * LIMIT_UP_OPEN:
                    counters["skip_limit_up_open"] += 1
                    continue
                if i + max(HORIZONS) >= len(bars):
                    counters["skip_forward_short"] += 1
                    break
                fwd = {h: bars[i + h][2] / entry - 1 for h in HORIZONS if bars[i + h][2] > 0}
                if len(fwd) < len(HORIZONS):
                    counters["skip_forward_short"] += 1
                    continue
                counters["observations"] += 1
                y = d[:4]
                for h in HORIZONS:
                    a = pool_year[y][h][d]
                    a[0] += fwd[h]
                    a[1] += 1
                pct = ind.get(d, {}).get(board) if board else None
                if pct is None:
                    counters["industry_missing"] += 1
                    continue
                label = band_of(IND_BANDS, pct)
                if label is None:
                    continue
                for h in HORIZONS:
                    a = daily_year[y][label][h][d]
                    a[0] += fwd[h]
                    a[1] += 1
                    g = agg_year[y][label][h]
                    g[0] += 1
                    g[1] += fwd[h]
                    g[2] += 1 if fwd[h] > 0 else 0
                syms_year[y][label].add(symbol)
            if si % 400 == 0:
                print(f"  进度 {si}/{len(rows)} 观察点 {counters['observations']}", flush=True)

    def daily_year_lookup(years: tuple[str, ...], label: str, h: int) -> dict:
        out: dict = {}
        for y in years:
            out.update(daily_year[y][label][h])
        return out

    def block(years: tuple[str, ...]) -> dict:
        out: dict = {"bands": {}}
        for label, _, _ in IND_BANDS:
            out["bands"][label] = {}
            for h in HORIZONS:
                days: dict[str, list[float]] = {}
                for y in years:
                    for d, a in daily_year[y][label][h].items():
                        if a[1]:
                            days[d] = [a[0] / a[1], a[1]]
                obs = sum(agg_year[y][label][h][0] for y in years)
                tot = sum(agg_year[y][label][h][1] for y in years)
                win = sum(agg_year[y][label][h][2] for y in years)
                syms: set[str] = set()
                for y in years:
                    syms |= syms_year[y][label]
                excess = []
                for d in sorted(days):
                    bm, bn = days[d]
                    p = None
                    for y in years:
                        if d in pool_year[y][h]:
                            p = pool_year[y][h][d]
                            break
                    if p and p[1]:
                        excess.append(bm - p[0] / p[1])
                out["bands"][label][f"{h}d"] = {
                    "obs": obs,
                    "symbols": len(syms),
                    "days": len(excess),
                    "mean_fwd_pct": round(tot / obs * 100, 3) if obs else None,
                    "win_rate_pct": round(win / obs * 100, 1) if obs else None,
                    "excess_mean_pct": round(statistics.fmean(excess) * 100, 3) if excess else None,
                    "excess_overlapping": _t_any(excess),
                    "excess_non_overlap_20d": _t_any(excess[::20]),
                }
            # 高 ≥85 − 低 <50 的逐日价差
            out.setdefault("spread", {})
            for h in HORIZONS:
                diffs = []
                hi, lo = daily_year_lookup(years, "≥85", h), daily_year_lookup(years, "<50", h)
                for d in sorted(set(hi) & set(lo)):
                    a, b = hi[d], lo[d]
                    if a[1] and b[1]:
                        diffs.append(a[0] / a[1] - b[0] / b[1])
                out["spread"][f"{h}d"] = {
                    "hi_minus_lo_pct": round(statistics.fmean(diffs) * 100, 3) if diffs else None,
                    "overlapping": _t_any(diffs),
                    "non_overlap_20d": _t_any(diffs[::20]),
                }
        return out

    res: dict = {
        "meta": {
            "window": [START_DATE, END_DATE],
            "entry": "open(d+1)",
            "price_filter": [PRICE_MIN, PRICE_MAX],
            "universe": ("沪深主板 · 当前非ST（is_st=0，与发现过程一致）"
                         if universe == "non_st" else
                         "沪深主板 · 全部（含当前 ST 与已退市，用于削生存偏差的敏感性检验）"),
            "industry_pool": "仅 kind='plate'，池 <30 记缺失",
            "counters": dict(counters),
            "bars_table": "research_bars_hist",
        },
        "pool": {},
        "per_year": {},
        "pooled": {},
    }
    for y in YEARS:
        res["per_year"][y] = block((y,))
    res["pooled"] = block(YEARS)
    for y in tuple(YEARS) + ("pooled",):
        years = YEARS if y == "pooled" else (y,)
        res["pool"][y] = {}
        for h in HORIZONS:
            means = []
            for yy in years:
                for a in pool_year[yy][h].values():
                    if a[1]:
                        means.append(a[0] / a[1])
            if means:
                res["pool"][y][f"{h}d"] = {
                    "days": len(means),
                    "mean_of_daily_mean_pct": round(statistics.fmean(means) * 100, 3),
                }
    if out_path:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str),
                                  encoding="utf-8")
    return res


# ============================================================
# 2024-2026 的 5 日窗（现有数据，回答「台账 5 天能否出信号」）
# ============================================================

def five_day_2024(out_path: str | None = None) -> dict:
    """用现有 research_bars / research_board_bars（2024-03 起）复算 5 日窗方向"""
    from src.research_fundamentals_bt import (build_industry_percentile, load_bars,
                                              START_DATE as S24)
    daily: dict[str, dict[str, list[float]]] = {label: {} for label, _, _ in IND_BANDS}
    pool = defaultdict(lambda: [0.0, 0])
    counters = defaultdict(int)
    with rs.open_research(readonly=True) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT symbol, industry FROM research_symbols "
            "WHERE is_main_board=1 AND is_st=0 ORDER BY symbol")]
        plate_names = [r[0] for r in conn.execute(
            "SELECT name FROM research_boards WHERE kind='plate'")]
        ind = build_industry_percentile(conn)
        for row in rows:
            bars = load_bars(conn, row["symbol"])
            if len(bars) < 3:
                counters["skip_short"] += 1
                continue
            board = _resolve_board(plate_names, row["industry"] or "") if row["industry"] else None
            for i in range(len(bars) - 1):
                d, close = bars[i].date, bars[i].close
                if d < S24:
                    continue
                if not close or not (PRICE_MIN <= close <= PRICE_MAX):
                    counters["skip_price"] += 1
                    continue
                entry = bars[i + 1].open
                if entry <= 0 or entry >= close * LIMIT_UP_OPEN:
                    counters["skip_open"] += 1
                    continue
                if i + 5 >= len(bars) or bars[i + 5].close <= 0:
                    counters["skip_forward_short"] += 1
                    break
                r = bars[i + 5].close / entry - 1
                counters["observations"] += 1
                a = pool[d]
                a[0] += r
                a[1] += 1
                pct = ind.get(d, {}).get(board) if board else None
                if pct is None:
                    counters["industry_missing"] += 1
                    continue
                label = band_of(IND_BANDS, pct)
                if label is None:
                    continue
                daily[label].setdefault(d, []).append(r)
    out: dict = {"meta": {"window": [S24, "now"], "horizon": 5,
                          "counters": dict(counters)}, "bands": {}}
    for label, _, _ in IND_BANDS:
        excess = []
        day_means = []
        rets = []
        for d in sorted(daily[label]):
            vals = daily[label][d]
            if not vals or not pool[d][1]:
                continue
            m = statistics.fmean(vals)
            day_means.append(m)
            rets.extend(vals)
            excess.append(m - pool[d][0] / pool[d][1])
        out["bands"][label] = {
            "obs": len(rets),
            "mean_fwd_pct": round(statistics.fmean(rets) * 100, 3) if rets else None,
            "win_rate_pct": round(sum(1 for r in rets if r > 0) / len(rets) * 100, 1)
            if rets else None,
            "excess_mean_pct": round(statistics.fmean(excess) * 100, 3) if excess else None,
            "excess_overlapping": _t(excess),
            "excess_non_overlap_5d": _t(excess[::5]),
        }
    if out_path:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str),
                                  encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="outputs/研究/hist_bt_2019_2023.json")
    ap.add_argument("--universe", default="non_st", choices=("non_st", "all"))
    ap.add_argument("--five-day-2024", action="store_true")
    a = ap.parse_args()
    if a.five_day_2024:
        r = five_day_2024("outputs/研究/five_day_2024_2026.json")
        for label, _, _ in IND_BANDS:
            b = r["bands"][label]
            print(f"{label:>6} n={b['obs']:>6} 均值%={b['mean_fwd_pct']} "
                  f"胜率%={b['win_rate_pct']} 超额%={b['excess_mean_pct']} "
                  f"t(重叠)={b['excess_overlapping'].get('t_stat')} "
                  f"t(非重叠)={b['excess_non_overlap_5d'].get('t_stat')}")
        return
    res = run(a.limit, a.out, a.universe)
    for y in list(YEARS) + ["pooled"]:
        blk = res["per_year"].get(y) if y != "pooled" else res["pooled"]
        print(f"\n== {y} ==")
        for label, _, _ in IND_BANDS:
            b = blk["bands"][label]["20d"]
            print(f"  {label:>6} n={b['obs']:>7} 股票={b['symbols']:>4} "
                  f"均值%={b['mean_fwd_pct']} 胜率%={b['win_rate_pct']} "
                  f"超额%={b['excess_mean_pct']} t重叠={b['excess_overlapping'].get('t_stat')} "
                  f"t非重叠={b['excess_non_overlap_20d'].get('t_stat')}"
                  f"({b['excess_non_overlap_20d'].get('days')})")


if __name__ == "__main__":
    main()
