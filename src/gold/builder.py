"""
三角闭环选股体系 — Gold 层构建器

从 Bronze(daily_bars) 计算技术指标，物化到 Gold(gold_bars)。

构建策略：
    - 全量：build(symbol) → 读取全部 daily_bars，计算所有指标
    - 增量：build(symbol, start) → 为计算窗口指标，会多读 start 之前的 WARMUP 根K线
      （如 MA60 需要前 60 根，start 前的数据只用于计算，不重新写回旧行）

幂等性：
    - INSERT OR REPLACE，(symbol, date) 主键，重复构建无脏数据
"""

from __future__ import annotations

from dataclasses import dataclass

from src.db import get_connection
from src.logger import get_logger
from src.strategy import _rsi  # 复用 RSI 计算（同源同果）
from src.regime import detect_regime

from .schema import init_gold_db

log = get_logger(__name__)

# MA60 需要 60 根，加缓冲
WARMUP = 70


@dataclass
class GoldBuildResult:
    symbol: str
    upserted: int
    start_date: str | None
    end_date: str | None
    status: str
    message: str = ""


class GoldBuilder:
    """Gold 层构建器：Bronze → Gold"""

    def __init__(self, db_path: str | None = None):
        from src.config import MARKET_DB_PATH

        self.db_path = db_path or MARKET_DB_PATH
        with get_connection(self.db_path) as conn:
            init_gold_db(conn)

    # ── 读取 Bronze ──
    def _read_bronze(self, symbol: str, start: str | None = None) -> list[dict]:
        """读取 daily_bars（升序）

        增量时读取最近 5000 根（含 start 之后的新数据 + start 之前的 warmup，
        供 MA60/RSI 等窗口指标计算；start 之前只用于计算，不写回）。
        """
        with get_connection(self.db_path, readonly=True) as conn:
            rows = conn.execute(
                "SELECT date, open, close, high, low, volume "
                "FROM daily_bars WHERE symbol=? "
                "ORDER BY date DESC LIMIT 5000", (symbol,)
            ).fetchall()
        return list(reversed([dict(r) for r in rows]))

    def _last_gold_date(self, symbol: str) -> str | None:
        with get_connection(self.db_path, readonly=True) as conn:
            row = conn.execute(
                "SELECT MAX(date) AS d FROM gold_bars WHERE symbol=?", (symbol,)
            ).fetchone()
            return row["d"] if row and row["d"] else None

    # ── 指标计算 ──
    def _compute_row(self, idx: int, bars: list[dict], symbol: str) -> dict:
        """计算第 idx 根K线的指标（依赖前序 bars）"""
        bar = bars[idx]
        closes = [b["close"] for b in bars[: idx + 1]]
        out = dict(bar)

        # 涨跌幅
        if idx > 0 and bars[idx - 1]["close"] > 0:
            out["pct_chg"] = (bar["close"] - bars[idx - 1]["close"]) / bars[idx - 1]["close"]

        # 均线
        for n, key in ((5, "ma5"), (10, "ma10"), (20, "ma20"), (60, "ma60")):
            if len(closes) >= n:
                out[key] = sum(closes[-n:]) / n

        # RSI(14)
        if len(closes) >= 15:
            out["rsi14"] = _rsi(closes, 14)

        # 涨跌停（主板±10%；创业板/科创板±20%）
        if out.get("pct_chg") is not None:
            limit = 0.20 if symbol_is_gem(symbol) else 0.10
            out["limit_up"] = 1 if out["pct_chg"] >= limit - 0.001 else 0
            out["limit_down"] = 1 if out["pct_chg"] <= -limit + 0.001 else 0

        # 量比（当日量 / 前5日均量）
        volumes = [b.get("volume") for b in bars[max(0, idx - 5): idx]]
        volumes = [v for v in volumes if v]
        if bar.get("volume") and volumes:
            avg = sum(volumes) / len(volumes)
            if avg > 0:
                out["volume_ratio"] = bar["volume"] / avg

        # 市场状态快照（基于截至当日的K线）
        regime_bars = bars[: idx + 1]
        if len(regime_bars) >= 61:
            from src.data_source import KlineBar

            kb = [KlineBar(b["date"], b["open"], b["close"], b["high"], b["low"]) for b in regime_bars]
            r = detect_regime(kb)
            if r:
                out["regime"] = r.regime.value
        return out

    # ── 构建 ──
    def build(self, symbol: str, start: str | None = None, force: bool = False) -> GoldBuildResult:
        """构建某标的的 Gold 指标

        Args:
            symbol: 标的代码
            start:  增量起点（只构建该日期之后的指标）；None=全量
            force:  True 时忽略增量起点，全量重建
        """
        try:
            # 增量：从 gold 已构建的下一日开始（但需要 warmup 计算窗口）
            if not force:
                last = self._last_gold_date(symbol)
                if last and (start is None or start <= last):
                    start = _next_day(last)

            bars = self._read_bronze(symbol, start=start)
            if not bars:
                msg = f"{symbol}: Bronze 无数据"
                log.warning(msg)
                self._log_build(symbol, None, None, 0, "ok", msg)
                return GoldBuildResult(symbol, 0, None, None, "ok", msg)

            # 计算指标（仅 start 之后的行写入；之前的行是 warmup 不写）
            rows_to_write = []
            for idx in range(len(bars)):
                date = bars[idx]["date"]
                if start and date < start:
                    continue
                rows_to_write.append(self._compute_row(idx, bars, symbol))

            upserted = self._upsert(symbol, rows_to_write)
            actual_start = rows_to_write[0]["date"] if rows_to_write else None
            actual_end = rows_to_write[-1]["date"] if rows_to_write else None
            self._log_build(symbol, actual_start, actual_end, upserted, "ok")
            log.info("Gold构建 %s: %d 行（%s ~ %s）", symbol, upserted,
                     actual_start, actual_end)
            return GoldBuildResult(symbol, upserted, actual_start, actual_end, "ok")

        except Exception as e:
            log.exception("Gold构建 %s 失败: %s", symbol, e)
            self._log_build(symbol, None, None, 0, "error", str(e))
            return GoldBuildResult(symbol, 0, None, None, "error", str(e))

    def build_many(self, symbols: list[str], workers: int = 4) -> dict[str, GoldBuildResult]:
        """并发构建多个标的"""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        results = {}
        with ThreadPoolExecutor(max_workers=min(workers, len(symbols))) as pool:
            futures = {pool.submit(self.build, sym): sym for sym in symbols}
            for fut in as_completed(futures):
                sym = futures[fut]
                results[sym] = fut.result()
        return results

    # ── 内部 ──
    def _upsert(self, symbol: str, rows: list[dict]) -> int:
        with get_connection(self.db_path) as conn:
            cur = conn.executemany(
                """INSERT OR REPLACE INTO gold_bars
                   (symbol, date, open, close, high, low, volume,
                    pct_chg, ma5, ma10, ma20, ma60, rsi14,
                    limit_up, limit_down, volume_ratio, regime)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(
                    symbol, r["date"], r["open"], r["close"], r["high"], r["low"],
                    r.get("volume"), r.get("pct_chg"), r.get("ma5"), r.get("ma10"),
                    r.get("ma20"), r.get("ma60"), r.get("rsi14"),
                    r.get("limit_up", 0), r.get("limit_down", 0),
                    r.get("volume_ratio"), r.get("regime"),
                ) for r in rows],
            )
            return cur.rowcount if cur.rowcount > 0 else len(rows)

    def _log_build(self, symbol: str, start: str | None, end: str | None,
                   upserted: int, status: str, message: str = "") -> None:
        with get_connection(self.db_path) as conn:
            conn.execute(
                """INSERT INTO gold_build_log
                   (symbol, start_date, end_date, rows_upserted, status, message)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (symbol, start, end, upserted, status, message),
            )


def symbol_is_gem(symbol: str) -> bool:
    """创业板(30xxxx)/科创板(688xxx) 涨跌停 ±20%，其余 ±10%"""
    code = symbol[2:] if symbol[:2] in ("sh", "sz", "bj") else symbol
    return code.startswith("30") or code.startswith("688")


def _next_day(date_str: str) -> str:
    from datetime import date, timedelta

    y, m, d = (int(x) for x in date_str.split("-"))
    return (date(y, m, d) + timedelta(days=1)).isoformat()
