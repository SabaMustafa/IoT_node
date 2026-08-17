from __future__ import annotations

import pytest

from iot_node.sensors.bme280 import BME280, _sign12
from iot_node.sensors.sgp40 import _crc8
from tests.conftest import FakeSMBus, bme280_registers

# --- BME280 ------------------------------------------------------------------


def open_fake_bme280(registers) -> BME280:
    sensor = BME280(bus=1, address=0x76)
    sensor._bus = FakeSMBus(registers)
    sensor._read_calibration()
    return sensor


def test_bme280_matches_datasheet_reference_values():
    """The compensation examples from the BME280 datasheet (rev 1.6)."""
    sensor = open_fake_bme280(bme280_registers(adc_t=519888, adc_p=415148))
    reading = sensor.read()

    assert reading.sensor == "bme280"
    assert reading.values["temperature_c"] == pytest.approx(25.08, abs=0.01)
    assert reading.values["pressure_hpa"] == pytest.approx(1006.53, abs=0.05)
    assert 0.0 <= reading.values["humidity_pct"] <= 100.0


def test_bme280_temperature_tracks_the_raw_reading():
    warmer = open_fake_bme280(bme280_registers(adc_t=530000, adc_p=415148)).read()
    cooler = open_fake_bme280(bme280_registers(adc_t=510000, adc_p=415148)).read()
    assert warmer.values["temperature_c"] > cooler.values["temperature_c"]


def test_bme280_rejects_a_foreign_chip_id(monkeypatch):
    """Reading calibration off the wrong chip would silently produce garbage."""
    import smbus2

    from iot_node.sensors.base import SensorError

    registers = bme280_registers(adc_t=519888, adc_p=415148, chip_id=0x42)
    monkeypatch.setattr(smbus2, "SMBus", lambda _bus: FakeSMBus(registers))

    with pytest.raises(SensorError, match="unexpected chip id"):
        BME280().open()


def test_bme280_accepts_a_bmp280_without_humidity(monkeypatch):
    import smbus2

    registers = bme280_registers(adc_t=519888, adc_p=415148, chip_id=0x58)
    monkeypatch.setattr(smbus2, "SMBus", lambda _bus: FakeSMBus(registers))

    sensor = BME280()
    sensor.open()
    values = sensor.read().values
    assert "humidity_pct" not in values
    assert values["temperature_c"] == pytest.approx(25.08, abs=0.01)


def test_sign12_handles_the_negative_half():
    assert _sign12(0x000) == 0
    assert _sign12(0x7FF) == 2047
    assert _sign12(0x800) == -2048
    assert _sign12(0xFFF) == -1


def test_bme280_read_before_open_is_an_error():
    from iot_node.sensors.base import SensorError

    with pytest.raises(SensorError, match="not opened"):
        BME280().read()


# --- SGP40 -------------------------------------------------------------------


def test_crc8_matches_the_sensirion_example():
    # The SGP40 datasheet gives CRC(0xBEEF) = 0x92.
    assert _crc8((0xBE, 0xEF)) == 0x92


def test_crc8_is_byte_order_sensitive():
    assert _crc8((0xEF, 0xBE)) != _crc8((0xBE, 0xEF))


def test_sraw_plausibility_bounds():
    from iot_node.sensors.sgp40 import _plausible_sraw

    # Values a settled sensor actually produces.
    assert _plausible_sraw(23742)
    assert _plausible_sraw(27332)
    # The two artefacts seen on real hardware before the hotplate settles.
    assert not _plausible_sraw(0)
    assert not _plausible_sraw(65477)
    assert not _plausible_sraw(65535)


def test_warmup_samples_never_reach_the_index_algorithm():
    """A not-yet-settled reading must not poison the learned VOC baseline."""
    from iot_node.sensors.base import SensorError
    from iot_node.sensors.sgp40 import SGP40

    processed: list[int] = []

    class StubVoc:
        def process(self, sraw: int) -> int:
            processed.append(sraw)
            return 42

    sensor = SGP40()
    sensor._bus = object()  # only needs to be non-None
    sensor._voc = StubVoc()
    sensor.measure_raw = lambda *a, **k: 65477  # type: ignore[method-assign]

    with pytest.raises(SensorError, match="warming up"):
        sensor.read()
    assert processed == []

    sensor.measure_raw = lambda *a, **k: 27332  # type: ignore[method-assign]
    assert sensor.read().values["voc_index"] == 42
    assert processed == [27332]


def test_warmup_stops_once_a_good_sample_arrives():
    from iot_node.sensors.sgp40 import SGP40

    samples = iter([0, 65477, 26000, 26100])
    calls: list[int] = []

    sensor = SGP40()
    sensor._bus = object()

    def fake_measure(*_a, **_k):
        value = next(samples)
        calls.append(value)
        return value

    sensor.measure_raw = fake_measure  # type: ignore[method-assign]
    sensor._warm_up()

    # Two bad readings discarded, then it stops at the first usable one.
    assert calls == [0, 65477, 26000]


def test_humidity_and_temperature_ticks_match_the_defaults():
    """50 %RH and 25 °C must encode to the datasheet's default parameters."""
    rh_ticks = round(50.0 * 65535 / 100)
    t_ticks = round((25.0 + 45) * 65535 / 175)
    assert rh_ticks == 0x8000
    assert t_ticks == 0x6666
