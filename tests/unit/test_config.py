"""Unit tests for :mod:`docker_disk_toolkit.config` — validation & precedence."""

from __future__ import annotations

from pathlib import Path

import pytest

from docker_disk_toolkit.config import (
    DEFAULT_PROTECT_REGEXES,
    ToolkitConfig,
    load_config,
    resolve_config_path,
)
from docker_disk_toolkit.errors import ConfigError
from docker_disk_toolkit.models import PruneLevel


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip any ambient DOCKER_DISK_* env so tests are deterministic."""
    import os

    for key in list(os.environ):
        if key.startswith("DOCKER_DISK_"):
            monkeypatch.delenv(key, raising=False)


class TestDefaults:
    def test_safe_defaults(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DOCKER_DISK_REPORT_DIR", str(tmp_path / "reports"))
        cfg = load_config()
        assert cfg.dry_run is True
        assert cfg.assume_yes is False
        assert cfg.prune_level is PruneLevel.SAFE
        assert cfg.thresholds.min_free_gb == 10.0

    def test_default_protectlist_matches_databases(self) -> None:
        cfg = ToolkitConfig(protect_volumes_regex=[], use_default_protect=True)
        matcher = cfg.protect_matcher()
        assert matcher.matches("my_postgres_data")
        assert matcher.matches("nextcloud_files")
        assert not matcher.matches("scratch_build_tmp")

    def test_disable_default_protect(self) -> None:
        cfg = ToolkitConfig(use_default_protect=False)
        assert cfg.effective_protect_regexes() == []


class TestValidation:
    def test_threshold_ordering_enforced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DOCKER_DISK_THRESHOLDS__CRITICAL_FREE_GB", "50")
        monkeypatch.setenv("DOCKER_DISK_THRESHOLDS__MIN_FREE_GB", "10")
        with pytest.raises(ConfigError) as exc:
            load_config()
        assert "critical_free_gb" in str(exc.value)

    def test_percent_bounds(self) -> None:
        with pytest.raises(Exception):
            ToolkitConfig(thresholds={"max_docker_percent": 150})

    def test_bad_regex_rejected(self) -> None:
        with pytest.raises(Exception):
            ToolkitConfig(protect_volumes_regex=["("])

    def test_unsafe_report_dir_rejected(self) -> None:
        with pytest.raises(Exception):
            ToolkitConfig(report_dir=Path("/"))

    def test_log_level_normalised(self) -> None:
        assert ToolkitConfig(log_level="debug").log_level == "DEBUG"
        with pytest.raises(Exception):
            ToolkitConfig(log_level="LOUD")

    def test_unknown_field_forbidden(self) -> None:
        with pytest.raises(Exception):
            ToolkitConfig(bogus=1)  # type: ignore[call-arg]


class TestPrecedence:
    def _write_yaml(self, path: Path, body: str) -> Path:
        cfg = path / "config.yaml"
        cfg.write_text(body, encoding="utf-8")
        return cfg

    def test_yaml_over_defaults(self, tmp_path: Path) -> None:
        cfg_file = self._write_yaml(
            tmp_path,
            "dry_run: false\nthresholds:\n  min_free_gb: 15\nreport_dir: "
            + repr(str(tmp_path / "r")),
        )
        cfg = load_config(config_path=cfg_file)
        assert cfg.dry_run is False
        assert cfg.thresholds.min_free_gb == 15

    def test_env_over_yaml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg_file = self._write_yaml(tmp_path, "thresholds:\n  min_free_gb: 12\n")
        monkeypatch.setenv("DOCKER_DISK_THRESHOLDS__MIN_FREE_GB", "18")
        cfg = load_config(config_path=cfg_file)
        assert cfg.thresholds.min_free_gb == 18

    def test_cli_over_env_and_yaml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg_file = self._write_yaml(tmp_path, "prune_level: 1\n")
        monkeypatch.setenv("DOCKER_DISK_PRUNE_LEVEL", "2")
        cfg = load_config(config_path=cfg_file, cli_overrides={"prune_level": PruneLevel.NUCLEAR})
        assert cfg.prune_level is PruneLevel.NUCLEAR

    def test_cli_none_values_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DOCKER_DISK_DRY_RUN", "false")
        cfg = load_config(cli_overrides={"dry_run": None, "assume_yes": True})
        # dry_run None override is dropped, so env value (False) wins
        assert cfg.dry_run is False
        assert cfg.assume_yes is True


class TestConfigPathResolution:
    def test_explicit_missing_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as exc:
            load_config(config_path=tmp_path / "nope.yaml")
        assert "not found" in str(exc.value)

    def test_env_config_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = tmp_path / "custom.yaml"
        target.write_text("log_level: DEBUG\n", encoding="utf-8")
        monkeypatch.setenv("DOCKER_DISK_CONFIG", str(target))
        assert resolve_config_path() == target

    def test_bad_yaml_content(self, tmp_path: Path) -> None:
        bad = tmp_path / "config.yaml"
        bad.write_text("just a string, not a mapping", encoding="utf-8")
        with pytest.raises(ConfigError) as exc:
            load_config(config_path=bad)
        assert "mapping" in str(exc.value)


class TestDerivedPaths:
    def test_derived_paths_under_report_dir(self, tmp_path: Path) -> None:
        cfg = ToolkitConfig(report_dir=tmp_path / "reports")
        assert cfg.audit_path == tmp_path / "reports" / "audit.jsonl"
        assert cfg.history_jsonl_path.name == "history.jsonl"
        assert cfg.history_db_path.suffix == ".db"
        assert cfg.metrics_path.name == "metrics.json"

    def test_effective_regexes_include_defaults(self) -> None:
        cfg = ToolkitConfig(protect_volumes_regex=["custom"])
        regexes = cfg.effective_protect_regexes()
        assert "custom" in regexes
        assert all(d in regexes for d in DEFAULT_PROTECT_REGEXES)
