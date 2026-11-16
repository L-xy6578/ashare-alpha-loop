"""
三角闭环选股体系 — 数据管道命令行入口

用法:
    python -m src.pipeline --symbols sh600535,sh600019 --start 2026-07-01 --end 2026-08-03
    python -m src.pipeline --symbols sh600535,sh600019 --force        # 全量重拉
    python -m src.pipeline --all-watchlist                           # 拉取自选池全部
    python -m src.pipeline --quality sh600535                        # 仅质量检查
"""

import argparse
import sys

from src.logger import get_logger
from src.data_source import TencentHttpDataSource
from src.pipeline.collector import Collector
from src.pipeline.quality import check_data_quality
from src.watchlist import WatchlistStore

log = get_logger(__name__)

# 指数锚点 + 基准：沪深300（sh000300）用于冻结期满"是否跑赢沪深300"的离线复算/审计
INDEX_SYMBOLS = ["sh000001", "sz399001", "sz399006", "sh000300"]


def _default_symbols(with_indexes: bool = True) -> list[str]:
    """默认采集集：观察池单一数据源（#89），可选附加三大指数"""
    wl = [w.symbol for w in WatchlistStore().all()]
    if not with_indexes:
        return wl
    seen = set(INDEX_SYMBOLS)
    return INDEX_SYMBOLS + [s for s in wl if s not in seen]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="三角闭环行情数据管道")
    p.add_argument("--symbols", type=str, default=None, help="标的列表，逗号分隔")
    p.add_argument("--start", type=str, default=None, help="起始日期 YYYY-MM-DD")
    p.add_argument("--end", type=str, default=None, help="截止日期 YYYY-MM-DD")
    p.add_argument("--force", action="store_true", help="忽略增量，全量重拉")
    p.add_argument("--all-watchlist", action="store_true", help="拉取自选池全部标的")
    p.add_argument("--quality", type=str, default=None, help="仅检查某标的质量")
    return p


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")   # #33：GBK 控制台打印 ⚠/✅ 防 UnicodeEncodeError
    args = build_parser().parse_args(argv)

    # 仅质量检查模式
    if args.quality:
        rep = check_data_quality(args.quality)
        print(f"[{rep.grade}] {rep.symbol}: {rep.detail}")
        return 0

    # 确定标的列表
    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    elif args.all_watchlist:
        symbols = _default_symbols(with_indexes=False)
    else:
        symbols = _default_symbols(with_indexes=True)

    # 数据源：默认腾讯公开 HTTP（零依赖），可改 AkshareDataSource / StaticDataSource
    source = TencentHttpDataSource()
    collector = Collector(source)

    print(f"数据源: {source.__class__.__name__}")
    print(f"标的:   {symbols}")
    print(f"区间:   {args.start or '增量'} ~ {args.end or '今天'}" + ("（强制全量）" if args.force else ""))
    print("-" * 60)

    ok = 0
    for sym in symbols:
        res = collector.collect(sym, start=args.start, end=args.end, force=args.force)
        q = check_data_quality(sym)
        mark = "✅" if res.status == "ok" and res.upserted > 0 else "⚠️"
        print(f"{mark} {sym}: upsert={res.upserted}, quality=[{q.grade}] {q.detail}")
        if res.status == "ok":
            ok += 1

    print("-" * 60)
    print(f"完成: {ok}/{len(symbols)} 采集成功")
    return 0 if ok == len(symbols) else 1


if __name__ == "__main__":
    sys.exit(main())
