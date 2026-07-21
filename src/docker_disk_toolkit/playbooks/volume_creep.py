"""Playbook: named-volume creep (databases, Nextcloud, TrueNAS mounts).

These volumes hold real data, so this playbook never proposes deletion — it
surfaces the biggest/growing stateful volumes and offers backup-first guidance.
"""

from __future__ import annotations

import re

from ..models import Finding, HealthStatus, VolumeInfo
from ..utils import humanize_size
from .base import BasePlaybook, GeneratedScript, PlaybookContext

STATEFUL_PATTERN = re.compile(
    r"(?i)(postgres|mysql|mariadb|mongo|redis|nextcloud|grafana|prometheus|influx|"
    r"vault|elastic|opensearch|minio|paperless|immich|truenas|pgdata|db[_-]?data)"
)
STATEFUL_WARN_BYTES = 2 * 1000**3
ANY_VOLUME_WARN_BYTES = 5 * 1000**3


def _is_creep(volume: VolumeInfo) -> bool:
    size = volume.size_bytes or 0
    if STATEFUL_PATTERN.search(volume.name) and size >= STATEFUL_WARN_BYTES:
        return True
    return size >= ANY_VOLUME_WARN_BYTES


class VolumeCreepPlaybook(BasePlaybook):
    """Surface large stateful volumes with backup-first guidance."""

    id = "volume-creep"
    title = "Named-volume creep (databases, Nextcloud, TrueNAS)"

    def _candidates(self, ctx: PlaybookContext) -> list[VolumeInfo]:
        return sorted(
            (v for v in ctx.usage.volume_list if _is_creep(v)),
            key=lambda v: v.size_bytes or 0,
            reverse=True,
        )

    def detect(self, ctx: PlaybookContext) -> Finding | None:
        candidates = self._candidates(ctx)
        if not candidates:
            return None
        total = sum(v.size_bytes or 0 for v in candidates)
        return Finding(
            severity=HealthStatus.WARNING,
            code=self.id,
            message=(
                f"{len(candidates)} large/stateful volume(s) hold {humanize_size(total)}. "
                "These likely contain real data — back up before touching them."
            ),
            detail={
                "volumes": [
                    {
                        "name": v.name,
                        "size_bytes": v.size_bytes,
                        "in_use": v.in_use,
                        "stateful": bool(STATEFUL_PATTERN.search(v.name)),
                    }
                    for v in candidates
                ],
            },
            playbook=self.id,
            est_reclaimable_bytes=None,  # never proposed for deletion
        )

    def render_script(self, ctx: PlaybookContext) -> GeneratedScript:
        candidates = self._candidates(ctx)
        backup_lines = (
            "\n".join(
                f"# {v.name}  ({humanize_size(v.size_bytes)})\n"
                f'docker run --rm -v {v.name}:/data -v "$PWD":/backup alpine '
                f"tar czf /backup/{v.name}.tar.gz -C /data ."
                for v in candidates
            )
            or "# (no large stateful volumes detected)"
        )
        steps = [
            "Identify which app owns each volume (do NOT delete blindly).",
            "Back up each volume to a tarball before any change.",
            "Reclaim inside the app instead (e.g. VACUUM for Postgres, trash for Nextcloud).",
            "Only remove a volume once you have a verified backup and it is truly unused.",
        ]
        content = f"""#!/usr/bin/env bash
# Named-volume creep — BACKUP FIRST. This script only backs up; it never deletes.
set -euo pipefail

{backup_lines}

# App-side reclaim examples (preferred over deleting the volume):
#   Postgres:  docker exec <pg> psql -c 'VACUUM FULL;'
#   Nextcloud: docker exec <nc> php occ trashbin:cleanup --all-users
#   MySQL:     OPTIMIZE TABLE ...;

echo "Backups complete. Verify them before removing anything."
"""
        return GeneratedScript(
            playbook_id=self.id,
            title=self.title,
            shell="bash",
            filename="recover_volume_creep.sh",
            steps=steps,
            content=content,
            est_reclaim_bytes=None,
            danger="These volumes hold real data. Never delete without a verified backup.",
        )
