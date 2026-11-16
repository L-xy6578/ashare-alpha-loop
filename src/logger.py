"""
三角闭环选股体系 — 统一日志模块

所有模块通过 get_logger(name) 获取 logger，日志同时输出到：
    - 文件 logs/app.log   （按 config.LOG_FILE，UTF-8 编码）
    - 控制台 stdout        （便于实时观察）

设计要点:
    - root logger 只配置一次（_ROOT_CONFIGURED 守卫），避免重复 addHandler
    - 文件日志不可用时自动降级为仅控制台，不影响主流程

用法:
    from src.logger import get_logger
    log = get_logger(__name__)
    log.info("交易记录已写入对题本 #%s", record_id)
"""

import logging
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler

from .config import LOG_FILE, LOG_LEVEL, LOG_FORMAT


_ROOT_CONFIGURED = False


def _configure_root() -> None:
    """配置一次 root logger（幂等，重复调用安全）"""
    global _ROOT_CONFIGURED
    if _ROOT_CONFIGURED:
        return

    root = logging.getLogger()
    root.setLevel(LOG_LEVEL)

    formatter = logging.Formatter(LOG_FORMAT)

    # 控制台 handler
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    # 文件 handler（失败则降级）
    try:
        Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
        # 按大小轮转：单文件 5MB，保留 5 个备份（logs/app.log.1 ~ .5）
        file_handler = RotatingFileHandler(
            LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except Exception:
        root.warning("无法初始化文件日志，仅使用控制台输出", exc_info=True)

    _ROOT_CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """获取模块 logger（首次调用自动完成 root 配置）"""
    _configure_root()
    return logging.getLogger(name)
