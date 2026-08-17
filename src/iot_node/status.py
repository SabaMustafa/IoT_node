"""The live status snapshot the dashboard reads.

The daemon writes a small JSON file; the dashboard reads it. They deliberately
share nothing else — no sockets, no shared memory, no imports in either
direction. That means a crashed or missing dashboard cannot affect recording,
and a restarting daemon cannot take the screen down with it: the dashboard just
shows the data going stale, which is exactly the right thing for a human to
see.

Writes are atomic (temp file plus rename), so the dashboard can never catch a
half-written file.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

STATUS_NAME = "status.json"

# Touching this file asks the node to re-read config.ini immediately instead of
# waiting for its next scheduled fetch. A file rather than a socket keeps the
# dashboard decoupled: it can ask for a refresh without importing, connecting
# to, or being able to disturb the node in any other way.
REFRESH_REQUEST_NAME = "refresh.request"

# Same mechanism for "record a few seconds now and tell me what you heard".
# The node owns the microphone — only one process can open it — so the test
# has to be performed by the node, not by whatever asked for it.
MIC_TEST_REQUEST_NAME = "mictest.request"


def request_mic_test(state_dir: Path, seconds: float = 5.0) -> bool:
    """Ask the node to record a short test clip and report on it."""
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / MIC_TEST_REQUEST_NAME).write_text(f"{seconds:g}")
        return True
    except OSError as exc:
        log.warning("could not request a microphone test: %s", exc)
        return False


def take_mic_test_request(state_dir: Path) -> float | None:
    """Consume a pending request, returning the requested duration."""
    path = state_dir / MIC_TEST_REQUEST_NAME
    try:
        if not path.exists():
            return None
        raw = path.read_text().strip()
        path.unlink()
    except OSError:
        return None
    try:
        return max(1.0, min(30.0, float(raw)))
    except ValueError:
        return 5.0


def request_refresh(state_dir: Path) -> bool:
    """Ask the node to re-read config.ini on its next tick."""
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / REFRESH_REQUEST_NAME).touch()
        return True
    except OSError as exc:
        log.warning("could not request a config refresh: %s", exc)
        return False


def take_refresh_request(state_dir: Path) -> bool:
    """Consume a pending request. True if one was waiting."""
    path = state_dir / REFRESH_REQUEST_NAME
    try:
        if not path.exists():
            return False
        path.unlink()
        return True
    except OSError:
        return False

# The dashboard calls the node "offline" if the snapshot stops updating for
# this long. Generous enough to survive a slow upload pass.
STALE_AFTER_S = 180.0


@dataclass
class Status:
    """Everything the screen needs, in one flat structure."""

    device_id: str = ""
    site: str = ""
    updated_utc: str = ""
    updated_local: str = ""
    updated_epoch: float = 0.0
    uptime_s: float = 0.0

    # Live environment
    temperature_c: float | None = None
    humidity_pct: float | None = None
    pressure_hpa: float | None = None
    voc_index: int | None = None
    voc_raw: int | None = None
    sensor_error: str = ""

    # Acoustics
    last_recording: str = ""
    last_recording_utc: str = ""
    dominant_hz: float | None = None
    rms_dbfs: float | None = None
    spl_estimate_db: float | None = None
    peaks: list[dict[str, float]] = field(default_factory=list)
    audio_device: str = ""
    peak_dbfs: float | None = None
    analysis_min_hz: float = 20.0
    analysis_max_hz: float = 2000.0
    recording_elapsed_s: float = 0.0
    recording_total_s: float = 0.0
    analysed_today: int = 0

    # Live microphone test — proves which device is really being recorded from.
    mic_test_state: str = ""  # "", "running", "ok", "failed"
    mic_test_at: str = ""
    mic_test_epoch: float = 0.0
    mic_test_device: str = ""
    mic_test_is_audiomoth: bool = False
    mic_test_seconds: float = 0.0
    mic_test_rate: int = 0
    mic_test_channels: int = 0
    mic_test_rms_dbfs: float | None = None
    mic_test_peak_dbfs: float | None = None
    mic_test_dominant_hz: float | None = None
    mic_test_peaks: list[dict[str, float]] = field(default_factory=list)
    mic_test_message: str = ""

    # Live input level while a recording is in progress, so the screen shows
    # the microphone working rather than only reporting once it has finished.
    live_db: float | None = None
    history_live_db: list[float] = field(default_factory=list)

    def mic_test_age_s(self) -> float:
        return time.time() - self.mic_test_epoch if self.mic_test_epoch else float("inf")

    # Schedule
    hour: int = 0
    sensors_on_now: bool = False
    microphone_on_now: bool = False
    recording_now: bool = False
    sensor_hours: list[int] = field(default_factory=list)
    microphone_hours: list[int] = field(default_factory=list)
    sample_rate: int = 0
    upload_frequency: str = ""
    config_source: str = ""

    # Housekeeping
    queue_pending: int = 0
    queue_uploaded_today: int = 0
    samples_today: int = 0
    recordings_today: int = 0
    disk_free_gb: float = 0.0
    disk_total_gb: float = 0.0
    disk_free_pct: float = 100.0
    cloud_free_gb: float | None = None
    last_upload_utc: str = ""
    config_fetched_utc: str = ""
    online: bool = False
    healthy: bool = True
    notes: str = ""

    # Recent history for the sparklines, oldest first.
    history_temperature: list[float] = field(default_factory=list)
    history_humidity: list[float] = field(default_factory=list)
    history_voc: list[float] = field(default_factory=list)
    history_db: list[float] = field(default_factory=list)

    def age_s(self) -> float:
        return max(0.0, time.time() - self.updated_epoch) if self.updated_epoch else float("inf")

    def is_stale(self) -> bool:
        return self.age_s() > STALE_AFTER_S


HISTORY_LIMIT = 120  # ~6 hours at one sample every 3 minutes


class StatusWriter:
    """Owned by the daemon. Accumulates state and publishes it atomically."""

    def __init__(self, path: Path, device_id: str, site: str = "") -> None:
        self.path = path
        self.status = Status(device_id=device_id, site=site)

    def update(self, **fields: Any) -> None:
        for key, value in fields.items():
            if hasattr(self.status, key):
                setattr(self.status, key, value)

    def push_history(self, **series: float | None) -> None:
        for name, value in series.items():
            if value is None:
                continue
            attribute = f"history_{name}"
            history = getattr(self.status, attribute, None)
            if history is None:
                continue
            history.append(round(float(value), 2))
            del history[:-HISTORY_LIMIT]

    def record_disk(self, spool_dir: Path) -> None:
        try:
            usage = shutil.disk_usage(spool_dir)
        except OSError:
            return
        self.status.disk_free_gb = usage.free / 1e9
        self.status.disk_total_gb = usage.total / 1e9
        self.status.disk_free_pct = usage.free / usage.total * 100.0

    def publish(self) -> None:
        """Stamp the time and write the file atomically."""
        now = datetime.now(UTC)
        self.status.updated_utc = now.isoformat(timespec="seconds").replace("+00:00", "Z")
        self.status.updated_local = now.astimezone().isoformat(timespec="seconds")
        self.status.updated_epoch = time.time()

        payload = json.dumps(asdict(self.status), separators=(",", ":"))
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(payload, encoding="utf-8")
            os.replace(temp, self.path)
        except OSError as exc:
            log.debug("could not publish status: %s", exc)


def read_status(path: Path) -> Status | None:
    """Read a snapshot. Returns None if absent or unreadable."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None

    status = Status()
    for key, value in raw.items():
        if hasattr(status, key):
            setattr(status, key, value)
    return status
