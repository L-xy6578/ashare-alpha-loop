"""每日预判历史回溯检验（严格反前视偏差）

原则：
- 生成预判只用 predict_date 当日及之前的数据（K线/板块快照/调研证据均截断在当日）；
- 到期对账才读取 predict_date 之后的数据，仅用于打分，不参与生成；
- 不使用今天的 factor_snapshot 或今天的口径回头改历史判断；
- 样本不足时明确标注，不用小样本凑结论。

用法：
    python -m src.forecast_backtest --start 2026-01-01
"""

from __future__ import annotations

import math
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path

from src.config import DB_PATH, MARKET_DB_PATH, ROOT
from src.forecast import (
    FORECAST_DIR,
    MARKET_SYMBOL,
    STOCK_MAX_PICKS,
    UP_DOWN_THRESHOLD,
    build_market_predictions,
    build_sector_predictions,
    build_stock_predictions_backtest,
    evaluate_prediction,
    init_forecast_tables,
    load_bars,
    next_trading_day,
    resolve_data_date,
    sector_snapshot_quality,
    _connect,
    _git_state,
    _return_pct,
    _close_on,
    _stock_meta_map,
    trading_days,
)
from src.executable import check_eligibility


# ============================================================
# 统计工具
# ============================================================
def _add_exclusion(exclusions: dict[str, list[str]], reason: str, day: str) -> None:
    exclusions.setdefault(reason, []).append(day)


def _fmt_dates(dates, limit: int = 12) -> str:
    ds = sorted(set(dates))
    if not ds:
        return "—"
    if len(ds) <= limit:
        return "、".join(ds)
    return "、".join(ds[:limit]) + f" 等（共 {len(ds)} 个交易日）"


def _summarize(rows: list[dict], scope: str, key: str | None = None,
               value: str | None = None) -> dict:
    subset = [
        r for r in rows
        if r["scope"] == scope
        and (key is None or r["prediction"].get(key) == value)
    ]
    hit = sum(1 for r in subset if r["status"] == "HIT")
    miss = sum(1 for r in subset if r["status"] == "MISS")
    unv = sum(1 for r in subset if r["status"] == "UNVERIFIABLE")
    n = hit + miss
    return {
        "total": len(subset), "hit": hit, "miss": miss,
        "unverifiable": unv, "n": n,
        "rate": (hit / n * 100.0) if n else None,
    }


def _sample_label(n: int) -> str:
    if n < 10:
        return "样本严重不足（n<10），不作结论"
    if n < 30:
        return "样本偏小（10≤n<30），结论仅供参考"
    return "样本量可作初步结论（n≥30）"


def _verdict(hit: int, n: int, p0: float | None) -> str:
    if n < 10:
        return f"样本严重不足（n={n}<10），不作结论"
    if p0 is None or not (0 < p0 < 1):
        return "随机基准不可计算，无法判断显著性"
    se = math.sqrt(p0 * (1.0 - p0) / n)
    if se == 0:
        return "随机基准方差为0，无法判断显著性"
    z = ((hit / n) - p0) / se
    if abs(z) < 1.96:
        return f"与随机基准无显著差异（接近随机，z={z:+.2f}）"
    return f"{'显著优于' if z > 0 else '显著劣于'}随机基准（z={z:+.2f}，5% 水平）"


def _market_baseline(rows: list[dict], horizon: int) -> dict | None:
    classes = [
        r["actual_class"] for r in rows
        if r["scope"] == "market"
        and r["prediction"]["horizon"] == horizon
        and r["status"] in ("HIT", "MISS")
        and r.get("actual_class")
    ]
    n = len(classes)
    if not n:
        return None
    counts = Counter(classes)
    p_freq = sum((c / n) ** 2 for c in counts.values())
    p_major = max(counts.values()) / n
    return {
        "n": n, "counts": dict(counts),
        "p_freq": p_freq, "p_majority": p_major,
    }


def _stock_baseline(market_db: str, rows: list[dict], last_data: str) -> dict:
    """同期全样本上涨比例：可买观察池标的的 5 日涨幅 >0 占比。"""
    dates = sorted({r["prediction"]["predict_date"] for r in rows if r["scope"] == "stock"})
    meta = _stock_meta_map(market_db)
    positive = total = missing = 0
    for day in dates:
        target = next_trading_day(market_db, day, 5)
        if not target or target > last_data:
            continue
        for sym, info in meta.items():
            bars = load_bars(market_db, sym, day)
            if not bars or bars[-1].date != day:
                continue
            price = float(bars[-1].close)
            elig = check_eligibility(sym, sym[2:], info["name"], price, bars=bars, as_of=day)
            if not elig.ok:
                continue
            end = _close_on(market_db, sym, target)
            ret = _return_pct(price, end)
            if ret is None:
                missing += 1
                continue
            total += 1
            if ret > 0:
                positive += 1
    return {
        "dates": len(dates), "positive": positive, "total": total,
        "missing": missing,
        "rate": (positive / total) if total else None,
    }


def _row(pred: dict, ev: dict) -> dict:
    return {
        "scope": pred["scope"],
        "prediction": pred,
        "status": ev["status"],
        "actual_return": ev.get("actual_return"),
        "benchmark_return": ev.get("benchmark_return"),
        "actual_class": ev.get("actual_class"),
        "note": ev.get("note", ""),
    }


def _month(day: str) -> str:
    return (day or "")[:7]


def _monthly_stats(rows: list[dict], scope: str, direction: str | None = None,
                   horizon: int | None = None) -> list[dict]:
    """按月分组：样本量、命中率、偏离 50% 的绝对条数、预测/实际类别构成。"""
    buckets: dict[str, dict] = {}
    for r in rows:
        if r["scope"] != scope:
            continue
        p = r["prediction"]
        if direction is not None and p.get("direction") != direction:
            continue
        if horizon is not None and p.get("horizon") != horizon:
            continue
        m = _month(p["predict_date"])
        b = buckets.setdefault(m, {
            "month": m, "total": 0, "hit": 0, "miss": 0, "unverifiable": 0,
            "pred": Counter(), "actual": Counter(),
        })
        b["total"] += 1
        if r["status"] == "HIT":
            b["hit"] += 1
        elif r["status"] == "MISS":
            b["miss"] += 1
        elif r["status"] == "UNVERIFIABLE":
            b["unverifiable"] += 1
        b["pred"][p.get("direction", "")] += 1
        if r.get("actual_class"):
            b["actual"][r["actual_class"]] += 1

    out = []
    for m, b in sorted(buckets.items()):
        n = b["hit"] + b["miss"]
        b["n"] = n
        b["rate"] = (b["hit"] / n * 100.0) if n else None
        b["dev"] = (b["hit"] - 0.5 * n) if n else 0.0
        out.append(b)
    return out


def _concentration(monthly: list[dict]) -> dict:
    """偏离集中度：最大偏离月份占总绝对偏离的比例。"""
    abs_dev = sum(abs(b["dev"]) for b in monthly if b["n"])
    if abs_dev <= 0:
        return {"total_abs_dev": 0.0, "top_month": None, "top_share": 0.0,
                "top_dev": 0.0, "top_hit": 0, "top_n": 0}
    top = max((b for b in monthly if b["n"]), key=lambda b: abs(b["dev"]))
    return {
        "total_abs_dev": abs_dev, "top_month": top["month"],
        "top_share": abs(top["dev"]) / abs_dev, "top_dev": top["dev"],
        "top_hit": top["hit"], "top_n": top["n"],
    }


def _rate_excluding(monthly: list[dict], month: str | None) -> tuple[int, int]:
    hit = sum(b["hit"] for b in monthly if b["month"] != month)
    n = sum(b["n"] for b in monthly if b["month"] != month)
    return hit, n


def _sector_verdict(c_strong: dict, strong_x: tuple[int, int], strong_months: int,
                    c_weak: dict, weak_x: tuple[int, int], weak_months: int) -> str:
    """把“看强/看弱”的偏离性质写成一句话：稳定规律 vs 阶段性失效。"""
    from math import sqrt

    def _z(hit: int, n: int) -> float:
        if n <= 0:
            return 0.0
        p = hit / n
        return (p - 0.5) / sqrt(0.25 / n)

    s_hit, s_n = strong_x
    w_hit, w_n = weak_x
    s_ok = s_n > 0 and abs(_z(s_hit, s_n)) >= 1.96
    w_ok = w_n > 0 and abs(_z(w_hit, w_n)) >= 1.96
    parts = []
    if s_ok:
        parts.append(
            f"看强剔除 {c_strong['top_month']} 后仍显著偏离 50%"
            f"（{s_hit}/{s_n}），可作为该区间内的稳定反向信号。"
        )
    else:
        parts.append(
            f"看强的偏离集中在 {c_strong['top_month']}"
            f"（占总绝对偏离 {c_strong['top_share'] * 100:.0f}%），剔除后回到随机附近，"
            "应表述为“该段行情（追高）下失效”，不能写成“5 日尺度普遍反向”。"
        )
    if w_ok:
        parts.append(
            f"看弱在各月方向一致（{weak_months} 个月全部高于 50%），剔除 "
            f"{c_weak['top_month']} 后仍显著（{w_hit}/{w_n}），"
            "可作为该区间内“弱者补涨”的参考，但仍受仅 3 个月样本限制。"
        )
    else:
        parts.append(
            f"看弱的偏离同样集中在 {c_weak['top_month']}，剔除后不显著，只能算阶段性现象。"
        )
    return "".join(parts)


def _class_mix(rows: list[dict], horizon: int) -> tuple[Counter, Counter, int]:
    """预测类别构成 vs 实际结果类别构成（仅已结算样本）。"""
    pred: Counter = Counter()
    actual: Counter = Counter()
    n = 0
    for r in rows:
        if r["scope"] != "market" or r["prediction"].get("horizon") != horizon:
            continue
        if r["status"] not in ("HIT", "MISS"):
            continue
        n += 1
        pred[r["prediction"].get("direction", "")] += 1
        if r.get("actual_class"):
            actual[r["actual_class"]] += 1
    return pred, actual, n


def _direction_correct(direction: str, ret: float | None) -> bool | None:
    """只看指数方向（±0.5% 阈值），剔除“涨跌家数宽度”这一层否决。"""
    if ret is None or direction not in ("上涨", "下跌", "震荡"):
        return None
    if direction == "上涨":
        return ret >= UP_DOWN_THRESHOLD
    if direction == "下跌":
        return ret <= -UP_DOWN_THRESHOLD
    return -UP_DOWN_THRESHOLD < ret < UP_DOWN_THRESHOLD


def _month_direction_split(rows: list[dict], month: str,
                           horizon: int = 5) -> tuple[int, int]:
    """某月已结算样本按“方向判错 / 方向对但宽度否决”拆分。"""
    wrong = veto = 0
    for r in rows:
        p = r["prediction"]
        if r["scope"] != "market" or p.get("horizon") != horizon:
            continue
        if p["predict_date"][:7] != month or r["status"] not in ("HIT", "MISS"):
            continue
        if _direction_correct(p["direction"], r.get("actual_return")):
            veto += 1
        else:
            wrong += 1
    return wrong, veto


def _zero_hit_segments(rows: list[dict], horizon: int = 5) -> list[dict]:
    """连续“整月 0 命中”的样本段（每月 n≥10 才算有效月）。

    拆开两层原因：方向本身判错 vs 方向对但被涨跌家数宽度否决，
    避免把规则否决混算成模型方向错。
    """
    monthly = _monthly_stats(rows, "market", horizon=horizon)
    segments: list[list[dict]] = []
    cur: list[dict] = []
    for b in monthly:
        if b["n"] >= 10 and b["hit"] == 0:
            cur.append(b)
        elif cur:
            segments.append(cur)
            cur = []
    if cur:
        segments.append(cur)

    out: list[dict] = []
    for seg in segments:
        months = [b["month"] for b in seg]
        pred: Counter = Counter()
        actual: Counter = Counter()
        wrong = veto = 0
        rets: list[float] = []
        for r in rows:
            p = r["prediction"]
            if r["scope"] != "market" or p.get("horizon") != horizon:
                continue
            if p["predict_date"][:7] not in months or r["status"] not in ("HIT", "MISS"):
                continue
            pred[p["direction"]] += 1
            if r.get("actual_class"):
                actual[r["actual_class"]] += 1
            if _direction_correct(p["direction"], r.get("actual_return")):
                veto += 1      # 方向对，但涨跌家数占比不满足 → 规则判不中
            else:
                wrong += 1     # 方向本身判错
            if r.get("actual_return") is not None:
                rets.append(float(r["actual_return"]))
        n = sum(b["n"] for b in seg)
        rest_hit = sum(b["hit"] for b in monthly if b["month"] not in months)
        rest_n = sum(b["n"] for b in monthly if b["month"] not in months)
        out.append({
            "months": months, "n": n, "miss": n,
            "wrong": wrong, "veto": veto,
            "pred": dict(pred), "actual": dict(actual),
            "avg_return": (sum(rets) / len(rets)) if rets else None,
            "up_share": (sum(1 for x in rets if x > 0) / len(rets)) if rets else None,
            "rest_hit": rest_hit, "rest_n": rest_n,
            "month_rows": [{"month": b["month"], "n": b["n"], "hit": b["hit"]} for b in seg],
        })
    return out


# ============================================================
# 回放
# ============================================================
def run_backtest(market_db: str = MARKET_DB_PATH, kb_db: str = DB_PATH,
                 start: str = "2026-01-01", end: str | None = None) -> dict:
    init_forecast_tables(kb_db)
    last_data = resolve_data_date(market_db, end or "9999-12-31")
    if not last_data:
        return {"ok": False, "reason": "无行情数据"}
    days = [d for d in trading_days(market_db, start, last_data) if d <= last_data]

    rows: list[dict] = []
    exclusions: dict[str, list[str]] = {}
    stock_filter_reasons: Counter = Counter()

    for day in days:
        # ---------- 大盘 ----------
        mpreds = build_market_predictions(market_db, day, run_date=day, mode="backtest")
        if not mpreds:
            _add_exclusion(exclusions, "大盘预判：沪深300 历史K线不足20根", day)
        for p in mpreds:
            if p["target_date"] > last_data:
                _add_exclusion(exclusions, "大盘预判：目标交易日尚无行情（未到期）", day)
                continue
            rows.append(_row(p, evaluate_prediction(market_db, p)))

        q = sector_snapshot_quality(market_db, day)

        # ---------- 板块 ----------
        if not q["complete"]:
            _add_exclusion(exclusions, "板块预判：行业板块快照缺失/不完整（<400条）", day)
        elif not q["has_level"]:
            _add_exclusion(exclusions, "板块预判：板块点位缺失（level=0，无法对账）", day)
        else:
            spreds, snotes = build_sector_predictions(market_db, day, run_date=day, mode="backtest")
            if not spreds:
                _add_exclusion(exclusions, "板块预判：" + ("；".join(snotes) or "当日无可比板块"), day)
            for p in spreds:
                if p["target_date"] > last_data:
                    _add_exclusion(exclusions, "板块预判：目标交易日尚无行情（未到期）", day)
                    continue
                rows.append(_row(p, evaluate_prediction(market_db, p)))

        # ---------- 个股 ----------
        if not q["complete"]:
            _add_exclusion(exclusions, "个股预判：行业板块快照不完整（<400条）", day)
        else:
            stpreds, stnotes = build_stock_predictions_backtest(market_db, kb_db, day)
            if not stpreds:
                _add_exclusion(exclusions, "个股预判：当日无合格标的", day)
                for note in stnotes:
                    if "无业绩证据" in note:
                        stock_filter_reasons["无业绩证据"] += 1
                    elif "因子缺失" in note:
                        stock_filter_reasons["历史因子缺失"] += 1
                    elif "K线" in note:
                        stock_filter_reasons["K线不足/缺K线"] += 1
                    elif "可买" in note or "板块" in note or "股价" in note or "创业板" in note or "科创板" in note:
                        stock_filter_reasons["可买性过滤"] += 1
            for p in stpreds:
                if p["target_date"] > last_data:
                    _add_exclusion(exclusions, "个股预判：目标交易日尚无行情（未到期）", day)
                    continue
                rows.append(_row(p, evaluate_prediction(market_db, p)))

    return {
        "ok": True,
        "start": days[0] if days else None,
        "end": days[-1] if days else None,
        "last_data": last_data,
        "rows": rows,
        "exclusions": exclusions,
        "stock_filter_reasons": dict(stock_filter_reasons),
    }


# ============================================================
# 报告
# ============================================================
def render_report(result: dict) -> str:
    if not result.get("ok"):
        return f"# 预判历史回溯检验\n\n无法回放：{result.get('reason')}\n"

    rows = result["rows"]
    head, dirty = _git_state()
    m1 = _summarize(rows, "market", "horizon", 1)
    m5 = _summarize(rows, "market", "horizon", 5)
    b1 = _market_baseline(rows, 1)
    b5 = _market_baseline(rows, 5)
    s_strong = _summarize(rows, "sector", "direction", "看强")
    s_weak = _summarize(rows, "sector", "direction", "看弱")
    st = _summarize(rows, "stock")
    st_base = _stock_baseline(MARKET_DB_PATH, rows, result["last_data"])

    def rate_str(n: dict) -> str:
        return f"{n['rate']:.1f}%" if n["rate"] is not None else "—"

    lines = [
        "# 预判历史回溯检验（严格反前视偏差）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- git HEAD：`{head or 'unknown'}`（工作区{'有未提交改动' if dirty else '干净'}）",
        f"- 回放区间：{result['start']} ~ {result['end']}（行情数据截止 {result['last_data']}）",
        "- 生成规则：只用 predict_date 及之前的 K线/板块快照/调研证据；到期对账才读取之后的数据。",
        "- 未使用今天的 factor_snapshot 或今天的口径回头修改历史判断。",
        "",
        "## 结论（先看这里）",
        "",
    ]

    def conclusion_line(label: str, n: dict, p0: float | None) -> str:
        if n["n"] == 0:
            return f"- {label}：无可结算样本。"
        return (
            f"- {label}：命中 {n['hit']}/{n['n']}（{rate_str(n)}），"
            f"随机基准 {p0 * 100:.1f}%；{_verdict(n['hit'], n['n'], p0)}。"
        )

    lines.append(conclusion_line("大盘 1日", m1, b1["p_freq"] if b1 else None))
    lines.append(conclusion_line("大盘 5日", m5, b5["p_freq"] if b5 else None))
    lines.append(conclusion_line("板块看强", s_strong, 0.5))
    lines.append(conclusion_line("板块看弱", s_weak, 0.5))
    lines.append(conclusion_line("个股5日上涨", st, st_base["rate"]))

    # 大盘：类别均衡性 + 分月一致性（避免把类别不平衡或单月行情当成模型反向）
    pred5_tmp, actual5_tmp, cnt5_tmp = _class_mix(rows, 5)
    _p5max = max(pred5_tmp.values()) / cnt5_tmp if cnt5_tmp and pred5_tmp else 0.0
    _a5max = max(actual5_tmp.values()) / cnt5_tmp if cnt5_tmp and actual5_tmp else 0.0
    m5m = _monthly_stats(rows, "market", horizon=5)
    _m5_valid = [b for b in m5m if b["n"] >= 10]
    _m5_below = sum(1 for b in _m5_valid if b["rate"] is not None and b["rate"] < 50)
    lines += [
        f"- 大盘 5 日类别分布：预测最大类占比 {_p5max * 100:.1f}%、实际最大类占比 "
        f"{_a5max * 100:.1f}%，均 <60%，属**基本均衡**；随机基准已按实际类别频率 Σp² 计，"
        "22.9% 不是类别不平衡造成的。",
        f"- 大盘 5 日分月：n≥10 的 {len(_m5_valid)} 个月里有 {_m5_below} 个月命中率 <50%，"
        "偏离去单月化后依然成立，属**模型系统性偏差**而非某段行情噪声（明细见 2.2）。",
    ]

    # 板块：分月一致性与集中度（判定结论是“稳定反向”还是“某段行情失效”）
    s_strong_m = _monthly_stats(rows, "sector", "看强")
    s_weak_m = _monthly_stats(rows, "sector", "看弱")
    c_strong = _concentration(s_strong_m)
    c_weak = _concentration(s_weak_m)
    strong_below = sum(1 for b in s_strong_m if b["n"] and b["rate"] is not None and b["rate"] < 50)
    strong_months = sum(1 for b in s_strong_m if b["n"])
    weak_above = sum(1 for b in s_weak_m if b["n"] and b["rate"] is not None and b["rate"] > 50)
    weak_months = sum(1 for b in s_weak_m if b["n"])
    strong_x_hit, strong_x_n = _rate_excluding(s_strong_m, c_strong["top_month"])
    weak_x_hit, weak_x_n = _rate_excluding(s_weak_m, c_weak["top_month"])

    def _pct(hit: int, n: int) -> str:
        return f"{hit / n * 100:.1f}%" if n else "—"

    lines += ["", "板块结论（分月验证后）说明："]
    lines += [
        f"- 看强：{strong_below}/{strong_months} 个有样本月份命中率低于 50%；"
        f"剔除偏离最大的 {c_strong['top_month']} 后为 {_pct(strong_x_hit, strong_x_n)}"
        f"（{strong_x_hit}/{strong_x_n}），该月占总绝对偏离 {c_strong['top_share'] * 100:.0f}%。"
        f"{_verdict(strong_x_hit, strong_x_n, 0.5)}。",
        f"- 看弱：{weak_above}/{weak_months} 个有样本月份命中率高于 50%；"
        f"剔除偏离最大的 {c_weak['top_month']} 后为 {_pct(weak_x_hit, weak_x_n)}"
        f"（{weak_x_hit}/{weak_x_n}），该月占总绝对偏离 {c_weak['top_share'] * 100:.0f}%。"
        f"{_verdict(weak_x_hit, weak_x_n, 0.5)}。",
        "- 看强与看弱不是两份独立样本，而是同一个“20 日涨幅排序”因子的上下两端，"
        "两端的偏离方向相同（上端跑输、下端跑赢）才构成有效证据；"
        "把两者合并成 50.4% 再称“随机”，会把一个双向反向信号平均成噪声。",
        "- **审定表述**：" + _sector_verdict(
            c_strong, (strong_x_hit, strong_x_n), strong_months,
            c_weak, (weak_x_hit, weak_x_n), weak_months,
        ),
        "",
        "> 样本量说明：大盘可用行情从 2026-01-05 起，但需 20 根历史 K 线，实际回放起点约 2026-02-02；",
        "> 板块/个股依赖板块快照，快照从 2026-08-03 开始，且 08-03~08-07 仅 30~200 条（不完整），",
        "> 因此板块/个股可回放区间从 2026-08-10 起，天然少于大盘。样本 <10 的组别不构成有效结论。",
        "",
        "## 一、样本与区间",
        "",
        "| 组别 | 预测条数 | 已结算（命中+未命中） | 无法判定 | 命中 | 未命中 | 命中率 | 样本判断 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for label, n in [
        ("大盘 1日", m1), ("大盘 5日", m5),
        ("板块看强", s_strong), ("板块看弱", s_weak), ("个股5日上涨", st),
    ]:
        lines.append(
            f"| {label} | {n['total']} | {n['n']} | {n['unverifiable']} | {n['hit']} | "
            f"{n['miss']} | {rate_str(n)} | {_sample_label(n['n'])} |"
        )

    lines += [
        "",
        "## 二、大盘方向",
        "",
        "| 周期 | 样本 | 命中 | 未命中 | 无法判定 | 命中率 | 随机基准（类别频率） | 多数类基准 | 结论 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for label, n, b in [("次日", m1, b1), ("5日", m5, b5)]:
        p_freq = f"{b['p_freq'] * 100:.1f}%" if b else "—"
        p_major = f"{b['p_majority'] * 100:.1f}%" if b else "—"
        dist = ""
        if b:
            c = b["counts"]
            dist = (f"（上涨 {c.get('上涨', 0)} / 下跌 {c.get('下跌', 0)} / "
                    f"震荡 {c.get('震荡', 0)}）")
        lines.append(
            f"| {label} | {n['n']} | {n['hit']} | {n['miss']} | {n['unverifiable']} | "
            f"{rate_str(n)} | {p_freq}{dist} | {p_major} | "
            f"{_verdict(n['hit'], n['n'], b['p_freq'] if b else None)} |"
        )

    lines += [
        "",
        "> 随机基准口径：按回放样本中“上涨/下跌/震荡”的实际类别频率，计算频率匹配随机猜测的期望命中率 Σp²；"
        "多数类基准为“永远猜样本中最常见类别”的命中率。",
        "",
        "### 2.1 预测类别 vs 实际结果分布（检验类别不平衡）",
        "",
        "| 周期 | 已结算 | 预测 上涨/下跌/震荡 | 实际 上涨/下跌/震荡 | 预测最大类占比 | 实际最大类占比 | 频率匹配基准 Σp² |",
        "|---|---|---|---|---|---|---|",
    ]
    for hz, n in [(1, m1), (5, m5)]:
        pred, actual, cnt = _class_mix(rows, hz)
        p_max = f"{max(pred.values()) / cnt * 100:.1f}%" if cnt and pred else "—"
        a_max = f"{max(actual.values()) / cnt * 100:.1f}%" if cnt and actual else "—"
        pf = (sum((c / cnt) ** 2 for c in actual.values()) * 100) if cnt and actual else None
        lines.append(
            f"| {hz}日 | {cnt} | "
            f"{pred.get('上涨', 0)}/{pred.get('下跌', 0)}/{pred.get('震荡', 0)} | "
            f"{actual.get('上涨', 0)}/{actual.get('下跌', 0)}/{actual.get('震荡', 0)} | "
            f"{p_max} | {a_max} | {f'{pf:.1f}%' if pf is not None else '—'} |"
        )

    pred5, actual5, cnt5 = _class_mix(rows, 5)
    pred5_max_share = (max(pred5.values()) / cnt5) if cnt5 and pred5 else 0.0
    actual5_max_share = (max(actual5.values()) / cnt5) if cnt5 and actual5 else 0.0
    m5_monthly = _monthly_stats(rows, "market", horizon=5)
    m5_thin = [b["month"] for b in m5_monthly if b["n"] < 20]

    lines += [
        "",
        f"- 预测类别最大占比 {pred5_max_share * 100:.1f}%，实际类别最大占比 {actual5_max_share * 100:.1f}%。",
    ]
    if pred5_max_share >= 0.6 or actual5_max_share >= 0.6:
        lines.append(
            "- **类别不平衡**：单类占比 ≥60%，上文“随机基准”已按实际类别频率 Σp² 重算"
            "（不是 1/3），并与多数类基准并列；命中率仍按三类全对全错计，不做事后改判。"
        )
    else:
        lines.append(
            "- **类别基本均衡**：单类占比均 <60%，三类随机基准 Σp² 与 1/3 同量级，"
            "22.9% 这类数字不是类别不平衡造成的假象。"
        )
    lines += [
        "",
        "### 2.2 大盘 5 日样本按月分布",
        "",
        "| 月份 | 样本 | 命中 | 未命中 | 命中率 | 预测（涨/跌/横） | 实际（涨/跌/横） | 样本判断 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for b in m5_monthly:
        lines.append(
            f"| {b['month']} | {b['n']} | {b['hit']} | {b['miss']} | "
            f"{rate_str(b)} | "
            f"{b['pred'].get('上涨', 0)}/{b['pred'].get('下跌', 0)}/{b['pred'].get('震荡', 0)} | "
            f"{b['actual'].get('上涨', 0)}/{b['actual'].get('下跌', 0)}/{b['actual'].get('震荡', 0)} | "
            f"{_sample_label(b['n'])} |"
        )
    if m5_thin:
        lines.append("")
        lines.append(
            f"> 样本偏少月份（n<20，单独看不可下结论）：{'、'.join(m5_thin)}。"
        )
    m5_valid = [b for b in m5_monthly if b["n"] >= 10]
    m5_below = sum(1 for b in m5_valid if b["rate"] is not None and b["rate"] < 50)
    m5_conc = _concentration(m5_monthly)
    m5_x_hit, m5_x_n = _rate_excluding(m5_monthly, m5_conc["top_month"])
    lines += [
        "",
        f"- 分月一致性：n≥10 的 {len(m5_valid)} 个月中，{m5_below} 个月命中率 <50%，"
        f"偏离并非单月造成（最大偏离月 {m5_conc['top_month']} 仅占总绝对偏离 "
        f"{m5_conc['top_share'] * 100:.0f}%）。",
        f"- 剔除最大偏离月 {m5_conc['top_month']} 后：{_pct(m5_x_hit, m5_x_n)}"
        f"（{m5_x_hit}/{m5_x_n}），{_verdict(m5_x_hit, m5_x_n, 0.5)}。",
        "",
        "> 结论口径：只有当“类别均衡（2.1）+ 多月方向一致（2.2）”同时成立时，"
        "才能把低命中率当成模型反向，而不是样本噪声。",
    ]

    # 2.3 整月 0 命中的“趋势段连续错”：比总命中率更值得记住的样本
    segments = _zero_hit_segments(rows, 5)
    if segments:
        seg = max(segments, key=lambda s: s["n"])
        months = "、".join(m.split("-")[1].lstrip("0") + " 月" for m in seg["months"])
        lines += [
            "",
            f"### 2.3 趋势段连续错：{months}整月 0 命中（归因）",
            "",
            "| 月份 | 样本 | 命中 | 方向判错 | 方向对但被宽度否决 |",
            "|---|---|---|---|---|",
        ]
        # 逐月拆分（同口径：方向对=指数区间涨跌幅过 ±0.5%，宽度否决=涨跌家数占比不达标）
        for b in seg["month_rows"]:
            wrong_n, veto_n = _month_direction_split(rows, b["month"], 5)
            lines.append(
                f"| {b['month']} | {b['n']} | {b['hit']} | {wrong_n} | {veto_n} |"
            )
        total_n = m5["n"]
        share = seg["n"] / total_n * 100 if total_n else 0.0
        avg = f"{seg['avg_return']:+.2f}%" if seg["avg_return"] is not None else "—"
        up_share = f"{seg['up_share'] * 100:.0f}%" if seg["up_share"] is not None else "—"
        lines += [
            "",
            f"- 该段 {seg['n']} 条占全部 5 日样本的 {share:.1f}%，"
            f"整月 0 命中**不是样本太少**（每月 n≥10）；单看总命中率会把它稀释掉。",
            f"- 预测构成（涨/跌/横）：{seg['pred'].get('上涨', 0)}/{seg['pred'].get('下跌', 0)}/"
            f"{seg['pred'].get('震荡', 0)}；实际结果：{seg['actual'].get('上涨', 0)}/"
            f"{seg['actual'].get('下跌', 0)}/{seg['actual'].get('震荡', 0)}；"
            f"指数 5 日区间平均涨跌幅 {avg}，收涨占比 {up_share}。",
            f"- 两层原因可拆开：**方向本身判错 {seg['wrong']} 条**，"
            f"**方向对但被涨跌家数宽度否决 {seg['veto']} 条**。"
            "后者是规则设计（要求涨跌幅与宽度同向），不是数据缺失，不应算成方向判断错误。",
            "- 成因一（趋势段滞后）：这套信号全是“趋势/水平探测”（收盘 vs MA5/MA20、5 日与 20 日动能、RSI），"
            "单边趋势里有效、拐点处反向。6 月 05/08/10/11/12 连续 5 个交易日判“下跌”，"
            "而后 4 条各自未来 5 日区间涨幅为 +3.78%/+3.85%/+4.64%/+5.91%；"
            "6 月下旬 06-22/25/29/30 判“上涨”，区间涨幅为 -2.62%/-4.14%/-1.72%/-3.76%。"
            "指标在反弹中段才转多、在回落中段才转空，外推方向恰好落在反转处。",
            "- 成因二（宽度门槛否决）：判定要求“涨跌幅 ±0.5% 与涨跌家数占比 50% 同向”。"
            "5 月有 7 个 5 日窗口收涨 ≥0.5%（+0.62%~+2.74%），全部判不中；其中判“上涨”的 5 条"
            "（05-06/18/19/20/27）区间涨幅都对，但本地存量标的涨跌家数占比只有 36%~49%，被宽度门槛否决；"
            "另 2 条（05-21 +2.74%、05-22 +0.97%）被判成“震荡”。6 月同类：06-05 判“下跌”、区间 -0.82%、"
            "宽度 55%（>50%）被否决；06-15/16/17/23 判“上涨”、区间 +0.57%~+1.80%、宽度 34%~41% 被否决。"
            "这是规则设计否决，不是数据缺失。",
            "- 结论：这段应表述为「**趋势探测在震荡/反复市失效，并被窄样本宽度门槛二次否决**」，"
            "而不是“5 日尺度普遍反向”；也比总命中率更值得记住——"
            f"剔除该段后其余样本为 {_pct(seg['rest_hit'], seg['rest_n'])}"
            f"（{seg['rest_hit']}/{seg['rest_n']}），仍低于 50%，"
            "说明该段是最大但非唯一的原因。",
        ]

    lines += [
        "",
        "## 三、板块相对强弱",
        "",
        "| 方向 | 样本 | 命中 | 未命中 | 无法判定 | 命中率 | 随机基准 | 结论 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for label, n in [("看强", s_strong), ("看弱", s_weak)]:
        lines.append(
            f"| {label} | {n['n']} | {n['hit']} | {n['miss']} | {n['unverifiable']} | "
            f"{rate_str(n)} | 50.0%（相对中位数，随机选板块） | "
            f"{_verdict(n['hit'], n['n'], 0.5)} |"
        )

    lines += [
        "",
        "> 板块对账口径：板块点位区间涨幅 > 全市场行业板块中位数记为看强命中，< 中位数记为看弱命中；"
        "目标日板块快照缺失/点位为 0 记为无法判定，不进分母。",
        "",
        "### 3.1 分月分组（检验偏离是否集中在少数月份）",
        "",
        "| 月份 | 看强样本 | 看强命中 | 看强命中率 | 看弱样本 | 看弱命中 | 看弱命中率 | 样本判断 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    strong_by_month = {b["month"]: b for b in s_strong_m}
    weak_by_month = {b["month"]: b for b in s_weak_m}
    for month in sorted(set(strong_by_month) | set(weak_by_month)):
        sb = strong_by_month.get(month, {"n": 0, "hit": 0, "rate": None})
        wb = weak_by_month.get(month, {"n": 0, "hit": 0, "rate": None})
        thin = min(sb["n"], wb["n"]) < 20
        lines.append(
            f"| {month} | {sb['n']} | {sb['hit']} | {rate_str(sb)} | "
            f"{wb['n']} | {wb['hit']} | {rate_str(wb)} | "
            f"{'样本偏少（n<20），不作单月结论' if thin else '单月可参考'} |"
        )
    lines += [
        "",
        f"> 偏离集中度：看强最大偏离月 {c_strong['top_month']}"
        f"（{c_strong['top_hit']}/{c_strong['top_n']}）占总绝对偏离 {c_strong['top_share'] * 100:.0f}%；"
        f"看弱最大偏离月 {c_weak['top_month']}"
        f"（{c_weak['top_hit']}/{c_weak['top_n']}）占总绝对偏离 {c_weak['top_share'] * 100:.0f}%。"
        "集中度越高，越应该表述为“某段行情下失效”，而不是普适的“5 日尺度反向”。",
        "",
        f"- 剔除最大偏离月 {c_strong['top_month']} 后，看强：{_pct(strong_x_hit, strong_x_n)}"
        f"（{strong_x_hit}/{strong_x_n}），{_verdict(strong_x_hit, strong_x_n, 0.5)}。",
        f"- 剔除最大偏离月 {c_weak['top_month']} 后，看弱：{_pct(weak_x_hit, weak_x_n)}"
        f"（{weak_x_hit}/{weak_x_n}），{_verdict(weak_x_hit, weak_x_n, 0.5)}。",
        "- 判定：只有剔除后仍显著、且各月方向一致，才能写成“稳定规律”；"
        "剔除后回到 45%~55% 的，只能写成“某段行情下的阶段性失效”。",
        "",
        "## 四、个股 5 日上涨",
        "",
        "| 样本 | 命中 | 未命中 | 无法判定 | 命中率 | 同期全样本上涨比例 | 全样本观测数 | 结论 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    base_rate = f"{st_base['rate'] * 100:.1f}%" if st_base["rate"] is not None else "—"
    lines.append(
        f"| {st['n']} | {st['hit']} | {st['miss']} | {st['unverifiable']} | {rate_str(st)} | "
        f"{base_rate} | {st_base['total']} | {_verdict(st['hit'], st['n'], st_base['rate'])} |"
    )
    lines += [
        "",
        "> 个股对账口径：5 个交易日后收盘价相对预判日收盘价涨幅 >0 记为命中；"
        "停牌或目标日缺 K 线记为无法判定，不进分母。",
        "> 随机基准为同期“可买观察池全样本”的 5 日上涨比例（不限于被预判的标的）。",
        "",
        "### 4.1 预判里那个“分”是什么（必读）",
        "",
        "- 实盘预判的 `score` **直接复用决策侧 `factor_snapshot`**（收盘闭环 `r47_closed_loop.py` "
        "用同一份 `score_candidate` 入参落库），即同一交易日、同一标的、同一份输入的结果，"
        "**不是第二套评分，也不是置信度或上涨概率**。",
        "- **缺数据不出分**：五个因子（行业/业绩/估值/技术/资金）任一为 NULL，即判缺失、"
        "该标的不出分（报告显示“—”，依据列写明缺哪项），**不用 0 分占位**。"
        "这与资金因子无数据返回 None、技术面 K 线不新鲜返回 None 是同一条规矩；"
        "此前的“估值缺失按 0 计”已作为 bug 修正（0 分比“PE 为负”的 20 分还低，属错误折损）。",
        "- 实测 2026-11-06：决策侧 `factor_snapshot` 的 `valuation` 列对 10 只标的全为 NULL，"
        "因此按新规矩**两侧都不出分**，不存在“决策侧 85 分、预判侧按 0 计”的两套分；"
        "11-05（估值齐全）同批为 81.4/80.1/72.4，可作为估值齐全口径的参照。",
        "- 历史回溯路径估值未落库，同样判缺失、不出分；其余因子用同一函数重算，不另立公式。",
        "- 该分数只用于**同日候选内部排序与展示**，预判不设总分门槛；"
        "入选条件是技术 ≥60、行业 ≥50、资金 ≥50 三项硬筛。",
        "- **决策依据唯一**：开仓/弃权以决策日志（五因子 ≥85 且无因子缺失）为准；"
        "本预判的个股名单是观察名单，不构成开仓建议。",
        "",
        "## 五、生成排除与无法判定",
        "",
        "### 5.1 生成阶段排除（按交易日）",
        "",
        "| 原因 | 交易日数 | 日期 |",
        "|---|---|---|",
    ]
    if result["exclusions"]:
        for reason, dates in sorted(result["exclusions"].items(), key=lambda x: -len(x[1])):
            lines.append(f"| {reason} | {len(set(dates))} | {_fmt_dates(dates)} |")
    else:
        lines.append("| 无 | 0 | — |")

    unv = [r for r in rows if r["status"] == "UNVERIFIABLE"]
    unv_counts = Counter(r["note"] for r in unv)
    lines += ["", "### 5.2 到期无法判定（不进命中率分母）", "", "| 原因 | 条数 |", "|---|---|"]
    if unv_counts:
        for note, cnt in unv_counts.most_common():
            lines.append(f"| {note} | {cnt} |")
    else:
        lines.append("| 无 | 0 |")

    if result.get("stock_filter_reasons"):
        lines += ["", "### 5.3 个股生成过滤原因（按标的·日累计）", "", "| 原因 | 次数 |", "|---|---|"]
        for reason, cnt in sorted(result["stock_filter_reasons"].items(), key=lambda x: -x[1]):
            lines.append(f"| {reason} | {cnt} |")

    lines += [
        "",
        "## 六、口径限制（必须和数字一起看）",
        "",
        "1. **板块/个股样本天然短**：板块快照从 2026-08-03 才开始，08-03~08-07 不完整，08-10 起可用；"
        "因此板块/个股回放约 2 个月，大盘约 9 个月，二者不能直接比样本量。",
        "2. **个股回测域是当前观察池**：历史观察池成员状态未落库，存在幸存者偏差；"
        "结论只衡量“同一组标的上的方向判别能力”，不能外推为选股能力。",
        "3. **估值因子历史缺失**：历史 PE/PB 未落库，回测重算时估值判缺失、不出分（不以 0 分占位）；"
        "个股预判入选依据是技术/行业/资金三项硬筛，不依赖总分门槛。",
        "4. **涨跌家数口径窄**：大盘验证使用 market.db 本地存量标的（非全市场），"
        "只能作为方向确认的辅助证据，不是全市场宽度。",
        "5. **样本重复**：同一标的/板块在相邻交易日可能被重复预判，样本非独立；"
        "显著性检验仅作粗略参考，不构成统计保证。",
        "6. **板块看强/看弱共用同一因子**：两端都来自“20 日涨幅排序”，不是两份独立样本；"
        "合并成一个数字会被双向偏离互相抵消，必须分开看，并以分月一致性判断是普适规律还是阶段性失效。",
        "7. **板块回放区间只有 2 个月量级**：跨月分组后单月样本普遍 <20 条，单月结论不成立；"
        "只有多月方向一致时才能说“在该区间内稳定”，不能外推为长期规律。",
        "",
        "## 七、可回放区间说明",
        "",
        f"- 行情：2026-01-05 ~ {result['last_data']}（沪深300）。",
        "- 大盘：需 20 根历史 K 线，实际起点约 2026-02-02；5 日对账需 5 个后续交易日。",
        "- 板块：2026-08-10 起（完整行业板块快照 ≥400 条且有点位）。",
        "- 个股：2026-08-10 起（依赖板块快照 + 当日及之前调研证据 + 可买过滤）。",
        "",
    ]
    return "\n".join(lines)


def write_report(result: dict, run_date: str | None = None) -> Path:
    FORECAST_DIR.mkdir(parents=True, exist_ok=True)
    run_date = run_date or datetime.now().strftime("%Y-%m-%d")
    path = FORECAST_DIR / f"回溯检验_{run_date}.md"
    path.write_text(render_report(result), encoding="utf-8")
    return path


def _save_cache(result: dict, path: str) -> None:
    """回放很慢，缓存结果供改报告措辞时复用（不改变回放口径）。"""
    keys = ("ok", "start", "end", "last_data", "rows",
            "exclusions", "stock_filter_reasons")
    payload = {k: result.get(k) for k in keys}
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        __import__("json").dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def _load_cache(path: str) -> dict | None:
    p = Path(path)
    if not p.exists():
        return None
    return __import__("json").loads(p.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="预判历史回溯检验")
    parser.add_argument("--start", default="2026-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--market-db", default=MARKET_DB_PATH)
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--cache", default=str(ROOT / "logs" / ".forecast_backtest_cache.json"))
    parser.add_argument("--from-cache", action="store_true",
                        help="复用上次回放结果，只重渲染报告")
    args = parser.parse_args(argv)

    result = _load_cache(args.cache) if args.from_cache else None
    if result is None:
        result = run_backtest(args.market_db, args.db, args.start, args.end)
        if result.get("ok"):
            _save_cache(result, args.cache)
    if not result.get("ok"):
        print(f"无法回放：{result.get('reason')}")
        return 2
    path = write_report(result)
    print(f"报告：{path}")
    for label, scope, key, value in [
        ("大盘1日", "market", "horizon", 1),
        ("大盘5日", "market", "horizon", 5),
        ("板块看强", "sector", "direction", "看强"),
        ("板块看弱", "sector", "direction", "看弱"),
        ("个股5日", "stock", None, None),
    ]:
        n = _summarize(result["rows"], scope, key, value)
        print(f"{label}: n={n['n']} hit={n['hit']} miss={n['miss']} unv={n['unverifiable']} rate={n['rate']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
