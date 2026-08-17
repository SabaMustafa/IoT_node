"""Keeps the node's settings in step with config.ini on the drive.

The file lives at ``<remote_root>/config.ini`` — one level above the per-device
folders, so a fleet of nodes can share one timetable. On the first run, if no
config.ini is there, the node writes the documented default so the operator has
something to edit rather than a blank page.

**Three layers of fallback**, because a node in a field loses its link for days
at a time and must keep working through it:

1. ``config.ini`` on the drive — authoritative whenever it can be reached.
2. ``var/state/config.ini`` — the last copy actually fetched.
3. ``var/state/config.ini.backup`` — the last copy that *parsed cleanly*.

The distinction between 2 and 3 is the point. If someone edits the file on
Drive and saves something malformed, the node fetches it, fails to make sense
of it, and falls back to the last version that worked rather than reverting to
built-in defaults and quietly abandoning the operator's timetable. Only a
configuration that parses without complaint is promoted to the backup.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from ..remote_config import DEFAULT_CONFIG_INI, RemoteConfig, parse_config_ini
from .backend import Backend, UploadError

log = logging.getLogger(__name__)

CONFIG_NAME = "config.ini"
BACKUP_NAME = "config.ini.backup"
MASTER_LOG_NAME = "master_log.csv"

# Where the settings in force came from, for the dashboard and the logs.
SOURCE_DRIVE = "drive"
SOURCE_CACHE = "cached (offline)"
SOURCE_BACKUP = "backup (offline)"
SOURCE_DEFAULT = "built-in defaults"


class ConfigSync:
    def __init__(self, backend: Backend, remote_root: str, cache_dir: Path) -> None:
        self.backend = backend
        self.remote_root = remote_root.strip("/")
        self.cache_dir = cache_dir
        self.cache_path = cache_dir / CONFIG_NAME
        self.backup_path = cache_dir / BACKUP_NAME
        self.config = RemoteConfig()
        self.source = SOURCE_DEFAULT
        self.online = False
        self.last_success: float | None = None
        self._last_text: str | None = None
        self._last_fetch: float = 0.0

    @property
    def remote_path(self) -> str:
        return f"{self.remote_root}/{CONFIG_NAME}"

    @property
    def master_log_remote_path(self) -> str:
        return f"{self.remote_root}/{MASTER_LOG_NAME}"

    # --- loading -------------------------------------------------------------

    def load_cached(self) -> RemoteConfig:
        """Best available settings without touching the network.

        Used at startup so the node begins working immediately, before the
        first fetch, and whenever the link is down.
        """
        for path, source in ((self.cache_path, SOURCE_CACHE), (self.backup_path, SOURCE_BACKUP)):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                log.warning("cannot read %s: %s", path, exc)
                continue
            config = parse_config_ini(text)
            if config.warnings and path is self.cache_path and self.backup_path.is_file():
                # The cached copy is damaged but a known-good one exists.
                log.warning(
                    "cached config.ini has %d problem(s) — trying the known-good backup",
                    len(config.warnings),
                )
                continue
            self.config, self.source, self._last_text = config, source, text
            for warning in config.warnings:
                log.warning("config.ini: %s", warning)
            log.info("using %s config.ini: %s", source, config.summary())
            return self.config

        self.config, self.source = RemoteConfig(), SOURCE_DEFAULT
        log.info("no usable config.ini yet — %s: %s", SOURCE_DEFAULT, self.config.summary())
        return self.config

    def refresh(self, *, force: bool = False, min_interval_s: float = 300.0) -> bool:
        """Fetch config.ini if it is due. Returns True if the settings changed."""
        now = time.monotonic()
        if not force and (now - self._last_fetch) < min_interval_s:
            return False
        self._last_fetch = now

        # Unique per process: the daemon and any CLI command (`iot-node config`,
        # `iot-node refresh`) share this state directory, and a fixed temp name
        # let two concurrent fetches clobber one another — one process deleting
        # the file the other was about to read, so a stale or partial config
        # could be adopted while the file on the drive was perfectly fine.
        temp = self.cache_dir / f"{CONFIG_NAME}.fetched.{os.getpid()}"
        try:
            found = self.backend.download(self.remote_path, temp)
        except UploadError as exc:
            self.online = False
            log.warning(
                "could not fetch config.ini (%s) — continuing on the %s copy", exc, self.source
            )
            return False

        self.online = True
        self.last_success = time.time()

        if not found:
            # Only seed a default when this node has genuinely never had a
            # config. Otherwise a transient "not found" — a flaky link, a
            # rename in progress, a throttled request — would overwrite an
            # operator's timetable with the factory one, on the drive as well
            # as locally. Losing their settings is far worse than running on
            # yesterday's copy for a few minutes.
            if self.cache_path.is_file() or self.backup_path.is_file():
                log.warning(
                    "config.ini was not found on the drive, but this node already has "
                    "one — keeping it rather than overwriting with defaults"
                )
                return False
            log.info("no config.ini on the drive — publishing the default for you to edit")
            self._publish_default()
            return True

        try:
            text = temp.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.warning("could not read the fetched config.ini: %s", exc)
            return False
        finally:
            temp.unlink(missing_ok=True)

        if text == self._last_text and self.source == SOURCE_DRIVE:
            return False

        config = parse_config_ini(text)
        for warning in config.warnings:
            log.warning("config.ini: %s", warning)

        self.config = config
        self.source = SOURCE_DRIVE
        self._last_text = text
        self._write(self.cache_path, text)

        # Only a clean parse earns the right to be the offline fallback.
        if not config.warnings:
            self._write(self.backup_path, text)
        else:
            log.warning(
                "config.ini parsed with %d warning(s), so the known-good backup was left "
                "untouched", len(config.warnings),
            )

        log.info("config.ini updated from the drive: %s", config.summary())
        return True

    def _write(self, path: Path, text: str) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(path.suffix + ".tmp")
            temp.write_text(text, encoding="utf-8")
            temp.replace(path)
        except OSError as exc:
            log.warning("could not write %s: %s", path, exc)

    def _publish_default(self) -> None:
        """Seed the drive with the documented default so there is a starting point."""
        self._write(self.cache_path, DEFAULT_CONFIG_INI)
        self._write(self.backup_path, DEFAULT_CONFIG_INI)
        try:
            self.backend.upload(self.cache_path, self.remote_path)
            log.info("published a default config.ini to %s", self.remote_path)
        except UploadError as exc:
            log.warning("could not publish a default config.ini: %s", exc)
        self._last_text = DEFAULT_CONFIG_INI
        self.config = parse_config_ini(DEFAULT_CONFIG_INI)
        self.source = SOURCE_DRIVE

    def push_master_log(self, local_path: Path) -> bool:
        """Upload the master log so the drive always shows what the node has done."""
        if not local_path.is_file():
            return False
        try:
            self.backend.upload(local_path, self.master_log_remote_path)
            self.online = True
            return True
        except UploadError as exc:
            self.online = False
            log.warning("could not upload the master log: %s", exc)
            return False
