"""SQLite bookkeeping: what has been ingested, what has been uploaded.

Kept deliberately small — it exists so that a power cut mid-upload, or a source
directory that is never cleared, cannot cause duplicate uploads or a re-ingest
storm on restart.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
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
    """The upload ledger.

    Recording happens on a worker thread, so this is touched from more than one
    thread: sqlite3 refuses that by default, and the connection is guarded by a
    lock rather than opened per thread — the traffic is a handful of rows a
    minute, and one connection keeps the WAL bookkeeping simple.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, timeout=30.0, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        # WAL survives an unclean shutdown far better than the default journal.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(_SCHEMA)
        self.db.commit()

    def close(self) -> None:
        with self._lock:
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
        with self._lock:
            row = self.db.execute(
                "SELECT 1 FROM ingested WHERE source_key = ?", (key,)
            ).fetchone()
        return row is not None

    def mark_ingested(self, key: str, source: Path, spool: Path) -> None:
        with self._lock:
            self.db.execute(
                "INSERT OR REPLACE INTO ingested (source_key, source_path, spool_path, ingested_at)"
                " VALUES (?, ?, ?, ?)",
                (key, str(source), str(spool), time.time()),
            )
            self.db.commit()

    def prune_ingested(self, older_than_s: float) -> int:
        with self._lock:
            cutoff = time.time() - older_than_s
            cur = self.db.execute("DELETE FROM ingested WHERE ingested_at < ?", (cutoff,))
            self.db.commit()
            return cur.rowcount

    # --- uploads --------------------------------------------------------------

    def enqueue(self, spool_path: Path, kind: str, size: int) -> None:
        """Queue a file, re-queueing it if the content at that path has changed.

        Plain INSERT OR IGNORE loses data here. The same spool path is reused
        deliberately: a part-day CSV is published for an early upload, and the
        midnight rollover then writes the *complete* day to that identical
        path. With IGNORE, the finished day is silently dropped because a row
        already says 'uploaded', and housekeeping later deletes it — so Drive
        keeps a truncated morning's readings and the rest is gone. Comparing
        size re-queues the real file instead.
        """
        with self._lock:
            self.db.execute(
                "INSERT INTO uploads (spool_path, kind, size, status, created_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(spool_path) DO UPDATE SET"
                "   status = ?, size = excluded.size, attempts = 0,"
                "   last_error = NULL, uploaded_at = NULL"
                " WHERE uploads.size != excluded.size",
                (str(spool_path), kind, size, PENDING, time.time(), PENDING),
            )
            self.db.commit()

    def pending(self, limit: int = 100) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self.db.execute(
                    "SELECT * FROM uploads WHERE status IN (?, ?)"
                    " ORDER BY attempts ASC, created_at ASC LIMIT ?",
                    (PENDING, FAILED, limit),
                )
            )

    def mark_uploaded(self, spool_path: Path, remote_path: str) -> None:
        with self._lock:
            self.db.execute(
                "UPDATE uploads SET status = ?, remote_path = ?, uploaded_at = ?, last_error = NULL"
                " WHERE spool_path = ?",
                (UPLOADED, remote_path, time.time(), str(spool_path)),
            )
            self.db.commit()

    def mark_failed(self, spool_path: Path, error: str) -> None:
        with self._lock:
            self.db.execute(
                "UPDATE uploads SET status = ?, attempts = attempts + 1, last_error = ?"
                " WHERE spool_path = ?",
                (FAILED, error[:500], str(spool_path)),
            )
            self.db.commit()

    def uploaded_files(self, before: float | None = None) -> Iterator[sqlite3.Row]:
        """Rows are read fully inside the lock, then yielded.

        Yielding from inside the lock would hold it for as long as the caller
        takes to consume the results — and the caller here deletes files, which
        is slow enough to stall the recording thread behind it.
        """
        with self._lock:
            if before is None:
                rows = list(
                    self.db.execute(
                        "SELECT * FROM uploads WHERE status = ? ORDER BY uploaded_at ASC",
                        (UPLOADED,),
                    )
                )
            else:
                rows = list(
                    self.db.execute(
                        "SELECT * FROM uploads WHERE status = ? AND uploaded_at < ?"
                        " ORDER BY uploaded_at ASC",
                        (UPLOADED, before),
                    )
                )
        yield from rows

    def forget(self, spool_path: Path) -> None:
        with self._lock:
            self.db.execute("DELETE FROM uploads WHERE spool_path = ?", (str(spool_path),))
            self.db.commit()

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self.db.execute("SELECT status, COUNT(*) AS n FROM uploads GROUP BY status")
            return {row["status"]: row["n"] for row in rows}

