from __future__ import annotations

import os
import time
from pathlib import Path

from iot_node.cloud.backend import UploadError
from iot_node.cloud.config_sync import (
    SOURCE_BACKUP,
    SOURCE_CACHE,
    SOURCE_DEFAULT,
    SOURCE_DRIVE,
    ConfigSync,
)
from iot_node.status import HISTORY_LIMIT, STALE_AFTER_S, StatusWriter, read_status

GOOD_INI = """
[schedule]
sensors    = 111111111111111111111111
microphone = 100100100100100100100100
[audio]
sample_rate = 48000
[upload]
frequency = 6h
"""

OTHER_INI = GOOD_INI.replace("sample_rate = 48000", "sample_rate = 96000")

# Parses, but every value is junk — exactly what a fat-fingered phone edit looks like.
BROKEN_INI = """
[schedule]
sensors = banana
[audio]
sample_rate = fast
"""


class FakeBackend:
    """A drive that can be made to hold anything, or to be unreachable."""

    name = "fake"

    def __init__(self, files: dict[str, str] | None = None) -> None:
        self.files = dict(files or {})
        self.offline = False
        self.uploaded: list[str] = []

    def _guard(self) -> None:
        if self.offline:
            raise UploadError("network is down", retryable=True)

    def check(self) -> str:
        self._guard()
        return "fake drive"

    def upload(self, local_path: Path, remote_path: str) -> dict:
        self._guard()
        self.files[remote_path] = local_path.read_text()
        self.uploaded.append(remote_path)
        return {"size": local_path.stat().st_size}

    def download(self, remote_path: str, local_path: Path) -> bool:
        self._guard()
        if remote_path not in self.files:
            return False
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(self.files[remote_path])
        return True

    def remote_size(self, remote_path: str) -> int | None:
        return len(self.files.get(remote_path, "")) or None

    def close(self) -> None:
        pass


def make_sync(tmp_path, files=None) -> tuple[ConfigSync, FakeBackend]:
    backend = FakeBackend(files)
    return ConfigSync(backend, "IoT", tmp_path / "state"), backend


# --- fetching ----------------------------------------------------------------


def test_fetches_and_caches_the_drive_copy(tmp_path):
    sync, _ = make_sync(tmp_path, {"IoT/config.ini": GOOD_INI})

    assert sync.refresh(force=True) is True
    assert sync.source == SOURCE_DRIVE
    assert sync.config.sample_rate == 48000
    assert sync.cache_path.is_file()
    assert sync.backup_path.is_file()  # a clean parse becomes the known-good copy


def test_an_unchanged_file_reports_no_change(tmp_path):
    sync, _ = make_sync(tmp_path, {"IoT/config.ini": GOOD_INI})
    assert sync.refresh(force=True) is True
    assert sync.refresh(force=True) is False


def test_an_edited_file_is_picked_up(tmp_path):
    sync, backend = make_sync(tmp_path, {"IoT/config.ini": GOOD_INI})
    sync.refresh(force=True)

    backend.files["IoT/config.ini"] = OTHER_INI

    assert sync.refresh(force=True) is True
    assert sync.config.sample_rate == 96000


def test_a_missing_remote_file_is_seeded_with_the_default(tmp_path):
    sync, backend = make_sync(tmp_path)
    sync.refresh(force=True)

    assert "IoT/config.ini" in backend.files
    assert "[schedule]" in backend.files["IoT/config.ini"]
    assert sync.config.sample_rate == 48000


def test_refresh_respects_its_minimum_interval(tmp_path):
    sync, _ = make_sync(tmp_path, {"IoT/config.ini": GOOD_INI})
    sync.refresh(force=True)
    assert sync.refresh(min_interval_s=3600) is False


# --- offline behaviour -------------------------------------------------------


def test_going_offline_keeps_the_last_settings(tmp_path):
    """The whole point: a lost link must not change what the node does."""
    sync, backend = make_sync(tmp_path, {"IoT/config.ini": OTHER_INI})
    sync.refresh(force=True)
    assert sync.config.sample_rate == 96000

    backend.offline = True

    assert sync.refresh(force=True) is False
    assert sync.config.sample_rate == 96000  # unchanged
    assert sync.online is False


def test_a_restart_offline_loads_the_cached_copy(tmp_path):
    sync, backend = make_sync(tmp_path, {"IoT/config.ini": OTHER_INI})
    sync.refresh(force=True)

    # Simulate a reboot with no network: fresh object, same state directory.
    backend.offline = True
    restarted = ConfigSync(backend, "IoT", tmp_path / "state")
    settings = restarted.load_cached()

    assert settings.sample_rate == 96000
    assert restarted.source == SOURCE_CACHE


def test_a_damaged_cache_falls_back_to_the_known_good_backup(tmp_path):
    """A bad edit on Drive must not become the node's permanent settings."""
    sync, backend = make_sync(tmp_path, {"IoT/config.ini": OTHER_INI})
    sync.refresh(force=True)  # 96000 is now both cache and backup

    backend.files["IoT/config.ini"] = BROKEN_INI
    sync.refresh(force=True)  # cache updated, backup deliberately not

    restarted = ConfigSync(backend, "IoT", tmp_path / "state")
    settings = restarted.load_cached()

    assert restarted.source == SOURCE_BACKUP
    assert settings.sample_rate == 96000  # the last good value, not the default


def test_a_bad_edit_does_not_overwrite_the_backup(tmp_path):
    sync, backend = make_sync(tmp_path, {"IoT/config.ini": OTHER_INI})
    sync.refresh(force=True)
    good = sync.backup_path.read_text()

    backend.files["IoT/config.ini"] = BROKEN_INI
    sync.refresh(force=True)

    assert sync.backup_path.read_text() == good


def test_with_nothing_cached_at_all_the_defaults_apply(tmp_path):
    sync, _ = make_sync(tmp_path)
    settings = sync.load_cached()
    assert sync.source == SOURCE_DEFAULT
    assert settings.sample_rate == 48000


def test_master_log_upload_tracks_connectivity(tmp_path):
    sync, backend = make_sync(tmp_path)
    log = tmp_path / "master_log.csv"
    log.write_text("date\n2026-08-17\n")

    assert sync.push_master_log(log) is True
    assert sync.online is True

    backend.offline = True
    assert sync.push_master_log(log) is False
    assert sync.online is False


def test_uploading_a_missing_master_log_is_a_no_op(tmp_path):
    sync, _ = make_sync(tmp_path)
    assert sync.push_master_log(tmp_path / "nope.csv") is False


# --- status snapshot ---------------------------------------------------------


def test_status_round_trips(tmp_path):
    writer = StatusWriter(tmp_path / "status.json", "pi5-node-01", "apiary")
    writer.update(temperature_c=20.5, humidity_pct=57.0, dominant_hz=232.5)
    writer.publish()

    status = read_status(tmp_path / "status.json")
    assert status is not None
    assert status.device_id == "pi5-node-01"
    assert status.site == "apiary"
    assert status.temperature_c == 20.5
    assert status.dominant_hz == 232.5
    assert status.updated_utc.endswith("Z")


def test_history_is_bounded(tmp_path):
    writer = StatusWriter(tmp_path / "status.json", "n")
    for value in range(HISTORY_LIMIT * 2):
        writer.push_history(temperature=float(value))

    assert len(writer.status.history_temperature) == HISTORY_LIMIT
    # The newest values survive, the oldest are dropped.
    assert writer.status.history_temperature[-1] == float(HISTORY_LIMIT * 2 - 1)


def test_history_ignores_missing_readings(tmp_path):
    writer = StatusWriter(tmp_path / "status.json", "n")
    writer.push_history(temperature=None, humidity=50.0)
    assert writer.status.history_temperature == []
    assert writer.status.history_humidity == [50.0]


def test_unknown_status_fields_are_ignored(tmp_path):
    writer = StatusWriter(tmp_path / "status.json", "n")
    writer.update(not_a_field=1, temperature_c=9.0)
    assert writer.status.temperature_c == 9.0
    assert not hasattr(writer.status, "not_a_field")


def test_a_missing_snapshot_reads_as_none(tmp_path):
    assert read_status(tmp_path / "absent.json") is None


def test_a_corrupt_snapshot_reads_as_none(tmp_path):
    path = tmp_path / "status.json"
    path.write_text("{ this is not json")
    assert read_status(path) is None


def test_staleness_is_detected(tmp_path):
    writer = StatusWriter(tmp_path / "status.json", "n")
    writer.publish()
    fresh = read_status(tmp_path / "status.json")
    assert fresh is not None and not fresh.is_stale()

    writer.status.updated_epoch = time.time() - STALE_AFTER_S - 10
    writer.status.updated_utc = "old"
    stale = writer.status
    assert stale.is_stale()


def test_disk_figures_are_recorded(tmp_path):
    writer = StatusWriter(tmp_path / "status.json", "n")
    writer.record_disk(tmp_path)
    assert writer.status.disk_total_gb > 0
    assert 0 <= writer.status.disk_free_pct <= 100


# --- concurrency and destructive-fallback guards ------------------------------


def test_concurrent_fetches_do_not_share_a_temp_file(tmp_path):
    """The daemon and a CLI command both fetch into the same state directory.

    With a fixed temp name they clobbered each other: one process deleted the
    file the other was about to read, so a stale config could be adopted while
    the drive copy was perfectly fine.
    """
    backend = FakeBackend({"IoT/config.ini": GOOD_INI})
    a = ConfigSync(backend, "IoT", tmp_path / "state")
    b = ConfigSync(backend, "IoT", tmp_path / "state")

    seen: list[Path] = []
    original = backend.download

    def spy(remote_path, local_path):
        seen.append(Path(local_path))
        return original(remote_path, local_path)

    backend.download = spy
    a.refresh(force=True)
    b.refresh(force=True)

    assert len(seen) == 2
    # Same directory is fine; the same *name* is not.
    assert seen[0].parent == seen[1].parent
    assert str(os.getpid()) in seen[0].name


def test_a_missing_remote_config_never_overwrites_an_existing_one(tmp_path):
    """A transient 'not found' must not destroy the operator's timetable."""
    backend = FakeBackend({"IoT/config.ini": OTHER_INI})
    sync = ConfigSync(backend, "IoT", tmp_path / "state")
    sync.refresh(force=True)
    assert sync.config.sample_rate == 96000

    # The drive momentarily reports the file as absent.
    del backend.files["IoT/config.ini"]
    uploads_before = len(backend.uploaded)
    changed = sync.refresh(force=True)

    assert changed is False
    assert sync.config.sample_rate == 96000        # settings untouched
    assert len(backend.uploaded) == uploads_before  # nothing written to the drive
    assert "sample_rate = 96000" in sync.cache_path.read_text()


def test_the_default_is_still_seeded_on_a_genuinely_fresh_node(tmp_path):
    backend = FakeBackend()
    sync = ConfigSync(backend, "IoT", tmp_path / "state")

    assert sync.refresh(force=True) is True
    assert "IoT/config.ini" in backend.files


def test_live_level_history_accumulates(tmp_path):
    """push_history builds 'history_<name>', so the field must match.

    A mismatch here is silent: live_db updates fine and the meter stays empty.
    """
    writer = StatusWriter(tmp_path / "status.json", "n")
    for db in (-40.0, -38.5, -42.0):
        writer.update(live_db=db)
        writer.push_history(live_db=db)
    writer.publish()

    status = read_status(tmp_path / "status.json")
    assert status is not None
    assert status.history_live_db == [-40.0, -38.5, -42.0]
    assert status.live_db == -42.0
