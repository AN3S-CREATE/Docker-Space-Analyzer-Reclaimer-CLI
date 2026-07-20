"""Docker Disk Space Analyzer & Recovery Toolkit.

A production-grade CLI to diagnose Docker disk usage, safely reclaim space,
prevent recurrence, and leave an audit trail. Works with Docker Engine and
Docker Desktop (Windows/WSL2, macOS) in both root and rootless modes.

The public entry point is :data:`docker_disk_toolkit.cli.app`.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
