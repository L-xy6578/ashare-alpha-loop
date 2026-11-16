"""
数据库连接管理

提供统一的 SQLite 连接，支持 WAL 模式、超时控制、行工厂，
并在上下文退出时自动提交/回滚；所有连接操作经统一日志模块记录。

用法:
    from src.db import get_connection
    with get_connection() as conn:
        conn.execute("SELECT ...")
    # 退出时自动 commit（无异常）或 rollback（异常）
"""

import sqlite3
from contextlib import contextmanager

from .config import DB_PATH, DB_TIMEOUT, DB_JOURNAL_MODE
from .logger import get_logger

log = get_logger(__name__)


@contextmanager
def get_connection(db_path: str | None = None, readonly: bool = False):
    """获取 SQLite 连接（上下文管理器，自动提交/回滚/关闭）

    Args:
        db_path: 数据库路径，默认使用 config.DB_PATH
        readonly: True 时以只读模式打开（并发读取更安全）
    """
    path = db_path or DB_PATH
    uri = f"file:{path}?mode=ro" if readonly else path
    conn = sqlite3.connect(
        uri if readonly else path,
        timeout=DB_TIMEOUT,
        uri=readonly,
    )
    conn.row_factory = sqlite3.Row
    if not readonly:
        conn.execute(f"PRAGMA journal_mode={DB_JOURNAL_MODE}")
    log.debug("open connection -> %s (readonly=%s)", path, readonly)
    try:
        yield conn
        if not readonly:
            conn.commit()
    except Exception:
        conn.rollback()
        log.exception("transaction failed, rolled back -> %s", path)
        raise
    finally:
        conn.close()
        log.debug("closed connection -> %s", path)
