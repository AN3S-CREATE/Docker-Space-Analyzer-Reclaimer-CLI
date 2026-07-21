"""Shared pytest fixtures.

Key design point: every Docker interaction is injected via a
:class:`FixtureCommandRunner`, so no test ever touches a live daemon. An autouse
``_safe_mode`` fixture also strips ambient ``DOCKER_DISK_*`` env vars for
determinism.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from docker_disk_toolkit.config import ToolkitConfig
from docker_disk_toolkit.context import RunContext
from docker_disk_toolkit.docker_client import CliDockerClient
from tests.docker_fixtures import make_runner

FROZEN_NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _safe_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip DOCKER_DISK_* env vars so config tests are deterministic."""
    for key in list(os.environ):
        if key.startswith("DOCKER_DISK_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def frozen_now() -> datetime:
    """A fixed 'now' for deterministic time-based assertions."""
    return FROZEN_NOW


@pytest.fixture
def tmp_report_dir(tmp_path: Path) -> Path:
    """A writable, isolated report directory."""
    d = tmp_path / "docker-disk-reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def make_config(tmp_report_dir: Path) -> Callable[..., ToolkitConfig]:
    """Factory building a ToolkitConfig rooted at the temp report dir."""

    def _factory(**overrides: object) -> ToolkitConfig:
        params: dict[str, object] = {"report_dir": tmp_report_dir}
        params.update(overrides)
        return ToolkitConfig(**params)  # type: ignore[arg-type]

    return _factory


@pytest.fixture
def cli_client(make_config: Callable[..., ToolkitConfig]) -> Callable[[str], CliDockerClient]:
    """Factory building a CliDockerClient wired to a named fixture scenario."""

    def _factory(scenario: str = "typical") -> CliDockerClient:
        return CliDockerClient(make_runner(scenario), make_config(), cli_path="docker")

    return _factory


@pytest.fixture
def patched_disks(monkeypatch: pytest.MonkeyPatch):
    """Patch analyzer's disk/VHDX collection; returns a setter for custom disks."""
    from docker_disk_toolkit import analyzer
    from docker_disk_toolkit.models import DiskUsage

    default = [
        DiskUsage(
            mountpoint="/var/lib/docker",
            total_bytes=500 * 1000**3,
            used_bytes=200 * 1000**3,
            free_bytes=300 * 1000**3,
            percent_used=40.0,
            is_docker_root=True,
        )
    ]

    def _set(disks: list | None = None, vhdx: list | None = None) -> None:
        monkeypatch.setattr(
            analyzer.system_info, "collect_disks", lambda **_: list(disks or default)
        )
        monkeypatch.setattr(
            analyzer.system_info, "find_vhdx_files", lambda *a, **k: list(vhdx or [])
        )

    _set()
    return _set


@pytest.fixture
def make_report(
    patched_disks,
    make_config: Callable[..., ToolkitConfig],
    cli_client: Callable[[str], CliDockerClient],
):
    """Factory building a DiagnosticReport from a named scenario."""
    from docker_disk_toolkit import analyzer

    def _factory(scenario: str = "typical", **config_overrides: object):
        config = make_config(**config_overrides)
        ctx = RunContext.create(
            config, docker=cli_client(scenario), now=FROZEN_NOW, correlation_id="rid123"
        )
        return analyzer.analyze(ctx)

    return _factory
