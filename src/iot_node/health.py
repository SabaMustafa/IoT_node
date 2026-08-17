"""Liveness: telling systemd we are alive, and noticing when we are not.

Two layers, because they fail differently.

**systemd watchdog.** With ``WatchdogSec`` set, systemd expects a ping on the
notify socket at least that often. If the process wedges — a stuck I2C read, a
blocked socket, a deadlock — the pings stop and systemd kills and restarts it.
This catches hangs the process cannot notice about itself, which is exactly the
class of failure that strands a node in a field for a fortnight.

**Task heartbeats.** A process can be perfectly responsive while doing nothing
useful, so each periodic task stamps its own last-success time. If a task falls
badly behind its own schedule, the node stops pinging the watchdog on purpose,
and systemd restarts it. Silence is treated as failure rather than as health.

Nothing here imports systemd bindings; the notify protocol is a few lines of
datagram over a Unix socket, and doing it by hand keeps the dependency list
short on a device that must be rebuildable years from now.
"""

from __future__ import annotations

import logging
import os
import socket
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# How far past its own interval a task may drift before we call it stuck. A
# task that normally runs every 60 s is not in trouble at 70 s; it is in
# trouble at 10 minutes. Generous, because a false restart loses data.
OVERDUE_FACTOR = 8.0
OVERDUE_FLOOR_S = 300.0


class SystemdNotifier:
    """Minimal sd_notify. Silently inert when not run under systemd."""

    def __init__(self, address: str | None = None) -> None:
        self.address = address if address is not None else os.environ.get("NOTIFY_SOCKET", "")
        self._socket: socket.socket | None = None
        if self.address:
            try:
                self._socket = socket.socket(
                    socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC
                )
            except OSError as exc:  # pragma: no cover - platform guard
                log.warning("cannot open the systemd notify socket: %s", exc)
                self._socket = None

    @property
    def enabled(self) -> bool:
        return self._socket is not None and bool(self.address)

    def _send(self, message: str) -> bool:
        if not self.enabled:
            return False
        assert self._socket is not None
        # A leading '@' means an abstract socket, encoded with a NUL byte.
        path = "\0" + self.address[1:] if self.address.startswith("@") else self.address
        try:
            self._socket.sendto(message.encode("utf-8"), path)
            return True
        except OSError as exc:
            log.debug("notify failed: %s", exc)
            return False

    def ready(self, status: str = "") -> bool:
        return self._send(f"READY=1\nSTATUS={status}" if status else "READY=1")

    def ping(self) -> bool:
        return self._send("WATCHDOG=1")

    def status(self, text: str) -> bool:
        return self._send(f"STATUS={text}")

    def stopping(self) -> bool:
        return self._send("STOPPING=1")

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    @staticmethod
    def watchdog_interval_s() -> float | None:
        """Half of WATCHDOG_USEC — the standard ping cadence."""
        raw = os.environ.get("WATCHDOG_USEC")
        if not raw:
            return None
        try:
            return max(1.0, int(raw) / 2_000_000.0)
        except ValueError:
            return None


@dataclass
class TaskHealth:
    name: str
    interval_s: float
    last_success: float = field(default_factory=time.monotonic)
    failures: int = 0

    @property
    def deadline_s(self) -> float:
        return max(self.interval_s * OVERDUE_FACTOR, OVERDUE_FLOOR_S)

    def overdue_by(self, now: float) -> float:
        return (now - self.last_success) - self.deadline_s


class HealthMonitor:
    """Tracks task liveness and drives the systemd watchdog accordingly."""

    def __init__(self, notifier: SystemdNotifier | None = None) -> None:
        self.notifier = notifier or SystemdNotifier()
        self.tasks: dict[str, TaskHealth] = {}
        self.started = time.monotonic()
        self.unhealthy_since: float | None = None

    def register(self, name: str, interval_s: float) -> None:
        self.tasks[name] = TaskHealth(name=name, interval_s=interval_s)

    def set_interval(self, name: str, interval_s: float) -> None:
        task = self.tasks.get(name)
        if task is not None:
            task.interval_s = interval_s

    def ok(self, name: str) -> None:
        task = self.tasks.get(name)
        if task is not None:
            task.last_success = time.monotonic()
            task.failures = 0

    def failed(self, name: str) -> None:
        """A task raised. Not itself fatal — only prolonged silence is."""
        task = self.tasks.get(name)
        if task is not None:
            task.failures += 1

    def stuck_tasks(self, now: float | None = None) -> list[TaskHealth]:
        now = now if now is not None else time.monotonic()
        return [task for task in self.tasks.values() if task.overdue_by(now) > 0]

    @property
    def uptime_s(self) -> float:
        return time.monotonic() - self.started

    def pump(self, status: str = "") -> bool:
        """Call often. Pings the watchdog only while everything is on schedule.

        Returns True if healthy. When unhealthy we deliberately stop pinging:
        systemd then restarts us, which is the whole point — a node that has
        quietly stopped sampling is no more use than one that has crashed.
        """
        stuck = self.stuck_tasks()
        if stuck:
            if self.unhealthy_since is None:
                self.unhealthy_since = time.monotonic()
                for task in stuck:
                    log.error(
                        "task %r has not succeeded for %.0fs (limit %.0fs) — withholding "
                        "the watchdog ping so systemd restarts us",
                        task.name, time.monotonic() - task.last_success, task.deadline_s,
                    )
            self.notifier.status(f"UNHEALTHY: {', '.join(t.name for t in stuck)} stalled")
            return False

        if self.unhealthy_since is not None:
            log.info("all tasks healthy again after %.0fs", time.monotonic() - self.unhealthy_since)
            self.unhealthy_since = None

        self.notifier.ping()
        if status:
            self.notifier.status(status)
        return True

    def report(self) -> dict[str, float]:
        now = time.monotonic()
        return {name: now - task.last_success for name, task in self.tasks.items()}
