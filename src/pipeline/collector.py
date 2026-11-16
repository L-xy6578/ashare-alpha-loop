"""
三角闭环选股体系 — 增量采集器

从 DataSource 拉取日K线，增量写入 market.db。

核心设计：
    - 幂等：INSERT OR REPLACE，同一 (symbol, date) 重复采集不产生脏数据
    - 增量：自动探测库内该标的已有最大日期，只拉增量区间
    - 来源可追溯：rows_source 记录数据来自哪个 DataSource
    - 采集日志：每次任务写 collect_log，便于审计
"""

from __future__ import annotations

from dataclasses import dataclass

from src.db import get_connection
from src.logger import get_logger
from src.data_source import DataSource, KlineBar

from .schema import init_market_db

log = get_logger(__name__)


@dataclass
class CollectResult:
    """一次采集任务的结果"""

    symbol: str
    upserted: int           # 本次 upsert 的行数
    source: str
    start_date: str | None  # 实际拉取的起始日期
    end_date: str | None    # 实际拉取的截止日期
    status: str             # ok / error
    message: str = ""


class Collector:
    """行情增量采集器"""

    def __init__(self, source: DataSource, db_path: str | None = None):
        """Args:
            source: 行情数据源（westock-mcp 静态注入 / akshare / tencent-http）
            db_path: market.db 路径，None 用 config.MARKET_DB_PATH（默认 data/market.db）
        """
        from src.config import MARKET_DB_PATH

        self.source = source
        self.db_path = db_path or MARKET_DB_PATH
        self.source_name = source.__class__.__name__.replace("DataSource", "").lower() or "unknown"

        # 确保表结构存在
        with get_connection(self.db_path) as conn:
            init_market_db(conn)

    def last_date(self, symbol: str) -> str | None:
        """查询库内该标的已有最大交易日

        Returns:
            最新日期 YYYY-MM-DD；无数据返回 None
        """
        with get_connection(self.db_path, readonly=True) as conn:
            row = conn.execute(
                "SELECT MAX(date) AS d FROM daily_bars WHERE symbol = ?", (symbol,)
            ).fetchone()
            return row["d"] if row and row["d"] else None

    def _upsert_bars(self, symbol: str, bars: list[KlineBar], source: str,
                     adjust: str = "unknown") -> int:
        """批量 upsert K 线（含 volume/amount），返回写入行数"""
        with get_connection(self.db_path) as conn:
            cur = conn.executemany(
                """INSERT OR REPLACE INTO daily_bars
                   (symbol, date, open, close, high, low, volume, amount,
                    rows_source, adjust)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(symbol, b.date, b.open, b.close, b.high, b.low,
                  b.volume, b.amount, source, adjust) for b in bars],
            )
            return cur.rowcount if cur.rowcount > 0 else len(bars)

    def save_bars(self, symbol: str, bars: list[KlineBar],
                  source: str | None = None, adjust: str | None = None) -> int:
        """直接落库（不回拉）：供闭环脚本把补拉的行情写入行情库

        Args:
            symbol: 标的代码
            bars: 日K列表
            source: 覆盖来源标注，默认 collector.source_name（如 tencenthttp）
        """
        if not bars:
            return 0
        src = source or self.source_name_for(symbol)
        adj = adjust or self._adjust_for(symbol)
        n = self._upsert_bars(symbol, bars, src, adj)
        self._log_collect(symbol, bars[0].date, bars[-1].date, n, "ok",
                          "direct save (closed-loop)")
        log.info("闭环落库 %s: %d 根K线（%s~%s, source=%s, adjust=%s）",
                 symbol, n, bars[0].date, bars[-1].date, src, adj)
        return n

    def _adjust_for(self, symbol: str) -> str:
        """复权口径：优先数据源按标的判定，兼容未实现 adjust_for 的测试/旧数据源"""
        fn = getattr(self.source, "adjust_for", None)
        if callable(fn):
            return fn(symbol)
        return getattr(self.source, "adjust", "unknown")

    def source_name_for(self, symbol: str) -> str:
        """落库来源标注：指数加 ``_index`` 后缀，与个股（tencenthttp）区分"""
        from src.data_source import is_index_symbol

        return f"{self.source_name}_index" if is_index_symbol(symbol) else self.source_name

    def _log_collect(self, symbol: str, start: str | None, end: str | None,
                     upserted: int, status: str, message: str = "") -> None:
        with get_connection(self.db_path) as conn:
            conn.execute(
                """INSERT INTO collect_log
                   (symbol, start_date, end_date, rows_upserted, source, status, message)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (symbol, start, end, upserted, self.source_name, status, message),
            )

    def collect(self, symbol: str, start: str | None = None, end: str | None = None,
                force: bool = False, retries: int = 2) -> CollectResult:
        """增量采集单个标的

        Args:
            symbol: 标的代码 sh600535 / sz399006
            start:  强制起始日期（优先于自动增量起点）；自动模式为库内最大日期+1
            end:    截止日期，默认今天
            force:  True 时忽略库内已有数据，全量重拉 start~end
            retries: 失败自动重试次数（P1-4，网络抖动自愈）
        """
        last_err: str | None = None
        for attempt in range(retries + 1):
            if attempt > 0:
                log.warning("%s 第%d次重试...", symbol, attempt)
            try:
                return self._collect_once(symbol, start, end, force)
            except Exception as e:
                last_err = str(e)
                log.exception("采集 %s 失败(第%d次): %s", symbol, attempt + 1, e)

        # 全部重试失败
        self._log_collect(symbol, start, end, 0, "error", last_err or "unknown")
        return CollectResult(symbol, 0, self.source_name, start, end, "error", last_err or "unknown")

    def _collect_once(self, symbol: str, start: str | None, end: str | None,
                      force: bool) -> CollectResult:
        """单次采集（无重试）"""
        # 自动增量：若库内已有数据且未强制，从 last_date+1 开始
        if not force:
            last = self.last_date(symbol)
            if last and (start is None or start <= last):
                start = _next_day(last)

        bars = self.source.daily_kline(symbol, start=start, end=end)
        if not bars:
            msg = f"{symbol}: 无数据（区间 {start}~{end}）"
            log.warning(msg)
            self._log_collect(symbol, start, end, 0, "ok", "empty range")
            return CollectResult(symbol, 0, self.source_name, start, end, "ok", msg)

        upserted = self._upsert_bars(symbol, bars, self.source_name_for(symbol),
                                     self._adjust_for(symbol))
        actual_start, actual_end = bars[0].date, bars[-1].date
        self._log_collect(symbol, actual_start, actual_end, upserted, "ok")
        log.info("采集 %s: %d 根K线（%s ~ %s, source=%s, adjust=%s）",
                 symbol, upserted, actual_start, actual_end,
                 self.source_name_for(symbol), self._adjust_for(symbol))
        return CollectResult(symbol, upserted, self.source_name,
                             actual_start, actual_end, "ok")

    def collect_many(self, symbols: list[str], start: str | None = None,
                     end: str | None = None, force: bool = False,
                     workers: int = 4) -> dict[str, CollectResult]:
        """批量采集多个标的（P1-4：线程池并发）

        Args:
            symbols: 标的列表
            workers: 并发线程数（SQLite WAL 模式支持并发写）
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        results: dict[str, CollectResult] = {}
        if len(symbols) <= 1 or workers <= 1:
            for sym in symbols:
                results[sym] = self.collect(sym, start=start, end=end, force=force)
            return results

        with ThreadPoolExecutor(max_workers=min(workers, len(symbols))) as pool:
            futures = {
                pool.submit(self.collect, sym, start, end, force): sym
                for sym in symbols
            }
            for fut in as_completed(futures):
                sym = futures[fut]
                results[sym] = fut.result()
        log.info("并发采集完成 %d 标的（workers=%d）", len(results), workers)
        return results


def _next_day(date_str: str) -> str:
    """返回 date_str 的下一天（YYYY-MM-DD）"""
    from datetime import date, timedelta

    y, m, d = (int(x) for x in date_str.split("-"))
    return (date(y, m, d) + timedelta(days=1)).isoformat()
