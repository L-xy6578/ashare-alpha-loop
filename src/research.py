"""
三角闭环选股体系 — 调研情报弹药库（第一层）

背景：16 个 skill 中一半是"信息驱动型"（宏观/政策/游资/调查员），
通病是每轮现场搜索信息——慢、贵、防幻觉易翻车。
本模块把"半静态信息"（基本面/行业/资金/风险调研）结构化入库，
skill 每轮查库而非现场搜，信息有源可溯（满足 AH-08 证据要求）。

用法:
    from src.research import ResearchStore

    store = ResearchStore()
    store.add(symbol="sz002366", category="基本面", title="2026Q1净利+224%",
              content="...", source_url="https://stcn.com/article/detail/3822552.html")
    notes = store.query(symbol="sz002366")          # 查某标的全部情报
    latest = store.latest(symbol="sz002366", category="基本面")  # 最新一条
"""

from __future__ import annotations

from dataclasses import dataclass

from src.config import DB_PATH
from src.db import get_connection
from src.logger import get_logger

log = get_logger(__name__)

# 情报类别（对应五因子/调研维度）
CATEGORIES = ("基本面", "行业景气", "估值", "技术面", "资金面", "风险", "政策")

# 建表 SQL
_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,                -- 标的代码 sh600535
    name        TEXT,                         -- 标的名
    category    TEXT NOT NULL,                -- 类别：基本面/行业景气/估值/技术面/资金面/风险/政策
    title       TEXT NOT NULL,                -- 情报标题
    content     TEXT NOT NULL,                -- 情报内容（含关键数字）
    source_url  TEXT,                         -- 来源 URL（AH-08 证据要求）
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_research_symbol ON research_notes(symbol, category);
"""


@dataclass(frozen=True)
class ResearchNote:
    """一条调研情报"""

    symbol: str
    name: str
    category: str
    title: str
    content: str
    source_url: str | None = None
    created_at: str = ""


def init_research_db(db_path: str = DB_PATH) -> str:
    """建表（幂等），返回 db 路径"""
    with get_connection(db_path) as conn:
        conn.executescript(_SCHEMA)
    log.info("research_notes 表就绪: %s", db_path)
    return db_path


class ResearchStore:
    """调研情报弹药库"""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        init_research_db(db_path)

    def add(
        self,
        symbol: str,
        name: str,
        category: str,
        title: str,
        content: str,
        source_url: str | None = None,
        created_at: str | None = None,
    ) -> int:
        """写入一条调研情报，返回 id"""
        if category not in CATEGORIES:
            raise ValueError(f"非法类别 {category!r}，可选: {CATEGORIES}")
        from datetime import datetime

        created_at = created_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with get_connection(self.db_path) as conn:
            cur = conn.execute(
                """INSERT INTO research_notes
                   (symbol, name, category, title, content, source_url, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (symbol, name, category, title, content, source_url, created_at),
            )
            note_id = cur.lastrowid
        log.info("调研情报入库 #%d %s[%s]: %s", note_id, name, category, title)
        return note_id

    def query(
        self,
        symbol: str | None = None,
        category: str | None = None,
        limit: int = 50,
    ) -> list[ResearchNote]:
        """查询调研情报（可按标的/类别过滤，按时间倒序）"""
        sql = "SELECT symbol, name, category, title, content, source_url, created_at FROM research_notes"
        conds, args = [], []
        if symbol:
            conds.append("symbol = ?")
            args.append(symbol)
        if category:
            conds.append("category = ?")
            args.append(category)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with get_connection(self.db_path, readonly=True) as conn:
            rows = conn.execute(sql, args).fetchall()
        return [ResearchNote(*r) for r in rows]

    def latest(self, symbol: str, category: str | None = None) -> ResearchNote | None:
        """某标的最新一条情报（按类别可选）"""
        notes = self.query(symbol=symbol, category=category, limit=1)
        return notes[0] if notes else None

    def count(self) -> int:
        with get_connection(self.db_path, readonly=True) as conn:
            return conn.execute("SELECT COUNT(*) FROM research_notes").fetchone()[0]


