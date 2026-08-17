from __future__ import annotations

import json
from pathlib import Path

import pytest

from iot_node.cloud.backend import UploadError
from iot_node.cloud.onedrive import GraphError, OneDriveClient, _next_expected
from iot_node.config import OneDriveConfig


class FakeResponse:
    def __init__(self, status: int, body: dict | None = None, headers: dict | None = None) -> None:
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.content = json.dumps(self._body).encode() if body is not None else b""
        self.text = self.content.decode()

    def json(self) -> dict:
        return self._body


class FakeSession:
    """Replays a scripted list of responses and records every request."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def request(self, method, url, headers=None, **kwargs):
        self.calls.append(
            {"method": method, "url": url, "headers": headers or {}, "kwargs": kwargs}
        )
        if not self.responses:
            raise AssertionError(f"unexpected request: {method} {url}")
        return self.responses.pop(0)

    def close(self) -> None:
        pass


class StubAuth:
    def token(self) -> str:
        return "stub-token"


@pytest.fixture
def client() -> OneDriveClient:
    cfg = OneDriveConfig(
        tenant_id="tenant", client_id="client", target="me", conflict_behavior="replace"
    )
    return OneDriveClient(cfg, StubAuth(), max_retries=2)


# --- URL building ------------------------------------------------------------


def test_item_url_encodes_the_path(client):
    url = client.item_url("IoT/node 1/audio/rec 01.WAV", "/content")
    assert url.endswith("/root:/IoT/node%201/audio/rec%2001.WAV:/content")


def test_drive_base_follows_the_target(client):
    assert client.drive_base.endswith("/me/drive")

    client.cfg.target = "user"
    client.cfg.user_principal_name = "someone@aut.ac.nz"
    assert client.drive_base.endswith("/users/someone%40aut.ac.nz/drive")

    client.cfg.target = "drive"
    client.cfg.drive_id = "b!abc"
    assert client.drive_base.endswith("/drives/b%21abc")


def test_missing_drive_id_is_reported(client):
    client.cfg.target = "drive"
    with pytest.raises(GraphError, match="drive_id"):
        _ = client.drive_base


# --- chunked upload ----------------------------------------------------------


def test_large_file_uploads_in_chunks(client, tmp_path):
    size = 320 * 1024 * 3
    path = tmp_path / "big.WAV"
    path.write_bytes(b"x" * size)
    chunk = 320 * 1024

    client.session = FakeSession(
        [
            FakeResponse(200, {"uploadUrl": "https://upload.example/session"}),
            FakeResponse(202, {"nextExpectedRanges": [f"{chunk}-"]}),
            FakeResponse(202, {"nextExpectedRanges": [f"{chunk * 2}-"]}),
            FakeResponse(201, {"id": "item-1", "size": size}),
        ]
    )

    item = client._upload_session(path, "IoT/x/big.WAV", size=size, chunk_size=chunk)

    assert item["id"] == "item-1"
    chunk_calls = [c for c in client.session.calls if c["url"] == "https://upload.example/session"]
    assert len(chunk_calls) == 3
    assert chunk_calls[0]["headers"]["Content-Range"] == f"bytes 0-{chunk - 1}/{size}"
    assert chunk_calls[-1]["headers"]["Content-Range"] == f"bytes {chunk * 2}-{size - 1}/{size}"


def test_upload_url_is_not_sent_a_bearer_token(client, tmp_path):
    """Graph rejects chunks that carry an Authorization header."""
    size = 320 * 1024
    path = tmp_path / "big.WAV"
    path.write_bytes(b"x" * size)

    client.session = FakeSession(
        [
            FakeResponse(200, {"uploadUrl": "https://upload.example/session"}),
            FakeResponse(201, {"id": "item-1", "size": size}),
        ]
    )
    client._upload_session(path, "IoT/x/big.WAV", size=size, chunk_size=size)

    chunk_call = client.session.calls[-1]
    assert "Authorization" not in chunk_call["headers"]
    assert "Authorization" in client.session.calls[0]["headers"]  # but the session request has one


def test_expired_session_is_recreated(client, tmp_path):
    size = 320 * 1024
    path = tmp_path / "big.WAV"
    path.write_bytes(b"x" * size)

    client.session = FakeSession(
        [
            FakeResponse(200, {"uploadUrl": "https://upload.example/one"}),
            FakeResponse(404, {"error": "expired"}),
            FakeResponse(200, {"uploadUrl": "https://upload.example/two"}),
            FakeResponse(201, {"id": "item-1", "size": size}),
        ]
    )

    item = client._upload_session(path, "IoT/x/big.WAV", size=size, chunk_size=size)

    assert item["id"] == "item-1"
    assert any(c["url"] == "https://upload.example/two" for c in client.session.calls)


def test_chunk_size_must_match_graphs_granularity(client, tmp_path):
    path = tmp_path / "big.WAV"
    path.write_bytes(b"x" * 1024)
    with pytest.raises(GraphError, match="320 KiB"):
        client._upload_session(path, "IoT/x/big.WAV", size=1024, chunk_size=1000)


def test_small_file_uses_a_single_put(client, tmp_path):
    path = tmp_path / "small.jsonl"
    path.write_bytes(b"{}\n")
    client.session = FakeSession([FakeResponse(201, {"id": "item-2", "size": 3})])

    item = client.upload(path, "small.jsonl", chunk_size=10 * 1024 * 1024)

    assert item["id"] == "item-2"
    assert client.session.calls[0]["method"] == "PUT"


def test_next_expected_ranges_parsing():
    assert _next_expected({"nextExpectedRanges": ["1024-"]}, default=0) == 1024
    assert _next_expected({"nextExpectedRanges": ["512-1023"]}, default=0) == 512
    assert _next_expected({}, default=99) == 99
    assert _next_expected({"nextExpectedRanges": []}, default=99) == 99


# --- folders -----------------------------------------------------------------


def test_folder_tree_is_created_once(client):
    client.session = FakeSession(
        [FakeResponse(201, {"id": "a"}), FakeResponse(201, {"id": "b"})]
    )
    client.ensure_folder("IoT/node-1")
    assert len(client.session.calls) == 2

    # Second call is served from the cache, not the network.
    client.ensure_folder("IoT/node-1")
    assert len(client.session.calls) == 2


def test_existing_folder_is_not_an_error(client):
    client.session = FakeSession([FakeResponse(409, {"error": "nameAlreadyExists"})])
    client.ensure_folder("IoT")
    assert "IoT" in client._known_folders


# --- remote layout -----------------------------------------------------------


class StubBackend:
    """A backend that accepts everything, so tests can exercise the loop."""

    name = "stub"

    def __init__(self) -> None:
        self.uploaded: list[tuple[Path, str]] = []

    def check(self) -> str:
        return "stub backend"

    def upload(self, local_path: Path, remote_path: str) -> dict:
        self.uploaded.append((local_path, remote_path))
        return {"size": local_path.stat().st_size}

    def remote_size(self, remote_path: str) -> int | None:
        return None

    def close(self) -> None:
        pass


def make_uploader(cfg):
    from iot_node.cloud.uploader import Uploader
    from iot_node.state import Store

    return Uploader(cfg, Store(cfg.state_db), backend=StubBackend())


def test_audio_goes_to_a_per_day_audio_folder(cfg):
    cfg.cloud.rclone.remote_root = "IoT"
    uploader = make_uploader(cfg)

    remote = uploader.remote_path_for(Path("/spool/audio/20260814_155418.WAV"), "audio")

    assert remote == "IoT/test-node/2026-08-14/audio/20260814_155418.WAV"


def test_daily_csv_sits_at_the_day_folder_root(cfg):
    cfg.cloud.rclone.remote_root = "IoT"
    uploader = make_uploader(cfg)

    remote = uploader.remote_path_for(
        Path("/spool/sensors/sensors_test-node_2026-08-14.csv"), "sensors"
    )

    assert remote == "IoT/test-node/2026-08-14/sensors_test-node_2026-08-14.csv"


def test_remote_path_falls_back_to_mtime(cfg, tmp_path):
    cfg.cloud.rclone.remote_root = "IoT"
    uploader = make_uploader(cfg)
    path = tmp_path / "no-timestamp-here.WAV"
    path.write_bytes(b"x")

    remote = uploader.remote_path_for(path, "audio")

    assert remote.endswith("/audio/no-timestamp-here.WAV")
    assert remote.startswith("IoT/test-node/")


def test_orphaned_spool_files_are_adopted(cfg):
    uploader = make_uploader(cfg)
    stray = cfg.spool_dir / "audio" / "20260814_155418.WAV"
    stray.write_bytes(b"x" * 100)
    partial = cfg.spool_dir / "audio" / "20260814_160000.WAV.part"
    partial.write_bytes(b"x" * 100)

    uploader.adopt_orphans()

    queued = [Path(row["spool_path"]).name for row in uploader.store.pending()]
    assert queued == ["20260814_155418.WAV"]  # the .part file is still being written


# --- the drain loop ----------------------------------------------------------


def spool_file(cfg, name: str = "20260814_155418.WAV", size: int = 100) -> Path:
    path = cfg.spool_dir / "audio" / name
    path.write_bytes(b"x" * size)
    return path


def test_run_once_uploads_and_records(cfg):
    uploader = make_uploader(cfg)
    uploader.settings.delete_after_upload = False
    uploader.settings.max_local_days = 0
    path = spool_file(cfg)

    uploaded, failed = uploader.run_once()

    assert (uploaded, failed) == (1, 0)
    assert uploader.backend.uploaded[0][1].endswith("/20260814_155418.WAV")
    assert [row["status"] for row in uploader.store.uploaded_files()] == ["uploaded"]
    assert path.exists()  # retention keeps it for now


def test_uploaded_files_are_removed_when_configured(cfg):
    uploader = make_uploader(cfg)
    uploader.settings.delete_after_upload = True
    path = spool_file(cfg)

    uploader.run_once()

    assert not path.exists()
    assert uploader.store.counts() == {}  # the ledger entry goes with the file


def test_a_transient_failure_keeps_draining_the_queue(cfg):
    uploader = make_uploader(cfg)
    spool_file(cfg, "20260814_155418.WAV")
    spool_file(cfg, "20260814_160500.WAV")

    def flaky(local_path, remote_path):
        if "155418" in local_path.name:
            raise UploadError("connection reset", retryable=True)
        return {"size": local_path.stat().st_size}

    uploader.backend.upload = flaky

    uploaded, failed = uploader.run_once()

    assert (uploaded, failed) == (1, 1)  # the second file still went up


def test_a_fatal_failure_stops_the_pass(cfg):
    uploader = make_uploader(cfg)
    spool_file(cfg, "20260814_155418.WAV")
    spool_file(cfg, "20260814_160500.WAV")

    def broken(local_path, remote_path):
        raise UploadError("token expired", retryable=False)

    uploader.backend.upload = broken

    uploaded, failed = uploader.run_once()

    # Both would fail identically, so only the first is attempted.
    assert (uploaded, failed) == (0, 1)


def test_a_size_mismatch_is_caught(cfg):
    uploader = make_uploader(cfg)
    spool_file(cfg, size=100)
    uploader.backend.upload = lambda local_path, remote_path: {"size": 42}

    uploaded, failed = uploader.run_once()

    assert (uploaded, failed) == (0, 1)
    assert "size mismatch" in uploader.store.pending()[0]["last_error"]


def test_a_vanished_file_is_dropped_from_the_queue(cfg):
    uploader = make_uploader(cfg)
    path = spool_file(cfg)
    uploader.adopt_orphans()
    path.unlink()

    uploaded, failed = uploader.run_once()

    assert (uploaded, failed) == (0, 0)
    assert uploader.store.pending() == []
