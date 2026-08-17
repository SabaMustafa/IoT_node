"""Bosch BME280 — temperature, relative humidity, barometric pressure.

Compensation follows the floating-point reference in the BME280 datasheet
(rev 1.6, section 4.2.3). We drive the sensor in forced mode: one conversion
per sample, sleep in between, which is what the datasheet recommends for slow
periodic sampling and keeps self-heating out of the temperature reading.
"""

from __future__ import annotations

import logging
import struct
import time
from typing import TYPE_CHECKING

from .base import Reading, SensorError

if TYPE_CHECKING:
    from smbus2 import SMBus

log = logging.getLogger(__name__)

_REG_ID = 0xD0
_REG_RESET = 0xE0
_REG_CTRL_HUM = 0xF2
_REG_STATUS = 0xF3
_REG_CTRL_MEAS = 0xF4
_REG_CONFIG = 0xF5
_REG_DATA = 0xF7

_CHIP_ID_BME280 = 0x60
_CHIP_ID_BMP280 = 0x58  # same part without the humidity channel

_OSRS_X1 = 0b001
_MODE_SLEEP = 0b00
_MODE_FORCED = 0b01


class BME280:
    name = "bme280"

    def __init__(self, bus: int = 1, address: int = 0x76) -> None:
        self.bus_num = bus
        self.address = address
        self._bus: SMBus | None = None
        self._cal: dict[str, float] = {}
        self._has_humidity = True

    def open(self) -> None:
        try:
            from smbus2 import SMBus
        except ImportError as exc:  # pragma: no cover - packaging guard
            raise SensorError(f"smbus2 not installed: {exc}") from exc

        try:
            self._bus = SMBus(self.bus_num)
            chip_id = self._bus.read_byte_data(self.address, _REG_ID)
        except OSError as exc:
            raise SensorError(
                f"no BME280 at /dev/i2c-{self.bus_num} address 0x{self.address:02x}: {exc}"
            ) from exc

        if chip_id == _CHIP_ID_BMP280:
            self._has_humidity = False
            log.warning("chip id 0x%02x is a BMP280 — no humidity channel", chip_id)
        elif chip_id != _CHIP_ID_BME280:
            raise SensorError(
                f"unexpected chip id 0x{chip_id:02x} at 0x{self.address:02x} "
                f"(expected 0x{_CHIP_ID_BME280:02x})"
            )

        self._bus.write_byte_data(self.address, _REG_RESET, 0xB6)
        time.sleep(0.005)
        self._read_calibration()
        # Filter off, standby irrelevant in forced mode.
        self._bus.write_byte_data(self.address, _REG_CONFIG, 0x00)
        log.info("BME280 ready on bus %d at 0x%02x", self.bus_num, self.address)

    def close(self) -> None:
        if self._bus is not None:
            try:
                self._bus.write_byte_data(self.address, _REG_CTRL_MEAS, _MODE_SLEEP)
            except OSError:
                pass
            self._bus.close()
            self._bus = None

    def _read_calibration(self) -> None:
        assert self._bus is not None
        block1 = bytes(self._bus.read_i2c_block_data(self.address, 0x88, 24))
        h1 = self._bus.read_byte_data(self.address, 0xA1)

        t1, t2, t3 = struct.unpack("<Hhh", block1[0:6])
        p1, p2, p3, p4, p5, p6, p7, p8, p9 = struct.unpack("<Hhhhhhhhh", block1[6:24])
        cal: dict[str, float] = {
            "T1": t1, "T2": t2, "T3": t3,
            "P1": p1, "P2": p2, "P3": p3, "P4": p4, "P5": p5,
            "P6": p6, "P7": p7, "P8": p8, "P9": p9,
        }

        if self._has_humidity:
            block2 = bytes(self._bus.read_i2c_block_data(self.address, 0xE1, 7))
            h2 = struct.unpack("<h", block2[0:2])[0]
            h3 = block2[2]
            e4, e5, e6 = block2[3], block2[4], block2[5]
            h6 = struct.unpack("<b", block2[6:7])[0]
            # H4 and H5 are signed 12-bit values sharing the nibbles of 0xE5.
            h4 = _sign12((e4 << 4) | (e5 & 0x0F))
            h5 = _sign12((e6 << 4) | (e5 >> 4))
            cal.update({"H1": h1, "H2": h2, "H3": h3, "H4": h4, "H5": h5, "H6": h6})

        self._cal = cal

    def read(self) -> Reading:
        if self._bus is None:
            raise SensorError("BME280 not opened")

        hum_os = _OSRS_X1 if self._has_humidity else 0
        try:
            # ctrl_hum only takes effect on the next ctrl_meas write.
            self._bus.write_byte_data(self.address, _REG_CTRL_HUM, hum_os)
            self._bus.write_byte_data(
                self.address,
                _REG_CTRL_MEAS,
                (_OSRS_X1 << 5) | (_OSRS_X1 << 2) | _MODE_FORCED,
            )
            self._wait_for_conversion()
            raw = bytes(self._bus.read_i2c_block_data(self.address, _REG_DATA, 8))
        except OSError as exc:
            raise SensorError(f"BME280 read failed: {exc}") from exc

        adc_p = (raw[0] << 12) | (raw[1] << 4) | (raw[2] >> 4)
        adc_t = (raw[3] << 12) | (raw[4] << 4) | (raw[5] >> 4)
        adc_h = (raw[6] << 8) | raw[7]

        temperature, t_fine = self._compensate_temperature(adc_t)
        values: dict[str, float] = {
            "temperature_c": round(temperature, 2),
            "pressure_hpa": round(self._compensate_pressure(adc_p, t_fine) / 100.0, 2),
        }
        if self._has_humidity:
            values["humidity_pct"] = round(self._compensate_humidity(adc_h, t_fine), 2)
        return Reading(sensor=self.name, values=values)

    def _wait_for_conversion(self, timeout_s: float = 0.5) -> None:
        assert self._bus is not None
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            # bit 3 = measuring, bit 0 = im_update
            if not self._bus.read_byte_data(self.address, _REG_STATUS) & 0x09:
                return
            time.sleep(0.002)
        raise SensorError("BME280 conversion timed out")

    def _compensate_temperature(self, adc_t: int) -> tuple[float, float]:
        c = self._cal
        var1 = (adc_t / 16384.0 - c["T1"] / 1024.0) * c["T2"]
        var2 = (adc_t / 131072.0 - c["T1"] / 8192.0) ** 2 * c["T3"]
        t_fine = var1 + var2
        return t_fine / 5120.0, t_fine

    def _compensate_pressure(self, adc_p: int, t_fine: float) -> float:
        c = self._cal
        var1 = t_fine / 2.0 - 64000.0
        var2 = var1 * var1 * c["P6"] / 32768.0
        var2 = var2 + var1 * c["P5"] * 2.0
        var2 = var2 / 4.0 + c["P4"] * 65536.0
        var1 = (c["P3"] * var1 * var1 / 524288.0 + c["P2"] * var1) / 524288.0
        var1 = (1.0 + var1 / 32768.0) * c["P1"]
        if var1 == 0.0:
            raise SensorError("BME280 pressure calibration divide-by-zero")
        p = 1048576.0 - adc_p
        p = (p - var2 / 4096.0) * 6250.0 / var1
        var1 = c["P9"] * p * p / 2147483648.0
        var2 = p * c["P8"] / 32768.0
        return p + (var1 + var2 + c["P7"]) / 16.0

    def _compensate_humidity(self, adc_h: int, t_fine: float) -> float:
        c = self._cal
        h = t_fine - 76800.0
        h = (adc_h - (c["H4"] * 64.0 + c["H5"] / 16384.0 * h)) * (
            c["H2"] / 65536.0 * (1.0 + c["H6"] / 67108864.0 * h * (1.0 + c["H3"] / 67108864.0 * h))
        )
        h = h * (1.0 - c["H1"] * h / 524288.0)
        return min(100.0, max(0.0, h))


def _sign12(value: int) -> int:
    """Interpret a 12-bit two's-complement value."""
    return value - 4096 if value & 0x800 else value
