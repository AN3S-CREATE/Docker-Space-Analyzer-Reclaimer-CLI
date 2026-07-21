"""Playbook: Linux overlay2 storage-driver bloat."""

from __future__ import annotations

from ..models import DockerBackend, Finding, HealthStatus
from ..utils import humanize_size, safe_percent
from .base import BasePlaybook, GeneratedScript, PlaybookContext

RECLAIMABLE_WARN_BYTES = 10 * 1000**3
RECLAIMABLE_SHARE_PERCENT = 50.0

_LINUX_ENGINE = {DockerBackend.ENGINE, DockerBackend.ROOTLESS}


class Overlay2BloatPlaybook(BasePlaybook):
    """Detect a bloated overlay2 tree on a native Linux engine."""

    id = "overlay2-bloat"
    title = "Linux overlay2 storage bloat"

    def detect(self, ctx: PlaybookContext) -> Finding | None:
        if ctx.backend not in _LINUX_ENGINE:
            return None
        if (ctx.storage_driver or "").lower() != "overlay2":
            return None
        reclaimable = ctx.usage.reclaimable_bytes
        share = safe_percent(reclaimable, ctx.usage.total_bytes)
        if reclaimable < RECLAIMABLE_WARN_BYTES and share < RECLAIMABLE_SHARE_PERCENT:
            return None
        return Finding(
            severity=HealthStatus.WARNING,
            code=self.id,
            message=(
                f"overlay2 holds {humanize_size(ctx.usage.total_bytes)} with "
                f"{humanize_size(reclaimable)} ({share:.0f}%) reclaimable across images, "
                "containers, and cache."
            ),
            detail={
                "docker_root_dir": ctx.docker_root_dir,
                "total_bytes": ctx.usage.total_bytes,
                "reclaimable_bytes": reclaimable,
            },
            playbook=self.id,
            est_reclaimable_bytes=reclaimable,
        )

    def render_script(self, ctx: PlaybookContext) -> GeneratedScript:
        root = ctx.docker_root_dir or "/var/lib/docker"
        steps = [
            "Review reclaimable space: docker system df",
            "Safe prune first: docker-disk cleanup --level 1",
            "Then remove all unused images: docker system prune -a",
            f"Inspect the overlay2 tree if still large: sudo du -sh {root}/overlay2",
        ]
        content = f"""#!/usr/bin/env bash
# overlay2 bloat cleanup — review before running.
set -euo pipefail

echo "Reclaimable summary:"
docker system df

echo "Safe prune (dangling images, stopped containers, networks, build cache)..."
docker-disk cleanup --level 1 --yes || docker system prune -f

# Aggressive: also remove ALL images not used by a container:
#   docker system prune -a

# Diagnose remaining usage directly on disk:
#   sudo du -sh {root}/overlay2
#   sudo du -h --max-depth=1 {root} | sort -h

echo "Done. Re-run: docker-disk analyze"
"""
        return GeneratedScript(
            playbook_id=self.id,
            title=self.title,
            shell="bash",
            filename="recover_overlay2.sh",
            steps=steps,
            content=content,
            requires_elevation=True,
            est_reclaim_bytes=ctx.usage.reclaimable_bytes,
            danger="system prune -a removes all images not used by a container.",
        )
