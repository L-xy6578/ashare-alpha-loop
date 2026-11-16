"""
回归检查 — 每次代码变更后跑一遍（幂等 / 主赛道完整性 / 资金单位）

覆盖（2026-08-05 多跑回归抓出的 bug 类）：
    1. r47 连跑 2 次幂等（同一临时库，持仓/R47/K线/观察池快照一致）
    2. 主赛道完整性：月度 TOP 无 zdf20 缺失板块、季度 TOP 无 zdf60 缺失板块（#34/#37）
    3. 资金单位量级：板块 zljlr/zljlr_d5/zljlr_d20 应为"万元"量级（#35）

用法:
    python regression_check.py            # 三查（r47×2 需网络，约 15-25 秒）
    python regression_check.py --tests    # 附加全量 pytest
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent


def check_r47_idempotent() -> tuple[bool, str]:
    """r47 dry-run 同一临时库连跑 2 次，快照必须一致"""
    import r47_closed_loop as r47

    db_dir = tempfile.mkdtemp(prefix="regression_")

    def snap():
        kb, mk = f"{db_dir}/knowledge.db", f"{db_dir}/market.db"
        with sqlite3.connect(kb) as c:
            pos = c.execute(
                "SELECT symbol, shares FROM positions WHERE account='sim' AND shares>0 ORDER BY symbol"
            ).fetchall()
            r47c = c.execute("SELECT COUNT(*) FROM correct_book WHERE round_number='R47'").fetchone()[0]
            r47m = c.execute("SELECT COUNT(*) FROM mistake_book WHERE round_number='R47'").fetchone()[0]
        with sqlite3.connect(mk) as c:
            bars = c.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0]
            wl = c.execute("SELECT COUNT(*) FROM watchlist WHERE status='active'").fetchone()[0]
        return pos, r47c, r47m, bars, wl

    r47.main(dry_run=True, db_dir=db_dir)
    s1 = snap()
    r47.main(dry_run=True, db_dir=db_dir)
    s2 = snap()
    return s1 == s2, f"持仓{s1[0]} R47 {s1[1]}/{s1[2]} K线{s1[3]} 观察池{s1[4]} 两次{'一致' if s1 == s2 else '不一致'}"


def check_tracks_integrity() -> tuple[bool, str]:
    """主赛道 TOP 不得含数据缺失维度（月度 zdf20=0 / 季度 zdf60=0）"""
    from src.sector_analysis import SectorAnalyzer

    r = SectorAnalyzer().analyze()
    bad_monthly = [t.name for t in r["monthly_tracks"] if t.zdf20 == 0]
    bad_quarterly = [t.name for t in r["quarterly_tracks"] if t.zdf60 == 0]
    if bad_monthly or bad_quarterly:
        return False, f"月度含缺失:{bad_monthly} 季度含缺失:{bad_quarterly}"
    top_m = "、".join(t.name for t in r["monthly_tracks"][:3])
    top_q = "、".join(t.name for t in r["quarterly_tracks"][:3])
    return True, f"月度TOP[{top_m}] 季度TOP[{top_q}] 无缺失维度"


def check_fund_units() -> tuple[bool, str]:
    """板块资金单位量级：应为万元（单板块 20 日资金不应超过 1 亿万元=1 万亿）"""
    from src.config import MARKET_DB_PATH

    with sqlite3.connect(MARKET_DB_PATH) as conn:
        rows = conn.execute(
            """SELECT name, zljlr, zljlr_d5, zljlr_d20 FROM sector_daily
               WHERE date=(SELECT MAX(date) FROM sector_daily WHERE kind='plate')
                 AND kind='plate'"""
        ).fetchall()
    bad = []
    for name, z, z5, z20 in rows:
        for v, tag in ((z, "当日"), (z5, "5日"), (z20, "20日")):
            if abs(v or 0) >= 1e8:   # ≥1 万亿万元（1 万亿元）→ 单位错误
                bad.append(f"{name}{tag}={v:.0f}")
                break
    if bad:
        return False, f"资金单位异常: {bad[:5]}"
    return True, f"板块 {len(rows)} 只资金量级正常（万元）"


def main() -> None:
    parser = argparse.ArgumentParser(description="回归检查（变更后必跑）")
    parser.add_argument("--tests", action="store_true", help="附加全量 pytest")
    args = parser.parse_args()

    print("=" * 60)
    print("  回归检查")
    print("=" * 60)
    checks = [
        ("r47 幂等（连跑 2 次）", check_r47_idempotent),
        ("主赛道完整性", check_tracks_integrity),
        ("资金单位量级", check_fund_units),
    ]
    all_ok = True
    for label, fn in checks:
        try:
            ok, msg = fn()
        except Exception as e:
            ok, msg = False, f"异常: {e}"
        print(f"  {'✅' if ok else '❌'} {label}: {msg}")
        all_ok = all_ok and ok

    if args.tests:
        print("\n-- 全量 pytest --")
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "src/tests/", "-q"],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        )
        tail = (r.stdout or r.stderr or "").strip().splitlines()[-1:]
        ok = r.returncode == 0
        print(f"  {'✅' if ok else '❌'} pytest exit={r.returncode}: {tail}")
        all_ok = all_ok and ok

    print("=" * 60)
    print("  回归" + ("✅ 全部通过" if all_ok else "❌ 有失败，先修再上线"))
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
