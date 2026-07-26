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
from docker_disk_toolkit.monitor import OsaScriptNotifier
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


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class TestWebhookNotifier:
    """The webhook was configured and documented but never actually sent."""

    def _notifier(self, responses: list[object], **kwargs) -> tuple[monitor.WebhookNotifier, list]:
        calls: list = []

        def opener(request, timeout=None):
            calls.append((request, timeout))
            outcome = responses[min(len(calls) - 1, len(responses) - 1)]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        notifier = monitor.WebhookNotifier(
            "https://hooks.example.com/abc",
            opener=opener,
            sleep_fn=lambda _s: None,
            **kwargs,
        )
        return notifier, calls

    def test_posts_json_payload(self) -> None:
        notifier, calls = self._notifier([_FakeResponse(200)])
        assert notifier.send("Docker disk CRITICAL", "2 GB free", "critical") is True
        request, timeout = calls[0]
        assert request.method == "POST"
        assert request.get_header("Content-type") == "application/json"
        assert timeout == monitor.WEBHOOK_TIMEOUT_S
        body = json.loads(request.data.decode("utf-8"))
        assert body["severity"] == "critical"
        assert "2 GB free" in body["text"]

    def test_retries_then_succeeds(self) -> None:
        notifier, calls = self._notifier([OSError("connection refused"), _FakeResponse(200)])
        assert notifier.send("t", "b", "critical") is True
        assert len(calls) == 2

    def test_gives_up_after_configured_attempts(self) -> None:
        notifier, calls = self._notifier([OSError("down")], attempts=3)
        assert notifier.send("t", "b", "critical") is False
        assert len(calls) == 3

    def test_non_2xx_is_failure(self) -> None:
        notifier, _ = self._notifier([_FakeResponse(500)], attempts=1)
        assert notifier.send("t", "b", "critical") is False

    def test_never_raises_so_log_sink_still_runs(self) -> None:
        notifier, _ = self._notifier([RuntimeError("boom")], attempts=1)
        assert notifier.send("t", "b", "critical") is False

    def test_composite_includes_webhook_when_configured(self) -> None:
        config = NotificationConfig(
            enabled=True, webhook_url="https://hooks.example.com/abc", desktop=False
        )
        composite = monitor.get_notifier(config)
        assert composite._webhook is not None

    def test_composite_omits_webhook_when_unset(self) -> None:
        composite = monitor.get_notifier(NotificationConfig(enabled=True, desktop=False))
        assert composite._webhook is None


class TestWebhookUrlValidation:
    @pytest.mark.parametrize(
        "url", ["file:///etc/passwd", "ftp://example.com/x", "not-a-url", "//example.com"]
    )
    def test_rejects_non_http_schemes(self, url: str) -> None:
        with pytest.raises(Exception):
            NotificationConfig(webhook_url=url)

    @pytest.mark.parametrize("url", ["http://localhost:9000/hook", "https://hooks.slack.com/x"])
    def test_accepts_http_and_https(self, url: str) -> None:
        assert NotificationConfig(webhook_url=url).webhook_url is not None

    def test_error_message_does_not_leak_the_url(self) -> None:
        secret = "ftp://user:pa55w0rd@internal.example.com/hook"
        with pytest.raises(Exception) as excinfo:
            NotificationConfig(webhook_url=secret)
        assert "pa55w0rd" not in str(excinfo.value)


class TestNotifierQuoting:
    """Defence in depth: send() accepts an arbitrary title/body."""

    def test_powershell_quote_doubles_single_quotes(self) -> None:
        assert monitor.WinToastNotifier._quote("it's") == "it''s"

    def test_powershell_injection_cannot_escape_literal(self) -> None:
        runner = FixtureCommandRunner([], default=result(""))
        evil = "'; Remove-Item C:\\ -Recurse -Force; '"
        monitor.WinToastNotifier(runner).send("Docker disk CRITICAL", evil, "critical")

        script = runner.calls[0][-1]
        # The payload is embedded with every quote doubled, so it stays data.
        assert "'" + evil.replace("'", "''") + "'" in script
        # Balanced quotes => no string literal was terminated early.
        assert script.count("'") % 2 == 0

    def test_applescript_quote_escapes_quotes_and_backslashes(self) -> None:
        assert OsaScriptNotifier._quote('a"b') == 'a\\"b'
        assert OsaScriptNotifier._quote("a\\b") == "a\\\\b"

    def test_quote_flattens_newlines(self) -> None:
        assert "\n" not in OsaScriptNotifier._quote("line1\nline2")
        assert "\r" not in OsaScriptNotifier._quote("line1\r\nline2")

    def test_malicious_volume_name_cannot_break_out(self) -> None:
        from docker_disk_toolkit.utils import CommandResult, FixtureCommandRunner

        runner = FixtureCommandRunner([], default=CommandResult(["osascript"], 0, "", "", 0.0))
        evil = 'x" with title "pwned" ignoring application responses --'
        OsaScriptNotifier(runner).send("Docker disk CRITICAL", evil, "critical")

        script = runner.calls[0][-1]
        # Exactly two unescaped quote pairs remain: the body and the title.
        unescaped = script.replace('\\"', "")
        assert unescaped.count('"') == 4
        assert "ignoring application responses" not in unescaped.split('"')[0]
