"""
三角闭环选股体系 — Gold 层（分析就绪指标）

Medallion 架构第三层：
    Bronze(daily_bars 原始行情) → Silver(质量校准) → Gold(gold_bars 技术指标物化)

Gold 层消费方：可视化仪表盘、报表、指标分析——直接读指标，不重复计算。
"""

from .builder import GoldBuilder, GoldBuildResult, symbol_is_gem
from .schema import init_gold_db

__all__ = ["GoldBuilder", "GoldBuildResult", "symbol_is_gem", "init_gold_db"]
