"""Forwards readings, audio analysis and recordings to the Flask server.

The Flask app (Queen Bee Watch, on the Windows PC) is a second destination
alongside Drive, not a replacement for it. It is also the less reliable one:
the PC sleeps, Windows restarts for updates, the link drops. So nothing is sent
straight from the sampling code. Everything goes into an outbox first, a small
SQLite file of its own, and a background pass drains it whenever the server
answers. A reading taken while the PC was off arrives when it comes back,
stamped with when it was measured, not when it was sent.

Three kinds of thing go across:

    readings   POST /api/v1/hives/<id>/readings                (exists on Flask)
    analysis   POST /api/v1/hives/<id>/audio-analysis          (to be added)
    audio      PUT  /api/v1/hives/<id>/recordings/<filename>   (to be added)

Readings and analysis rows reach the outbox two ways: live, as each one is
produced, and from a scan of the CSVs still on the card, which is the backfill.
Both key a row the same way (a reading by its UTC timestamp, an analysis by its
recording's filename), so a row seen live and found again in a CSV goes once.

Recordings are not copied into the outbox; they are sent from the spool. The
Drive uploader deletes audio once it lands on Drive, so it asks :meth:`holds`
before deleting, and keeps any recording Flask has not had yet.

The contract with the server is written up in docs/FLASK-API.md.
"""

from __future__ import annotations

import csv
import json
import logging
import re
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from ..config import Config

log = logging.getLogger(__name__)

PENDING = "pending"
SENT = "sent"
REJECTED = "rejected"

READING = "reading"
ANALYSIS = "analysis"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    kind        TEXT NOT NULL,
    key         TEXT NOT NULL,
    payload     TEXT,
    status      TEXT NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    created_at  REAL NOT NULL,
    sent_at     REAL,
    PRIMARY KEY (kind, key)
);

CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(kind, status, key);

CREATE TABLE IF NOT EXISTS recordings (
    filename    TEXT PRIMARY KEY,
    size        INTEGER NOT NULL,
    status      TEXT NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    sent_at     REAL
);

CREATE TABLE IF NOT EXISTS scanned (
    path        TEXT PRIMARY KEY,
    size        INTEGER NOT NULL
);
"""

# What crosses to the server, and as which type. CSV cells are strings; the
# server range-checks numbers, so they go as numbers.
_READING_FIELDS: dict[str, type] = {
    "temperature_c": float,
    "humidity_pct": float,
    "pressure_hpa": float,
    "voc_raw": int,
    "voc_index": int,
}
_ANALYSIS_FIELDS: dict[str, type] = {
    "sample_rate_hz": int,
    "duration_s": float,
    "rms_dbfs": float,
    "peak_dbfs": float,
    "spl_estimate_db": float,
    "calibration_db_offset": float,
    "dominant_hz": float,
}

# The recorder names files after its start time in local time: 20261025_125215.WAV
_RECORDING_NAME = re.compile(r"(\d{8})_(\d{6})")


class FlaskUnavailable(Exception):
    """The server cannot take anything now: unreachable, erroring, or refusing the key.

    Ends the pass. Everything still queued is retried on the next one.
    """


class _MissingEndpoint(Exception):
    """The server has no route for this kind yet. Skip the kind, carry on with the rest."""


class FlaskForwarder:
    """The outbox, and the pass that drains it.

    Touched from the main loop (live readings), the recorder thread (analysis)
    and the forwarding thread, so the connection is shared under a lock, as in
    :class:`~iot_node.state.Store`.
    """

    def __init__(
        self,
        cfg: Config,
        *,
        session: requests.Session | None = None,
        path: Path | None = None,
    ) -> None:
        self.cfg = cfg
        self.fc = cfg.flask
        self.base = f"{self.fc.url.rstrip('/')}/api/v1/hives/{self.fc.hive_id}"
        self.session = session if session is not None else requests.Session()
        self.tz = _zone(cfg.device.timezone)
        # None until the first request: "never tried" is not the same as "down".
        self.online: bool | None = None
        self._missing: set[str] = set()

        path = path or cfg.resolve("var/state") / "flask_outbox.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, timeout=30.0, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(_SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.session.close()
        with self._lock:
            self.db.close()

    # --- filling the outbox ---------------------------------------------------

    def enqueue_reading(self, record: dict[str, Any]) -> bool:
        """Queue one live sample, as returned by ``SensorManager.sample()``."""
        errors = "; ".join(
            f"{key.removesuffix('_error')}: {value}"
            for key, value in record.items()
            if key.endswith("_error")
        )
        return self._enqueue_reading(record.get("ts", ""), record, errors)

    def enqueue_analysis(self, row: dict[str, Any]) -> bool:
        """Queue one analysis result, as written to the analysis CSV."""
        payload = self._analysis_payload(row)
        if payload is None:
            return False
        return self._enqueue(ANALYSIS, payload["filename"], payload)

    def _enqueue_reading(self, ts: str, values: dict[str, Any], errors: str) -> bool:
        payload: dict[str, Any] = {"recorded_at": ts}
        for field, kind in _READING_FIELDS.items():
            number = _number(values.get(field), kind)
            if number is not None:
                payload[field] = number
        # A sample where every sensor failed has nothing to chart. It stays in
        # the CSV on Drive, errors and all; the server would only show a blank.
        if not ts or len(payload) == 1:
            return False
        payload["device_id"] = self.cfg.device.id
        if errors:
            payload["errors"] = errors
        return self._enqueue(READING, ts, payload)

    def _analysis_payload(self, row: dict[str, Any]) -> dict[str, Any] | None:
        filename = str(row.get("filename") or "")
        if not filename:
            return None
        payload: dict[str, Any] = {
            "filename": filename,
            "recorded_at": self.recorded_at(filename),
            "analysed_at": row.get("analysed_at_utc") or None,
            "device_id": self.cfg.device.id,
        }
        for field, kind in _ANALYSIS_FIELDS.items():
            payload[field] = _number(row.get(field), kind)
        payload["band_hz"] = str(row.get("band_hz") or "")
        payload["audio_device"] = str(row.get("device") or "")
        peaks = []
        index = 1
        while f"peak{index}_hz" in row:
            hz = _number(row.get(f"peak{index}_hz"), float)
            db = _number(row.get(f"peak{index}_db"), float)
            if hz is not None and db is not None:
                peaks.append({"hz": hz, "db": db})
            index += 1
        payload["peaks"] = peaks
        return payload

    def _enqueue(self, kind: str, key: str, payload: dict[str, Any]) -> bool:
        """Add a row unless one with this key is already queued or sent."""
        with self._lock:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO outbox (kind, key, payload, status, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (kind, key, json.dumps(payload), PENDING, time.time()),
            )
            self.db.commit()
        return cur.rowcount > 0

    def scan_backfill(self) -> int:
        """Queue every row in the CSVs still on the card that the server lacks.

        Covers the open day in var/current and finished days waiting in the
        spool. Days already uploaded to Drive and purged are gone from the card
        and are not recovered here. A file is re-read only when its size has
        changed, so the open day costs one read per new sample, not one per pass.
        """
        directories = [self.cfg.resolve("var/current"), self.cfg.spool_dir / "sensors"]
        added = 0
        for directory in directories:
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.csv")):
                if path.name.startswith("sensors_"):
                    added += self._scan(path, READING)
                elif path.name.startswith("audio_analysis_"):
                    added += self._scan(path, ANALYSIS)
        if added:
            log.info("flask backfill: queued %d row(s) from CSVs on the card", added)
        return added

    def _scan(self, path: Path, kind: str) -> int:
        try:
            size = path.stat().st_size
        except OSError:
            return 0
        with self._lock:
            row = self.db.execute(
                "SELECT size FROM scanned WHERE path = ?", (str(path),)
            ).fetchone()
        if row is not None and row["size"] == size:
            return 0

        added = 0
        try:
            with path.open(newline="", encoding="utf-8") as handle:
                for line in csv.DictReader(handle):
                    if kind == READING:
                        ts = line.get("timestamp_utc") or ""
                        added += self._enqueue_reading(ts, line, line.get("errors") or "")
                    else:
                        added += self.enqueue_analysis(line)
        except (OSError, csv.Error, UnicodeDecodeError) as exc:
            log.warning("flask backfill: could not read %s: %s", path.name, exc)
            return added

        with self._lock:
            self.db.execute(
                "INSERT OR REPLACE INTO scanned (path, size) VALUES (?, ?)", (str(path), size)
            )
            self.db.commit()
        return added

    # --- draining it ----------------------------------------------------------

    def run_once(self) -> dict[str, int]:
        """One pass: backfill scan, then readings, analysis, audio, in that order.

        Readings go first because they are what the dashboard shows live; a
        recording can be tens of megabytes and should never hold them up.
        """
        sent = {READING: 0, ANALYSIS: 0, "audio": 0}
        if self.fc.backfill:
            self.scan_backfill()
        try:
            for kind, endpoint, field in (
                (READING, "readings", "readings"),
                (ANALYSIS, "audio-analysis", "results"),
            ):
                try:
                    sent[kind] = self._drain(kind, endpoint, field)
                except _MissingEndpoint:
                    continue
            if self.fc.send_audio:
                try:
                    sent["audio"] = self._send_recordings()
                except _MissingEndpoint:
                    pass
        except FlaskUnavailable as exc:
            log.warning("flask: %s — will retry", exc)
        if any(sent.values()):
            log.info(
                "flask: sent %d reading(s), %d analysis row(s), %d recording(s)",
                sent[READING], sent[ANALYSIS], sent["audio"],
            )
        return sent

    def _drain(self, kind: str, endpoint: str, field: str) -> int:
        sent = 0
        while True:
            batch = self._pending(kind, self.fc.batch_size)
            if not batch:
                return sent
            sent += self._deliver(kind, endpoint, field, batch)

    def _deliver(self, kind: str, endpoint: str, field: str, batch: list[sqlite3.Row]) -> int:
        """Send a batch; if the server refuses it, find the bad rows one by one.

        The server validates a batch as a whole, so one reading out of range
        (a sensor glitch, say) would otherwise block every reading behind it
        forever.
        """
        body = {field: [json.loads(row["payload"]) for row in batch]}
        response = self._request("POST", f"{self.base}/{endpoint}", json=body)
        outcome = self._outcome(response, endpoint)
        if outcome == SENT:
            self._mark(kind, [row["key"] for row in batch], SENT)
            return len(batch)
        if len(batch) == 1:
            error = _error_text(response)
            log.warning("flask refused %s %s: %s", kind, batch[0]["key"], error)
            self._mark(kind, [batch[0]["key"]], REJECTED, error)
            return 0
        return sum(self._deliver(kind, endpoint, field, [row]) for row in batch)

    def _send_recordings(self) -> int:
        directory = self.cfg.spool_dir / "audio"
        if not directory.is_dir():
            return 0
        sent = 0
        for path in sorted(directory.iterdir()):
            if not path.is_file() or path.suffix.lower() != ".wav":
                continue  # .WAV.part is still being written
            try:
                outcome = self._send_recording(path)
            except OSError as exc:
                # Gone mid-pass: the low-disk purge dropped it. Nothing to send.
                log.warning("flask: could not send %s: %s", path.name, exc)
                continue
            sent += outcome == SENT
        return sent

    def _send_recording(self, path: Path) -> str | None:
        """PUT one recording unless the server already has it. Returns the outcome."""
        size = path.stat().st_size
        with self._lock:
            row = self.db.execute(
                "SELECT status, size FROM recordings WHERE filename = ?", (path.name,)
            ).fetchone()
        if row is not None and row["status"] != PENDING and row["size"] == size:
            return None

        headers = {"Content-Type": "audio/wav"}
        recorded = self.recorded_at(path.name)
        if recorded:
            headers["X-Recorded-At"] = recorded
        with path.open("rb") as handle:
            response = self._request(
                "PUT",
                f"{self.base}/recordings/{quote(path.name)}",
                data=handle,
                headers=headers,
                timeout=(self.fc.timeout_s, self.fc.audio_timeout_s),
            )
        outcome = self._outcome(response, "recordings")
        error = None if outcome == SENT else _error_text(response)
        if error:
            log.warning("flask refused recording %s: %s", path.name, error)
        with self._lock:
            self.db.execute(
                "INSERT INTO recordings (filename, size, status, attempts, last_error, sent_at)"
                " VALUES (?, ?, ?, 1, ?, ?)"
                " ON CONFLICT(filename) DO UPDATE SET size = excluded.size,"
                "   status = excluded.status, attempts = attempts + 1,"
                "   last_error = excluded.last_error, sent_at = excluded.sent_at",
                (path.name, size, outcome, error, time.time() if outcome == SENT else None),
            )
            self.db.commit()
        if outcome == SENT:
            log.info("flask: sent %s (%.1f MiB)", path.name, size / 1048576)
        return outcome

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        headers = {"X-API-Key": self.fc.api_key, **kwargs.pop("headers", {})}
        kwargs.setdefault("timeout", self.fc.timeout_s)
        try:
            response = self.session.request(method, url, headers=headers, **kwargs)
        except requests.RequestException as exc:
            self.online = False
            raise FlaskUnavailable(f"{self.fc.url} unreachable ({exc.__class__.__name__})") from exc
        self.online = True
        return response

    def _outcome(self, response: requests.Response, endpoint: str) -> str:
        """Map a response to SENT or REJECTED, or raise to stop."""
        status = response.status_code
        if 200 <= status < 300 or (status == 409 and endpoint == "recordings"):
            self._missing.discard(endpoint)
            return SENT
        if status in (400, 413, 422):
            return REJECTED
        if status == 404:
            # The hive id is wrong, or the server predates this endpoint. Say
            # so once, not every 30 seconds; the rows wait until it exists.
            if endpoint not in self._missing:
                self._missing.add(endpoint)
                log.warning(
                    "flask has no /%s for hive %d (404) — see docs/FLASK-API.md; "
                    "queued rows are kept until it does",
                    endpoint, self.fc.hive_id,
                )
            raise _MissingEndpoint(endpoint)
        if status in (401, 403):
            raise FlaskUnavailable(
                f"API key refused ({status}) — flask.api_key must match the server's "
                "DEFAULT_HIVE_API_KEY"
            )
        raise FlaskUnavailable(f"server error {status}: {_error_text(response)}")

    # --- bookkeeping ----------------------------------------------------------

    def _pending(self, kind: str, limit: int) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self.db.execute(
                    "SELECT key, payload FROM outbox WHERE kind = ? AND status = ?"
                    " ORDER BY key LIMIT ?",
                    (kind, PENDING, limit),
                )
            )

    def _mark(self, kind: str, keys: list[str], status: str, error: str | None = None) -> None:
        # A sent row keeps only its key, which is all deduplication needs; the
        # payload would be a second copy of the CSV that never shrinks.
        with self._lock:
            self.db.executemany(
                "UPDATE outbox SET status = ?, last_error = ?, attempts = attempts + 1,"
                " sent_at = ?, payload = CASE WHEN ? = ? THEN NULL ELSE payload END"
                " WHERE kind = ? AND key = ?",
                [
                    (status, error, time.time() if status == SENT else None, status, SENT,
                     kind, key)
                    for key in keys
                ],
            )
            self.db.commit()

    def holds(self, path: Path) -> bool:
        """True while a spooled recording still has to reach the server.

        The Drive uploader asks this before deleting. A rejected recording is
        not held: the server has said no, and keeping it would fill the card.
        """
        if not self.fc.send_audio or path.suffix.lower() != ".wav":
            return False
        if path.parent != self.cfg.spool_dir / "audio":
            return False
        with self._lock:
            row = self.db.execute(
                "SELECT status FROM recordings WHERE filename = ?", (path.name,)
            ).fetchone()
        return row is None or row["status"] == PENDING

    def counts(self) -> dict[str, dict[str, int]]:
        """Rows per status, per kind: {"reading": {"pending": 3, "sent": 1200}, ...}."""
        out: dict[str, dict[str, int]] = {}
        with self._lock:
            for row in self.db.execute(
                "SELECT kind, status, COUNT(*) AS n FROM outbox GROUP BY kind, status"
            ):
                out.setdefault(row["kind"], {})[row["status"]] = row["n"]
            for row in self.db.execute(
                "SELECT status, COUNT(*) AS n FROM recordings GROUP BY status"
            ):
                out.setdefault("audio", {})[row["status"]] = row["n"]
        return out

    def recorded_at(self, filename: str) -> str | None:
        """When a recording started, in UTC, from its local-time filename."""
        match = _RECORDING_NAME.search(filename)
        if not match:
            return None
        try:
            local = datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S")
        except ValueError:
            return None
        moment = local.replace(tzinfo=self.tz).astimezone(UTC)
        return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _number(value: Any, kind: type) -> float | int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return kind(float(value))
    except (TypeError, ValueError):
        return None


def _error_text(response: requests.Response) -> str:
    try:
        body = response.json()
        if isinstance(body, dict) and body.get("error"):
            return str(body["error"])[:300]
    except ValueError:
        pass
    return (response.text or f"HTTP {response.status_code}")[:300]


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")
