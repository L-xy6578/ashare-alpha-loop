"""VCP 波动收缩识别单元测试：两次收缩成立 / 只有一次 / 放量破坏"""

import src.vcp as vcp
from src.data_source import KlineBar
from src.vcp import build_risk_plan, derive_zone, detect_vcp, trim_shares


def _mk(closes, vols):
    return [KlineBar(f"d{i:02d}", c, c, c * 1.01, c * 0.99, volume=v)
            for i, (c, v) in enumerate(zip(closes, vols))]


def _vcp_bars(last_vol=3000):
    up = [88 + i * 1.2 for i in range(11)]
    down = [98, 96, 94, 92, 90]
    up2 = [91, 93, 95, 97]
    down2 = [95, 94, 93]
    up3 = [93.5, 94, 94.5]
    down3 = [94.2, 93.8, 93.5]
    closes = up + down + up2 + down2 + up3 + down3
    vols = [12000] * len(closes)
    last_start = len(up) + len(down) + len(up2) + len(down2) + len(up3) - 1
    vols[last_start:] = [last_vol] * (len(closes) - last_start)
    return _mk(closes, vols)


def test_vcp_two_contractions_pass():
    r = detect_vcp(_vcp_bars())
    assert r.is_vcp
    assert r.contractions >= 2
    assert r.depths[-1] < r.depths[-2]     # 回撤逐次变浅
    assert r.last_vol_ratio <= 0.5
    assert r.box_high > r.box_low


def test_vcp_only_one_contraction_fails():
    closes = [80 + i for i in range(25)] + [104, 102]
    bars = _mk(closes, [8000] * len(closes))
    r = detect_vcp(bars)
    assert not r.is_vcp
    assert r.contractions < 2
    assert any("收缩次数不足" in x for x in r.reasons)


def test_vcp_volume_expansion_breaks():
    r = detect_vcp(_vcp_bars(last_vol=12000))
    assert not r.is_vcp
    assert r.last_vol_ratio > 0.5
    assert any("放量破坏" in x for x in r.reasons)


def test_derive_zone_and_risk_plan():
    bars = _vcp_bars()
    r = detect_vcp(bars)
    zone = derive_zone(r, bars)
    assert zone["zone_low"] == r.box_low
    assert zone["zone_high"] >= zone["zone_low"] * 1.005
    risk = build_risk_plan(zone["zone_high"], zone, shares=200, bars=bars)
    # 止损取更近者（更高价）
    assert risk["stop_price"] >= risk["hard_stop"] - 1e-9
    assert risk["stop_price"] >= risk["structural_stop"] - 1e-9
    # 单只最大亏损 ≤450
    assert 200 * (risk["entry_ref"] - risk["stop_price"]) <= 450.0 + 1e-6
    assert risk["tp1"] > risk["entry_ref"] and risk["tp2"] > risk["tp1"]


def test_trim_shares_rounding():
    assert trim_shares(600) == {"tier1": 200, "tier2": 200, "trail": 200}
    assert trim_shares(200) == {"tier1": 100, "tier2": 100, "trail": 0}
    assert trim_shares(100) == {"tier1": 100, "tier2": 0, "trail": 0}


# ---------- 量能比分母：箱体整体均量（2026-11-09 冻结期 bug 修复） ----------

def _vol_bars(n=60, hi_vol=200.0, lo_vol=50.0, switch=25):
    return [KlineBar(f"d{i:02d}", 10.0, 10.0, 10.0, 9.5,
                     volume=(hi_vol if i < switch else lo_vol))
            for i in range(n)]


def test_vol_ratio_denominator_is_box_mean(monkeypatch):
    """分母 = 首个有效收缩段起点至今的均量，而不是收缩段前 5 根"""
    bars = _vol_bars(switch=25)
    monkeypatch.setattr(vcp, "_find_pivot_highs", lambda b, span=2: [5, 15, 25])
    r = detect_vcp(bars)
    vol_box = (200.0 * 20 + 50.0 * 35) / 55        # segments[0][0]=5 起算
    assert r.last_vol_ratio == round(50.0 / vol_box, 2) == 0.48
    assert r.last_vol_ratio != round(50.0 / 200.0, 2)   # 旧口径（前 5 日）会得 0.25


def test_vol_ratio_truncated_box_is_missing(monkeypatch):
    """箱体左边界落在 lookback 起点且仍有更早历史 → 判缺失，不静默回退"""
    bars = _vol_bars()
    monkeypatch.setattr(vcp, "_find_pivot_highs", lambda b, span=2: [0, 15, 25])
    r = detect_vcp(bars)                            # n_all=60 == lookback，无截断
    assert r.last_vol_ratio is not None

    older = _vol_bars(70)                           # n_all=70 > lookback → 窗口被截断
    monkeypatch.setattr(vcp, "_find_pivot_highs", lambda b, span=2: [0, 15, 25])
    r2 = detect_vcp(older)
    assert r2.last_vol_ratio is None and not r2.is_vcp
    assert any("箱体左边界触及回看窗口起点" in x for x in r2.reasons)
