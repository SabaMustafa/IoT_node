"""CSV outputs: one file per day of sensor readings, and the master log.

Two different jobs:

* :class:`DailyCsvWriter` accumulates readings into ``<date>.csv``. The open
  day lives in ``var/current`` and is moved into the spool once the date rolls
  over, so a partly written day is never uploaded and then uploaded again.
* :class:`MasterLog` maintains one row per day summarising what the node
  actually did — how many readings, how much audio, at what rate, whether it
  was uploaded. It sits beside config.ini on the drive, so the operator can see
  the whole deployment at a glance without opening a single data file.
"""

from __future__ import annotations

import csv
import logging
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Fixed column order so every daily file is comparable and diffable.
SENSOR_COLUMNS = [
    "timestamp_utc",
    "timestamp_local",
    "device_id",
    "site",
    "temperature_c",
    "humidity_pct",
    "pressure_hpa",
    "voc_raw",
    "voc_index",
    "errors",
]

MASTER_COLUMNS = [
    "date",
    "sensor_hours",
    "mic_hours",
    "sensor_samples",
    "audio_files",
    "audio_seconds",
    "audio_mib",
    "sample_rate_hz",
    "analysed_files",
    "mean_dbfs",
    "dominant_hz",
    "uploaded_files",
    "upload_frequency",
    "restarts",
    "last_updated_utc",
    "notes",
]


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


class DailyCsvWriter:
    """Appends sensor readings to a per-day CSV, rolling over at midnight."""

    def __init__(self, current_dir: Path, spool_dir: Path, device_id: str) -> None:
        self.current_dir = current_dir
        self.spool_dir = spool_dir
        self.device_id = device_id
        self._path: Path | None = None
        self._day: str | None = None
        self.rows_today = 0

    def path_for(self, day: str) -> Path:
        return self.current_dir / f"sensors_{self.device_id}_{day}.csv"

    def write(self, record: dict[str, Any], *, day: str | None = None) -> Path:
        """Append one reading, rolling the file over on a date change."""
        day = day or datetime.now().strftime("%Y-%m-%d")
        if day != self._day:
            self.rollover(day)

        assert self._path is not None
        new_file = not self._path.exists()
        self.current_dir.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=SENSOR_COLUMNS, extrasaction="ignore")
            if new_file:
                writer.writeheader()
            writer.writerow(self._row(record))
        self.rows_today += 1
        return self._path

    @staticmethod
    def _row(record: dict[str, Any]) -> dict[str, Any]:
        errors = [
            f"{key.removesuffix('_error')}: {value}"
            for key, value in record.items()
            if key.endswith("_error")
        ]
        return {
            "timestamp_utc": record.get("ts", ""),
            "timestamp_local": record.get("ts_local", ""),
            "device_id": record.get("device_id", ""),
            "site": record.get("site", ""),
            "temperature_c": record.get("temperature_c", ""),
            "humidity_pct": record.get("humidity_pct", ""),
            "pressure_hpa": record.get("pressure_hpa", ""),
            "voc_raw": record.get("voc_raw", ""),
            "voc_index": record.get("voc_index", ""),
            "errors": "; ".join(errors),
        }

    def rollover(self, new_day: str | None) -> Path | None:
        """Close the open day into the spool and start a new one."""
        finished: Path | None = None
        if self._path is not None and self._path.exists():
            target = self.spool_dir / "sensors" / self._path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            self._path.replace(target)
            log.info("sensor CSV ready for upload: %s (%d rows)", target.name, self.rows_today)
            finished = target

        self.rows_today = 0
        self._day = new_day
        self._path = self.path_for(new_day) if new_day else None
        return finished

    def flush(self) -> Path | None:
        """Hand the open day to the spool without starting a new one."""
        return self.rollover(None)

    def publish_today(self) -> Path | None:
        """Copy the in-progress day into the spool so it can be uploaded early.

        Used by the 12-hourly upload schedule: the day is not over, but the
        operator asked to see it now. The original keeps accumulating.
        """
        if self._path is None or not self._path.exists():
            return None
        target = self.spool_dir / "sensors" / self._path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self._path.read_bytes())
        return target


class AnalysisCsvWriter:
    """Daily CSV of frequency-analysis results, one row per recording.

    Kept separate from the sensor CSV because the two have nothing in common
    but a date: different cadence, different columns, and an analyst usually
    wants one or the other, not a wide sparse join of both.
    """

    def __init__(self, current_dir: Path, spool_dir: Path, device_id: str, columns: list[str]):
        self.current_dir = current_dir
        self.spool_dir = spool_dir
        self.device_id = device_id
        self.columns = columns
        self._path: Path | None = None
        self._day: str | None = None
        self.rows_today = 0

    def path_for(self, day: str) -> Path:
        return self.current_dir / f"audio_analysis_{self.device_id}_{day}.csv"

    def write(self, row: dict[str, Any], *, day: str | None = None) -> Path:
        day = day or datetime.now().strftime("%Y-%m-%d")
        if day != self._day:
            self.rollover(day)

        assert self._path is not None
        new_file = not self._path.exists()
        self.current_dir.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.columns, extrasaction="ignore")
            if new_file:
                writer.writeheader()
            writer.writerow(row)
        self.rows_today += 1
        return self._path

    def rollover(self, new_day: str | None) -> Path | None:
        finished: Path | None = None
        if self._path is not None and self._path.exists():
            target = self.spool_dir / "sensors" / self._path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            self._path.replace(target)
            log.info("analysis CSV ready for upload: %s (%d rows)", target.name, self.rows_today)
            finished = target

        self.rows_today = 0
        self._day = new_day
        self._path = self.path_for(new_day) if new_day else None
        return finished

    def flush(self) -> Path | None:
        return self.rollover(None)

    def publish_today(self) -> Path | None:
        if self._path is None or not self._path.exists():
            return None
        target = self.spool_dir / "sensors" / self._path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self._path.read_bytes())
        return target


class MasterLog:
    """One row per day, describing what the node did. Small enough to rewrite."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> dict[str, dict[str, str]]:
        if not self.path.is_file():
            return {}
        try:
            with self.path.open(newline="", encoding="utf-8") as handle:
                return {row["date"]: row for row in csv.DictReader(handle) if row.get("date")}
        except (OSError, csv.Error) as exc:
            log.warning("could not read master log %s: %s", self.path, exc)
            return {}

    def update(self, day: str | date, **fields: Any) -> None:
        """Merge values into a day's row, creating it if new.

        Counters passed as ``+n`` are added to whatever is already there, so
        callers can report increments without reading the file first.
        """
        day = day.isoformat() if isinstance(day, date) else str(day)
        rows = self.read()
        row = rows.get(day, {"date": day})

        for key, value in fields.items():
            if key not in MASTER_COLUMNS:
                continue
            if isinstance(value, str) and value.startswith("+"):
                try:
                    row[key] = _fmt(float(row.get(key) or 0) + float(value[1:]))
                except ValueError:
                    row[key] = value
            else:
                row[key] = _fmt(value)

        row["last_updated_utc"] = _iso(datetime.now(UTC))
        rows[day] = row
        self._write(rows)

    def bump(self, day: str | date, key: str, amount: float = 1) -> None:
        """Increment one counter for a day."""
        self.update(day, **{key: f"+{amount}"})

    def _write(self, rows: dict[str, dict[str, str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        try:
            with temp.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=MASTER_COLUMNS, extrasaction="ignore")
                writer.writeheader()
                for day in sorted(rows):
                    row = rows[day]
                    writer.writerow({column: row.get(column, "") for column in MASTER_COLUMNS})
            temp.replace(self.path)
        except OSError as exc:
            log.warning("could not write master log: %s", exc)
            temp.unlink(missing_ok=True)


def _fmt(value: Any) -> str:
    """Render numbers without trailing .0 so the CSV stays readable."""
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".") if value % 1 else str(int(value))
    return str(value)
