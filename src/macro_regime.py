"""
宏观仓位提示（只读观察层 · 不参与任何决策）

背景：总仓上限硬编码 18,000 元（`src/executable.py:31`），不随市场环境变化；
本模块实现「宏观评分 → 市场阶段 → 仓位上限」映射。

本模块只做"多看一层、写进报告"：
- 按 SKILL.md 2.2 的权重与打分规则算宏观综合评分（0-100），映射到阶段与**相对仓位系数**
- 系数只乘在既有 18,000 元硬上限之内，**不覆盖** 18,000（100% 档位照原文登记但封顶为 18,000）

六个指标的可得性（实测，2026-11-10）：

| 指标 | 权重 | 可得性 | 数据源 |
|---|---|---|---|
| PMI（制造业） | 20 | ✅ | 东财 RPT_ECONOMY_PMI（MAKE_INDEX） |
| 社融存量增速 | 20 | ❌ **不可得** | 东财无该报表（12 个候选 reportName 均「报表配置不存在」），新浪宏观 fininfo 分类亦无社融表 |
| CPI 同比 | 15 | ✅ | 东财 RPT_ECONOMY_CPI（NATIONAL_SAME） |
| PPI 同比 | 10 | ✅ | 东财 RPT_ECONOMY_PPI（BASE_SAME） |
| M2 同比 | 20 | ✅ | 东财 RPT_ECONOMY_CURRENCY_SUPPLY（FREE_CASH_SAME） |
| LPR 1Y | 15 | ✅ | 东财 RPTA_WEB_RATE（LPR1Y，月度） |

因此 **六指标完整评分算不出来**：社融缺失，只能按可得五项（权重合计 80）计算并标注「不完整」。
不估算、不用别的指标顶替社融。

SKILL.md 打分规则原文有两处区间空档（CPI 0~2% 与 3~4%、PPI 3~5%），
本模块取相邻档的保守端点（10 分）并在报表中列明，不擅自发明新档。

用法:
    python -m src.macro_regime --date 2026-11-10          # 打印（不写库）
    python -m src.macro_regime --date 2026-11-10 --save    # 落库 + 报表
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
import urllib.parse
from pathlib import Path

from src.config import MARKET_DB_PATH
from src.logger import get_logger
from src.net import fetch_json

log = get_logger(__name__)

DC_API = "https://datacenter-web.eastmoney.com/api/data/v1/get"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}
SOURCE = "eastmoney:RPT_ECONOMY_PMI/CPI/PPI/CURRENCY_SUPPLY+RPTA_WEB_RATE"

TOTAL_CAP = 18000        # 既有可执行性硬上限（不改，只作为系数映射基数）
BENCH = "sh000300"       # 沪深300 作为检验基准

# SKILL.md 2.2 权重（照原文）
WEIGHTS = {"pmi": 20, "sfe": 20, "cpi": 15, "ppi": 10, "m2": 20, "lpr": 15}

# 评分 → 阶段 → 相对仓位系数（SKILL.md 2.2 五档；100% 档在映射时封顶到 18,000）
LEVELS = (
    (80, "进攻", 1.0),
    (65, "乐观", 0.8),
    (50, "中性", 0.5),
    (35, "谨慎", 0.3),
    (0, "防御", 0.1),
)
LEVEL_MISSING = "数据不足"


def _num(x):
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _fetch_series(report: str, date_col: str, *, timeout: int = 12,
                  attempts: int = 2, n: int = 4) -> list[dict]:
    url = (f"{DC_API}?reportName={report}&columns=ALL&pageSize={n}&pageNumber=1"
           f"&sortColumns={date_col}&sortTypes=-1&source=WEB&client=WEB")
    js = fetch_json(url, headers=HEADERS, timeout=timeout, attempts=attempts)
    return (js.get("result") or {}).get("data") or []


def fetch_pmi(asof: str) -> dict:
    rows = [r for r in _fetch_series("RPT_ECONOMY_PMI", "REPORT_DATE")
            if str(r.get("REPORT_DATE") or "")[:10] <= asof]
    if not rows:
        return {}
    return {"value": _num(rows[0].get("MAKE_INDEX")),
            "date": str(rows[0].get("REPORT_DATE"))[:10],
            "prev": _num(rows[1].get("MAKE_INDEX")) if len(rows) > 1 else None}


def fetch_cpi(asof: str) -> dict:
    rows = [r for r in _fetch_series("RPT_ECONOMY_CPI", "REPORT_DATE")
            if str(r.get("REPORT_DATE") or "")[:10] <= asof]
    if not rows:
        return {}
    return {"value": _num(rows[0].get("NATIONAL_SAME")),
            "date": str(rows[0].get("REPORT_DATE"))[:10],
            "prev": _num(rows[1].get("NATIONAL_SAME")) if len(rows) > 1 else None}


def fetch_ppi(asof: str) -> dict:
    rows = [r for r in _fetch_series("RPT_ECONOMY_PPI", "REPORT_DATE")
            if str(r.get("REPORT_DATE") or "")[:10] <= asof]
    if not rows:
        return {}
    return {"value": _num(rows[0].get("BASE_SAME")),
            "date": str(rows[0].get("REPORT_DATE"))[:10],
            "prev": _num(rows[1].get("BASE_SAME")) if len(rows) > 1 else None}


def fetch_m2(asof: str) -> dict:
    rows = [r for r in _fetch_series("RPT_ECONOMY_CURRENCY_SUPPLY", "REPORT_DATE")
            if str(r.get("REPORT_DATE") or "")[:10] <= asof]
    if not rows:
        return {}
    return {"value": _num(rows[0].get("FREE_CASH_SAME")),
            "date": str(rows[0].get("REPORT_DATE"))[:10],
            "prev": _num(rows[1].get("FREE_CASH_SAME")) if len(rows) > 1 else None}


def fetch_lpr(asof: str) -> dict:
    rows = [r for r in _fetch_series("RPTA_WEB_RATE", "TRADE_DATE")
            if str(r.get("TRADE_DATE") or "")[:10] <= asof]
    if not rows:
        return {}
    cur = _num(rows[0].get("LPR1Y"))
    prev = _num(rows[1].get("LPR1Y")) if len(rows) > 1 else None
    trend = "unknown"
    if cur is not None and prev is not None:
        trend = "down" if cur < prev else ("up" if cur > prev else "flat")
    return {"value": cur, "date": str(rows[0].get("TRADE_DATE"))[:10],
            "prev": prev, "trend": trend}


def fetch_bundle(asof: str) -> dict:
    """五项可得指标（社融无数据源，固定为空）"""
    return {
        "pmi": fetch_pmi(asof),
        "cpi": fetch_cpi(asof),
        "ppi": fetch_ppi(asof),
        "m2": fetch_m2(asof),
        "lpr": fetch_lpr(asof),
    }


# —— SKILL.md 2.2 打分规则（每项 0-20；区间内线性插值，档间跳跃照原文保留） ——
def score_pmi(v: float) -> float:
    if v >= 51:
        return _clamp(18 + (v - 51) * 2, 18, 20)      # >51=18-20
    if v >= 50:
        return 15 + (v - 50) * 3                       # >50=15-18
    if v >= 48:
        return 8 + (v - 48) * 2                        # 48-50=8-12
    return _clamp(7 - (48 - v) * 1.5, 0, 7)            # <48=0-7


def score_cpi(v: float) -> float:
    if v >= 4:
        return _clamp(10 - (v - 4) * 2.5, 5, 10)      # >4%=5-10（过热）
    if v >= 3:
        return 10.0                                    # 3-4% 原文空档 → 保守取 10
    if v >= 2:
        return 15 + (v - 2) * 3                        # 2-3%=15-18（最优）
    if v >= 0:
        return 10.0                                    # 0-2% 原文空档 → 保守取 10
    return _clamp(10 + v * 1.67, 5, 10)                # <0%=5-10（通缩）


def score_ppi(v: float) -> float:
    if v >= 5:
        return _clamp(10 - (v - 5) * 1.67, 5, 10)     # >5%=5-10
    if v >= 3:
        return 10.0                                    # 3-5% 原文空档 → 保守取 10
    if v >= 0:
        return 12 + v                                  # 0-3%=12-15
    return _clamp(10 + v * 1.67, 5, 10)               # <0%=5-10


def score_m2(v: float) -> float:
    if v > 10:
        return _clamp(15 + (v - 10) * 0.6, 15, 18)     # >10%=15-18
    if v >= 8:
        return 10 + (v - 8) * 2                        # 8-10%=10-14
    return _clamp(9 - (8 - v) * 1.33, 5, 9)            # <8%=5-9


def score_lpr(trend: str) -> float:
    return {"down": 16.5, "flat": 12.0, "up": 7.0}.get(trend, 12.0)   # 档内取中值


def level_of(score: float) -> tuple[str, float]:
    for threshold, name, ratio in LEVELS:
        if score >= threshold:
            return name, ratio
    return LEVELS[-1][1], LEVELS[-1][2]


def assess(bundle: dict, asof: str) -> dict:
    """五项可得指标 → 宏观评分 → 阶段 → 相对仓位系数 → 建议上限（封顶 18,000）"""
    missing = ["社融（全部数据源均无此表）"]
    scores: dict[str, float] = {}

    pmi, cpi, ppi, m2, lpr = (bundle.get(k) or {} for k in ("pmi", "cpi", "ppi", "m2", "lpr"))
    if pmi.get("value") is not None:
        scores["pmi"] = round(score_pmi(pmi["value"]), 2)
    else:
        missing.append("PMI")
    if cpi.get("value") is not None:
        scores["cpi"] = round(score_cpi(cpi["value"]), 2)
    else:
        missing.append("CPI")
    if ppi.get("value") is not None:
        scores["ppi"] = round(score_ppi(ppi["value"]), 2)
    else:
        missing.append("PPI")
    if m2.get("value") is not None:
        scores["m2"] = round(score_m2(m2["value"]), 2)
    else:
        missing.append("M2")
    if lpr.get("value") is not None:
        scores["lpr"] = round(score_lpr(lpr.get("trend", "unknown")), 2)
    else:
        missing.append("LPR")

    row = {
        "date": asof,
        "pmi": pmi.get("value"), "pmi_date": pmi.get("date"), "pmi_score": scores.get("pmi"),
        "pmi_prev": pmi.get("prev"),
        "cpi": cpi.get("value"), "cpi_date": cpi.get("date"), "cpi_score": scores.get("cpi"),
        "cpi_prev": cpi.get("prev"),
        "ppi": ppi.get("value"), "ppi_date": ppi.get("date"), "ppi_score": scores.get("ppi"),
        "ppi_prev": ppi.get("prev"),
        "m2": m2.get("value"), "m2_date": m2.get("date"), "m2_score": scores.get("m2"),
        "m2_prev": m2.get("prev"),
        "lpr": lpr.get("value"), "lpr_date": lpr.get("date"),
        "lpr_prev": lpr.get("prev"),
        "lpr_trend": lpr.get("trend"), "lpr_score": scores.get("lpr"),
        "sfe": None, "sfe_date": None,
        "weight_covered": sum(WEIGHTS[k] for k in scores),
        "score": None, "level": LEVEL_MISSING, "cap_ratio": None, "suggest_cap": None,
        "missing": missing, "note": "社融缺失，按可得五项（权重80）折算，评分不完整",
        "bench_symbol": BENCH, "bench_close": None,
        "ret_5d": None, "ret_10d": None, "ret_20d": None,
    }
    # 缺失超过 3 项降级（SKILL.md 2.1）；社融恒缺 1 项，故另有 3 项失败才降级
    if len(missing) >= 4 or not scores:
        row["note"] = f"缺失 {len(missing)} 项（含社融），按 SKILL.md 2.1 降级为数据不足"
        return row

    total_w = sum(WEIGHTS[k] for k in scores)
    raw = sum(scores[k] * WEIGHTS[k] for k in scores)          # 满分 20×total_w
    score = round(raw / (total_w / 5), 1)                      # 折回 100 分制
    level, ratio = level_of(score)
    row.update({"score": score, "level": level, "cap_ratio": ratio,
                "suggest_cap": int(round(TOTAL_CAP * ratio))})
    return row


_DDL = """
CREATE TABLE IF NOT EXISTS macro_regime (
    date           TEXT PRIMARY KEY,  -- 判定日
    pmi            REAL, pmi_date      TEXT, pmi_score REAL,
    cpi            REAL, cpi_date      TEXT, cpi_score REAL,
    ppi            REAL, ppi_date      TEXT, ppi_score REAL,
    m2             REAL, m2_date       TEXT, m2_score  REAL,
    lpr            REAL, lpr_date      TEXT, lpr_trend TEXT, lpr_score REAL,
    sfe            REAL, sfe_date      TEXT,          -- 社融：无数据源，恒 NULL
    weight_covered INTEGER,           -- 实际覆盖权重（满分 100）
    score          REAL,              -- 宏观综合评分（0-100，折算后）
    level          TEXT,              -- 进攻/乐观/中性/谨慎/防御/数据不足
    cap_ratio      REAL,              -- 相对仓位系数
    suggest_cap    INTEGER,           -- 18000 × cap_ratio（仅提示）
    missing        TEXT, note TEXT,
    bench_symbol   TEXT, bench_close REAL,
    ret_5d         REAL, ret_10d REAL, ret_20d REAL,   -- 后续大盘涨跌（回填）
    source         TEXT, fetched_at TEXT
)
"""

_COLS = ["date", "pmi", "pmi_date", "pmi_score", "cpi", "cpi_date", "cpi_score",
         "ppi", "ppi_date", "ppi_score", "m2", "m2_date", "m2_score",
         "lpr", "lpr_date", "lpr_trend", "lpr_score", "sfe", "sfe_date",
         "weight_covered", "score", "level", "cap_ratio", "suggest_cap",
         "missing", "note", "bench_symbol", "bench_close"]


def save_rows(rows: list[dict], *, db_path: str | None = None,
              fetched_at: str | None = None) -> int:
    path = db_path or MARKET_DB_PATH
    ts = fetched_at or time.strftime("%Y-%m-%d %H:%M:%S")
    cols = [*_COLS, "source", "fetched_at"]
    holders = ",".join(["?"] * len(cols))
    with sqlite3.connect(path) as conn:
        conn.execute(_DDL)
        for r in rows:
            vals = [r.get(c) for c in _COLS]
            vals[_COLS.index("missing")] = "|".join(r.get("missing") or [])
            conn.execute(
                f"INSERT OR REPLACE INTO macro_regime ({','.join(cols)}) "
                f"VALUES ({holders})",
                [*vals, SOURCE, ts])
        conn.commit()
    return len(rows)


def backfill_returns(db_path: str | None = None, bench: str = BENCH) -> int:
    """回填 5/10/20 交易日后的基准涨跌（只补算得出来的行，算不出来保持 NULL）"""
    path = db_path or MARKET_DB_PATH
    with sqlite3.connect(path) as conn:
        conn.execute(_DDL)
        try:
            bars = conn.execute(
                "SELECT date, close FROM daily_bars WHERE symbol=? ORDER BY date", (bench,)
            ).fetchall()
        except sqlite3.Error:
            return 0    # 空库/无 daily_bars：回填跳过，不抛异常拖垮闭环
        if not bars:
            return 0
        closes = {d: c for d, c in bars}
        idx = {d: i for i, (d, _) in enumerate(bars)}
        filled = 0
        for (d,) in conn.execute("SELECT date FROM macro_regime ORDER BY date").fetchall():
            if d not in idx:
                continue
            i = idx[d]
            updates = {"bench_close": closes[d]}
            for horizon in (5, 10, 20):
                if i + horizon < len(bars):
                    base, later = bars[i][1], bars[i + horizon][1]
                    updates[f"ret_{horizon}d"] = round(later / base - 1, 4) if base else None
            conn.execute(
                f"UPDATE macro_regime SET {', '.join(f'{k}=?' for k in updates)} WHERE date=?",
                [*updates.values(), d])
            filled += 1
        conn.commit()
    return filled


def _fmt(v, suffix="", nd=2):
    if v is None or v == "":
        return "—"
    if isinstance(v, float):
        return f"{v:.{nd}f}{suffix}"
    return f"{v}{suffix}"


def _trend(a, b):
    if a is None or b is None:
        return "—"
    return "↑" if a > b else ("↓" if a < b else "→")


def render_markdown(asof: str, rows: list[dict], fetched_at: str | None = None,
                    history: list[dict] | None = None) -> str:
    r = rows[0] if rows else {}
    score, level = r.get("score"), r.get("level", LEVEL_MISSING)
    lines = [
        f"# 宏观仓位提示（{asof}）",
        "",
        "> **仅提示，不影响决策**：本表不修改评分/门槛/筛选/参数，不参与 ABSTAIN/ENTRY 判定；"
        f"实际下单仍执行既有硬上限 **{TOTAL_CAP:,} 元**（`src/executable.py:31`，本次未改）。",
        "> 口径：宏观综合评分 → 五档阶段 → 相对仓位系数；"
        f"建议上限 = {TOTAL_CAP:,} × 系数（100% 档同样封顶 {TOTAL_CAP:,}，不放大）。",
        f"> 生成时间：{fetched_at or time.strftime('%Y-%m-%d %H:%M:%S')}；数据源：{SOURCE}",
        "",
        "## 一、宏观评分 → 阶段",
        "",
    ]
    if score is None:
        lines += [f"**数据不足**：{r.get('note', '')}", ""]
    else:
        lines += [
            f"**宏观综合评分 {score}/100（覆盖权重 {r.get('weight_covered')}/100，"
            f"不含社融）→ {level}**",
            "",
            f"- 相对仓位系数：**{r.get('cap_ratio')}**",
            f"- 建议仓位上限：**{int(r.get('suggest_cap') or 0):,} 元**"
            f"（= {TOTAL_CAP:,} × {r.get('cap_ratio')}，仅提示；硬上限仍是 {TOTAL_CAP:,} 元）",
            "",
        ]

    lines += [
        "## 二、指标仪表盘",
        "",
        "| 指标 | 当前值 | 数据所属期 | 前值 | 方向 | 得分 | 权重 |",
        "|------|:---:|:---:|:---:|:---:|:---:|:---:|",
    ]
    spec = [
        ("PMI（制造业）", "pmi", "", "MAKE_INDEX"),
        ("社融存量增速", "sfe", "%", "无数据源"),
        ("CPI 同比", "cpi", "%", "NATIONAL_SAME"),
        ("PPI 同比", "ppi", "%", "BASE_SAME"),
        ("M2 同比", "m2", "%", "FREE_CASH_SAME"),
        ("LPR 1Y", "lpr", "%", "LPR1Y（趋势打分）"),
    ]
    for label, key, suffix, src in spec:
        val = r.get(key)
        d = r.get(f"{key}_date")
        prev = r.get(f"{key}_prev")
        valid = "—" if val is None else f"{val:.2f}{suffix}"
        prev_txt = "—" if prev is None else f"{prev:.2f}{suffix}"
        trend = r.get("lpr_trend") if key == "lpr" else _trend(val, prev)
        trend_cn = {"down": "↓", "up": "↑", "flat": "→"}.get(trend, trend or "—")
        lines.append(
            f"| {label} | {valid} | {d or '—'} | {prev_txt} | {trend_cn} | "
            f"{_fmt(r.get(f'{key}_score'))} | {WEIGHTS[key]} |")

    lines += ["", "## 三、数据来源与缺失说明", ""]
    lines += [
        "- 数据源：东方财富数据中心（`datacenter-web.eastmoney.com`）"
        "`RPT_ECONOMY_PMI` / `RPT_ECONOMY_CPI` / `RPT_ECONOMY_PPI` / "
        "`RPT_ECONOMY_CURRENCY_SUPPLY` / `RPTA_WEB_RATE`，均为**月度**数据。",
        "- **社融存量增速不可得**：东财无对应报表（RPT_ECONOMY_SOCIAL_FINANCE 等 12 个候选名均返回"
        "「报表配置不存在」），新浪宏观 fininfo 分类亦无社融表。**未用其他指标顶替、未估算**。",
        f"- 因此本次评分**不是六指标完整评分**：仅覆盖权重 {r.get('weight_covered', 0)}/100"
        "（PMI20+CPI15+PPI10+M2 20+LPR15），已按可得权重折算回 100 分制。",
        "- SKILL.md 打分规则原文存在区间空档（CPI 0~2% 与 3~4%、PPI 3~5%），"
        "本表按相邻档保守端点（10 分）处理，未新造档位。",
        "- 频率提醒：六项均为月度指标，月度内数值不变，档位通常每 10~30 天变化一次；"
        "用它做**日频**仓位调节只有滞后意义，仅供 20 日后检验其预测力。",
    ]
    if r.get("missing"):
        lines.append(f"- 本次缺失项：{'、'.join(r['missing'])}。")

    if history:
        lines += ["", "## 四、历史档位与后续大盘（检验用）", "",
                  f"基准：{BENCH}（沪深300），收益为后 N 个交易日涨跌", "",
                  "| 日期 | 评分 | 档位 | 系数 | 建议上限 | 基准收盘 | 5日 | 10日 | 20日 |",
                  "|------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|"]
        for h in history[-20:]:
            def pct(v):
                return "—" if v is None else f"{v * 100:+.2f}%"

            cap = h.get("suggest_cap")
            lines.append(
                f"| {h['date']} | {_fmt(h.get('score'), nd=1)} | {h.get('level', '—')} | "
                f"{_fmt(h.get('cap_ratio'))} | "
                f"{'—' if cap is None else f'{int(cap):,}'} | "
                f"{_fmt(h.get('bench_close'))} | {pct(h.get('ret_5d'))} | "
                f"{pct(h.get('ret_10d'))} | {pct(h.get('ret_20d'))} |")
    return "\n".join(lines) + "\n"


def load_history(db_path: str | None = None, limit: int = 20) -> list[dict]:
    path = db_path or MARKET_DB_PATH
    try:
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(
                "SELECT * FROM macro_regime ORDER BY date DESC LIMIT ?", (limit,))]
    except sqlite3.Error:
        return []


def write_report(asof: str, rows: list[dict], out_dir: Path | None = None,
                 fetched_at: str | None = None, history: list[dict] | None = None) -> Path:
    out = out_dir or (Path("outputs") / "收盘报表")
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"宏观仓位提示_{asof}.md"
    path.write_text(render_markdown(asof, rows, fetched_at, history), encoding="utf-8")
    return path


def run(asof: str, *, is_dry: bool = False, warnings: list[str] | None = None,
        provider=None, market_db: str | None = None,
        out_dir: Path | None = None) -> dict:
    provider = provider or fetch_bundle
    row = assess(provider(asof), asof)
    if is_dry:
        return {"rows": [row], "written": 0, "path": None}
    save_rows([row], db_path=market_db)
    backfill_returns(market_db)
    history = load_history(market_db, limit=30)
    path = write_report(asof, [row], out_dir=out_dir, history=history)
    return {"rows": [row], "written": 1, "path": path}


def _main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    from src.dates import last_trading_day

    parser = argparse.ArgumentParser(description="宏观仓位提示（只读观察层）")
    parser.add_argument("--date", default=None, help="判定日（默认最近交易日）")
    parser.add_argument("--save", action="store_true", help="落库并写报表")
    args = parser.parse_args()
    asof = args.date or last_trading_day(__import__("datetime").date.today().isoformat())
    res = run(asof, is_dry=not args.save)
    print(render_markdown(asof, res["rows"],
                          history=load_history(limit=30) if args.save else None))
    if args.save:
        print(f"已落库；报表 {res['path']}")
    else:
        print("（未落库；--save 落库并写报表）")


if __name__ == "__main__":
    _main()
