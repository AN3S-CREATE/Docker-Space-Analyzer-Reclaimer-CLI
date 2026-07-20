"""Pydantic v2 data models — the shared vocabulary of the toolkit.

All sizes are stored as **integer bytes**; human strings are parsed at the
Docker-client boundary (see :mod:`docker_disk_toolkit.utils`) so nothing
downstream re-parses. Models serialise cleanly to JSON via
``model_dump_json`` (bytes stay integers — the terminal/Markdown layers
humanise, never the JSON).
"""

from __future__ import annotations

from datetime import datetime
from enum import IntEnum, StrEnum

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1


class _Model(BaseModel):
    """Base model with strict field handling for constructed report objects."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class HealthStatus(StrEnum):
    """Overall or per-finding health severity."""

    HEALTHY = "healthy"
    WARNING = "warning"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


class DockerAvailability(StrEnum):
    """Structured result of probing for a usable Docker daemon."""

    OK = "ok"
    NOT_INSTALLED = "not_installed"
    DAEMON_UNREACHABLE = "daemon_unreachable"
    PERMISSION_DENIED = "permission_denied"
    VERSION_UNSUPPORTED = "version_unsupported"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


class DockerBackend(StrEnum):
    """Best-effort classification of the Docker backend/flavor."""

    DESKTOP_WINDOWS = "desktop-windows"
    DESKTOP_MAC = "desktop-mac"
    DESKTOP_LINUX = "desktop-linux"
    ENGINE = "engine"
    ROOTLESS = "rootless"
    WSL = "wsl"
    UNKNOWN = "unknown"


class ObjectKind(StrEnum):
    """A prunable Docker object kind."""

    IMAGE = "image"
    CONTAINER = "container"
    VOLUME = "volume"
    NETWORK = "network"
    BUILD_CACHE = "build-cache"


# ---------------------------------------------------------------------------
# Probe / info
# ---------------------------------------------------------------------------


class DockerProbe(_Model):
    """Structured availability result — never a bare exception for expected states."""

    availability: DockerAvailability
    client_version: str | None = None
    server_version: str | None = None
    api_version: str | None = None
    context_name: str | None = None
    backend: DockerBackend = DockerBackend.UNKNOWN
    remediation: str = ""
    raw_stderr: str | None = None

    @property
    def ok(self) -> bool:
        """Return ``True`` when a usable daemon was reached."""
        return self.availability is DockerAvailability.OK


class DockerInfo(_Model):
    """Selected fields from ``docker info``."""

    docker_root_dir: str | None = None
    storage_driver: str | None = None
    os_type: str | None = None
    operating_system: str | None = None
    server_version: str | None = None
    rootless: bool = False
    name: str | None = None


# ---------------------------------------------------------------------------
# Host disk
# ---------------------------------------------------------------------------


class DiskUsage(_Model):
    """Host filesystem usage for a single mountpoint (from psutil)."""

    mountpoint: str
    filesystem: str | None = None
    total_bytes: int
    used_bytes: int
    free_bytes: int
    percent_used: float
    inodes_total: int | None = None
    inodes_used: int | None = None
    inodes_free: int | None = None
    inodes_percent: float | None = None
    is_docker_root: bool = False
    backend_caveat: str | None = None


# ---------------------------------------------------------------------------
# Docker objects
# ---------------------------------------------------------------------------


class ImageInfo(_Model):
    """A Docker image with size + in-use correlation."""

    id: str
    repo_tags: list[str] = Field(default_factory=list)
    repo_digests: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    size_bytes: int = 0
    shared_size_bytes: int | None = None
    unique_size_bytes: int | None = None
    used_by_containers: int = 0
    in_use: bool = False
    dangling: bool = False
    labels: dict[str, str] = Field(default_factory=dict)

    @property
    def display_name(self) -> str:
        """Human-friendly identifier (first tag, else short id)."""
        if self.repo_tags:
            return self.repo_tags[0]
        short = self.id.split(":", 1)[-1][:12]
        return f"<none>@{short}"

    @property
    def reclaim_bytes(self) -> int:
        """Bytes actually freed if this image is removed (prefers unique size)."""
        if self.unique_size_bytes is not None:
            return self.unique_size_bytes
        return self.size_bytes


class ContainerInfo(_Model):
    """A Docker container (running or stopped)."""

    id: str
    name: str
    image: str
    image_id: str | None = None
    state: str = "unknown"
    running: bool = False
    size_rw_bytes: int | None = None
    size_root_fs_bytes: int | None = None
    created_at: datetime | None = None
    finished_at: datetime | None = None
    mounts: list[str] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)

    @property
    def reclaim_bytes(self) -> int:
        """Writable-layer bytes freed if this container is removed."""
        return self.size_rw_bytes or 0


class VolumeInfo(_Model):
    """A Docker volume. ``size_bytes`` is ``None`` when the driver reports N/A."""

    name: str
    driver: str = "local"
    mountpoint: str | None = None
    size_bytes: int | None = None
    links: int = 0
    in_use: bool = False
    protected: bool = False
    protect_reason: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    created_at: datetime | None = None

    @property
    def reclaim_bytes(self) -> int:
        """Bytes freed if this volume is removed (0 when size unknown)."""
        return self.size_bytes or 0


class BuildCacheInfo(_Model):
    """A single BuildKit build-cache record."""

    id: str
    cache_type: str | None = None
    size_bytes: int = 0
    in_use: bool = False
    shared: bool = False
    last_used_at: datetime | None = None
    usage_count: int = 0
    description: str | None = None


class NetworkInfo(_Model):
    """A Docker network (used for unused-network detection)."""

    id: str
    name: str
    driver: str = "bridge"
    scope: str = "local"
    created_at: datetime | None = None
    builtin: bool = False


# ---------------------------------------------------------------------------
# Aggregate usage
# ---------------------------------------------------------------------------


class CategoryUsage(_Model):
    """Summary totals for one ``docker system df`` category."""

    total_bytes: int = 0
    reclaimable_bytes: int = 0
    reclaimable_percent: float | None = None
    active: int = 0
    total_count: int = 0


class DockerUsage(_Model):
    """Complete Docker disk-usage snapshot (summary totals + per-object lists)."""

    images: CategoryUsage = Field(default_factory=CategoryUsage)
    containers: CategoryUsage = Field(default_factory=CategoryUsage)
    volumes: CategoryUsage = Field(default_factory=CategoryUsage)
    build_cache: CategoryUsage = Field(default_factory=CategoryUsage)

    image_list: list[ImageInfo] = Field(default_factory=list)
    container_list: list[ContainerInfo] = Field(default_factory=list)
    volume_list: list[VolumeInfo] = Field(default_factory=list)
    build_cache_list: list[BuildCacheInfo] = Field(default_factory=list)
    network_list: list[NetworkInfo] = Field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        """Total Docker-managed bytes across all categories."""
        return (
            self.images.total_bytes
            + self.containers.total_bytes
            + self.volumes.total_bytes
            + self.build_cache.total_bytes
        )

    @property
    def reclaimable_bytes(self) -> int:
        """Total reclaimable bytes Docker reports across all categories."""
        return (
            self.images.reclaimable_bytes
            + self.containers.reclaimable_bytes
            + self.volumes.reclaimable_bytes
            + self.build_cache.reclaimable_bytes
        )


# ---------------------------------------------------------------------------
# Findings, recommendations, report
# ---------------------------------------------------------------------------


class Finding(_Model):
    """A detected condition (threshold breach, bloat pattern, playbook trigger)."""

    severity: HealthStatus
    code: str
    message: str
    detail: dict[str, object] = Field(default_factory=dict)
    playbook: str | None = None
    est_reclaimable_bytes: int | None = None


class Recommendation(_Model):
    """A suggested next action with its projected reclaim."""

    action: str
    reclaimable_bytes: int
    command_hint: str
    protected_excluded: int = 0


class DiagnosticReport(_Model):
    """The top-level diagnostic snapshot produced by ``analyze``."""

    schema_version: int = SCHEMA_VERSION
    correlation_id: str
    generated_at: datetime
    hostname: str
    os: str
    docker_probe: DockerProbe
    docker_info: DockerInfo | None = None
    disks: list[DiskUsage] = Field(default_factory=list)
    docker: DockerUsage | None = None
    health: HealthStatus = HealthStatus.UNKNOWN
    findings: list[Finding] = Field(default_factory=list)
    recommendations: list[Recommendation] = Field(default_factory=list)
    reclaimable_total_bytes: int = 0

    @property
    def docker_root_disk(self) -> DiskUsage | None:
        """Return the disk hosting Docker's root dir, if identified."""
        for disk in self.disks:
            if disk.is_docker_root:
                return disk
        return self.disks[0] if self.disks else None


# ---------------------------------------------------------------------------
# Cleanup / audit / delta
# ---------------------------------------------------------------------------


class PruneLevel(IntEnum):
    """Cleanup aggressiveness levels."""

    REPORT = 0
    SAFE = 1
    AGGRESSIVE = 2
    NUCLEAR = 3


class Removable(_Model):
    """A candidate object selected (or spared) by a cleanup plan."""

    kind: ObjectKind
    id: str
    name: str
    reclaim_bytes: int = 0
    in_use: bool = False
    reasons: list[str] = Field(default_factory=list)
    spared_reason: str | None = None


class CleanupPlan(_Model):
    """A fully-resolved, reviewable cleanup plan for one level."""

    level: PruneLevel
    items: list[Removable] = Field(default_factory=list)
    protected: list[Removable] = Field(default_factory=list)
    total_reclaim_bytes: int = 0
    docker_reported_reclaim_bytes: int = 0
    commands: list[list[str]] = Field(default_factory=list)
    requires_confirmation: bool = False
    confirmation_mode: str = "none"  # none | single | type-to-confirm


class ConfirmationRecord(_Model):
    """How a destructive run was authorised (for the audit trail)."""

    mode: str = "none"
    prompt: str | None = None
    response: str | None = None
    tty: bool = False
    assume_yes: bool = False
    forced: bool = False


class AuditEvent(_Model):
    """One JSONL line per deletion attempt (including dry-run predictions)."""

    ts: datetime
    run_id: str
    level: PruneLevel
    dry_run: bool
    object_kind: ObjectKind
    object_id: str
    object_name: str
    reclaim_predicted_bytes: int = 0
    reclaim_actual_bytes: int | None = None
    command: list[str] = Field(default_factory=list)
    confirmation: ConfirmationRecord = Field(default_factory=ConfirmationRecord)
    outcome: str = "dry-run"  # removed | skipped-protected | dry-run | error
    error: str | None = None
    actor: str = "unknown"
    docker_backend: DockerBackend = DockerBackend.UNKNOWN


class SpaceDelta(_Model):
    """Before/after disk + Docker deltas around a cleanup."""

    host_free_before_bytes: int = 0
    host_free_after_bytes: int = 0
    docker_total_before_bytes: int = 0
    docker_total_after_bytes: int = 0
    reclaimed_bytes: int = 0

    @property
    def host_freed_bytes(self) -> int:
        """Increase in host free space (may be negative if other writers ran)."""
        return self.host_free_after_bytes - self.host_free_before_bytes


class CleanupResult(_Model):
    """The full outcome of a cleanup run (for reporting + exit-code mapping)."""

    correlation_id: str
    level: PruneLevel
    dry_run: bool
    plan: CleanupPlan
    audit_events: list[AuditEvent] = Field(default_factory=list)
    delta: SpaceDelta | None = None
    errors: list[str] = Field(default_factory=list)
    pre_health: HealthStatus = HealthStatus.UNKNOWN
    post_health: HealthStatus = HealthStatus.UNKNOWN

    @property
    def reclaimed_bytes(self) -> int:
        """Total bytes actually reclaimed (0 for dry runs)."""
        if self.dry_run:
            return 0
        return sum(
            e.reclaim_actual_bytes or e.reclaim_predicted_bytes
            for e in self.audit_events
            if e.outcome == "removed"
        )


# ---------------------------------------------------------------------------
# Monitor / history
# ---------------------------------------------------------------------------


class Breach(_Model):
    """A single threshold breach detected by the watchdog."""

    metric: str
    level: HealthStatus
    value: float
    threshold: float
    message: str


class MonitorReading(_Model):
    """One watchdog evaluation (appended to the metrics file)."""

    ts: datetime
    correlation_id: str
    metrics: dict[str, float] = Field(default_factory=dict)
    breaches: list[Breach] = Field(default_factory=list)
    health: HealthStatus = HealthStatus.UNKNOWN


class TrendStats(_Model):
    """Aggregated history over a time window (from the SQLite index)."""

    window_days: int
    runs: int = 0
    cleanups: int = 0
    total_freed_bytes: int = 0
    avg_freed_bytes: int = 0
    median_freed_bytes: int = 0
    largest_single_reclaim_bytes: int = 0
    freed_by_kind: dict[str, int] = Field(default_factory=dict)
    series: list[tuple[str, int]] = Field(default_factory=list)
