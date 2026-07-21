"""Recovery-playbook registry and orchestration.

Playbooks each detect a specific disk-bloat situation from the diagnostic
snapshot and can render a tailored, review-before-run recovery script.
"""

from __future__ import annotations

from ..models import Finding
from .ai_ml import AiMlBloatPlaybook
from .base import BasePlaybook, GeneratedScript, PlaybookContext
from .buildkit import BuildCachePlaybook
from .overlay2 import Overlay2BloatPlaybook
from .vhdx import VhdxBloatPlaybook
from .volume_creep import VolumeCreepPlaybook

__all__ = [
    "ALL_PLAYBOOKS",
    "BasePlaybook",
    "GeneratedScript",
    "PlaybookContext",
    "detect_all",
    "get_playbook",
    "render_scripts",
]

ALL_PLAYBOOKS: list[BasePlaybook] = [
    VhdxBloatPlaybook(),
    BuildCachePlaybook(),
    AiMlBloatPlaybook(),
    VolumeCreepPlaybook(),
    Overlay2BloatPlaybook(),
]

_BY_ID = {pb.id: pb for pb in ALL_PLAYBOOKS}


def get_playbook(playbook_id: str) -> BasePlaybook | None:
    """Return the playbook with the given id, or ``None``."""
    return _BY_ID.get(playbook_id)


def detect_all(ctx: PlaybookContext) -> list[Finding]:
    """Run every playbook detector, returning matched findings (severity-sorted)."""
    order = {"critical": 0, "warning": 1, "healthy": 2, "unknown": 3}
    findings = [f for pb in ALL_PLAYBOOKS if (f := pb.detect(ctx)) is not None]
    findings.sort(key=lambda f: order.get(f.severity.value, 9))
    return findings


def render_scripts(ctx: PlaybookContext, *, only: list[str] | None = None) -> list[GeneratedScript]:
    """Render recovery scripts for every playbook whose situation is present.

    Args:
        ctx: The diagnostic context.
        only: Optional list of playbook ids to restrict rendering to.

    Returns:
        Generated scripts for matched playbooks (or the ``only`` subset).
    """
    scripts: list[GeneratedScript] = []
    for pb in ALL_PLAYBOOKS:
        if only is not None and pb.id not in only:
            continue
        if pb.detect(ctx) is not None:
            scripts.append(pb.render_script(ctx))
    return scripts
