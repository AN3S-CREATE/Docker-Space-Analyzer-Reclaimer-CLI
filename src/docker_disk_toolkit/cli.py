"""Typer command-line interface.

Subcommands: ``analyze`` (default), ``cleanup``, ``monitor``, ``install-cron``,
``report``, ``emergency``. A single boundary maps :class:`ToolkitError` to its
exit code and any unexpected error to :attr:`ExitCode.FATAL`; every command
returns one of the four documented exit codes.
"""

from __future__ import annotations

import contextlib
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
import typer
from rich.console import Console
from rich.prompt import Confirm, Prompt

from . import cleaner, history, reporters, scheduling
from .analyzer import analyze as run_analyze
from .config import ToolkitConfig, load_config
from .context import RunContext
from .errors import ExitCode, ToolkitError
from .models import CleanupPlan, CleanupResult, HealthStatus, PruneLevel
from .utils import humanize_size, is_tty, new_run_id, parse_duration

app = typer.Typer(
    name="docker-disk",
    help="Analyze Docker disk usage, reclaim space safely, and prevent recurrence.",
    no_args_is_help=False,
    add_completion=False,
)

_err_console = Console(stderr=True)

_HEALTH_EXIT = {
    HealthStatus.HEALTHY: ExitCode.HEALTHY,
    HealthStatus.WARNING: ExitCode.WARNING_CLEANED,
    HealthStatus.CRITICAL: ExitCode.CRITICAL,
    HealthStatus.UNKNOWN: ExitCode.HEALTHY,
}


@dataclass
class AppState:
    """Global CLI state stored on the Typer context object."""

    config: ToolkitConfig
    json_out: bool
    console: Console


# ---------------------------------------------------------------------------
# Infrastructure
# ---------------------------------------------------------------------------


def _force_utf8_output() -> None:
    """Make stdout/stderr tolerate non-ASCII (emoji, box chars) on any console.

    Legacy Windows consoles use cp1252 and would raise UnicodeEncodeError on the
    health emoji; reconfiguring to UTF-8 with ``errors="replace"`` degrades
    gracefully instead of crashing.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(ValueError, OSError):  # pragma: no cover - stream dependent
            reconfigure(encoding="utf-8", errors="replace")


def _configure_logging(*, verbose: bool, quiet: bool, json_logs: bool) -> None:
    level = logging.DEBUG if verbose else logging.ERROR if quiet else logging.INFO
    logging.basicConfig(level=level, format="%(message)s", stream=sys.stderr)
    renderer = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )


def _build_run_context(config: ToolkitConfig, *, correlation_id: str | None = None) -> RunContext:
    """Build a run context. Overridden in tests to inject a fake Docker client."""
    return RunContext.create(config, correlation_id=correlation_id or new_run_id())


def _fail(exc: ToolkitError) -> None:
    _err_console.print(exc.rich_panel())
    raise typer.Exit(int(exc.exit_code))


def _overrides(
    *,
    report_dir: Path | None,
    dry_run: bool | None,
    assume_yes: bool,
    protect: list[str] | None,
    min_free_gb: float | None,
    max_docker_percent: float | None,
    log_json: bool,
) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    if report_dir is not None:
        overrides["report_dir"] = report_dir
    if dry_run is not None:
        overrides["dry_run"] = dry_run
    if assume_yes:
        overrides["assume_yes"] = True
    if protect:
        overrides["protect_volumes"] = protect
    if log_json:
        overrides["log_json"] = True
    thresholds: dict[str, Any] = {}
    if min_free_gb is not None:
        thresholds["min_free_gb"] = min_free_gb
    if max_docker_percent is not None:
        thresholds["max_docker_percent"] = max_docker_percent
    if thresholds:
        overrides["thresholds"] = thresholds
    return overrides


# ---------------------------------------------------------------------------
# Global callback
# ---------------------------------------------------------------------------


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    config_path: Path | None = typer.Option(None, "--config", help="Path to config YAML."),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="Verbose logging."),
    quiet: bool = typer.Option(False, "-q", "--quiet", help="Errors only."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable JSON output."),
    report_dir: Path | None = typer.Option(None, "--report-dir", help="Report directory."),
    dry_run: bool | None = typer.Option(
        None, "--dry-run/--no-dry-run", help="Dry run (default on)."
    ),
    assume_yes: bool = typer.Option(False, "-y", "--yes", help="Assume yes to confirmations."),
    protect: list[str] | None = typer.Option(None, "--protect", help="Protect volume pattern(s)."),
    min_free_gb: float | None = typer.Option(None, "--min-free-gb", help="Min free GB threshold."),
    max_docker_percent: float | None = typer.Option(
        None, "--max-docker-percent", help="Max Docker %% of host disk."
    ),
    log_json: bool = typer.Option(False, "--log-json", help="Structured JSON logs."),
) -> None:
    """Set up shared state; run ``analyze`` when no subcommand is given."""
    _force_utf8_output()
    _configure_logging(verbose=verbose, quiet=quiet, json_logs=log_json)
    try:
        config = load_config(
            config_path=config_path,
            cli_overrides=_overrides(
                report_dir=report_dir,
                dry_run=dry_run,
                assume_yes=assume_yes,
                protect=protect,
                min_free_gb=min_free_gb,
                max_docker_percent=max_docker_percent,
                log_json=log_json,
            ),
        )
    except ToolkitError as exc:
        _fail(exc)
    state = AppState(config=config, json_out=json_out, console=Console())
    ctx.obj = state
    if ctx.invoked_subcommand is None:
        code = _do_analyze(state, csv_out=False, forensic_since=None, write=True)
        raise typer.Exit(int(code))


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------


def _do_analyze(
    state: AppState,
    *,
    csv_out: bool,
    forensic_since: timedelta | None,
    write: bool,
) -> ExitCode:
    try:
        run_ctx = _build_run_context(state.config)
        report = run_analyze(run_ctx, forensic_since=forensic_since)
    except ToolkitError as exc:
        _fail(exc)

    if state.json_out:
        typer.echo(reporters.render_json(report))
    elif csv_out:
        typer.echo(reporters.to_csv(reporters.flatten_report(report)))
    else:
        reporters.render_terminal(report, state.console)

    if write:
        try:
            paths = reporters.write_report(report, state.config, command="analyze")
            if not state.json_out and not csv_out:
                state.console.print(f"[dim]Report written to {paths.md_path}[/dim]")
        except OSError as exc:  # pragma: no cover - fs dependent
            state.console.print(f"[yellow]Could not write report: {exc}[/yellow]")
    return _HEALTH_EXIT[report.health]


@app.command()
def analyze(
    ctx: typer.Context,
    csv_out: bool = typer.Option(False, "--csv", help="CSV output."),
    forensic: bool = typer.Option(False, "--forensic", help="'What just ate my disk?' mode."),
    since: str = typer.Option("24h", "--since", help="Forensic window (e.g. 24h, 7d)."),
    write: bool = typer.Option(True, "--write/--no-write", help="Write a report file."),
) -> None:
    """Deep diagnostic report of host + Docker disk usage."""
    state: AppState = ctx.obj
    forensic_since = parse_duration(since) if forensic else None
    code = _do_analyze(state, csv_out=csv_out, forensic_since=forensic_since, write=write)
    raise typer.Exit(int(code))


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------


def _make_confirm(console: Console) -> Callable[[CleanupPlan, str], bool]:
    def confirm(plan: CleanupPlan, mode: str) -> bool:
        reporters.render_plan(plan, console)
        if not is_tty():
            console.print("[yellow]Non-interactive shell and --yes not set; refusing.[/yellow]")
            return False
        if mode == "type-to-confirm":
            phrase = f"delete {len(plan.items)}"
            console.print(
                f"[bold red]NUCLEAR cleanup.[/bold red] Type '[bold]{phrase}[/bold]' to proceed."
            )
            answer = Prompt.ask("Confirm")
            return answer.strip().lower() == phrase
        return Confirm.ask("Proceed with cleanup?", default=False)

    return confirm


@app.command()
def cleanup(
    ctx: typer.Context,
    level: int = typer.Option(0, "-l", "--level", min=0, max=3, help="0 report..3 nuclear."),
    force: bool = typer.Option(False, "--force", help="Required for level-3 removal."),
    exclude_images: list[str] | None = typer.Option(None, "--exclude-images"),
    name: list[str] | None = typer.Option(None, "--name", help="Name glob filter."),
    label: list[str] | None = typer.Option(None, "--label", help="key=value label filter."),
    min_size: str | None = typer.Option(None, "--min-size", help="Only objects >= size."),
    age: str | None = typer.Option(None, "--age", help="Only objects older than (e.g. 7d)."),
    stop_on_error: bool = typer.Option(False, "--stop-on-error"),
) -> None:
    """Reclaim space with multi-level, protect-list-aware prune strategies."""
    state: AppState = ctx.obj
    console = state.console
    try:
        from .utils import parse_size

        labels = dict(pair.split("=", 1) for pair in (label or []) if "=" in pair)
        criteria = cleaner.SelectionCriteria.from_config(
            state.config,
            exclude_images=exclude_images or [],
            labels=labels,
            name_globs=name or [],
            min_age=parse_duration(age),
            min_size_bytes=parse_size(min_size) if min_size else None,
        )
        run_ctx = _build_run_context(state.config)
        result = cleaner.run_cleanup(
            run_ctx,
            level=PruneLevel(level),
            criteria=criteria,
            force=force,
            stop_on_error=stop_on_error,
            confirm_fn=_make_confirm(console),
        )
    except ToolkitError as exc:
        _fail(exc)

    if result.delta is not None and not result.dry_run:
        reporters.render_delta(result.delta, console)
    elif result.dry_run:
        console.print("[cyan]Dry run — nothing was removed.[/cyan]")
    if result.errors:
        console.print(f"[yellow]{len(result.errors)} action(s) failed:[/yellow]")
        for err in result.errors:
            console.print(f"  [red]•[/red] {err}")

    _persist_cleanup(state, result)
    raise typer.Exit(int(_cleanup_exit(result)))


def _persist_cleanup(state: AppState, result: CleanupResult) -> None:
    try:
        reporters.append_audit(state.config, result.audit_events)
        history.record_run(
            result,
            jsonl_path=state.config.history_jsonl_path,
            db_path=state.config.history_db_path,
            command="cleanup",
            ts=result.audit_events[0].ts if result.audit_events else None,
        )
    except OSError as exc:  # pragma: no cover - fs dependent
        state.console.print(f"[yellow]Could not persist audit/history: {exc}[/yellow]")


def _cleanup_exit(result: CleanupResult) -> ExitCode:
    if result.errors and result.post_health is HealthStatus.HEALTHY:
        return ExitCode.WARNING_CLEANED
    return _HEALTH_EXIT[result.post_health]


# ---------------------------------------------------------------------------
# monitor
# ---------------------------------------------------------------------------


@app.command()
def monitor(
    ctx: typer.Context,
    once: bool = typer.Option(True, "--once/--watch", help="Single check vs continuous loop."),
    interval: int = typer.Option(300, "--interval", help="Watch interval seconds."),
    prometheus_textfile: Path | None = typer.Option(None, "--prometheus-textfile"),
    notify: bool = typer.Option(True, "--notify/--no-notify"),
) -> None:
    """Evaluate thresholds, write metrics, and notify on breach."""
    from . import monitor as monitor_mod

    state: AppState = ctx.obj
    try:
        run_ctx = _build_run_context(state.config)
        if once:
            reading = monitor_mod.check_once(
                run_ctx, notify=notify, prometheus_path=prometheus_textfile
            )
            readings = [reading]
        else:  # pragma: no cover - blocking loop
            readings = monitor_mod.watch(run_ctx, interval_seconds=interval)
    except ToolkitError as exc:
        _fail(exc)

    latest = readings[-1]
    if state.json_out:
        typer.echo(latest.model_dump_json(indent=2))
    else:
        state.console.print(
            f"Health: [bold]{latest.health.value.upper()}[/bold] — "
            f"{len(latest.breaches)} breach(es)"
        )
        for breach in latest.breaches:
            state.console.print(f"  [yellow]•[/yellow] {breach.message}")
    raise typer.Exit(int(_HEALTH_EXIT[latest.health]))


# ---------------------------------------------------------------------------
# install-cron
# ---------------------------------------------------------------------------


@app.command(name="install-cron")
def install_cron(
    ctx: typer.Context,
    action: str = typer.Option("cleanup", "--action", help="analyze|cleanup|monitor."),
    level: int = typer.Option(1, "--level", min=0, max=3),
    schedule: str = typer.Option("daily", "--schedule", help="hourly|daily|weekly."),
    emit: str = typer.Option("auto", "--emit", help="auto|systemd|cron|windows|all."),
    output_dir: Path | None = typer.Option(None, "--output-dir", help="Write artifacts here."),
) -> None:
    """Generate systemd/cron/Windows Task Scheduler artifacts for periodic runs."""
    state: AppState = ctx.obj
    console = state.console
    try:
        artifacts = scheduling.generate(
            state.config, action=action, level=PruneLevel(level), schedule=schedule, emit=emit
        )
    except (ValueError, ToolkitError) as exc:
        if isinstance(exc, ToolkitError):
            _fail(exc)
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(int(ExitCode.FATAL)) from exc

    for art in artifacts:
        if output_dir is not None:
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / art.filename).write_text(art.content, encoding="utf-8")
            console.print(f"[green]Wrote[/green] {output_dir / art.filename}")
        else:
            console.rule(f"{art.kind} — {art.filename}")
            console.print(art.content)
        console.print(f"[dim]Install: {art.install_hint}[/dim]\n")
    raise typer.Exit(int(ExitCode.HEALTHY))


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


@app.command()
def report(
    ctx: typer.Context,
    last: str = typer.Option("30d", "--last", help="Trend window (e.g. 30d)."),
    rebuild_index: bool = typer.Option(False, "--rebuild-index", help="Rebuild SQLite from JSONL."),
    playbooks_out: bool = typer.Option(False, "--playbooks", help="Render recovery scripts."),
    list_reports: bool = typer.Option(False, "--list", help="List stored reports."),
) -> None:
    """Show historical trends, list reports, or render recovery playbooks."""
    state: AppState = ctx.obj
    console = state.console
    config = state.config

    if rebuild_index:
        count = history.rebuild_index(
            jsonl_path=config.history_jsonl_path, db_path=config.history_db_path
        )
        console.print(f"[green]Rebuilt history index from {count} run(s).[/green]")
        raise typer.Exit(int(ExitCode.HEALTHY))

    if list_reports:
        _list_reports(console, config.report_dir)
        raise typer.Exit(int(ExitCode.HEALTHY))

    if playbooks_out:
        _render_playbooks(state)
        raise typer.Exit(int(ExitCode.HEALTHY))

    window = parse_duration(last) or timedelta(days=30)
    stats = history.trend(db_path=config.history_db_path, window=window, now=datetime.now(UTC))
    if state.json_out:
        typer.echo(stats.model_dump_json(indent=2))
    else:
        console.print(
            f"[bold]Last {stats.window_days} days:[/bold] {stats.cleanups} cleanup(s), "
            f"freed {humanize_size(stats.total_freed_bytes)} total "
            f"(avg {humanize_size(stats.avg_freed_bytes)}, "
            f"largest {humanize_size(stats.largest_single_reclaim_bytes)})."
        )
        for kind, freed in stats.freed_by_kind.items():
            console.print(f"  {kind}: {humanize_size(freed)}")
    raise typer.Exit(int(ExitCode.HEALTHY))


def _list_reports(console: Console, report_dir: Path) -> None:
    if not report_dir.exists():
        console.print("[yellow]No reports directory yet.[/yellow]")
        return
    reports = sorted(report_dir.glob("*.json"), reverse=True)
    if not reports:
        console.print("[yellow]No reports found.[/yellow]")
        return
    for path in reports[:30]:
        console.print(f"  {path.name}")


def _render_playbooks(state: AppState) -> None:
    from . import system_info
    from .analyzer import analyze as analyze_fn
    from .playbooks import PlaybookContext, render_scripts

    run_ctx = _build_run_context(state.config)
    report_obj = analyze_fn(run_ctx)
    if report_obj.docker is None:
        state.console.print("[yellow]Docker unavailable — no playbooks to render.[/yellow]")
        return
    pctx = PlaybookContext(
        usage=report_obj.docker,
        disks=report_obj.disks,
        backend=report_obj.docker_probe.backend,
        storage_driver=report_obj.docker_info.storage_driver if report_obj.docker_info else None,
        docker_root_dir=report_obj.docker_info.docker_root_dir if report_obj.docker_info else None,
        vhdx_files=system_info.find_vhdx_files(),
        host_is_windows=system_info.is_windows(),
        now=run_ctx.now,
    )
    scripts = render_scripts(pctx)
    if not scripts:
        state.console.print("[green]No recovery playbooks triggered — nothing to do.[/green]")
        return
    for script in scripts:
        state.console.rule(f"{script.title} ({script.filename})")
        for step in script.steps:
            state.console.print(f"  • {step}")
        state.console.print(f"\n[dim]--- {script.filename} ---[/dim]")
        state.console.print(script.content)


# ---------------------------------------------------------------------------
# emergency
# ---------------------------------------------------------------------------


@app.command()
def emergency(
    ctx: typer.Context,
    min_free_gb: float | None = typer.Option(None, "--min-free-gb", help="Target free space."),
) -> None:
    """Free space now: safest high-impact prune first, then next-step guidance."""
    state: AppState = ctx.obj
    console = state.console
    try:
        run_ctx = _build_run_context(state.config)
        result = cleaner.run_emergency(run_ctx, confirm_fn=lambda plan, mode: True)
    except ToolkitError as exc:
        _fail(exc)

    if result.dry_run:
        reporters.render_plan(result.plan, console)
        console.print("[cyan]Dry run — re-run with --yes to actually free space.[/cyan]")
    elif result.delta is not None:
        reporters.render_delta(result.delta, console)

    run_ctx2 = _build_run_context(state.config)
    usage = run_ctx2.docker.collect_usage() if run_ctx2.docker.probe().ok else None
    if usage is not None:
        console.print("\n[bold]Higher-impact next steps (review before running):[/bold]")
        for step in cleaner.emergency_next_steps(usage):
            console.print(f"  • {step}")

    _persist_cleanup(state, result)
    raise typer.Exit(int(_cleanup_exit(result)))


if __name__ == "__main__":  # pragma: no cover
    app()
