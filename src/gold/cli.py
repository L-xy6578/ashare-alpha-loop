"""
三角闭环选股体系 — Gold 层构建命令行

用法:
    python -m src.gold                                # 构建观察池（默认，单一数据源）
    python -m src.gold --symbols sh600535,sh600019    # 指定标的全量构建
    python -m src.gold --symbols sh600535 --inspect   # 查看 Gold 数据
"""

import argparse
import sys

from src.db import get_connection
from src.logger import get_logger
from src.watchlist import WatchlistStore

from .builder import GoldBuilder

log = get_logger(__name__)


def _watchlist_symbols() -> list[str]:
    """构建范围跟随观察池表（#86：单一数据源，不再硬编码）"""
    return [w.symbol for w in WatchlistStore().all()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Gold 层指标构建")
    p.add_argument("--symbols", type=str, default=None)
    p.add_argument("--all-watchlist", action="store_true")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--inspect", action="store_true", help="仅查看 Gold 数据")
    return p


def _inspect(symbols: list[str]) -> None:
    from src.config import MARKET_DB_PATH

    with get_connection(MARKET_DB_PATH, readonly=True) as conn:
        for sym in symbols:
            row = conn.execute(
                """SELECT date, close, pct_chg, ma20, rsi14, limit_up, limit_down,
                          volume_ratio, regime
                   FROM gold_bars WHERE symbol=? ORDER BY date DESC LIMIT 3""",
                (sym,),
            ).fetchall()
            print(f"\n【{sym}】最近3根 Gold 指标:")
            if not row:
                print("  （无数据，先构建）")
                continue
            for r in row:
                print(f"  {r['date']} close={r['close']} chg={r['pct_chg'] and f'{r['pct_chg']:.2%}'}"
                      f" ma20={r['ma20'] and f'{r['ma20']:.2f}'} rsi={r['rsi14'] and f'{r['rsi14']:.1f}'}"
                      f" 涨停={r['limit_up']} 跌停={r['limit_down']}"
                      f" 量比={r['volume_ratio'] and f'{r['volume_ratio']:.2f}'}"
                      f" 状态={r['regime']}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.all_watchlist:
        symbols = _watchlist_symbols()
    elif args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = _watchlist_symbols()

    if args.inspect:
        _inspect(symbols)
        return 0

    builder = GoldBuilder()
    print(f"Gold 层构建 | 标的: {symbols} | workers={args.workers}")
    print("-" * 60)

    results = builder.build_many(symbols, workers=args.workers)
    ok = 0
    for sym, res in results.items():
        mark = "✅" if res.status == "ok" else "❌"
        print(f"{mark} {sym}: {res.upserted} 行（{res.start_date} ~ {res.end_date}）")
        if res.status == "ok":
            ok += 1

    print("-" * 60)
    print(f"完成: {ok}/{len(symbols)}")
    return 0 if ok == len(symbols) else 1


if __name__ == "__main__":
    sys.exit(main())
