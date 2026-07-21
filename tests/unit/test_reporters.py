"""Unit / golden tests for :mod:`docker_disk_toolkit.reporters`."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

from rich.console import Console

from docker_disk_toolkit import reporters
from docker_disk_toolkit.models import (
    CleanupPlan,
    DiagnosticReport,
    ObjectKind,
    PruneLevel,
    Removable,
    SpaceDelta,
)


class TestJsonCsv:
    def test_render_json_keeps_bytes(self, make_report: Callable[..., DiagnosticReport]) -> None:
        report = make_report("typical")
        payload = reporters.render_json(report)
        data = json.loads(payload)
        assert data["correlation_id"] == "rid123"
        # bytes are integers, not humanised
        assert isinstance(data["docker"]["images"]["total_bytes"], int)

    def test_flatten_and_csv(self, make_report: Callable[..., DiagnosticReport]) -> None:
        report = make_report("typical")
        rows = reporters.flatten_report(report)
        kinds = {r["record_type"] for r in rows}
        assert {"disk", "image", "container", "volume"} <= kinds
        csv_text = reporters.to_csv(rows)
        assert csv_text.splitlines()[0] == "record_type,name,size_bytes,size_human,in_use,extra"
        assert "nginx:latest" in csv_text

    def test_csv_handles_none_size(self, make_report: Callable[..., DiagnosticReport]) -> None:
        report = make_report("typical")
        # webdata volume has size from df -v; force a None to exercise the branch
        report.docker.volume_list[0].size_bytes = None  # type: ignore[union-attr]
        rows = reporters.flatten_report(report)
        vol_rows = [r for r in rows if r["record_type"] == "volume"]
        assert any(r["size_human"] == "n/a" for r in vol_rows)


class TestMarkdown:
    def test_renders_docker_report(self, make_report: Callable[..., DiagnosticReport]) -> None:
        report = make_report("typical")
        md = reporters.render_markdown(report, command="analyze")
        assert "# Docker Disk Report" in md
        assert "Docker usage" in md
        assert "nginx:latest" in md
        assert "Findings" in md  # ai-ml finding present

    def test_renders_without_docker(self, make_report: Callable[..., DiagnosticReport]) -> None:
        report = make_report("not-installed")
        md = reporters.render_markdown(report, command="analyze")
        assert "Docker is **not_installed**" in md
        assert "not found on PATH" in md

    def test_renders_delta(self, make_report: Callable[..., DiagnosticReport]) -> None:
        report = make_report("typical")
        delta = SpaceDelta(
            host_free_before_bytes=300 * 1000**3,
            host_free_after_bytes=310 * 1000**3,
            docker_total_before_bytes=9 * 1000**3,
            docker_total_after_bytes=2 * 1000**3,
            reclaimed_bytes=7 * 1000**3,
        )
        md = reporters.render_markdown(report, command="cleanup", delta=delta)
        assert "Reclaimed this run" in md
        assert "Total reclaimed" in md


class TestReportFiles:
    def test_write_report_creates_pair(
        self, make_report: Callable[..., DiagnosticReport], make_config: Callable[..., object]
    ) -> None:
        report = make_report("typical")
        config = make_config()
        paths = reporters.write_report(report, config, command="analyze")  # type: ignore[arg-type]
        assert paths.json_path.exists() and paths.md_path.exists()
        assert "rid123" in paths.json_path.name
        assert json.loads(paths.json_path.read_text())["hostname"] == report.hostname

    def test_report_paths_slug(self) -> None:
        from pathlib import Path

        paths = reporters.report_paths(
            Path("/reports"),
            command="cleanup",
            run_id="abc",
            ts=datetime(2026, 7, 21, 9, 5, 30, tzinfo=UTC),
        )
        assert paths.json_path.name == "2026-07-21T09-05-30Z_cleanup_abc.json"


class TestAudit:
    def test_append_audit(self, make_config: Callable[..., object]) -> None:
        from docker_disk_toolkit.models import AuditEvent

        config = make_config()
        events = [
            AuditEvent(
                ts=datetime(2026, 7, 21, tzinfo=UTC),
                run_id="r1",
                level=PruneLevel.SAFE,
                dry_run=False,
                object_kind=ObjectKind.IMAGE,
                object_id="i1",
                object_name="img",
                outcome="removed",
            )
        ]
        path = reporters.append_audit(config, events)  # type: ignore[arg-type]
        # appended, one JSON object per line
        reporters.append_audit(config, events)  # type: ignore[arg-type]
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["object_id"] == "i1"


class TestTerminal:
    def _console(self) -> Console:
        return Console(record=True, width=100, force_terminal=False)

    def test_render_terminal_docker(self, make_report: Callable[..., DiagnosticReport]) -> None:
        console = self._console()
        reporters.render_terminal(make_report("typical"), console)
        text = console.export_text()
        assert "Docker usage" in text and "Findings" in text

    def test_render_terminal_no_docker(self, make_report: Callable[..., DiagnosticReport]) -> None:
        console = self._console()
        reporters.render_terminal(make_report("not-installed"), console)
        assert "unavailable" in console.export_text().lower()

    def test_render_plan_and_delta(self) -> None:
        console = self._console()
        plan = CleanupPlan(
            level=PruneLevel.SAFE,
            items=[Removable(kind=ObjectKind.IMAGE, id="i1", name="img", reclaim_bytes=1000)],
            protected=[Removable(kind=ObjectKind.VOLUME, id="v", name="db", reclaim_bytes=0)],
            total_reclaim_bytes=1000,
            docker_reported_reclaim_bytes=5000,
        )
        reporters.render_plan(plan, console)
        reporters.render_delta(
            SpaceDelta(host_free_before_bytes=1, host_free_after_bytes=2, reclaimed_bytes=1000),
            console,
        )
        text = console.export_text()
        assert "Cleanup plan" in text and "Reclaimed" in text
