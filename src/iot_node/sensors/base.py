"""Shared sensor interface.

Drivers are deliberately thin wrappers over smbus2: open the bus lazily, read
raw registers, convert with the vendor's compensation maths, and raise
SensorError for anything the caller should log-and-continue on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class SensorError(RuntimeError):
    """A sensor could not be read. Never fatal — the loop skips this sample."""


@dataclass
class Reading:
    """One sensor's contribution to a sample."""

    sensor: str
    values: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Sensor(Protocol):
    name: str

    def open(self) -> None:
        """Probe the device and load calibration. Raises SensorError."""

    def read(self) -> Reading:
        """Take one measurement. Raises SensorError."""

    def close(self) -> None: ...
