"""Golden / structural tests for :mod:`docker_disk_toolkit.scheduling`."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Callable

import pytest

from docker_disk_toolkit import scheduling
from docker_disk_toolkit.config import ToolkitConfig
from docker_disk_toolkit.models import PruneLevel


@pytest.fixture
def config(make_config: Callable[..., ToolkitConfig]) -> ToolkitConfig:
    return make_config()


class TestSpec:
    def test_action_args(self) -> None:
        assert scheduling._action_args("analyze", PruneLevel.SAFE) == "analyze --json"
        assert (
            scheduling._action_args("cleanup", PruneLevel.AGGRESSIVE) == "cleanup --level 2 --yes"
        )
        assert scheduling._action_args("monitor", PruneLevel.SAFE) == "monitor --once"
        with pytest.raises(ValueError):
            scheduling._action_args("bogus", PruneLevel.SAFE)

    def test_build_spec(self, config: ToolkitConfig) -> None:
        spec = scheduling.build_spec(
            config, action="cleanup", level=PruneLevel.SAFE, exec_path="/x/docker-disk"
        )
        assert spec.args == "cleanup --level 1 --yes"
        assert spec.exec_path == "/x/docker-disk"

    def test_bad_schedule(self, config: ToolkitConfig) -> None:
        with pytest.raises(ValueError):
            scheduling.build_spec(config, schedule="fortnightly")


class TestResolveKinds:
    @pytest.mark.parametrize(
        ("emit", "expected"),
        [
            ("systemd", ["systemd-service", "systemd-timer"]),
            ("cron", ["crontab"]),
            ("windows", ["windows-task-xml"]),
            ("all", ["systemd-service", "systemd-timer", "crontab", "windows-task-xml"]),
        ],
    )
    def test_explicit(self, emit: str, expected: list[str]) -> None:
        assert scheduling.resolve_kinds(emit) == expected

    def test_auto_by_platform(self) -> None:
        assert scheduling.resolve_kinds("auto", platform="windows") == ["windows-task-xml"]
        assert scheduling.resolve_kinds("auto", platform="darwin") == ["crontab"]
        assert "systemd-timer" in scheduling.resolve_kinds("auto", platform="linux")


class TestRenderArtifacts:
    def test_all_render(self, config: ToolkitConfig) -> None:
        artifacts = scheduling.generate(
            config, action="cleanup", schedule="daily", emit="all", exec_path="/usr/bin/docker-disk"
        )
        by_kind = {a.kind: a for a in artifacts}
        assert set(by_kind) == {
            "systemd-service",
            "systemd-timer",
            "crontab",
            "windows-task-xml",
        }

    def test_systemd_service_content(self, config: ToolkitConfig) -> None:
        art = next(
            a
            for a in scheduling.generate(config, emit="systemd", exec_path="/usr/bin/docker-disk")
            if a.kind == "systemd-service"
        )
        assert "ExecStart=/usr/bin/docker-disk cleanup --level 1 --yes" in art.content
        assert "Type=oneshot" in art.content
        assert "IOSchedulingClass=idle" in art.content

    def test_systemd_timer_oncalendar(self, config: ToolkitConfig) -> None:
        art = next(
            a
            for a in scheduling.generate(config, emit="systemd", schedule="weekly")
            if a.kind == "systemd-timer"
        )
        assert "OnCalendar=Sun *-*-* 03:00:00" in art.content
        assert "Persistent=true" in art.content

    def test_crontab_line(self, config: ToolkitConfig) -> None:
        art = scheduling.generate(
            config, action="analyze", emit="cron", schedule="daily", exec_path="/x/docker-disk"
        )[0]
        assert "0 3 * * * /x/docker-disk analyze --json" in art.content
        assert "crontab -" in art.install_hint

    def test_windows_xml_is_wellformed(self, config: ToolkitConfig) -> None:
        art = scheduling.generate(
            config,
            action="cleanup",
            emit="windows",
            schedule="daily",
            exec_path="C:/tools/docker-disk.exe",
        )[0]
        # must parse as valid XML
        root = ET.fromstring(art.content)
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        command = root.find(".//t:Actions/t:Exec/t:Command", ns)
        assert command is not None and command.text == "C:/tools/docker-disk.exe"
        args = root.find(".//t:Actions/t:Exec/t:Arguments", ns)
        assert args is not None and args.text == "cleanup --level 1 --yes"
        assert "schtasks /Create" in art.install_hint

    def test_windows_xml_escapes_special_chars(self, config: ToolkitConfig) -> None:
        spec = scheduling.build_spec(config, exec_path="C:/a&b/docker-disk.exe")
        art = scheduling.render_artifacts(spec, ["windows-task-xml"])[0]
        # raw ampersand must be escaped; XML still parses
        assert "&amp;" in art.content
        ET.fromstring(art.content)
