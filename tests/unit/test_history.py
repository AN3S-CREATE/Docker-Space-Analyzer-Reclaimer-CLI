"""Unit tests for :mod:`docker_disk_toolkit.history`."""

from __future__ import annotations

import threading
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from docker_disk_toolkit import history
from docker_disk_toolkit.models import (
    AuditEvent,
    CleanupPlan,
    CleanupResult,
    ObjectKind,
    PruneLevel,
)

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


def _result(run_id: str, freed: int, *, dry_run: bool = False) -> CleanupResult:
    events = [
        AuditEvent(
            ts=NOW,
            run_id=run_id,
            level=PruneLevel.SAFE,
            dry_run=dry_run,
            object_kind=ObjectKind.IMAGE,
            object_id=f"{run_id}-img",
            object_name="img",
            reclaim_predicted_bytes=freed,
            reclaim_actual_bytes=freed,
            outcome="removed",
        )
    ]
    return CleanupResult(
        correlation_id=run_id,
        level=PruneLevel.SAFE,
        dry_run=dry_run,
        plan=CleanupPlan(level=PruneLevel.SAFE),
        audit_events=events,
    )


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "history.jsonl", tmp_path / "history.db"


class TestRecordAndTrend:
    def test_record_and_query(self, tmp_path: Path) -> None:
        jsonl, db = _paths(tmp_path)
        for i, freed in enumerate([1_000_000_000, 3_000_000_000, 2_000_000_000]):
            history.record_run(
                _result(f"r{i}", freed),
                jsonl_path=jsonl,
                db_path=db,
                ts=NOW - timedelta(days=i),
            )
        stats = history.trend(db_path=db, window=timedelta(days=30), now=NOW)
        assert stats.cleanups == 3
        assert stats.total_freed_bytes == 6_000_000_000
        assert stats.avg_freed_bytes == 2_000_000_000
        assert stats.median_freed_bytes == 2_000_000_000
        assert stats.largest_single_reclaim_bytes == 3_000_000_000
        assert stats.freed_by_kind.get("image") == 6_000_000_000

    def test_window_excludes_old(self, tmp_path: Path) -> None:
        jsonl, db = _paths(tmp_path)
        history.record_run(_result("recent", 5_000_000_000), jsonl_path=jsonl, db_path=db, ts=NOW)
        history.record_run(
            _result("old", 9_000_000_000),
            jsonl_path=jsonl,
            db_path=db,
            ts=NOW - timedelta(days=60),
        )
        stats = history.trend(db_path=db, window=timedelta(days=30), now=NOW)
        assert stats.cleanups == 1
        assert stats.total_freed_bytes == 5_000_000_000

    def test_dry_run_not_counted(self, tmp_path: Path) -> None:
        jsonl, db = _paths(tmp_path)
        history.record_run(
            _result("dry", 5_000_000_000, dry_run=True), jsonl_path=jsonl, db_path=db, ts=NOW
        )
        stats = history.trend(db_path=db, window=timedelta(days=30), now=NOW)
        assert stats.cleanups == 0 and stats.total_freed_bytes == 0
        assert stats.runs == 1  # the run is still recorded

    def test_trend_no_db(self, tmp_path: Path) -> None:
        stats = history.trend(db_path=tmp_path / "missing.db", window=timedelta(days=7), now=NOW)
        assert stats.runs == 0 and stats.window_days == 7


class TestRebuildIndex:
    def test_rebuild_from_jsonl(self, tmp_path: Path) -> None:
        jsonl, db = _paths(tmp_path)
        history.record_run(_result("a", 1_000_000_000), jsonl_path=jsonl, db_path=db, ts=NOW)
        history.record_run(_result("b", 2_000_000_000), jsonl_path=jsonl, db_path=db, ts=NOW)
        # nuke and rebuild solely from the JSONL source of truth
        count = history.rebuild_index(jsonl_path=jsonl, db_path=db)
        assert count == 2
        stats = history.trend(db_path=db, window=timedelta(days=30), now=NOW)
        assert stats.total_freed_bytes == 3_000_000_000

    def test_rebuild_tolerates_bad_lines(self, tmp_path: Path) -> None:
        jsonl, db = _paths(tmp_path)
        jsonl.write_text(
            '{"run_id":"x","ts":"2026-07-21T12:00:00+00:00","command":"cleanup",'
            '"level":1,"dry_run":false,"freed_bytes":100,"deletions":[]}\n'
            "not json\n\n",
            encoding="utf-8",
        )
        assert history.rebuild_index(jsonl_path=jsonl, db_path=db) == 1


class TestConcurrency:
    """A scheduled run and an interactive run routinely overlap."""

    def test_connection_sets_busy_timeout(self, tmp_path: Path) -> None:
        _, db = _paths(tmp_path)
        with closing(history._connect(db)) as conn:
            timeout_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout_ms >= 1000, "a zero busy_timeout fails instantly under contention"

    def test_concurrent_writers_do_not_raise_database_locked(self, tmp_path: Path) -> None:
        jsonl, db = _paths(tmp_path)
        errors: list[BaseException] = []

        def writer(prefix: str) -> None:
            try:
                for i in range(10):
                    history.record_run(
                        _result(f"{prefix}{i}", 1_000_000),
                        jsonl_path=jsonl,
                        db_path=db,
                        ts=NOW,
                    )
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(p,)) for p in ("a", "b", "c")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        assert not errors, f"concurrent writers raised: {errors!r}"
        stats = history.trend(db_path=db, window=timedelta(days=30), now=NOW)
        assert stats.runs == 30
