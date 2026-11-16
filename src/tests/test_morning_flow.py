"""盘前流程测试 — 真实跟单买入清单（模拟盘建仓 → 竞价预案指令，不新增输出文件）"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from morning_flow import mirror_buy_orders
from src.trim_rules import MIRROR_MARK


def _pos(account, symbol, shares, cost, note=""):
    return {"account": account, "symbol": symbol, "name": symbol,
            "shares": shares, "cost_price": cost, "note": note}


def test_sim_buy_with_mark_is_included():
    positions = [_pos("sim", "sh600019", 800, 5.86,
                      f"R47建仓800股@5.86 {MIRROR_MARK}")]
    orders = mirror_buy_orders(positions)
    assert len(orders) == 1
    assert orders[0]["symbol"] == "sh600019"
    assert orders[0]["shares"] == 800
    assert orders[0]["ref_price"] == 5.86


def test_sim_without_mark_excluded():
    positions = [_pos("sim", "sh600019", 800, 5.86, "R45建仓800股(模拟)")]
    assert mirror_buy_orders(positions) == []


def test_real_position_excluded():
    positions = [_pos("real", "sh600019", 800, 5.86, f"真实持仓 {MIRROR_MARK}")]
    assert mirror_buy_orders(positions) == []


def test_zero_share_excluded():
    positions = [_pos("sim", "sh600019", 0, 5.86, f"已清仓 {MIRROR_MARK}")]
    assert mirror_buy_orders(positions) == []
