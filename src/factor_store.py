"""
每日因子快照（D 方案）— 五因子评分落库，用于跨日回溯与稳定性监控

背景（2026-10-30 评分稳定性诊断）：旧链路只把评分写进当日 Markdown 报告，
隔天重跑就覆盖，无法回答"同一只票昨天多少分、今天多少分、为什么变"。
本表每天闭环写入一行/标的，同日覆盖（幂等），历史分数不改。

用法:
    from src.factor_store import FactorSnapshotStore

    store = FactorSnapshotStore()            # 默认 market.db
    store.save_many("2026-10-30", rows)      # 同日覆盖（幂等）
    rows = store.load("2026-10-30")
"""

from __future__ import annotations

from src.config import MARKET_DB_PATH
from src.db import get_connection
from src.logger import get_logger
from src.scoring import WEIGHTS

log = get_logger(__name__)

FACTOR_TABLE = """
CREATE TABLE IF NOT EXISTS factor_snapshot (
    date TEXT NOT NULL,              -- 评分日（交易日）
    symbol TEXT NOT NULL,            -- sh600019 / sz000858
    industry REAL,                   -- 行业景气（缺失记 NULL）
    growth REAL,
    valuation REAL,
    technical REAL,
    capital REAL,
    score REAL,                      -- 加权总分
    missing TEXT NOT NULL DEFAULT '',-- 缺失因子清单（"、"分隔），缺失按 0 分计入加权
    source TEXT NOT NULL DEFAULT '', -- 当日板块快照数据源
    resolved_board TEXT NOT NULL DEFAULT '',  -- 解析到的板块名（未命中为空）
    benchmark TEXT NOT NULL DEFAULT '',       -- 分位基准描述（样本量）
    growth_raw REAL,                 -- 业绩原始值：归母净利同比 %（东财 SJLTZ），复算依据
    growth_profitable INTEGER,       -- 业绩原始值：归母金额是否为正（1/0/NULL 未给金额）
    growth_source TEXT NOT NULL DEFAULT '',   -- 业绩源（eastmoney）
    PRIMARY KEY (date, symbol)
)
"""

# 旧库补列（新加字段统一在此登记，缺失即 ALTER TABLE）
_EXTRA_COLUMNS = {
    "growth_raw": "REAL",
    "growth_profitable": "INTEGER",
    "growth_source": "TEXT NOT NULL DEFAULT ''",
}

# 因子列 → score_candidate.missing 里的中文名（复算时按缺失规则置 0）
MISSING_FACTOR_MAP = {
    "industry": "行业景气",
    "growth": "业绩证据",
    "valuation": "估值",
    "technical": "技术面",
    "capital": "资金面",
}


def init_factor_db(db_path: str = MARKET_DB_PATH) -> str:
    with get_connection(db_path) as conn:
        conn.execute(FACTOR_TABLE)
        # 旧库补列：missing 显式记录"无数据"，与因子列 NULL 互为印证
        cols = {r[1] for r in conn.execute("PRAGMA table_info(factor_snapshot)")}
        if "missing" not in cols:
            conn.execute("ALTER TABLE factor_snapshot "
                         "ADD COLUMN missing TEXT NOT NULL DEFAULT ''")
        for name, ddl in _EXTRA_COLUMNS.items():
            if name not in cols:
                conn.execute(f"ALTER TABLE factor_snapshot ADD COLUMN {name} {ddl}")
    return db_path


class FactorSnapshotStore:
    """每日因子快照读写"""

    def __init__(self, db_path: str = MARKET_DB_PATH):
        self.db_path = db_path
        init_factor_db(db_path)

    def save_many(self, trade_date: str, rows: list[dict]) -> int:
        """批量写入（同日同标的主键覆盖，幂等）"""
        if not rows:
            return 0
        with get_connection(self.db_path) as conn:
            for r in rows:
                conn.execute(
                    """INSERT INTO factor_snapshot
                       (date, symbol, industry, growth, valuation, technical,
                        capital, score, missing, source, resolved_board, benchmark,
                        growth_raw, growth_profitable, growth_source)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(date, symbol) DO UPDATE SET
                         industry=excluded.industry, growth=excluded.growth,
                         valuation=excluded.valuation, technical=excluded.technical,
                         capital=excluded.capital, score=excluded.score,
                         missing=excluded.missing,
                         source=excluded.source,
                         resolved_board=excluded.resolved_board,
                         benchmark=excluded.benchmark,
                         growth_raw=excluded.growth_raw,
                         growth_profitable=excluded.growth_profitable,
                         growth_source=excluded.growth_source""",
                    (trade_date, r.get("symbol"),
                     _none(r.get("industry")), _none(r.get("growth")),
                     _none(r.get("valuation")), _none(r.get("technical")),
                     _none(r.get("capital")), _none(r.get("score")),
                     _missing_text(r.get("missing")),
                     r.get("source") or "", r.get("resolved_board") or "",
                     r.get("benchmark") or "",
                     _none(r.get("growth_raw")), _flag(r.get("growth_profitable")),
                     r.get("growth_source") or ""),
                )
        log.info("因子快照落库 %d 条（%s）", len(rows), trade_date)
        return len(rows)

    def load(self, trade_date: str) -> list[dict]:
        with get_connection(self.db_path, readonly=True) as conn:
            rows = conn.execute(
                "SELECT * FROM factor_snapshot WHERE date=? ORDER BY symbol",
                (trade_date,),
            ).fetchall()
        return [dict(r) for r in rows]

    def verify(self, trade_date: str) -> list[tuple[str, float | None, float]]:
        """可复现性自检：按落库字段复算每日分数，返回对不上的 (symbol, 落库分, 复算分)"""
        bad = []
        for r in self.load(trade_date):
            got = recompute_score(r)
            stored = r.get("score")
            if stored is None or abs(got - float(stored)) > 0.05:
                bad.append((r["symbol"], stored, got))
        return bad


def recompute_score(row: dict) -> float:
    """用落库的同一份字段复算加权分

    缺失因子（列为 NULL 或列入 missing）按 0 分计入 —— 与 score_candidate 的
    "缺数据不出分/按 0 占位"实现一致，保证留档可独立复现当日分数。
    业绩因子在落档了原始值（growth_raw，东财归母净利同比）时，用原始值重新过桶，
    而不是直接信任落库分 —— 这样换桶表/换实现后能立刻暴露"分数不可复算"。
    """
    missing = {x for x in (row.get("missing") or "").split("、") if x}
    total = 0.0
    for key, weight in WEIGHTS.items():
        if key == "growth":
            v = _recompute_growth(row, missing)
        else:
            v = row.get(key)
            if v is None or MISSING_FACTOR_MAP[key] in missing:
                v = 0.0
        total += float(v) * weight
    return round(total, 1)


def _recompute_growth(row: dict, missing: set[str]) -> float:
    """业绩因子复算：优先用落档原始值（growth_raw）重新过桶"""
    if MISSING_FACTOR_MAP["growth"] in missing:
        return 0.0
    raw = row.get("growth_raw")
    if raw is not None:
        from src.factors import growth_score_from_rate

        flag = row.get("growth_profitable")
        profitable = None if flag is None else bool(int(flag))
        return growth_score_from_rate(float(raw), profitable=profitable)
    v = row.get("growth")
    return 0.0 if v is None else float(v)


def _none(v):
    """缺失因子统一落 NULL（0 与缺失在回溯时必须可区分）"""
    return None if v is None else float(v)


def _flag(v):
    """布尔落库为 1/0；None（未给金额）保持 NULL，与"亏损"可区分"""
    return None if v is None else int(bool(v))


def _missing_text(v) -> str:
    """缺失清单落库：接受 list/tuple 或已拼接字符串"""
    if not v:
        return ""
    if isinstance(v, str):
        return v
    return "、".join(str(x) for x in v)
