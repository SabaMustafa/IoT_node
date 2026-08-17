from __future__ import annotations

import time
from pathlib import Path

from iot_node.state import FAILED, PENDING, UPLOADED, Store


def test_enqueue_is_idempotent(tmp_path):
    with Store(tmp_path / "s.db") as store:
        store.enqueue(Path("/spool/a.wav"), "audio", 100)
        store.enqueue(Path("/spool/a.wav"), "audio", 100)
        assert len(store.pending()) == 1


def test_upload_lifecycle(tmp_path):
    path = Path("/spool/a.wav")
    with Store(tmp_path / "s.db") as store:
        store.enqueue(path, "audio", 100)
        assert store.counts() == {PENDING: 1}

        store.mark_uploaded(path, "IoT/x/audio/a.wav")
        assert store.counts() == {UPLOADED: 1}
        assert store.pending() == []

        rows = list(store.uploaded_files())
        assert rows[0]["remote_path"] == "IoT/x/audio/a.wav"


def test_failures_are_retried_and_counted(tmp_path):
    path = Path("/spool/a.wav")
    with Store(tmp_path / "s.db") as store:
        store.enqueue(path, "audio", 100)
        store.mark_failed(path, "HTTP 503")
        store.mark_failed(path, "HTTP 503")

        pending = store.pending()
        assert len(pending) == 1  # still queued for another go
        assert pending[0]["attempts"] == 2
        assert pending[0]["status"] == FAILED


def test_pending_prefers_the_least_attempted(tmp_path):
    with Store(tmp_path / "s.db") as store:
        store.enqueue(Path("/spool/old.wav"), "audio", 1)
        store.enqueue(Path("/spool/new.wav"), "audio", 1)
        store.mark_failed(Path("/spool/old.wav"), "boom")

        assert Path(store.pending()[0]["spool_path"]).name == "new.wav"


def test_source_key_changes_with_content(tmp_path):
    path = tmp_path / "a.wav"
    assert Store.source_key(path, 100, 1000.0) == Store.source_key(path, 100, 1000.4)
    assert Store.source_key(path, 100, 1000.0) != Store.source_key(path, 200, 1000.0)
    assert Store.source_key(path, 100, 1000.0) != Store.source_key(path, 100, 2000.0)


def test_ingest_records_are_pruned_by_age(tmp_path):
    with Store(tmp_path / "s.db") as store:
        store.mark_ingested("key", Path("/card/a.wav"), Path("/spool/a.wav"))
        assert store.is_ingested("key")

        assert store.prune_ingested(older_than_s=3600) == 0
        assert store.prune_ingested(older_than_s=-1) == 1
        assert not store.is_ingested("key")


def test_uploaded_files_can_be_filtered_by_time(tmp_path):
    path = Path("/spool/a.wav")
    with Store(tmp_path / "s.db") as store:
        store.enqueue(path, "audio", 1)
        store.mark_uploaded(path, "remote/a.wav")

        assert list(store.uploaded_files(before=time.time() - 60)) == []
        assert len(list(store.uploaded_files(before=time.time() + 60))) == 1


def test_the_store_is_usable_from_another_thread(tmp_path):
    """Recording runs on a worker thread and enqueues from there.

    sqlite3 rejects cross-thread use by default; this failed in the field with
    'SQLite objects created in a thread can only be used in that same thread',
    which lost the recording from the upload queue entirely.
    """
    import threading

    errors: list[Exception] = []
    with Store(tmp_path / "s.db") as store:
        def worker(index: int) -> None:
            try:
                store.enqueue(Path(f"/spool/a{index}.wav"), "audio", 100)
                store.counts()
                list(store.uploaded_files())
            except Exception as exc:  # noqa: BLE001 - the point is to catch any
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        assert len(store.pending()) == 8


def test_concurrent_writers_do_not_corrupt_counts(tmp_path):
    import threading

    with Store(tmp_path / "s.db") as store:
        for index in range(20):
            store.enqueue(Path(f"/spool/f{index}.wav"), "audio", 1)

        def mark(start: int) -> None:
            for index in range(start, start + 10):
                store.mark_uploaded(Path(f"/spool/f{index}.wav"), f"remote/f{index}")

        threads = [threading.Thread(target=mark, args=(s,)) for s in (0, 10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert store.counts() == {"uploaded": 20}


def test_a_grown_file_at_an_uploaded_path_is_requeued(tmp_path):
    """The same spool path is reused deliberately, and must not be ignored.

    A part-day CSV is published for an early upload; at midnight the complete
    day is written to that identical path. INSERT OR IGNORE dropped the
    finished day silently — Drive kept a truncated morning and housekeeping
    deleted the rest.
    """
    path = Path("/spool/sensors/sensors_node_2026-08-17.csv")
    with Store(tmp_path / "s.db") as store:
        store.enqueue(path, "sensors", 153)          # partial day
        store.mark_uploaded(path, "remote/day.csv")
        assert store.pending() == []

        store.enqueue(path, "sensors", 2510)         # the complete day

        pending = store.pending()
        assert len(pending) == 1, "the finished day must be queued again"
        assert pending[0]["size"] == 2510
        assert pending[0]["attempts"] == 0


def test_an_unchanged_file_is_not_requeued(tmp_path):
    """Re-adopting an identical file must stay a no-op, or every pass re-uploads."""
    path = Path("/spool/audio/a.WAV")
    with Store(tmp_path / "s.db") as store:
        store.enqueue(path, "audio", 100)
        store.mark_uploaded(path, "remote/a.WAV")

        store.enqueue(path, "audio", 100)

        assert store.pending() == []
        assert store.counts() == {"uploaded": 1}


def test_requeue_clears_a_previous_failure(tmp_path):
    path = Path("/spool/audio/a.WAV")
    with Store(tmp_path / "s.db") as store:
        store.enqueue(path, "audio", 100)
        store.mark_failed(path, "boom")
        store.enqueue(path, "audio", 200)

        row = store.pending()[0]
        assert row["attempts"] == 0
        assert row["last_error"] is None
        assert row["size"] == 200
