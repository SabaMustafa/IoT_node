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
