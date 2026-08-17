"""SQLite bookkeeping: what has been ingested, what has been uploaded.

Kept deliberately small — it exists so that a power cut mid-upload, or a source
directory that is never cleared, cannot cause duplicate uploads or a re-ingest
storm on restart.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

log = logging.getLogger(__name__)

PENDING = "pending"
UPLOADED = "uploaded"
FAILED = "failed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ingested (
    source_key  TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    spool_path  TEXT NOT NULL,
    ingested_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS uploads (
    spool_path  TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    size        INTEGER NOT NULL,
    remote_path TEXT,
    status      TEXT NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    created_at  REAL NOT NULL,
    uploaded_at REAL
);

CREATE INDEX IF NOT EXISTS uploads_status ON uploads(status);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30.0)
        self.db.row_factory = sqlite3.Row
        # WAL survives an unclean shutdown far better than the default journal.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(_SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- ingest ---------------------------------------------------------------

    @staticmethod
    def source_key(path: Path, size: int, mtime: float) -> str:
        """Identify a source recording by path, size and mtime.

        Size and mtime are part of the key so a genuinely new file that reuses a
        filename (a reformatted card, say) is not mistaken for an old one.
        """
        return f"{path.resolve()}|{size}|{int(mtime)}"

    def is_ingested(self, key: str) -> bool:
        row = self.db.execute("SELECT 1 FROM ingested WHERE source_key = ?", (key,)).fetchone()
        return row is not None

    def mark_ingested(self, key: str, source: Path, spool: Path) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO ingested (source_key, source_path, spool_path, ingested_at)"
            " VALUES (?, ?, ?, ?)",
            (key, str(source), str(spool), time.time()),
        )
        self.db.commit()

    def prune_ingested(self, older_than_s: float) -> int:
        cutoff = time.time() - older_than_s
        cur = self.db.execute("DELETE FROM ingested WHERE ingested_at < ?", (cutoff,))
        self.db.commit()
        return cur.rowcount

    # --- uploads --------------------------------------------------------------

    def enqueue(self, spool_path: Path, kind: str, size: int) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO uploads (spool_path, kind, size, status, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (str(spool_path), kind, size, PENDING, time.time()),
        )
        self.db.commit()

    def pending(self, limit: int = 100) -> list[sqlite3.Row]:
        return list(
            self.db.execute(
                "SELECT * FROM uploads WHERE status IN (?, ?)"
                " ORDER BY attempts ASC, created_at ASC LIMIT ?",
                (PENDING, FAILED, limit),
            )
        )

    def mark_uploaded(self, spool_path: Path, remote_path: str) -> None:
        self.db.execute(
            "UPDATE uploads SET status = ?, remote_path = ?, uploaded_at = ?, last_error = NULL"
            " WHERE spool_path = ?",
            (UPLOADED, remote_path, time.time(), str(spool_path)),
        )
        self.db.commit()

    def mark_failed(self, spool_path: Path, error: str) -> None:
        self.db.execute(
            "UPDATE uploads SET status = ?, attempts = attempts + 1, last_error = ?"
            " WHERE spool_path = ?",
            (FAILED, error[:500], str(spool_path)),
        )
        self.db.commit()

    def uploaded_files(self, before: float | None = None) -> Iterator[sqlite3.Row]:
        if before is None:
            yield from self.db.execute(
                "SELECT * FROM uploads WHERE status = ? ORDER BY uploaded_at ASC", (UPLOADED,)
            )
        else:
            yield from self.db.execute(
                "SELECT * FROM uploads WHERE status = ? AND uploaded_at < ?"
                " ORDER BY uploaded_at ASC",
                (UPLOADED, before),
            )

    def forget(self, spool_path: Path) -> None:
        self.db.execute("DELETE FROM uploads WHERE spool_path = ?", (str(spool_path),))
        self.db.commit()

    def counts(self) -> dict[str, int]:
        rows = self.db.execute("SELECT status, COUNT(*) AS n FROM uploads GROUP BY status")
        return {row["status"]: row["n"] for row in rows}
