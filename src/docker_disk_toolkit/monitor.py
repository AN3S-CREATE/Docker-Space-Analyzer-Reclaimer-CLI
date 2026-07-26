"""Space watchdog — threshold evaluation, notifications, and metrics.

``check_once`` runs a single evaluation (for cron/``--once``); ``watch`` loops
for interactive use. Notifications use a platform strategy (notify-send / a
Windows toast / osascript) that **always** falls back to a log sink, so a
missed desktop toast is never a silent failure. Metrics are written atomically
as JSON and, optionally, in node_exporter textfile (Prometheus) format.
"""

from __future__ import annotations

import json
import time
import urllib.request
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import structlog

from . import analyzer, system_info
from .config import NotificationConfig, Thresholds, ToolkitConfig
from .context import RunContext
from .models import Breach, DiagnosticReport, HealthStatus, MonitorReading
from .utils import CommandRunner, atomic_write_text, default_runner, is_wsl

if TYPE_CHECKING:  # pragma: no cover
    from pathlib import Path

GB = 1000**3
_log = structlog.get_logger("docker_disk_toolkit.monitor")


# ---------------------------------------------------------------------------
# Threshold evaluation
# ---------------------------------------------------------------------------


def evaluate(
    report: DiagnosticReport, thresholds: Thresholds
) -> tuple[dict[str, float], list[Breach]]:
    """Compute the metric dict and threshold breaches for a report."""
    metrics: dict[str, float] = {}
    breaches: list[Breach] = []

    primary = report.docker_root_disk
    if primary is not None:
        metrics["host_free_bytes"] = float(primary.free_bytes)
        metrics["host_free_gb"] = round(primary.free_bytes / GB, 2)
        metrics["host_used_ratio"] = round(primary.percent_used / 100.0, 4)

    for disk in report.disks:
        free_gb = disk.free_bytes / GB
        if free_gb < thresholds.critical_free_gb:
            breaches.append(
                Breach(
                    metric="host_free_gb",
                    level=HealthStatus.CRITICAL,
                    value=round(free_gb, 2),
                    threshold=thresholds.critical_free_gb,
                    message=f"{disk.mountpoint} critically low: {free_gb:.1f} GB free",
                )
            )
        elif free_gb < thresholds.min_free_gb:
            breaches.append(
                Breach(
                    metric="host_free_gb",
                    level=HealthStatus.WARNING,
                    value=round(free_gb, 2),
                    threshold=thresholds.min_free_gb,
                    message=f"{disk.mountpoint} low on space: {free_gb:.1f} GB free",
                )
            )

    if report.docker is not None:
        metrics["docker_total_bytes"] = float(report.docker.total_bytes)
        metrics["docker_reclaimable_bytes"] = float(report.docker.reclaimable_bytes)
        if primary is not None and primary.total_bytes > 0:
            docker_pct = report.docker.total_bytes / primary.total_bytes * 100
            metrics["docker_percent"] = round(docker_pct, 2)
            if docker_pct > thresholds.max_docker_percent:
                breaches.append(
                    Breach(
                        metric="docker_percent",
                        level=HealthStatus.WARNING,
                        value=round(docker_pct, 2),
                        threshold=thresholds.max_docker_percent,
                        message=f"Docker using {docker_pct:.0f}% of {primary.mountpoint}",
                    )
                )
    return metrics, breaches


# ---------------------------------------------------------------------------
# Notifiers
# ---------------------------------------------------------------------------


class Notifier:
    """Base notifier interface."""

    name = "base"

    def available(self) -> bool:  # pragma: no cover - overridden
        return False

    def send(self, title: str, body: str, urgency: str) -> bool:  # pragma: no cover
        return False


class LogNotifier(Notifier):
    """Always-available sink that logs the notification."""

    name = "log"

    def available(self) -> bool:
        return True

    def send(self, title: str, body: str, urgency: str) -> bool:
        _log.warning("notification", title=title, body=body, urgency=urgency)
        return True


class _RunnerNotifier(Notifier):
    """A notifier that shells out via an injected runner (testable)."""

    def __init__(self, runner: CommandRunner) -> None:
        self._runner = runner


class NotifySendNotifier(_RunnerNotifier):
    """Linux desktop notifications via ``notify-send``."""

    name = "notify-send"

    def available(self) -> bool:
        import shutil

        return shutil.which("notify-send") is not None

    def send(self, title: str, body: str, urgency: str) -> bool:
        level = {"critical": "critical", "warning": "normal"}.get(urgency, "low")
        res = self._runner.run(["notify-send", "-u", level, title, body], timeout=10)
        return res.ok


class OsaScriptNotifier(_RunnerNotifier):
    """macOS desktop notifications via ``osascript``."""

    name = "osascript"

    def available(self) -> bool:
        import shutil

        return shutil.which("osascript") is not None

    @staticmethod
    def _quote(value: str) -> str:
        """Escape ``value`` for embedding in an AppleScript string literal.

        Today's breach messages only interpolate mountpoints and numbers, so
        this is defence in depth rather than a live exploit. It matters because
        :meth:`send` takes an arbitrary title/body: any future caller that
        passes a Docker object name (which anyone able to create a volume
        chooses) would otherwise be able to terminate the literal and append
        further AppleScript. The argv is passed without a shell, so the blast
        radius is the AppleScript statement, not the shell.
        """
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return escaped.replace("\r", " ").replace("\n", " ")

    def send(self, title: str, body: str, urgency: str) -> bool:
        script = f'display notification "{self._quote(body)}" ' f'with title "{self._quote(title)}"'
        res = self._runner.run(["osascript", "-e", script], timeout=10)
        return res.ok


class WinToastNotifier(_RunnerNotifier):
    """Windows toast via PowerShell (works from WSL via ``powershell.exe``)."""

    name = "win-toast"

    def available(self) -> bool:
        return system_info.is_windows() or is_wsl()

    @staticmethod
    def _quote(value: str) -> str:
        """Escape ``value`` for a PowerShell single-quoted string literal.

        Higher stakes than the AppleScript equivalent: this string is handed to
        ``powershell -Command``, so terminating the literal would yield
        arbitrary PowerShell execution rather than a malformed notification.
        Inside single quotes PowerShell treats everything literally, and a
        quote is escaped by doubling it.
        """
        return value.replace("'", "''").replace("\r", " ").replace("\n", " ")

    def send(self, title: str, body: str, urgency: str) -> bool:
        powershell = "powershell.exe" if is_wsl() else "powershell"
        script = (
            "[reflection.assembly]::loadwithpartialname('System.Windows.Forms') | Out-Null; "
            "$n = New-Object System.Windows.Forms.NotifyIcon; "
            "$n.Icon = [System.Drawing.SystemIcons]::Information; $n.Visible = $true; "
            f"$n.ShowBalloonTip(10000, '{self._quote(title)}', '{self._quote(body)}', 'Warning')"
        )
        res = self._runner.run([powershell, "-NoProfile", "-Command", script], timeout=15)
        return res.ok


WEBHOOK_TIMEOUT_S = 10.0
WEBHOOK_ATTEMPTS = 3
_WEBHOOK_BACKOFF_S = 0.5


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so the payload cannot be replayed to another host."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class WebhookNotifier(Notifier):
    """POST a JSON alert to an operator-configured webhook.

    Built on :mod:`urllib` rather than a new HTTP dependency: this is a
    single fire-and-forget POST and the toolkit is deliberately light.

    The URL is a :class:`~pydantic.SecretStr` and is **never** logged — a Slack
    or Teams webhook URL is itself the credential. Failures are logged without
    it and swallowed, because :class:`CompositeNotifier` guarantees the log sink
    still records the alert.
    """

    name = "webhook"

    def __init__(
        self,
        url: str,
        *,
        timeout_s: float = WEBHOOK_TIMEOUT_S,
        attempts: int = WEBHOOK_ATTEMPTS,
        opener: Callable[..., Any] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._url = url
        self._timeout_s = timeout_s
        self._attempts = max(1, attempts)
        self._opener = opener or urllib.request.build_opener(_NoRedirect).open
        self._sleep = sleep_fn

    def available(self) -> bool:
        return bool(self._url)

    def payload(self, title: str, body: str, urgency: str) -> dict[str, Any]:
        """Build the JSON body. ``text`` keeps Slack/Teams working out of the box."""
        return {
            "text": f"{title}: {body}",
            "title": title,
            "body": body,
            "severity": urgency,
            "source": "docker-disk-toolkit",
        }

    def send(self, title: str, body: str, urgency: str) -> bool:
        data = json.dumps(self.payload(title, body, urgency)).encode("utf-8")
        request = urllib.request.Request(
            self._url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        for attempt in range(1, self._attempts + 1):
            try:
                with self._opener(request, timeout=self._timeout_s) as response:
                    status = int(getattr(response, "status", 0) or 0)
                if 200 <= status < 300:
                    return True
                _log.warning("webhook_bad_status", status=status, attempt=attempt)
            except Exception as exc:
                # Deliberately excludes the URL: it is the credential.
                _log.warning("webhook_failed", error=type(exc).__name__, attempt=attempt)
            if attempt < self._attempts:
                self._sleep(_WEBHOOK_BACKOFF_S * attempt)
        return False


class CompositeNotifier(Notifier):
    """Try a platform notifier, then always fall through to the log sink."""

    name = "composite"

    def __init__(
        self,
        primary: Notifier | None,
        *,
        desktop_enabled: bool = True,
        webhook: Notifier | None = None,
    ) -> None:
        self._primary = primary
        self._log = LogNotifier()
        self._desktop_enabled = desktop_enabled
        self._webhook = webhook

    def available(self) -> bool:
        return True

    def send(self, title: str, body: str, urgency: str) -> bool:
        delivered = False
        if self._desktop_enabled and self._primary is not None:
            try:
                delivered = self._primary.send(title, body, urgency)
            except Exception as exc:
                _log.warning("desktop_notify_failed", notifier=self._primary.name, error=str(exc))
        if self._webhook is not None:
            try:
                delivered = self._webhook.send(title, body, urgency) or delivered
            except Exception as exc:  # pragma: no cover - WebhookNotifier is total
                _log.warning("webhook_notify_failed", error=type(exc).__name__)
        # The log sink always runs so a failed toast is never silent.
        self._log.send(title, body, urgency)
        return delivered


def get_notifier(
    config: NotificationConfig, *, runner: CommandRunner | None = None
) -> CompositeNotifier:
    """Select the platform notifier and wrap it with the log fallback."""
    runner = runner or default_runner()
    primary: Notifier | None = None
    for candidate in (
        NotifySendNotifier(runner),
        OsaScriptNotifier(runner),
        WinToastNotifier(runner),
    ):
        if candidate.available():
            primary = candidate
            break
    webhook: Notifier | None = None
    if config.webhook_url is not None:
        webhook = WebhookNotifier(config.webhook_url.get_secret_value())
    return CompositeNotifier(primary, desktop_enabled=config.desktop, webhook=webhook)


# ---------------------------------------------------------------------------
# Metrics persistence & Prometheus
# ---------------------------------------------------------------------------


def render_prometheus(reading: MonitorReading) -> str:
    """Render a reading in node_exporter textfile (Prometheus) format."""
    lines: list[str] = []

    def gauge(name: str, value: float, help_text: str) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} {value}")

    for key, value in reading.metrics.items():
        gauge(f"docker_disk_{key}", value, f"docker-disk-toolkit {key}")
    health_value = {"healthy": 0, "warning": 1, "critical": 2, "unknown": -1}[reading.health.value]
    gauge("docker_disk_health", float(health_value), "0=healthy 1=warning 2=critical -1=unknown")
    gauge("docker_disk_breaches", float(len(reading.breaches)), "active threshold breaches")
    return "\n".join(lines) + "\n"


def write_metrics(config: ToolkitConfig, reading: MonitorReading, *, ring_size: int = 50) -> Path:
    """Write the latest reading (+ a small history ring) to ``metrics.json``."""
    path = config.metrics_path
    history: list[dict[str, Any]] = []
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            history = existing.get("history", [])
        except (OSError, json.JSONDecodeError):
            history = []
    latest = json.loads(reading.model_dump_json())
    history.append(latest)
    history = history[-ring_size:]
    atomic_write_text(path, json.dumps({"latest": latest, "history": history}, indent=2))
    return path


def write_prometheus(reading: MonitorReading, path: Path) -> None:
    """Write a reading to a Prometheus textfile atomically."""
    atomic_write_text(path, render_prometheus(reading))


# ---------------------------------------------------------------------------
# Watchdog entry points
# ---------------------------------------------------------------------------


def check_once(
    ctx: RunContext,
    *,
    notify: bool = True,
    notifier: Notifier | None = None,
    prometheus_path: Path | None = None,
) -> MonitorReading:
    """Run a single watchdog evaluation, persist metrics, and notify on breach."""
    config = ctx.config
    report = analyzer.analyze(ctx)
    metrics, breaches = evaluate(report, config.thresholds)
    reading = MonitorReading(
        ts=ctx.now,
        correlation_id=ctx.correlation_id,
        metrics=metrics,
        breaches=breaches,
        health=report.health,
    )
    write_metrics(config, reading)
    if prometheus_path is not None:
        write_prometheus(reading, prometheus_path)

    if notify and config.notifications.enabled and report.health in config.notifications.on_events:
        active = notifier or get_notifier(config.notifications)
        title = f"Docker disk {report.health.value.upper()}"
        body = "; ".join(b.message for b in breaches) or "Threshold breach detected."
        active.send(title, body, report.health.value)
    return reading


def watch(
    ctx: RunContext,
    *,
    interval_seconds: float,
    max_iterations: int | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> list[MonitorReading]:
    """Loop the watchdog every ``interval_seconds`` (bounded for tests)."""
    readings: list[MonitorReading] = []
    iteration = 0
    while max_iterations is None or iteration < max_iterations:
        readings.append(check_once(ctx))
        iteration += 1
        if max_iterations is not None and iteration >= max_iterations:
            break
        sleep_fn(interval_seconds)
    return readings
