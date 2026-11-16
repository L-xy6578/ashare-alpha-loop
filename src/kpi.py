"""
KPI-07 选股准确率自动检票（08-24 首次评估准备）

规则（2026-08-05 修正版）：推荐股票自推荐日起第 22 个交易日（休市日不计，
截止日动态计算）内实际涨幅 ≥ 目标涨幅即达标；选股准确率 = 达标数 / 已评估数，目标 >60%。

能力：
    - 推荐登记：round/代码/推荐日收盘价/目标涨幅 → 自动算截止日（交易日历）
    - 到期检票：截止日收盘价（market.db 优先，缺失自动补拉腾讯）→ 达标/未达标
    - 数据完整性门禁：截止日无行情数据 → 状态 pending_data，成绩单标注"结论无效"，不算入分母
    - 首次成绩单：达标/总数 + 命中率，输出 outputs/收盘报表/KPI07评估_YYYY-MM-DD.md

用法:
    python -m src.kpi --seed      # 迁移手工跟踪表（宝钢，截止 08-24）
    python -m src.kpi --status    # 查看跟踪状态
    python -m src.kpi --eval --report   # 到期自动检票 + 生成成绩单（收盘链自动调用）
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from src.config import DB_PATH, MARKET_DB_PATH
from src.data_source import TdxDataSource, TencentHttpDataSource
from src.db import get_connection
from src.logger import get_logger

log = get_logger(__name__)

TRACKING_DAYS = 22            # 推荐日起第 22 个交易日（含推荐日为第 1 个）
ACCURACY_TARGET = 60.0        # 选股准确率目标 %
MIN_EVAL_SAMPLES = 3          # 首评保障：已评估 <3 笔不作体系结论（样本不足无统计意义）

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kpi_tracking (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    round_number TEXT NOT NULL,
    stock_code   TEXT NOT NULL,
    stock_name   TEXT NOT NULL,
    rec_date     TEXT NOT NULL,          -- 推荐日（必须为交易日）
    rec_close    REAL NOT NULL,          -- 推荐日收盘价
    target_pct   REAL NOT NULL,          -- 目标涨幅 %
    target_price REAL NOT NULL,          -- 目标价
    deadline     TEXT NOT NULL,          -- 截止日 = 推荐日起第22个交易日（动态计算）
    status       TEXT NOT NULL DEFAULT 'tracking',  -- tracking / hit / miss / pending_data
    eval_date    TEXT,
    eval_close   REAL,
    actual_pct   REAL,
    source       TEXT NOT NULL DEFAULT 'real',      -- sim / real（成绩单只认 real）
    note         TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kpi_status ON kpi_tracking(status, deadline);
"""

# 手工跟踪表迁移种子（R48-R52，2026-07-24 推荐）
MANUAL_SEEDS = [
    ("R48-R52", "600019", "宝钢股份", "2026-07-24", 5.91, 15.0, 6.80,
     "手工跟踪表迁移（V1.1 口径：截止日=第22个交易日）"),
]


def migrate_source_column(db_path: str = DB_PATH) -> int:
    """迁移 kpi_tracking 增加 source 列（sim/real，幂等可重复执行）

    历史样本（迁移前已存在的记录）全部来自模拟盘推演/手工跟踪 → 标注 sim；
    迁移完成后新登记的真实盘样本才会计入真实口径成绩单。

    Returns:
        本次迁移标注为 sim 的历史行数（已迁移过则返回 0）
    """
    with get_connection(db_path) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(kpi_tracking)").fetchall()]
        if "source" in cols:
            return 0
        conn.execute("ALTER TABLE kpi_tracking ADD COLUMN source TEXT NOT NULL DEFAULT 'real'")
        cur = conn.execute("UPDATE kpi_tracking SET source='sim'")
        n = cur.rowcount
    log.info("KPI-07 迁移：kpi_tracking 新增 source 列，历史 %d 条标注 sim", n)
    return n


def init_kpi_db(db_path: str = DB_PATH) -> str:
    with get_connection(db_path) as conn:
        conn.executescript(_SCHEMA)
    migrate_source_column(db_path)
    return db_path


def deadline_date(rec_date: str, n: int = TRACKING_DAYS,
                  market_db: str = MARKET_DB_PATH) -> str | None:
    """推荐日起第 n 个交易日（含推荐日为第 1 个）"""
    try:
        with get_connection(market_db, readonly=True) as conn:
            rows = conn.execute(
                """SELECT date FROM trade_calendar
                   WHERE is_trading=1 AND date >= ? ORDER BY date LIMIT ?""",
                (rec_date, n),
            ).fetchall()
    except Exception:
        return None
    return rows[-1][0] if len(rows) >= n else None


class KpiTracker:
    def __init__(self, db_path: str = DB_PATH, market_db: str = MARKET_DB_PATH):
        self.db = db_path
        self.market_db = market_db
        init_kpi_db(db_path)

    def seed_manual(self) -> int:
        """迁移手工跟踪表记录（表空时才写入）"""
        with get_connection(self.db, readonly=True) as conn:
            n = conn.execute("SELECT COUNT(*) FROM kpi_tracking").fetchone()[0]
        if n > 0:
            return 0
        from datetime import datetime

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        added = 0
        with get_connection(self.db) as conn:
            for rnd, code, name, rec_date, rec_close, target_pct, target_price, note in MANUAL_SEEDS:
                dl = deadline_date(rec_date, market_db=self.market_db)
                if dl is None:
                    log.warning("无法计算 %s 截止日（交易日历缺数据）", code)
                    continue
                conn.execute(
                    """INSERT INTO kpi_tracking
                       (round_number, stock_code, stock_name, rec_date, rec_close,
                        target_pct, target_price, deadline, status, source, note, created_at)
                       VALUES (?,?,?,?,?,?,?,?,'tracking','sim',?,?)""",
                    (rnd, code, name, rec_date, rec_close, target_pct,
                     target_price, dl, note, now),
                )
                added += 1
        log.info("KPI-07 手工记录迁移 %d 条（截止日=第%d个交易日）", added, TRACKING_DAYS)
        return added

    def register_recommendation(
        self,
        round_number: str,
        code: str,
        name: str,
        rec_date: str,
        rec_close: float,
        target_pct: float = 20.0,
        note: str = "闭环自动登记（五因子≥85 推荐）",
        source: str = "sim",
    ) -> str | None:
        """推荐自动登记（洞1：KPI-07 测量管道接通）

        五因子 ≥85 的候选在闭环内自动登记为 KPI-07 样本：
        截止日 = 推荐日起第 22 个交易日（动态计算，休市日不计）。
        幂等：同轮次同标的同推荐日不重复登记。
        source: sim（模拟盘推演，默认）/ real（真实盘；成绩单只认真实口径）

        Returns:
            截止日 YYYY-MM-DD；登记失败返回 None
        """
        dl = deadline_date(rec_date, market_db=self.market_db)
        if dl is None:
            log.warning("KPI-07 登记失败 %s：交易日历缺 %s 后续数据", code, rec_date)
            return None
        with get_connection(self.db, readonly=True) as conn:
            n = conn.execute(
                """SELECT COUNT(*) FROM kpi_tracking
                   WHERE round_number=? AND stock_code=? AND rec_date=?""",
                (round_number, code, rec_date),
            ).fetchone()[0]
        if n:
            return None
        from datetime import datetime

        target_price = round(rec_close * (1 + target_pct / 100), 2)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with get_connection(self.db) as conn:
            conn.execute(
                """INSERT INTO kpi_tracking
                   (round_number, stock_code, stock_name, rec_date, rec_close,
                    target_pct, target_price, deadline, status, source, note, created_at)
                   VALUES (?,?,?,?,?,?,?,?,'tracking',?,?,?)""",
                (round_number, code, name, rec_date, rec_close,
                 target_pct, target_price, dl, source, note, now),
            )
        log.info("KPI-07 自动登记推荐 %s(%s) %s 收%.2f → 截止%s（%s 口径）",
                 name, code, rec_date, rec_close, dl, source)
        return dl

    def all(self, status: str | None = None) -> list[dict]:
        with get_connection(self.db, readonly=True) as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM kpi_tracking WHERE status=? ORDER BY deadline",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM kpi_tracking ORDER BY deadline"
                ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _resolve_close(candidates: list[tuple[str, float]]) -> float | None:
        """多源收盘价核对：全部来源一致（容差 0.5%）才采用；不一致视为数据不可信"""
        if not candidates:
            return None
        base = candidates[0][1]
        for _, v in candidates[1:]:
            if base and abs(v - base) / base > 0.005:
                log.warning("KPI 检票多源不一致: %s", candidates)
                return None
        return base

    def _close_on(self, stock_code: str, day: str,
                  providers: list | None = None) -> float | None:
        """截止日收盘价：market.db + 网络同口径源核对（#84）

        stock_code 为 6 位裸代码（如 000519），需补 sh/sz 前缀再查库/调接口。
        providers: 网络源类列表（默认腾讯+通达信）；测试可传 [] 隔离网络。
        口径：只与库内 adjust 相同的源比对（qfq↔腾讯 / raw↔通达信），
        避免前复权/不复权混比把一致的价格误判为不一致。
        """
        prefix = "sh" if stock_code.startswith("6") else (
            "bj" if stock_code.startswith(("4", "8")) else "sz"
        )
        symbol = prefix + stock_code
        candidates: list[tuple[str, float]] = []
        stored_adjust = None
        with get_connection(self.market_db, readonly=True) as conn:
            row = conn.execute(
                "SELECT close, adjust FROM daily_bars WHERE symbol=? AND date=?",
                (symbol, day),
            ).fetchone()
        if row:
            candidates.append(("market", row[0]))
            stored_adjust = row[1]
        for provider_cls in (providers if providers is not None
                             else (TencentHttpDataSource, TdxDataSource)):
            inst = provider_cls()
            # 库内口径已知 → 只比对同口径源；库内未知 → 按前复权(qfq)保守处理
            if stored_adjust:
                if inst.adjust != stored_adjust:
                    continue
            elif inst.adjust != "qfq":
                continue
            try:
                bars = inst.daily_kline(symbol, day, day)
                if bars:
                    candidates.append((provider_cls.__name__, bars[-1].close))
            except Exception:
                continue
        return self._resolve_close(candidates)

    def evaluate_due(self, today: str | None = None,
                     close_providers: list | None = None) -> list[dict]:
        """到期检票：截止日 ≤ 今天且仍在跟踪的记录"""
        today = today or date.today().isoformat()
        results = []
        # tracking + pending_data（上次拉取失败的重试）
        due = [r for r in self.all(status="tracking") + self.all(status="pending_data")
               if r["deadline"] <= today]
        for item in due:
            close = self._close_on(item["stock_code"], item["deadline"],
                                   providers=close_providers)
            if close is None:
                self._update(item["id"], status="pending_data",
                             note="截止日无行情数据，结论无效（不计入分母）")
                results.append({**item, "status": "pending_data"})
                continue
            actual = round((close / item["rec_close"] - 1) * 100, 2)
            status = "hit" if actual >= item["target_pct"] else "miss"
            self._update(item["id"], status=status, eval_date=item["deadline"],
                         eval_close=close, actual_pct=actual,
                         note="达标" if status == "hit" else "未达标")
            results.append({**item, "status": status, "eval_close": close,
                            "actual_pct": actual})
        return results

    def _update(self, kpi_id: int, **fields) -> None:
        keys = ", ".join(f"{k}=?" for k in fields)
        with get_connection(self.db) as conn:
            conn.execute(f"UPDATE kpi_tracking SET {keys} WHERE id=?",
                         (*fields.values(), kpi_id))

    def accuracy(self) -> dict:
        """已评估命中率（真实盘口径：只统计 source=real；模拟盘推演不计入）"""
        rows = self.all()
        real = [r for r in rows if (r.get("source") or "real") == "real"]
        sim = [r for r in rows if (r.get("source") or "real") == "sim"]
        evaluated = [r for r in real if r["status"] in ("hit", "miss")]
        hits = sum(1 for r in evaluated if r["status"] == "hit")
        return {
            "evaluated": len(evaluated),
            "hits": hits,
            "accuracy": round(hits / len(evaluated) * 100, 1) if evaluated else None,
            "pending_data": sum(1 for r in real if r["status"] == "pending_data"),
            "tracking": sum(1 for r in real if r["status"] == "tracking"),
            "sim_total": len(sim),
            "sim_evaluated": sum(1 for r in sim if r["status"] in ("hit", "miss")),
        }

    def render_report(self, today: str | None = None) -> str:
        today = today or date.today().isoformat()
        acc = self.accuracy()
        lines = [f"# KPI-07 选股准确率评估（真实盘口径 · {today}）", ""]
        lines.append("> 规则：推荐日起第 22 个交易日（休市日不计）内实际涨幅≥目标涨幅即达标；"
                     "截止日动态计算；数据缺失的记录标注结论无效，不计入分母。")
        lines.append("> 口径：**只统计 source=real 的真实盘样本**；模拟盘推演样本单独展示，"
                     "不计入分子/分母。")
        lines.append("")
        lines.append("| 轮次 | 标的 | 口径 | 推荐日 | 推荐价 | 目标 | 截止日 | 截止收盘 | 实际涨幅 | 达标 |")
        lines.append("|------|------|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|")
        for r in self.all():
            status_cn = {
                "tracking": "⏳跟踪中", "hit": "✅达标", "miss": "❌未达标",
                "pending_data": "⚠️数据缺失",
            }.get(r["status"], r["status"])
            actual = f"{r['actual_pct']:+.2f}%" if r["actual_pct"] is not None else "—"
            eval_close_txt = (f"{r['eval_close']:.2f}"
                              if r["eval_close"] is not None else "—")
            lines.append(
                f"| {r['round_number']} | {r['stock_name']} | "
                f"{r.get('source') or 'real'} | {r['rec_date']} | "
                f"{r['rec_close']:.2f} | +{r['target_pct']:.0f}% | {r['deadline']} | "
                f"{eval_close_txt} | "
                f"{actual} | {status_cn} |"
            )
        lines.append("")
        if acc["accuracy"] is None:
            lines.append(f"**选股准确率（真实盘）：待评估**（真实盘已评估 0 笔，"
                         f"跟踪中 {acc['tracking']} 笔；模拟盘 {acc['sim_total']} 笔不计入）")
        elif acc["evaluated"] < MIN_EVAL_SAMPLES:
            # 首评保障：单票/少量样本无统计意义，只报数字不下体系结论
            lines.append(
                f"**选股准确率（真实盘）：{acc['hits']}/{acc['evaluated']} = {acc['accuracy']}%**"
                f"（目标 >{ACCURACY_TARGET:.0f}%；⚠️ 样本不足 {acc['evaluated']} 笔，"
                f"暂不作体系结论，待积累）"
            )
        else:
            flag = "✅达标" if acc["accuracy"] >= ACCURACY_TARGET else "❌未达标"
            lines.append(
                f"**选股准确率（真实盘）：{acc['hits']}/{acc['evaluated']} = {acc['accuracy']}%**"
                f"（目标 >{ACCURACY_TARGET:.0f}%，{flag}）"
            )
        if acc["evaluated"] and acc["evaluated"] < MIN_EVAL_SAMPLES:
            lines.append(
                "⚠️ 单票/少量样本结果不代表体系能力：请结合多轮样本评估，"
                "勿把单只股票涨跌读成体系成败。"
            )
        if acc["sim_evaluated"] or acc["sim_total"]:
            lines.append(f"ℹ️ 模拟盘推演样本 {acc['sim_total']} 笔"
                         f"（已评估 {acc['sim_evaluated']} 笔）单独展示，不计入真实口径。")
        if acc["pending_data"]:
            lines.append(f"⚠️ {acc['pending_data']} 笔因数据缺失未计入（结论无效）")
        return "\n".join(lines) + "\n"


def _main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="KPI-07 选股准确率自动检票")
    parser.add_argument("--seed", action="store_true", help="迁移手工跟踪表记录")
    parser.add_argument("--migrate-source", action="store_true",
                        help="迁移 kpi_tracking 增加 source 列（历史样本标注 sim，幂等）")
    parser.add_argument("--status", action="store_true", help="查看跟踪状态")
    parser.add_argument("--eval", action="store_true", help="到期自动检票")
    parser.add_argument("--report", action="store_true", help="生成成绩单（需 --eval）")
    parser.add_argument("--today", default=None, help="评估日 YYYY-MM-DD（测试用）")
    args = parser.parse_args()

    if args.migrate_source:
        n = migrate_source_column()
        print(f"✅ source 列迁移完成（本次标注 sim 的历史行 {n} 条）" if n
              else "ℹ️ source 列已存在，无需迁移")
    kpi = KpiTracker()
    if args.seed:
        n = kpi.seed_manual()
        print(f"✅ 迁移 {n} 条" if n else "ℹ️ 已存在，跳过")
    if args.status or not (args.seed or args.eval or args.migrate_source):
        for r in kpi.all():
            print(f"  [{r['status']}] [{r.get('source') or 'real'}] "
                  f"{r['stock_name']}({r['stock_code']}) "
                  f"推荐{r['rec_close']:.2f} → 目标+{r['target_pct']:.0f}% 截止{r['deadline']}")
    if args.eval:
        results = kpi.evaluate_due(today=args.today)
        for r in results:
            print(f"  检票 {r['stock_name']}: {r['status']} "
                  f"(实际 {r.get('actual_pct')}%)")
        if args.report:
            md = kpi.render_report(today=args.today)
            out = Path("outputs") / "收盘报表" / f"KPI07评估_{args.today or date.today().isoformat()}.md"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(md, encoding="utf-8")
            print(f"📄 成绩单已生成: {out}")


if __name__ == "__main__":
    _main()
