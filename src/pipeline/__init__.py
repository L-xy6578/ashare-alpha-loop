"""
三角闭环选股体系 — 数据管道（Data Pipeline）

负责把行情/宏观/财报数据自动采集入库，供后续分析、可视化、报表消费。

架构：
    src/pipeline/
    ├── __init__.py    # 包入口，导出主要类
    ├── schema.py      # market.db 表结构（daily_bars 等）
    ├── collector.py   # 增量采集器：从 DataSource 拉取 → 入库
    ├── quality.py     # 数据质量检查（OHLC 一致性、缺失、异常值）
    └── cli.py         # 命令行入口：python -m src.pipeline

数据流：
    DataSource (westock-mcp / akshare / tencent-http)
        → collector.Collector.collect(symbol, start, end)
        → market.db: daily_bars 表（增量 upsert，幂等）
        → quality.check(symbol) → 质量报告
"""

from .collector import Collector
from .quality import QualityReport, check_data_quality

__all__ = ["Collector", "QualityReport", "check_data_quality"]
