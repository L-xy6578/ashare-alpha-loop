"""
三角闭环选股体系 — 对题本 & 错题本 数据库模型

用法:
    from src.knowledge import init_db, CorrectBook, MistakeBook

    # 建库（首次）
    init_db("../data/knowledge.db")

    # 写入
    CorrectBook.create(db_path, stock_code="600519", stock_name="贵州茅台", ...)

    # 查询
    CorrectBook.recent(db_path, limit=10)
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from ..db import get_connection
from ..logger import get_logger

log = get_logger(__name__)


# ============================================================
# 对题本 — 记录"我们做对了什么"
# ============================================================

@dataclass
class CorrectBook:
    """一条正确的选股/交易记录"""

    stock_code: str            # 股票代码，如 "600519"
    stock_name: str            # 股票名称，如 "贵州茅台"
    entry_price: float         # 买入价
    exit_price: float          # 卖出价
    profit_pct: float          # 收益率（%），如 12.5 表示 +12.5%
    buy_reason: str            # 买入理由（市场调查员的判断依据）
    sell_reason: str           # 卖出理由
    strategy_tag: str          # 策略标签（如 "三筛突破"、"主线龙头回调"）
    round_number: str          # 轮次编号（如 "R43"），关联到具体分析轮次
    holding_days: int = 0      # 持仓天数
    source: str = "sim"        # 口径：sim=模拟盘推演 / real=真实成交（KPI 只认 real）
    created_at: str = ""       # 记录时间，留空则自动填当前时间
    id: int = 0                # 自增主键，插入后回填

    DDL = """
    CREATE TABLE IF NOT EXISTS correct_book (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        stock_code    TEXT    NOT NULL,
        stock_name    TEXT    NOT NULL,
        entry_price   REAL    NOT NULL,
        exit_price    REAL    NOT NULL,
        profit_pct    REAL    NOT NULL,
        buy_reason    TEXT    NOT NULL,
        sell_reason   TEXT    NOT NULL,
        strategy_tag  TEXT    NOT NULL,
        round_number  TEXT    NOT NULL,
        holding_days  INTEGER DEFAULT 0,
        source        TEXT    NOT NULL DEFAULT 'sim',
        created_at    TEXT    NOT NULL
    );
    """

    @classmethod
    def create(
        cls,
        db_path: str,
        *,
        stock_code: str,
        stock_name: str,
        entry_price: float,
        exit_price: float,
        profit_pct: float,
        buy_reason: str,
        sell_reason: str,
        strategy_tag: str,
        round_number: str,
        holding_days: int = 0,
        source: str = "sim",
        created_at: str = "",
    ) -> "CorrectBook":
        """插入一条正确记录"""
        if not created_at:
            created_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        record = cls(
            stock_code=stock_code, stock_name=stock_name,
            entry_price=entry_price, exit_price=exit_price,
            profit_pct=profit_pct, buy_reason=buy_reason,
            sell_reason=sell_reason, strategy_tag=strategy_tag,
            round_number=round_number, holding_days=holding_days,
            source=source,
            created_at=created_at,
        )
        with get_connection(db_path) as conn:
            conn.execute(
                """INSERT INTO correct_book
                   (stock_code, stock_name, entry_price, exit_price,
                    profit_pct, buy_reason, sell_reason, strategy_tag,
                    round_number, holding_days, source, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (record.stock_code, record.stock_name, record.entry_price,
                 record.exit_price, record.profit_pct, record.buy_reason,
                 record.sell_reason, record.strategy_tag, record.round_number,
                 record.holding_days, record.source, record.created_at),
            )
            record.id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        log.info("写入对题本 #%s %s(%s) 收益+%s%% [轮次 %s]",
                 record.id, record.stock_name, record.stock_code,
                 record.profit_pct, record.round_number)
        return record

    @staticmethod
    def recent(db_path: str, limit: int = 10) -> list[dict]:
        """最近 N 条正确记录"""
        with get_connection(db_path, readonly=True) as conn:
            rows = conn.execute(
                "SELECT * FROM correct_book ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    @staticmethod
    def stats(db_path: str, days: int = 90) -> dict:
        """统计：近 N 天的胜率、平均收益、最佳策略"""
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        with get_connection(db_path, readonly=True) as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM correct_book WHERE created_at >= ?",
                (cutoff,),
            ).fetchone()[0]
            avg_profit = conn.execute(
                "SELECT AVG(profit_pct) FROM correct_book WHERE created_at >= ?",
                (cutoff,),
            ).fetchone()[0] or 0
            best_tag = conn.execute(
                """SELECT strategy_tag, COUNT(*) as cnt, AVG(profit_pct) as avg
                   FROM correct_book WHERE created_at >= ?
                   GROUP BY strategy_tag ORDER BY cnt DESC LIMIT 3""",
                (cutoff,),
            ).fetchall()
        return {
            "total_correct": total,
            "avg_profit_pct": round(avg_profit, 2),
            "top_strategies": [
                {"tag": row[0], "count": row[1], "avg_return": round(row[2] or 0, 2)}
                for row in best_tag
            ],
        }


# ============================================================
# 错题本 — 记录"我们哪里栽了跟头"
# ============================================================

@dataclass
class MistakeBook:
    """一条失败教训"""

    stock_code: str
    stock_name: str
    entry_price: float
    exit_price: float
    loss_pct: float            # 亏损率（%）
    buy_reason: str            # 当初为什么买入
    loss_root_cause: str       # 失败根因（追高/止损晚/基本面误判/...）
    lesson_learned: str        # 教训总结
    mistake_type: str          # 错误分类（"追高"、"抄底过早"、"止损不及时"、"选股逻辑错误"）
    round_number: str
    strategy_tag: str = ""     # 策略标签（供收益报表按策略归因亏损）
    holding_days: int = 0
    source: str = "sim"        # 口径：sim=模拟盘推演 / real=真实成交
    created_at: str = ""
    id: int = 0

    DDL = """
    CREATE TABLE IF NOT EXISTS mistake_book (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        stock_code       TEXT    NOT NULL,
        stock_name       TEXT    NOT NULL,
        entry_price      REAL    NOT NULL,
        exit_price       REAL    NOT NULL,
        loss_pct         REAL    NOT NULL,
        buy_reason       TEXT    NOT NULL,
        loss_root_cause  TEXT    NOT NULL,
        lesson_learned   TEXT    NOT NULL,
        mistake_type     TEXT    NOT NULL,
        strategy_tag     TEXT    NOT NULL DEFAULT '',
        round_number     TEXT    NOT NULL,
        holding_days     INTEGER DEFAULT 0,
        source           TEXT    NOT NULL DEFAULT 'sim',
        created_at       TEXT    NOT NULL
    );
    """

    @classmethod
    def create(
        cls,
        db_path: str,
        *,
        stock_code: str,
        stock_name: str,
        entry_price: float,
        exit_price: float,
        loss_pct: float,
        buy_reason: str,
        loss_root_cause: str,
        lesson_learned: str,
        mistake_type: str,
        strategy_tag: str = "",
        round_number: str,
        holding_days: int = 0,
        source: str = "sim",
        created_at: str = "",
    ) -> "MistakeBook":
        """插入一条失败记录"""
        if not created_at:
            created_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        record = cls(
            stock_code=stock_code, stock_name=stock_name,
            entry_price=entry_price, exit_price=exit_price,
            loss_pct=loss_pct, buy_reason=buy_reason,
            loss_root_cause=loss_root_cause, lesson_learned=lesson_learned,
            mistake_type=mistake_type, strategy_tag=strategy_tag,
            round_number=round_number,
            holding_days=holding_days, source=source, created_at=created_at,
        )
        with get_connection(db_path) as conn:
            conn.execute(
                """INSERT INTO mistake_book
                   (stock_code, stock_name, entry_price, exit_price,
                    loss_pct, buy_reason, loss_root_cause, lesson_learned,
                    mistake_type, strategy_tag, round_number, holding_days,
                    source, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (record.stock_code, record.stock_name, record.entry_price,
                 record.exit_price, record.loss_pct, record.buy_reason,
                 record.loss_root_cause, record.lesson_learned,
                 record.mistake_type, record.strategy_tag, record.round_number,
                 record.holding_days, record.source, record.created_at),
            )
            record.id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        log.warning("写入错题本 #%s %s(%s) 亏损%s%% 类型=%s [轮次 %s]",
                    record.id, record.stock_name, record.stock_code,
                    record.loss_pct, record.mistake_type, record.round_number)
        return record

    @staticmethod
    def recent(db_path: str, limit: int = 10, days: int | None = None) -> list[dict]:
        """最近 N 条错误记录（可限定近 days 天内）"""
        with get_connection(db_path, readonly=True) as conn:
            if days:
                cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
                rows = conn.execute(
                    "SELECT * FROM mistake_book WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
                    (cutoff, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM mistake_book ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]

    @staticmethod
    def top_causes(db_path: str, limit: int = 5) -> list[dict]:
        """高频失败根因统计"""
        with get_connection(db_path, readonly=True) as conn:
            rows = conn.execute(
                """SELECT mistake_type, COUNT(*) as cnt, AVG(loss_pct) as avg_loss
                   FROM mistake_book
                   GROUP BY mistake_type
                   ORDER BY cnt DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [
                {"type": row[0], "count": row[1], "avg_loss": round(row[2] or 0, 2)}
                for row in rows
            ]


# ============================================================
# 初始化
# ============================================================

def init_db(db_path: str) -> str:
    """创建数据库和表，返回数据库文件路径"""
    path = Path(db_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with get_connection(str(path)) as conn:
        conn.execute(CorrectBook.DDL)
        conn.execute(MistakeBook.DDL)
        # 旧库迁移：补 source 列（存量归 sim，保持历史口径）
        for table in ("correct_book", "mistake_book"):
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if "source" not in cols:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN source TEXT NOT NULL DEFAULT 'sim'"
                )
                log.info("知识库迁移: %s 新增 source 列（存量归 sim）", table)
        # 旧库迁移：错题本补 strategy_tag（供收益报表按策略归因亏损）
        cols = [r[1] for r in conn.execute("PRAGMA table_info(mistake_book)").fetchall()]
        if "strategy_tag" not in cols:
            conn.execute(
                "ALTER TABLE mistake_book ADD COLUMN strategy_tag TEXT NOT NULL DEFAULT ''"
            )
            log.info("知识库迁移: mistake_book 新增 strategy_tag 列")
    log.info("知识库已初始化: %s", path)
    return str(path)


# ============================================================
# CLI — 可直接运行建库
# ============================================================

if __name__ == "__main__":
    # 从任意工作目录运行都能正确定位项目 data 目录
    _data_dir = Path(__file__).resolve().parent.parent.parent / "data"
    db = init_db(str(_data_dir / "knowledge.db"))
    print(f"✅ 对题本/错题本数据库已创建: {db}")
