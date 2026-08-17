"""Discover finished AudioMoth recordings and stage them for upload.

The AudioMoth writes WAVs to its SD card (mounted over USB, or copied into a
local directory by whatever is driving the recorder). We never upload straight
from the source: files are staged into the spool first, so a card can be
unmounted at any moment without stranding a half-finished upload.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from ..config import Config
from ..state import Store

log = logging.getLogger(__name__)

# Directories a removable card typically appears under.
_MOUNT_ROOTS = (Path("/media"), Path("/mnt"), Path("/run/media"))

# An AudioMoth card is recognised by its config file at the volume root.
_AUDIOMOTH_MARKERS = ("CONFIG.TXT", "config.txt")


def find_audiomoth_mounts(roots: tuple[Path, ...] = _MOUNT_ROOTS) -> list[Path]:
    """Return mounted volumes that look like an AudioMoth SD card."""
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        # Cards land at /media/<volume> or /media/<user>/<volume>.
        candidates: list[Path] = []
        try:
            for entry in root.iterdir():
                if not entry.is_dir():
                    continue
                candidates.append(entry)
                try:
                    candidates.extend(c for c in entry.iterdir() if c.is_dir())
                except OSError:
                    continue
        except OSError as exc:
            log.debug("cannot scan %s: %s", root, exc)
            continue

        for candidate in candidates:
            if any((candidate / marker).is_file() for marker in _AUDIOMOTH_MARKERS):
                found.append(candidate)
    return found


def parse_audiomoth_config(volume: Path) -> dict[str, str]:
    """Parse an AudioMoth CONFIG.TXT into a flat dict.

    The file is a human-readable "Key : value" dump written by the AudioMoth
    configuration app. Captured alongside the recordings so the deployment
    settings travel with the data.
    """
    for marker in _AUDIOMOTH_MARKERS:
        path = volume / marker
        if path.is_file():
            break
    else:
        return {}

    settings: dict[str, str] = {}
    try:
        text = path.read_text(errors="replace")
    except OSError as exc:
        log.warning("cannot read %s: %s", path, exc)
        return {}

    for line in text.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if key:
            settings[key] = value.strip()
    return settings


class AudioIngest:
    """Scans the source directories and stages stable recordings into the spool."""

    def __init__(self, cfg: Config, store: Store) -> None:
        self.cfg = cfg
        self.store = store
        self.spool = cfg.spool_dir / "audio"
        # Remembers sizes between scans so we can tell "finished" from "still growing".
        self._sizes: dict[Path, int] = {}

    def source_dirs(self) -> list[Path]:
        dirs = [self.cfg.resolve(d) for d in self.cfg.audio.source_dirs]
        if self.cfg.audio.auto_detect:
            dirs.extend(find_audiomoth_mounts())
        # Preserve order, drop duplicates and anything that vanished.
        seen: set[Path] = set()
        out: list[Path] = []
        for d in dirs:
            try:
                resolved = d.resolve()
            except OSError:
                continue
            if resolved in seen or not resolved.is_dir():
                continue
            seen.add(resolved)
            out.append(resolved)
        return out

    def scan(self) -> list[Path]:
        """Stage every new, finished recording. Returns the spooled paths."""
        staged: list[Path] = []
        for source_dir in self.source_dirs():
            for path in self._candidates(source_dir):
                spooled = self._ingest(path)
                if spooled is not None:
                    staged.append(spooled)
        return staged

    def _candidates(self, source_dir: Path) -> list[Path]:
        matches: list[Path] = []
        for pattern in self.cfg.audio.patterns:
            matches.extend(source_dir.glob(pattern))
        # Case-insensitive globs can return the same file twice.
        return sorted({m for m in matches if m.is_file()})

    def _is_stable(self, path: Path, size: int, mtime: float) -> bool:
        """A file counts as finished once it is old enough and stopped growing."""
        import time

        if size < self.cfg.audio.min_size_bytes:
            return False
        if time.time() - mtime < self.cfg.audio.min_age_s:
            self._sizes[path] = size
            return False
        previous = self._sizes.get(path)
        self._sizes[path] = size
        # First sighting of an already-old file is safe to take immediately.
        return previous is None or previous == size

    def _ingest(self, path: Path) -> Path | None:
        try:
            stat = path.stat()
        except OSError:
            return None

        key = Store.source_key(path, stat.st_size, stat.st_mtime)
        if self.store.is_ingested(key):
            return None
        if not self._is_stable(path, stat.st_size, stat.st_mtime):
            return None

        self.spool.mkdir(parents=True, exist_ok=True)
        target = self._unique_target(path)
        # Stage via a .part file so the uploader never sees a partial copy.
        partial = target.with_name(target.name + ".part")
        try:
            if self.cfg.audio.action == "move":
                shutil.move(str(path), str(partial))
            else:
                shutil.copy2(str(path), str(partial))
            os.replace(partial, target)
        except OSError as exc:
            log.error("failed to stage %s: %s", path, exc)
            partial.unlink(missing_ok=True)
            return None

        self.store.mark_ingested(key, path, target)
        self.store.enqueue(target, "audio", target.stat().st_size)
        log.info("staged %s (%.1f MiB)", target.name, target.stat().st_size / 1048576)
        self._sizes.pop(path, None)
        return target

    def _unique_target(self, source: Path) -> Path:
        """Avoid collisions when two cards hold identically named recordings."""
        target = self.spool / source.name
        if not target.exists():
            return target
        stem, suffix = source.stem, source.suffix
        for n in range(1, 1000):
            candidate = self.spool / f"{stem}_{n}{suffix}"
            if not candidate.exists():
                return candidate
        raise RuntimeError(f"cannot find a free name for {source.name} in {self.spool}")
