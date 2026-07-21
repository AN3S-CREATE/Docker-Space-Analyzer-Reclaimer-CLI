"""Per-run context threaded through every command.

Holds the correlation id, resolved config, the selected Docker client, a Rich
console, and the run timestamp (so time-dependent output is deterministic in
tests). Build one with :meth:`RunContext.create`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from rich.console import Console

from .config import ToolkitConfig
from .docker_client import DockerClient, build_docker_client
from .utils import CommandRunner, new_run_id


@dataclass
class RunContext:
    """Shared state for a single toolkit invocation."""

    correlation_id: str
    config: ToolkitConfig
    docker: DockerClient
    console: Console
    now: datetime

    @classmethod
    def create(
        cls,
        config: ToolkitConfig,
        *,
        docker: DockerClient | None = None,
        runner: CommandRunner | None = None,
        console: Console | None = None,
        now: datetime | None = None,
        correlation_id: str | None = None,
    ) -> RunContext:
        """Assemble a :class:`RunContext`, building a Docker client if needed."""
        return cls(
            correlation_id=correlation_id or new_run_id(),
            config=config,
            docker=docker if docker is not None else build_docker_client(config, runner=runner),
            console=console or Console(),
            now=now or datetime.now(UTC),
        )
