"""Docker abstraction — subprocess-first, with SDK and null fallbacks.

The rest of the toolkit talks to Docker exclusively through the
:class:`DockerClient` protocol. Three implementations exist:

* :class:`CliDockerClient` — the primary backend. Shells out to the ``docker``
  CLI via an injected :class:`~docker_disk_toolkit.utils.CommandRunner`,
  requesting machine-readable ``--format '{{json .}}'`` output. Because the
  runner is injected, the entire parsing surface is testable with recorded
  fixtures and no live daemon.
* :class:`ApiDockerClient` — an optional ``docker-py`` fallback used when the
  CLI is unavailable but the SDK can reach a daemon.
* :class:`NullDockerClient` — a graceful-degradation stand-in returned when no
  Docker is reachable, so "Docker absent" is a normal object rather than a
  scatter of ``None`` checks.

:func:`build_docker_client` selects one and **never raises**.
"""

from __future__ import annotations

import json
import platform
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from .models import (
    BuildCacheInfo,
    CategoryUsage,
    ContainerInfo,
    DockerAvailability,
    DockerBackend,
    DockerInfo,
    DockerProbe,
    DockerUsage,
    ImageInfo,
    NetworkInfo,
    ObjectKind,
    VolumeInfo,
)
from .utils import (
    CommandResult,
    CommandRunner,
    default_runner,
    iter_ndjson,
    parse_count,
    parse_docker_time,
    parse_reclaimable,
    parse_size,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import ToolkitConfig

BUILTIN_NETWORKS = {"bridge", "host", "none"}
_JSON_FORMAT = "{{json .}}"


# ---------------------------------------------------------------------------
# Result value objects (for mutating operations)
# ---------------------------------------------------------------------------


@dataclass
class RemoveResult:
    """Outcome of a single object removal."""

    ok: bool
    command: list[str]
    stdout: str = ""
    error: str | None = None


@dataclass
class PruneOutcome:
    """Outcome of a bulk prune (e.g. build cache)."""

    ok: bool
    command: list[str]
    reclaimed_bytes: int = 0
    deleted: list[str] = field(default_factory=list)
    error: str | None = None


# ---------------------------------------------------------------------------
# Pure parsers (module-level so they are trivially unit-tested)
# ---------------------------------------------------------------------------


def parse_labels(value: object) -> dict[str, str]:
    """Parse a Docker ``Labels`` field (``"k=v,k2=v2"`` string or dict)."""
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    if not value or not isinstance(value, str):
        return {}
    labels: dict[str, str] = {}
    for pair in value.split(","):
        pair = pair.strip()
        if not pair:
            continue
        key, _, val = pair.partition("=")
        labels[key.strip()] = val.strip()
    return labels


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return bool(value)


_CONTAINER_SIZE_RE = re.compile(
    r"^\s*(?P<rw>[\d.,]+\s*[A-Za-z]*)\s*(?:\(virtual\s+(?P<virt>[\d.,]+\s*[A-Za-z]*)\))?",
)


def parse_container_size(value: object) -> tuple[int | None, int | None]:
    """Parse a container ``Size`` field like ``"1.09kB (virtual 1.22GB)"``.

    Returns:
        ``(size_rw_bytes, size_root_fs_bytes)``; either may be ``None`` when the
        container was listed without ``--size``.
    """
    if not value or not isinstance(value, str):
        return (None, None)
    match = _CONTAINER_SIZE_RE.match(value)
    if match is None:
        return (None, None)
    rw = parse_size(match.group("rw")) if match.group("rw") else None
    virt = parse_size(match.group("virt")) if match.group("virt") else None
    return (rw, virt)


_DF_TYPE_MAP = {
    "Images": "images",
    "Containers": "containers",
    "Local Volumes": "volumes",
    "Build Cache": "build-cache",
}


def parse_df_summary(text: str) -> dict[str, CategoryUsage]:
    """Parse ``docker system df --format '{{json .}}'`` (4 NDJSON rows)."""
    out: dict[str, CategoryUsage] = {}
    for row in iter_ndjson(text):
        key = _DF_TYPE_MAP.get(str(row.get("Type", "")))
        if key is None:
            continue
        reclaim_bytes, reclaim_pct = parse_reclaimable(str(row.get("Reclaimable", "0B")))
        out[key] = CategoryUsage(
            total_bytes=parse_size(str(row.get("Size", "0B"))),
            reclaimable_bytes=reclaim_bytes,
            reclaimable_percent=reclaim_pct,
            active=parse_count(row.get("Active")),
            total_count=parse_count(row.get("TotalCount")),
        )
    return out


def parse_images(text: str) -> list[ImageInfo]:
    """Parse ``docker image ls`` JSON, grouping multiple tags by image ID."""
    grouped: dict[str, ImageInfo] = {}
    for row in iter_ndjson(text):
        image_id = str(row.get("ID", "")).strip()
        if not image_id:
            continue
        repo = str(row.get("Repository", "<none>"))
        tag = str(row.get("Tag", "<none>"))
        digest = str(row.get("Digest", "<none>"))
        unique_raw = row.get("UniqueSize")
        info = grouped.get(image_id)
        if info is None:
            info = ImageInfo(
                id=image_id,
                size_bytes=parse_size(str(row.get("Size", "0B"))),
                shared_size_bytes=parse_size(str(row.get("SharedSize", "0B"))),
                unique_size_bytes=(parse_size(str(unique_raw)) if unique_raw is not None else None),
                used_by_containers=parse_count(row.get("Containers")),
                created_at=parse_docker_time(str(row.get("CreatedAt", ""))),
                labels=parse_labels(row.get("Labels")),
            )
            grouped[image_id] = info
        if repo != "<none>" and tag != "<none>":
            info.repo_tags.append(f"{repo}:{tag}")
        if digest and digest != "<none>":
            digest_ref = f"{repo}@{digest}" if repo != "<none>" else digest
            if digest_ref not in info.repo_digests:
                info.repo_digests.append(digest_ref)
    for info in grouped.values():
        info.dangling = len(info.repo_tags) == 0
        info.in_use = info.used_by_containers > 0
    return list(grouped.values())


def parse_containers(text: str) -> list[ContainerInfo]:
    """Parse ``docker container ls -a --size`` JSON into containers."""
    containers: list[ContainerInfo] = []
    for row in iter_ndjson(text):
        container_id = str(row.get("ID", "")).strip()
        if not container_id:
            continue
        names = str(row.get("Names", "")).split(",")
        name = names[0].strip() if names and names[0].strip() else container_id[:12]
        state = str(row.get("State", "")).strip().lower()
        status = str(row.get("Status", ""))
        running = state == "running" or (not state and status.startswith("Up"))
        rw, root_fs = parse_container_size(row.get("Size"))
        mounts = [m.strip() for m in str(row.get("Mounts", "")).split(",") if m.strip()]
        containers.append(
            ContainerInfo(
                id=container_id,
                name=name,
                image=str(row.get("Image", "")),
                state=state or ("running" if running else "exited"),
                running=running,
                size_rw_bytes=rw,
                size_root_fs_bytes=root_fs,
                created_at=parse_docker_time(str(row.get("CreatedAt", ""))),
                mounts=mounts,
                labels=parse_labels(row.get("Labels")),
            )
        )
    return containers


def parse_volumes(text: str) -> list[VolumeInfo]:
    """Parse ``docker volume ls`` JSON (size/links enriched later from df -v)."""
    volumes: list[VolumeInfo] = []
    for row in iter_ndjson(text):
        name = str(row.get("Name", "")).strip()
        if not name:
            continue
        volumes.append(
            VolumeInfo(
                name=name,
                driver=str(row.get("Driver", "local")),
                mountpoint=row.get("Mountpoint") or None,
                labels=parse_labels(row.get("Labels")),
                links=parse_count(row.get("Links")),
            )
        )
    return volumes


def parse_networks(text: str) -> list[NetworkInfo]:
    """Parse ``docker network ls`` JSON into networks."""
    networks: list[NetworkInfo] = []
    for row in iter_ndjson(text):
        name = str(row.get("Name", "")).strip()
        if not name:
            continue
        networks.append(
            NetworkInfo(
                id=str(row.get("ID", "")),
                name=name,
                driver=str(row.get("Driver", "bridge")),
                scope=str(row.get("Scope", "local")),
                created_at=parse_docker_time(str(row.get("CreatedAt", ""))),
                builtin=name in BUILTIN_NETWORKS,
            )
        )
    return networks


def parse_df_verbose(text: str) -> tuple[dict[str, tuple[int | None, int]], list[BuildCacheInfo]]:
    """Parse ``docker system df -v`` JSON for volume sizes and build cache.

    Returns:
        A ``(volume_enrichment, build_cache)`` tuple where ``volume_enrichment``
        maps volume name -> ``(size_bytes, links)``. Degrades to empties when the
        output is missing or unparseable (older engines omit ``BuildCache``).
    """
    text = text.strip()
    if not text:
        return ({}, [])
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return ({}, [])
    if not isinstance(data, dict):
        return ({}, [])

    volume_enrichment: dict[str, tuple[int | None, int]] = {}
    for vol in data.get("Volumes", []) or []:
        if not isinstance(vol, dict):
            continue
        name = str(vol.get("Name", "")).strip()
        if not name:
            continue
        raw_size = vol.get("Size")
        size = (
            None
            if str(raw_size).strip().upper() in {"N/A", "", "NONE"}
            else parse_size(str(raw_size))
        )
        volume_enrichment[name] = (size, parse_count(vol.get("Links")))

    build_cache: list[BuildCacheInfo] = []
    for entry in data.get("BuildCache", []) or []:
        if not isinstance(entry, dict):
            continue
        build_cache.append(
            BuildCacheInfo(
                id=str(entry.get("ID") or entry.get("CacheID") or ""),
                cache_type=entry.get("CacheType") or entry.get("Type"),
                size_bytes=parse_size(str(entry.get("Size", "0B"))),
                in_use=_as_bool(entry.get("InUse")),
                shared=_as_bool(entry.get("Shared")),
                last_used_at=parse_docker_time(entry.get("LastUsedAt")),
                usage_count=parse_count(entry.get("UsageCount")),
                description=entry.get("Description"),
            )
        )
    return (volume_enrichment, build_cache)


def parse_reclaimed_space(text: str) -> int:
    """Extract ``Total reclaimed space: X`` from prune stdout."""
    match = re.search(r"Total reclaimed space:\s*([\d.,]+\s*[A-Za-z]+)", text)
    return parse_size(match.group(1)) if match else 0


# ---------------------------------------------------------------------------
# Probe / info parsing
# ---------------------------------------------------------------------------


def _classify_version(result: CommandResult, cli_path: str) -> DockerProbe:
    """Turn a ``docker version`` command result into a structured probe."""
    if result.returncode == 127:
        return DockerProbe(
            availability=DockerAvailability.NOT_INSTALLED,
            remediation=(
                f"'{cli_path}' was not found on PATH. Install Docker Engine or Docker "
                "Desktop, or set docker.cli_path in your config."
            ),
            raw_stderr=result.stderr or None,
        )
    if result.returncode == 124:
        return DockerProbe(
            availability=DockerAvailability.TIMEOUT,
            remediation="Docker did not respond in time (Desktop may be starting). Retry shortly.",
            raw_stderr=result.stderr or None,
        )

    stderr_lc = result.stderr.lower()
    permission = any(
        sig in stderr_lc for sig in ("permission denied", "access is denied", "dial unix")
    )
    unreachable = any(
        sig in stderr_lc
        for sig in (
            "cannot connect to the docker daemon",
            "error during connect",
            "the system cannot find the file",
            "is the docker daemon running",
            "open //./pipe/docker_engine",
        )
    )

    client_version: str | None = None
    server_version: str | None = None
    api_version: str | None = None
    context_name: str | None = None
    try:
        payload = json.loads(result.stdout) if result.stdout.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    if isinstance(payload, dict):
        client = payload.get("Client") or {}
        server = payload.get("Server") or {}
        if isinstance(client, dict):
            client_version = client.get("Version")
            api_version = client.get("ApiVersion")
        if isinstance(server, dict):
            server_version = server.get("Version")

    if permission and server_version is None:
        return DockerProbe(
            availability=DockerAvailability.PERMISSION_DENIED,
            client_version=client_version,
            remediation=(
                "Permission denied talking to the Docker socket. On Linux add your "
                "user to the 'docker' group (sudo usermod -aG docker $USER) and re-login, "
                "or start Docker Desktop on Windows/macOS."
            ),
            raw_stderr=result.stderr or None,
        )
    if server_version is None and (unreachable or result.returncode != 0):
        return DockerProbe(
            availability=DockerAvailability.DAEMON_UNREACHABLE,
            client_version=client_version,
            remediation=(
                "The Docker daemon is not reachable. Start Docker Desktop, or run "
                "'sudo systemctl start docker' on Linux."
            ),
            raw_stderr=result.stderr or None,
        )
    return DockerProbe(
        availability=DockerAvailability.OK,
        client_version=client_version,
        server_version=server_version,
        api_version=api_version,
        context_name=context_name,
    )


DESKTOP_BACKENDS = frozenset(
    {DockerBackend.DESKTOP_WINDOWS, DockerBackend.DESKTOP_MAC, DockerBackend.DESKTOP_LINUX}
)


def _classify_backend(info: DockerInfo, host_system: str | None = None) -> DockerBackend:
    """Infer the Docker backend/flavor from ``docker info`` + host platform.

    ``docker info`` alone cannot distinguish Docker Desktop on Windows vs macOS
    (Desktop always runs a Linux VM, so ``OSType`` is always ``linux``); the host
    platform (``platform.system()``) supplies that. Pass ``host_system``
    explicitly for deterministic tests.
    """
    host = (host_system or platform.system()).lower()
    root = (info.docker_root_dir or "").lower()
    op_sys = (info.operating_system or "").lower()
    if info.rootless:
        return DockerBackend.ROOTLESS
    if "docker desktop" in op_sys:
        if host.startswith("windows"):
            return DockerBackend.DESKTOP_WINDOWS
        if host.startswith("darwin"):
            return DockerBackend.DESKTOP_MAC
        return DockerBackend.DESKTOP_LINUX
    if "wsl" in root or "/mnt/wsl" in root or "docker-desktop" in root:
        return DockerBackend.WSL
    return DockerBackend.ENGINE


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class DockerClient(Protocol):
    """The typed surface the analyzer and cleaner depend on."""

    def probe(self, *, force_refresh: bool = False) -> DockerProbe: ...

    def info(self) -> DockerInfo: ...

    def collect_usage(
        self, *, with_container_sizes: bool = True, force_refresh: bool = False
    ) -> DockerUsage: ...

    def remove_image(self, image_id: str, *, force: bool, dry_run: bool) -> RemoveResult: ...

    def remove_container(
        self, container_id: str, *, force: bool, dry_run: bool
    ) -> RemoveResult: ...

    def remove_volume(self, name: str, *, force: bool, dry_run: bool) -> RemoveResult: ...

    def remove_network(self, network_id: str, *, dry_run: bool) -> RemoveResult: ...

    def prune_build_cache(
        self, *, all_cache: bool = False, dry_run: bool, until: str | None = None
    ) -> PruneOutcome: ...

    def prune_networks(self, *, dry_run: bool) -> PruneOutcome: ...


# ---------------------------------------------------------------------------
# CLI backend
# ---------------------------------------------------------------------------


class CliDockerClient:
    """Primary :class:`DockerClient` backed by the ``docker`` CLI."""

    def __init__(
        self,
        runner: CommandRunner,
        config: ToolkitConfig,
        *,
        cli_path: str = "docker",
    ) -> None:
        self._runner = runner
        self._config = config
        self._cli = cli_path
        self._timeout = config.docker.timeout_seconds
        self._probe: DockerProbe | None = None
        self._usage: DockerUsage | None = None

    # -- low-level ----------------------------------------------------------

    def _env(self) -> dict[str, str] | None:
        if self._config.docker.host:
            return {"DOCKER_HOST": self._config.docker.host}
        return None

    def _run(self, args: Sequence[str], *, timeout: float | None = None) -> CommandResult:
        return self._runner.run(
            [self._cli, *args], timeout=timeout or self._timeout, env=self._env()
        )

    # -- probe / info -------------------------------------------------------

    def probe(self, *, force_refresh: bool = False) -> DockerProbe:
        if self._probe is not None and not force_refresh:
            return self._probe
        result = self._run(["version", "--format", _JSON_FORMAT], timeout=min(self._timeout, 8.0))
        probe = _classify_version(result, self._cli)
        if probe.ok:
            info = self.info()
            probe = probe.model_copy(
                update={
                    "backend": _classify_backend(info, platform.system()),
                    "server_version": probe.server_version or info.server_version,
                }
            )
        self._probe = probe
        return probe

    def info(self) -> DockerInfo:
        result = self._run(["info", "--format", _JSON_FORMAT])
        if not result.ok or not result.stdout.strip():
            return DockerInfo()
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return DockerInfo()
        sec_opts = data.get("SecurityOptions") or []
        rootless = any("rootless" in str(opt).lower() for opt in sec_opts)
        return DockerInfo(
            docker_root_dir=data.get("DockerRootDir"),
            storage_driver=data.get("Driver"),
            os_type=data.get("OSType"),
            operating_system=data.get("OperatingSystem"),
            server_version=data.get("ServerVersion"),
            rootless=rootless,
            name=data.get("Name"),
        )

    # -- usage --------------------------------------------------------------

    def collect_usage(
        self, *, with_container_sizes: bool = True, force_refresh: bool = False
    ) -> DockerUsage:
        if self._usage is not None and not force_refresh:
            return self._usage

        summary = parse_df_summary(self._run(["system", "df", "--format", _JSON_FORMAT]).stdout)
        images = parse_images(
            self._run(
                ["image", "ls", "-a", "--no-trunc", "--digests", "--format", _JSON_FORMAT]
            ).stdout
        )
        container_args = ["container", "ls", "-a", "--no-trunc", "--format", _JSON_FORMAT]
        if with_container_sizes:
            container_args.insert(2, "--size")
        containers = parse_containers(self._run(container_args).stdout)
        volumes = parse_volumes(self._run(["volume", "ls", "--format", _JSON_FORMAT]).stdout)
        networks = parse_networks(
            self._run(["network", "ls", "--no-trunc", "--format", _JSON_FORMAT]).stdout
        )
        vol_enrich, build_cache = parse_df_verbose(
            self._run(["system", "df", "-v", "--format", _JSON_FORMAT]).stdout
        )
        for vol in volumes:
            if vol.name in vol_enrich:
                size, links = vol_enrich[vol.name]
                vol.size_bytes = size
                vol.links = links

        usage = DockerUsage(
            images=summary.get("images", CategoryUsage()),
            containers=summary.get("containers", CategoryUsage()),
            volumes=summary.get("volumes", CategoryUsage()),
            build_cache=summary.get("build-cache", CategoryUsage()),
            image_list=images,
            container_list=containers,
            volume_list=volumes,
            build_cache_list=build_cache,
            network_list=networks,
        )
        self._usage = usage
        return usage

    # -- mutations ----------------------------------------------------------

    def _remove(self, kind: ObjectKind, args: Sequence[str], *, dry_run: bool) -> RemoveResult:
        argv = [self._cli, *args]
        if dry_run:
            return RemoveResult(ok=True, command=argv, stdout="(dry-run: not executed)")
        result = self._run(args)
        return RemoveResult(
            ok=result.ok,
            command=argv,
            stdout=result.stdout,
            error=None if result.ok else (result.stderr or f"exit {result.returncode}"),
        )

    def remove_image(self, image_id: str, *, force: bool, dry_run: bool) -> RemoveResult:
        args = ["image", "rm", *(["-f"] if force else []), image_id]
        return self._remove(ObjectKind.IMAGE, args, dry_run=dry_run)

    def remove_container(self, container_id: str, *, force: bool, dry_run: bool) -> RemoveResult:
        args = ["container", "rm", *(["-f"] if force else []), container_id]
        return self._remove(ObjectKind.CONTAINER, args, dry_run=dry_run)

    def remove_volume(self, name: str, *, force: bool, dry_run: bool) -> RemoveResult:
        args = ["volume", "rm", *(["-f"] if force else []), name]
        return self._remove(ObjectKind.VOLUME, args, dry_run=dry_run)

    def remove_network(self, network_id: str, *, dry_run: bool) -> RemoveResult:
        return self._remove(ObjectKind.NETWORK, ["network", "rm", network_id], dry_run=dry_run)

    def prune_build_cache(
        self, *, all_cache: bool = False, dry_run: bool, until: str | None = None
    ) -> PruneOutcome:
        args = ["builder", "prune", "-f"]
        if all_cache:
            args.append("-a")
        if until:
            args.extend(["--filter", f"until={until}"])
        argv = [self._cli, *args]
        if dry_run:
            return PruneOutcome(ok=True, command=argv)
        result = self._run(args)
        return PruneOutcome(
            ok=result.ok,
            command=argv,
            reclaimed_bytes=parse_reclaimed_space(result.stdout),
            error=None if result.ok else (result.stderr or f"exit {result.returncode}"),
        )

    def prune_networks(self, *, dry_run: bool) -> PruneOutcome:
        args = ["network", "prune", "-f"]
        argv = [self._cli, *args]
        if dry_run:
            return PruneOutcome(ok=True, command=argv)
        result = self._run(args)
        return PruneOutcome(
            ok=result.ok,
            command=argv,
            error=None if result.ok else (result.stderr or f"exit {result.returncode}"),
        )


# ---------------------------------------------------------------------------
# Null backend (graceful degradation)
# ---------------------------------------------------------------------------


class NullDockerClient:
    """Stand-in returned when no Docker is reachable. Reads yield empties."""

    def __init__(self, probe: DockerProbe) -> None:
        self._probe = probe

    def probe(self, *, force_refresh: bool = False) -> DockerProbe:
        return self._probe

    def info(self) -> DockerInfo:
        return DockerInfo()

    def collect_usage(
        self, *, with_container_sizes: bool = True, force_refresh: bool = False
    ) -> DockerUsage:
        return DockerUsage()

    def _blocked(self, kind: ObjectKind) -> RemoveResult:
        return RemoveResult(
            ok=False,
            command=[],
            error=f"Docker is unavailable ({self._probe.availability}); cannot remove {kind}.",
        )

    def remove_image(self, image_id: str, *, force: bool, dry_run: bool) -> RemoveResult:
        return self._blocked(ObjectKind.IMAGE)

    def remove_container(self, container_id: str, *, force: bool, dry_run: bool) -> RemoveResult:
        return self._blocked(ObjectKind.CONTAINER)

    def remove_volume(self, name: str, *, force: bool, dry_run: bool) -> RemoveResult:
        return self._blocked(ObjectKind.VOLUME)

    def remove_network(self, network_id: str, *, dry_run: bool) -> RemoveResult:
        return self._blocked(ObjectKind.NETWORK)

    def prune_build_cache(
        self, *, all_cache: bool = False, dry_run: bool, until: str | None = None
    ) -> PruneOutcome:
        return PruneOutcome(
            ok=False,
            command=[],
            error=f"Docker is unavailable ({self._probe.availability}).",
        )

    def prune_networks(self, *, dry_run: bool) -> PruneOutcome:
        return PruneOutcome(
            ok=False, command=[], error=f"Docker is unavailable ({self._probe.availability})."
        )


# ---------------------------------------------------------------------------
# In-memory fake backend (behavioural tests)
# ---------------------------------------------------------------------------


class FakeDockerClient:
    """In-memory :class:`DockerClient` for behavioural cleaner/monitor tests.

    Removals mutate the in-memory ``usage`` so before/after deltas are testable
    without a daemon. ``fail_on`` lets a test simulate per-object failures.
    """

    def __init__(
        self,
        usage: DockerUsage | None = None,
        *,
        probe: DockerProbe | None = None,
        info: DockerInfo | None = None,
        fail_on: set[str] | None = None,
    ) -> None:
        self.usage = usage or DockerUsage()
        self._probe = probe or DockerProbe(
            availability=DockerAvailability.OK, backend=DockerBackend.ENGINE
        )
        self._info = info or DockerInfo(docker_root_dir="/var/lib/docker", os_type="linux")
        self.fail_on = fail_on or set()
        self.removed: list[tuple[str, str]] = []

    def probe(self, *, force_refresh: bool = False) -> DockerProbe:
        return self._probe

    def info(self) -> DockerInfo:
        return self._info

    def collect_usage(
        self, *, with_container_sizes: bool = True, force_refresh: bool = False
    ) -> DockerUsage:
        return self.usage

    def _do_remove(self, kind: ObjectKind, key: str, matcher) -> RemoveResult:  # type: ignore[no-untyped-def]
        argv = ["docker", str(kind), "rm", key]
        if key in self.fail_on:
            return RemoveResult(ok=False, command=argv, error="simulated failure")
        matcher()
        self.removed.append((str(kind), key))
        return RemoveResult(ok=True, command=argv, stdout="removed")

    def remove_image(self, image_id: str, *, force: bool, dry_run: bool) -> RemoveResult:
        if dry_run:
            return RemoveResult(ok=True, command=["docker", "image", "rm", image_id])
        return self._do_remove(
            ObjectKind.IMAGE,
            image_id,
            lambda: self.usage.image_list.__setitem__(
                slice(None), [i for i in self.usage.image_list if i.id != image_id]
            ),
        )

    def remove_container(self, container_id: str, *, force: bool, dry_run: bool) -> RemoveResult:
        if dry_run:
            return RemoveResult(ok=True, command=["docker", "container", "rm", container_id])
        return self._do_remove(
            ObjectKind.CONTAINER,
            container_id,
            lambda: self.usage.container_list.__setitem__(
                slice(None), [c for c in self.usage.container_list if c.id != container_id]
            ),
        )

    def remove_volume(self, name: str, *, force: bool, dry_run: bool) -> RemoveResult:
        if dry_run:
            return RemoveResult(ok=True, command=["docker", "volume", "rm", name])
        return self._do_remove(
            ObjectKind.VOLUME,
            name,
            lambda: self.usage.volume_list.__setitem__(
                slice(None), [v for v in self.usage.volume_list if v.name != name]
            ),
        )

    def remove_network(self, network_id: str, *, dry_run: bool) -> RemoveResult:
        if dry_run:
            return RemoveResult(ok=True, command=["docker", "network", "rm", network_id])
        return self._do_remove(
            ObjectKind.NETWORK,
            network_id,
            lambda: self.usage.network_list.__setitem__(
                slice(None), [n for n in self.usage.network_list if n.id != network_id]
            ),
        )

    def prune_build_cache(
        self, *, all_cache: bool = False, dry_run: bool, until: str | None = None
    ) -> PruneOutcome:
        reclaim = sum(
            bc.size_bytes for bc in self.usage.build_cache_list if all_cache or not bc.in_use
        )
        if not dry_run:
            self.usage.build_cache_list = [
                bc for bc in self.usage.build_cache_list if not (all_cache or not bc.in_use)
            ]
        return PruneOutcome(
            ok=True, command=["docker", "builder", "prune", "-f"], reclaimed_bytes=reclaim
        )

    def prune_networks(self, *, dry_run: bool) -> PruneOutcome:
        removable = [n for n in self.usage.network_list if not n.builtin]
        if not dry_run:
            self.usage.network_list = [n for n in self.usage.network_list if n.builtin]
            self.removed.extend(("network", n.id) for n in removable)
        return PruneOutcome(ok=True, command=["docker", "network", "prune", "-f"])


# ---------------------------------------------------------------------------
# Optional docker-py (SDK) backend
# ---------------------------------------------------------------------------


class ApiDockerClient:  # pragma: no cover - requires docker-py + a live daemon
    """Best-effort ``docker-py`` fallback (used only when the CLI is absent).

    This adapter implements the read/probe surface; destructive operations are
    delegated to the SDK. It is intentionally thin — the CLI backend is the
    tested, primary path.
    """

    def __init__(self, sdk_client: object) -> None:
        self._sdk = sdk_client

    def probe(self, *, force_refresh: bool = False) -> DockerProbe:
        try:
            version = self._sdk.version()  # type: ignore[attr-defined]
        except Exception as exc:
            return DockerProbe(
                availability=DockerAvailability.DAEMON_UNREACHABLE,
                remediation="docker-py could not reach the daemon; start Docker.",
                raw_stderr=str(exc),
            )
        return DockerProbe(
            availability=DockerAvailability.OK,
            server_version=version.get("Version"),
            api_version=version.get("ApiVersion"),
        )

    def info(self) -> DockerInfo:
        data = self._sdk.info()  # type: ignore[attr-defined]
        return DockerInfo(
            docker_root_dir=data.get("DockerRootDir"),
            storage_driver=data.get("Driver"),
            os_type=data.get("OSType"),
            operating_system=data.get("OperatingSystem"),
            server_version=data.get("ServerVersion"),
        )

    def collect_usage(
        self, *, with_container_sizes: bool = True, force_refresh: bool = False
    ) -> DockerUsage:
        return DockerUsage()

    def remove_image(self, image_id: str, *, force: bool, dry_run: bool) -> RemoveResult:
        if dry_run:
            return RemoveResult(ok=True, command=["<sdk>", "image", "rm", image_id])
        self._sdk.images.remove(image_id, force=force)  # type: ignore[attr-defined]
        return RemoveResult(ok=True, command=["<sdk>", "image", "rm", image_id])

    def remove_container(self, container_id: str, *, force: bool, dry_run: bool) -> RemoveResult:
        if dry_run:
            return RemoveResult(ok=True, command=["<sdk>", "container", "rm", container_id])
        self._sdk.containers.get(container_id).remove(force=force)  # type: ignore[attr-defined]
        return RemoveResult(ok=True, command=["<sdk>", "container", "rm", container_id])

    def remove_volume(self, name: str, *, force: bool, dry_run: bool) -> RemoveResult:
        if dry_run:
            return RemoveResult(ok=True, command=["<sdk>", "volume", "rm", name])
        self._sdk.volumes.get(name).remove(force=force)  # type: ignore[attr-defined]
        return RemoveResult(ok=True, command=["<sdk>", "volume", "rm", name])

    def remove_network(self, network_id: str, *, dry_run: bool) -> RemoveResult:
        if dry_run:
            return RemoveResult(ok=True, command=["<sdk>", "network", "rm", network_id])
        self._sdk.networks.get(network_id).remove()  # type: ignore[attr-defined]
        return RemoveResult(ok=True, command=["<sdk>", "network", "rm", network_id])

    def prune_build_cache(
        self, *, all_cache: bool = False, dry_run: bool, until: str | None = None
    ) -> PruneOutcome:
        if dry_run:
            return PruneOutcome(ok=True, command=["<sdk>", "builder", "prune"])
        self._sdk.api.prune_builds()  # type: ignore[attr-defined]
        return PruneOutcome(ok=True, command=["<sdk>", "builder", "prune"])

    def prune_networks(self, *, dry_run: bool) -> PruneOutcome:
        if dry_run:
            return PruneOutcome(ok=True, command=["<sdk>", "network", "prune"])
        self._sdk.networks.prune()  # type: ignore[attr-defined]
        return PruneOutcome(ok=True, command=["<sdk>", "network", "prune"])


def _try_build_sdk_client() -> ApiDockerClient | None:  # pragma: no cover - optional dep
    try:
        import docker
    except ImportError:
        return None
    try:
        sdk = docker.from_env()
        sdk.ping()
    except Exception:
        return None
    return ApiDockerClient(sdk)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_docker_client(
    config: ToolkitConfig,
    *,
    runner: CommandRunner | None = None,
) -> DockerClient:
    """Select a :class:`DockerClient` backend. Never raises.

    Preference order: CLI (if the binary is on PATH) → docker-py SDK (if the CLI
    is missing but the SDK can reach a daemon) → :class:`NullDockerClient`.
    Whether an unavailable daemon is fatal is decided per-command by the caller,
    based on ``client.probe()``.
    """
    runner = runner or default_runner()
    cli_path = config.docker.cli_path or "docker"

    if config.docker.prefer_cli and shutil.which(cli_path) is not None:
        return CliDockerClient(runner, config, cli_path=cli_path)

    if config.docker.api_fallback:
        sdk_client = _try_build_sdk_client()
        if sdk_client is not None:
            return sdk_client

    probe = DockerProbe(
        availability=DockerAvailability.NOT_INSTALLED,
        remediation=(
            f"'{cli_path}' was not found on PATH and no reachable docker-py SDK is "
            "available. Install Docker Engine or Docker Desktop. In WSL2, Docker "
            "usually lives on the Windows/Docker Desktop side or inside the distro."
        ),
    )
    return NullDockerClient(probe)
