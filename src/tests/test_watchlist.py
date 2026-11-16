"""观察池（单一数据源）测试"""

import pytest

from src.watchlist import WatchlistStore


@pytest.fixture()
def store(tmp_path):
    return WatchlistStore(str(tmp_path / "market_test.db"))


def test_seed_defaults(store):
    """首次运行自动写入演示观察池（5 只）"""
    items = store.all()
    assert len(items) == 5
    assert {i.symbol for i in items} == {
        "sh600535", "sh600019", "sz002366", "sz002202", "sz300880",
    }


def test_add_and_get(store):
    store.add("sz000001", "000001", "平安银行", sector="银行",
              strategy_tag="低估值", source="research", note="测试")
    item = store.get("sz000001")
    assert item is not None
    assert item.sector == "银行"
    assert item.source == "research"
    assert item.status == "active"


def test_add_overwrites(store):
    store.add("sz000001", "000001", "平安银行", cost_price=10.0)
    store.add("sz000001", "000001", "平安银行", cost_price=11.0)
    assert store.get("sz000001").cost_price == 11.0


def test_remove_soft_delete(store):
    store.add("sz000001", "000001", "平安银行")
    assert store.remove("sz000001", "评分不达标") is True
    assert store.get("sz000001").status == "removed"
    assert all(i.symbol != "sz000001" for i in store.all())
    assert store.remove("sz000001") is False  # 已移除，不可重复删


def test_export_markdown(store):
    store.add("sh600535", "600535", "示例医药", sector="医药",
              strategy_tag="示例策略", note="示例")
    store.add("sz002202", "002202", "示例风电", sector="电力设备/风电",
              strategy_tag="示例策略", note="示例")
    md = store.export_markdown()
    assert "观察池" in md
    assert "示例医药" in md
    assert "示例风电" in md


def test_import_markdown(tmp_path, store):
    md = tmp_path / "pool.md"
    md.write_text(
        """# 动态股票池

### A. 当前推荐组合
| 股票名称 | 代码 | 价格 | PE | 净利 | 行业 | 评分 |
|---------|------|------|-----|------|------|------|
| 宗申动力 | 001696 | 14.50 | 27.98 | +44% | 低空经济 | 88 |
| 天士力 | 600535 | 16.00 | — | +54% | 医药生物 | 89 |

### B. 候补股票
| 序号 | 股票名称 | 代码 | 价格 | PE | 净利 | 行业 | 备注 |
|:---:|---------|------|------|-----|------|------|------|
| 1 | 晋控煤业 | 601001 | 19.28 | 15.61 | +70% | 煤炭 | ✅备选 |

### C. 不合格（不入池）
| 股票名称 | 代码 | 原因 |
|---------|------|------|
| 航天电子 | 600879 | 价格超限 |
""",
        encoding="utf-8",
    )
    added, skipped = store.import_markdown(str(md))
    assert added == 2          # 宗申动力 + 晋控煤业（天士力已存在 → 跳过；C 节不导入）
    assert skipped == 1
    assert store.get("sz001696") is not None
    assert store.get("sz001696").sector == "低空经济"
    assert store.get("sh601001").sector == "煤炭"
    assert store.get("sh600879") is None      # C 节未导入


def test_monthly_check_suggestions(tmp_path, store):
    """月度淘汰检查：超价格区间/无数据 → 建议；正常标的不出建议（#16）"""
    import sqlite3

    from src.data_source import StaticDataSource
    from src.pipeline.schema import init_market_db

    with sqlite3.connect(store.db_path) as conn:
        init_market_db(conn)
        conn.execute(
            """INSERT INTO daily_bars
               (symbol, date, open, close, high, low, volume, amount, rows_source)
               VALUES ('sh600019','2026-08-05',6.1,6.11,6.2,6.0,0,0,'test')"""
        )
        conn.execute(
            """INSERT INTO daily_bars
               (symbol, date, open, close, high, low, volume, amount, rows_source)
               VALUES ('sz002202','2026-08-05',19.0,25.0,25.5,18.5,0,0,'test')"""
        )
    result = store.monthly_check(
        today="2026-08-05",
        valuation_provider=lambda s: {"pe": 20, "pb": 2},
        source=StaticDataSource(),   # 无外部行情 → 其余标的标"无行情数据"
    )
    names = {s["name"] for s in result["suggestions"]}
    assert "金风科技" in names        # 25.0 超 5-20 区间
    assert "宝钢股份" not in names    # 6.11 在区间内
    assert any(s["name"] == "迦南智能" for s in result["suggestions"])  # 无数据 → 建议


def test_monthly_apply_removes_hard_rules(tmp_path, store):
    """hard 规则（数据长期陈旧）→ --monthly-apply 自动软删；
    价格区间 8/8 #1 修订后仅提示、不自动删除（避免误杀主赛道高价达标候选）"""
    import sqlite3

    from src.data_source import StaticDataSource
    from src.pipeline.schema import init_market_db

    with sqlite3.connect(store.db_path) as conn:
        init_market_db(conn)
        # 价格 >20：仅提示，不自动删除
        conn.execute(
            """INSERT INTO daily_bars
               (symbol, date, open, close, high, low, volume, amount, rows_source)
               VALUES ('sz002202','2026-08-05',19.0,25.0,25.5,18.5,0,0,'test')"""
        )
        # 数据长期陈旧（>30天）：hard → 自动删除
        conn.execute(
            """INSERT INTO daily_bars
               (symbol, date, open, close, high, low, volume, amount, rows_source)
               VALUES ('sh600019','2026-06-01',6.0,6.1,6.2,5.9,0,0,'test')"""
        )
    result = store.monthly_apply(
        today="2026-08-05",
        valuation_provider=lambda s: {"pe": 20, "pb": 2},
        source=StaticDataSource(),
    )
    removed_syms = {s["symbol"] for s in result["removed"]}
    assert "sh600019" in removed_syms      # 数据陈旧 → 自动删
    assert "sz002202" not in removed_syms  # 仅价格出界 → 提示不删
    assert store.get("sh600019").status == "removed"
    assert store.get("sz002202").status == "active"


def test_restore_soft_deleted(store):
    """回补误删：--restore 把 removed 恢复为 active 并写审计备注"""
    store.add("sz000001", "000001", "平安银行")
    assert store.remove("sz000001", "误删测试") is True
    assert store.get("sz000001").status == "removed"
    assert store.restore("sz000001", "回补：8/5 规则冲突误删") is True
    assert store.get("sz000001").status == "active"
    assert store.get("sz000001").note == "回补：8/5 规则冲突误删"
    assert store.restore("sz000001") is False  # 已是 active，不可重复恢复
