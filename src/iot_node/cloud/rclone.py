"""Upload backend that drives the `rclone` binary.

rclone already solves the hard parts — resumable chunked uploads, throttle
handling, hash verification after transfer, and OAuth token refresh — so this is
a thin, careful wrapper rather than a reimplementation. It shells out per file
and reads rclone's exit code to decide whether a failure is worth retrying.

The credential lives in rclone's own config file, created once with
`rclone config` by signing in through a browser. Nothing here ever handles a
password or a token.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..config import RcloneConfig
from .backend import UploadError

log = logging.getLogger(__name__)

# rclone's documented exit codes.
_EXIT_OK = 0
_EXIT_RETRYABLE = {5}  # temporary error, rclone says try again
_EXIT_NOT_FOUND = {3, 4}
_EXIT_FATAL = {1, 7}  # usage error / fatal error — retrying changes nothing


class RcloneError(UploadError):
    def __init__(self, message: str, *, code: int | None = None, retryable: bool = False) -> None:
        super().__init__(message, retryable=retryable)
        self.code = code


class RcloneBackend:
    name = "rclone"

    def __init__(self, cfg: RcloneConfig, *, root: Path | None = None) -> None:
        self.cfg = cfg
        self.root = root or Path.cwd()
        self._binary: str | None = None

    def close(self) -> None:
        pass

    # --- invocation ----------------------------------------------------------

    @property
    def binary(self) -> str:
        if self._binary is None:
            found = shutil.which(self.cfg.binary)
            if not found:
                raise RcloneError(
                    f"rclone binary {self.cfg.binary!r} not found on PATH — "
                    f"run scripts/setup-onedrive.sh"
                )
            self._binary = found
        return self._binary

    def _base_args(self) -> list[str]:
        args = [self.binary]
        if self.cfg.config_path:
            config = Path(self.cfg.config_path)
            config = config if config.is_absolute() else self.root / config
            args += ["--config", str(config)]
        # One transfer at a time: a Pi on a slow uplink gains nothing from
        # parallelism, and serial transfers keep the log readable.
        args += ["--transfers", "1", "--checkers", "2"]
        if self.cfg.bwlimit:
            args += ["--bwlimit", self.cfg.bwlimit]
        args += list(self.cfg.extra_args)
        return args

    def _run(self, *args: str, timeout_s: float | None = None) -> subprocess.CompletedProcess[str]:
        cmd = self._base_args() + list(args)
        log.debug("running %s", " ".join(cmd))
        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_s or self.cfg.timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RcloneError(
                f"rclone timed out after {exc.timeout:.0f}s: {' '.join(args)}", retryable=True
            ) from exc
        except OSError as exc:
            raise RcloneError(f"could not run rclone: {exc}") from exc

    @staticmethod
    def _fail(result: subprocess.CompletedProcess[str], what: str) -> RcloneError:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        message = detail[-1] if detail else f"exit code {result.returncode}"
        retryable = result.returncode in _EXIT_RETRYABLE or result.returncode not in (
            _EXIT_FATAL | _EXIT_NOT_FOUND
        )
        return RcloneError(
            f"{what} failed (exit {result.returncode}): {message}",
            code=result.returncode,
            retryable=retryable,
        )

    # --- backend interface ---------------------------------------------------

    def target(self, remote_path: str = "") -> str:
        """Build an rclone target like `onedrive:IoT/node/audio/x.WAV`."""
        remote = self.cfg.remote.rstrip(":")
        return f"{remote}:{remote_path.strip('/')}" if remote_path else f"{remote}:"

    def check(self) -> str:
        """Confirm the remote exists and the stored token still works."""
        remotes = self._run("listremotes", timeout_s=30)
        if remotes.returncode == _EXIT_OK:
            names = {
                line.strip().rstrip(":") for line in remotes.stdout.splitlines() if line.strip()
            }
            if names and self.cfg.remote.rstrip(":") not in names:
                raise RcloneError(
                    f"rclone remote {self.cfg.remote!r} is not configured "
                    f"(found: {', '.join(sorted(names)) or 'none'}) — run 'rclone config'"
                )

        result = self._run("about", self.target(), "--json", timeout_s=60)
        if result.returncode != _EXIT_OK:
            raise self._fail(result, f"rclone about {self.cfg.remote}")

        try:
            info: dict[str, Any] = json.loads(result.stdout or "{}")
        except ValueError:
            return f"{self.cfg.remote} (reachable)"

        free = info.get("free")
        total = info.get("total")
        if free is None or total is None:
            return f"{self.cfg.remote} (reachable)"
        return f"{self.cfg.remote} ({free / 1e9:.1f} GB free of {total / 1e9:.1f} GB)"

    def upload(self, local_path: Path, remote_path: str) -> dict[str, Any]:
        """Copy one file up. rclone creates parent folders and verifies hashes."""
        result = self._run(
            "copyto",
            str(local_path),
            self.target(remote_path),
            "--retries",
            str(self.cfg.retries),
            "--low-level-retries",
            "10",
            "--stats",
            "0",
        )
        if result.returncode != _EXIT_OK:
            raise self._fail(result, f"upload {local_path.name}")
        return {"remote_path": remote_path}

    def download(self, remote_path: str, local_path: Path) -> bool:
        """Fetch a remote file. False means it is absent, not that we failed."""
        local_path.parent.mkdir(parents=True, exist_ok=True)
        result = self._run(
            "copyto", self.target(remote_path), str(local_path), "--stats", "0", timeout_s=300
        )
        if result.returncode in _EXIT_NOT_FOUND:
            return False
        if result.returncode != _EXIT_OK:
            # "directory not found" also surfaces as a generic error on some
            # backends; treat a missing source as absence, not failure.
            stderr = (result.stderr or "").lower()
            if "not found" in stderr or "no such" in stderr:
                return False
            raise self._fail(result, f"download {remote_path}")
        return local_path.is_file()

    def remote_size(self, remote_path: str) -> int | None:
        result = self._run("lsjson", self.target(remote_path), timeout_s=120)
        if result.returncode in _EXIT_NOT_FOUND:
            return None
        if result.returncode != _EXIT_OK:
            # Verification is best-effort; the copy itself already hash-checked.
            log.debug("could not stat %s: %s", remote_path, result.stderr.strip())
            return None
        try:
            entries = json.loads(result.stdout or "[]")
        except ValueError:
            return None
        for entry in entries:
            if not entry.get("IsDir"):
                size = entry.get("Size")
                return int(size) if size is not None else None
        return None

    def version(self) -> str:
        result = self._run("version", timeout_s=30)
        first = (result.stdout or "").splitlines()
        return first[0].strip() if first else "unknown"
