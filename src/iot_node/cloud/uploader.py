"""Drains the spool to OneDrive and keeps local disk under control.

The spool is the source of truth for what still needs to go up; the SQLite store
records what already did. Anything sitting in the spool that the store has never
seen is adopted on the next pass, so a lost database or a file dropped in by
hand still gets uploaded exactly once.
"""

from __future__ import annotations

import logging
import re
import shutil
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from ..config import Config
from ..remote_config import RemoteConfig
from ..state import Store
from .backend import Backend, UploadError, make_backend

log = logging.getLogger(__name__)

# AudioMoth names recordings after the moment they started: 20260814_155418.WAV
_TIMESTAMP_NAME = re.compile(r"(\d{8})[_\-]?(\d{6})")
# Daily sensor CSVs: sensors_pi5-node-01_2026-08-14.csv
_DATE_NAME = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


class Uploader:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        backend: Backend | None = None,
        settings: RemoteConfig | None = None,
        hold: Callable[[Path], bool] | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.backend = backend if backend is not None else make_backend(cfg)
        # Storage policy comes from config.ini so it can be changed in the
        # field; the built-in defaults apply until the first fetch lands.
        self.settings = settings if settings is not None else RemoteConfig()
        # Asked before deleting an uploaded file: True keeps it, because
        # another destination (the Flask server) has not had it yet.
        self.hold = hold

    def close(self) -> None:
        self.backend.close()

    def check_credentials(self) -> str:
        """Verify we can actually reach the target drive. Raises on failure."""
        return self.backend.check()

    # --- the loop ------------------------------------------------------------

    def run_once(self) -> tuple[int, int]:
        """One drain pass. Returns (uploaded, failed)."""
        self.adopt_orphans()
        pending = self.store.pending()
        if not pending:
            return 0, 0

        log.info("uploading %d file(s)", len(pending))
        uploaded = failed = 0
        for row in pending:
            path = Path(row["spool_path"])
            if not path.is_file():
                log.warning("spooled file disappeared: %s", path)
                self.store.forget(path)
                continue
            try:
                self.upload_file(path, row["kind"])
                uploaded += 1
            except UploadError as exc:
                log.error("upload failed for %s: %s", path.name, exc)
                self.store.mark_failed(path, str(exc))
                failed += 1
                if not exc.retryable:
                    # Credentials or config are wrong; the rest of the queue
                    # would fail identically. Stop and try again next pass.
                    log.error("stopping this pass — the problem is not transient")
                    break
            except OSError as exc:
                log.error("cannot read %s: %s", path, exc)
                self.store.mark_failed(path, str(exc))
                failed += 1

        self.housekeep()
        return uploaded, failed

    def adopt_orphans(self) -> None:
        """Queue spool files the database does not know about."""
        for kind in ("audio", "sensors"):
            directory = self.cfg.spool_dir / kind
            if not directory.is_dir():
                continue
            for path in sorted(directory.iterdir()):
                # .part files are still being staged.
                if not path.is_file() or path.suffix == ".part":
                    continue
                self.store.enqueue(path, kind, path.stat().st_size)

    def upload_file(self, path: Path, kind: str) -> str:
        remote_path = self.remote_path_for(path, kind)
        size = path.stat().st_size
        started = time.monotonic()

        item = self.backend.upload(path, remote_path)

        if self.cfg.upload.verify_size:
            remote_size = item.get("size")
            if remote_size is None:
                remote_size = self.backend.remote_size(remote_path)
            if remote_size is not None and int(remote_size) != size:
                raise UploadError(
                    f"size mismatch for {remote_path}: local {size}, remote {remote_size}",
                    retryable=True,
                )

        elapsed = max(time.monotonic() - started, 1e-6)
        log.info(
            "uploaded %s → %s (%.1f MiB in %.1fs, %.2f MiB/s)",
            path.name, remote_path, size / 1048576, elapsed, size / 1048576 / elapsed,
        )
        self.store.mark_uploaded(path, remote_path)
        return remote_path

    # --- naming --------------------------------------------------------------

    def remote_path_for(self, path: Path, kind: str) -> str:
        """Lay files out as <root>/<device>/<YYYY-MM-DD>/, audio in its own folder.

        config.ini and master_log.csv live at <root> itself, one level above the
        per-device folders, so one timetable can serve a fleet of nodes.
        """
        when = self._timestamp_for(path)
        root = self.cfg.cloud.remote_root.format(
            device_id=self.cfg.device.id, site=self.cfg.device.site or self.cfg.device.id
        ).strip("/")
        day = f"{root}/{self.cfg.device.id}/{when:%Y-%m-%d}"
        return f"{day}/audio/{path.name}" if kind == "audio" else f"{day}/{path.name}"

    @staticmethod
    def _timestamp_for(path: Path) -> datetime:
        """Prefer the timestamp encoded in the filename over the file's mtime.

        A recording copied off a card long after the fact still belongs to the
        day it was recorded, not the day it was copied.
        """
        match = _TIMESTAMP_NAME.search(path.name)
        if match:
            try:
                return datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S").replace(
                    tzinfo=UTC
                )
            except ValueError:
                pass
        match = _DATE_NAME.search(path.name)
        if match:
            try:
                return datetime.strptime(match.group(0), "%Y-%m-%d").replace(tzinfo=UTC)
            except ValueError:
                pass
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)

    # --- housekeeping --------------------------------------------------------

    def housekeep(self) -> None:
        """Keep the card from filling. A full card stops sensors, not just audio.

        Three escalating steps, gentlest first: drop what is safely uploaded,
        then drop uploaded data that is simply old, and only if the card is
        *still* too full, start discarding un-uploaded recordings — loudly, and
        only when the operator has allowed it in config.ini.
        """
        policy = self.settings
        removed = 0

        if policy.delete_after_upload:
            removed += self._purge(self.store.uploaded_files())
        elif policy.max_local_days > 0:
            cutoff = time.time() - policy.max_local_days * 86400
            removed += self._purge(self.store.uploaded_files(before=cutoff))

        # Age limit applies regardless of the delete_after_upload setting.
        if policy.max_local_days > 0:
            cutoff = time.time() - policy.max_local_days * 86400
            removed += self._purge(self.store.uploaded_files(before=cutoff))

        if self._free_pct() < policy.min_free_percent:
            log.warning(
                "free disk %.1f%% is below the %.0f%% floor — purging uploaded files early",
                self._free_pct(), policy.min_free_percent,
            )
            for row in self.store.uploaded_files():
                if self._free_pct() >= policy.min_free_percent:
                    break
                # Below the floor, the card matters more than the Flask copy.
                removed += self._purge([row], force=True)

        if removed:
            log.info("housekeeping removed %d uploaded file(s)", removed)

        if self._free_pct() < policy.min_free_percent:
            self._emergency_purge(policy)

        # Ingest records outlive the files they describe, but not forever.
        self.store.prune_ingested(older_than_s=30 * 86400)

    def _emergency_purge(self, policy: RemoteConfig) -> None:
        """Last resort: discard the oldest un-uploaded audio to stay alive."""
        if not policy.drop_oldest_when_full:
            log.error(
                "free disk %.1f%% is below the %.0f%% floor and everything uploaded has "
                "already been removed. drop_oldest_when_full is off, so nothing further "
                "will be deleted — the card will fill and recording will stop.",
                self._free_pct(), policy.min_free_percent,
            )
            return

        # Sensor CSVs are a few KB a day and are the irreplaceable record; only
        # audio is ever discarded unsent.
        candidates = sorted(
            (p for p in (self.cfg.spool_dir / "audio").glob("*") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
        )
        dropped = 0
        for path in candidates:
            if self._free_pct() >= policy.min_free_percent:
                break
            size_mib = path.stat().st_size / 1048576
            try:
                path.unlink()
            except OSError as exc:
                log.warning("could not drop %s: %s", path.name, exc)
                continue
            self.store.forget(path)
            dropped += 1
            log.error(
                "DROPPED un-uploaded recording %s (%.1f MiB) — disk was below the "
                "%.0f%% floor", path.name, size_mib, policy.min_free_percent,
            )
        if dropped:
            log.error(
                "discarded %d un-uploaded recording(s) to keep the node running; "
                "free disk is now %.1f%%", dropped, self._free_pct(),
            )
        elif self._free_pct() < policy.min_free_percent:
            log.error(
                "free disk %.1f%% is below the floor and there is nothing left to drop",
                self._free_pct(),
            )

    def _purge(self, rows, *, force: bool = False) -> int:
        count = 0
        for row in rows:
            path = Path(row["spool_path"])
            if not force and self.hold is not None and self.hold(path):
                continue
            try:
                path.unlink(missing_ok=True)
                count += 1
            except OSError as exc:
                log.warning("could not delete %s: %s", path, exc)
                continue
            self.store.forget(path)
        return count

    def _free_pct(self) -> float:
        try:
            usage = shutil.disk_usage(self.cfg.spool_dir)
        except OSError:
            return 100.0
        return usage.free / usage.total * 100.0
