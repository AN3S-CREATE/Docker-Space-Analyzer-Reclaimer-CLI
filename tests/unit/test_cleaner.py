"""Safety-focused unit tests for :mod:`docker_disk_toolkit.cleaner`."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from docker_disk_toolkit import analyzer, cleaner
from docker_disk_toolkit.cleaner import SelectionCriteria, build_plan, run_cleanup
from docker_disk_toolkit.config import ToolkitConfig
from docker_disk_toolkit.context import RunContext
from docker_disk_toolkit.docker_client import FakeDockerClient
from docker_disk_toolkit.errors import DockerUnavailableError
from docker_disk_toolkit.models import (
    BuildCacheInfo,
    CategoryUsage,
    ContainerInfo,
    DiskUsage,
    DockerAvailability,
    DockerProbe,
    DockerUsage,
    ImageInfo,
    NetworkInfo,
    ObjectKind,
    PruneLevel,
    VolumeInfo,
)

RUN_NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


def build_usage() -> DockerUsage:
    """A representative snapshot: dangling + tagged-unused images, stopped
    container, protected + unprotected volumes, build cache, unused network."""
    return DockerUsage(
        images=CategoryUsage(total_bytes=6_580_000_000, reclaimable_bytes=6_400_000_000),
        containers=CategoryUsage(total_bytes=12_000_000, reclaimable_bytes=10_000_000),
        volumes=CategoryUsage(total_bytes=1_500_000_000, reclaimable_bytes=1_450_000_000),
        build_cache=CategoryUsage(total_bytes=800_000_000, reclaimable_bytes=800_000_000),
        image_list=[
            ImageInfo(
                id="sha256:nginx",
                repo_tags=["nginx:latest"],
                size_bytes=180_000_000,
                unique_size_bytes=175_000_000,
            ),
            ImageInfo(
                id="sha256:bbb",
                repo_tags=[],
                dangling=True,
                size_bytes=400_000_000,
                unique_size_bytes=400_000_000,
            ),
            ImageInfo(
                id="sha256:ollama",
                repo_tags=["ollama/ollama:latest"],
                size_bytes=6_000_000_000,
                unique_size_bytes=6_000_000_000,
            ),
        ],
        container_list=[
            ContainerInfo(
                id="c1",
                name="web",
                image="nginx:latest",
                running=True,
                mounts=["webdata"],
                size_rw_bytes=2_000_000,
            ),
            ContainerInfo(
                id="c2", name="old_job", image="busybox", running=False, size_rw_bytes=10_000_000
            ),
        ],
        volume_list=[
            VolumeInfo(name="webdata", size_bytes=50_000_000),
            VolumeInfo(name="postgres_data", size_bytes=1_200_000_000),
            VolumeInfo(name="scratch_tmp", size_bytes=250_000_000),
        ],
        build_cache_list=[BuildCacheInfo(id="bc1", size_bytes=800_000_000, in_use=False)],
        network_list=[
            NetworkInfo(id="n1", name="bridge", builtin=True),
            NetworkInfo(id="n3", name="myapp_default", builtin=False),
        ],
    )


def correlated_usage(config: ToolkitConfig | None = None) -> DockerUsage:
    usage = build_usage()
    analyzer.correlate_usage(usage, (config or ToolkitConfig()).protect_matcher())
    return usage


@pytest.fixture
def fake_ctx(monkeypatch: pytest.MonkeyPatch, make_config: Callable[..., ToolkitConfig]):
    """Build a RunContext backed by a FakeDockerClient + patched disks."""
    disks = [
        DiskUsage(
            mountpoint="/var/lib/docker",
            total_bytes=500_000_000_000,
            used_bytes=200_000_000_000,
            free_bytes=300_000_000_000,
            percent_used=40.0,
            is_docker_root=True,
        )
    ]
    monkeypatch.setattr(cleaner.system_info, "collect_disks", lambda **_: list(disks))

    def _factory(**config_overrides: object) -> tuple[RunContext, FakeDockerClient]:
        config = make_config(**config_overrides)
        usage = build_usage()
        client = FakeDockerClient(usage)
        ctx = RunContext.create(config, docker=client, now=RUN_NOW, correlation_id="cleanupcid")
        return ctx, client

    return _factory


# ---------------------------------------------------------------------------
# build_plan level semantics
# ---------------------------------------------------------------------------


class TestBuildPlanLevels:
    def _plan(self, level: PruneLevel, config: ToolkitConfig | None = None):
        config = config or ToolkitConfig()
        return build_plan(
            correlated_usage(config), level, SelectionCriteria.from_config(config), now=RUN_NOW
        )

    def test_level0_empty(self) -> None:
        plan = self._plan(PruneLevel.REPORT)
        assert plan.items == [] and plan.requires_confirmation is False

    def test_level1_safe(self) -> None:
        plan = self._plan(PruneLevel.SAFE)
        names = {i.name for i in plan.items}
        assert any("bbb" in n for n in names)  # dangling image (<none>@bbb)
        assert "old_job" in names  # stopped container
        assert "build cache" in names
        assert any("network" in n for n in names)
        # L1 does NOT touch volumes or tagged-unused images
        assert not any(i.kind is ObjectKind.VOLUME for i in plan.items)
        assert "ollama/ollama:latest" not in names

    def test_level2_adds_unused_volumes_but_protects(self) -> None:
        plan = self._plan(PruneLevel.AGGRESSIVE)
        vol_items = {i.name for i in plan.items if i.kind is ObjectKind.VOLUME}
        assert "scratch_tmp" in vol_items
        assert "webdata" not in vol_items  # in use
        # postgres_data is protected -> spared, never in items
        assert "postgres_data" not in vol_items
        assert any(p.name == "postgres_data" for p in plan.protected)

    def test_level3_adds_unused_tagged_images(self) -> None:
        plan = self._plan(PruneLevel.NUCLEAR)
        names = {i.name for i in plan.items}
        assert "ollama/ollama:latest" in names  # tagged but unreferenced
        assert plan.confirmation_mode == "type-to-confirm"

    def test_reclaim_totals(self) -> None:
        plan = self._plan(PruneLevel.AGGRESSIVE)
        # dangling(400M) + stopped(10M) + build cache(800M) + scratch(250M)
        assert plan.total_reclaim_bytes == 400_000_000 + 10_000_000 + 800_000_000 + 250_000_000
        assert plan.docker_reported_reclaim_bytes == correlated_usage().reclaimable_bytes


# ---------------------------------------------------------------------------
# Selective filters / protect / exclude
# ---------------------------------------------------------------------------


class TestSelectors:
    def test_min_size_filter(self) -> None:
        crit = SelectionCriteria(min_size_bytes=500_000_000)
        plan = build_plan(correlated_usage(), PruneLevel.AGGRESSIVE, crit, now=RUN_NOW)
        # only build cache (800M) survives; dangling 400M / scratch 250M filtered out
        sizes = [i.reclaim_bytes for i in plan.items]
        assert all(s == 0 or s >= 500_000_000 for s in sizes if s)

    def test_name_glob_filter(self) -> None:
        crit = SelectionCriteria(name_globs=["scratch*"])
        plan = build_plan(correlated_usage(), PruneLevel.AGGRESSIVE, crit, now=RUN_NOW)
        vol_items = {i.name for i in plan.items if i.kind is ObjectKind.VOLUME}
        assert vol_items == {"scratch_tmp"}

    def test_exclude_images(self) -> None:
        crit = SelectionCriteria(
            exclude_images_matcher=cleaner.compile_protect_matchers(globs=["ollama/ollama:latest"])
        )
        plan = build_plan(correlated_usage(), PruneLevel.NUCLEAR, crit, now=RUN_NOW)
        names = {i.name for i in plan.items}
        assert "ollama/ollama:latest" not in names
        assert any(p.name == "ollama/ollama:latest" for p in plan.protected)

    def test_extra_protect_volume(self) -> None:
        config = ToolkitConfig(protect_volumes=["scratch_tmp"])
        crit = SelectionCriteria.from_config(config)
        usage = correlated_usage(config)
        plan = build_plan(usage, PruneLevel.AGGRESSIVE, crit, now=RUN_NOW)
        assert "scratch_tmp" not in {i.name for i in plan.items if i.kind is ObjectKind.VOLUME}


# ---------------------------------------------------------------------------
# run_cleanup — the safety flow
# ---------------------------------------------------------------------------


class TestRunCleanupSafety:
    def test_dry_run_removes_nothing(self, fake_ctx) -> None:
        ctx, client = fake_ctx(dry_run=True)
        result = run_cleanup(ctx, level=PruneLevel.AGGRESSIVE)
        assert result.dry_run is True
        assert client.removed == []  # nothing mutated
        assert result.reclaimed_bytes == 0
        assert all(e.outcome in {"dry-run", "skipped-protected"} for e in result.audit_events)

    def test_real_cleanup_executes_with_yes(self, fake_ctx) -> None:
        ctx, client = fake_ctx(dry_run=False, assume_yes=True)
        result = run_cleanup(ctx, level=PruneLevel.SAFE)
        assert result.dry_run is False
        assert result.reclaimed_bytes > 0
        # dangling image, stopped container removed; nginx (in use) untouched
        removed_ids = {rid for _, rid in client.removed}
        assert "sha256:bbb" in removed_ids
        assert "c2" in removed_ids
        assert "sha256:nginx" not in removed_ids

    def test_confirmation_refused_is_safe(self, fake_ctx) -> None:
        ctx, client = fake_ctx(dry_run=False, assume_yes=False)
        result = run_cleanup(ctx, level=PruneLevel.SAFE, confirm_fn=lambda plan, mode: False)
        assert result.dry_run is True  # coerced to simulate
        assert client.removed == []

    def test_confirmation_granted_executes(self, fake_ctx) -> None:
        ctx, client = fake_ctx(dry_run=False, assume_yes=False)
        result = run_cleanup(ctx, level=PruneLevel.SAFE, confirm_fn=lambda plan, mode: True)
        assert result.dry_run is False and client.removed

    def test_nuclear_needs_force_even_with_yes(self, fake_ctx) -> None:
        # assume_yes alone must NOT nuke at level 3
        ctx, client = fake_ctx(dry_run=False, assume_yes=True)
        result = run_cleanup(ctx, level=PruneLevel.NUCLEAR, force=False)
        assert result.dry_run is True and client.removed == []

    def test_nuclear_with_force_executes(self, fake_ctx) -> None:
        ctx, client = fake_ctx(dry_run=False, assume_yes=True)
        result = run_cleanup(ctx, level=PruneLevel.NUCLEAR, force=True)
        assert result.dry_run is False
        # tagged-unused ollama image removed at level 3
        assert "ollama/ollama:latest" in {rid for _, rid in client.removed}

    def test_protected_volume_never_removed(self, fake_ctx) -> None:
        ctx, client = fake_ctx(dry_run=False, assume_yes=True)
        run_cleanup(ctx, level=PruneLevel.AGGRESSIVE)
        assert "postgres_data" not in {rid for _, rid in client.removed}

    def test_per_item_failure_continues(self, fake_ctx, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx, client = fake_ctx(dry_run=False, assume_yes=True)
        client.fail_on = {"c2"}  # stopped container removal fails
        result = run_cleanup(ctx, level=PruneLevel.SAFE)
        assert result.errors  # recorded
        # other objects still removed despite the failure
        assert "sha256:bbb" in {rid for _, rid in client.removed}

    def test_stop_on_error_halts(self, fake_ctx) -> None:
        ctx, client = fake_ctx(dry_run=False, assume_yes=True)
        client.fail_on = {"sha256:bbb"}  # first item fails
        result = run_cleanup(ctx, level=PruneLevel.SAFE, stop_on_error=True)
        assert result.errors
        # halted before removing the stopped container
        assert "c2" not in {rid for _, rid in client.removed}

    def test_docker_unavailable_raises(self, make_config: Callable[..., ToolkitConfig]) -> None:
        client = FakeDockerClient(
            build_usage(), probe=DockerProbe(availability=DockerAvailability.NOT_INSTALLED)
        )
        ctx = RunContext.create(make_config(), docker=client, now=RUN_NOW)
        with pytest.raises(DockerUnavailableError):
            run_cleanup(ctx, level=PruneLevel.SAFE, dry_run=False, assume_yes=True)

    def test_delta_and_health(self, fake_ctx) -> None:
        ctx, client = fake_ctx(dry_run=False, assume_yes=True)
        result = run_cleanup(ctx, level=PruneLevel.SAFE)
        assert result.delta is not None
        assert result.delta.reclaimed_bytes == result.reclaimed_bytes
        assert result.pre_health.value in {"healthy", "warning", "critical"}
