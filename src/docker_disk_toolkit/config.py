"""Configuration model and loader (pydantic-settings v2).

Effective precedence (highest wins):

    CLI flag  >  env ``DOCKER_DISK_*``  >  YAML config file  >  field defaults

The YAML file is resolved from (first existing wins): an explicit ``--config``
path / ``DOCKER_DISK_CONFIG`` env var, ``~/.config/docker-disk-toolkit/
config.yaml``, and the platform config dir. All validation failures surface as
:class:`docker_disk_toolkit.errors.ConfigError` with actionable messages.
"""

from __future__ import annotations

import os
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import yaml
from platformdirs import user_config_dir
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic import (
    ValidationError as PydanticValidationError,
)
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from .errors import ConfigError
from .models import HealthStatus, PruneLevel
from .utils import RegexCompileError, compile_protect_matchers

# Runtime context threaded into ``settings_customise_sources`` (the classmethod
# has no other channel for the resolved YAML path). ``None`` sentinel avoids a
# shared-mutable default.
_YAML_DATA: ContextVar[dict[str, Any] | None] = ContextVar("yaml_data", default=None)

APP_NAME = "docker-disk-toolkit"

# Well-known stateful-app volume patterns protected by default (overridable, and
# disableable with ``--no-default-protect``). Never nuke a database by accident.
DEFAULT_PROTECT_REGEXES: tuple[str, ...] = (
    r"(?i)(postgres|postgresql|pgdata)",
    r"(?i)(mysql|mariadb)",
    r"(?i)mongo",
    r"(?i)redis",
    r"(?i)nextcloud",
    r"(?i)(grafana|prometheus|influxdb|loki)",
    r"(?i)vault",
    r"(?i)(elastic|opensearch)",
    r"(?i)minio",
    r"(?i)(truenas|nextcloud|paperless|immich)",
)


class Thresholds(BaseModel):
    """Free-space and Docker-usage thresholds driving health assessment."""

    model_config = ConfigDict(extra="forbid")

    critical_free_gb: float = Field(default=5.0, ge=0)
    min_free_gb: float = Field(default=10.0, ge=0)
    warn_free_gb: float = Field(default=20.0, ge=0)
    min_free_percent: float = Field(default=10.0, ge=0, le=100)
    max_docker_percent: float = Field(default=70.0, ge=0, le=100)

    @model_validator(mode="after")
    def _check_ordering(self) -> Thresholds:
        if not (self.critical_free_gb <= self.min_free_gb <= self.warn_free_gb):
            raise ValueError(
                "thresholds must satisfy critical_free_gb <= min_free_gb <= warn_free_gb "
                f"(got {self.critical_free_gb}, {self.min_free_gb}, {self.warn_free_gb})"
            )
        return self


class NotificationConfig(BaseModel):
    """Watchdog notification preferences."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    on_events: set[HealthStatus] = Field(default_factory=lambda: {HealthStatus.CRITICAL})
    webhook_url: SecretStr | None = None
    desktop: bool = True


class DockerConfig(BaseModel):
    """Docker client behaviour."""

    model_config = ConfigDict(extra="forbid")

    cli_path: str | None = None
    host: str | None = None
    timeout_seconds: float = Field(default=30.0, gt=0)
    prefer_cli: bool = True
    api_fallback: bool = True


def _default_report_dir() -> Path:
    return Path.home() / "docker-disk-reports"


class _YamlSettingsSource(PydanticBaseSettingsSource):
    """Settings source that supplies values parsed from the YAML config file.

    The data is read outside the source (path resolution needs runtime input)
    and passed in via the :data:`_YAML_DATA` context var so this source can be
    slotted into the precedence chain below env and above defaults.
    """

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls)
        self._data = dict(_YAML_DATA.get() or {})

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        if field_name in self._data:
            return self._data[field_name], field_name, False
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return self._data


class ToolkitConfig(BaseSettings):
    """Top-level toolkit configuration.

    Env vars use the ``DOCKER_DISK_`` prefix with ``__`` for nesting, e.g.
    ``DOCKER_DISK_THRESHOLDS__MIN_FREE_GB=15``.
    """

    model_config = SettingsConfigDict(
        env_prefix="DOCKER_DISK_",
        env_nested_delimiter="__",
        extra="forbid",
        validate_default=True,
    )

    thresholds: Thresholds = Field(default_factory=Thresholds)
    protect_volumes: list[str] = Field(default_factory=list)
    protect_volumes_regex: list[str] = Field(default_factory=list)
    use_default_protect: bool = True
    exclude_images: list[str] = Field(default_factory=list)
    prune_level: PruneLevel = PruneLevel.SAFE
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)
    docker: DockerConfig = Field(default_factory=DockerConfig)
    report_dir: Path = Field(default_factory=_default_report_dir)
    dry_run: bool = True
    assume_yes: bool = False
    log_level: str = "INFO"
    log_json: bool = False

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Precedence: init(CLI) > env > YAML > defaults."""
        return (
            init_settings,
            env_settings,
            _YamlSettingsSource(settings_cls),
            file_secret_settings,
        )

    # -- validators ---------------------------------------------------------

    @field_validator("protect_volumes_regex")
    @classmethod
    def _validate_regexes(cls, value: list[str]) -> list[str]:
        for pattern in value:
            try:
                compile_protect_matchers(regexes=[pattern])
            except RegexCompileError as exc:
                raise ValueError(str(exc)) from exc
        return value

    @field_validator("report_dir")
    @classmethod
    def _validate_report_dir(cls, value: Path) -> Path:
        from .utils import is_safe_output_path

        expanded = value.expanduser()
        if not is_safe_output_path(expanded):
            raise ValueError(
                f"report_dir {value!r} is unsafe (traversal or a system root); "
                "choose a directory under your home folder"
            )
        return expanded

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        upper = value.upper()
        if upper not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}, got {value!r}")
        return upper

    # -- derived helpers ----------------------------------------------------

    def effective_protect_regexes(self) -> list[str]:
        """Return the full protect-regex list (user + built-in defaults)."""
        regexes = list(self.protect_volumes_regex)
        if self.use_default_protect:
            regexes.extend(DEFAULT_PROTECT_REGEXES)
        return regexes

    def protect_matcher(self) -> Any:
        """Compile the effective volume protect-list into a matcher."""
        return compile_protect_matchers(
            globs=self.protect_volumes,
            regexes=self.effective_protect_regexes(),
        )

    @property
    def audit_path(self) -> Path:
        """Path to the append-only audit JSONL log."""
        return self.report_dir / "audit.jsonl"

    @property
    def history_jsonl_path(self) -> Path:
        """Path to the append-only history summary JSONL (source of truth)."""
        return self.report_dir / "history.jsonl"

    @property
    def history_db_path(self) -> Path:
        """Path to the rebuildable SQLite history index."""
        return self.report_dir / "history.db"

    @property
    def metrics_path(self) -> Path:
        """Path to the latest-reading metrics JSON file."""
        return self.report_dir / "metrics.json"


# ---------------------------------------------------------------------------
# Config file resolution & loading
# ---------------------------------------------------------------------------


def resolve_config_path(explicit: Path | str | None = None) -> Path | None:
    """Resolve the YAML config file path, returning the first that exists.

    Order: explicit arg / ``DOCKER_DISK_CONFIG`` env, then
    ``~/.config/docker-disk-toolkit/config.yaml``, then the platform config dir.

    Returns:
        The first existing config path, or ``None`` if none exist. If an
        *explicit* path is given it is returned even when missing (so a clear
        "not found" error can be raised by the caller).
    """
    if explicit is not None:
        return Path(explicit).expanduser()
    env_path = os.environ.get("DOCKER_DISK_CONFIG")
    if env_path:
        return Path(env_path).expanduser()

    candidates = [
        Path.home() / ".config" / APP_NAME / "config.yaml",
        Path(user_config_dir(APP_NAME)) / "config.yaml",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(
            f"could not read config file {path}",
            remediation="Check the path and file permissions, or pass --config.",
        ) from exc
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(
            f"invalid YAML in config file {path}: {exc}",
            remediation="Fix the YAML syntax; see docs for the config reference.",
        ) from exc
    if not isinstance(data, dict):
        raise ConfigError(
            f"config file {path} must contain a mapping at the top level",
            remediation="Wrap settings under keys like 'thresholds:' and 'report_dir:'.",
        )
    return data


def _humanize_validation_error(exc: PydanticValidationError) -> str:
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(item) for item in err["loc"]) or "(root)"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)


def load_config(
    *,
    config_path: Path | str | None = None,
    cli_overrides: dict[str, Any] | None = None,
) -> ToolkitConfig:
    """Load configuration honouring the CLI > env > YAML > defaults precedence.

    Args:
        config_path: Explicit config file path (from ``--config``). If given and
            the file is missing, a :class:`ConfigError` is raised.
        cli_overrides: Mapping of *explicitly-set* CLI options. Only keys the
            user actually passed should be present (Typer defaults must be
            filtered out by the caller) so they don't clobber env/YAML values.

    Returns:
        A validated :class:`ToolkitConfig`.

    Raises:
        ConfigError: On unreadable/invalid config or failed validation.
    """
    cli_overrides = {k: v for k, v in (cli_overrides or {}).items() if v is not None}

    resolved = resolve_config_path(config_path)
    yaml_data: dict[str, Any] = {}
    if resolved is not None:
        if resolved.is_file():
            yaml_data = _load_yaml(resolved)
        elif config_path is not None:
            raise ConfigError(
                f"config file not found: {resolved}",
                remediation="Create the file or omit --config to use defaults.",
            )

    # First resolve env > YAML > defaults (no CLI), then deep-merge CLI overrides
    # on top so partial nested overrides (e.g. one threshold) don't reset siblings.
    token = _YAML_DATA.set(yaml_data)
    try:
        base = ToolkitConfig()
    except PydanticValidationError as exc:
        raise ConfigError(
            f"invalid configuration: {_humanize_validation_error(exc)}",
            remediation="Correct the offending setting (config file or env var).",
        ) from exc
    finally:
        _YAML_DATA.reset(token)

    if not cli_overrides:
        return base

    merged = _deep_merge(base.model_dump(mode="python"), cli_overrides)
    empty = _YAML_DATA.set({})
    try:
        return ToolkitConfig(**merged)
    except PydanticValidationError as exc:
        raise ConfigError(
            f"invalid configuration from command-line options: {_humanize_validation_error(exc)}",
            remediation="Check values passed to flags like --min-free-gb / --protect.",
        ) from exc
    finally:
        _YAML_DATA.reset(empty)


def _deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``overrides`` into a copy of ``base`` (overrides win)."""
    result = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result
