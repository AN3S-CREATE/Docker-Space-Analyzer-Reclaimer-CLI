"""Unit tests for :mod:`docker_disk_toolkit.analyzer`."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from docker_disk_toolkit import analyzer, system_info
from docker_disk_toolkit.config import ToolkitConfig
from docker_disk_toolkit.context import RunContext
from docker_disk_toolkit.docker_client import CliDockerClient
from docker_disk_toolkit.models import (
    BuildCacheInfo,
    DiskUsage,
    DockerUsage,
    HealthStatus,
    ImageInfo,
)
from docker_disk_toolkit.system_info import VhdxFile
from docker_disk_toolkit.utils import compile_protect_matchers

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


def _disk(mount: str, total: int, free: int, *, docker_root: bool = False) -> DiskUsage:
    used = total - free
    return DiskUsage(
        mountpoint=mount,
        total_bytes=total,
        used_bytes=used,
        free_bytes=free,
        percent_used=used / total * 100,
        is_docker_root=docker_root,
    )


@pytest.fixture
def healthy_disks(monkeypatch: pytest.MonkeyPatch) -> None:
    disks = [_disk("/var/lib/docker", 500 * analyzer.GB, 300 * analyzer.GB, docker_root=True)]
    monkeypatch.setattr(system_info, "collect_disks", lambda **_: list(disks))
    monkeypatch.setattr(analyzer.system_info, "collect_disks", lambda **_: list(disks))
    monkeypatch.setattr(analyzer.system_info, "find_vhdx_files", lambda *a, **k: [])


def _ctx(client: CliDockerClient, config: ToolkitConfig) -> RunContext:
    return RunContext.create(config, docker=client, now=NOW, correlation_id="testcid")


class TestCorrelation:
    def test_marks_in_use_and_protected(self) -> None:
        usage = DockerUsage(
            image_list=[
                ImageInfo(id="sha256:img1", repo_tags=["nginx:latest"]),
                ImageInfo(id="sha256:img2", repo_tags=["ollama/ollama:latest"]),
            ],
        )
        from docker_disk_toolkit.models import ContainerInfo, VolumeInfo

        usage.container_list = [
            ContainerInfo(
                id="c1", name="web", image="nginx:latest", running=True, mounts=["webdata"]
            )
        ]
        usage.volume_list = [
            VolumeInfo(name="webdata"),
            VolumeInfo(name="postgres_data"),
            VolumeInfo(name="scratch"),
        ]
        matcher = compile_protect_matchers(regexes=["postgres"])
        analyzer.correlate_usage(usage, matcher)

        by_tag = {i.repo_tags[0]: i for i in usage.image_list}
        assert by_tag["nginx:latest"].in_use is True
        assert by_tag["ollama/ollama:latest"].in_use is False
        vols = {v.name: v for v in usage.volume_list}
        assert vols["webdata"].in_use is True
        assert vols["postgres_data"].protected is True
        assert vols["scratch"].in_use is False and vols["scratch"].protected is False


class TestDiskHealth:
    def test_thresholds(self) -> None:
        t = ToolkitConfig().thresholds
        assert analyzer.assess_disk_health(_disk("/", 100 * analyzer.GB, 2 * analyzer.GB), t) is (
            HealthStatus.CRITICAL
        )
        assert analyzer.assess_disk_health(_disk("/", 100 * analyzer.GB, 8 * analyzer.GB), t) is (
            HealthStatus.WARNING
        )
        assert analyzer.assess_disk_health(_disk("/", 100 * analyzer.GB, 50 * analyzer.GB), t) is (
            HealthStatus.HEALTHY
        )

    def test_worst(self) -> None:
        assert analyzer.worst(HealthStatus.HEALTHY, HealthStatus.CRITICAL) is HealthStatus.CRITICAL
        assert analyzer.worst(HealthStatus.HEALTHY, HealthStatus.WARNING) is HealthStatus.WARNING


class TestAnalyzeTypical:
    def test_full_report(
        self,
        healthy_disks: None,
        cli_client: Callable[[str], CliDockerClient],
        make_config: Callable[..., ToolkitConfig],
    ) -> None:
        config = make_config()
        report = analyzer.analyze(_ctx(cli_client("typical"), config))

        assert report.docker is not None
        assert report.reclaimable_total_bytes > 0
        # correlation
        images = {i.display_name: i for i in report.docker.image_list}
        assert images["nginx:latest"].in_use is True
        # ai-ml playbook fires on the 6GB ollama image -> WARNING overall
        codes = {f.code for f in report.findings}
        assert "ai-ml-bloat" in codes
        assert report.health is HealthStatus.WARNING
        # recommendations include dangling + build cache + unused volume
        actions = {r.action for r in report.recommendations}
        assert any("dangling" in a.lower() for a in actions)
        assert any("build cache" in a.lower() for a in actions)
        unused_vol_rec = next(r for r in report.recommendations if "volume" in r.action.lower())
        assert unused_vol_rec.protected_excluded == 1  # postgres_data protected

    def test_json_serialisable(
        self,
        healthy_disks: None,
        cli_client: Callable[[str], CliDockerClient],
        make_config: Callable[..., ToolkitConfig],
    ) -> None:
        report = analyzer.analyze(_ctx(cli_client("typical"), make_config()))
        payload = report.model_dump_json()
        assert '"correlation_id":"testcid"' in payload


class TestAnalyzeDockerUnavailable:
    def test_graceful_degradation(
        self,
        healthy_disks: None,
        cli_client: Callable[[str], CliDockerClient],
        make_config: Callable[..., ToolkitConfig],
    ) -> None:
        report = analyzer.analyze(_ctx(cli_client("not-installed"), make_config()))
        assert report.docker is None
        codes = {f.code for f in report.findings}
        assert "DOCKER_UNAVAILABLE" in codes
        # host disk is healthy, so docker-absence alone does not fail the run
        assert report.health is HealthStatus.HEALTHY

    def test_low_disk_still_assessed_without_docker(
        self,
        monkeypatch: pytest.MonkeyPatch,
        cli_client: Callable[[str], CliDockerClient],
        make_config: Callable[..., ToolkitConfig],
    ) -> None:
        low = [_disk("/", 100 * analyzer.GB, 2 * analyzer.GB)]
        monkeypatch.setattr(analyzer.system_info, "collect_disks", lambda **_: list(low))
        report = analyzer.analyze(_ctx(cli_client("daemon-down"), make_config()))
        assert report.health is HealthStatus.CRITICAL


class TestVhdxCaveat:
    def test_desktop_vhdx_flags_caveat_and_playbook(
        self,
        monkeypatch: pytest.MonkeyPatch,
        cli_client: Callable[[str], CliDockerClient],
        make_config: Callable[..., ToolkitConfig],
    ) -> None:
        disks = [_disk("C:\\", 500 * analyzer.GB, 300 * analyzer.GB, docker_root=False)]
        monkeypatch.setattr(analyzer.system_info, "collect_disks", lambda **_: list(disks))
        big_vhdx = [VhdxFile(path="C:/vhdx/ext4.vhdx", size_bytes=60 * analyzer.GB, label="data")]
        monkeypatch.setattr(analyzer.system_info, "find_vhdx_files", lambda *a, **k: big_vhdx)

        report = analyzer.analyze(_ctx(cli_client("desktop-windows"), make_config()))
        codes = {f.code for f in report.findings}
        assert "DESKTOP_VHDX_CAVEAT" in codes
        assert "vhdx-bloat" in codes
        # 60GB vhdx vs ~9GB used -> >40GB slack -> critical
        assert report.health is HealthStatus.CRITICAL
        assert report.disks[0].backend_caveat is not None


class TestForensic:
    def test_suspects_ranked(self) -> None:
        usage = DockerUsage(
            image_list=[
                ImageInfo(
                    id="i1",
                    repo_tags=["big:latest"],
                    size_bytes=5_000_000_000,
                    created_at=NOW - timedelta(hours=2),
                ),
                ImageInfo(
                    id="i2",
                    repo_tags=["old:latest"],
                    size_bytes=9_000_000_000,
                    created_at=NOW - timedelta(days=30),
                ),
            ],
            build_cache_list=[
                BuildCacheInfo(
                    id="bc1", size_bytes=3_000_000_000, last_used_at=NOW - timedelta(hours=1)
                )
            ],
        )
        suspects = analyzer.forensic_suspects(usage, timedelta(hours=24), NOW)
        # old image (30d) excluded; recent image + cache included, ranked by size
        assert [s["name"] for s in suspects] == ["big:latest", "bc1"]
        finding = analyzer.forensic_finding(suspects, timedelta(hours=24))
        assert finding is not None and finding.code == "FORENSIC_RECENT_GROWTH"

    def test_no_suspects(self) -> None:
        assert analyzer.forensic_finding([], timedelta(hours=1)) is None

    def test_analyze_with_forensic(
        self,
        healthy_disks: None,
        cli_client: Callable[[str], CliDockerClient],
        make_config: Callable[..., ToolkitConfig],
    ) -> None:
        report = analyzer.analyze(
            _ctx(cli_client("typical"), make_config()), forensic_since=timedelta(days=120)
        )
        assert any(f.code == "FORENSIC_RECENT_GROWTH" for f in report.findings)
