from __future__ import annotations

import csv

from iot_node.sensors.base import Reading, SensorError
from iot_node.sensors.manager import SensorManager


class StubBME:
    def __init__(self, values=None, error: str | None = None) -> None:
        self.values = values or {
            "temperature_c": 21.5, "humidity_pct": 62.0, "pressure_hpa": 1013.2
        }
        self.error = error

    def read(self) -> Reading:
        if self.error:
            raise SensorError(self.error)
        return Reading(sensor="bme280", values=dict(self.values))

    def close(self) -> None:
        pass


class StubSGP:
    def __init__(self) -> None:
        self.seen: list[tuple[float, float]] = []

    def read(self, humidity_pct: float = 50.0, temperature_c: float = 25.0) -> Reading:
        self.seen.append((humidity_pct, temperature_c))
        return Reading(sensor="sgp40", values={"voc_raw": 30000, "voc_index": 100})

    def close(self) -> None:
        pass

    def save_state(self) -> None:
        pass


def test_sample_merges_both_sensors(cfg):
    manager = SensorManager(cfg)
    manager.bme, manager.sgp = StubBME(), StubSGP()

    record = manager.sample()

    assert record["device_id"] == "test-node"
    assert record["ts"].endswith("Z")
    assert record["temperature_c"] == 21.5
    assert record["voc_index"] == 100


def test_bme_readings_compensate_the_sgp40(cfg):
    """The VOC raw signal is humidity- and temperature-dependent."""
    manager = SensorManager(cfg)
    sgp = StubSGP()
    manager.bme, manager.sgp = StubBME(), sgp

    manager.sample()

    assert sgp.seen == [(62.0, 21.5)]


def test_sgp40_falls_back_to_configured_defaults(cfg):
    manager = SensorManager(cfg)
    sgp = StubSGP()
    manager.bme, manager.sgp = None, sgp

    manager.sample()

    assert sgp.seen == [
        (cfg.sensors.sgp40.default_humidity_pct, cfg.sensors.sgp40.default_temperature_c)
    ]


def test_one_failed_sensor_does_not_lose_the_other(cfg):
    manager = SensorManager(cfg)
    manager.bme, manager.sgp = StubBME(error="bus error"), StubSGP()

    record = manager.sample()

    assert "bme280_error" in record
    assert record["voc_raw"] == 30000  # the SGP40 still contributed


def test_records_append_to_todays_csv(cfg):
    manager = SensorManager(cfg)
    manager.bme, manager.sgp = StubBME(), StubSGP()

    manager.write(manager.sample())
    manager.write(manager.sample())

    current = list((cfg.root / "var/current").glob("*.csv"))
    assert len(current) == 1
    rows = list(csv.DictReader(current[0].open(newline="")))
    assert len(rows) == 2  # one header, then two data rows
    assert rows[0]["device_id"] == "test-node"
    assert rows[0]["temperature_c"] == "21.5"
    assert rows[0]["voc_index"] == "100"


def test_csv_carries_both_utc_and_local_time(cfg):
    manager = SensorManager(cfg)
    manager.bme = StubBME()
    manager.write(manager.sample())

    row = next(csv.DictReader(next((cfg.root / "var/current").glob("*.csv")).open(newline="")))
    assert row["timestamp_utc"].endswith("Z")
    assert row["timestamp_local"]  # the timetable is written in local hours


def test_sensor_errors_are_recorded_in_the_csv(cfg):
    manager = SensorManager(cfg)
    manager.bme, manager.sgp = StubBME(error="bus error"), StubSGP()
    manager.write(manager.sample())

    row = next(csv.DictReader(next((cfg.root / "var/current").glob("*.csv")).open(newline="")))
    assert "bme280" in row["errors"]
    assert row["voc_raw"] == "30000"  # the working sensor still logged


def test_day_rollover_moves_the_finished_csv_to_the_spool(cfg):
    manager = SensorManager(cfg)
    manager.bme = StubBME()
    manager.write(manager.sample())

    manager.writer.rollover("2026-08-15")  # the clock crosses local midnight

    spooled = list((cfg.spool_dir / "sensors").glob("*.csv"))
    assert len(spooled) == 1
    assert next(csv.DictReader(spooled[0].open(newline="")))["temperature_c"] == "21.5"


def test_flush_hands_over_the_open_day(cfg):
    manager = SensorManager(cfg)
    manager.bme, manager.sgp = StubBME(), StubSGP()
    manager.write(manager.sample())

    manager.flush()

    assert list((cfg.spool_dir / "sensors").glob("*.csv"))
    assert list((cfg.root / "var/current").glob("*.csv")) == []


def test_available_reflects_what_opened(cfg):
    manager = SensorManager(cfg)
    assert not manager.available
    manager.bme = StubBME()
    assert manager.available
