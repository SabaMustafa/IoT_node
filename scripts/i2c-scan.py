#!/usr/bin/env python3
"""Scan the I2C buses for the sensors this node expects.

    scripts/i2c-scan.py            # scan every /dev/i2c-* bus
    scripts/i2c-scan.py --bus 1    # just one

Buses with no pull-up resistors (the Pi's HDMI DDC buses, typically i2c-13 and
i2c-14) answer at *every* address. The scan flags that so a floating bus is not
mistaken for a board full of sensors.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

EXPECTED = {
    0x76: "BME280 (or BMP280)",
    0x77: "BME280 alternate address",
    0x59: "SGP40",
}


def buses() -> list[int]:
    return sorted(int(p.name.removeprefix("i2c-")) for p in Path("/dev").glob("i2c-*"))


def scan(bus_num: int) -> list[int]:
    from smbus2 import SMBus, i2c_msg

    found: list[int] = []
    with SMBus(bus_num) as bus:
        for address in range(0x03, 0x78):
            try:
                bus.i2c_rdwr(i2c_msg.write(address, b""))
            except OSError:
                continue
            found.append(address)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bus", type=int, help="scan only this bus number")
    args = parser.parse_args()

    available = [args.bus] if args.bus is not None else buses()
    if not available:
        print("no /dev/i2c-* devices — run scripts/enable-i2c.sh, then reboot", file=sys.stderr)
        return 1

    hits = 0
    for bus_num in available:
        try:
            found = scan(bus_num)
        except OSError as exc:
            print(f"/dev/i2c-{bus_num}: cannot open ({exc})")
            continue

        if len(found) > 32:
            print(f"/dev/i2c-{bus_num}: {len(found)} addresses — floating bus, not real devices")
            continue

        if not found:
            print(f"/dev/i2c-{bus_num}: nothing responding")
            continue

        labels = [f"0x{a:02x} {EXPECTED.get(a, '')}".rstrip() for a in found]
        print(f"/dev/i2c-{bus_num}: {', '.join(labels)}")
        hits += sum(1 for a in found if a in EXPECTED)

    if not hits:
        print("\nnone of the expected sensor addresses (0x76/0x77 BME280, 0x59 SGP40) responded")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
