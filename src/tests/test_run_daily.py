"""守护编排配置测试（#116：盘前阶段给更长重试窗口；2026-11-06 缺口检测/心跳）"""

import json
import os
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))) 

import run_daily as rd
from run_daily import PHASES, PHASE_MAX_ATTEMPTS, _missing_phases, _backfill_plan


def test_phase_max_attempts_covers_all_phases():
    names = {p[0] for p in PHASES}
    assert names == {"sector", "lhb", "auction", "close"}
    assert set(PHASE_MAX_ATTEMPTS) >= names


def test_preopen_phases_get_longer_window():
    """早盘 DNS/网络间歇不可用可达 1 小时：sector/lhb 窗口应到 ~10:30"""
    assert PHASE_MAX_ATTEMPTS["sector"] == 11   # 08:50 + 10×10min ≈ 10:30
    assert PHASE_MAX_ATTEMPTS["lhb"] == 11
    assert PHASE_MAX_ATTEMPTS["auction"] == 3
    assert PHASE_MAX_ATTEMPTS["close"] == 3


# ---------- 硬时点 / LATE（2026-11-09 竞价预案 13:28 才成功） ----------

def test_auction_deadline_rejects_late_backfill():
    """竞价预案 09:26 触发、9:30 开盘即失效；过点补做只能记 LATE"""
    assert rd._deadline_passed("auction", datetime(2026, 11, 9, 9, 26)) is False
    assert rd._deadline_passed("auction", datetime(2026, 11, 9, 9, 30)) is True
    assert rd._deadline_passed("auction", datetime(2026, 11, 9, 13, 28)) is True
    # 其余阶段没有硬时点，晚到仍可跑
    for phase in ("sector", "lhb", "close"):
        assert rd._deadline_passed(phase, datetime(2026, 11, 9, 13, 28)) is False


def test_late_is_terminal_so_no_retry():
    """LATE 必须算终态：否则守护会在当天反复重试，继续产出事后文件"""
    assert "LATE" in rd.TERMINAL_STATUS
    state = {("2026-11-09", "sector"): "OK", ("2026-11-09", "lhb"): "OK",
             ("2026-11-09", "auction"): "LATE"}
    assert _missing_phases("2026-11-09", state) == ["close"]


def test_pid_alive_on_self_and_missing_pid():
    """重写后的存活判定：本进程必须判活；不存在的 PID 必须判死"""
    assert rd._pid_alive(os.getpid()) is True
    assert rd._pid_alive(999999) is False


# ---------- 缺口检测 / 补跑时间窗 ----------

def test_gap_detection_finds_missing_close():
    """11-04 真实形态：sector/lhb/auction 有终态、close 整段缺失"""
    state = {
        ("2026-11-04", "sector"): "FAIL",
        ("2026-11-04", "lhb"): "OK",
        ("2026-11-04", "auction"): "OK",
    }
    assert _missing_phases("2026-11-04", state) == ["close"]


def test_gap_detection_returns_empty_when_all_terminal():
    state = {("2026-11-04", p): "OK" for p in ("sector", "lhb", "auction", "close")}
    assert _missing_phases("2026-11-04", state) == []

    state["close"] = "DEGRADED"     # DEGRADED 也算终态（不重复补跑）
    assert _missing_phases("2026-11-04", state) == []


def test_recent_gaps_finds_1104_close(monkeypatch):
    """启动缺口检测窗口覆盖最近 5 个交易日：11-04 缺收盘必须被识别"""
    monkeypatch.setattr(rd, "_recent_trading_days",
                        lambda day, n=5, market_db=None:
                        ["2026-11-06", "2026-11-05", "2026-11-04"])
    state = {
        ("2026-11-06", p): "OK" for p in ("sector", "lhb", "auction", "close")
    }
    state.update({("2026-11-05", p): "OK" for p in ("sector", "lhb", "auction", "close")})
    state.update({("2026-11-04", "lhb"): "OK", ("2026-11-04", "auction"): "OK",
                  ("2026-11-04", "sector"): "FAIL"})
    gaps = rd._recent_gaps("2026-11-05", 5, state=state)
    assert gaps == [{"date": "2026-11-04", "phases": ["close"]}]


def test_recent_gaps_empty_when_complete(monkeypatch):
    monkeypatch.setattr(rd, "_recent_trading_days",
                        lambda day, n=5, market_db=None: ["2026-11-06", "2026-11-05"])
    state = {(d, p): "OK" for d in ("2026-11-06", "2026-11-05")
             for p in ("sector", "lhb", "auction", "close")}
    assert rd._recent_gaps("2026-11-06", 5, state=state) == []


def test_backfill_not_allowed_before_close_time():
    state = {("2026-11-06", p): "OK" for p in ("sector", "lhb", "auction")}
    plan = _backfill_plan(datetime(2026, 11, 6, 10, 0), "15:35", state, trading_day=True)
    assert plan == []


def test_backfill_close_after_close_time():
    state = {("2026-11-06", p): "OK" for p in ("sector", "lhb", "auction")}
    plan = _backfill_plan(datetime(2026, 11, 6, 16, 0), "15:35", state, trading_day=True)
    assert plan == ["close"]        # 收盘缺失优先补收盘


def test_backfill_skipped_on_non_trading_day():
    plan = _backfill_plan(datetime(2026, 11, 7, 16, 0), "15:35", {}, trading_day=False)
    assert plan == []


def test_backfill_skipped_when_close_already_done():
    state = {("2026-11-06", p): "OK" for p in ("sector", "lhb", "auction", "close")}
    plan = _backfill_plan(datetime(2026, 11, 6, 20, 0), "15:35", state, trading_day=True)
    assert plan == []


def test_last_trading_day_uses_trade_calendar(tmp_path):
    db = tmp_path / "market.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE trade_calendar (date TEXT PRIMARY KEY, is_trading INTEGER)")
        conn.execute("INSERT INTO trade_calendar VALUES ('2026-11-05', 1)")
        conn.execute("INSERT INTO trade_calendar VALUES ('2026-11-06', 1)")
    assert rd._last_trading_day("2026-11-06", str(db)) == "2026-11-06"
    # 日历无记录 → 工作日兜底（2016-11-05 是周六 → 回退到周五 11-04）
    assert rd._last_trading_day("2016-11-05", str(db)) == "2016-11-04"


# ---------- 心跳 ----------

def test_heartbeat_written(tmp_path):
    path = tmp_path / "hb.json"
    rd._write_heartbeat("task-scheduler", status="running", phase="close", path=path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["source"] == "task-scheduler"
    assert data["status"] == "running"
    assert data["phase"] == "close"
    assert data["pid"] == os.getpid()
    assert data["last_run"]


def test_heartbeat_overwrites_not_appends(tmp_path):
    path = tmp_path / "hb.json"
    rd._write_heartbeat("vbs", status="idle", phase="watch-loop", path=path)
    rd._write_heartbeat("manual", status="running", phase="lhb", path=path)
    assert json.loads(path.read_text(encoding="utf-8"))["source"] == "manual"
    assert len(list(tmp_path.glob("hb.json")) ) == 1


# ---------- close 状态：健康检查 FAIL → DEGRADED ----------

def test_health_check_fail_returns_degraded(monkeypatch):
    monkeypatch.setattr(rd, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(rd, "_log_watch", lambda *a, **k: None)

    def fake_run(*args):
        return 2 if args[0] == "health_check.py" else 0

    monkeypatch.setattr(rd, "_run", fake_run)
    assert rd._run_daily_flow(backups=False) == 3      # DEGRADED


def _stub_close(monkeypatch):
    """钝化通知/日志，返回 (fake_run 工厂用到的记录, notes)"""
    notes = []
    monkeypatch.setattr(rd, "_notify", lambda *a, **k: notes.append(a))
    monkeypatch.setattr(rd, "_log_watch", lambda *a, **k: None)
    return notes


def test_health_check_crash_returns_degraded(monkeypatch):
    """健康检查自身崩溃（exit=3）不得 fail-open 记 OK：close 必须 DEGRADED 并告警"""
    notes = _stub_close(monkeypatch)

    def fake_run(*args):
        return 3 if args[0] == "health_check.py" else 0

    monkeypatch.setattr(rd, "_run", fake_run)
    assert rd._run_daily_flow(backups=False) == 3
    assert any("HEALTHCHECK CRASH" in a[0] for a in notes)


def test_health_check_unexpected_exit_returns_degraded(monkeypatch):
    """超时(124)等非预期退出码同样判 degraded，不只认 1 和 2"""
    notes = _stub_close(monkeypatch)

    def fake_run(*args):
        return 124 if args[0] == "health_check.py" else 0

    monkeypatch.setattr(rd, "_run", fake_run)
    assert rd._run_daily_flow(backups=False) == 3
    assert any("HEALTHCHECK CRASH" in a[0] for a in notes)


def test_audit_crash_returns_degraded(monkeypatch):
    """硬检查自身崩溃（exit=3）同样必须 degraded，原实现只认 rc==2"""
    notes = _stub_close(monkeypatch)

    def fake_run(*args):
        return 3 if args[:2] == ("-m", "src.daily_audit") else 0

    monkeypatch.setattr(rd, "_run", fake_run)
    assert rd._run_daily_flow(backups=False) == 3
    assert any("DAILY AUDIT CRASH" in a[0] for a in notes)


def test_audit_unexpected_exit_returns_degraded(monkeypatch):
    """硬检查非 0 非 2 的退出码（此前只写 watch 日志）一律判 degraded"""
    notes = _stub_close(monkeypatch)

    def fake_run(*args):
        return 1 if args[:2] == ("-m", "src.daily_audit") else 0

    monkeypatch.setattr(rd, "_run", fake_run)
    assert rd._run_daily_flow(backups=False) == 3
    assert any("DAILY AUDIT CRASH" in a[0] for a in notes)


def test_health_check_warn_only_still_ok(monkeypatch):
    """只有 WARN（exit=1，无 FAIL/崩溃）时 close 仍记 OK，只写 watch 日志"""
    notes = _stub_close(monkeypatch)

    def fake_run(*args):
        return 1 if args[0] == "health_check.py" else 0

    monkeypatch.setattr(rd, "_run", fake_run)
    assert rd._run_daily_flow(backups=False) == 0
    assert notes == []


def test_health_check_entry_crash_returns_exit_3(monkeypatch):
    """健康检查入口把未捕获异常映射成独立退出码 3（而不是 1=有 WARN）"""
    import health_check as hc

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(sys, "argv", ["health_check.py", "--date", "2026-11-10"])
    monkeypatch.setattr(hc, "check_schedule", boom)
    assert hc.main() == hc.EXIT_CRASH


def test_degraded_status_is_terminal(tmp_path, monkeypatch):
    monkeypatch.setattr(rd, "STATE_FILE", tmp_path / "state.txt")
    rd._mark_ran("2026-11-06", "close", "DEGRADED")
    assert rd._finished("2026-11-06", "close") is True
    assert (tmp_path / "state.txt").read_text(encoding="ascii").strip() == \
        "2026-11-06 close DEGRADED"


def test_backup_failure_does_not_block_close(monkeypatch):
    """备份失败只告警：收盘流程仍返回 0"""
    monkeypatch.setattr(rd, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(rd, "_log_watch", lambda *a, **k: None)

    def fake_run(*args):
        return 1 if args[:2] == ("-m", "src.backup_db") else 0

    monkeypatch.setattr(rd, "_run", fake_run)
    assert rd._run_daily_flow() == 0


# ---------- sector 同日多时段重试 / 失败原因分类 ----------

def test_sector_retry_slots_cover_premarket_to_close():
    """08:50 首发；覆盖早盘 DNS 窗口；15:10 为 15:35 收盘前最后时点"""
    slots = rd.SECTOR_RETRY_SLOTS
    assert slots[0] == "08:50" and slots[-1] == "15:10"
    assert list(slots) == sorted(slots), "时段必须递增，守护按序推进"
    assert len(slots) == 8


def test_classify_failure_network():
    code, reason = rd._classify_failure(
        1, "", "requests.exceptions.ConnectionError: ... getaddrinfo failed")
    assert code == "network"
    assert "网络不可达" in reason


def test_classify_failure_dns_in_stdout():
    code, reason = rd._classify_failure(
        1, "板块列表拉取失败：NameResolutionError ...", "")
    assert code == "network"


def test_classify_failure_incomplete_data():
    code, reason = rd._classify_failure(
        1, "❌ 板块快照不完整（2026-11-06）：资金字段全 NULL/0 或板块列表不可用", "")
    assert code == "incomplete"
    assert "数据源返回不完整" in reason


def test_classify_failure_timeout_is_network():
    code, _ = rd._classify_failure(124, "", "TimeoutExpired")
    assert code == "network"


def test_classify_failure_unknown():
    code, reason = rd._classify_failure(1, "", "Traceback: KeyError")
    assert code == "unknown" and "exit=1" in reason


def test_retry_records_then_ok_not_overwritten(tmp_path, monkeypatch):
    """重试写 RETRYn/reason（非终态）；成功后终态不得被后续重试覆盖"""
    monkeypatch.setattr(rd, "STATE_FILE", tmp_path / "state.txt")
    rd._mark_retry("2026-11-06", "sector", 1, "network")
    assert rd._read_state()[("2026-11-06", "sector")] == "RETRY1/network"
    assert rd._finished("2026-11-06", "sector") is False

    rd._mark_retry("2026-11-06", "sector", 2, "incomplete")
    assert rd._read_state()[("2026-11-06", "sector")] == "RETRY2/incomplete"

    rd._mark_ran("2026-11-06", "sector", "OK")
    assert rd._finished("2026-11-06", "sector") is True
    rd._mark_retry("2026-11-06", "sector", 3, "network")   # 迟到重试
    assert rd._read_state()[("2026-11-06", "sector")] == "OK"


def test_sector_snapshot_ok_reads_meta(tmp_path):
    """收盘后收敛依据是库内快照 meta，不是猜测"""
    db = tmp_path / "m.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE sector_snapshot_meta (date TEXT, complete INTEGER)")
        conn.execute("INSERT INTO sector_snapshot_meta VALUES ('2026-11-06', 1)")
        conn.execute("INSERT INTO sector_snapshot_meta VALUES ('2026-11-05', 0)")
    assert rd._sector_snapshot_ok("2026-11-06", str(db)) is True
    assert rd._sector_snapshot_ok("2026-11-05", str(db)) is False
    assert rd._sector_snapshot_ok("2026-11-04", str(db)) is False


def test_settle_sector_after_close(tmp_path, monkeypatch):
    """闭环补拉成功 → OK；未成功 → FAIL，避免 RETRY 永久 pending"""
    monkeypatch.setattr(rd, "STATE_FILE", tmp_path / "state.txt")
    monkeypatch.setattr(rd, "_log_watch", lambda *a, **k: None)
    monkeypatch.setattr(rd, "_sector_snapshot_ok", lambda day, market_db=None: True)
    rd._mark_retry("2026-11-06", "sector", 1, "network")
    rd._settle_sector_after_close("2026-11-06")
    assert rd._read_state()[("2026-11-06", "sector")] == "OK"

    rd.STATE_FILE.unlink()
    monkeypatch.setattr(rd, "_sector_snapshot_ok", lambda day, market_db=None: False)
    rd._mark_retry("2026-11-06", "sector", 1, "incomplete")
    rd._settle_sector_after_close("2026-11-06")
    assert rd._read_state()[("2026-11-06", "sector")] == "FAIL"
