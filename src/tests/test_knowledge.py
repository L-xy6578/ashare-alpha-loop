"""
三角闭环知识库 — 完整测试套件

运行:
    cd "Workbuudy test"
    pytest src/tests/test_knowledge.py -v

覆盖:
    - 建库建表
    - 盈利/亏损记录自动路由
    - 选前情报 (preflight)
    - 轮次复盘 (round_summary)
    - 信号交叉验证 (cross_check)
    - 边界情况（空库、混合轮次）
"""

import sqlite3
import tempfile
import os
import pytest

# 设置工作目录，确保相对导入正确
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.knowledge import init_db, CorrectBook, MistakeBook, KnowledgeBase
from src.knowledge.repository import PreflightReport, RoundReport


@pytest.fixture
def db():
    """每次测试使用独立的临时数据库"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    yield path
    try:
        os.unlink(path)
    except PermissionError:
        pass  # Windows 文件锁，清理失败不影响测试


@pytest.fixture
def kb(db, tmp_path):
    """带预置数据的 KnowledgeBase（行情库指向不存在路径 → #80 校验走跳过路径，测试与真实库隔离）"""
    return KnowledgeBase(db, market_db=str(tmp_path / "no_market.db"))


@pytest.fixture
def populated_kb(kb):
    """预填充 4 条交易记录的知识库：2 盈 2 亏"""
    # 盈利 1
    kb.record(
        stock_code="600519", stock_name="贵州茅台",
        entry_price=1680, exit_price=1850,
        buy_reason="PE低位+白酒旺季+三筛通过",
        sell_reason="目标位到达",
        strategy_tag="三筛价值回调",
        round_number="R43",
        holding_days=21,
    )
    # 亏损 1
    kb.record(
        stock_code="000858", stock_name="五粮液",
        entry_price=145, exit_price=132,
        buy_reason="跟随茅台反弹",
        sell_reason="跌破止损线",
        strategy_tag="跟涨抄底",
        round_number="R43",
        holding_days=7,
        loss_root_cause="没有独立逻辑，纯跟涨标的追高",
        lesson_learned="龙头不涨、跟涨不追",
        mistake_type="跟涨追高",
    )
    # 盈利 2
    kb.record(
        stock_code="002230", stock_name="科大讯飞",
        entry_price=45, exit_price=54,
        buy_reason="AI主线+营收加速+资金持续流入",
        sell_reason="短期超买+分批止盈",
        strategy_tag="主线龙头持有",
        round_number="R44",
        holding_days=35,
    )
    # 亏损 2
    kb.record(
        stock_code="601012", stock_name="隆基绿能",
        entry_price=22, exit_price=19.5,
        buy_reason="光伏触底反弹预期",
        sell_reason="止损",
        strategy_tag="超跌反弹",
        round_number="R44",
        holding_days=5,
        loss_root_cause="行业基本面未改善，反弹是假信号",
        lesson_learned="超跌反弹必须有基本面拐点确认",
        mistake_type="抄底过早",
    )
    return kb


# ================================================================
# 表结构测试
# ================================================================

class TestDatabaseInit:
    def test_tables_created(self, db):
        conn = sqlite3.connect(db)
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = [t[0] for t in tables]
        assert "correct_book" in table_names
        assert "mistake_book" in table_names
        conn.close()

    def test_correct_book_schema(self, db):
        conn = sqlite3.connect(db)
        col_info = conn.execute("PRAGMA table_info(correct_book)").fetchall()
        cols = {row[1]: row[2] for row in col_info}
        assert "stock_code" in cols
        assert "profit_pct" in cols
        assert "strategy_tag" in cols
        assert "round_number" in cols
        conn.close()

    def test_mistake_book_schema(self, db):
        conn = sqlite3.connect(db)
        col_info = conn.execute("PRAGMA table_info(mistake_book)").fetchall()
        cols = {row[1]: row[2] for row in col_info}
        assert "loss_root_cause" in cols
        assert "lesson_learned" in cols
        assert "mistake_type" in cols
        conn.close()


# ================================================================
# CRUD 基础测试
# ================================================================

class TestCorrectBook:
    def test_create_and_read(self, db):
        r = CorrectBook.create(
            db, stock_code="600519", stock_name="贵州茅台",
            entry_price=1680, exit_price=1850, profit_pct=10.12,
            buy_reason="PE低位", sell_reason="目标位到达",
            strategy_tag="三筛价值回调", round_number="R43",
        )
        assert r.id == 1

        recent = CorrectBook.recent(db, 5)
        assert len(recent) == 1
        assert recent[0]["stock_code"] == "600519"
        assert recent[0]["profit_pct"] == 10.12

    def test_stats_empty(self, db):
        stats = CorrectBook.stats(db, days=365)
        assert stats["total_correct"] == 0

    def test_stats_with_data(self, populated_kb):
        stats = CorrectBook.stats(populated_kb.db, days=365)
        assert stats["total_correct"] == 2


class TestMistakeBook:
    def test_create_and_read(self, db):
        m = MistakeBook.create(
            db, stock_code="000858", stock_name="五粮液",
            entry_price=145, exit_price=132, loss_pct=-8.97,
            buy_reason="跟涨茅台", loss_root_cause="纯跟涨",
            lesson_learned="不追跟涨", mistake_type="跟涨追高",
            round_number="R43",
        )
        assert m.id == 1

        recent = MistakeBook.recent(db, 5)
        assert len(recent) == 1

    def test_top_causes(self, populated_kb):
        causes = MistakeBook.top_causes(populated_kb.db, 5)
        assert len(causes) >= 2
        types = [c["type"] for c in causes]
        assert "跟涨追高" in types
        assert "抄底过早" in types


# ================================================================
# KnowledgeBase 集成测试
# ================================================================

class TestRecordRouting:
    def test_record_profit_goes_to_correct(self, kb):
        r = kb.record(
            stock_code="600519", stock_name="茅台",
            entry_price=100, exit_price=110,
            buy_reason="低估", sell_reason="到目标",
            strategy_tag="价值回归", round_number="R43",
        )
        assert r["book"] == "correct_book"
        assert r["return_pct"] == 10.0

    def test_record_loss_goes_to_mistake(self, kb):
        r = kb.record(
            stock_code="000001", stock_name="平安",
            entry_price=100, exit_price=95,
            buy_reason="低估", sell_reason="跌破止损",
            strategy_tag="价值回归", round_number="R43",
            loss_root_cause="判断早了", lesson_learned="等右侧",
            mistake_type="抄底过早",
        )
        assert r["book"] == "mistake_book"
        assert r["return_pct"] == -5.0

    def test_record_breakeven_goes_to_correct(self, kb):
        """盈亏平衡也算对题本"""
        r = kb.record(
            stock_code="000001", stock_name="平安",
            entry_price=100, exit_price=100,
            buy_reason="测试", sell_reason="平出",
            strategy_tag="测试策略", round_number="R43",
        )
        assert r["book"] == "correct_book"
        assert r["return_pct"] == 0.0


class TestPreflight:
    def test_empty_preflight(self, kb):
        report = kb.preflight()
        assert isinstance(report, PreflightReport)
        assert report.total_trades == 0
        assert len(report.danger_signals) == 0

    def test_preflight_with_data(self, populated_kb):
        report = populated_kb.preflight()
        assert len(report.recent_mistakes) > 0
        assert len(report.mistake_patterns) >= 2
        assert len(report.best_strategies) > 0
        assert report.total_trades == 4
        assert report.win_rate == 50.0
        # 危险信号应包含避坑提示
        assert len(report.danger_signals) > 0
        assert any("警惕" in s for s in report.danger_signals)

    def test_preflight_dedupes_repeated_trades(self, kb):
        """#27：同代码同价重复轮次不计入胜率（R45/R46 同价重复不再虚高）"""
        for i in range(2):
            kb.record(
                stock_code="600535", stock_name="天士力",
                entry_price=15.78, exit_price=16.12,
                buy_reason="x", sell_reason="y",
                strategy_tag="测试策略", round_number=f"RT{i}",
                holding_days=1,
            )
        report = kb.preflight()
        assert report.total_trades == 1
        assert report.win_rate == 100.0
        assert report.by_source["sim"]["correct"] == 1


class TestRoundSummary:
    def test_round_summary_mixed(self, populated_kb):
        report = populated_kb.round_summary("R43")
        assert isinstance(report, RoundReport)
        assert report.correct_count == 1
        assert report.mistake_count == 1
        assert "正确 1 笔, 错误 1 笔" in report.summary

    def test_round_summary_not_found(self, kb):
        report = kb.round_summary("R999")
        assert report.correct_count == 0
        assert report.mistake_count == 0


class TestCrossCheck:
    def test_known_good_strategy(self, populated_kb):
        """"主线龙头持有" 应该被判定为安全（2次成功, 0次失败）"""
        result = populated_kb.cross_check("主线龙头持有")
        # 样本不够3次，所以不完全安全
        assert result["sample_count"] == 1

    def test_unknown_strategy(self, kb):
        result = kb.cross_check("未知策略")
        assert result["safe"] is False
        assert any("样本不足" in r for r in result["risk_signals"])

    def test_failed_strategy(self, populated_kb):
        result = populated_kb.cross_check("跟涨抄底")
        assert result["safe"] is False


# ================================================================
# 边界测试
# ================================================================

class TestEdgeCases:
    def test_idempotent_init(self, db):
        """重复 init_db 不报错"""
        path = init_db(db)
        assert os.path.exists(path)

    def test_empty_recent(self, db):
        corrects = CorrectBook.recent(db, 10)
        assert len(corrects) == 0

    def test_recent_limit(self, populated_kb):
        corrects = CorrectBook.recent(populated_kb.db, 1)
        assert len(corrects) == 1

    def test_stats_short_window(self, populated_kb):
        """今日数据，1天窗口也应包含"""
        stats = CorrectBook.stats(populated_kb.db, days=1)
        assert stats["total_correct"] == 2


# ================================================================
# source 口径（sim 模拟 / real 真实）— KPI-07 只认 real
# ================================================================

class TestSourceTracking:
    def test_record_default_source_sim(self, kb):
        kb.record(
            stock_code="600519", stock_name="茅台",
            entry_price=100, exit_price=110,
            buy_reason="低估", sell_reason="到目标",
            strategy_tag="价值回归", round_number="R43",
        )
        with sqlite3.connect(kb.db) as conn:
            src = conn.execute("SELECT source FROM correct_book").fetchone()[0]
        assert src == "sim"

    def test_record_source_real(self, kb):
        kb.record(
            stock_code="600000", stock_name="示例银行",
            entry_price=10.0, exit_price=8.0,
            buy_reason="真实持仓", sell_reason="清仓止损",
            strategy_tag="真实策略", round_number="RREAL",
            source="real",
            loss_root_cause="示例根因", lesson_learned="示例教训",
            mistake_type="止损不及时",
        )
        report = kb.preflight()
        assert report.by_source["real"]["mistake"] == 1
        assert report.by_source["real"]["total"] == 1
        assert report.by_source["real"]["win_rate"] == 0.0

    def test_preflight_separates_sources(self, populated_kb):
        report = populated_kb.preflight()
        assert report.by_source["sim"]["correct"] == 2
        assert report.by_source["sim"]["mistake"] == 2
        assert report.by_source["real"]["total"] == 0

    def test_round_summary_source_filter(self, populated_kb):
        populated_kb.record(
            stock_code="600519", stock_name="茅台",
            entry_price=100, exit_price=120,
            buy_reason="真实成交", sell_reason="止盈",
            strategy_tag="真实策略", round_number="R44",
            source="real",
        )
        r_all = populated_kb.round_summary("R44")
        r_real = populated_kb.round_summary("R44", source="real")
        assert r_all.correct_count == 2   # 科大讯飞(sim) + 茅台(real)
        assert r_real.correct_count == 1

    def test_legacy_schema_migration_adds_source(self, tmp_path):
        """旧库（无 source 列）init_db 后自动补列，存量归 sim"""
        db = str(tmp_path / "legacy_kb.db")
        with sqlite3.connect(db) as conn:
            conn.execute(
                """CREATE TABLE correct_book (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stock_code TEXT NOT NULL, stock_name TEXT NOT NULL,
                    entry_price REAL NOT NULL, exit_price REAL NOT NULL,
                    profit_pct REAL NOT NULL, buy_reason TEXT NOT NULL,
                    sell_reason TEXT NOT NULL, strategy_tag TEXT NOT NULL,
                    round_number TEXT NOT NULL, holding_days INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL)"""
            )
            conn.execute(
                """INSERT INTO correct_book
                   (stock_code, stock_name, entry_price, exit_price, profit_pct,
                    buy_reason, sell_reason, strategy_tag, round_number, created_at)
                   VALUES ('600519','茅台',100,110,10,'低估','到目标','价值回归','R43','2026-08-01')"""
            )
        init_db(db)
        with sqlite3.connect(db) as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(correct_book)").fetchall()]
            assert "source" in cols
            src = conn.execute("SELECT source FROM correct_book").fetchone()[0]
        assert src == "sim"


# ==================================================================
# #82 holding_days 自动计算（entry/exit 日期 → 自然日，≥1）
# ==================================================================

class TestHoldingDaysAutoCompute:
    def test_mistake_auto_holding_days(self, kb):
        kb.record(
            stock_code="600019", stock_name="宝钢股份",
            entry_price=5.95, exit_price=5.76,
            buy_reason="测试", sell_reason="测试",
            strategy_tag="R45", round_number="R47",
            loss_root_cause="x", lesson_learned="y", mistake_type="止损不及时",
            entry_date="2026-07-29", exit_date="2026-08-07",
        )
        with sqlite3.connect(kb.db) as conn:
            hd = conn.execute(
                "SELECT holding_days FROM mistake_book WHERE round_number='R47'"
            ).fetchone()[0]
        assert hd == 9

    def test_correct_auto_holding_days(self, kb):
        kb.record(
            stock_code="600519", stock_name="茅台",
            entry_price=100, exit_price=110,
            buy_reason="x", sell_reason="y",
            strategy_tag="t", round_number="RT",
            entry_date="2026-08-01", exit_date="2026-08-17",
        )
        with sqlite3.connect(kb.db) as conn:
            hd = conn.execute(
                "SELECT holding_days FROM correct_book WHERE round_number='RT'"
            ).fetchone()[0]
        assert hd == 16

    def test_dates_override_passed_zero(self, kb):
        kb.record(
            stock_code="000001", stock_name="平安",
            entry_price=100, exit_price=95,
            buy_reason="x", sell_reason="y",
            strategy_tag="t", round_number="RT2", holding_days=0,
            loss_root_cause="x", lesson_learned="y", mistake_type="抄底过早",
            entry_date="2026-07-29", exit_date="2026-08-07",
        )
        with sqlite3.connect(kb.db) as conn:
            hd = conn.execute(
                "SELECT holding_days FROM mistake_book WHERE round_number='RT2'"
            ).fetchone()[0]
        assert hd == 9

    def test_no_dates_keeps_passed_value(self, kb):
        kb.record(
            stock_code="600519", stock_name="茅台",
            entry_price=100, exit_price=110,
            buy_reason="x", sell_reason="y",
            strategy_tag="t", round_number="RT3", holding_days=21,
        )
        with sqlite3.connect(kb.db) as conn:
            hd = conn.execute(
                "SELECT holding_days FROM correct_book WHERE round_number='RT3'"
            ).fetchone()[0]
        assert hd == 21

    def test_invalid_dates_keeps_passed_value(self, kb):
        kb.record(
            stock_code="600519", stock_name="茅台",
            entry_price=100, exit_price=110,
            buy_reason="x", sell_reason="y",
            strategy_tag="t", round_number="RT4", holding_days=3,
            entry_date="bad-date", exit_date="worse",
        )
        with sqlite3.connect(kb.db) as conn:
            hd = conn.execute(
                "SELECT holding_days FROM correct_book WHERE round_number='RT4'"
            ).fetchone()[0]
        assert hd == 3

    def test_exit_before_entry_keeps_passed_value(self, kb):
        kb.record(
            stock_code="600519", stock_name="茅台",
            entry_price=100, exit_price=110,
            buy_reason="x", sell_reason="y",
            strategy_tag="t", round_number="RT5", holding_days=5,
            entry_date="2026-08-17", exit_date="2026-08-01",
        )
        with sqlite3.connect(kb.db) as conn:
            hd = conn.execute(
                "SELECT holding_days FROM correct_book WHERE round_number='RT5'"
            ).fetchone()[0]
        assert hd == 5


# ==================================================================
# #80 行情一致性校验（WARN 不阻断）
# ==================================================================

from src.knowledge.repository import _to_market_symbol  # noqa: E402


class TestToMarketSymbol:
    def test_prefix_rules(self):
        assert _to_market_symbol("600519") == "sh600519"
        assert _to_market_symbol("688396") == "sh688396"
        assert _to_market_symbol("002366") == "sz002366"
        assert _to_market_symbol("300750") == "sz300750"
        assert _to_market_symbol("830799") == "bj830799"
        assert _to_market_symbol("sz002366") == "sz002366"
        assert _to_market_symbol(" SH600019 ") == "sh600019"


@pytest.fixture
def market_db(tmp_path):
    """临时行情库：前 10 日高点 9.0，后 90 日区间 [5.0, 7.0]"""
    path = str(tmp_path / "market.db")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE daily_bars (symbol TEXT, date TEXT, low REAL, high REAL)")
        rows = []
        for i in range(1, 11):  # 久远段：高点区 8.0~9.0
            rows.append(("sh600000", f"2026-03-{i:02d}", 7.5, 9.0))
        for i in range(11, 101):  # 近 90 个交易日：5.0~7.0
            rows.append(("sh600000", f"2026-04-{i:03d}", 5.0, 7.0))
        conn.executemany("INSERT INTO daily_bars VALUES (?,?,?,?)", rows)
    return path


@pytest.fixture
def kb_market(db, market_db):
    return KnowledgeBase(db, market_db=market_db)


class TestMarketConsistency:
    def _record(self, kb, entry, exit_):
        return kb.record(
            stock_code="600000", stock_name="示例银行",
            entry_price=entry, exit_price=exit_,
            buy_reason="测试", sell_reason="测试",
            strategy_tag="测试", round_number="T80", source="sim",
        )

    def test_price_in_recent_range_no_warn(self, kb_market):
        r = self._record(kb_market, 6.0, 6.5)
        assert r["book"] == "correct_book"   # 盈利路由
        assert r["market_warnings"] == []

    def test_price_outside_full_history_warns(self, kb_market):
        r = self._record(kb_market, 99.0, 6.0)
        assert any("超出" in w and "历史K线区间" in w for w in r["market_warnings"])

    def test_price_inside_history_outside_recent_warns(self, kb_market):
        """entry 在历史区间内但不在近 90 交易日区间 → WARN"""
        r = self._record(kb_market, 8.0, 6.5)
        warns = r["market_warnings"]
        assert any("近90个交易日" in w and "entry_price=8.0" in w for w in warns)
        assert not any("exit_price" in w for w in warns)  # exit 在近段区间内

    def test_unknown_symbol_warns_skip(self, db, market_db):
        kb = KnowledgeBase(db, market_db=market_db)
        r = kb.record(
            stock_code="999999", stock_name="无K线标的",
            entry_price=10.0, exit_price=11.0,
            buy_reason="测试", sell_reason="测试",
            strategy_tag="测试", round_number="T80",
        )
        assert any("无法核对" in w for w in r["market_warnings"])

    def test_missing_market_db_warns_skip(self, db, tmp_path):
        kb = KnowledgeBase(db, market_db=str(tmp_path / "not_exist.db"))
        r = kb.record(
            stock_code="600519", stock_name="茅台",
            entry_price=100.0, exit_price=110.0,
            buy_reason="测试", sell_reason="测试",
            strategy_tag="测试", round_number="T80",
        )
        assert any("行情校验跳过" in w for w in r["market_warnings"])

    def test_warn_does_not_block_write(self, kb_market):
        with sqlite3.connect(kb_market.db) as conn:
            before = conn.execute("SELECT COUNT(*) FROM mistake_book").fetchone()[0]
        self._record(kb_market, 99.0, 6.0)  # 必触发 WARN
        with sqlite3.connect(kb_market.db) as conn:
            after = conn.execute("SELECT COUNT(*) FROM mistake_book").fetchone()[0]
        assert after == before + 1
