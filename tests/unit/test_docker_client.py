"""Unit tests for :mod:`docker_disk_toolkit.docker_client`.

Exercises the pure parsers, the CLI backend end-to-end against fixtures, the
Null backend (graceful degradation), the Fake backend (behavioural removals),
and the factory's backend selection.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from docker_disk_toolkit import docker_client as dc
from docker_disk_toolkit.docker_client import (
    CliDockerClient,
    FakeDockerClient,
    NullDockerClient,
    build_docker_client,
    parse_container_size,
    parse_containers,
    parse_df_summary,
    parse_df_verbose,
    parse_images,
    parse_labels,
    parse_networks,
    parse_reclaimed_space,
    parse_volumes,
)
from docker_disk_toolkit.models import DockerAvailability, DockerBackend, DockerProbe
from tests.docker_fixtures import (
    CONTAINERS_TYPICAL,
    DF_SUMMARY_TYPICAL,
    DF_VERBOSE_TYPICAL,
    IMAGES_TYPICAL,
    NETWORKS_TYPICAL,
    VOLUMES_TYPICAL,
)


class TestPureParsers:
    def test_parse_labels(self) -> None:
        assert parse_labels("a=1,b=2") == {"a": "1", "b": "2"}
        assert parse_labels({"x": 1}) == {"x": "1"}
        assert parse_labels("") == {}
        assert parse_labels(None) == {}

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("2MB (virtual 182MB)", (2_000_000, 182_000_000)),
            ("10MB", (10_000_000, None)),
            ("", (None, None)),
            ("0B (virtual 500MB)", (0, 500_000_000)),
        ],
    )
    def test_parse_container_size(self, value: str, expected: tuple) -> None:
        assert parse_container_size(value) == expected

    def test_parse_df_summary(self) -> None:
        summary = parse_df_summary(DF_SUMMARY_TYPICAL)
        assert summary["images"].total_bytes == 6_580_000_000
        assert summary["images"].reclaimable_percent == 97.0
        assert summary["volumes"].total_count == 3
        assert summary["build-cache"].reclaimable_bytes == 800_000_000

    def test_parse_images_groups_and_flags(self) -> None:
        images = {img.id[:9]: img for img in parse_images(IMAGES_TYPICAL)}
        nginx = images["sha256:aa"]
        assert nginx.repo_tags == ["nginx:latest"]
        assert nginx.in_use is True and nginx.dangling is False
        dangling = images["sha256:bb"]
        assert dangling.dangling is True and dangling.repo_tags == []
        ollama = images["sha256:cc"]
        assert ollama.repo_tags == ["ollama/ollama:latest"]
        assert ollama.reclaim_bytes == 6_000_000_000  # uses unique size

    def test_parse_containers(self) -> None:
        containers = {c.name: c for c in parse_containers(CONTAINERS_TYPICAL)}
        assert containers["web"].running is True
        assert containers["web"].mounts == ["webdata"]
        assert containers["old_job"].running is False
        assert containers["old_job"].size_rw_bytes == 10_000_000

    def test_parse_volumes_and_networks(self) -> None:
        volumes = parse_volumes(VOLUMES_TYPICAL)
        assert {v.name for v in volumes} == {"webdata", "postgres_data", "scratch_tmp"}
        networks = parse_networks(NETWORKS_TYPICAL)
        builtin = {n.name: n.builtin for n in networks}
        assert builtin["bridge"] is True and builtin["myapp_default"] is False

    def test_parse_df_verbose(self) -> None:
        enrich, build_cache = parse_df_verbose(DF_VERBOSE_TYPICAL)
        assert enrich["postgres_data"] == (1_200_000_000, 0)
        assert enrich["webdata"] == (50_000_000, 1)
        assert len(build_cache) == 1 and build_cache[0].size_bytes == 800_000_000

    def test_parse_df_verbose_degrades(self) -> None:
        assert parse_df_verbose("") == ({}, [])
        assert parse_df_verbose("not json") == ({}, [])
        assert parse_df_verbose('{"Volumes":[{"Name":"x","Size":"N/A","Links":"0"}]}')[0]["x"] == (
            None,
            0,
        )

    def test_parse_reclaimed_space(self) -> None:
        assert parse_reclaimed_space("Total reclaimed space: 1.5GB") == 1_500_000_000
        assert parse_reclaimed_space("nothing here") == 0


class TestCliClientProbe:
    def test_probe_ok_sets_backend(self, cli_client: Callable[[str], CliDockerClient]) -> None:
        probe = cli_client("typical").probe()
        assert probe.availability is DockerAvailability.OK
        assert probe.backend is DockerBackend.ENGINE
        assert probe.server_version == "27.1.1"

    def test_probe_desktop_backend_family(
        self, cli_client: Callable[[str], CliDockerClient]
    ) -> None:
        # Host OS (windows vs mac) depends on the machine running the test; assert
        # membership in the desktop family rather than a specific host.
        probe = cli_client("desktop-windows").probe()
        assert probe.backend in dc.DESKTOP_BACKENDS

    def test_classify_backend_host_distinction(self) -> None:
        from docker_disk_toolkit.models import DockerInfo

        desktop = DockerInfo(operating_system="Docker Desktop", os_type="linux")
        assert dc._classify_backend(desktop, "Windows") is DockerBackend.DESKTOP_WINDOWS
        assert dc._classify_backend(desktop, "Darwin") is DockerBackend.DESKTOP_MAC
        assert dc._classify_backend(desktop, "Linux") is DockerBackend.DESKTOP_LINUX
        engine = DockerInfo(operating_system="Ubuntu 24.04", docker_root_dir="/var/lib/docker")
        assert dc._classify_backend(engine, "Linux") is DockerBackend.ENGINE
        wsl = DockerInfo(operating_system="Ubuntu", docker_root_dir="/mnt/wsl/docker")
        assert dc._classify_backend(wsl, "Linux") is DockerBackend.WSL

    def test_probe_rootless_backend(self, cli_client: Callable[[str], CliDockerClient]) -> None:
        assert cli_client("rootless").probe().backend is DockerBackend.ROOTLESS

    def test_probe_daemon_down(self, cli_client: Callable[[str], CliDockerClient]) -> None:
        probe = cli_client("daemon-down").probe()
        assert probe.availability is DockerAvailability.DAEMON_UNREACHABLE
        assert "daemon" in probe.remediation.lower()

    def test_probe_permission_denied(self, cli_client: Callable[[str], CliDockerClient]) -> None:
        probe = cli_client("permission-denied").probe()
        assert probe.availability is DockerAvailability.PERMISSION_DENIED
        assert "group" in probe.remediation.lower()

    def test_probe_not_installed(self, cli_client: Callable[[str], CliDockerClient]) -> None:
        assert cli_client("not-installed").probe().availability is DockerAvailability.NOT_INSTALLED

    def test_probe_timeout(self, cli_client: Callable[[str], CliDockerClient]) -> None:
        assert cli_client("timeout").probe().availability is DockerAvailability.TIMEOUT

    def test_probe_cached(self, cli_client: Callable[[str], CliDockerClient]) -> None:
        client = cli_client("typical")
        first = client.probe()
        assert client.probe() is first  # cached
        client.probe(force_refresh=True)  # re-runs without error


class TestCliClientUsage:
    def test_collect_usage_full(self, cli_client: Callable[[str], CliDockerClient]) -> None:
        usage = cli_client("typical").collect_usage()
        assert usage.images.total_bytes == 6_580_000_000
        assert len(usage.image_list) == 3
        assert len(usage.container_list) == 2
        # volume sizes enriched from df -v
        postgres = next(v for v in usage.volume_list if v.name == "postgres_data")
        assert postgres.size_bytes == 1_200_000_000 and postgres.links == 0
        assert len(usage.build_cache_list) == 1
        assert usage.total_bytes > 0

    def test_collect_usage_cached(self, cli_client: Callable[[str], CliDockerClient]) -> None:
        client = cli_client("typical")
        assert client.collect_usage() is client.collect_usage()

    def test_empty_scenario(self, cli_client: Callable[[str], CliDockerClient]) -> None:
        usage = cli_client("empty").collect_usage()
        assert usage.image_list == []
        assert usage.total_bytes == 0

    def test_info_backend_fields(self, cli_client: Callable[[str], CliDockerClient]) -> None:
        info = cli_client("rootless").info()
        assert info.rootless is True
        assert info.docker_root_dir == "/home/user/.local/share/docker"


class TestCliClientMutations:
    def test_dry_run_does_not_execute(self, cli_client: Callable[[str], CliDockerClient]) -> None:
        client = cli_client("typical")
        res = client.remove_image("sha256:bbb", force=False, dry_run=True)
        assert res.ok and "dry-run" in res.stdout
        # runner recorded no rm call
        runner = client._runner  # type: ignore[attr-defined]
        assert not any("rm" in call for call in runner.calls)

    def test_real_remove_executes(self, cli_client: Callable[[str], CliDockerClient]) -> None:
        client = cli_client("typical")
        res = client.remove_container("c2", force=True, dry_run=False)
        assert res.ok
        assert client.remove_volume("scratch_tmp", force=False, dry_run=False).ok
        assert client.remove_network("n3", dry_run=False).ok

    def test_prune_build_cache(self, cli_client: Callable[[str], CliDockerClient]) -> None:
        outcome = cli_client("typical").prune_build_cache(dry_run=False)
        assert outcome.ok and outcome.reclaimed_bytes == 800_000_000

    def test_prune_build_cache_dry_run(self, cli_client: Callable[[str], CliDockerClient]) -> None:
        outcome = cli_client("typical").prune_build_cache(dry_run=True)
        assert outcome.ok and outcome.reclaimed_bytes == 0


class TestNullClient:
    def test_reads_are_empty(self) -> None:
        probe = DockerProbe(availability=DockerAvailability.NOT_INSTALLED, remediation="install it")
        client = NullDockerClient(probe)
        assert client.probe() is probe
        assert client.collect_usage().total_bytes == 0
        assert client.info().docker_root_dir is None

    def test_mutations_blocked(self) -> None:
        client = NullDockerClient(DockerProbe(availability=DockerAvailability.DAEMON_UNREACHABLE))
        assert not client.remove_image("x", force=True, dry_run=False).ok
        assert not client.remove_volume("v", force=False, dry_run=False).ok
        assert not client.prune_build_cache(dry_run=False).ok


class TestFakeClient:
    def test_removals_mutate_state(self) -> None:
        from docker_disk_toolkit.models import DockerUsage, ImageInfo, VolumeInfo

        usage = DockerUsage(
            image_list=[ImageInfo(id="i1", size_bytes=100), ImageInfo(id="i2", size_bytes=200)],
            volume_list=[VolumeInfo(name="v1", size_bytes=50)],
        )
        client = FakeDockerClient(usage)
        assert client.remove_image("i1", force=False, dry_run=False).ok
        assert [i.id for i in client.usage.image_list] == ["i2"]
        assert client.remove_volume("v1", force=False, dry_run=False).ok
        assert client.usage.volume_list == []
        assert client.removed == [("image", "i1"), ("volume", "v1")]

    def test_dry_run_no_mutation(self) -> None:
        from docker_disk_toolkit.models import DockerUsage, ImageInfo

        client = FakeDockerClient(DockerUsage(image_list=[ImageInfo(id="i1", size_bytes=100)]))
        client.remove_image("i1", force=False, dry_run=True)
        assert len(client.usage.image_list) == 1

    def test_fail_on(self) -> None:
        from docker_disk_toolkit.models import DockerUsage, ImageInfo

        client = FakeDockerClient(
            DockerUsage(image_list=[ImageInfo(id="i1", size_bytes=1)]), fail_on={"i1"}
        )
        res = client.remove_image("i1", force=False, dry_run=False)
        assert not res.ok and res.error == "simulated failure"
        assert len(client.usage.image_list) == 1


class TestFactory:
    def test_returns_cli_when_binary_present(
        self, make_config: Callable[..., object], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(dc.shutil, "which", lambda _: "/usr/bin/docker")
        client = build_docker_client(make_config())  # type: ignore[arg-type]
        assert isinstance(client, CliDockerClient)

    def test_returns_null_when_absent(
        self, make_config: Callable[..., object], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(dc.shutil, "which", lambda _: None)
        monkeypatch.setattr(dc, "_try_build_sdk_client", lambda: None)
        client = build_docker_client(make_config())  # type: ignore[arg-type]
        assert isinstance(client, NullDockerClient)
        assert client.probe().availability is DockerAvailability.NOT_INSTALLED
