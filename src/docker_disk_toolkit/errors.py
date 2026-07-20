"""Exception hierarchy and process exit codes.

The toolkit maps every terminal outcome to one of four exit codes (per the
project specification)::

    0  HEALTHY          all thresholds satisfied / operation succeeded
    1  WARNING_CLEANED  issues found and/or low space, but remediated
    2  CRITICAL         thresholds still breached after any actions
    3  FATAL            tool / config / docker error; could not complete

:class:`ToolkitError` subclasses carry both an exit code and a human-facing
remediation string so the CLI boundary can render an actionable panel instead
of a bare traceback.
"""

from __future__ import annotations

from enum import IntEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from rich.panel import Panel


class ExitCode(IntEnum):
    """Process exit codes (authoritative, per specification)."""

    HEALTHY = 0
    WARNING_CLEANED = 1
    CRITICAL = 2
    FATAL = 3


class ToolkitError(Exception):
    """Base class for all expected, handled toolkit errors.

    Attributes:
        message: Short, human-readable summary of what went wrong.
        remediation: Concrete next step(s) the user can take to recover.
        exit_code: Process exit code to return when this error reaches the CLI
            boundary. Defaults to :attr:`ExitCode.FATAL`.
    """

    exit_code: ExitCode = ExitCode.FATAL

    def __init__(self, message: str, *, remediation: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.remediation = remediation

    def rich_panel(self) -> Panel:
        """Render this error as a Rich panel with remediation guidance.

        Returns:
            A :class:`rich.panel.Panel` suitable for printing to stderr.
        """
        from rich.panel import Panel
        from rich.text import Text

        body = Text()
        body.append(self.message, style="bold red")
        if self.remediation:
            body.append("\n\n")
            body.append("How to fix: ", style="bold yellow")
            body.append(self.remediation)
        return Panel(
            body,
            title=f"[red]{type(self).__name__}[/red]",
            border_style="red",
            expand=False,
        )


class ConfigError(ToolkitError):
    """Configuration is invalid or could not be loaded."""

    exit_code = ExitCode.FATAL


class DockerUnavailableError(ToolkitError):
    """Docker is required for the requested command but is not usable.

    Raised only by command handlers that cannot proceed without Docker (for
    example ``cleanup`` and ``emergency``). Read-only commands such as
    ``analyze`` tolerate a missing daemon and emit a finding instead.
    """

    def __init__(
        self,
        message: str,
        *,
        remediation: str = "",
        fatal: bool = True,
    ) -> None:
        super().__init__(message, remediation=remediation)
        self.exit_code = ExitCode.FATAL if fatal else ExitCode.WARNING_CLEANED


class DockerCommandError(ToolkitError):
    """A specific ``docker`` command failed after the daemon was reachable."""

    exit_code = ExitCode.FATAL

    def __init__(
        self,
        message: str,
        *,
        argv: list[str] | None = None,
        returncode: int | None = None,
        stderr: str = "",
        remediation: str = "",
    ) -> None:
        super().__init__(message, remediation=remediation)
        self.argv = argv or []
        self.returncode = returncode
        self.stderr = stderr


class SafetyError(ToolkitError):
    """A destructive action was attempted against a protected/critical object.

    This is a guardrail: it should never be reachable in normal operation and
    signals a logic error or an attempt to bypass the protect-list.
    """

    exit_code = ExitCode.FATAL


class ValidationError(ToolkitError):
    """User-supplied input (path, regex, threshold, size) failed validation."""

    exit_code = ExitCode.FATAL
