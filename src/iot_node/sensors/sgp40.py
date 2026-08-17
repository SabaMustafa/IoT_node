"""Sensirion SGP40 — VOC sensor.

The SGP40 returns a raw resistance signal (SRAW), not an air-quality number.
Sensirion's gas index algorithm turns that into a 1-500 VOC Index by tracking a
slow baseline, so the index is only meaningful after the algorithm has seen a
few hours of data. We persist its internal state across restarts to avoid
re-learning that baseline every time the node reboots.

Raw readings are compensated with live humidity/temperature when the BME280 is
present — without it the datasheet defaults (50 %RH, 25 °C) are used.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .base import Reading, SensorError

if TYPE_CHECKING:
    from smbus2 import SMBus

log = logging.getLogger(__name__)

_CMD_MEASURE_RAW = (0x26, 0x0F)
_CMD_SELF_TEST = (0x28, 0x0E)
_CMD_HEATER_OFF = (0x36, 0x15)
_CMD_SERIAL = (0x36, 0x82)

_MEASURE_DELAY_S = 0.035  # datasheet: 30 ms max
_SELF_TEST_DELAY_S = 0.32  # datasheet: 320 ms
_SELF_TEST_PASS = 0xD4

# Conversions to burn off at open() while the hotplate settles.
_WARMUP_SAMPLES = 3

# A settled SGP40 reads roughly 20000-40000 in clean air. We only reject the
# extremes, which signal "not ready" rather than an unusual atmosphere.
_SRAW_MIN = 1
_SRAW_MAX = 65000

# Sampling interval the VOC algorithm was tuned for. Deviating from 1 s is
# supported, but the algorithm must be told so its time constants scale.
_DEFAULT_SAMPLING_INTERVAL_S = 1.0


class SGP40:
    name = "sgp40"

    def __init__(
        self,
        bus: int = 1,
        address: int = 0x59,
        *,
        sampling_interval_s: float = _DEFAULT_SAMPLING_INTERVAL_S,
        state_file: Path | None = None,
    ) -> None:
        self.bus_num = bus
        self.address = address
        self.sampling_interval_s = sampling_interval_s
        self.state_file = state_file
        self._bus: SMBus | None = None
        self._voc: Any | None = None
        self._serial: str | None = None

    def open(self) -> None:
        try:
            from smbus2 import SMBus, i2c_msg
        except ImportError as exc:  # pragma: no cover - packaging guard
            raise SensorError(f"smbus2 not installed: {exc}") from exc

        self._i2c_msg = i2c_msg
        try:
            self._bus = SMBus(self.bus_num)
            serial = self._command(_CMD_SERIAL, read_words=3, delay_s=0.001)
        except OSError as exc:
            raise SensorError(
                f"no SGP40 at /dev/i2c-{self.bus_num} address 0x{self.address:02x}: {exc}"
            ) from exc

        self._serial = "".join(f"{w:04x}" for w in serial)
        self._voc = self._make_algorithm()
        self._warm_up()
        log.info(
            "SGP40 ready on bus %d at 0x%02x (serial %s)",
            self.bus_num, self.address, self._serial,
        )

    def _warm_up(self, samples: int = _WARMUP_SAMPLES) -> None:
        """Burn off the first conversions, which the datasheet says to discard.

        These land at the extremes of the range (0 or near 65535) while the
        hotplate comes up to temperature. They never reach the index algorithm.
        """
        for _ in range(samples):
            try:
                sraw = self.measure_raw()
            except SensorError as exc:
                log.debug("discarded warm-up sample: %s", exc)
                continue
            if _plausible_sraw(sraw):
                return
            log.debug("discarded warm-up sample sraw=%d", sraw)

    def close(self) -> None:
        if self._bus is not None:
            try:
                self._write(_CMD_HEATER_OFF)
            except OSError:
                pass
            self._bus.close()
            self._bus = None
        self.save_state()

    def self_test(self) -> bool:
        """Run the on-chip self test. Only valid when the sensor is idle."""
        if self._bus is None:
            raise SensorError("SGP40 not opened")
        result = self._command(_CMD_SELF_TEST, read_words=1, delay_s=_SELF_TEST_DELAY_S)
        return (result[0] >> 8) == _SELF_TEST_PASS

    def measure_raw(self, humidity_pct: float = 50.0, temperature_c: float = 25.0) -> int:
        """One raw SRAW measurement, with humidity/temperature compensation."""
        if self._bus is None:
            raise SensorError("SGP40 not opened")

        rh_ticks = int(round(min(100.0, max(0.0, humidity_pct)) * 65535 / 100))
        t_ticks = int(round((min(130.0, max(-45.0, temperature_c)) + 45) * 65535 / 175))
        params = bytearray()
        for ticks in (rh_ticks, t_ticks):
            msb, lsb = (ticks >> 8) & 0xFF, ticks & 0xFF
            params += bytes((msb, lsb, _crc8((msb, lsb))))

        try:
            words = self._command(
                _CMD_MEASURE_RAW, read_words=1, delay_s=_MEASURE_DELAY_S, params=bytes(params)
            )
        except OSError as exc:
            raise SensorError(f"SGP40 read failed: {exc}") from exc
        return words[0]

    def read(self, humidity_pct: float = 50.0, temperature_c: float = 25.0) -> Reading:
        sraw = self.measure_raw(humidity_pct, temperature_c)
        values: dict[str, Any] = {"voc_raw": sraw}

        # The hotplate needs a moment to settle; readings taken before it has
        # produce nonsense at the extremes of the range (0, or close to 65535).
        # Passing one of those to the index algorithm corrupts the baseline it
        # spends hours learning, so drop the sample instead.
        if not _plausible_sraw(sraw):
            raise SensorError(f"implausible SGP40 raw signal {sraw} — sensor still warming up")

        if self._voc is not None:
            try:
                values["voc_index"] = int(self._voc.process(sraw))
            except Exception as exc:  # algorithm is C-backed; never let it kill the loop
                log.warning("VOC index algorithm failed: %s", exc)
        return Reading(sensor=self.name, values=values)

    # --- VOC algorithm state -------------------------------------------------

    def _make_algorithm(self) -> Any | None:
        try:
            from sensirion_gas_index_algorithm.voc_algorithm import VocAlgorithm
        except ImportError as exc:
            log.warning("VOC index unavailable (%s) — reporting raw signal only", exc)
            return None

        voc = VocAlgorithm(sampling_interval=self.sampling_interval_s)
        states = self._load_state()
        if states is not None:
            try:
                voc.set_states(*states)
                log.info("restored VOC algorithm baseline from %s", self.state_file)
            except Exception as exc:
                log.warning("could not restore VOC state: %s", exc)
        return voc

    def _load_state(self) -> tuple[float, ...] | None:
        if self.state_file is None or not self.state_file.is_file():
            return None
        try:
            data = json.loads(self.state_file.read_text())
        except (OSError, ValueError) as exc:
            log.warning("unreadable VOC state file %s: %s", self.state_file, exc)
            return None
        # The baseline drifts; a stale one is worse than starting clean.
        age_s = time.time() - float(data.get("saved_at", 0))
        if age_s > 7 * 24 * 3600:
            log.info("VOC state is %.1f days old — starting fresh", age_s / 86400)
            return None
        states = data.get("states")
        return tuple(float(s) for s in states) if states else None

    def save_state(self) -> None:
        """Persist the algorithm baseline so a restart doesn't reset it."""
        if self._voc is None or self.state_file is None:
            return
        try:
            states = self._voc.get_states()
        except Exception as exc:
            log.warning("could not read VOC state: %s", exc)
            return
        if not isinstance(states, (list, tuple)):
            states = (states,)
        payload = {"saved_at": time.time(), "serial": self._serial, "states": list(states)}
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload))
            tmp.replace(self.state_file)
        except OSError as exc:
            log.warning("could not save VOC state: %s", exc)

    # --- raw I2C -------------------------------------------------------------

    def _write(self, cmd: tuple[int, int], params: bytes = b"") -> None:
        assert self._bus is not None
        self._bus.i2c_rdwr(self._i2c_msg.write(self.address, bytes(cmd) + params))

    def _command(
        self, cmd: tuple[int, int], *, read_words: int, delay_s: float, params: bytes = b""
    ) -> list[int]:
        """Write a command, wait out the conversion, read back CRC'd words.

        The SGP40 needs a stop condition and a delay between write and read, so
        this cannot use SMBus block transactions.
        """
        assert self._bus is not None
        self._write(cmd, params)
        time.sleep(delay_s)

        read = self._i2c_msg.read(self.address, read_words * 3)
        self._bus.i2c_rdwr(read)
        raw = bytes(read)

        words: list[int] = []
        for i in range(0, len(raw), 3):
            msb, lsb, crc = raw[i], raw[i + 1], raw[i + 2]
            if _crc8((msb, lsb)) != crc:
                raise SensorError(f"SGP40 CRC mismatch (got 0x{crc:02x})")
            words.append((msb << 8) | lsb)
        return words


def _plausible_sraw(sraw: int) -> bool:
    """Reject the out-of-band values a not-yet-settled hotplate produces."""
    return _SRAW_MIN <= sraw <= _SRAW_MAX


def _crc8(data: bytes | tuple[int, ...]) -> int:
    """CRC-8, polynomial 0x31, init 0xFF — per the SGP40 datasheet."""
    crc = 0xFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x31) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc
