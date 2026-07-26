"""Integration tests for the Typer CLI via CliRunner.

Docker access is injected by patching ``cli._build_run_context`` to return a
context wired to a fixture/fake client; disk metrics are patched too, so these
run without a daemon and deterministically.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from docker_disk_toolkit import cli, system_info
from docker_disk_toolkit.context import RunContext
from docker_disk_toolkit.docker_client import CliDockerClient, FakeDockerClient, NullDockerClient
from docker_disk_toolkit.errors import ConfigError, ExitCode
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
    VolumeInfo,
)
from tests.docker_fixtures import make_runner

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
GB = 1000**3
runner = CliRunner()


def _fake_usage() -> DockerUsage:
    return DockerUsage(
        images=CategoryUsage(total_bytes=6 * GB, reclaimable_bytes=6 * GB),
        build_cache=CategoryUsage(total_bytes=GB, reclaimable_bytes=GB),
        image_list=[
            ImageInfo(id="sha256:nginx", repo_tags=["nginx:latest"], size_bytes=180_000_000),
            ImageInfo(
                id="sha256:bbb",
                repo_tags=[],
                dangling=True,
                size_bytes=400_000_000,
                unique_size_bytes=400_000_000,
            ),
        ],
        container_list=[
            ContainerInfo(
                id="c1", name="web", image="nginx:latest", running=True, mounts=["webdata"]
            ),
            ContainerInfo(
                id="c2", name="old", image="busybox", running=False, size_rw_bytes=10_000_000
            ),
        ],
        volume_list=[
            VolumeInfo(name="webdata", size_bytes=50_000_000),
            VolumeInfo(name="postgres_data", size_bytes=GB),
        ],
        build_cache_list=[BuildCacheInfo(id="bc", size_bytes=GB, in_use=False)],
        network_list=[NetworkInfo(id="n", name="app_net", builtin=False)],
    )


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Patch disk collection + the run-context builder; return a client setter."""
    disks = [
        DiskUsage(
            mountpoint="/var/lib/docker",
            total_bytes=500 * GB,
            used_bytes=200 * GB,
            free_bytes=300 * GB,
            percent_used=40.0,
            is_docker_root=True,
        )
    ]
    monkeypatch.setattr(system_info, "collect_disks", lambda **_: list(disks))
    monkeypatch.setattr(system_info, "find_vhdx_files", lambda *a, **k: [])

    state: dict = {"client": None, "disks": disks}

    def _set(client, custom_disks=None) -> None:
        state["client"] = client
        if custom_disks is not None:
            monkeypatch.setattr(system_info, "collect_disks", lambda **_: list(custom_disks))

        def fake_ctx(config, *, correlation_id=None) -> RunContext:
            return RunContext.create(
                config, docker=client, now=NOW, correlation_id=correlation_id or "clicid"
            )

        monkeypatch.setattr(cli, "_build_run_context", fake_ctx)

    return _set


def _report_dir(tmp_path: Path) -> list[str]:
    return ["--report-dir", str(tmp_path / "reports")]


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------


class TestAnalyze:
    def test_json_output(self, cli_env, tmp_path: Path) -> None:
        cli_env(CliDockerClient(make_runner("typical"), _cfg(tmp_path)))
        result = runner.invoke(cli.app, [*_report_dir(tmp_path), "--json", "analyze", "--no-write"])
        assert result.exit_code == 1  # ai-ml warning present
        data = json.loads(result.stdout)
        assert data["docker"]["images"]["total_bytes"] > 0

    def test_default_command_is_analyze(self, cli_env, tmp_path: Path) -> None:
        cli_env(NullDockerClient(DockerProbe(availability=DockerAvailability.NOT_INSTALLED)))
        result = runner.invoke(cli.app, _report_dir(tmp_path))
        assert result.exit_code == 0  # healthy host, docker absent
        assert "HEALTHY" in result.stdout

    def test_writes_report_file(self, cli_env, tmp_path: Path) -> None:
        cli_env(NullDockerClient(DockerProbe(availability=DockerAvailability.NOT_INSTALLED)))
        result = runner.invoke(cli.app, [*_report_dir(tmp_path), "analyze"])
        assert result.exit_code == 0
        assert list((tmp_path / "reports").glob("*.md"))

    def test_csv_output(self, cli_env, tmp_path: Path) -> None:
        cli_env(CliDockerClient(make_runner("typical"), _cfg(tmp_path)))
        result = runner.invoke(cli.app, [*_report_dir(tmp_path), "analyze", "--csv", "--no-write"])
        assert "record_type,name,size_bytes" in result.stdout


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------


class TestCleanup:
    def test_report_level_default(self, cli_env, tmp_path: Path) -> None:
        client = FakeDockerClient(_fake_usage())
        cli_env(client)
        result = runner.invoke(cli.app, [*_report_dir(tmp_path), "cleanup"])  # level 0
        assert result.exit_code == 0
        assert client.removed == []

    def test_level1_with_yes_executes(self, cli_env, tmp_path: Path) -> None:
        client = FakeDockerClient(_fake_usage())
        cli_env(client)
        result = runner.invoke(
            cli.app, [*_report_dir(tmp_path), "--yes", "--no-dry-run", "cleanup", "--level", "1"]
        )
        assert result.exit_code in (0, 1)
        removed = {rid for _, rid in client.removed}
        assert "sha256:bbb" in removed and "c2" in removed
        # audit + history persisted
        assert (tmp_path / "reports" / "audit.jsonl").exists()

    def test_noninteractive_without_yes_refuses(self, cli_env, tmp_path: Path) -> None:
        client = FakeDockerClient(_fake_usage())
        cli_env(client)
        # non-tty (CliRunner) + no --yes -> must refuse and remove nothing
        result = runner.invoke(
            cli.app, [*_report_dir(tmp_path), "--no-dry-run", "cleanup", "--level", "1"]
        )
        assert result.exit_code == 0
        assert client.removed == []

    def test_docker_unavailable_is_fatal(self, cli_env, tmp_path: Path) -> None:
        cli_env(NullDockerClient(DockerProbe(availability=DockerAvailability.NOT_INSTALLED)))
        result = runner.invoke(
            cli.app, [*_report_dir(tmp_path), "--yes", "--no-dry-run", "cleanup", "--level", "1"]
        )
        assert result.exit_code == 3

    def test_protect_flag_spares_volume(self, cli_env, tmp_path: Path) -> None:
        client = FakeDockerClient(_fake_usage())
        cli_env(client)
        result = runner.invoke(
            cli.app,
            [
                *_report_dir(tmp_path),
                "--yes",
                "--no-dry-run",
                "--protect",
                "webdata",
                "cleanup",
                "--level",
                "2",
            ],
        )
        assert result.exit_code in (0, 1)
        assert "postgres_data" not in {rid for _, rid in client.removed}  # default-protected


# ---------------------------------------------------------------------------
# monitor / install-cron / report / emergency
# ---------------------------------------------------------------------------


class TestOtherCommands:
    def test_monitor_once(self, cli_env, tmp_path: Path) -> None:
        cli_env(NullDockerClient(DockerProbe(availability=DockerAvailability.NOT_INSTALLED)))
        result = runner.invoke(cli.app, [*_report_dir(tmp_path), "monitor", "--once"])
        assert result.exit_code == 0
        assert (tmp_path / "reports" / "metrics.json").exists()

    def test_install_cron_writes_files(self, cli_env, tmp_path: Path) -> None:
        cli_env(NullDockerClient(DockerProbe(availability=DockerAvailability.NOT_INSTALLED)))
        out = tmp_path / "artifacts"
        result = runner.invoke(
            cli.app,
            [*_report_dir(tmp_path), "install-cron", "--emit", "all", "--output-dir", str(out)],
        )
        assert result.exit_code == 0
        assert (out / "DockerDiskToolkit-cleanup.xml").exists()
        assert list(out.glob("*.service"))

    def test_report_trend_and_rebuild(self, cli_env, tmp_path: Path) -> None:
        cli_env(NullDockerClient(DockerProbe(availability=DockerAvailability.NOT_INSTALLED)))
        # rebuild on empty history is a no-op that still succeeds
        r1 = runner.invoke(cli.app, [*_report_dir(tmp_path), "report", "--rebuild-index"])
        assert r1.exit_code == 0
        r2 = runner.invoke(cli.app, [*_report_dir(tmp_path), "--json", "report", "--last", "30d"])
        assert r2.exit_code == 0
        assert json.loads(r2.stdout)["window_days"] == 30

    def test_emergency_dry_run_preview(self, cli_env, tmp_path: Path) -> None:
        client = FakeDockerClient(_fake_usage())
        cli_env(client)
        result = runner.invoke(cli.app, [*_report_dir(tmp_path), "emergency"])
        assert result.exit_code in (0, 1)
        # default dry-run: nothing removed
        assert client.removed == []
        assert "Dry run" in result.stdout

    def test_emergency_execute(self, cli_env, tmp_path: Path) -> None:
        client = FakeDockerClient(_fake_usage())
        cli_env(client)
        result = runner.invoke(cli.app, [*_report_dir(tmp_path), "--no-dry-run", "emergency"])
        assert result.exit_code in (0, 1)
        assert "sha256:bbb" in {rid for _, rid in client.removed}
        assert "next steps" in result.stdout.lower()

    def test_monitor_json(self, cli_env, tmp_path: Path) -> None:
        cli_env(CliDockerClient(make_runner("typical"), _cfg(tmp_path)))
        result = runner.invoke(cli.app, [*_report_dir(tmp_path), "--json", "monitor", "--once"])
        assert result.exit_code in (0, 1)
        assert "metrics" in json.loads(result.stdout)

    def test_install_cron_print(self, cli_env, tmp_path: Path) -> None:
        cli_env(NullDockerClient(DockerProbe(availability=DockerAvailability.NOT_INSTALLED)))
        result = runner.invoke(cli.app, [*_report_dir(tmp_path), "install-cron", "--emit", "cron"])
        assert result.exit_code == 0
        assert "docker-disk" in result.stdout

    def test_install_cron_bad_schedule(self, cli_env, tmp_path: Path) -> None:
        cli_env(NullDockerClient(DockerProbe(availability=DockerAvailability.NOT_INSTALLED)))
        result = runner.invoke(
            cli.app, [*_report_dir(tmp_path), "install-cron", "--schedule", "fortnightly"]
        )
        assert result.exit_code == 3

    def test_report_list_and_playbooks(self, cli_env, tmp_path: Path) -> None:
        # write a report first, then list it
        cli_env(NullDockerClient(DockerProbe(availability=DockerAvailability.NOT_INSTALLED)))
        runner.invoke(cli.app, [*_report_dir(tmp_path), "analyze"])
        listing = runner.invoke(cli.app, [*_report_dir(tmp_path), "report", "--list"])
        assert listing.exit_code == 0 and ".json" in listing.stdout
        # playbooks against the typical (docker-present) scenario
        cli_env(CliDockerClient(make_runner("typical"), _cfg(tmp_path)))
        pb = runner.invoke(cli.app, [*_report_dir(tmp_path), "report", "--playbooks"])
        assert pb.exit_code == 0

    def test_analyze_forensic(self, cli_env, tmp_path: Path) -> None:
        cli_env(CliDockerClient(make_runner("typical"), _cfg(tmp_path)))
        result = runner.invoke(
            cli.app,
            [*_report_dir(tmp_path), "analyze", "--forensic", "--since", "200d", "--no-write"],
        )
        assert result.exit_code in (0, 1)

    def test_cleanup_with_filters(self, cli_env, tmp_path: Path) -> None:
        client = FakeDockerClient(_fake_usage())
        cli_env(client)
        result = runner.invoke(
            cli.app,
            [
                *_report_dir(tmp_path),
                "--yes",
                "--no-dry-run",
                "cleanup",
                "--level",
                "2",
                "--name",
                "scratch*",
                "--min-size",
                "1MB",
                "--label",
                "app=db",
            ],
        )
        assert result.exit_code in (0, 1)


class TestExitCodeContract:
    """Every terminal outcome must be one of the four documented exit codes.

    These matter most under cron/systemd, where the exit code is the only
    signal: a crash that exits 1 is indistinguishable from a successful
    cleanup, and a typo that exits 2 looks like a breached threshold.
    """

    def test_unexpected_exception_is_fatal_not_warning(self, monkeypatch) -> None:
        def boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("simulated internal failure")

        monkeypatch.setattr(cli, "run_analyze", boom)
        result = runner.invoke(cli.app, ["analyze", "--no-write"])
        assert result.exit_code == int(ExitCode.FATAL)
        assert result.exit_code != int(ExitCode.WARNING_CLEANED)

    def test_unexpected_exception_renders_panel_not_traceback(self, monkeypatch) -> None:
        def boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("simulated internal failure")

        monkeypatch.setattr(cli, "run_analyze", boom)
        result = runner.invoke(cli.app, ["analyze", "--no-write"])
        assert "Traceback (most recent call last)" not in result.output

    def test_toolkit_error_keeps_its_own_exit_code(self, monkeypatch) -> None:
        def boom(*_args: object, **_kwargs: object) -> None:
            raise ConfigError("bad config", remediation="fix it")

        monkeypatch.setattr(cli, "run_analyze", boom)
        result = runner.invoke(cli.app, ["analyze", "--no-write"])
        assert result.exit_code == int(ConfigError.exit_code)

    @pytest.mark.parametrize(
        "argv",
        [["analyze", "--no-such-flag"], ["no-such-command"], ["cleanup", "--level", "nine"]],
    )
    def test_usage_errors_are_fatal_not_critical(self, argv: list[str]) -> None:
        result = runner.invoke(cli.app, argv)
        assert result.exit_code == int(ExitCode.FATAL)
        assert result.exit_code != int(ExitCode.CRITICAL)

    def test_help_still_exits_zero(self) -> None:
        assert runner.invoke(cli.app, ["--help"]).exit_code == 0
        assert runner.invoke(cli.app, ["cleanup", "--help"]).exit_code == 0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _cfg(tmp_path: Path):
    from docker_disk_toolkit.config import ToolkitConfig

    return ToolkitConfig(report_dir=tmp_path / "reports")
