"""Unit / golden tests for the recovery playbooks and emergency mode."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from docker_disk_toolkit import cleaner, playbooks
from docker_disk_toolkit.models import (
    BuildCacheInfo,
    CategoryUsage,
    DockerBackend,
    DockerUsage,
    HealthStatus,
    ImageInfo,
    VolumeInfo,
)
from docker_disk_toolkit.playbooks import PlaybookContext
from docker_disk_toolkit.playbooks.ai_ml import AiMlBloatPlaybook
from docker_disk_toolkit.playbooks.buildkit import BuildCachePlaybook
from docker_disk_toolkit.playbooks.overlay2 import Overlay2BloatPlaybook
from docker_disk_toolkit.playbooks.vhdx import VhdxBloatPlaybook
from docker_disk_toolkit.playbooks.volume_creep import VolumeCreepPlaybook
from docker_disk_toolkit.system_info import VhdxFile

NOW = datetime(2026, 7, 21, tzinfo=UTC)
GB = 1000**3


def _ctx(usage: DockerUsage, **overrides) -> PlaybookContext:
    params = dict(
        usage=usage,
        disks=[],
        backend=DockerBackend.ENGINE,
        storage_driver="overlay2",
        docker_root_dir="/var/lib/docker",
        vhdx_files=[],
        host_is_windows=False,
        now=NOW,
    )
    params.update(overrides)
    return PlaybookContext(**params)  # type: ignore[arg-type]


class TestVhdx:
    def test_detects_and_renders(self) -> None:
        usage = DockerUsage(images=CategoryUsage(total_bytes=9 * GB))
        ctx = _ctx(
            usage,
            backend=DockerBackend.DESKTOP_WINDOWS,
            vhdx_files=[VhdxFile(path="C:/x/ext4.vhdx", size_bytes=60 * GB, label="data")],
        )
        finding = VhdxBloatPlaybook().detect(ctx)
        assert finding is not None and finding.severity is HealthStatus.CRITICAL
        script = VhdxBloatPlaybook().render_script(ctx)
        assert script.shell == "powershell" and script.requires_elevation
        assert "wsl --shutdown" in script.content
        assert "Optimize-VHD" in script.content
        assert "C:/x/ext4.vhdx" in script.content

    def test_no_detect_on_engine(self) -> None:
        assert VhdxBloatPlaybook().detect(_ctx(DockerUsage())) is None

    def test_no_detect_without_slack(self) -> None:
        usage = DockerUsage(images=CategoryUsage(total_bytes=58 * GB))
        ctx = _ctx(
            usage,
            backend=DockerBackend.WSL,
            vhdx_files=[VhdxFile(path="x", size_bytes=60 * GB, label="d")],
        )
        assert VhdxBloatPlaybook().detect(ctx) is None  # <10GB slack


class TestBuildKit:
    def test_detects_large_cache(self) -> None:
        usage = DockerUsage(
            build_cache=CategoryUsage(total_bytes=6 * GB, reclaimable_bytes=6 * GB),
            images=CategoryUsage(total_bytes=2 * GB),
        )
        finding = BuildCachePlaybook().detect(_ctx(usage))
        assert finding is not None
        script = BuildCachePlaybook().render_script(_ctx(usage))
        assert "builder prune" in script.content

    def test_no_detect_small_cache(self) -> None:
        usage = DockerUsage(
            build_cache=CategoryUsage(total_bytes=1 * GB, reclaimable_bytes=1 * GB),
            images=CategoryUsage(total_bytes=50 * GB),
        )
        assert BuildCachePlaybook().detect(_ctx(usage)) is None


class TestAiMl:
    def test_detects_ollama_and_excludes_running(self) -> None:
        usage = DockerUsage(
            image_list=[
                ImageInfo(
                    id="i1",
                    repo_tags=["ollama/ollama:latest"],
                    size_bytes=6 * GB,
                    unique_size_bytes=6 * GB,
                    in_use=False,
                ),
                ImageInfo(
                    id="i2",
                    repo_tags=["nvidia/cuda:12.4"],
                    size_bytes=8 * GB,
                    unique_size_bytes=8 * GB,
                    in_use=True,
                ),
            ]
        )
        finding = AiMlBloatPlaybook().detect(_ctx(usage))
        assert finding is not None
        # removable only counts the unused ollama image
        assert finding.est_reclaimable_bytes == 6 * GB
        script = AiMlBloatPlaybook().render_script(_ctx(usage))
        assert "ollama/ollama:latest" in script.content
        assert "ollama rm" in script.content
        # running cuda image not offered for removal
        assert "docker image rm nvidia/cuda:12.4" not in script.content

    def test_detects_large_base(self) -> None:
        usage = DockerUsage(
            image_list=[ImageInfo(id="i", repo_tags=["myapp:latest"], size_bytes=7 * GB)]
        )
        assert AiMlBloatPlaybook().detect(_ctx(usage)) is not None


class TestVolumeCreep:
    def test_detects_and_backups_only(self) -> None:
        usage = DockerUsage(volume_list=[VolumeInfo(name="postgres_data", size_bytes=3 * GB)])
        finding = VolumeCreepPlaybook().detect(_ctx(usage))
        assert finding is not None
        assert finding.est_reclaimable_bytes is None  # never proposes deletion
        script = VolumeCreepPlaybook().render_script(_ctx(usage))
        assert "tar czf" in script.content
        assert "docker volume rm" not in script.content  # backup-only


class TestOverlay2:
    def test_detects_on_linux_engine(self) -> None:
        usage = DockerUsage(
            images=CategoryUsage(total_bytes=30 * GB, reclaimable_bytes=12 * GB),
            containers=CategoryUsage(total_bytes=0),
            volumes=CategoryUsage(total_bytes=0),
            build_cache=CategoryUsage(total_bytes=0),
        )
        finding = Overlay2BloatPlaybook().detect(_ctx(usage))
        assert finding is not None

    def test_no_detect_on_desktop(self) -> None:
        usage = DockerUsage(images=CategoryUsage(total_bytes=30 * GB, reclaimable_bytes=12 * GB))
        assert (
            Overlay2BloatPlaybook().detect(_ctx(usage, backend=DockerBackend.DESKTOP_WINDOWS))
            is None
        )


class TestRegistry:
    def test_detect_all_and_render(self) -> None:
        usage = DockerUsage(
            build_cache=CategoryUsage(total_bytes=6 * GB, reclaimable_bytes=6 * GB),
            images=CategoryUsage(total_bytes=10 * GB),
            image_list=[ImageInfo(id="i", repo_tags=["ollama/ollama:latest"], size_bytes=6 * GB)],
        )
        findings = playbooks.detect_all(_ctx(usage))
        codes = {f.code for f in findings}
        assert "buildkit-explosion" in codes and "ai-ml-bloat" in codes
        scripts = playbooks.render_scripts(_ctx(usage))
        assert {s.playbook_id for s in scripts} >= {"buildkit-explosion", "ai-ml-bloat"}

    def test_render_scripts_only_filter(self) -> None:
        usage = DockerUsage(
            build_cache=CategoryUsage(total_bytes=6 * GB, reclaimable_bytes=6 * GB),
            images=CategoryUsage(total_bytes=10 * GB),
        )
        scripts = playbooks.render_scripts(_ctx(usage), only=["buildkit-explosion"])
        assert [s.playbook_id for s in scripts] == ["buildkit-explosion"]

    def test_get_playbook(self) -> None:
        assert playbooks.get_playbook("vhdx-bloat") is not None
        assert playbooks.get_playbook("nope") is None


class TestEmergency:
    def test_next_steps_includes_oneliner(self) -> None:
        usage = DockerUsage(
            volume_list=[VolumeInfo(name="scratch", size_bytes=5 * GB)],
            image_list=[ImageInfo(id="i", repo_tags=["big:latest"], size_bytes=7 * GB)],
        )
        steps = cleaner.emergency_next_steps(usage)
        assert any("system prune -af --volumes" in s for s in steps)
        assert any("scratch" in s for s in steps)

    def test_run_emergency_safe_tier(
        self, monkeypatch: pytest.MonkeyPatch, make_config, cli_client
    ):
        from docker_disk_toolkit.context import RunContext
        from docker_disk_toolkit.docker_client import FakeDockerClient
        from docker_disk_toolkit.models import ContainerInfo, DiskUsage, NetworkInfo

        monkeypatch.setattr(
            cleaner.system_info,
            "collect_disks",
            lambda **_: [
                DiskUsage(
                    mountpoint="/",
                    total_bytes=100 * GB,
                    used_bytes=50 * GB,
                    free_bytes=50 * GB,
                    percent_used=50.0,
                    is_docker_root=True,
                )
            ],
        )
        usage = DockerUsage(
            image_list=[ImageInfo(id="d", repo_tags=[], dangling=True, size_bytes=GB)],
            container_list=[ContainerInfo(id="c", name="old", image="x", running=False)],
            build_cache=CategoryUsage(total_bytes=GB, reclaimable_bytes=GB),
            build_cache_list=[BuildCacheInfo(id="bc", size_bytes=GB, in_use=False)],
            network_list=[NetworkInfo(id="n", name="app_net", builtin=False)],
        )
        client = FakeDockerClient(usage)
        ctx = RunContext.create(make_config(), docker=client, now=NOW)
        result = cleaner.run_emergency(ctx, dry_run=False)
        assert result.dry_run is False
        # dangling image + stopped container removed, no volumes touched
        assert "d" in {rid for _, rid in client.removed}
