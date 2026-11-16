"""
三角闭环选股体系 — 全局配置

所有模块通过此文件获取配置，禁止在业务代码中硬编码路径、参数。

用法:
    from src.config import DB_PATH, LOG_FILE
"""

import os
from pathlib import Path

# ============================================================
# 路径（相对于项目根目录）
# ============================================================
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"

# ============================================================
# 数据库
# ============================================================
DB_PATH = str(DATA_DIR / "knowledge.db")
DB_TIMEOUT = 5.0        # 秒，SQLite 连接超时
DB_JOURNAL_MODE = "WAL" # Write-Ahead Logging

# 行情库（数据管道写入，独立于知识库）
MARKET_DB_PATH = str(DATA_DIR / "market.db")

# 指数化组合库（2026-11-11 策略路线变更：五因子选股退役后的 sim 记账）
# 与 knowledge.db 完全分离：只记"该买什么、值多少钱"，不写 real 口径
INDEX_DB_PATH = str(DATA_DIR / "index_portfolio.db")

# ============================================================
# 真实账户风控（禁止满仓单票）
# ============================================================
REAL_TOTAL_ASSETS = float(os.getenv("REAL_TOTAL_ASSETS", "100000"))
REAL_MAX_POSITION_PCT = float(os.getenv("REAL_MAX_POSITION_PCT", "20"))

# ============================================================
# 日志
# ============================================================
LOG_FILE = str(LOG_DIR / "app.log")
LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
