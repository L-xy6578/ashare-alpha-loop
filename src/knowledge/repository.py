"""
三角闭环选股体系 — 知识库读写层

封装工作流级操作，直接映射到三个角色的日常调用：

    调查员选股前 → KnowledgeBase.preflight()
    交易记录后   → KnowledgeBase.record(trade)
    评审员复盘   → KnowledgeBase.round_summary("R43")
    信号验证     → KnowledgeBase.cross_check("主线龙头持有")

用法:
    from src.knowledge.repository import KnowledgeBase
    kb = KnowledgeBase()                     # 默认使用 config.DB_PATH
    kb = KnowledgeBase("data/knowledge.db")  # 或显式指定
    kb.record(stock_code="600519", ...)
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .models import CorrectBook, MistakeBook, init_db
from ..config import DB_PATH, MARKET_DB_PATH
from ..db import get_connection
from ..logger import get_logger

log = get_logger(__name__)


def _to_market_symbol(code: str) -> str:
    """裸代码 → market.db 带市场前缀代码（sz002366 / sh600019）"""
    c = code.strip().lower()
    if c[:2] in ("sh", "sz", "bj"):
        return c
    if c.startswith("6"):
        return "sh" + c
    if c.startswith(("0", "3")):
        return "sz" + c
    if c.startswith(("4", "8")):
        return "bj" + c
    return c


@dataclass
class TradeRecord:
    """一笔完整的交易记录——自动判断归属"""
    stock_code: str
    stock_name: str
    entry_price: float
    exit_price: float
    buy_reason: str
    sell_reason: str
    strategy_tag: str
    round_number: str
    holding_days: int = 0
    source: str = "sim"        # sim=模拟盘推演 / real=真实成交

    @property
    def return_pct(self) -> float:
        return round((self.exit_price - self.entry_price) / self.entry_price * 100, 2)

    @property
    def is_profit(self) -> bool:
        return self.return_pct >= 0


@dataclass
class PreflightReport:
    """调查员选股前获取的情报"""
    recent_mistakes: list[dict] = field(default_factory=list)      # 最近错误
    mistake_patterns: list[dict] = field(default_factory=list)     # 高频失败模式
    best_strategies: list[dict] = field(default_factory=list)      # 当前有效策略
    total_trades: int = 0
    win_rate: float = 0.0
    by_source: dict = field(default_factory=dict)   # {"sim": {...}, "real": {...}}
    generated_at: str = ""

    @property
    def danger_signals(self) -> list[str]:
        """生成文字版避坑指南"""
        tips = []
        for m in self.mistake_patterns[:3]:
            tips.append(f"⚠️ 警惕「{m['type']}」(已出现{m['count']}次, 平均亏损{m['avg_loss']}%)")
        for s in self.best_strategies[:2]:
            tips.append(f"✅ 有效策略「{s['tag']}」({s['count']}次, 均收益+{s['avg_return']}%)")
        return tips


@dataclass
class RoundReport:
    """单轮复盘"""
    round_number: str
    correct_count: int = 0
    mistake_count: int = 0
    total_return: float = 0.0
    avg_profit: float = 0.0
    max_loss: float = 0.0
    mistakes: list[dict] = field(default_factory=list)
    summary: str = ""


class KnowledgeBase:
    """知识库统一入口"""

    def __init__(self, db_path: str | None = None, market_db: str | None = None):
        self.db = db_path or DB_PATH
        self.market_db = market_db or MARKET_DB_PATH
        init_db(self.db)  # 建表/迁移（幂等），保证 source 列存在

    # ================================================================
    # #80 行情一致性校验（WARN 不阻断）
    # ================================================================

    def _market_consistency(self, trade: TradeRecord) -> list[str]:
        """校验 entry/exit 价格与 market.db K线区间的一致性

        两层检查（都只产 WARN，不阻断写入）：
        1. 价格超出该标的全部历史K线 [min(low), max(high)] → 疑似录入错误/复权口径不一致
        2. 价格在历史区间内、但不在最近 90 个交易日区间 → 疑似久远回补
           记录或复权口径不一致（历史入场价出现在不匹配的日期区间）
        """
        try:
            with get_connection(self.market_db, readonly=True) as conn:
                sym = _to_market_symbol(trade.stock_code)
                rows = conn.execute(
                    "SELECT date, low, high FROM daily_bars "
                    "WHERE symbol IN (?, ?) ORDER BY date",
                    (sym, trade.stock_code),
                ).fetchall()
        except Exception as exc:
            return [f"行情校验跳过（无法读取行情库 {self.market_db}: {exc}）"]

        if not rows:
            return [f"行情校验跳过（market.db 无 {trade.stock_code} K线，无法核对价格）"]

        lows = [r["low"] for r in rows]
        highs = [r["high"] for r in rows]
        lo, hi = min(lows), max(highs)
        recent = rows[-90:]
        r_lo = min(r["low"] for r in recent)
        r_hi = max(r["high"] for r in recent)

        warns = []
        for label, price in (("entry_price", trade.entry_price),
                             ("exit_price", trade.exit_price)):
            if not (lo <= price <= hi):
                warns.append(
                    f"{label}={price} 超出 {trade.stock_code} 历史K线区间 "
                    f"[{lo}, {hi}]，疑似录入错误或复权口径不一致")
            elif not (r_lo <= price <= r_hi):
                warns.append(
                    f"{label}={price} 在历史区间内但不在近{len(recent)}个交易日区间 "
                    f"[{r_lo}, {r_hi}]，疑似久远回补记录或复权口径不一致，需人工核对")
        return warns

    # ================================================================
    # 写入：统一交易记录入口
    # ================================================================

    def record(
        self,
        stock_code: str,
        stock_name: str,
        entry_price: float,
        exit_price: float,
        buy_reason: str,
        sell_reason: str,
        strategy_tag: str,
        round_number: str,
        holding_days: int = 0,
        source: str = "sim",
        *,
        loss_root_cause: str = "",
        lesson_learned: str = "",
        mistake_type: str = "",
        force_mistake: bool = False,
        entry_date: str | None = None,
        exit_date: str | None = None,
    ) -> dict:
        """写入一条交易记录，自动根据盈亏路由到对题本或错题本

        盈利时：只需基础字段
        亏损时：建议补充 loss_root_cause / lesson_learned / mistake_type
        当 force_mistake=True 时强制写入错题本（评审员判定为错误但盈亏平衡时使用）
        entry_date/exit_date 同时提供时，自动计算 holding_days（自然日，≥1），
        覆盖手动传入的 holding_days（#82：字段系统性失真修复）
        """
        if entry_date and exit_date:
            try:
                d0 = date.fromisoformat(entry_date)
                d1 = date.fromisoformat(exit_date)
                if d1 >= d0:
                    holding_days = max(1, (d1 - d0).days)
                else:
                    log.warning("exit_date(%s) 早于 entry_date(%s)，holding_days 保持 %s",
                                exit_date, entry_date, holding_days)
            except ValueError:
                log.warning("日期格式非法（entry=%s exit=%s），holding_days 保持 %s",
                            entry_date, exit_date, holding_days)
        trade = TradeRecord(
            stock_code=stock_code,
            stock_name=stock_name,
            entry_price=entry_price,
            exit_price=exit_price,
            buy_reason=buy_reason,
            sell_reason=sell_reason,
            strategy_tag=strategy_tag,
            round_number=round_number,
            holding_days=holding_days,
            source=source,
        )

        if force_mistake or not trade.is_profit:
            m = MistakeBook.create(
                self.db,
                stock_code=trade.stock_code,
                stock_name=trade.stock_name,
                entry_price=trade.entry_price,
                exit_price=trade.exit_price,
                loss_pct=trade.return_pct,
                buy_reason=trade.buy_reason,
                loss_root_cause=loss_root_cause or "未分析",
                lesson_learned=lesson_learned or "待补充",
                mistake_type=mistake_type or "未分类",
                strategy_tag=trade.strategy_tag,
                round_number=trade.round_number,
                holding_days=trade.holding_days,
                source=trade.source,
            )
            log.warning("交易路由→错题本 %s(%s) 收益%s%% [轮次 %s]",
                        trade.stock_name, trade.stock_code, trade.return_pct, trade.round_number)
            warns = self._market_consistency(trade)
            for w in warns:
                log.warning("[#80行情校验] %s(%s) id=%s %s",
                            trade.stock_name, trade.stock_code, m.id, w)
            return {"book": "mistake_book", "id": m.id,
                    "return_pct": trade.return_pct, "market_warnings": warns}
        else:
            r = CorrectBook.create(
                self.db,
                stock_code=trade.stock_code,
                stock_name=trade.stock_name,
                entry_price=trade.entry_price,
                exit_price=trade.exit_price,
                profit_pct=trade.return_pct,
                buy_reason=trade.buy_reason,
                sell_reason=trade.sell_reason,
                strategy_tag=trade.strategy_tag,
                round_number=trade.round_number,
                holding_days=trade.holding_days,
                source=trade.source,
            )
            log.info("交易路由→对题本 %s(%s) 收益+%s%% [轮次 %s]",
                     trade.stock_name, trade.stock_code, trade.return_pct, trade.round_number)
            warns = self._market_consistency(trade)
            for w in warns:
                log.warning("[#80行情校验] %s(%s) id=%s %s",
                            trade.stock_name, trade.stock_code, r.id, w)
            return {"book": "correct_book", "id": r.id,
                    "return_pct": trade.return_pct, "market_warnings": warns}

    # ================================================================
    # 读取：调查员选前情报
    # ================================================================

    def preflight(self, lookback_days: int = 90) -> PreflightReport:
        """调查员选股前获取避坑情报"""
        recent_mistakes = MistakeBook.recent(self.db, limit=10, days=lookback_days)
        mistake_patterns = MistakeBook.top_causes(self.db, limit=5)
        cutoff = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        with get_connection(self.db, readonly=True) as conn:
            corrects = [dict(r) for r in conn.execute(
                """SELECT stock_code, strategy_tag, profit_pct, entry_price,
                          exit_price, source, created_at
                   FROM correct_book WHERE created_at >= ?""", (cutoff,)).fetchall()]
            mistakes = [dict(r) for r in conn.execute(
                """SELECT stock_code, mistake_type, loss_pct, entry_price,
                          exit_price, source, created_at
                   FROM mistake_book WHERE created_at >= ?""", (cutoff,)).fetchall()]

        # #27：去重（同代码同价保留最早一轮，R45/R46 重复不再虚高胜率）
        def _dedupe(rows: list[dict]) -> list[dict]:
            seen, out = set(), []
            for r in sorted(rows, key=lambda x: x["created_at"]):
                key = (r["stock_code"], r["entry_price"], r["exit_price"])
                if key in seen:
                    continue
                seen.add(key)
                out.append(r)
            return out

        corrects, mistakes = _dedupe(corrects), _dedupe(mistakes)
        by_source = {}
        for src in ("sim", "real"):
            c = sum(1 for r in corrects if r["source"] == src)
            m = sum(1 for r in mistakes if r["source"] == src)
            by_source[src] = {
                "correct": c,
                "mistake": m,
                "total": c + m,
                "win_rate": round(c / (c + m) * 100, 1) if c + m else 0.0,
            }

        total = len(corrects) + len(mistakes)
        win_rate = round(len(corrects) / total * 100, 1) if total else 0.0
        # 有效策略（去重后的对题本）
        tag_stats: dict[str, list[float]] = {}
        for r in corrects:
            tag_stats.setdefault(r["strategy_tag"], []).append(r["profit_pct"])
        best_strategies = sorted(
            ({"tag": t, "count": len(p), "avg_return": round(sum(p) / len(p), 2)}
             for t, p in tag_stats.items()),
            key=lambda x: -x["count"],
        )[:3]

        report = PreflightReport(
            recent_mistakes=recent_mistakes,
            mistake_patterns=mistake_patterns,
            best_strategies=best_strategies,
            total_trades=total,
            win_rate=win_rate,
            by_source=by_source,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        )
        log.info("生成选前情报: 总交易%d 胜率%s%% 分口径%s 避坑信号%d条",
                 total, win_rate, by_source, len(report.danger_signals))
        return report

    # ================================================================
    # 复盘：评审员轮次总结
    # ================================================================

    def round_summary(self, round_number: str, source: str | None = None) -> RoundReport:
        """评审员复盘某一轮（可按 sim/real 口径过滤）"""
        with get_connection(self.db, readonly=True) as conn:
            if source:
                correct_rows = conn.execute(
                    "SELECT * FROM correct_book WHERE round_number = ? AND source = ?",
                    (round_number, source),
                ).fetchall()
                mistake_rows = conn.execute(
                    "SELECT * FROM mistake_book WHERE round_number = ? AND source = ?",
                    (round_number, source),
                ).fetchall()
            else:
                correct_rows = conn.execute(
                    "SELECT * FROM correct_book WHERE round_number = ?",
                    (round_number,),
                ).fetchall()
                mistake_rows = conn.execute(
                    "SELECT * FROM mistake_book WHERE round_number = ?",
                    (round_number,),
                ).fetchall()

        corrects = [dict(r) for r in correct_rows]
        mistakes = [dict(r) for r in mistake_rows]
        corrections = [r["profit_pct"] for r in corrects]
        losses = [r["loss_pct"] for r in mistakes]

        total = sum(corrections) + sum(losses)
        avg_profit = round(sum(corrections) / len(corrections), 2) if corrections else 0
        max_loss = round(min(losses), 2) if losses else 0

        # 生成文字总结
        parts = [f"## {round_number} 复盘"]
        parts.append(f"- 正确 {len(corrects)} 笔, 错误 {len(mistakes)} 笔")
        parts.append(f"- 平均盈利 +{avg_profit}%, 最大亏损 {max_loss}%")
        parts.append(f"- 合计收益 {round(total, 2)}%")
        if mistakes:
            types = set(m["mistake_type"] for m in mistakes)
            parts.append(f"- 失败类型: {'、'.join(types)}")

        log.info("复盘 %s: 正确%d 错误%d 合计%s%%",
                 round_number, len(corrects), len(mistakes), round(total, 2))
        return RoundReport(
            round_number=round_number,
            correct_count=len(corrects),
            mistake_count=len(mistakes),
            total_return=round(total, 2),
            avg_profit=avg_profit,
            max_loss=max_loss,
            mistakes=mistakes,
            summary="\n".join(parts),
        )

    # ================================================================
    # 预警：信号交叉验证
    # ================================================================

    def cross_check(self, strategy_tag: str) -> dict:
        """用历史数据交叉验证某个策略在当前是否安全

        返回:
            {"safe": bool, "risk_signals": [...], "historical_return": float}

        口径（#58，与收益报表一致）：
            - 对题本+错题本合并，按（代码,入场价,出场价）去重（同报表 dedupe）
            - 历史收益 = 两本合并算术平均（含亏损，不再只算对题本虚高）
            - 错题匹配用 strategy_tag 列（不用 buy_reason LIKE，避免漏配/误配）
        """
        with get_connection(self.db, readonly=True) as conn:
            rows = conn.execute(
                """SELECT 'correct' AS book, stock_code, entry_price, exit_price,
                          profit_pct AS pnl
                   FROM correct_book WHERE strategy_tag = ?
                   UNION ALL
                   SELECT 'mistake' AS book, stock_code, entry_price, exit_price,
                          loss_pct AS pnl
                   FROM mistake_book WHERE strategy_tag = ?""",
                (strategy_tag, strategy_tag),
            ).fetchall()

        seen: dict[tuple, tuple[str, float]] = {}
        for book, code, entry, exit_, pnl in rows:
            key = (code, entry, exit_)
            if key not in seen:
                seen[key] = (book, float(pnl))
        pnls = [v[1] for v in seen.values()]
        count = len(pnls)
        mistake_cnt = sum(1 for b, _ in seen.values() if b == "mistake")
        avg_return = round(sum(pnls) / count, 2) if count else 0.0
        safe = count >= 3 and avg_return > 0 and mistake_cnt == 0

        risks = []
        if mistake_cnt > 0:
            risks.append(f"该策略历史上出现过 {mistake_cnt} 次失败")
        if count < 3:
            risks.append(f"样本不足（仅 {count} 次），可信度低")
        if avg_return <= 0:
            risks.append(f"历史平均收益 {avg_return}%，策略可能失效")

        log.info("信号交叉验证 %s: safe=%s 样本%d 历史收益%s%%",
                 strategy_tag, safe, count, avg_return)
        return {
            "strategy_tag": strategy_tag,
            "safe": safe,
            "risk_signals": risks,
            "sample_count": count,
            "historical_return": avg_return,
        }
