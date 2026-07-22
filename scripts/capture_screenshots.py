"""Capture real terminal-UI screenshots (SVG) of the running application.

Renders the actual Rich output produced by the toolkit's own render functions
and saves them under ``assets/screenshots/``. Re-run after any UI change:

    .venv/Scripts/python.exe scripts/capture_screenshots.py

The populated views use the bundled fixture scenario (mirrors real ``docker``
JSON output) so the captures are reproducible without a live daemon; the
graceful-degradation view is captured against this host's real state.
"""

from __future__ import annotations

import io
import sys
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from docker_disk_toolkit import analyzer, reporters, scheduling  # noqa: E402
from docker_disk_toolkit.cleaner import SelectionCriteria, build_plan  # noqa: E402
from docker_disk_toolkit.config import ToolkitConfig  # noqa: E402
from docker_disk_toolkit.context import RunContext  # noqa: E402
from docker_disk_toolkit.docker_client import CliDockerClient  # noqa: E402
from docker_disk_toolkit.models import PruneLevel, SpaceDelta  # noqa: E402
from tests.docker_fixtures import make_runner  # noqa: E402

OUT = ROOT / "assets" / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)
CFG = ToolkitConfig()
NOW = datetime(2026, 7, 22, 9, 0, tzinfo=timezone.utc)


def _console() -> Console:
    return Console(record=True, width=96, force_terminal=True, file=io.StringIO())


def capture(name: str, title: str, render) -> None:
    console = _console()
    render(console)
    (OUT / name).write_text(console.export_svg(title=title), encoding="utf-8")
    print(f"wrote {name}")


def main() -> None:
    # Populated dashboard: fixture-backed docker client + this host's real disks.
    client = CliDockerClient(make_runner("typical"), CFG, cli_path="docker")
    ctx = RunContext.create(CFG, docker=client, now=NOW, correlation_id="demo0001")
    report = analyzer.analyze(ctx)
    capture(
        "01_analyze_dashboard.svg",
        "docker-disk analyze",
        lambda c: reporters.render_terminal(report, c),
    )

    # Graceful degradation: real host state (no reachable daemon).
    ctx2 = RunContext.create(CFG, now=NOW, correlation_id="demo0002")
    report2 = analyzer.analyze(ctx2)
    capture(
        "02_graceful_degradation.svg",
        "docker-disk analyze  (no daemon)",
        lambda c: reporters.render_terminal(report2, c),
    )

    # Cleanup plan (level 2, dry-run) — protected volumes spared.
    usage = client.collect_usage()
    analyzer.correlate_usage(usage, CFG.protect_matcher())
    plan = build_plan(
        usage, PruneLevel.AGGRESSIVE, SelectionCriteria.from_config(CFG), now=NOW
    )
    capture(
        "03_cleanup_plan.svg",
        "docker-disk cleanup --level 2 --dry-run",
        lambda c: reporters.render_plan(plan, c),
    )

    # Before/after reclaim delta banner.
    delta = SpaceDelta(
        host_free_before_bytes=144_100_000_000,
        host_free_after_bytes=152_600_000_000,
        docker_total_before_bytes=8_900_000_000,
        docker_total_after_bytes=1_450_000_000,
        reclaimed_bytes=7_450_000_000,
    )
    capture(
        "04_before_after_delta.svg",
        "docker-disk cleanup  (reclaimed)",
        lambda c: reporters.render_delta(delta, c),
    )

    # install-cron generated artifacts (systemd service + timer).
    artifacts = scheduling.generate(
        CFG, action="cleanup", level=PruneLevel.SAFE, schedule="weekly", emit="systemd"
    )

    def render_cron(c: Console) -> None:
        for art in artifacts:
            c.print(
                Panel(
                    Syntax(art.content.strip(), "ini", theme="ansi_dark", word_wrap=True),
                    title=f"[cyan]{art.filename}[/cyan]",
                    border_style="cyan",
                )
            )

    capture("05_install_cron.svg", "docker-disk install-cron --emit systemd", render_cron)


if __name__ == "__main__":
    main()
