"""Host system inspection — disks, inodes, platform, and Docker Desktop/WSL.

Uses :mod:`psutil` for cross-platform disk metrics and :mod:`os.statvfs` for
inode usage where available (POSIX only; Windows reports ``None``). Also locates
Docker Desktop's WSL2 VHDX files so the analyzer can flag "host disk is not
Docker's disk" and drive the compaction playbook.
"""

from __future__ import annotations

import os
import platform
import socket
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil

from .models import DiskUsage
from .utils import is_wsl, safe_percent

# ---------------------------------------------------------------------------
# Platform basics
# ---------------------------------------------------------------------------


def hostname() -> str:
    """Return the host name (best effort)."""
    try:
        return socket.gethostname() or platform.node() or "unknown"
    except OSError:  # pragma: no cover - platform dependent
        return platform.node() or "unknown"


def os_description() -> str:
    """Return a human-readable OS description."""
    detail = platform.platform(terse=True)
    if is_wsl():
        return f"{detail} (WSL2)"
    return detail


def is_windows() -> bool:
    """Return ``True`` on native Windows."""
    return platform.system() == "Windows"


def is_macos() -> bool:
    """Return ``True`` on macOS."""
    return platform.system() == "Darwin"


def is_linux() -> bool:
    """Return ``True`` on Linux (including WSL)."""
    return platform.system() == "Linux"


# ---------------------------------------------------------------------------
# Disk & inode collection
# ---------------------------------------------------------------------------


def get_inode_usage(mountpoint: str) -> tuple[int | None, int | None, int | None, float | None]:
    """Return ``(total, used, free, percent)`` inodes for a mountpoint.

    Windows has no inode concept; returns all ``None`` there and on error.
    """
    statvfs = getattr(os, "statvfs", None)
    if statvfs is None:
        return (None, None, None, None)
    try:
        stats = statvfs(mountpoint)
    except OSError:
        return (None, None, None, None)
    total = stats.f_files
    free = stats.f_ffree
    used = total - free
    percent = safe_percent(used, total) if total else 0.0
    return (total, used, free, percent)


def _make_disk_usage(
    mountpoint: str,
    *,
    filesystem: str | None = None,
    is_docker_root: bool = False,
) -> DiskUsage | None:
    """Build a :class:`DiskUsage` for a mountpoint, or ``None`` if inaccessible."""
    try:
        usage = psutil.disk_usage(mountpoint)
    except (OSError, PermissionError):
        return None
    inodes_total, inodes_used, inodes_free, inodes_percent = get_inode_usage(mountpoint)
    return DiskUsage(
        mountpoint=mountpoint,
        filesystem=filesystem,
        total_bytes=usage.total,
        used_bytes=usage.used,
        free_bytes=usage.free,
        percent_used=usage.percent,
        inodes_total=inodes_total,
        inodes_used=inodes_used,
        inodes_free=inodes_free,
        inodes_percent=inodes_percent,
        is_docker_root=is_docker_root,
    )


def _normalize(path: str | None) -> str | None:
    if not path:
        return None
    try:
        return str(Path(path).resolve())
    except (OSError, RuntimeError):
        return path


def _mountpoint_for(path: str, partitions: Sequence[Any]) -> str | None:
    """Return the deepest partition mountpoint containing ``path``."""
    target = _normalize(path)
    if target is None:
        return None
    best: str | None = None
    best_len = -1
    for part in partitions:
        mp = _normalize(part.mountpoint)
        if mp is None:
            continue
        try:
            common = os.path.commonpath([target, mp])
        except ValueError:
            continue
        if common == mp and len(mp) > best_len:
            best, best_len = part.mountpoint, len(mp)
    return best


def collect_disks(
    *,
    docker_root_dir: str | None = None,
    extra_paths: Iterable[str] = (),
) -> list[DiskUsage]:
    """Collect usage for all real filesystems, flagging the Docker-root disk.

    Args:
        docker_root_dir: Docker's ``DockerRootDir`` (from ``docker info``). The
            partition containing it is flagged ``is_docker_root``. On Docker
            Desktop this path lives inside the VM and won't match a host
            partition — that mismatch is the caveat the analyzer surfaces.
        extra_paths: Additional paths whose partitions should be included (e.g.
            the report dir or home).

    Returns:
        A de-duplicated list of :class:`DiskUsage`, one per mountpoint.
    """
    try:
        partitions = psutil.disk_partitions(all=False)
    except OSError:  # pragma: no cover - platform dependent
        partitions = []

    docker_mp = _mountpoint_for(docker_root_dir, partitions) if docker_root_dir else None

    disks: dict[str, DiskUsage] = {}
    fs_by_mp = {p.mountpoint: p.fstype for p in partitions}

    wanted_mps = [p.mountpoint for p in partitions]
    for extra in extra_paths:
        mp = _mountpoint_for(extra, partitions)
        if mp and mp not in wanted_mps:
            wanted_mps.append(mp)

    for mountpoint in wanted_mps:
        if mountpoint in disks:
            continue
        disk = _make_disk_usage(
            mountpoint,
            filesystem=fs_by_mp.get(mountpoint),
            is_docker_root=(mountpoint == docker_mp),
        )
        if disk is not None:
            disks[mountpoint] = disk
    return list(disks.values())


def disk_for_path(path: str) -> DiskUsage | None:
    """Return the :class:`DiskUsage` for the filesystem containing ``path``."""
    try:
        partitions = psutil.disk_partitions(all=False)
    except OSError:  # pragma: no cover
        partitions = []
    mp = _mountpoint_for(path, partitions) or path
    fs = next((p.fstype for p in partitions if p.mountpoint == mp), None)
    return _make_disk_usage(mp, filesystem=fs)


# ---------------------------------------------------------------------------
# Docker Desktop / WSL2 VHDX discovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VhdxFile:
    """A discovered WSL2/Docker Desktop virtual disk file."""

    path: str
    size_bytes: int
    label: str


def _localappdata_roots() -> list[Path]:
    """Candidate ``%LOCALAPPDATA%\\Docker`` roots (native Windows and via WSL)."""
    roots: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "Docker")
    if is_wsl():
        # Translate the Windows user profile to a /mnt/c path (best effort).
        user = os.environ.get("USER") or os.environ.get("LOGNAME")
        for base in (Path("/mnt/c/Users"), Path("/mnt/host/c/Users")):
            if user and (base / user).exists():
                roots.append(base / user / "AppData" / "Local" / "Docker")
    return roots


def find_vhdx_files(search_roots: Iterable[Path] | None = None) -> list[VhdxFile]:
    """Locate Docker Desktop / WSL2 ``*.vhdx`` files and their on-disk sizes.

    Args:
        search_roots: Directories to scan. Defaults to Docker Desktop's
            ``%LOCALAPPDATA%\\Docker`` locations (also resolved from inside WSL).

    Returns:
        Discovered VHDX files, largest first. Empty when none are found (the
        common case on Linux engines and this dev machine).
    """
    roots = list(search_roots) if search_roots is not None else _localappdata_roots()
    found: list[VhdxFile] = []
    seen: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        try:
            candidates = root.rglob("*.vhdx")
        except OSError:  # pragma: no cover - permission dependent
            continue
        for vhdx in candidates:
            try:
                resolved = str(vhdx.resolve())
                if resolved in seen:
                    continue
                size = vhdx.stat().st_size
            except OSError:
                continue
            seen.add(resolved)
            label = "docker-desktop-data" if "data" in vhdx.parts else vhdx.parent.name
            found.append(VhdxFile(path=resolved, size_bytes=size, label=label))
    found.sort(key=lambda v: v.size_bytes, reverse=True)
    return found
