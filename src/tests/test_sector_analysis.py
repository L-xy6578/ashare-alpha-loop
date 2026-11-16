"""赛道分析模块测试"""
import pytest

from src.sector_analysis import SectorAnalyzer, SectorSnapshot


@pytest.fixture()
def analyzer(tmp_path):
    """临时库的分析器"""
    return SectorAnalyzer(db_path=str(tmp_path / "sector_test.db"))


def _mk(name, zdf20, zdf60, zljlr_d20=0, zdf5=0, code=None, kind="plate", zdf=0, hsl=0):
    return SectorSnapshot(
        code=code or f"code_{name}", name=name, kind=kind,
        zdf=zdf, zdf5=zdf5, zdf20=zdf20, zdf60=zdf60,
        zljlr_d20=zljlr_d20, hsl=hsl,
    )


class TestSaveLoad:
    def test_save_and_load(self, analyzer):
        snaps = [
            _mk("白酒", 10.0, 5.0, 100),
            _mk("半导体", -5.0, -3.0, -200),
        ]
        n = analyzer.save_snapshot(snaps)
        assert n == 2
        loaded = analyzer.load_date()
        assert len(loaded) == 2
        names = {s.name for s in loaded}
        assert names == {"白酒", "半导体"}

    def test_upsert_same_day(self, analyzer):
        analyzer.save_snapshot([_mk("白酒", 10.0, 5.0)])
        analyzer.save_snapshot([_mk("白酒", 12.0, 6.0)])
        loaded = analyzer.load_date()
        assert len(loaded) == 1
        assert loaded[0].zdf20 == 12.0

    def test_available_dates(self, analyzer):
        analyzer.save_snapshot([_mk("A", 1, 1)])
        dates = analyzer.available_dates()
        assert len(dates) == 1


class TestFromWestock:
    def test_parse_rank_and_fundflow(self, analyzer):
        payload = {
            "rank": {
                "plate": [
                    {"bd_code": "p1", "bd_name": "白酒",
                     "bd_zdf": "0.09", "bd_zdf5": "5.67",
                     "bd_zdf20": "11.35", "bd_zdf60": "-5.09"},
                ],
                "concept": [
                    {"bd_code": "c1", "bd_name": "白酒概念",
                     "bd_zdf": "1.0", "bd_zdf5": "2.0",
                     "bd_zdf20": "3.0", "bd_zdf60": "4.0"},
                ],
            },
            "fundflow": {
                "plate": {
                    "top": [
                        {"code": "p1", "zljlr": "100",
                         "zljlr_d5": "50", "zljlr_d20": "200"},
                    ],
                    "bottom": [],
                },
            },
        }
        n = analyzer.from_westock_json(payload, trade_date="2026-08-01")
        assert n == 2
        plates = analyzer.load_date("2026-08-01", kind="plate")
        assert len(plates) == 1
        assert plates[0].zljlr_d20 == 200
        concepts = analyzer.load_date("2026-08-01", kind="concept")
        assert len(concepts) == 1


class TestScoring:
    def test_monthly_ranks_higher_20d_first(self, analyzer):
        snaps = [
            _mk("强势", zdf20=20.0, zdf60=10.0, zljlr_d20=500),
            _mk("弱势", zdf20=-10.0, zdf60=-5.0, zljlr_d20=-500),
            _mk("中等", zdf20=5.0, zdf60=2.0, zljlr_d20=0),
        ]
        ranked = analyzer.score_monthly(snaps)
        assert ranked[0].name == "强势"
        assert ranked[-1].name == "弱势"
        assert ranked[0].score > ranked[1].score > ranked[2].score

    def test_quarterly_uses_60d(self, analyzer):
        snaps = [
            # 60日强但20日弱(退潮期) vs 60日弱但20日强(启动期)
            _mk("季度强", zdf20=-5.0, zdf60=25.0, zljlr_d20=300),
            _mk("季度弱", zdf20=15.0, zdf60=2.0, zljlr_d20=300),
        ]
        ranked = analyzer.score_quarterly(snaps)
        assert ranked[0].name == "季度强"

    def test_negative_values_handled(self, analyzer):
        snaps = [_mk("跌", zdf20=-30.0, zdf60=-20.0)]
        ranked = analyzer.score_monthly(snaps)
        assert 0 <= ranked[0].score <= 100

    def test_empty_returns_empty(self, analyzer):
        assert analyzer.score_monthly([]) == []
        assert analyzer.score_quarterly([]) == []

    def test_missing_zdf_excluded_from_tracks(self, analyzer):
        """#34：主赛道评分排除缺失维度的板块（月度看 20 日、季度看 60 日）"""
        snaps = [
            _mk("有数据", zdf20=10, zdf60=5),
            _mk("无数据", zdf20=0, zdf60=0),
            _mk("只缺60日", zdf20=8, zdf60=0),   # 月度可参与，季度排除
        ]
        monthly = analyzer.score_monthly(snaps)
        quarterly = analyzer.score_quarterly(snaps)
        assert {r.name for r in monthly} == {"有数据", "只缺60日"}
        assert [r.name for r in quarterly] == ["有数据"]


class TestSubSectors:
    def test_keyword_match(self, analyzer):
        plates = [_mk("电网设备", zdf20=5, zdf60=3, code="p1")]
        concepts = [
            _mk("核电概念", zdf20=8, zdf60=1, code="c1", kind="concept"),
            _mk("AI营销", zdf20=10, zdf60=2, code="c2", kind="concept"),
        ]
        subs = analyzer.sub_sectors("电网设备", plates + concepts)
        # 电网设备 应匹配不到概念(核电概念无"电网"字样) → 回退活跃概念TOP
        assert len(subs) > 0
        assert subs[0].name == "AI营销"  # 20日涨幅更高

    def test_direct_match(self, analyzer):
        plates = [_mk("半导体", zdf20=5, zdf60=3, code="p1")]
        concepts = [
            _mk("半导体设备", zdf20=8, zdf60=1, code="c1", kind="concept"),
            _mk("白酒", zdf20=10, zdf60=2, code="c2", kind="concept"),
        ]
        subs = analyzer.sub_sectors("半导体", plates + concepts)
        assert len(subs) == 1
        assert subs[0].name == "半导体设备"


class TestAnalyze:
    def test_full_analyze(self, analyzer):
        snaps = [
            _mk("白酒", zdf20=10, zdf60=5, code="p1"),
            _mk("银行", zdf20=8, zdf60=6, code="p2"),
            _mk("半导体", zdf20=-5, zdf60=-3, code="p3"),
            _mk("白酒概念", zdf20=12, zdf60=4, code="c1", kind="concept"),
        ]
        analyzer.save_snapshot(snaps)
        r = analyzer.analyze()
        assert r["date"] == snaps[0].date
        assert len(r["monthly_tracks"]) == 3
        assert len(r["quarterly_tracks"]) == 3
        assert r["monthly_tracks"][0].name == "白酒"

    def test_summary_string(self, analyzer):
        analyzer.save_snapshot([_mk("白酒", 10, 5, code="p1")])
        s = analyzer.summary()
        assert "赛道分析报告" in s
        assert "月度主赛道" in s
        assert "季度主赛道" in s


class TestLevelAccumulation:
    """板块指数点位自积累：按**交易日历**回算 20/60 日涨幅（2026-10-30 修正）"""

    @staticmethod
    def _seed_calendar(db, dates):
        import sqlite3

        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS trade_calendar "
                "(date TEXT PRIMARY KEY, is_trading INTEGER NOT NULL DEFAULT 1, note TEXT)"
            )
            for d in dates:
                conn.execute(
                    "INSERT OR REPLACE INTO trade_calendar (date, is_trading, note) "
                    "VALUES (?,1,'')", (d,))

    def test_level_roundtrip(self, analyzer):
        analyzer.save_snapshot([
            SectorSnapshot(code="p1", name="板块A", date="2026-08-05",
                           zdf=1.0, level=1234.5),
        ])
        loaded = analyzer.load_date("2026-08-05")
        assert loaded[0].level == 1234.5

    def test_refresh_zdf20_from_levels(self, analyzer):
        """21 个交易日点位 100→120 → zdf20 = +20%"""
        from datetime import date, timedelta

        days = [(date(2026, 1, 1) + timedelta(days=i)).isoformat() for i in range(21)]
        self._seed_calendar(analyzer.db_path, days)
        snaps = [SectorSnapshot(code="p1", name="板块A", date=d, level=100 + i)
                 for i, d in enumerate(days)]
        analyzer.save_snapshot(snaps)
        n = analyzer.refresh_zdf_from_levels(days[-1])
        assert n == 1
        loaded = analyzer.load_date(days[-1])
        assert loaded[0].zdf20 == pytest.approx(20.0)   # 120/100-1
        # 2026-11-10 口径修正：不足 60 交易日时写 NULL（无真值），不再用 0 冒充
        assert loaded[0].zdf60 is None

    def test_refresh_uses_trading_calendar_not_row_count(self, analyzer):
        """只有 2 行快照，但它们在交易日历上相隔 20 个交易日 → 仍能算 20 日涨幅"""
        from datetime import date, timedelta

        days = [(date(2026, 1, 1) + timedelta(days=i)).isoformat() for i in range(40)]
        self._seed_calendar(analyzer.db_path, days)
        analyzer.save_snapshot([
            SectorSnapshot(code="p1", name="板块A", date=days[0], level=100),
            SectorSnapshot(code="p1", name="板块A", date=days[20], level=120),
        ])
        assert analyzer.refresh_zdf_from_levels(days[20]) == 1
        loaded = analyzer.load_date(days[20])
        assert loaded[0].zdf20 == pytest.approx(20.0)

    def test_refresh_skips_when_insufficient(self, analyzer):
        analyzer.save_snapshot([
            SectorSnapshot(code="p1", name="板块A", date="2026-08-05", level=100),
        ])
        assert analyzer.refresh_zdf_from_levels("2026-08-05") == 0

    def test_legacy_schema_adds_level(self, tmp_path):
        """旧表（无 level/source 列）初始化时自动迁移"""
        import sqlite3

        db = str(tmp_path / "legacy_sector.db")
        with sqlite3.connect(db) as conn:
            conn.execute(
                """CREATE TABLE sector_daily (
                    date TEXT NOT NULL, code TEXT NOT NULL, name TEXT NOT NULL,
                    kind TEXT NOT NULL, zdf REAL DEFAULT 0, zdf5 REAL DEFAULT 0,
                    zdf20 REAL DEFAULT 0, zdf60 REAL DEFAULT 0,
                    zljlr REAL DEFAULT 0, zljlr_d5 REAL DEFAULT 0,
                    zljlr_d20 REAL DEFAULT 0, hsl REAL DEFAULT 0,
                    PRIMARY KEY (date, code))"""
            )
        SectorAnalyzer(db)
        with sqlite3.connect(db) as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(sector_daily)").fetchall()]
        assert "level" in cols
        assert "source" in cols


class TestSourceMigration:
    def test_source_backfill_by_code_prefix(self, analyzer):
        analyzer.save_snapshot([
            SectorSnapshot(code="bk_BK1283", name="银行", kind="plate",
                           date="2026-10-30", source=""),
            SectorSnapshot(code="pt_pt01801780", name="银行", kind="plate",
                           date="2026-10-29", source=""),
            SectorSnapshot(code="bd_x", name="白酒", kind="plate",
                           date="2026-08-01", source=""),
        ])
        # 只把 source 置空后再触发迁移，验证回填规则
        import sqlite3

        with sqlite3.connect(analyzer.db_path) as conn:
            conn.execute("UPDATE sector_daily SET source=''")
        SectorAnalyzer(analyzer.db_path)      # 重新初始化 → 幂等回填
        rows = {r[0]: r[1] for r in sqlite3.connect(analyzer.db_path).execute(
            "SELECT code, source FROM sector_daily")}
        assert rows["bk_BK1283"] == "eastmoney"
        assert rows["pt_pt01801780"] == "tencent_fallback"
        assert rows["bd_x"] == "other"


class TestSnapshotMeta:
    def test_save_and_load_meta(self, analyzer):
        analyzer.save_meta("2026-10-30", "eastmoney", 496, 200, False,
                           "概念板块仅 200 条")
        info = analyzer.snapshot_info("2026-10-30")
        assert info["source"] == "eastmoney"
        assert info["plate_count"] == 496 and info["concept_count"] == 200
        assert info["complete"] is False
        assert "200" in info["note"]

    def test_derived_info_without_meta(self, analyzer):
        snaps = ([SectorSnapshot(code=f"bk_p{i}", name=f"板块{i}", kind="plate",
                                 date="2026-10-26", zdf20=1.0, source="eastmoney")
                  for i in range(450)]
                 + [SectorSnapshot(code=f"bk_c{i}", name=f"概念{i}", kind="concept",
                                   date="2026-10-26", source="eastmoney")
                    for i in range(200)])
        analyzer.save_snapshot(snaps)
        info = analyzer.snapshot_info("2026-10-26")
        assert info["source"] == "eastmoney"
        assert info["complete"] is False      # 概念 200 < 400
        assert "不完整" in info["note"]


class TestSuspiciousBoards:
    def test_level_jump_flagged_and_excluded(self, analyzer):
        analyzer.save_snapshot([
            SectorSnapshot(code="bk_bad", name="其他数字媒体", kind="plate",
                           date="2026-10-20", level=893.26, zdf20=2.0),
            SectorSnapshot(code="bk_bad", name="其他数字媒体", kind="plate",
                           date="2026-10-30", level=1392.31, zdf20=48.69),
            SectorSnapshot(code="bk_ok", name="银行", kind="plate",
                           date="2026-10-30", level=4505.8, zdf20=2.48),
        ])
        # 交易日历只覆盖 10-20→10-30，窗口内可检测到 +36% 跳变
        import sqlite3
        from datetime import date, timedelta

        with sqlite3.connect(analyzer.db_path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS trade_calendar "
                         "(date TEXT PRIMARY KEY, is_trading INTEGER, note TEXT)")
            d = date(2026, 6, 1)
            while d <= date(2026, 10, 30):
                conn.execute("INSERT OR REPLACE INTO trade_calendar VALUES (?,1,'')",
                             (d.isoformat(),))
                d += timedelta(days=1)
        bad = analyzer.suspicious_codes("2026-10-30")
        assert "bk_bad" in bad
        assert "bk_ok" not in bad
        kept = analyzer.load_date("2026-10-30", exclude_suspicious=True)
        assert {s.code for s in kept} == {"bk_ok"}

