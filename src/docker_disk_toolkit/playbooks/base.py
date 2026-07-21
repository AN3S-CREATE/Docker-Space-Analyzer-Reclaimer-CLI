"""Recovery-playbook framework: detector → finding → generated script.

Each playbook inspects a :class:`PlaybookContext` (the diagnostic snapshot) and,
when its situation is present, returns a :class:`~docker_disk_toolkit.models.Finding`
and can render a copy-paste-ready recovery script. Nothing destructive is ever
run automatically — scripts are generated for the user to review and execute.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

from ..models import DiskUsage, DockerBackend, DockerUsage, Finding
from ..system_info import VhdxFile


@dataclass
class PlaybookContext:
    """Everything a playbook needs to detect its situation and render a script."""

    usage: DockerUsage
    disks: list[DiskUsage]
    backend: DockerBackend
    storage_driver: str | None
    docker_root_dir: str | None
    vhdx_files: list[VhdxFile] = field(default_factory=list)
    host_is_windows: bool = False
    now: datetime | None = None

    @property
    def primary_disk(self) -> DiskUsage | None:
        """The Docker-root disk if known, else the first disk."""
        for disk in self.disks:
            if disk.is_docker_root:
                return disk
        return self.disks[0] if self.disks else None


@dataclass
class GeneratedScript:
    """A rendered, review-before-run recovery script."""

    playbook_id: str
    title: str
    shell: str  # "bash" | "powershell"
    filename: str
    steps: list[str]
    content: str
    requires_elevation: bool = False
    est_reclaim_bytes: int | None = None
    danger: str = ""


class BasePlaybook(ABC):
    """Abstract recovery playbook."""

    id: str = "base"
    title: str = "Base playbook"

    @abstractmethod
    def detect(self, ctx: PlaybookContext) -> Finding | None:
        """Return a :class:`Finding` if this playbook's situation applies."""

    @abstractmethod
    def render_script(self, ctx: PlaybookContext) -> GeneratedScript:
        """Render a tailored recovery script for the current situation."""
