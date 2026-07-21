"""Playbook: BuildKit / build-cache explosion."""

from __future__ import annotations

from ..models import Finding, HealthStatus
from ..utils import humanize_size, safe_percent
from .base import BasePlaybook, GeneratedScript, PlaybookContext

CACHE_WARN_BYTES = 5 * 1000**3
CACHE_CRITICAL_BYTES = 20 * 1000**3
CACHE_SHARE_WARN_PERCENT = 40.0


class BuildCachePlaybook(BasePlaybook):
    """Detect and remediate a bloated BuildKit build cache."""

    id = "buildkit-explosion"
    title = "BuildKit / build-cache explosion"

    def detect(self, ctx: PlaybookContext) -> Finding | None:
        cache = ctx.usage.build_cache
        total = cache.total_bytes
        share = safe_percent(total, ctx.usage.total_bytes)
        if total < CACHE_WARN_BYTES and share < CACHE_SHARE_WARN_PERCENT:
            return None
        severity = HealthStatus.CRITICAL if total >= CACHE_CRITICAL_BYTES else HealthStatus.WARNING
        return Finding(
            severity=severity,
            code=self.id,
            message=(
                f"Build cache is {humanize_size(total)} ({share:.0f}% of Docker usage); "
                f"{humanize_size(cache.reclaimable_bytes)} is reclaimable."
            ),
            detail={
                "build_cache_bytes": total,
                "reclaimable_bytes": cache.reclaimable_bytes,
                "share_percent": round(share, 1),
            },
            playbook=self.id,
            est_reclaimable_bytes=cache.reclaimable_bytes,
        )

    def render_script(self, ctx: PlaybookContext) -> GeneratedScript:
        steps = [
            "Preview reclaimable cache: docker builder du",
            "Prune cache older than 7 days: docker builder prune --filter until=168h -f",
            "Or prune everything not in use: docker builder prune -a -f",
            "Keep a size budget going forward: docker builder prune --keep-storage 10GB -f",
        ]
        content = """#!/usr/bin/env bash
# BuildKit build-cache cleanup — review before running.
set -euo pipefail

echo "Current build cache:"
docker builder du || true

echo "Pruning build cache older than 7 days (safe: keeps in-use cache)..."
docker builder prune --filter until=168h -f

# Aggressive alternative (removes ALL cache not currently in use):
#   docker builder prune -a -f

# Cap future growth by keeping at most 10GB of cache:
#   docker builder prune --keep-storage 10GB -f

echo "Done. Re-run: docker-disk analyze"
"""
        return GeneratedScript(
            playbook_id=self.id,
            title=self.title,
            shell="bash",
            filename="recover_build_cache.sh",
            steps=steps,
            content=content,
            est_reclaim_bytes=ctx.usage.build_cache.reclaimable_bytes,
            danger="Pruning -a removes all cache not in use; next build will be slower.",
        )
