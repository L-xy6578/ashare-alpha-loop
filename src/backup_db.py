"""数据库每日备份（VACUUM INTO，保留最近 N 份）

为什么用 VACUUM INTO 而不是直接复制文件：
    - 复制 SQLite 主文件会漏掉 WAL 里尚未折叠的最新数据；
    - VACUUM INTO 产生一个"读事务一致快照"的干净副本（已压缩、无 WAL），
      恢复时可直接替换，不会带着 -wal/-shm 半成品。

目录：data/backups/（与 data/*.db 同级，*.db 已在 .gitignore 中，不会误入库）
命名：{库名}_{YYYY-MM-DD}.db（同日重复执行覆盖当日份，天然幂等）
轮转：每库只保留最近 keep 份（默认 7），按日期文件名排序淘汰最旧。
      只认严格自动备份名；手工快照（data/backups/manual/ 及自定义命名）不参与轮转。

失败策略：单库失败只记日志并返回非 0，不影响调用方主流程（收盘流程只告警）。

用法:
    python -m src.backup_db                  # 备份 market.db + knowledge.db，保留 7 份
    python -m src.backup_db --keep 3         # 只保留 3 份
    python -m src.backup_db --out data/backups --dry-run
"""

from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
from datetime import date
from pathlib import Path

from src.config import DATA_DIR, DB_PATH, MARKET_DB_PATH
from src.logger import get_logger

log = get_logger(__name__)

BACKUP_DIR = DATA_DIR / "backups"
DEFAULT_KEEP = 7
DEFAULT_DBS = (MARKET_DB_PATH, DB_PATH)


def _auto_backup_re(prefix: str) -> re.Pattern:
    """自动备份文件名的严格匹配（#119 P3）

    只认 {库名}_{YYYY-MM-DD}.db：手工快照（如 market_2026-11-06_before_d20fix.db）、
    临时文件、其他命名一律不参与轮转，避免被当作旧份删除。
    """
    return re.compile(rf"^{re.escape(prefix)}_\d{{4}}-\d{{2}}-\d{{2}}\.db$")


def _sidecars(db: Path) -> tuple[Path, Path]:
    """SQLite 边车文件（WAL / 共享内存索引）"""
    return Path(f"{db}-wal"), Path(f"{db}-shm")


def _finalize_snapshot(db: Path) -> list[Path]:
    """把快照规范化为自包含单文件（WAL 折回主库 + 清边车）

    边车为什么会出现：
      - 源库是 WAL 模式；降级路径（checkpoint+复制）只复制主文件，副本头里的
        journal_mode 仍是 WAL，之后任何进程（含校验、恢复演练）一打开就会生成
        -wal/-shm；VACUUM INTO 产物若被同源 WAL 副本替换也会继承该模式。
      - VACUUM INTO 本身写的是干净副本，正常情况下不会带边车。

    清理安全性：必须先 checkpoint 成功（busy=0，WAL 内容已折回主库）再删；
    busy=1 时保留 -wal 并告警，绝不删除可能仍有已提交数据的 WAL。-shm 只是
    共享内存索引，无连接时删除安全（下次打开会重建）。
    """
    if not db.exists():
        return []
    safe_wal = True
    try:
        conn = sqlite3.connect(str(db), timeout=10)
        try:
            mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            if mode == "wal":
                # 打开 WAL 库本身会生成边车：先折回主库，再切回 DELETE 模式
                busy, _, _ = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if busy == 0:
                    conn.execute("PRAGMA journal_mode=DELETE")
                else:
                    safe_wal = False
                    log.warning("快照 WAL checkpoint 未完成（busy=1），保留边车: %s", db.name)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        safe_wal = False
        log.warning("快照边车清理失败（%s）: %s", db.name, exc)

    removed: list[Path] = []
    wal, shm = _sidecars(db)
    if shm.exists():
        shm.unlink(missing_ok=True)
        removed.append(shm)
    if wal.exists() and safe_wal:
        wal.unlink(missing_ok=True)
        removed.append(wal)
    return removed


def _prune(prefix: str, out_dir: Path, keep: int,
           dry_run: bool = False) -> list[Path]:
    """按文件名日期倒序保留最近 keep 份，返回被删除（或 dry-run 下将被删除）的文件

    只匹配严格自动备份名，且只在 out_dir 顶层查找：
    手工快照目录 data/backups/manual/ 及其它命名文件不会进入轮转。
    """
    pattern = _auto_backup_re(prefix)
    files = sorted(
        (p for p in out_dir.glob(f"{prefix}_*.db")
         if p.is_file() and pattern.match(p.name)),
        key=lambda p: p.name, reverse=True,
    )
    removed = files[max(keep, 0):]
    if not dry_run:
        for p in removed:
            p.unlink(missing_ok=True)
    return removed



def backup_one(src: str | Path, out_dir: Path = BACKUP_DIR,
               day: str | None = None, keep: int = DEFAULT_KEEP,
               dry_run: bool = False) -> Path | None:
    """备份单个库；源库不存在/备份失败返回 None（不抛异常，不影响主流程）"""
    src = Path(src)
    if not src.exists():
        log.error("备份跳过：源库不存在 %s", src)
        return None
    day = day or date.today().isoformat()
    dst = out_dir / f"{src.stem}_{day}.db"
    if dry_run:
        log.info("[dry-run] 将备份 %s → %s（轮转保留 %d 份）", src, dst, keep)
        preview = _prune(src.stem, out_dir, keep, dry_run=True) if out_dir.exists() else []
        if preview:
            log.info("[dry-run] 轮转将删除 %d 份：%s",
                     len(preview), ", ".join(p.name for p in preview))
        return dst
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / f"{src.stem}_{day}.db.tmp"
    tmp.unlink(missing_ok=True)   # VACUUM INTO 要求目标文件不存在
    for sc in _sidecars(tmp):
        sc.unlink(missing_ok=True)
    try:
        with sqlite3.connect(str(src), timeout=10) as conn:
            conn.execute("VACUUM INTO ?", (str(tmp),))
    except sqlite3.Error as exc:
        # SQLite < 3.27 或库被长时间独占：退化为 checkpoint + 文件复制（仍保证可用）
        log.warning("VACUUM INTO 失败（%s），退化为 checkpoint+复制: %s", exc, src)
        tmp.unlink(missing_ok=True)
        try:
            with sqlite3.connect(str(src), timeout=10) as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as exc2:
            log.warning("WAL checkpoint 失败（%s），按现有主库复制", exc2)
        try:
            shutil.copy2(src, tmp)
        except OSError as exc3:
            log.error("备份失败（复制异常）: %s → %s（%s）", src, tmp, exc3)
            tmp.unlink(missing_ok=True)
            return None
    tmp.replace(dst)
    # 降级路径会把源库的 WAL 模式带到副本；统一折回并清掉边车，保证只有一个 .db
    for sc in _sidecars(tmp):       # tmp 已改名，残留的 tmp-wal/tmp-shm 一并清掉
        sc.unlink(missing_ok=True)
    _finalize_snapshot(dst)
    removed = _prune(src.stem, out_dir, keep)
    log.info("数据库备份完成 %s（%.1f MB）%s", dst, dst.stat().st_size / 1e6,
             f"，轮转删除 {len(removed)} 份旧备份" if removed else "")
    return dst


def run_backup(dbs=DEFAULT_DBS, out_dir: Path = BACKUP_DIR,
               keep: int = DEFAULT_KEEP, dry_run: bool = False) -> int:
    """备份全部库（单库失败继续其余），全部成功返回 0，否则 1"""
    failed = 0
    for src in dbs:
        if backup_one(src, out_dir=out_dir, keep=keep, dry_run=dry_run) is None:
            failed += 1
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    import sys as _sys
    _sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="数据库每日备份（VACUUM INTO，保留最近 N 份）")
    ap.add_argument("--keep", type=int, default=DEFAULT_KEEP, help="每库保留份数（默认 7）")
    ap.add_argument("--out", default=str(BACKUP_DIR), help="备份目录（默认 data/backups）")
    ap.add_argument("--db", action="append", dest="dbs", default=None,
                    help="指定库路径（可重复；默认 market.db + knowledge.db）")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不写文件")
    args = ap.parse_args(argv)
    dbs = tuple(args.dbs) if args.dbs else DEFAULT_DBS
    rc = run_backup(dbs, out_dir=Path(args.out), keep=args.keep, dry_run=args.dry_run)
    if rc == 0:
        print(f"✅ 数据库备份完成（目录 {args.out}，保留 {args.keep} 份）")
    else:
        print(f"⚠ 数据库备份部分失败（见 logs/app.log），目录 {args.out}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
