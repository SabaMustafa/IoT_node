"""Sampling for the I2C sensors, and the daily CSV that feeds uploads.

Readings accumulate in an open CSV under var/current. When the date rolls over
the finished day is moved into the spool, which is what the uploader watches —
so a partly written day is never uploaded twice.

Timestamps are recorded twice: UTC for analysis, and local time because the
recording timetable in config.ini is expressed in local hours, and a reader
comparing the two should not have to do the conversion in their head.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..config import Config
from ..csv_log import DailyCsvWriter
from .base import SensorError
from .bme280 import BME280
from .sgp40 import SGP40

log = logging.getLogger(__name__)


class SensorManager:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.device_id = cfg.device.id
        self.bme: BME280 | None = None
        self.sgp: SGP40 | None = None
        self.tz = _load_timezone(cfg.device.timezone)
        self.writer = DailyCsvWriter(
            current_dir=cfg.resolve("var/current"),
            spool_dir=cfg.spool_dir,
            device_id=cfg.device.id,
        )

    def open(self) -> None:
        """Bring up whichever sensors are configured and reachable.

        A missing sensor is logged and skipped: a node with one working sensor
        should keep recording rather than refuse to start.
        """
        s = self.cfg.sensors
        if s.bme280.enabled:
            bme = BME280(bus=s.i2c_bus, address=s.bme280.address)
            try:
                bme.open()
                self.bme = bme
            except SensorError as exc:
                log.error("BME280 unavailable: %s", exc)

        if s.sgp40.enabled:
            sgp = SGP40(
                bus=s.i2c_bus,
                address=s.sgp40.address,
                sampling_interval_s=s.sample_interval_s,
                state_file=self.cfg.state_db.parent / "voc_state.json",
            )
            try:
                sgp.open()
                self.sgp = sgp
            except SensorError as exc:
                log.error("SGP40 unavailable: %s", exc)

        if not (self.bme or self.sgp):
            log.error("no sensors available on /dev/i2c-%d — sampling disabled", s.i2c_bus)

    def close(self) -> None:
        for sensor in (self.bme, self.sgp):
            if sensor is not None:
                sensor.close()
        self.bme = self.sgp = None

    @property
    def available(self) -> bool:
        return self.bme is not None or self.sgp is not None

    def local_now(self) -> datetime:
        return datetime.now(self.tz)

    def sample(self) -> dict[str, Any]:
        """Read every sensor once and return a flat record."""
        now = datetime.now(UTC)
        record: dict[str, Any] = {
            "ts": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "ts_local": now.astimezone(self.tz).isoformat(timespec="seconds"),
            "device_id": self.device_id,
            "site": self.cfg.device.site,
        }

        humidity = self.cfg.sensors.sgp40.default_humidity_pct
        temperature = self.cfg.sensors.sgp40.default_temperature_c

        if self.bme is not None:
            try:
                values = self.bme.read().values
                record.update(values)
                # Feed the real conditions into the SGP40's compensation.
                humidity = values.get("humidity_pct", humidity)
                temperature = values.get("temperature_c", temperature)
            except SensorError as exc:
                log.warning("BME280 sample failed: %s", exc)
                record["bme280_error"] = str(exc)

        if self.sgp is not None:
            try:
                reading = self.sgp.read(humidity_pct=humidity, temperature_c=temperature)
                record.update(reading.values)
            except SensorError as exc:
                log.warning("SGP40 sample failed: %s", exc)
                record["sgp40_error"] = str(exc)

        return record

    def write(self, record: dict[str, Any]) -> Path:
        """Append one record to today's CSV, rolling over at local midnight."""
        return self.writer.write(record, day=self.local_now().strftime("%Y-%m-%d"))

    def flush(self) -> Path | None:
        """Close out the open day and hand it to the spool (used at shutdown)."""
        finished = self.writer.flush()
        if self.sgp is not None:
            self.sgp.save_state()
        return finished


def _load_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("unknown timezone %r — falling back to UTC", name)
        return ZoneInfo("UTC")
