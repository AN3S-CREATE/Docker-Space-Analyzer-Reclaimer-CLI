"""Diagnostics engine — assemble the snapshot, correlate, and assess health.

:func:`analyze` builds a :class:`~docker_disk_toolkit.models.DiagnosticReport`
from the host (:mod:`system_info`) and Docker (:mod:`docker_client`), correlates
in-use objects, runs the recovery-playbook detectors, and grades overall health.
It tolerates a missing daemon: the report is still produced with ``docker=None``
and a ``DOCKER_UNAVAILABLE`` finding.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from . import system_info
from .config import Thresholds, ToolkitConfig
from .context import RunContext
from .docker_client import DESKTOP_BACKENDS, DockerClient
from .models import (
    DiagnosticReport,
    DiskUsage,
    DockerBackend,
    DockerInfo,
    DockerProbe,
    DockerUsage,
    Finding,
    HealthStatus,
    Recommendation,
)
from .playbooks import PlaybookContext, detect_all
from .utils import ProtectMatcher, humanize_size, safe_percent

GB = 1000**3
_SEVERITY_ORDER = {
    HealthStatus.CRITICAL: 3,
    HealthStatus.WARNING: 2,
    HealthStatus.HEALTHY: 1,
    HealthStatus.UNKNOWN: 0,
}
_VHDX_BACKENDS = DESKTOP_BACKENDS | {DockerBackend.WSL}


def worst(*statuses: HealthStatus) -> HealthStatus:
    """Return the most severe of the given health statuses."""
    return max(statuses, key=lambda s: _SEVERITY_ORDER[s], default=HealthStatus.UNKNOWN)


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------


def correlate_usage(usage: DockerUsage, protect_matcher: ProtectMatcher) -> None:
    """Mark images/volumes as in-use (from containers) and volumes as protected.

    Mutates ``usage`` in place.
    """
    image_by_tag: dict[str, str] = {}
    for image in usage.image_list:
        for tag in image.repo_tags:
            image_by_tag[tag] = image.id
        image.used_by_containers = 0
        image.in_use = False

    images_by_id = {img.id: img for img in usage.image_list}
    mount_names: set[str] = set()

    for container in usage.container_list:
        mount_names.update(container.mounts)
        ref = container.image
        image_id = image_by_tag.get(ref)
        if image_id is None:
            # Match by (short) id prefix.
            for img in usage.image_list:
                bare = img.id.split(":", 1)[-1]
                if ref and (bare.startswith(ref.split(":", 1)[-1]) or ref in img.id):
                    image_id = img.id
                    break
        if image_id and image_id in images_by_id:
            container.image_id = image_id
            img = images_by_id[image_id]
            img.used_by_containers += 1
            img.in_use = True

    for volume in usage.volume_list:
        volume.in_use = volume.name in mount_names or volume.links > 0
        if protect_matcher.matches(volume.name):
            volume.protected = True
            volume.protect_reason = "matches protect-list"


# ---------------------------------------------------------------------------
# Health & findings
# ---------------------------------------------------------------------------


def assess_disk_health(disk: DiskUsage, thresholds: Thresholds) -> HealthStatus:
    """Grade a single disk against the free-space thresholds."""
    free_gb = disk.free_bytes / GB
    free_percent = 100.0 - disk.percent_used
    if free_gb < thresholds.critical_free_gb:
        return HealthStatus.CRITICAL
    if free_gb < thresholds.min_free_gb or free_percent < thresholds.min_free_percent:
        return HealthStatus.WARNING
    return HealthStatus.HEALTHY


def disk_findings(disks: list[DiskUsage], thresholds: Thresholds) -> list[Finding]:
    """Emit findings for disks that breach free-space or inode thresholds."""
    findings: list[Finding] = []
    for disk in disks:
        status = assess_disk_health(disk, thresholds)
        if status is HealthStatus.CRITICAL:
            findings.append(
                Finding(
                    severity=HealthStatus.CRITICAL,
                    code="DISK_CRITICAL_FREE",
                    message=(
                        f"{disk.mountpoint} has only {humanize_size(disk.free_bytes)} free "
                        f"(< {thresholds.critical_free_gb:g} GB)."
                    ),
                    detail={"mountpoint": disk.mountpoint, "free_bytes": disk.free_bytes},
                )
            )
        elif status is HealthStatus.WARNING:
            findings.append(
                Finding(
                    severity=HealthStatus.WARNING,
                    code="DISK_LOW_FREE",
                    message=(
                        f"{disk.mountpoint} is low on space: {humanize_size(disk.free_bytes)} free "
                        f"({100 - disk.percent_used:.0f}%)."
                    ),
                    detail={"mountpoint": disk.mountpoint, "free_bytes": disk.free_bytes},
                )
            )
        if disk.inodes_percent is not None and disk.inodes_percent > 90.0:
            findings.append(
                Finding(
                    severity=HealthStatus.WARNING,
                    code="DISK_INODES_LOW",
                    message=f"{disk.mountpoint} inode usage is {disk.inodes_percent:.0f}%.",
                    detail={"mountpoint": disk.mountpoint, "inodes_percent": disk.inodes_percent},
                )
            )
    return findings


def docker_usage_findings(
    usage: DockerUsage,
    primary_disk: DiskUsage | None,
    thresholds: Thresholds,
    *,
    caveat: bool = False,
) -> list[Finding]:
    """Emit a finding when Docker consumes too much of its host disk."""
    if primary_disk is None or primary_disk.total_bytes <= 0:
        return []
    docker_percent = safe_percent(usage.total_bytes, primary_disk.total_bytes)
    if docker_percent <= thresholds.max_docker_percent:
        return []
    message = (
        f"Docker is using {humanize_size(usage.total_bytes)} — {docker_percent:.0f}% of "
        f"{primary_disk.mountpoint} (limit {thresholds.max_docker_percent:g}%)."
    )
    if caveat:
        message += " Note: Docker data lives in a WSL2 VHDX, so this ratio is approximate."
    return [
        Finding(
            severity=HealthStatus.WARNING,
            code="DOCKER_HIGH_USAGE",
            message=message,
            detail={
                "docker_bytes": usage.total_bytes,
                "disk_total_bytes": primary_disk.total_bytes,
                "docker_percent": round(docker_percent, 1),
            },
        )
    ]


def build_recommendations(usage: DockerUsage) -> list[Recommendation]:
    """Derive concrete cleanup recommendations from the usage snapshot."""
    recs: list[Recommendation] = []
    dangling = [i for i in usage.image_list if i.dangling and not i.in_use]
    if dangling:
        recs.append(
            Recommendation(
                action="Prune dangling images",
                reclaimable_bytes=sum(i.reclaim_bytes for i in dangling),
                command_hint="docker-disk cleanup --level 1",
            )
        )
    stopped = [c for c in usage.container_list if not c.running]
    if stopped:
        recs.append(
            Recommendation(
                action="Remove stopped containers",
                reclaimable_bytes=sum(c.reclaim_bytes for c in stopped),
                command_hint="docker-disk cleanup --level 1",
            )
        )
    if usage.build_cache.reclaimable_bytes > 0:
        recs.append(
            Recommendation(
                action="Prune build cache",
                reclaimable_bytes=usage.build_cache.reclaimable_bytes,
                command_hint="docker-disk cleanup --level 1",
            )
        )
    unused_volumes = [v for v in usage.volume_list if not v.in_use and not v.protected]
    if unused_volumes:
        recs.append(
            Recommendation(
                action="Remove unused (unprotected) volumes",
                reclaimable_bytes=sum(v.reclaim_bytes for v in unused_volumes),
                command_hint="docker-disk cleanup --level 2",
                protected_excluded=sum(
                    1 for v in usage.volume_list if not v.in_use and v.protected
                ),
            )
        )
    return recs


# ---------------------------------------------------------------------------
# Forensic mode — "what just ate my disk?"
# ---------------------------------------------------------------------------


def forensic_suspects(
    usage: DockerUsage, since: timedelta, now: datetime
) -> list[dict[str, object]]:
    """Rank Docker objects created/used within ``since`` by size (largest first)."""
    cutoff = now - since
    suspects: list[dict[str, object]] = []
    for img in usage.image_list:
        if img.created_at and img.created_at >= cutoff:
            suspects.append(
                {
                    "kind": "image",
                    "name": img.display_name,
                    "size_bytes": img.size_bytes,
                    "when": img.created_at,
                }
            )
    for bc in usage.build_cache_list:
        when = bc.last_used_at
        if when and when >= cutoff:
            suspects.append(
                {"kind": "build-cache", "name": bc.id, "size_bytes": bc.size_bytes, "when": when}
            )
    for c in usage.container_list:
        if c.created_at and c.created_at >= cutoff:
            suspects.append(
                {
                    "kind": "container",
                    "name": c.name,
                    "size_bytes": c.reclaim_bytes,
                    "when": c.created_at,
                }
            )
    suspects.sort(key=lambda s: int(s["size_bytes"] or 0), reverse=True)  # type: ignore[call-overload]
    return suspects


def forensic_finding(suspects: list[dict[str, object]], since: timedelta) -> Finding | None:
    """Summarise the top forensic suspects as a finding."""
    if not suspects:
        return None
    top = suspects[:5]
    total = sum(int(s["size_bytes"] or 0) for s in top)  # type: ignore[call-overload]
    hours = int(since.total_seconds() // 3600)
    names = ", ".join(
        f"{s['name']} ({humanize_size(int(s['size_bytes'] or 0))})"  # type: ignore[call-overload]
        for s in top
    )
    return Finding(
        severity=HealthStatus.WARNING,
        code="FORENSIC_RECENT_GROWTH",
        message=(
            f"Top disk consumers created in the last {hours}h "
            f"({humanize_size(total)}): {names}."
        ),
        detail={"suspects": top},
        playbook=None,
    )


# ---------------------------------------------------------------------------
# Top-level analyze
# ---------------------------------------------------------------------------


def _docker_unavailable_finding(probe: DockerProbe) -> Finding:
    return Finding(
        severity=HealthStatus.WARNING,
        code="DOCKER_UNAVAILABLE",
        message=f"Docker is not usable ({probe.availability.value}).",
        detail={"availability": probe.availability.value, "remediation": probe.remediation},
    )


def _vhdx_caveat_finding(disk: DiskUsage | None) -> Finding:
    mp = disk.mountpoint if disk else "the host disk"
    return Finding(
        severity=HealthStatus.HEALTHY,
        code="DESKTOP_VHDX_CAVEAT",
        message=(
            "Docker Desktop stores data in a WSL2 VHDX; host free space on "
            f"{mp} is not the same as space available inside Docker."
        ),
        detail={},
    )


def analyze(
    ctx: RunContext,
    *,
    forensic_since: timedelta | None = None,
) -> DiagnosticReport:
    """Produce a full :class:`DiagnosticReport`.

    Args:
        ctx: The run context (config, docker client, correlation id, clock).
        forensic_since: When set, include "what just ate my disk?" suspects for
            objects created within this window.

    Returns:
        A populated :class:`DiagnosticReport` (never raises for a missing daemon).
    """
    config: ToolkitConfig = ctx.config
    client: DockerClient = ctx.docker
    probe = client.probe()
    now = ctx.now

    hostname = system_info.hostname()
    os_desc = system_info.os_description()
    findings: list[Finding] = []

    if not probe.ok:
        disks = system_info.collect_disks(extra_paths=[str(config.report_dir)])
        findings.append(_docker_unavailable_finding(probe))
        findings.extend(disk_findings(disks, config.thresholds))
        health = worst(*(assess_disk_health(d, config.thresholds) for d in disks))
        return DiagnosticReport(
            correlation_id=ctx.correlation_id,
            generated_at=now,
            hostname=hostname,
            os=os_desc,
            docker_probe=probe,
            docker_info=None,
            disks=disks,
            docker=None,
            health=health if disks else HealthStatus.UNKNOWN,
            findings=findings,
            recommendations=[],
            reclaimable_total_bytes=0,
        )

    info: DockerInfo = client.info()
    usage = client.collect_usage()
    correlate_usage(usage, config.protect_matcher())

    disks = system_info.collect_disks(
        docker_root_dir=info.docker_root_dir,
        extra_paths=[str(config.report_dir)],
    )
    primary = None
    for disk in disks:
        if disk.is_docker_root:
            primary = disk
            break
    if primary is None and disks:
        primary = disks[0]

    is_desktop = probe.backend in _VHDX_BACKENDS
    vhdx_files = system_info.find_vhdx_files() if is_desktop else []
    if is_desktop and primary is not None:
        primary.backend_caveat = "Docker data is stored in a WSL2 VHDX on this disk."

    # Findings: thresholds + docker usage + playbooks.
    findings.extend(disk_findings(disks, config.thresholds))
    findings.extend(docker_usage_findings(usage, primary, config.thresholds, caveat=is_desktop))
    if is_desktop and vhdx_files:
        findings.append(_vhdx_caveat_finding(primary))

    playbook_ctx = PlaybookContext(
        usage=usage,
        disks=disks,
        backend=probe.backend,
        storage_driver=info.storage_driver,
        docker_root_dir=info.docker_root_dir,
        vhdx_files=vhdx_files,
        host_is_windows=system_info.is_windows(),
        now=now,
    )
    playbook_findings = detect_all(playbook_ctx)
    findings.extend(playbook_findings)

    if forensic_since is not None:
        suspects = forensic_suspects(usage, forensic_since, now)
        forensic = forensic_finding(suspects, forensic_since)
        if forensic is not None:
            findings.append(forensic)

    recommendations = build_recommendations(usage)

    health = worst(
        *(assess_disk_health(d, config.thresholds) for d in disks),
        *(f.severity for f in findings),
    )

    return DiagnosticReport(
        correlation_id=ctx.correlation_id,
        generated_at=now,
        hostname=hostname,
        os=os_desc,
        docker_probe=probe,
        docker_info=info,
        disks=disks,
        docker=usage,
        health=health,
        findings=findings,
        recommendations=recommendations,
        reclaimable_total_bytes=usage.reclaimable_bytes,
    )


def assess_health(report: DiagnosticReport, thresholds: Thresholds) -> HealthStatus:
    """Re-grade a report's health (used for before/after comparisons)."""
    statuses = [assess_disk_health(d, thresholds) for d in report.disks]
    statuses.extend(f.severity for f in report.findings)
    return worst(*statuses) if statuses else HealthStatus.UNKNOWN
