"""赛道分析模块 — 月度/季度主赛道 + 细分赛道,全部数据驱动

数据来源:westock-mcp data_sector(mode=ranking)返回的板块快照,包含:
  - 当日涨幅 zdf / 5日 zdf5 / 20日 zdf20 / 60日 zdf60
  - 主力资金:当日 zljlr / 5日 zljlr_d5 / 20日 zljlr_d20
  - 换手率 hsl

设计原则(中心思想落地):
  - 不预测赛道,只测量赛道——用已发生的涨幅/资金数据排序
  - 月度主赛道 = 20日强度,季度主赛道 = 60日强度
  - 评分全部来自数据,无人工填分
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_cls

from .config import MARKET_DB_PATH
from .db import get_connection
from .logger import get_logger

log = get_logger(__name__)

# 板块表结构
SECTOR_TABLE = """
CREATE TABLE IF NOT EXISTS sector_daily (
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,          -- plate(行业) | concept(概念)
    zdf REAL DEFAULT 0,          -- 当日涨幅 %
    zdf5 REAL DEFAULT 0,         -- 5日涨幅 %
    zdf20 REAL DEFAULT 0,        -- 20日涨幅 %
    zdf60 REAL DEFAULT 0,        -- 60日涨幅 %
    zljlr REAL DEFAULT 0,        -- 当日主力净流入(万元)
    zljlr_d5 REAL DEFAULT 0,     -- 5日主力净流入(万元)
    zljlr_d20 REAL,              -- 20日主力净流入(万元)；NULL=无数据/窗口不完整，0=真实零净流入
    hsl REAL DEFAULT 0,          -- 换手率 %
    level REAL DEFAULT 0,        -- 板块指数点位（自积累用）
    source TEXT NOT NULL DEFAULT '',  -- eastmoney / tencent_fallback / westock / other
    PRIMARY KEY (date, code)
)
"""

# 每日快照元信息（数据源 + 条数 + 是否完整），供因子层做数据源门禁
SECTOR_META_TABLE = """
CREATE TABLE IF NOT EXISTS sector_snapshot_meta (
    date TEXT PRIMARY KEY,
    source TEXT NOT NULL DEFAULT '',
    plate_count INTEGER NOT NULL DEFAULT 0,
    concept_count INTEGER NOT NULL DEFAULT 0,
    complete INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT ''
)
"""

# 快照完整性阈值（东财全量：行业 ~490+、概念 ~500+）。
# 低于阈值视为分页失败/残缺，整日不落库（注入层硬门禁）。
MIN_PLATE_BOARDS = 400
MIN_CONCEPT_BOARDS = 400

# 板块指数单日变动上限（A股涨跌幅上限 20%，留 1pp 容差）。
# 超过则视为点位/涨幅字段不自洽，该板块不参与赛道与分位基准。
LEVEL_JUMP_LIMIT = 21.0

# 回算窗口：20/60 日涨幅按交易日历回看（不是按库内行数）
ZDF20_TRADING_DAYS = 20
ZDF60_TRADING_DAYS = 60



@dataclass
class SectorSnapshot:
    """单个板块某日快照

    zdf20 / zdf60 可为 None：2026-11-10 起由板块历史表按交易日历计算，
    历史不足时写 NULL 表示"无真值"，绝不沿用上一次的旧值冒充当日值。
    """
    code: str
    name: str
    kind: str = "plate"          # plate | concept
    date: str = ""
    zdf: float = 0.0
    zdf5: float = 0.0
    zdf20: float | None = None
    zdf60: float | None = None
    zljlr: float = 0.0
    zljlr_d5: float = 0.0
    zljlr_d20: float | None = None   # None=无数据（20 日窗口不完整）；0=真实零净流入
    hsl: float = 0.0
    level: float = 0.0           # 板块指数点位
    source: str = ""             # eastmoney / tencent_fallback / westock / other

    def __post_init__(self):
        for f in ("zdf", "zdf5", "zljlr", "zljlr_d5", "hsl", "level"):
            try:
                setattr(self, f, float(getattr(self, f) or 0))
            except (TypeError, ValueError):
                setattr(self, f, 0.0)
        # zdf20/zdf60/zljlr_d20 可为 None：保留"无数据"，不能把 None 强转成 0
        for f in ("zdf20", "zdf60", "zljlr_d20"):
            v = getattr(self, f)
            if v is None:
                continue
            try:
                setattr(self, f, float(v))
            except (TypeError, ValueError):
                setattr(self, f, None)
        if not self.date:
            self.date = date_cls.today().isoformat()


@dataclass
class TrackResult:
    """赛道评分结果"""
    name: str
    code: str
    kind: str
    score: float            # 赛道评分 0-100
    zdf20: float
    zdf60: float
    zljlr_d20: float | None
    rank_20: int            # 20日涨幅排名
    rank_60: int            # 60日涨幅排名

    def __str__(self):
        fund = "无数据" if self.zljlr_d20 is None else f"{self.zljlr_d20/10000:+.1f}亿"
        return (f"{self.name}({self.code}) {self.score:.1f}分 | "
                f"20日{self.zdf20:+.1f}% 60日{self.zdf60:+.1f}% "
                f"20日资金{fund} | 排名20/60: {self.rank_20}/{self.rank_60}")


class SectorAnalyzer:
    """赛道分析器:注入板块快照 → 月度/季度主赛道 + 细分"""

    def __init__(self, db_path: str = MARKET_DB_PATH):
        self.db_path = db_path
        self._init_schema()

    def _init_schema(self):
        with get_connection(self.db_path) as conn:
            conn.execute(SECTOR_TABLE)
            conn.execute(SECTOR_META_TABLE)
            # 旧表迁移：补 level 列（自积累指数点位）
            cols = [r[1] for r in conn.execute("PRAGMA table_info(sector_daily)").fetchall()]
            if "level" not in cols:
                conn.execute(
                    "ALTER TABLE sector_daily ADD COLUMN level REAL DEFAULT 0"
                )
                log.info("板块表迁移: sector_daily 新增 level 列（指数点位自积累）")
            # 旧表迁移：补 source 列（数据源，因子门禁用；幂等）
            if "source" not in cols:
                conn.execute(
                    "ALTER TABLE sector_daily ADD COLUMN source TEXT NOT NULL DEFAULT ''"
                )
                log.info("板块表迁移: sector_daily 新增 source 列（数据源）")
            # 幂等回填：pt_=腾讯兜底快照，bk_=东财，其余=other
            conn.execute(
                "UPDATE sector_daily SET source='tencent_fallback' "
                "WHERE source='' AND code LIKE 'pt_%'"
            )
            conn.execute(
                "UPDATE sector_daily SET source='eastmoney' "
                "WHERE source='' AND code LIKE 'bk_%'"
            )
            conn.execute(
                "UPDATE sector_daily SET source='other' WHERE source=''"
            )

    # ---------- 写入 ----------
    def save_snapshot(self, snapshots: list[SectorSnapshot]) -> int:
        """批量保存板块快照(upsert)"""
        if not snapshots:
            return 0
        n = 0
        with get_connection(self.db_path) as conn:
            for s in snapshots:
                conn.execute(
                    """INSERT INTO sector_daily
                       (date, code, name, kind, zdf, zdf5, zdf20, zdf60,
                        zljlr, zljlr_d5, zljlr_d20, hsl, level, source)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(date, code) DO UPDATE SET
                         name=excluded.name, zdf=excluded.zdf, zdf5=excluded.zdf5,
                         zdf20=excluded.zdf20, zdf60=excluded.zdf60,
                         zljlr=excluded.zljlr, zljlr_d5=excluded.zljlr_d5,
                         zljlr_d20=excluded.zljlr_d20, hsl=excluded.hsl,
                         level=excluded.level,
                         source=CASE WHEN excluded.source='' THEN source
                                     ELSE excluded.source END""",
                    (s.date, s.code, s.name, s.kind, s.zdf, s.zdf5, s.zdf20, s.zdf60,
                     s.zljlr, s.zljlr_d5, s.zljlr_d20, s.hsl, s.level, s.source),
                )
                n += 1
        log.info("保存板块快照 %d 条 (date=%s)", n, snapshots[0].date)
        return n

    def from_westock_json(self, payload: dict, trade_date: str | None = None) -> int:
        """从 westock data_sector(mode=ranking) 的返回解析并保存

        参数:payload 为 data_sector 返回的 JSON dict
        """
        date_str = trade_date or date_cls.today().isoformat()
        snapshots: list[SectorSnapshot] = []

        # rank 榜单:plate/concept 每项含 bd_code/bd_name/bd_zdf/bd_zdf5/bd_zdf20/bd_zdf60/bd_hsl
        rank = (payload or {}).get("rank", {})
        seen = set()
        for kind in ("plate", "concept"):
            for bucket in (kind, f"{kind}_zdf5", f"{kind}_zdf20", f"{kind}_zdf60",
                           f"{kind}_zdfY", f"{kind}_zdfW52", f"{kind}_zs",
                           f"{kind}_hsl", f"{kind}_lb"):
                for item in rank.get(bucket, []):
                    code = item.get("bd_code")
                    if not code or (code, kind) in seen:
                        continue
                    seen.add((code, kind))
                    snapshots.append(SectorSnapshot(
                        code=code,
                        name=item.get("bd_name", code),
                        kind=kind,
                        date=date_str,
                        zdf=item.get("bd_zdf", 0),
                        zdf5=item.get("bd_zdf5", 0),
                        zdf20=item.get("bd_zdf20", 0),
                        zdf60=item.get("bd_zdf60", 0),
                        hsl=item.get("bd_hsl", 0),
                        source="westock",
                    ))

        # fundflow:板块主力资金(zljlr 当日/zljlr_d5/zljlr_d20)
        ff = ((payload or {}).get("fundflow") or {}).get("plate", {})
        for bucket in ("top", "bottom"):
            for item in ff.get(bucket, []):
                code = item.get("code")
                if not code:
                    continue
                # 找到已建的快照补资金
                match = next((s for s in snapshots if s.code == code), None)
                if match:
                    match.zljlr = item.get("zljlr", 0)
                    match.zljlr_d5 = item.get("zljlr_d5", 0)
                    match.zljlr_d20 = item.get("zljlr_d20")

        if not snapshots:
            log.warning("westock payload 未解析出板块,检查返回结构")
            return 0
        return self.save_snapshot(snapshots)

    # ---------- 读取 ----------
    def load_date(self, trade_date: str | None = None, kind: str = "plate",
                  exclude_suspicious: bool = False) -> list[SectorSnapshot]:
        """读取某日全部板块快照(默认最新日期)

        Args:
            exclude_suspicious: True 时剔除点位/涨幅字段不自洽的可疑板块
                （这些板块不参与赛道与分位基准，见 suspicious_codes）
        """
        with get_connection(self.db_path, readonly=True) as conn:
            if trade_date:
                rows = conn.execute(
                    "SELECT * FROM sector_daily WHERE kind=? AND date=?",
                    (kind, trade_date),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM sector_daily WHERE kind=? AND date =
                       (SELECT MAX(date) FROM sector_daily WHERE kind=?)""",
                    (kind, kind),
                ).fetchall()
            cols = [d[0] for d in conn.execute("SELECT * FROM sector_daily LIMIT 0").description]
        out = []
        for r in rows:
            d = dict(zip(cols, r))
            out.append(SectorSnapshot(
                code=d["code"], name=d["name"], kind=d["kind"], date=d["date"],
                zdf=d["zdf"], zdf5=d["zdf5"], zdf20=d["zdf20"], zdf60=d["zdf60"],
                zljlr=d["zljlr"], zljlr_d5=d["zljlr_d5"], zljlr_d20=d["zljlr_d20"],
                hsl=d["hsl"], level=d.get("level") or 0.0,
                source=d.get("source") or "",
            ))
        if exclude_suspicious and out:
            day = trade_date or out[0].date
            bad = self.suspicious_codes(day)
            if bad:
                out = [s for s in out if s.code not in bad]
                log.warning("剔除可疑板块 %d 个（点位与涨幅字段不自洽，%s）：%s",
                            len(bad), day, "、".join(sorted(bad)[:5]))
        return out

    def refresh_zdf_from_levels(self, trade_date: str | None = None) -> int:
        """用库内累积的板块指数点位计算 20/60 日涨幅（自给自足，不依赖历史接口）

        2026-10-30 口径修正：窗口按**交易日历**回看 20/60 个交易日，不再按库内
        行数（历史快照有缺日时，按行数会把 35 个交易日的涨幅标成"20 日涨幅"）。
        点位/涨幅不自洽的可疑板块跳过回算。

        Returns:
            已回写 20/60 日涨幅的行数
        """
        with get_connection(self.db_path, readonly=True) as conn:
            day = trade_date
            if not day:
                row = conn.execute(
                    "SELECT MAX(date) FROM sector_daily WHERE kind IN ('plate','concept')"
                ).fetchone()
                day = row[0] if row else None
            if not day:
                return 0
            base20 = self._trading_days_before(conn, day, ZDF20_TRADING_DAYS)
            base60 = self._trading_days_before(conn, day, ZDF60_TRADING_DAYS)
            if base20 is None and base60 is None:
                log.warning("交易日历不足，跳过 20/60 日涨幅回算（%s）", day)
                return 0
            rows = conn.execute(
                "SELECT code, date, level FROM sector_daily "
                "WHERE kind IN ('plate','concept') AND date=?",   # #104：概念板块自积累
                (day,),
            ).fetchall()
            if not rows:
                return 0
            level20 = {
                r[0]: r[1] for r in conn.execute(
                    "SELECT code, level FROM sector_daily WHERE date=? AND level>0 "
                    "AND kind IN ('plate','concept')", (base20,),
                ).fetchall()
            } if base20 else {}
            level60 = {
                r[0]: r[1] for r in conn.execute(
                    "SELECT code, level FROM sector_daily WHERE date=? AND level>0 "
                    "AND kind IN ('plate','concept')", (base60,),
                ).fetchall()
            } if base60 else {}

        suspicious = self.suspicious_codes(day)
        updated = 0
        with get_connection(self.db_path) as conn:
            for code, _day, level in rows:
                if code in suspicious or level <= 0:
                    continue
                zdf20 = None
                zdf60 = None
                if base20 and level20.get(code, 0) > 0:
                    zdf20 = (level / level20[code] - 1) * 100
                if base60 and level60.get(code, 0) > 0:
                    zdf60 = (level / level60[code] - 1) * 100
                if zdf20 is None and zdf60 is None:
                    continue
                if zdf20 is None:
                    conn.execute(
                        "UPDATE sector_daily SET zdf60=? WHERE date=? AND code=?",
                        (round(zdf60, 2), day, code),
                    )
                elif zdf60 is None:
                    conn.execute(
                        "UPDATE sector_daily SET zdf20=? WHERE date=? AND code=?",
                        (round(zdf20, 2), day, code),
                    )
                else:
                    conn.execute(
                        "UPDATE sector_daily SET zdf20=?, zdf60=? WHERE date=? AND code=?",
                        (round(zdf20, 2), round(zdf60, 2), day, code),
                    )
                updated += 1
        if updated:
            log.info("自积累指数点位回写 %d 个板块的 20/60 日涨幅（%s，按交易日历 %d/%d 日）",
                     updated, day, ZDF20_TRADING_DAYS, ZDF60_TRADING_DAYS)
        return updated

    @staticmethod
    def _trading_days_before(conn, day: str, n: int) -> str | None:
        """取 day 之前第 n 个交易日（含 day 当天共 n+1 个），日历不足返回 None"""
        try:
            rows = conn.execute(
                "SELECT date FROM trade_calendar WHERE is_trading=1 AND date<=? "
                "ORDER BY date DESC LIMIT ?",
                (day, n + 1),
            ).fetchall()
        except Exception:
            return None
        return rows[-1][0] if len(rows) >= n + 1 else None

    def suspicious_codes(self, trade_date: str | None = None) -> dict[str, str]:
        """点位/涨幅字段不自洽的板块（近 60 交易日窗口内单日点位变动超上限）

        用途：这类极端值不得进入赛道排序与分位基准（"其他数字媒体"单日
        点位 +36% 而当日涨幅字段仅 13.62%，属典型不自洽）。
        """
        with get_connection(self.db_path, readonly=True) as conn:
            day = trade_date
            if not day:
                row = conn.execute("SELECT MAX(date) FROM sector_daily").fetchone()
                day = row[0] if row else None
            if not day:
                return {}
            start = self._trading_days_before(conn, day, ZDF60_TRADING_DAYS)
            if start is None:
                return {}
            codes = [r[0] for r in conn.execute(
                "SELECT DISTINCT code FROM sector_daily WHERE date=?", (day,)
            ).fetchall()]
            out: dict[str, str] = {}
            for code in codes:
                rows = conn.execute(
                    "SELECT date, level FROM sector_daily WHERE code=? AND level>0 "
                    "AND date>=? AND date<=? ORDER BY date",
                    (code, start, day),
                ).fetchall()
                for i in range(1, len(rows)):
                    prev, cur = rows[i - 1], rows[i]
                    if prev[1] <= 0:
                        continue
                    chg = (cur[1] / prev[1] - 1) * 100
                    if abs(chg) > LEVEL_JUMP_LIMIT:
                        out[code] = (f"{prev[0]}→{cur[0]} 点位变动 {chg:+.1f}% "
                                     f"超过 {LEVEL_JUMP_LIMIT:.0f}% 上限")
                        break
        return out

    def save_meta(self, trade_date: str, source: str, plate_count: int,
                  concept_count: int, complete: bool, note: str = "") -> None:
        """写入当日快照元信息（幂等覆盖）"""
        with get_connection(self.db_path) as conn:
            conn.execute(
                """INSERT INTO sector_snapshot_meta
                   (date, source, plate_count, concept_count, complete, note)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(date) DO UPDATE SET
                     source=excluded.source, plate_count=excluded.plate_count,
                     concept_count=excluded.concept_count,
                     complete=excluded.complete, note=excluded.note""",
                (trade_date, source, plate_count, concept_count, 1 if complete else 0, note),
            )

    def snapshot_info(self, trade_date: str | None = None) -> dict:
        """当日快照元信息：数据源 / plate 与 concept 条数 / 是否完整

        优先读 sector_snapshot_meta；历史日期没有 meta 时按当日行推导
        （source 取主导来源，complete 按条数阈值判断）。
        """
        with get_connection(self.db_path, readonly=True) as conn:
            day = trade_date
            if not day:
                row = conn.execute("SELECT MAX(date) FROM sector_daily").fetchone()
                day = row[0] if row else None
            if not day:
                return {"date": "", "source": "", "plate_count": 0,
                        "concept_count": 0, "complete": False, "note": "无板块快照"}
            meta = conn.execute(
                "SELECT source, plate_count, concept_count, complete, note "
                "FROM sector_snapshot_meta WHERE date=?", (day,),
            ).fetchone()
            if meta:
                return {"date": day, "source": meta[0], "plate_count": meta[1],
                        "concept_count": meta[2], "complete": bool(meta[3]),
                        "note": meta[4] or ""}
            counts = {r[0]: r[1] for r in conn.execute(
                "SELECT kind, COUNT(*) FROM sector_daily WHERE date=? GROUP BY kind",
                (day,),
            ).fetchall()}
            srcs = {r[0]: r[1] for r in conn.execute(
                "SELECT source, COUNT(*) FROM sector_daily WHERE date=? "
                "GROUP BY source", (day,),
            ).fetchall()}
            source = max(srcs, key=srcs.get) if srcs else ""
            plate = counts.get("plate", 0)
            concept = counts.get("concept", 0)
            complete = (plate >= MIN_PLATE_BOARDS
                        and concept >= MIN_CONCEPT_BOARDS
                        and source == "eastmoney")
            note = "历史快照（无 meta，按条数推导）"
            if concept and concept < MIN_CONCEPT_BOARDS:
                note = f"概念板块仅 {concept} 条（阈值 {MIN_CONCEPT_BOARDS}），快照不完整"
            return {"date": day, "source": source, "plate_count": plate,
                    "concept_count": concept, "complete": complete, "note": note}


    def available_dates(self) -> list[str]:
        with get_connection(self.db_path, readonly=True) as conn:
            rows = conn.execute(
                "SELECT DISTINCT date FROM sector_daily ORDER BY date DESC"
            ).fetchall()
        return [r[0] for r in rows]

    # ---------- 评分 ----------
    @staticmethod
    def _norm(value: float, lo: float, hi: float) -> float:
        """线性归一化到 0-100"""
        if hi <= lo:
            return 50.0
        return max(0.0, min(100.0, (value - lo) / (hi - lo) * 100))

    def score_monthly(self, snapshots: list[SectorSnapshot]) -> list[TrackResult]:
        """月度主赛道:20日强度为纲
        评分 = zdf20 排名分(50%) + 20日资金分(30%) + 5日动能分(20%)

        #34：月度以 20 日涨幅为纲——zdf20 缺失(=0/NULL) 的板块不参与排名，
        否则"0 数据板块"凭 5日/资金分挤进 TOP，污染真实赛道。
        """
        snapshots = [s for s in snapshots if s.zdf20 is not None and s.zdf20 != 0]
        if not snapshots:
            return []
        n = len(snapshots)
        z20s = sorted((s.zdf20 for s in snapshots), reverse=True)
        z5s = sorted((s.zdf5 for s in snapshots), reverse=True)
        funds = sorted((s.zljlr_d20 for s in snapshots if s.zljlr_d20 is not None),
                       reverse=True)
        fund_lo = min(funds) if funds else 0.0
        fund_hi = max(funds) if funds else 0.0
        results = []
        for s in snapshots:
            r20 = z20s.index(s.zdf20) + 1 if s.zdf20 in z20s else n
            # 资金缺失（NULL）按池内最低资金处理，与原"0 分贡献"口径一致
            fund_val = s.zljlr_d20 if s.zljlr_d20 is not None else fund_lo
            score = (
                self._norm(s.zdf20, min(z20s), max(z20s)) * 0.5 +
                self._norm(fund_val, fund_lo, fund_hi) * 0.3 +
                self._norm(s.zdf5, min(z5s), max(z5s)) * 0.2
            )
            results.append(TrackResult(
                name=s.name, code=s.code, kind=s.kind, score=score,
                zdf20=s.zdf20, zdf60=s.zdf60, zljlr_d20=s.zljlr_d20,
                rank_20=r20, rank_60=0,
            ))
        results.sort(key=lambda x: -x.score)
        for i, r in enumerate(results):
            r.rank_20 = i + 1
        return results

    def score_quarterly(self, snapshots: list[SectorSnapshot]) -> list[TrackResult]:
        """季度主赛道:60日强度为纲
        评分 = zdf60 排名分(50%) + 20日资金分(30%) + 20日延续分(20%)

        #34：季度以 60 日涨幅为纲——zdf60 缺失(=0/NULL) 的板块不参与季度排名。
        """
        snapshots = [s for s in snapshots if s.zdf60 is not None and s.zdf60 != 0]
        if not snapshots:
            return []
        n = len(snapshots)
        z60s = sorted((s.zdf60 for s in snapshots), reverse=True)
        # 20 日涨幅在季度评分里只是"延续分"：缺失（NULL）沿用旧口径按 0 参与
        z20s = sorted((s.zdf20 if s.zdf20 is not None else 0.0
                       for s in snapshots), reverse=True)
        funds = sorted((s.zljlr_d20 for s in snapshots if s.zljlr_d20 is not None),
                       reverse=True)
        fund_lo = min(funds) if funds else 0.0
        fund_hi = max(funds) if funds else 0.0
        results = []
        for s in snapshots:
            r60 = z60s.index(s.zdf60) + 1 if s.zdf60 in z60s else n
            fund_val = s.zljlr_d20 if s.zljlr_d20 is not None else fund_lo
            z20 = s.zdf20 if s.zdf20 is not None else 0.0
            score = (
                self._norm(s.zdf60, min(z60s), max(z60s)) * 0.5 +
                self._norm(fund_val, fund_lo, fund_hi) * 0.3 +
                self._norm(z20, min(z20s), max(z20s)) * 0.2
            )
            results.append(TrackResult(
                name=s.name, code=s.code, kind=s.kind, score=score,
                zdf20=z20, zdf60=s.zdf60, zljlr_d20=s.zljlr_d20,
                rank_20=0, rank_60=r60,
            ))
        results.sort(key=lambda x: -x.score)
        for i, r in enumerate(results):
            r.rank_60 = i + 1
        return results

    def sub_sectors(self, main_name: str, snapshots: list[SectorSnapshot], top: int = 5) -> list[TrackResult]:
        """细分赛道:优先关键词匹配主赛道概念(如"电力设备"→核电/电网概念),
        无匹配时返回概念板块月度强度 TOP(活跃细分)
        """
        if not snapshots:
            return []
        # 主赛道关键词(取2字以上的部分)
        main_keywords = [main_name.replace("Ⅱ", "").strip()]
        if len(main_name) >= 3:
            main_keywords.append(main_name[:2])
        subs = []
        for s in snapshots:
            if s.kind != "concept":
                continue
            if any(kw in s.name for kw in main_keywords if kw):
                subs.append(s)
        if subs:
            return self.score_monthly(subs)[:top]
        # 无关键词匹配 → 返回概念板块活跃 TOP(标注为"活跃细分")
        concepts = [s for s in snapshots if s.kind == "concept"]
        if not concepts:
            return []
        return self.score_monthly(concepts)[:top]

    def analyze(self, trade_date: str | None = None) -> dict:
        """一键分析:返回月度/季度主赛道 + 细分

        2026-10-30 口径修正：点位/涨幅不自洽的可疑板块不参与赛道排序
        （其极端值会污染排名与分位基准）；全量赛道分放在 all_tracks 里，
        供展示与板块解析，不参与评分（评分已改为板块内分位）。
        """
        plates = self.load_date(trade_date, kind="plate", exclude_suspicious=True)
        concepts = self.load_date(trade_date, kind="concept", exclude_suspicious=True)
        monthly = self.score_monthly(plates)
        quarterly = self.score_quarterly(plates)
        result = {
            "date": plates[0].date if plates else (trade_date or ""),
            "monthly_tracks": monthly[:5],
            "quarterly_tracks": quarterly[:3],
            "all_tracks": monthly,
            "suspicious": self.suspicious_codes(
                plates[0].date if plates else trade_date),
            "sub_sectors": {},
        }
        # 月度主赛道细分
        for t in monthly[:3]:
            result["sub_sectors"][t.name] = self.sub_sectors(t.name, concepts)
        return result

    def summary(self, trade_date: str | None = None) -> str:
        """人类可读的赛道报告"""
        r = self.analyze(trade_date)
        lines = [f"📅 赛道分析报告({r['date']})", "=" * 60]
        lines.append("\n📈 月度主赛道 TOP5(20日强度+资金):")
        for i, t in enumerate(r["monthly_tracks"], 1):
            lines.append(f"  {i}. {t}")
        lines.append("\n📊 季度主赛道 TOP3(60日强度+资金):")
        for i, t in enumerate(r["quarterly_tracks"], 1):
            lines.append(f"  {i}. {t}")
        lines.append("\n🔬 细分赛道(月度主赛道关联概念):")
        for main, subs in r["sub_sectors"].items():
            lines.append(f"  {main} →")
            for s in subs:
                lines.append(f"    - {s}")
            if not subs:
                lines.append("    (暂无匹配概念板块)")
        return "\n".join(lines)


if __name__ == "__main__":
    a = SectorAnalyzer()
    print(a.summary())
