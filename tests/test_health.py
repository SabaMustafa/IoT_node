from __future__ import annotations

import socket
import tempfile
from pathlib import Path

from iot_node.health import OVERDUE_FLOOR_S, HealthMonitor, SystemdNotifier


class FakeNotifier(SystemdNotifier):
    """Records what would have been sent, without a socket."""

    def __init__(self) -> None:
        self.address = ""
        self._socket = None
        self.messages: list[str] = []

    @property
    def enabled(self) -> bool:
        return True

    def _send(self, message: str) -> bool:
        self.messages.append(message)
        return True

    @property
    def pings(self) -> int:
        return sum(1 for m in self.messages if m == "WATCHDOG=1")


# --- notifier ----------------------------------------------------------------


def test_notifier_is_inert_without_systemd(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    notifier = SystemdNotifier()
    assert not notifier.enabled
    assert notifier.ping() is False  # must not raise off systemd


def test_notifier_sends_over_a_real_socket():
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "notify.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        server.bind(path)
        server.settimeout(2)
        try:
            notifier = SystemdNotifier(path)
            assert notifier.enabled
            assert notifier.ready("hello")
            assert server.recv(256) == b"READY=1\nSTATUS=hello"
            assert notifier.ping()
            assert server.recv(256) == b"WATCHDOG=1"
        finally:
            server.close()
            notifier.close()


def test_watchdog_interval_is_half_the_configured_timeout(monkeypatch):
    monkeypatch.setenv("WATCHDOG_USEC", "30000000")  # 30 s
    assert SystemdNotifier.watchdog_interval_s() == 15.0

    monkeypatch.setenv("WATCHDOG_USEC", "rubbish")
    assert SystemdNotifier.watchdog_interval_s() is None

    monkeypatch.delenv("WATCHDOG_USEC")
    assert SystemdNotifier.watchdog_interval_s() is None


# --- health monitor ----------------------------------------------------------


def test_a_healthy_node_pings_the_watchdog():
    notifier = FakeNotifier()
    monitor = HealthMonitor(notifier)
    monitor.register("sample", 60)

    assert monitor.pump() is True
    assert notifier.pings == 1


def test_a_stalled_task_withholds_the_ping():
    """This is the whole mechanism: silence is how systemd learns to restart us."""
    notifier = FakeNotifier()
    monitor = HealthMonitor(notifier)
    monitor.register("sample", 60)

    # Backdate the last success well past the deadline.
    monitor.tasks["sample"].last_success -= OVERDUE_FLOOR_S * 2

    assert monitor.pump() is False
    assert notifier.pings == 0
    assert any("UNHEALTHY" in m for m in notifier.messages)


def test_recovery_resumes_pinging():
    notifier = FakeNotifier()
    monitor = HealthMonitor(notifier)
    monitor.register("sample", 60)
    monitor.tasks["sample"].last_success -= OVERDUE_FLOOR_S * 2
    assert monitor.pump() is False

    monitor.ok("sample")

    assert monitor.pump() is True
    assert notifier.pings == 1


def test_a_slow_task_is_not_mistaken_for_a_stalled_one():
    """A 60 s task at 90 s is late, not broken. False restarts lose data."""
    monitor = HealthMonitor(FakeNotifier())
    monitor.register("sample", 60)
    monitor.tasks["sample"].last_success -= 90

    assert monitor.stuck_tasks() == []
    assert monitor.pump() is True


def test_long_interval_tasks_use_their_own_deadline():
    monitor = HealthMonitor(FakeNotifier())
    monitor.register("config", 3600)
    monitor.tasks["config"].last_success -= 3600 * 4  # late but within 8x

    assert monitor.stuck_tasks() == []

    monitor.tasks["config"].last_success -= 3600 * 6  # now well past
    assert [t.name for t in monitor.stuck_tasks()] == ["config"]


def test_short_interval_tasks_get_a_floor_not_a_hair_trigger():
    """A 5 s status task must not trip the watchdog after 40 s."""
    monitor = HealthMonitor(FakeNotifier())
    monitor.register("status", 5)
    monitor.tasks["status"].last_success -= 60

    assert monitor.stuck_tasks() == []
    assert monitor.tasks["status"].deadline_s == OVERDUE_FLOOR_S


def test_failures_are_counted_but_not_immediately_fatal():
    monitor = HealthMonitor(FakeNotifier())
    monitor.register("upload", 300)
    for _ in range(5):
        monitor.failed("upload")

    assert monitor.tasks["upload"].failures == 5
    assert monitor.pump() is True  # transient failures alone do not restart us


def test_success_clears_the_failure_count():
    monitor = HealthMonitor(FakeNotifier())
    monitor.register("upload", 300)
    monitor.failed("upload")
    monitor.ok("upload")
    assert monitor.tasks["upload"].failures == 0


def test_interval_changes_move_the_deadline():
    monitor = HealthMonitor(FakeNotifier())
    monitor.register("sample", 60)
    monitor.set_interval("sample", 600)
    assert monitor.tasks["sample"].deadline_s == 600 * 8


def test_unknown_task_names_are_ignored():
    monitor = HealthMonitor(FakeNotifier())
    monitor.ok("nope")
    monitor.failed("nope")
    monitor.set_interval("nope", 5)
    assert monitor.tasks == {}


def test_report_lists_seconds_since_each_success():
    monitor = HealthMonitor(FakeNotifier())
    monitor.register("sample", 60)
    report = monitor.report()
    assert set(report) == {"sample"}
    assert report["sample"] >= 0
