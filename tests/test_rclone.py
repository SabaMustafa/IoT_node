from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from iot_node.cloud.rclone import RcloneBackend, RcloneError
from iot_node.config import RcloneConfig


class FakeRun:
    """Stands in for subprocess.run, replaying scripted results per subcommand."""

    def __init__(self, results: dict[str, tuple[int, str, str]]) -> None:
        self.results = results
        self.calls: list[list[str]] = []

    SUBCOMMANDS = frozenset({"about", "copyto", "listremotes", "lsjson", "version"})

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        # Global flags come first, so find the subcommand by name.
        subcommand = next((a for a in cmd[1:] if a in self.SUBCOMMANDS), "")
        code, out, err = self.results.get(subcommand, (0, "", ""))
        return subprocess.CompletedProcess(cmd, code, out, err)


@pytest.fixture
def backend(monkeypatch) -> RcloneBackend:
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    return RcloneBackend(RcloneConfig(remote="onedrive"))


def script(monkeypatch, results: dict[str, tuple[int, str, str]]) -> FakeRun:
    fake = FakeRun(results)
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


def test_missing_binary_is_reported_clearly(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _name: None)
    with pytest.raises(RcloneError, match="not found on PATH"):
        _ = RcloneBackend(RcloneConfig()).binary


def test_target_builds_a_remote_path(backend):
    assert backend.target("IoT/node/a.WAV") == "onedrive:IoT/node/a.WAV"
    assert backend.target() == "onedrive:"

    backend.cfg.remote = "onedrive:"  # a trailing colon is easy to type by hand
    assert backend.target("x") == "onedrive:x"


def test_check_reports_free_space(backend, monkeypatch):
    script(
        monkeypatch,
        {
            "listremotes": (0, "onedrive:\n", ""),
            "about": (0, json.dumps({"free": 1.5e12, "total": 5e12}), ""),
        },
    )
    assert backend.check() == "onedrive (1500.0 GB free of 5000.0 GB)"


def test_check_names_the_missing_remote(backend, monkeypatch):
    script(monkeypatch, {"listremotes": (0, "gdrive:\nbackup:\n", "")})
    with pytest.raises(RcloneError, match="not configured"):
        backend.check()


def test_check_surfaces_an_auth_failure(backend, monkeypatch):
    script(
        monkeypatch,
        {
            "listremotes": (0, "onedrive:\n", ""),
            "about": (7, "", "Failed to about: couldn't fetch token: invalid_grant"),
        },
    )
    with pytest.raises(RcloneError, match="invalid_grant") as exc:
        backend.check()
    assert exc.value.retryable is False  # re-running will not fix a dead token


def test_upload_invokes_copyto(backend, monkeypatch, tmp_path):
    fake = script(monkeypatch, {"copyto": (0, "", "")})
    local = tmp_path / "20260814_155418.WAV"
    local.write_bytes(b"x" * 100)

    backend.upload(local, "IoT/node/audio/2026/08/14/20260814_155418.WAV")

    cmd = fake.calls[0]
    assert "copyto" in cmd
    assert str(local) in cmd
    assert "onedrive:IoT/node/audio/2026/08/14/20260814_155418.WAV" in cmd


def test_transient_failures_stay_retryable(backend, monkeypatch, tmp_path):
    script(monkeypatch, {"copyto": (5, "", "connection reset by peer")})
    local = tmp_path / "a.WAV"
    local.write_bytes(b"x")

    with pytest.raises(RcloneError) as exc:
        backend.upload(local, "IoT/a.WAV")
    assert exc.value.retryable is True
    assert exc.value.code == 5


def test_fatal_failures_are_not_retried(backend, monkeypatch, tmp_path):
    script(monkeypatch, {"copyto": (7, "", "Fatal error: quota exceeded")})
    local = tmp_path / "a.WAV"
    local.write_bytes(b"x")

    with pytest.raises(RcloneError) as exc:
        backend.upload(local, "IoT/a.WAV")
    assert exc.value.retryable is False


def test_timeout_is_retryable(backend, monkeypatch, tmp_path):
    def timeout(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1800)

    monkeypatch.setattr(subprocess, "run", timeout)
    local = tmp_path / "a.WAV"
    local.write_bytes(b"x")

    with pytest.raises(RcloneError, match="timed out") as exc:
        backend.upload(local, "IoT/a.WAV")
    assert exc.value.retryable is True


def test_remote_size_reads_lsjson(backend, monkeypatch):
    script(
        monkeypatch,
        {"lsjson": (0, json.dumps([{"Name": "a.WAV", "Size": 28800488, "IsDir": False}]), "")},
    )
    assert backend.remote_size("IoT/a.WAV") == 28800488


def test_remote_size_is_none_when_absent(backend, monkeypatch):
    script(monkeypatch, {"lsjson": (3, "", "directory not found")})
    assert backend.remote_size("IoT/missing.WAV") is None


def test_bwlimit_and_extra_args_reach_the_command(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    cfg = RcloneConfig(remote="onedrive", bwlimit="2M", extra_args=["--onedrive-chunk-size", "10M"])
    backend = RcloneBackend(cfg)
    fake = script(monkeypatch, {"copyto": (0, "", "")})

    backend.upload(Path("/tmp/a.WAV"), "IoT/a.WAV")

    cmd = fake.calls[0]
    assert cmd[cmd.index("--bwlimit") + 1] == "2M"
    assert "--onedrive-chunk-size" in cmd


def test_config_path_is_passed_through(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    backend = RcloneBackend(RcloneConfig(config_path=tmp_path / "rclone.conf"))
    fake = script(monkeypatch, {"copyto": (0, "", "")})

    backend.upload(Path("/tmp/a.WAV"), "IoT/a.WAV")

    cmd = fake.calls[0]
    assert cmd[cmd.index("--config") + 1] == str(tmp_path / "rclone.conf")
