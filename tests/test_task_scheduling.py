"""The main loop must never block on the network.

systemd kills the node if the watchdog goes unanswered for 90 seconds, and
rclone is allowed far longer than that for one transfer — 300s for a download,
half an hour by default for an upload. Running either on the main loop cost
this node 21 restarts in a single night, each one killing a transfer in flight
and none of it visible to the test suite, which never called these tasks.

So these tests hold the slow work open and assert the task comes straight back.
"""

from __future__ import annotations

import threading
import time

import pytest

from iot_node.main import Node
from iot_node.remote_config import RemoteConfig

# Generous next to the sub-millisecond a correct implementation takes, and far
# below the 90s watchdog, so this stays meaningful on a loaded Pi.
RETURNS_PROMPTLY_S = 2.0


class BlockingUploader:
    """A pass that is mid-transfer and not coming back yet."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.backend = object()

    def run_once(self) -> tuple[int, int]:
        self.calls += 1
        self.started.set()
        self.release.wait(timeout=30)
        return 1, 0


class FailingUploader(BlockingUploader):
    def run_once(self) -> tuple[int, int]:
        self.calls += 1
        self.started.set()
        raise RuntimeError("the drive went away mid-pass")


class BlockingSync:
    """A config fetch stuck on a slow link."""

    def __init__(self, config: RemoteConfig | None = None) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.config = config or RemoteConfig()
        self.online = True
        self.source = "drive"
        self.pushes = 0
        self.changed = True

    def refresh(self, *, min_interval_s: float = 0.0, force: bool = False) -> bool:
        self.started.set()
        self.release.wait(timeout=30)
        return self.changed

    def push_master_log(self, path) -> bool:
        self.pushes += 1
        return True


@pytest.fixture
def node(cfg):
    built = Node(cfg)
    yield built
    # Let any worker finish so one test's thread cannot fail the next.
    for event in (built._uploading, built._syncing):
        event.clear()


class TestUploadDoesNotBlockTheLoop:
    def test_returns_while_the_pass_is_still_running(self, node) -> None:
        uploader = BlockingUploader()
        node._uploader = uploader
        node._sync = BlockingSync()

        start = time.monotonic()
        node.task_upload()
        elapsed = time.monotonic() - start

        assert uploader.started.wait(timeout=5), "the pass never started"
        assert elapsed < RETURNS_PROMPTLY_S, (
            f"task_upload blocked the main loop for {elapsed:.1f}s while rclone "
            "worked; the watchdog would kill the node at 90s"
        )
        uploader.release.set()

    def test_does_not_start_a_second_pass_over_the_first(self, node) -> None:
        uploader = BlockingUploader()
        node._uploader = uploader
        node._sync = BlockingSync()

        node.task_upload()
        assert uploader.started.wait(timeout=5)
        node._last_upload = 0.0  # make it "due" again
        node.task_upload()

        uploader.release.set()
        time.sleep(0.2)
        assert uploader.calls == 1, "two passes ran at once over the same queue"

    def test_a_failed_pass_does_not_wedge_the_next_one(self, node) -> None:
        failing = FailingUploader()
        node._uploader = failing
        node._sync = BlockingSync()

        node.task_upload()
        assert failing.started.wait(timeout=5)
        # The flag must clear even though run_once raised, or uploads stop
        # forever after one bad night.
        deadline = time.monotonic() + 5
        while node._uploading.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not node._uploading.is_set()

    def test_respects_the_upload_interval(self, node) -> None:
        uploader = BlockingUploader()
        node._uploader = uploader
        node._sync = BlockingSync()
        node.settings = RemoteConfig()

        node.task_upload()
        assert uploader.started.wait(timeout=5)
        uploader.release.set()
        time.sleep(0.2)
        node.task_upload()  # immediately again: not yet due
        time.sleep(0.2)
        assert uploader.calls == 1


class TestConfigSyncDoesNotBlockTheLoop:
    def test_returns_while_the_fetch_is_still_running(self, node) -> None:
        node._uploader = BlockingUploader()
        sync = BlockingSync()
        node._sync = sync

        start = time.monotonic()
        node.task_config()
        elapsed = time.monotonic() - start

        assert sync.started.wait(timeout=5), "the fetch never started"
        assert elapsed < RETURNS_PROMPTLY_S, (
            f"task_config blocked the main loop for {elapsed:.1f}s on the network"
        )
        sync.release.set()

    def test_settings_are_applied_on_the_calling_thread(self, node) -> None:
        """Applying settings reconfigures writers the main loop also uses.

        Doing that from the fetch thread would race the loop, so the fetch only
        parks the result and the loop adopts it on its next turn.
        """
        node._uploader = BlockingUploader()
        sync = BlockingSync()
        node._sync = sync

        applied: list[str] = []
        original = node._apply_settings

        def record(settings) -> None:
            applied.append(threading.current_thread().name)
            original(settings)

        node._apply_settings = record  # type: ignore[method-assign]

        node.task_config()
        sync.release.set()
        deadline = time.monotonic() + 5
        while node._pending_settings is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert node._pending_settings is not None, "the fetch parked nothing"

        # The next turn both adopts the parked settings and starts another
        # fetch; tell the fake there is nothing new, so what remains parked
        # afterwards is unambiguous.
        sync.changed = False
        node.task_config()  # the loop's next turn adopts it
        assert applied == [threading.current_thread().name]
        assert node._pending_settings is None

    def test_does_not_start_a_second_fetch_over_the_first(self, node) -> None:
        node._uploader = BlockingUploader()
        sync = BlockingSync()
        node._sync = sync

        node.task_config()
        assert sync.started.wait(timeout=5)
        node.task_config()
        sync.release.set()
        time.sleep(0.3)
        assert sync.pushes <= 1, "two fetches ran concurrently"
