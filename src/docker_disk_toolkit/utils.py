"""Pure, dependency-light helpers shared across the toolkit.

This module is a leaf: it imports only the standard library (plus a lazy
:mod:`structlog` binding for the subprocess runner). It hosts the functions
that fixtures hammer hardest — size parsing, protect-list matching — and the
:class:`CommandRunner` seam that makes the whole suite runnable without a live
Docker daemon.

Sizing conventions (important):
    * Docker ``system df`` / ``ls`` emit **decimal** human strings (``kB``,
      ``MB``, ``GB`` at base 1000). :func:`parse_size` therefore defaults to
      decimal. Binary units (``KiB``, ``MiB``, ...) are recognised by the
      ``i`` and use base 1024.
    * ``docker inspect`` returns raw integer bytes — never route those through
      :func:`parse_size`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Protocol

# ---------------------------------------------------------------------------
# Errors local to parsing (kept as ValueError subclasses so utils stays a leaf)
# ---------------------------------------------------------------------------


class SizeParseError(ValueError):
    """Raised when a size string cannot be interpreted as a byte count."""


class RegexCompileError(ValueError):
    """Raised when a user-supplied protect regex fails to compile."""


# ---------------------------------------------------------------------------
# Size parsing / humanizing
# ---------------------------------------------------------------------------

# Sentinels Docker emits for "no measurable size". Treated as zero bytes.
_EMPTY_SIZE_TOKENS = {"", "-", "0", "0b", "n/a", "na", "<unknown>", "none", "null"}

# Prefix -> power of the base. ``k`` is 10^3 / 2^10 depending on the ``i``.
_PREFIX_POWER = {"k": 1, "m": 2, "g": 3, "t": 4, "p": 5, "e": 6, "z": 7, "y": 8}

_SIZE_RE = re.compile(r"^\s*([+-]?\d+(?:[.,]\d+)?)\s*([a-zA-Z]*)\s*$")


def parse_size(value: str | int | float | None, *, default_binary: bool = False) -> int:
    """Parse a human-readable size string into an integer byte count.

    Args:
        value: A size such as ``"4.1GB"``, ``"955.5MiB"``, ``"0B"``, an int/
            float already in bytes, or an "empty" sentinel (``""``, ``"N/A"``,
            ``"<unknown>"``) which maps to ``0``.
        default_binary: When the unit carries no explicit ``i`` (e.g. ``"GB"``),
            interpret it as binary (base 1024) instead of decimal (base 1000).
            Docker ``df``/``ls`` output is decimal, so this stays ``False``.

    Returns:
        The size in whole bytes.

    Raises:
        SizeParseError: If ``value`` is a non-empty string that cannot be parsed.

    Examples:
        >>> parse_size("4.1GB")
        4100000000
        >>> parse_size("955.5MiB")
        1001914368
        >>> parse_size("N/A")
        0
    """
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)

    text = value.strip()
    if text.lower() in _EMPTY_SIZE_TOKENS:
        return 0

    match = _SIZE_RE.match(text)
    if match is None:
        raise SizeParseError(f"cannot parse size: {value!r}")

    number_raw, unit_raw = match.groups()
    number = float(number_raw.replace(",", "."))
    unit = unit_raw.lower()

    if unit in {"", "b"}:
        return int(number)

    binary = "i" in unit
    prefix = unit[0]
    power = _PREFIX_POWER.get(prefix)
    if power is None:
        raise SizeParseError(f"unknown size unit {unit_raw!r} in {value!r}")

    base = 1024 if (binary or default_binary) else 1000
    multiplier: int = base**power
    return round(number * multiplier)


_DECIMAL_UNITS = ["B", "kB", "MB", "GB", "TB", "PB", "EB"]
_BINARY_UNITS = ["B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB"]


def humanize_size(num_bytes: int | None, *, binary: bool = False, precision: int = 1) -> str:
    """Render a byte count as a compact human-readable string.

    Args:
        num_bytes: Size in bytes. ``None`` renders as ``"n/a"``.
        binary: Use base-1024 units (``KiB``) instead of decimal (``kB``).
        precision: Decimal places for values >= 1 unit.

    Returns:
        A string such as ``"4.1 GB"`` or ``"512 B"``.

    Examples:
        >>> humanize_size(4_100_000_000)
        '4.1 GB'
        >>> humanize_size(0)
        '0 B'
    """
    if num_bytes is None:
        return "n/a"
    base = 1024 if binary else 1000
    units = _BINARY_UNITS if binary else _DECIMAL_UNITS
    negative = num_bytes < 0
    size = float(abs(num_bytes))
    for unit in units:
        if size < base or unit == units[-1]:
            if unit == "B":
                rendered = f"{int(size)} {unit}"
            else:
                rendered = f"{size:.{precision}f} {unit}"
            return f"-{rendered}" if negative else rendered
        size /= base
    return f"{num_bytes} B"  # pragma: no cover - unreachable given units[-1] guard


_RECLAIMABLE_RE = re.compile(r"^(?P<size>.*?)(?:\s*\((?P<pct>[\d.]+)%\))?\s*$")


def parse_reclaimable(value: str | None) -> tuple[int, float | None]:
    """Parse a Docker "reclaimable" field like ``"2.3GB (56%)"``.

    Args:
        value: The reclaimable string. May be just a size (``"0B"``) or a size
            with a percentage clause.

    Returns:
        A ``(bytes, percent)`` tuple; ``percent`` is ``None`` when absent.

    Examples:
        >>> parse_reclaimable("2.3GB (56%)")
        (2300000000, 56.0)
        >>> parse_reclaimable("0B")
        (0, None)
    """
    if value is None:
        return (0, None)
    match = _RECLAIMABLE_RE.match(value.strip())
    if match is None:  # pragma: no cover - regex always matches
        return (parse_size(value), None)
    size = parse_size(match.group("size"))
    pct_raw = match.group("pct")
    percent = float(pct_raw) if pct_raw is not None else None
    return (size, percent)


def parse_count(value: str | int | None) -> int:
    """Coerce a Docker count field (often a JSON string like ``"12"``) to int.

    Negative sentinels such as ``"-1"`` (Docker's "unknown") collapse to ``0``.
    """
    if value is None:
        return 0
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    return max(parsed, 0)


# ---------------------------------------------------------------------------
# Time parsing
# ---------------------------------------------------------------------------

_TZNAME_SUFFIX_RE = re.compile(r"\s+[A-Za-z]{2,5}$")


def parse_docker_time(value: str | None) -> datetime | None:
    """Parse the several timestamp formats Docker emits into an aware datetime.

    Handles both the ``ls`` ``CreatedAt`` form
    (``"2026-07-20 14:05:31 +0200 SAST"``) and the RFC3339 ``inspect`` form
    (``"2026-07-20T14:05:31.123456789Z"`` with nanosecond precision).

    Args:
        value: The timestamp string, or ``None``/empty.

    Returns:
        A timezone-aware :class:`datetime`, or ``None`` if unparseable/empty.
    """
    if not value:
        return None
    text = value.strip()
    if text in {"0001-01-01 00:00:00 +0000 UTC", "0001-01-01T00:00:00Z"}:
        return None

    # RFC3339 form (inspect). Truncate nanoseconds to microseconds for stdlib.
    iso = text.replace("Z", "+00:00")
    iso = re.sub(r"(\.\d{6})\d+", r"\1", iso)
    try:
        parsed = datetime.fromisoformat(iso)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        pass

    # ls "CreatedAt" form: "YYYY-MM-DD HH:MM:SS +ZZZZ TZNAME".
    stripped = _TZNAME_SUFFIX_RE.sub("", text)
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(stripped, fmt)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def age(when: datetime | None, *, now: datetime | None = None) -> timedelta | None:
    """Return the age (now - when) of a timestamp, or ``None`` if unknown."""
    if when is None:
        return None
    reference = now or datetime.now(UTC)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return reference - when


_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw])\s*$", re.IGNORECASE)
_DURATION_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(value: str | None) -> timedelta | None:
    """Parse a compact duration like ``"7d"``, ``"12h"``, ``"30m"`` into a delta.

    Returns:
        A :class:`timedelta`, or ``None`` for empty input.

    Raises:
        ValueError: If a non-empty value cannot be parsed.
    """
    if value is None or not value.strip():
        return None
    match = _DURATION_RE.match(value)
    if match is None:
        raise ValueError(f"cannot parse duration: {value!r} (use e.g. 7d, 12h, 30m)")
    number, unit = match.groups()
    return timedelta(seconds=float(number) * _DURATION_SECONDS[unit.lower()])


# ---------------------------------------------------------------------------
# Protect-list matching
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProtectSpec:
    """A single protect/exclude pattern with auto-detected match semantics.

    Grammar (auto-detected from the raw token):
        * ``regex:^db_``  -> regular-expression match
        * ``*_data``      -> glob match (any of ``* ? [``)
        * ``postgres``    -> exact, case-sensitive match
    """

    raw: str
    kind: str  # "exact" | "glob" | "regex"
    _compiled: re.Pattern[str] | None = field(default=None, compare=False)

    @classmethod
    def parse(cls, token: str) -> ProtectSpec:
        """Build a :class:`ProtectSpec` from a raw CSV token.

        Raises:
            RegexCompileError: If a ``regex:`` token fails to compile.
        """
        token = token.strip()
        if token.lower().startswith("regex:"):
            pattern = token[len("regex:") :]
            try:
                compiled = re.compile(pattern)
            except re.error as exc:
                raise RegexCompileError(f"invalid regex {pattern!r}: {exc}") from exc
            return cls(raw=pattern, kind="regex", _compiled=compiled)
        if any(ch in token for ch in "*?["):
            return cls(raw=token, kind="glob")
        return cls(raw=token, kind="exact")

    def matches(self, name: str) -> bool:
        """Return ``True`` if ``name`` matches this spec."""
        if self.kind == "regex":
            assert self._compiled is not None
            return self._compiled.search(name) is not None
        if self.kind == "glob":
            return fnmatch(name, self.raw)
        return name == self.raw


@dataclass(frozen=True)
class ProtectMatcher:
    """A compiled set of protect specs; matches if *any* spec matches."""

    specs: tuple[ProtectSpec, ...]

    def matches(self, name: str) -> bool:
        """Return ``True`` if ``name`` is protected by any spec."""
        return any(spec.matches(name) for spec in self.specs)

    def __bool__(self) -> bool:
        return bool(self.specs)


def parse_protect_tokens(tokens: Sequence[str]) -> list[str]:
    """Split a sequence that may contain comma-separated tokens into a flat list.

    Accepts both ``["a,b", "c"]`` and ``["a", "b", "c"]`` forms and drops blanks.
    """
    out: list[str] = []
    for token in tokens:
        out.extend(part.strip() for part in token.split(",") if part.strip())
    return out


def compile_protect_matchers(
    globs: Sequence[str] = (),
    regexes: Sequence[str] = (),
) -> ProtectMatcher:
    """Compile glob and regex protect patterns into a single matcher.

    Args:
        globs: Glob/exact tokens (may include comma-separated groups). A token
            prefixed with ``regex:`` is treated as a regex even here.
        regexes: Raw regex patterns (no prefix required).

    Returns:
        A :class:`ProtectMatcher`.

    Raises:
        RegexCompileError: If any regex fails to compile.
    """
    specs: list[ProtectSpec] = []
    for token in parse_protect_tokens(list(globs)):
        specs.append(ProtectSpec.parse(token))
    for pattern in parse_protect_tokens(list(regexes)):
        specs.append(ProtectSpec.parse(f"regex:{pattern}"))
    return ProtectMatcher(specs=tuple(specs))


def is_protected(name: str, matcher: ProtectMatcher) -> bool:
    """Return ``True`` if ``name`` is protected by ``matcher``."""
    return matcher.matches(name)


# ---------------------------------------------------------------------------
# Numeric / percentage helpers
# ---------------------------------------------------------------------------


def safe_percent(part: float, whole: float) -> float:
    """Return ``part/whole*100`` guarding against division by zero."""
    if whole <= 0:
        return 0.0
    return (part / whole) * 100.0


# ---------------------------------------------------------------------------
# Path & environment helpers
# ---------------------------------------------------------------------------


def _system_dir_candidates(*, windows: bool) -> list[str]:
    """Return the raw protected-directory list for the given platform.

    Kept pure (strings in, strings out) so branch selection is testable from
    either host without constructing platform-specific :class:`Path` objects.

    Only genuinely system-owned directories are listed. Locations a service may
    legitimately write to (``/var``, ``%ProgramData%``) are deliberately
    excluded so a systemd/Task-Scheduler deployment is not blocked.
    """
    if windows:
        # ``os.environ`` upper-cases keys on Windows, so these match regardless
        # of how the variable is spelled in the parent environment.
        return [
            os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR") or "C:/Windows",
            os.environ.get("PROGRAMFILES") or "C:/Program Files",
            os.environ.get("PROGRAMFILES(X86)") or "C:/Program Files (x86)",
        ]
    return ["/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/boot", "/sys", "/proc", "/dev"]


def _resolve_system_dirs() -> frozenset[Path]:
    """Resolve the platform's protected system directories once, at import.

    Entries **must** be resolved, because :func:`is_safe_output_path` compares
    them against a resolved candidate. Storing them unresolved silently
    disables the guard on Windows, where ``Path("/").resolve()`` yields the
    *current drive* root (``D:\\``) rather than ``\\``.

    Resolution also follows symlinks, so a distro where ``/lib`` points at
    ``/usr/lib`` is still matched correctly.
    """
    dirs: set[Path] = set()
    for entry in _system_dir_candidates(windows=os.name == "nt"):
        try:
            dirs.add(Path(entry).resolve())
        except (OSError, RuntimeError):  # pragma: no cover - hostile environment
            continue
    return frozenset(dirs)


# Non-empty on every supported platform; asserted by the test suite, because an
# empty set would silently downgrade the guard to "roots only".
_SYSTEM_DIRS: frozenset[Path] = _resolve_system_dirs()


def is_safe_output_path(path: Path) -> bool:
    """Return ``True`` if ``path`` is a sane place to write reports.

    Rejects, in order: unresolved traversal (``..``), any filesystem root, and
    the platform's system directories *including everything beneath them*, so a
    misconfigured ``report_dir`` can never target a critical location.

    Filesystem roots are detected structurally (``resolved.parent == resolved``)
    rather than by enumeration, so every drive letter and UNC share is covered
    instead of only ``C:``.

    Examples:
        >>> is_safe_output_path(Path("reports/../.."))
        False
        >>> is_safe_output_path(Path("/"))
        False
    """
    if any(part == ".." for part in path.parts):
        return False
    try:
        resolved = path.expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    if resolved.parent == resolved:  # "/", "C:\\", "D:\\", "\\\\server\\share"
        return False
    return not any(resolved.is_relative_to(system_dir) for system_dir in _SYSTEM_DIRS)


def is_wsl() -> bool:
    """Return ``True`` when running inside a WSL distribution."""
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    proc_version = Path("/proc/version")
    try:
        return "microsoft" in proc_version.read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        return False


def is_tty() -> bool:
    """Return ``True`` if stdin is an interactive terminal."""
    try:
        return sys.stdin.isatty()
    except (ValueError, OSError):  # pragma: no cover - detached stdin
        return False


def new_run_id() -> str:
    """Generate a short, unique, time-ordered-ish correlation id for a run."""
    return uuid.uuid4().hex[:12]


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Write ``content`` to ``path`` atomically (temp file + ``os.replace``).

    Creates parent directories as needed. The temp file lives in the same
    directory so the final ``os.replace`` is atomic on all platforms.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(content, encoding=encoding)
    tmp.replace(path)


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` (and parents) restricted to the invoking user.

    Reports and the audit log record Docker object names — image tags, volume
    and container names — which routinely embed customer, project or personal
    identifiers. On a shared host the default ``0o755`` would expose them to
    every local account, so the directory is created ``0o700`` and an existing
    directory is tightened.

    ``mkdir(mode=...)`` is subject to the umask and only applies to a directory
    it actually creates, hence the explicit :meth:`~pathlib.Path.chmod`. This is
    a no-op on Windows, where POSIX mode bits are not meaningful and NTFS
    inherits ACLs from the parent instead.
    """
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        # Best effort: a foreign filesystem or a dir owned by another user
        # cannot be tightened, and that must not fail the run.
        with suppress(OSError):
            path.chmod(0o700)
    return path


# ---------------------------------------------------------------------------
# Cross-process file locking (audit trail integrity)
# ---------------------------------------------------------------------------

LOCK_TIMEOUT_S = 10.0
_LOCK_POLL_S = 0.05


class FileLockTimeout(OSError):
    """Raised when an exclusive file lock could not be acquired in time."""


if sys.platform == "win32":  # pragma: no cover - platform specific
    import msvcrt

    def _lock_acquire(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _lock_release(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:  # pragma: no cover - platform specific
    import fcntl

    def _lock_acquire(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _lock_release(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def file_lock(target: Path, *, timeout_s: float = LOCK_TIMEOUT_S) -> Iterator[None]:
    """Hold a cross-process exclusive lock associated with ``target``.

    The audit trail is append-only and is written by both scheduled and
    interactive runs. A multi-line append is *not* atomic, so without a lock two
    overlapping runs can interleave their records and corrupt the very log the
    safety story depends on.

    Locking is done on a sidecar ``<name>.lock`` file so the data file's
    contents and file position are never disturbed. The lock is advisory, which
    suffices because every writer in this toolkit goes through this helper.

    Raises:
        FileLockTimeout: If the lock is still held after ``timeout_s``.
    """
    lock_path = target.with_name(target.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_s
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        while True:
            try:
                _lock_acquire(fd)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise FileLockTimeout(f"could not lock {target} within {timeout_s}s") from exc
                time.sleep(_LOCK_POLL_S)
        try:
            yield
        finally:
            _lock_release(fd)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# NDJSON parsing
# ---------------------------------------------------------------------------


def iter_ndjson(text: str) -> Iterator[dict[str, Any]]:
    """Yield JSON objects from newline-delimited JSON, tolerating noise.

    Docker occasionally prints a deprecation/warning line to stdout alongside
    ``--format '{{json .}}'`` output; such non-JSON lines are skipped rather
    than raising.

    Args:
        text: Raw stdout from a ``docker ... --format '{{json .}}'`` command.

    Yields:
        Each successfully-parsed JSON object.
    """
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] not in "{[":
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj


# ---------------------------------------------------------------------------
# CommandRunner seam — the testability keystone
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    """The outcome of running an external command.

    Attributes:
        argv: The argument vector that was executed.
        returncode: Process exit code. Sentinels: ``127`` = command not found,
            ``124`` = timed out.
        stdout: Captured standard output (text).
        stderr: Captured standard error (text).
        duration_s: Wall-clock duration in seconds.
    """

    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_s: float

    @property
    def ok(self) -> bool:
        """Return ``True`` if the command exited zero."""
        return self.returncode == 0


class CommandRunner(Protocol):
    """Protocol for running external commands.

    Production uses :class:`SubprocessRunner`; tests inject a fixture-backed
    runner so the entire suite runs without a live Docker daemon.
    """

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        """Execute ``argv`` and return a :class:`CommandResult` (never raises)."""
        ...


class SubprocessRunner:
    """Production :class:`CommandRunner` backed by :func:`subprocess.run`.

    The runner is *total*: it never raises. Missing binaries, timeouts, and
    OS-level failures are folded into a :class:`CommandResult` with a sentinel
    ``returncode`` so higher layers can classify them structurally.
    """

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        argv_list = list(argv)
        merged_env = {**os.environ, **env} if env else None
        start = time.monotonic()
        try:
            proc = subprocess.run(
                argv_list,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=merged_env,
                check=False,
            )
        except FileNotFoundError as exc:
            return CommandResult(argv_list, 127, "", str(exc), time.monotonic() - start)
        except subprocess.TimeoutExpired as exc:
            stderr = f"command timed out after {timeout}s"
            partial = exc.stdout or ""
            if isinstance(partial, bytes):
                partial = partial.decode("utf-8", "replace")
            return CommandResult(argv_list, 124, partial, stderr, time.monotonic() - start)
        except OSError as exc:  # pragma: no cover - platform dependent
            return CommandResult(argv_list, 126, "", str(exc), time.monotonic() - start)
        return CommandResult(
            argv_list,
            proc.returncode,
            proc.stdout or "",
            proc.stderr or "",
            time.monotonic() - start,
        )


class FixtureCommandRunner:
    """Test :class:`CommandRunner` that maps argv prefixes to canned results.

    Args:
        mapping: Ordered list of ``(argv_prefix, CommandResult)`` rules. The
            first rule whose ``argv_prefix`` is a prefix of the requested argv
            wins. This keeps fixtures terse (match on ``["docker", "image",
            "ls"]`` regardless of trailing flags).
        default: Result returned when no rule matches. If ``None``, a
            ``returncode=127`` "no fixture" result is returned.
    """

    def __init__(
        self,
        mapping: Sequence[tuple[Sequence[str], CommandResult]],
        *,
        default: CommandResult | None = None,
    ) -> None:
        self._rules = [(list(prefix), result) for prefix, result in mapping]
        self._default = default
        self.calls: list[list[str]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        argv_list = list(argv)
        self.calls.append(argv_list)
        for prefix, result in self._rules:
            if argv_list[: len(prefix)] == prefix:
                # Re-stamp argv so callers see what they asked for.
                return CommandResult(
                    argv_list,
                    result.returncode,
                    result.stdout,
                    result.stderr,
                    result.duration_s,
                )
        if self._default is not None:
            return CommandResult(
                argv_list,
                self._default.returncode,
                self._default.stdout,
                self._default.stderr,
                self._default.duration_s,
            )
        return CommandResult(argv_list, 127, "", "no fixture for command", 0.0)


def result(stdout: str = "", *, returncode: int = 0, stderr: str = "") -> CommandResult:
    """Convenience factory for building fixture :class:`CommandResult` objects."""
    return CommandResult(
        argv=[], returncode=returncode, stdout=stdout, stderr=stderr, duration_s=0.0
    )


_DEFAULT_RUNNER: SubprocessRunner | None = None


def default_runner() -> SubprocessRunner:
    """Return a shared production :class:`SubprocessRunner` instance."""
    global _DEFAULT_RUNNER
    if _DEFAULT_RUNNER is None:
        _DEFAULT_RUNNER = SubprocessRunner()
    return _DEFAULT_RUNNER
