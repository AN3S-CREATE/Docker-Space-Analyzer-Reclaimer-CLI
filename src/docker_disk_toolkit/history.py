"""Historical trend tracking — append-only JSONL + rebuildable SQLite index.

The JSONL file (``history.jsonl``) is the durable, human-greppable source of
truth: one summary line per run. The SQLite index (``history.db``) is a
disposable, fast query layer rebuilt from the JSONL with
:func:`rebuild_index`. Trend queries (``space freed X times in 30 days, avg
Y``) run against SQLite.
"""

from __future__ import annotations

import json
import os
import sqlite3
import statistics
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .models import CleanupResult, TrendStats
from .utils import (  # noqa: F401  (atomic_write_text re-exported for convenience)
    atomic_write_text,
    ensure_private_dir,
    file_lock,
)

# How long a writer waits for a competing writer before giving up. Scheduled
# and interactive runs routinely overlap on a busy host.
_BUSY_TIMEOUT_S = 10.0
_RETRY_SLEEP_S = 0.05

# Individual statements rather than one ``executescript`` blob: executescript
# issues an implicit COMMIT and takes a write lock for the whole batch, which
# is precisely what collides when two runs initialise the database at once.
_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY,
        ts TEXT NOT NULL,
        command TEXT NOT NULL,
        level INTEGER,
        dry_run INTEGER NOT NULL,
        freed_bytes INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS deletions (
        run_id TEXT NOT NULL,
        ts TEXT NOT NULL,
        kind TEXT NOT NULL,
        object_id TEXT,
        reclaim_bytes INTEGER NOT NULL DEFAULT 0,
        outcome TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_runs_ts ON runs(ts)",
    "CREATE INDEX IF NOT EXISTS idx_deletions_ts ON deletions(ts)",
    "CREATE INDEX IF NOT EXISTS idx_deletions_kind ON deletions(kind)",
)


def _summary_line(result: CleanupResult, *, command: str, ts: datetime) -> dict[str, Any]:
    """Build the JSONL summary record for a cleanup run."""
    return {
        "run_id": result.correlation_id,
        "ts": ts.isoformat(),
        "command": command,
        "level": int(result.level),
        "dry_run": result.dry_run,
        "freed_bytes": result.reclaimed_bytes,
        "deletions": [
            {
                "kind": e.object_kind.value,
                "object_id": e.object_id,
                "reclaim_bytes": e.reclaim_actual_bytes or e.reclaim_predicted_bytes,
                "outcome": e.outcome,
            }
            for e in result.audit_events
            if e.outcome == "removed"
        ],
    }


def record_run(
    result: CleanupResult,
    *,
    jsonl_path: Path,
    db_path: Path,
    command: str = "cleanup",
    ts: datetime | None = None,
) -> None:
    """Append a run summary to JSONL and upsert it into the SQLite index."""
    ts = ts or datetime.now(UTC)
    record = _summary_line(result, command=command, ts=ts)
    ensure_private_dir(jsonl_path.parent)
    # The JSONL is the durable source of truth, so the append is locked against
    # concurrent scheduled/interactive runs before the disposable index update.
    with file_lock(jsonl_path), jsonl_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    with closing(_connect(db_path)) as conn:
        _upsert(conn, record)


def _initialise(conn: sqlite3.Connection) -> None:
    """Apply pragmas and DDL, tolerating a concurrent initialiser.

    Enabling WAL and creating tables both need a write lock, and for those
    SQLite answers ``SQLITE_BUSY`` *without* consulting the busy handler — so
    ``busy_timeout`` alone does not prevent a crash when a scheduled run and an
    interactive run open a fresh database simultaneously. Retrying briefly lets
    the loser observe the winner's schema instead of failing the run.
    """
    deadline = time.monotonic() + _BUSY_TIMEOUT_S
    while True:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            for statement in _SCHEMA_STATEMENTS:
                conn.execute(statement)
            conn.commit()
            return
        except sqlite3.OperationalError:
            conn.rollback()
            if time.monotonic() >= deadline:
                raise
            time.sleep(_RETRY_SLEEP_S)


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open the history index, creating the schema if needed.

    A scheduled run and an interactive run can overlap, so the connection sets
    ``busy_timeout``: without it SQLite raises ``database is locked``
    immediately rather than waiting for the other writer to commit.
    """
    ensure_private_dir(db_path.parent)
    conn = sqlite3.connect(db_path, timeout=_BUSY_TIMEOUT_S)
    conn.execute(f"PRAGMA busy_timeout={int(_BUSY_TIMEOUT_S * 1000)}")
    _initialise(conn)
    return conn


def _upsert(conn: sqlite3.Connection, record: dict[str, Any]) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO runs(run_id, ts, command, level, dry_run, freed_bytes) "
        "VALUES(?,?,?,?,?,?)",
        (
            record["run_id"],
            record["ts"],
            record["command"],
            record["level"],
            1 if record["dry_run"] else 0,
            record["freed_bytes"],
        ),
    )
    conn.execute("DELETE FROM deletions WHERE run_id = ?", (record["run_id"],))
    for deletion in record.get("deletions", []):
        conn.execute(
            "INSERT INTO deletions(run_id, ts, kind, object_id, reclaim_bytes, outcome) "
            "VALUES(?,?,?,?,?,?)",
            (
                record["run_id"],
                record["ts"],
                deletion["kind"],
                deletion["object_id"],
                deletion["reclaim_bytes"],
                deletion["outcome"],
            ),
        )
    conn.commit()


def rebuild_index(*, jsonl_path: Path, db_path: Path) -> int:
    """Rebuild the SQLite index by replaying the JSONL log.

    Returns:
        The number of run records replayed.
    """
    for suffix in ("", "-wal", "-shm"):
        candidate = db_path.with_name(db_path.name + suffix)
        if candidate.exists():
            candidate.unlink()
    count = 0
    with closing(_connect(db_path)) as conn:
        if jsonl_path.exists():
            for line in jsonl_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                _upsert(conn, record)
                count += 1
    return count


def trend(*, db_path: Path, window: timedelta, now: datetime | None = None) -> TrendStats:
    """Compute aggregate cleanup statistics over the trailing ``window``."""
    now = now or datetime.now(UTC)
    window_days = max(int(window.total_seconds() // 86400), 1)
    cutoff = (now - window).isoformat()

    if not db_path.exists():
        return TrendStats(window_days=window_days)

    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT ts, freed_bytes, dry_run FROM runs WHERE ts >= ? ORDER BY ts",
            (cutoff,),
        ).fetchall()
        kind_rows = conn.execute(
            "SELECT kind, SUM(reclaim_bytes) FROM deletions WHERE ts >= ? GROUP BY kind",
            (cutoff,),
        ).fetchall()

    real_freed = [int(freed) for _, freed, dry in rows if not dry and freed]
    total = sum(real_freed)
    cleanups = sum(1 for _, _, dry in rows if not dry)
    series = [(ts[:10], int(freed)) for ts, freed, dry in rows if not dry]
    return TrendStats(
        window_days=window_days,
        runs=len(rows),
        cleanups=cleanups,
        total_freed_bytes=total,
        avg_freed_bytes=int(total / len(real_freed)) if real_freed else 0,
        median_freed_bytes=int(statistics.median(real_freed)) if real_freed else 0,
        largest_single_reclaim_bytes=max(real_freed, default=0),
        freed_by_kind={str(k): int(v or 0) for k, v in kind_rows},
        series=series,
    )
