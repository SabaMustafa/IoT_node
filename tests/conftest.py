from __future__ import annotations

import struct

import pytest

from iot_node.config import Config


@pytest.fixture
def cfg(tmp_path) -> Config:
    """A Config rooted in a temp directory, so tests never touch the real spool."""
    config = Config(root=tmp_path)
    config.device.id = "test-node"
    config.ensure_dirs()
    return config


class FakeSMBus:
    """Minimal stand-in for smbus2.SMBus backed by a register map."""

    def __init__(self, registers: dict[int, int] | None = None) -> None:
        self.registers: dict[int, int] = dict(registers or {})
        self.writes: list[tuple[int, int]] = []
        self.closed = False

    def read_byte_data(self, _address: int, register: int) -> int:
        return self.registers.get(register, 0)

    def read_i2c_block_data(self, _address: int, register: int, length: int) -> list[int]:
        return [self.registers.get(register + i, 0) for i in range(length)]

    def write_byte_data(self, _address: int, register: int, value: int) -> None:
        self.writes.append((register, value))
        self.registers[register] = value

    def close(self) -> None:
        self.closed = True


def bme280_registers(
    *,
    adc_t: int,
    adc_p: int,
    adc_h: int = 30905,
    chip_id: int = 0x60,
) -> dict[int, int]:
    """Register map for a BME280 holding the datasheet's example calibration."""
    regs: dict[int, int] = {0xD0: chip_id, 0xF3: 0x00}

    calib = struct.pack(
        "<Hhh" + "Hhhhhhhhh",
        27504, 26435, -1000,  # T1..T3
        36477, -10685, 3024, 2855, 140, -7, 15500, -14600, 6000,  # P1..P9
    )
    for offset, byte in enumerate(calib):
        regs[0x88 + offset] = byte

    regs[0xA1] = 75  # H1
    h_block = struct.pack("<h", 364) + bytes((0, 339 >> 4, (339 & 0x0F), 0)) + struct.pack("<b", 30)
    for offset, byte in enumerate(h_block):
        regs[0xE1 + offset] = byte

    raw = bytes(
        (
            (adc_p >> 12) & 0xFF, (adc_p >> 4) & 0xFF, (adc_p << 4) & 0xF0,
            (adc_t >> 12) & 0xFF, (adc_t >> 4) & 0xFF, (adc_t << 4) & 0xF0,
            (adc_h >> 8) & 0xFF, adc_h & 0xFF,
        )
    )
    for offset, byte in enumerate(raw):
        regs[0xF7 + offset] = byte
    return regs
