"""Unit tests for :mod:`docker_disk_toolkit.models` and ``errors``."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from docker_disk_toolkit import errors
from docker_disk_toolkit.models import (
    AuditEvent,
    BuildCacheInfo,
    CategoryUsage,
    CleanupPlan,
    CleanupResult,
    ContainerInfo,
    DiagnosticReport,
    DiskUsage,
    DockerAvailability,
    DockerBackend,
    DockerProbe,
    DockerUsage,
    Finding,
    HealthStatus,
    ImageInfo,
    ObjectKind,
    PruneLevel,
    Removable,
    SpaceDelta,
    VolumeInfo,
)


class TestExitCodesAndErrors:
    def test_exit_codes(self) -> None:
        assert int(errors.ExitCode.HEALTHY) == 0
        assert int(errors.ExitCode.WARNING_CLEANED) == 1
        assert int(errors.ExitCode.CRITICAL) == 2
        assert int(errors.ExitCode.FATAL) == 3

    def test_toolkit_error_defaults_fatal(self) -> None:
        err = errors.ToolkitError("boom", remediation="do X")
        assert err.exit_code is errors.ExitCode.FATAL
        assert err.message == "boom"
        assert "do X" in err.remediation

    def test_docker_unavailable_fatal_flag(self) -> None:
        fatal = errors.DockerUnavailableError("no daemon", fatal=True)
        soft = errors.DockerUnavailableError("no daemon", fatal=False)
        assert fatal.exit_code is errors.ExitCode.FATAL
        assert soft.exit_code is errors.ExitCode.WARNING_CLEANED

    def test_docker_command_error_captures_context(self) -> None:
        err = errors.DockerCommandError(
            "cmd failed", argv=["docker", "rm", "x"], returncode=1, stderr="nope"
        )
        assert err.argv == ["docker", "rm", "x"]
        assert err.returncode == 1

    def test_rich_panel_renders(self) -> None:
        panel = errors.ConfigError("bad config", remediation="fix it").rich_panel()
        # Panel object is renderable; smoke-test that remediation text is present.
        from rich.console import Console

        console = Console(record=True, width=80)
        console.print(panel)
        text = console.export_text()
        assert "bad config" in text
        assert "fix it" in text


class TestImageInfo:
    def test_display_name_and_reclaim(self) -> None:
        tagged = ImageInfo(id="sha256:abc", repo_tags=["nginx:latest"], size_bytes=100)
        assert tagged.display_name == "nginx:latest"
        # reclaim prefers unique size when present
        img = ImageInfo(id="sha256:deadbeef" + "0" * 56, size_bytes=100, unique_size_bytes=40)
        assert img.reclaim_bytes == 40
        assert img.display_name.startswith("<none>@")

    def test_reclaim_falls_back_to_size(self) -> None:
        img = ImageInfo(id="sha256:x", size_bytes=100)
        assert img.reclaim_bytes == 100


class TestDockerUsageTotals:
    def test_totals_and_reclaimable(self) -> None:
        usage = DockerUsage(
            images=CategoryUsage(total_bytes=100, reclaimable_bytes=40),
            containers=CategoryUsage(total_bytes=10, reclaimable_bytes=5),
            volumes=CategoryUsage(total_bytes=1000, reclaimable_bytes=200),
            build_cache=CategoryUsage(total_bytes=50, reclaimable_bytes=50),
        )
        assert usage.total_bytes == 1160
        assert usage.reclaimable_bytes == 295


class TestVolumeAndContainer:
    def test_volume_reclaim_none_size(self) -> None:
        assert VolumeInfo(name="v", size_bytes=None).reclaim_bytes == 0
        assert VolumeInfo(name="v", size_bytes=500).reclaim_bytes == 500

    def test_container_reclaim(self) -> None:
        assert ContainerInfo(id="c", name="n", image="i", size_rw_bytes=123).reclaim_bytes == 123
        assert ContainerInfo(id="c", name="n", image="i").reclaim_bytes == 0


class TestDiagnosticReport:
    def _probe(self) -> DockerProbe:
        return DockerProbe(availability=DockerAvailability.OK, backend=DockerBackend.ENGINE)

    def test_json_roundtrip_keeps_bytes_as_int(self) -> None:
        report = DiagnosticReport(
            correlation_id="cid1",
            generated_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
            hostname="host",
            os="Linux",
            docker_probe=self._probe(),
            disks=[
                DiskUsage(
                    mountpoint="/",
                    total_bytes=1000,
                    used_bytes=800,
                    free_bytes=200,
                    percent_used=80.0,
                    is_docker_root=True,
                )
            ],
        )
        payload = report.model_dump_json()
        assert '"total_bytes":1000' in payload
        restored = DiagnosticReport.model_validate_json(payload)
        assert restored.disks[0].free_bytes == 200

    def test_docker_root_disk_selection(self) -> None:
        report = DiagnosticReport(
            correlation_id="c",
            generated_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
            hostname="h",
            os="o",
            docker_probe=self._probe(),
            disks=[
                DiskUsage(mountpoint="C:", total_bytes=1, used_bytes=0, free_bytes=1, percent_used=0),
                DiskUsage(
                    mountpoint="/var",
                    total_bytes=1,
                    used_bytes=0,
                    free_bytes=1,
                    percent_used=0,
                    is_docker_root=True,
                ),
            ],
        )
        assert report.docker_root_disk is not None
        assert report.docker_root_disk.mountpoint == "/var"

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(Exception):
            DockerProbe(availability=DockerAvailability.OK, bogus_field=1)  # type: ignore[call-arg]


class TestCleanupModels:
    def test_cleanup_result_reclaimed_bytes(self) -> None:
        plan = CleanupPlan(level=PruneLevel.SAFE)
        removed = AuditEvent(
            ts=datetime(2026, 7, 20, tzinfo=timezone.utc),
            run_id="r",
            level=PruneLevel.SAFE,
            dry_run=False,
            object_kind=ObjectKind.IMAGE,
            object_id="i1",
            object_name="img",
            reclaim_predicted_bytes=100,
            outcome="removed",
        )
        skipped = removed.model_copy(update={"object_id": "i2", "outcome": "skipped-protected"})
        result_obj = CleanupResult(
            correlation_id="c",
            level=PruneLevel.SAFE,
            dry_run=False,
            plan=plan,
            audit_events=[removed, skipped],
        )
        assert result_obj.reclaimed_bytes == 100

    def test_dry_run_reclaims_zero(self) -> None:
        plan = CleanupPlan(level=PruneLevel.SAFE)
        event = AuditEvent(
            ts=datetime(2026, 7, 20, tzinfo=timezone.utc),
            run_id="r",
            level=PruneLevel.SAFE,
            dry_run=True,
            object_kind=ObjectKind.IMAGE,
            object_id="i1",
            object_name="img",
            reclaim_predicted_bytes=100,
            outcome="dry-run",
        )
        result_obj = CleanupResult(
            correlation_id="c", level=PruneLevel.SAFE, dry_run=True, plan=plan, audit_events=[event]
        )
        assert result_obj.reclaimed_bytes == 0

    def test_space_delta_host_freed(self) -> None:
        delta = SpaceDelta(host_free_before_bytes=100, host_free_after_bytes=350)
        assert delta.host_freed_bytes == 250

    def test_removable_and_finding_construct(self) -> None:
        rem = Removable(kind=ObjectKind.VOLUME, id="v", name="vol", reclaim_bytes=10)
        assert rem.kind is ObjectKind.VOLUME
        finding = Finding(severity=HealthStatus.WARNING, code="X", message="m")
        assert finding.severity is HealthStatus.WARNING


def test_build_cache_info_defaults() -> None:
    bc = BuildCacheInfo(id="bc1", size_bytes=500, in_use=True)
    assert bc.usage_count == 0 and bc.in_use is True
