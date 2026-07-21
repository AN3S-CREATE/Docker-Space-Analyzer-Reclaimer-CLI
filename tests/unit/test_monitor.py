"""Unit tests for :mod:`docker_disk_toolkit.monitor`."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from docker_disk_toolkit import monitor
from docker_disk_toolkit.config import NotificationConfig, ToolkitConfig
from docker_disk_toolkit.context import RunContext
from docker_disk_toolkit.models import (
    Breach,
    DiagnosticReport,
    DiskUsage,
    DockerAvailability,
    DockerProbe,
    DockerUsage,
    HealthStatus,
    MonitorReading,
)
from docker_disk_toolkit.utils import FixtureCommandRunner, result

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


def _report(free_gb: float, *, docker_bytes: int = 0) -> DiagnosticReport:
    disk = DiskUsage(
        mountpoint="/",
        total_bytes=100 * monitor.GB,
        used_bytes=int((100 - free_gb) * monitor.GB),
        free_bytes=int(free_gb * monitor.GB),
        percent_used=(100 - free_gb),
        is_docker_root=True,
    )
    from docker_disk_toolkit.models import CategoryUsage

    docker = None
    if docker_bytes:
        docker = DockerUsage(images=CategoryUsage(total_bytes=docker_bytes))
    return DiagnosticReport(
        correlation_id="c",
        generated_at=NOW,
        hostname="h",
        os="o",
        docker_probe=DockerProbe(availability=DockerAvailability.OK),
        disks=[disk],
        docker=docker,
        health=HealthStatus.HEALTHY,
    )


class TestEvaluate:
    def test_healthy(self) -> None:
        metrics, breaches = monitor.evaluate(_report(50), ToolkitConfig().thresholds)
        assert metrics["host_free_gb"] == 50.0
        assert breaches == []

    def test_warning_breach(self) -> None:
        _, breaches = monitor.evaluate(_report(8), ToolkitConfig().thresholds)
        assert any(b.level is HealthStatus.WARNING for b in breaches)

    def test_critical_breach(self) -> None:
        _, breaches = monitor.evaluate(_report(2), ToolkitConfig().thresholds)
        assert any(b.level is HealthStatus.CRITICAL for b in breaches)

    def test_docker_percent_breach(self) -> None:
        report = _report(50, docker_bytes=80 * monitor.GB)
        metrics, breaches = monitor.evaluate(report, ToolkitConfig().thresholds)
        assert metrics["docker_percent"] == 80.0
        assert any(b.metric == "docker_percent" for b in breaches)


class TestNotifiers:
    def test_log_notifier_always_available(self) -> None:
        n = monitor.LogNotifier()
        assert n.available() and n.send("t", "b", "warning")

    def test_composite_falls_back_to_log(self) -> None:
        class Boom(monitor.Notifier):
            name = "boom"

            def available(self) -> bool:
                return True

            def send(self, title: str, body: str, urgency: str) -> bool:
                raise RuntimeError("no display")

        composite = monitor.CompositeNotifier(Boom())
        # never raises; log fallback keeps it safe
        assert composite.send("t", "b", "critical") is False

    def test_notify_send_uses_runner(self) -> None:
        runner = FixtureCommandRunner([(["notify-send"], result("ok"))])
        n = monitor.NotifySendNotifier(runner)
        assert n.send("Title", "Body", "critical") is True
        assert runner.calls[-1][:2] == ["notify-send", "-u"]

    def test_desktop_disabled_skips_primary(self) -> None:
        class Track(monitor.Notifier):
            name = "track"
            called = False

            def available(self) -> bool:
                return True

            def send(self, title: str, body: str, urgency: str) -> bool:
                Track.called = True
                return True

        composite = monitor.CompositeNotifier(Track(), desktop_enabled=False)
        composite.send("t", "b", "warning")
        assert Track.called is False


class TestMetrics:
    def _reading(self) -> MonitorReading:
        return MonitorReading(
            ts=NOW,
            correlation_id="c",
            metrics={"host_free_gb": 42.0, "docker_total_bytes": 1000.0},
            breaches=[
                Breach(
                    metric="host_free_gb",
                    level=HealthStatus.WARNING,
                    value=8.0,
                    threshold=10.0,
                    message="low",
                )
            ],
            health=HealthStatus.WARNING,
        )

    def test_render_prometheus(self) -> None:
        text = monitor.render_prometheus(self._reading())
        assert "docker_disk_host_free_gb 42.0" in text
        assert "docker_disk_health 1.0" in text
        assert "docker_disk_breaches 1.0" in text
        assert "# TYPE docker_disk_health gauge" in text

    def test_write_metrics_ring(self, make_config: Callable[..., ToolkitConfig]) -> None:
        config = make_config()
        for _ in range(3):
            monitor.write_metrics(config, self._reading(), ring_size=2)
        data = json.loads(config.metrics_path.read_text())
        assert len(data["history"]) == 2  # ring bounded
        assert data["latest"]["health"] == "warning"

    def test_write_prometheus_file(
        self, make_config: Callable[..., ToolkitConfig], tmp_path
    ) -> None:
        target = tmp_path / "docker.prom"
        monitor.write_prometheus(self._reading(), target)
        assert "docker_disk_health" in target.read_text()


class TestCheckOnce:
    @pytest.fixture
    def ctx_factory(
        self,
        patched_disks,
        make_config: Callable[..., ToolkitConfig],
        cli_client: Callable[[str], object],
    ):
        def _factory(scenario: str = "not-installed", **overrides):
            config = make_config(**overrides)
            return RunContext.create(
                config, docker=cli_client(scenario), now=NOW, correlation_id="moncid"
            )

        return _factory

    def test_check_once_writes_metrics(self, ctx_factory) -> None:
        ctx = ctx_factory("not-installed")
        reading = monitor.check_once(ctx, notify=False)
        assert ctx.config.metrics_path.exists()
        assert reading.correlation_id == "moncid"

    def test_notify_on_breach(self, patched_disks, make_config, cli_client) -> None:
        # low disk forces a critical health; notifications enabled
        low = [
            DiskUsage(
                mountpoint="/",
                total_bytes=100 * monitor.GB,
                used_bytes=98 * monitor.GB,
                free_bytes=2 * monitor.GB,
                percent_used=98.0,
                is_docker_root=True,
            )
        ]
        patched_disks(low)
        config = make_config(
            notifications=NotificationConfig(enabled=True, on_events={HealthStatus.CRITICAL})
        )
        ctx = RunContext.create(config, docker=cli_client("not-installed"), now=NOW)

        sent: list[tuple[str, str, str]] = []

        class Spy(monitor.Notifier):
            def available(self) -> bool:
                return True

            def send(self, title: str, body: str, urgency: str) -> bool:
                sent.append((title, body, urgency))
                return True

        monitor.check_once(ctx, notifier=Spy())
        assert sent and sent[0][2] == "critical"

    def test_watch_bounded(self, ctx_factory) -> None:
        ctx = ctx_factory("not-installed")
        readings = monitor.watch(ctx, interval_seconds=0, max_iterations=3, sleep_fn=lambda s: None)
        assert len(readings) == 3
