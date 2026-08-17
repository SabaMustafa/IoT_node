from __future__ import annotations

import csv

from iot_node.csv_log import MASTER_COLUMNS, SENSOR_COLUMNS, DailyCsvWriter, MasterLog


def reading(**overrides):
    record = {
        "ts": "2026-08-14T06:52:28Z",
        "ts_local": "2026-08-14T18:52:28+12:00",
        "device_id": "pi5-node-01",
        "site": "bench",
        "temperature_c": 20.76,
        "humidity_pct": 56.99,
        "pressure_hpa": 1001.01,
        "voc_raw": 24170,
        "voc_index": 100,
    }
    record.update(overrides)
    return record


def make_writer(tmp_path) -> DailyCsvWriter:
    return DailyCsvWriter(tmp_path / "current", tmp_path / "spool", "pi5-node-01")


# --- daily CSV ---------------------------------------------------------------


def test_header_is_written_once(tmp_path):
    writer = make_writer(tmp_path)
    writer.write(reading(), day="2026-08-14")
    path = writer.write(reading(), day="2026-08-14")

    lines = path.read_text().strip().splitlines()
    assert lines[0] == ",".join(SENSOR_COLUMNS)
    assert len(lines) == 3  # header + two readings


def test_filename_carries_device_and_date(tmp_path):
    writer = make_writer(tmp_path)
    path = writer.write(reading(), day="2026-08-14")
    assert path.name == "sensors_pi5-node-01_2026-08-14.csv"


def test_date_change_moves_the_finished_day_to_the_spool(tmp_path):
    writer = make_writer(tmp_path)
    writer.write(reading(), day="2026-08-14")
    writer.write(reading(), day="2026-08-15")

    spooled = sorted((tmp_path / "spool" / "sensors").glob("*.csv"))
    assert [p.name for p in spooled] == ["sensors_pi5-node-01_2026-08-14.csv"]
    # The new day is still open, not spooled.
    assert (tmp_path / "current" / "sensors_pi5-node-01_2026-08-15.csv").is_file()


def test_errors_column_records_a_failed_sensor(tmp_path):
    writer = make_writer(tmp_path)
    path = writer.write(reading(sgp40_error="CRC mismatch"), day="2026-08-14")

    row = next(csv.DictReader(path.open(newline="")))
    assert "sgp40: CRC mismatch" in row["errors"]


def test_publish_today_copies_without_closing_the_day(tmp_path):
    """The 12-hourly schedule ships the day so far and keeps writing."""
    writer = make_writer(tmp_path)
    writer.write(reading(), day="2026-08-14")

    published = writer.publish_today()

    assert published is not None and published.is_file()
    # Original still open and still accepting rows.
    writer.write(reading(), day="2026-08-14")
    assert len(writer.path_for("2026-08-14").read_text().strip().splitlines()) == 3


def test_flush_closes_the_day_without_starting_another(tmp_path):
    writer = make_writer(tmp_path)
    writer.write(reading(), day="2026-08-14")

    finished = writer.flush()

    assert finished is not None and finished.parent.name == "sensors"
    assert list((tmp_path / "current").glob("*.csv")) == []


# --- master log --------------------------------------------------------------


def test_master_log_creates_a_row_per_day(tmp_path):
    master = MasterLog(tmp_path / "master_log.csv")
    master.update("2026-08-14", sensor_samples=10, audio_files=3)
    master.update("2026-08-15", sensor_samples=20)

    rows = master.read()
    assert sorted(rows) == ["2026-08-14", "2026-08-15"]
    assert rows["2026-08-14"]["audio_files"] == "3"


def test_counters_accumulate_with_the_plus_prefix(tmp_path):
    master = MasterLog(tmp_path / "master_log.csv")
    for _ in range(5):
        master.bump("2026-08-14", "sensor_samples")
    master.update("2026-08-14", audio_mib="+1.5")
    master.update("2026-08-14", audio_mib="+2.5")

    row = master.read()["2026-08-14"]
    assert row["sensor_samples"] == "5"
    assert row["audio_mib"] == "4"


def test_plain_values_replace_rather_than_add(tmp_path):
    master = MasterLog(tmp_path / "master_log.csv")
    master.update("2026-08-14", sample_rate_hz=48000)
    master.update("2026-08-14", sample_rate_hz=192000)

    assert master.read()["2026-08-14"]["sample_rate_hz"] == "192000"


def test_master_log_header_and_ordering(tmp_path):
    master = MasterLog(tmp_path / "master_log.csv")
    master.update("2026-08-15", sensor_samples=1)
    master.update("2026-08-13", sensor_samples=1)

    lines = (tmp_path / "master_log.csv").read_text().strip().splitlines()
    assert lines[0] == ",".join(MASTER_COLUMNS)
    assert lines[1].startswith("2026-08-13")  # sorted by date, not write order


def test_every_update_stamps_the_time(tmp_path):
    master = MasterLog(tmp_path / "master_log.csv")
    master.update("2026-08-14", sensor_samples=1)
    assert master.read()["2026-08-14"]["last_updated_utc"].endswith("Z")


def test_unknown_fields_are_ignored(tmp_path):
    master = MasterLog(tmp_path / "master_log.csv")
    master.update("2026-08-14", nonsense="x", sensor_samples=1)

    row = master.read()["2026-08-14"]
    assert "nonsense" not in row
    assert row["sensor_samples"] == "1"


def test_unreadable_master_log_does_not_raise(tmp_path):
    path = tmp_path / "master_log.csv"
    path.write_bytes(b"\x00\x01 not a csv")
    master = MasterLog(path)

    master.update("2026-08-14", sensor_samples=1)  # must not raise
    assert "2026-08-14" in master.read()
