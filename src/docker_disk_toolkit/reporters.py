"""Rendering & persistence — terminal, Markdown, JSON, CSV, audit log.

Every command can emit a timestamped ``.json`` + ``.md`` report pair into the
report directory and append audit events to ``audit.jsonl``. Terminal output
uses Rich tables/panels. Pure ``render_*`` functions are golden-tested.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .config import ToolkitConfig
from .models import (
    AuditEvent,
    CleanupPlan,
    DiagnosticReport,
    HealthStatus,
    SpaceDelta,
)
from .templating import render_template
from .utils import atomic_write_text, humanize_size

_HEALTH_EMOJI = {
    HealthStatus.HEALTHY: "🟢",
    HealthStatus.WARNING: "🟡",
    HealthStatus.CRITICAL: "🔴",
    HealthStatus.UNKNOWN: "⚪",
}
_HEALTH_STYLE = {
    HealthStatus.HEALTHY: "green",
    HealthStatus.WARNING: "yellow",
    HealthStatus.CRITICAL: "red",
    HealthStatus.UNKNOWN: "dim",
}
_SEVERITY_STYLE = {
    HealthStatus.CRITICAL: "bold red",
    HealthStatus.WARNING: "yellow",
    HealthStatus.HEALTHY: "green",
    HealthStatus.UNKNOWN: "dim",
}


@dataclass
class ReportPaths:
    """The on-disk paths of a written report pair."""

    json_path: Path
    md_path: Path


# ---------------------------------------------------------------------------
# JSON / CSV
# ---------------------------------------------------------------------------


def render_json(report: DiagnosticReport) -> str:
    """Serialise a report to canonical, machine-clean JSON (bytes stay int)."""
    return report.model_dump_json(indent=2)


def flatten_report(report: DiagnosticReport) -> list[dict[str, Any]]:
    """Flatten a report into one row per object for CSV export."""
    rows: list[dict[str, Any]] = []

    def add(record_type: str, name: str, size: int | None, **extra: Any) -> None:
        rows.append(
            {
                "record_type": record_type,
                "name": name,
                "size_bytes": size if size is not None else "",
                "size_human": humanize_size(size) if size is not None else "n/a",
                "in_use": extra.pop("in_use", ""),
                "extra": ";".join(f"{k}={v}" for k, v in extra.items()),
            }
        )

    for disk in report.disks:
        add(
            "disk",
            disk.mountpoint,
            disk.total_bytes,
            free_bytes=disk.free_bytes,
            percent_used=round(disk.percent_used, 1),
            docker_root=disk.is_docker_root,
        )
    if report.docker is not None:
        for img in report.docker.image_list:
            add("image", img.display_name, img.size_bytes, in_use=img.in_use, dangling=img.dangling)
        for cont in report.docker.container_list:
            add("container", cont.name, cont.size_rw_bytes, in_use=cont.running, state=cont.state)
        for vol in report.docker.volume_list:
            add(
                "volume",
                vol.name,
                vol.size_bytes,
                in_use=vol.in_use,
                protected=vol.protected,
            )
        for bc in report.docker.build_cache_list:
            add("build_cache", bc.id, bc.size_bytes, in_use=bc.in_use)
    return rows


def to_csv(rows: list[dict[str, Any]]) -> str:
    """Render flattened rows as CSV text."""
    columns = ["record_type", "name", "size_bytes", "size_human", "in_use", "extra"]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def _delta_context(delta: SpaceDelta | None) -> dict[str, Any] | None:
    if delta is None:
        return None
    docker_freed = delta.docker_total_before_bytes - delta.docker_total_after_bytes
    return {
        "reclaimed": humanize_size(delta.reclaimed_bytes),
        "host_free_before": humanize_size(delta.host_free_before_bytes),
        "host_free_after": humanize_size(delta.host_free_after_bytes),
        "host_freed": humanize_size(delta.host_freed_bytes),
        "docker_before": humanize_size(delta.docker_total_before_bytes),
        "docker_after": humanize_size(delta.docker_total_after_bytes),
        "docker_freed": humanize_size(docker_freed),
    }


def build_markdown_context(
    report: DiagnosticReport,
    *,
    command: str,
    delta: SpaceDelta | None = None,
) -> dict[str, Any]:
    """Assemble the (humanised) Jinja context for the Markdown report."""
    ctx: dict[str, Any] = {
        "command": command,
        "generated_at": report.generated_at.isoformat(),
        "hostname": report.hostname,
        "os": report.os,
        "correlation_id": report.correlation_id,
        "health": report.health.value,
        "health_emoji": _HEALTH_EMOJI[report.health],
        "disks": [
            {
                "mountpoint": d.mountpoint,
                "total": humanize_size(d.total_bytes),
                "used": humanize_size(d.used_bytes),
                "free": humanize_size(d.free_bytes),
                "percent_used": f"{d.percent_used:.0f}%",
                "inodes_percent": (
                    f"{d.inodes_percent:.0f}%" if d.inodes_percent is not None else "-"
                ),
                "is_docker_root": d.is_docker_root,
                "caveat": d.backend_caveat,
            }
            for d in report.disks
        ],
        "docker_available": report.docker is not None,
        "docker_status": report.docker_probe.availability.value,
        "docker_remediation": report.docker_probe.remediation,
        "findings": [
            {"severity": f.severity.value, "code": f.code, "message": f.message}
            for f in report.findings
        ],
        "recommendations": [
            {
                "action": r.action,
                "reclaimable": humanize_size(r.reclaimable_bytes),
                "command_hint": r.command_hint,
            }
            for r in report.recommendations
        ],
        "delta": _delta_context(delta),
    }

    if report.docker is not None:
        usage = report.docker

        def cat(c: Any) -> dict[str, Any]:
            return {
                "total": humanize_size(c.total_bytes),
                "reclaimable": humanize_size(c.reclaimable_bytes),
                "active": c.active,
                "total_count": c.total_count,
            }

        top_images = sorted(usage.image_list, key=lambda i: i.size_bytes, reverse=True)[:10]
        top_volumes = sorted(usage.volume_list, key=lambda v: v.size_bytes or 0, reverse=True)[:10]
        ctx["docker"] = {
            "images": cat(usage.images),
            "containers": cat(usage.containers),
            "volumes": cat(usage.volumes),
            "build_cache": cat(usage.build_cache),
            "total": humanize_size(usage.total_bytes),
            "reclaimable": humanize_size(usage.reclaimable_bytes),
        }
        ctx["top_images"] = [
            {"name": i.display_name, "size": humanize_size(i.size_bytes), "in_use": i.in_use}
            for i in top_images
        ]
        ctx["top_volumes"] = [
            {
                "name": v.name,
                "size": humanize_size(v.size_bytes),
                "in_use": v.in_use,
                "protected": v.protected,
            }
            for v in top_volumes
        ]
    else:
        ctx["docker"] = None
        ctx["top_images"] = []
        ctx["top_volumes"] = []
    return ctx


def render_markdown(
    report: DiagnosticReport,
    *,
    command: str,
    delta: SpaceDelta | None = None,
) -> str:
    """Render a full Markdown report."""
    context = build_markdown_context(report, command=command, delta=delta)
    return render_template("reports/report.md.j2", **context)


# ---------------------------------------------------------------------------
# Report files
# ---------------------------------------------------------------------------


def _timestamp_slug(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H-%M-%SZ")


def report_paths(report_dir: Path, *, command: str, run_id: str, ts: datetime) -> ReportPaths:
    """Compute the timestamped JSON + Markdown paths for a report."""
    stem = f"{_timestamp_slug(ts)}_{command}_{run_id}"
    return ReportPaths(
        json_path=report_dir / f"{stem}.json",
        md_path=report_dir / f"{stem}.md",
    )


def write_report(
    report: DiagnosticReport,
    config: ToolkitConfig,
    *,
    command: str,
    delta: SpaceDelta | None = None,
) -> ReportPaths:
    """Write the JSON + Markdown report pair atomically and return their paths."""
    paths = report_paths(
        config.report_dir,
        command=command,
        run_id=report.correlation_id,
        ts=report.generated_at,
    )
    atomic_write_text(paths.json_path, render_json(report))
    atomic_write_text(paths.md_path, render_markdown(report, command=command, delta=delta))
    return paths


def append_audit(config: ToolkitConfig, events: list[AuditEvent]) -> Path:
    """Append audit events (one JSON object per line) to the audit log."""
    path = config.audit_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(event.model_dump_json() + "\n")
        handle.flush()
    return path


# ---------------------------------------------------------------------------
# Terminal rendering (Rich)
# ---------------------------------------------------------------------------


def _health_panel(report: DiagnosticReport) -> Panel:
    style = _HEALTH_STYLE[report.health]
    body = Text()
    body.append(
        f"{_HEALTH_EMOJI[report.health]} {report.health.value.upper()}", style=f"bold {style}"
    )
    body.append(f"\nHost: {report.hostname} ({report.os})")
    body.append(f"\nCorrelation: {report.correlation_id}")
    if report.docker is not None:
        body.append(
            f"\nDocker: {humanize_size(report.docker.total_bytes)} used, "
            f"{humanize_size(report.docker.reclaimable_bytes)} reclaimable"
        )
    else:
        body.append(f"\nDocker: unavailable ({report.docker_probe.availability.value})")
    return Panel(body, title="Docker Disk Health", border_style=style, expand=False)


def _disk_table(report: DiagnosticReport) -> Table:
    table = Table(title="Host filesystems", expand=False)
    table.add_column("Mount")
    table.add_column("Total", justify="right")
    table.add_column("Used", justify="right")
    table.add_column("Free", justify="right")
    table.add_column("Used %", justify="right")
    table.add_column("Docker root", justify="center")
    for disk in report.disks:
        pct_style = (
            "red" if disk.percent_used >= 90 else "yellow" if disk.percent_used >= 80 else ""
        )
        table.add_row(
            disk.mountpoint,
            humanize_size(disk.total_bytes),
            humanize_size(disk.used_bytes),
            humanize_size(disk.free_bytes),
            Text(f"{disk.percent_used:.0f}%", style=pct_style),
            "✓" if disk.is_docker_root else "",
        )
    return table


def _docker_table(report: DiagnosticReport) -> Table:
    usage = report.docker
    assert usage is not None
    table = Table(title="Docker usage", expand=False)
    table.add_column("Category")
    table.add_column("Size", justify="right")
    table.add_column("Reclaimable", justify="right")
    table.add_column("Active/Total", justify="right")
    for label, cat in (
        ("Images", usage.images),
        ("Containers", usage.containers),
        ("Volumes", usage.volumes),
        ("Build cache", usage.build_cache),
    ):
        table.add_row(
            label,
            humanize_size(cat.total_bytes),
            humanize_size(cat.reclaimable_bytes),
            f"{cat.active}/{cat.total_count}",
        )
    table.add_row(
        Text("Total", style="bold"),
        Text(humanize_size(usage.total_bytes), style="bold"),
        Text(humanize_size(usage.reclaimable_bytes), style="bold"),
        "",
    )
    return table


def render_terminal(report: DiagnosticReport, console: Console) -> None:
    """Render a full diagnostic report to the terminal."""
    console.print(_health_panel(report))
    console.print(_disk_table(report))
    for disk in report.disks:
        if disk.backend_caveat:
            console.print(f"[yellow]⚠ {disk.mountpoint}:[/yellow] {disk.backend_caveat}")
    if report.docker is not None:
        console.print(_docker_table(report))
    else:
        console.print(
            Panel(
                Text(report.docker_probe.remediation or "Docker is unavailable."),
                title="[yellow]Docker unavailable[/yellow]",
                border_style="yellow",
                expand=False,
            )
        )
    if report.findings:
        console.print("\n[bold]Findings[/bold]")
        for finding in report.findings:
            style = _SEVERITY_STYLE[finding.severity]
            console.print(
                f"  [{style}]● {finding.severity.value.upper()}[/{style}] "
                f"({finding.code}) {finding.message}"
            )
    if report.recommendations:
        console.print("\n[bold]Recommended actions[/bold]")
        for rec in report.recommendations:
            console.print(
                f"  • {rec.action} — reclaim ~{humanize_size(rec.reclaimable_bytes)}  "
                f"[dim]{rec.command_hint}[/dim]"
            )


def render_plan(plan: CleanupPlan, console: Console) -> None:
    """Render a cleanup plan (candidates + protected) before confirmation."""
    table = Table(title=f"Cleanup plan (level {int(plan.level)})", expand=False)
    table.add_column("Kind")
    table.add_column("Name")
    table.add_column("Reclaim", justify="right")
    table.add_column("In use", justify="center")
    for item in plan.items:
        table.add_row(
            item.kind.value,
            item.name,
            humanize_size(item.reclaim_bytes),
            "yes" if item.in_use else "",
        )
    console.print(table)
    console.print(
        f"[bold]Would free ~{humanize_size(plan.total_reclaim_bytes)}[/bold] "
        f"across {len(plan.items)} object(s); "
        f"[green]{len(plan.protected)} protected/spared[/green]."
    )
    if plan.docker_reported_reclaim_bytes:
        console.print(
            f"[dim]Docker reports {humanize_size(plan.docker_reported_reclaim_bytes)} total "
            f"reclaimable; this plan targets a filtered/protected subset.[/dim]"
        )


def render_delta(delta: SpaceDelta, console: Console) -> None:
    """Render a before/after space-reclaim banner."""
    body = Text()
    body.append(f"Reclaimed: {humanize_size(delta.reclaimed_bytes)}\n", style="bold green")
    body.append(
        f"Host free: {humanize_size(delta.host_free_before_bytes)} → "
        f"{humanize_size(delta.host_free_after_bytes)}\n"
    )
    body.append(
        f"Docker total: {humanize_size(delta.docker_total_before_bytes)} → "
        f"{humanize_size(delta.docker_total_after_bytes)}"
    )
    console.print(Panel(body, title="Before / After", border_style="green", expand=False))
