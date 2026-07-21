"""Generate scheduling artifacts (systemd, cron, Windows Task Scheduler).

``install-cron`` renders ready-to-use artifacts for periodic ``analyze`` /
``cleanup`` / ``monitor`` runs and prints the exact install command. It never
performs a privileged install itself — the user reviews and applies the output.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from . import system_info
from .config import ToolkitConfig
from .models import PruneLevel
from .templating import render_template

# Schedule preset -> per-platform expressions. Uses a fixed start date so the
# generated Windows XML is deterministic (Task Scheduler recurs from it).
_SCHEDULES: dict[str, dict[str, str]] = {
    "hourly": {
        "cron": "0 * * * *",
        "oncalendar": "hourly",
        "windows": "daily",
        "start_boundary": "2026-01-01T00:00:00",
    },
    "daily": {
        "cron": "0 3 * * *",
        "oncalendar": "*-*-* 03:00:00",
        "windows": "daily",
        "start_boundary": "2026-01-01T03:00:00",
    },
    "weekly": {
        "cron": "0 3 * * 0",
        "oncalendar": "Sun *-*-* 03:00:00",
        "windows": "weekly",
        "start_boundary": "2026-01-04T03:00:00",
    },
}


@dataclass
class ScheduleSpec:
    """A fully-resolved scheduling request."""

    action: str  # analyze | cleanup | monitor
    schedule: str  # hourly | daily | weekly
    exec_path: str
    args: str
    report_dir: str
    level: PruneLevel = PruneLevel.SAFE
    working_dir: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    unit_name: str = "docker-disk"
    task_name: str = "DockerDiskToolkit"


@dataclass
class Artifact:
    """A rendered scheduling artifact."""

    kind: str  # systemd-service | systemd-timer | crontab | windows-task-xml
    filename: str
    content: str
    install_hint: str


def resolve_exec_path() -> str:
    """Best-effort path to the installed ``docker-disk`` console script."""
    found = shutil.which("docker-disk")
    if found:
        return found
    # Fall back to the current interpreter running the module.
    return f"{Path(sys.executable).name} -m docker_disk_toolkit"


def _action_args(action: str, level: PruneLevel) -> str:
    if action == "analyze":
        return "analyze --json"
    if action == "cleanup":
        return f"cleanup --level {int(level)} --yes"
    if action == "monitor":
        return "monitor --once"
    raise ValueError(f"unknown action {action!r}; use analyze|cleanup|monitor")


def build_spec(
    config: ToolkitConfig,
    *,
    action: str = "cleanup",
    level: PruneLevel = PruneLevel.SAFE,
    schedule: str = "daily",
    exec_path: str | None = None,
) -> ScheduleSpec:
    """Assemble a :class:`ScheduleSpec` from config + options."""
    if schedule not in _SCHEDULES:
        raise ValueError(f"unknown schedule {schedule!r}; use {sorted(_SCHEDULES)}")
    return ScheduleSpec(
        action=action,
        schedule=schedule,
        level=level,
        exec_path=exec_path or resolve_exec_path(),
        args=_action_args(action, level),
        report_dir=str(config.report_dir),
        unit_name=f"docker-disk-{action}",
        task_name=f"DockerDiskToolkit-{action}",
    )


def resolve_kinds(emit: str, *, platform: str | None = None) -> list[str]:
    """Resolve which artifact kinds to render from ``emit`` and the platform."""
    if emit == "systemd":
        return ["systemd-service", "systemd-timer"]
    if emit == "cron":
        return ["crontab"]
    if emit == "windows":
        return ["windows-task-xml"]
    if emit == "all":
        return ["systemd-service", "systemd-timer", "crontab", "windows-task-xml"]
    # auto
    if platform is None:
        platform = "windows" if system_info.is_windows() else "linux"
    if platform == "windows":
        return ["windows-task-xml"]
    if platform == "darwin":
        return ["crontab"]
    return ["systemd-service", "systemd-timer", "crontab"]


def render_artifacts(spec: ScheduleSpec, kinds: list[str]) -> list[Artifact]:
    """Render the requested artifact kinds for ``spec``."""
    presets = _SCHEDULES[spec.schedule]
    artifacts: list[Artifact] = []
    for kind in kinds:
        if kind == "systemd-service":
            content = render_template(
                "scheduling/systemd.service.j2",
                action=spec.action,
                exec_path=spec.exec_path,
                args=spec.args,
                working_dir=spec.working_dir,
                report_dir=spec.report_dir,
                env=spec.env,
            )
            artifacts.append(
                Artifact(
                    kind,
                    f"{spec.unit_name}.service",
                    content,
                    f"Copy to ~/.config/systemd/user/{spec.unit_name}.service",
                )
            )
        elif kind == "systemd-timer":
            content = render_template(
                "scheduling/systemd.timer.j2",
                action=spec.action,
                oncalendar=presets["oncalendar"],
                unit_name=spec.unit_name,
            )
            artifacts.append(
                Artifact(
                    kind,
                    f"{spec.unit_name}.timer",
                    content,
                    (
                        f"Copy to ~/.config/systemd/user/{spec.unit_name}.timer, then: "
                        f"systemctl --user daemon-reload && "
                        f"systemctl --user enable --now {spec.unit_name}.timer"
                    ),
                )
            )
        elif kind == "crontab":
            content = render_template(
                "scheduling/crontab.j2",
                action=spec.action,
                cron_expr=presets["cron"],
                exec_path=spec.exec_path,
                args=spec.args,
                report_dir=spec.report_dir,
            )
            artifacts.append(
                Artifact(
                    kind,
                    "docker-disk.cron",
                    content,
                    "Install with: (crontab -l 2>/dev/null; cat docker-disk.cron) | crontab -",
                )
            )
        elif kind == "windows-task-xml":
            content = render_template(
                "scheduling/windows-task.xml.j2",
                action=spec.action,
                task_name=spec.task_name,
                start_boundary=presets["start_boundary"],
                schedule=presets["windows"],
                command=xml_escape(spec.exec_path),
                arguments=xml_escape(spec.args),
                working_dir=xml_escape(spec.working_dir) if spec.working_dir else None,
            )
            artifacts.append(
                Artifact(
                    kind,
                    f"{spec.task_name}.xml",
                    content,
                    f'Install with: schtasks /Create /TN "{spec.task_name}" '
                    f"/XML {spec.task_name}.xml",
                )
            )
        else:  # pragma: no cover - guarded by resolve_kinds
            raise ValueError(f"unknown artifact kind {kind!r}")
    return artifacts


def generate(
    config: ToolkitConfig,
    *,
    action: str = "cleanup",
    level: PruneLevel = PruneLevel.SAFE,
    schedule: str = "daily",
    emit: str = "auto",
    exec_path: str | None = None,
    platform: str | None = None,
) -> list[Artifact]:
    """High-level entry: build a spec and render the resolved artifact kinds."""
    spec = build_spec(config, action=action, level=level, schedule=schedule, exec_path=exec_path)
    kinds = resolve_kinds(emit, platform=platform)
    return render_artifacts(spec, kinds)
