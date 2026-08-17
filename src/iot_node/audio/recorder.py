"""Records from the AudioMoth (or any USB capture device) with arecord.

Deliberately captures through ``hw:`` rather than ``plughw:``. plughw would
silently resample whatever the device actually supports up or down to the rate
we asked for, which is fine for voice and wrong for bioacoustics — a 192 kHz
file resampled from a 48 kHz capture looks ultrasonic and contains nothing above
24 kHz. If a requested rate is not natively supported we say so and fall back to
the nearest rate the hardware genuinely offers.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..remote_config import SUPPORTED_SAMPLE_RATES

log = logging.getLogger(__name__)

# arecord -l lines look like:
#   card 3: AudioMoth [AudioMoth USB Microphone], device 0: USB Audio [USB Audio]
_CARD_LINE = re.compile(
    r"^card (?P<card>\d+): (?P<id>\S+) \[(?P<name>[^\]]+)\], device (?P<device>\d+):"
)

_AUDIOMOTH_HINTS = ("audiomoth", "usb microphone")

# Formats to try, best first. The AudioMoth USB Microphone is 16-bit mono.
_FORMATS = ("S16_LE", "S24_3LE", "S32_LE")

# Channel counts to try, in order of preference. The AudioMoth is mono, but
# plenty of USB capture hardware refuses mono outright ("Channels count non
# available"), so we negotiate rather than assume. Analysis mixes to mono
# anyway, so recording stereo costs disk space and nothing else.
_CHANNEL_CHOICES = (1, 2)


class RecorderError(RuntimeError):
    """Recording failed. Logged and retried; never fatal to the node."""


@dataclass(frozen=True)
class CaptureDevice:
    card: int
    device: int
    card_id: str
    name: str

    @property
    def hw(self) -> str:
        """ALSA device string for native, non-resampling capture."""
        return f"hw:{self.card},{self.device}"

    @property
    def is_audiomoth(self) -> bool:
        haystack = f"{self.card_id} {self.name}".lower()
        return any(hint in haystack for hint in _AUDIOMOTH_HINTS)

    def __str__(self) -> str:
        return f"{self.name} ({self.hw})"


def list_capture_devices(arecord: str = "arecord") -> list[CaptureDevice]:
    """Every ALSA capture device the system currently sees."""
    binary = shutil.which(arecord)
    if not binary:
        raise RecorderError("arecord not found — install alsa-utils")
    try:
        result = subprocess.run(
            [binary, "-l"], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RecorderError(f"could not list capture devices: {exc}") from exc

    devices: list[CaptureDevice] = []
    for line in result.stdout.splitlines():
        match = _CARD_LINE.match(line.strip())
        if match:
            devices.append(
                CaptureDevice(
                    card=int(match["card"]),
                    device=int(match["device"]),
                    card_id=match["id"],
                    name=match["name"],
                )
            )
    return devices


def find_audiomoth(arecord: str = "arecord") -> CaptureDevice | None:
    """Pick the AudioMoth if it is plugged in, else nothing.

    We do not silently fall back to another capture device: recording the Pi's
    webcam microphone for a week and calling it hive data would be worse than
    recording nothing.
    """
    for device in list_capture_devices(arecord):
        if device.is_audiomoth:
            return device
    return None


# An AudioMoth presents two different USB identities depending on the switch,
# and both were observed on real hardware:
#
#   10c4:0002  Silicon Labs USBXpress, interface class 03 (HID)
#              — the USB/OFF position: configuration mode, talks to the
#                AudioMoth app, and is NOT a recording device
#   16d0:06f3  MCS "384kHz AudioMoth USB Microphone", interface class 01
#              — the DEFAULT/CUSTOM position: an actual USB audio device
#
# Matching on the vendor id alone would miss the microphone mode entirely, so
# the product string is the reliable signal and the ids are a fallback for a
# device whose strings cannot be read.
_AUDIOMOTH_USB_VENDORS = ("10c4", "16d0")
_AUDIOMOTH_PRODUCT_HINT = "audiomoth"


def audiomoth_usb_present() -> bool | None:
    """Is an AudioMoth on the USB bus at all, whatever mode it is in?

    This separates the two failure modes that look identical from ALSA's point
    of view: nothing plugged in, versus plugged in but not presenting itself as
    a microphone (configuration switch position, or stock firmware).
    Returns None if the question cannot be answered on this system.
    """
    root = Path("/sys/bus/usb/devices")
    if not root.is_dir():
        return None
    found = False
    try:
        for entry in root.iterdir():
            product = entry / "product"
            if product.is_file():
                try:
                    if _AUDIOMOTH_PRODUCT_HINT in product.read_text().strip().lower():
                        return True
                except OSError:
                    pass
            vendor_file = entry / "idVendor"
            if not vendor_file.is_file():
                continue
            try:
                if vendor_file.read_text().strip().lower() in _AUDIOMOTH_USB_VENDORS:
                    found = True
            except OSError:
                continue
    except OSError:
        return None
    return found


def diagnose_missing_audiomoth(arecord: str = "arecord") -> str:
    """Explain *why* there is no AudioMoth to record from."""
    try:
        devices = list_capture_devices(arecord)
    except RecorderError as exc:
        return str(exc)

    on_bus = audiomoth_usb_present()
    others = ", ".join(d.name for d in devices) or "none"

    if on_bus is False:
        return (
            "no AudioMoth is plugged in — nothing from its chip family is on the USB "
            f"bus at all. Capture devices present: {others}. Check the cable is a data "
            "cable, not charge-only."
        )
    if on_bus is True:
        return (
            "an AudioMoth is plugged in but is offering a configuration interface "
            "rather than audio (USB interface class 03/HID, not 01/Audio). Move the "
            "switch to the other position — DEFAULT or CUSTOM makes it a microphone, "
            "USB/OFF is the mode the configuration app uses — and replug. If neither "
            "position gives an audio device, it needs AudioMoth-USB-Microphone "
            "firmware."
        )
    return f"no AudioMoth capture device found. Capture devices present: {others}."


def select_device(preference: str = "auto", arecord: str = "arecord") -> CaptureDevice | None:
    """Resolve the ``input_device`` setting to a capture device.

    ``auto`` accepts only a genuine AudioMoth. ``any`` takes the first capture
    device available, which is how the system can be exercised on a bench with
    no AudioMoth attached — the device name is recorded alongside every
    analysis row so such audio is never mistaken for the real thing. Anything
    else is treated as an explicit ALSA name like ``hw:3,0``.
    """
    preference = (preference or "auto").strip()
    devices = list_capture_devices(arecord)

    if preference.lower() in ("auto", "audiomoth", ""):
        return next((d for d in devices if d.is_audiomoth), None)

    if preference.lower() in ("any", "first"):
        # Still prefer a real AudioMoth when one is present.
        return next((d for d in devices if d.is_audiomoth), devices[0] if devices else None)

    match = re.match(r"(?:plug)?hw:(\d+)(?:,(\d+))?$", preference)
    if match:
        card, sub = int(match.group(1)), int(match.group(2) or 0)
        for device in devices:
            if device.card == card and device.device == sub:
                return device
        log.warning("configured input_device %r is not present", preference)
        return None

    # A name fragment, e.g. "CAMERA" or "AudioMoth".
    needle = preference.lower()
    return next(
        (d for d in devices if needle in f"{d.card_id} {d.name}".lower()), None
    )


def _try_capture(
    binary: str, device: CaptureDevice, *, fmt: str, channels: int, rate: int, seconds: float
) -> str | None:
    """One short trial capture. Returns None on success, else the error line."""
    try:
        proc = subprocess.run(
            [
                binary, "-D", device.hw, "-f", fmt, "-c", str(channels),
                "-r", str(rate), "-d", str(max(1, int(seconds + 0.5))),
                "-t", "raw", "/dev/null",
            ],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    if proc.returncode == 0:
        return None
    lines = (proc.stderr or "").strip().splitlines()
    return lines[-1] if lines else f"exit code {proc.returncode}"


def probe_channels(
    device: CaptureDevice, *, rate: int = 48000, arecord: str = "arecord"
) -> int | None:
    """Find a channel count the device will actually accept."""
    binary = shutil.which(arecord)
    if not binary:
        raise RecorderError("arecord not found — install alsa-utils")
    for channels in _CHANNEL_CHOICES:
        if _try_capture(
            binary, device, fmt="S16_LE", channels=channels, rate=rate, seconds=0.4
        ) is None:
            return channels
    return None


def probe_sample_rates(
    device: CaptureDevice,
    rates: tuple[int, ...] = SUPPORTED_SAMPLE_RATES,
    *,
    arecord: str = "arecord",
    seconds: float = 0.4,
    channels: int | None = None,
) -> dict[int, str | None]:
    """Try each rate natively. Maps rate -> None if it works, else the error."""
    binary = shutil.which(arecord)
    if not binary:
        raise RecorderError("arecord not found — install alsa-utils")

    # Some devices refuse mono outright, which would make every rate look
    # unsupported for a reason that has nothing to do with the rate.
    candidates = (channels,) if channels else _CHANNEL_CHOICES

    results: dict[int, str | None] = {}
    for rate in rates:
        error: str | None = "no supported sample format"
        for count in candidates:
            for fmt in _FORMATS:
                error = _try_capture(
                    binary, device, fmt=fmt, channels=count, rate=rate, seconds=seconds
                )
                if error is None:
                    break
            if error is None:
                break
        results[rate] = error
    return results


class Recorder:
    """Captures fixed-length WAV files into a directory."""

    def __init__(
        self,
        output_dir: Path,
        *,
        device: CaptureDevice | None = None,
        arecord: str = "arecord",
        device_id: str = "node",
        preference: str = "auto",
    ) -> None:
        self.output_dir = output_dir
        self.device = device
        self.arecord = arecord
        self.device_id = device_id
        self.preference = preference
        self._supported: dict[int, str | None] | None = None
        self._good_rates: set[int] = set()
        self._bad_rates: set[int] = set()
        self._channels: int | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._warned_substitute = False
        # The .part file currently being written, so a live meter can read
        # its tail — the capture device itself can only be opened once.
        self.active_partial: Path | None = None
        self.active_rate = 0
        self.active_channels = 1

    def set_preference(self, preference: str) -> None:
        """Change the configured input device, re-detecting if it differs."""
        if preference != self.preference:
            self.preference = preference
            self.device = None
            self._supported = None
            self._good_rates.clear()
            self._bad_rates.clear()
            self._channels = None
            self._warned_substitute = False

    def usable_channels(self, rate: int = 48000) -> int:
        """A channel count this device accepts. Probed once, then cached."""
        if self._channels is not None:
            return self._channels
        device = self.detect()
        if device is None:
            return 1
        found = probe_channels(device, rate=rate, arecord=self.arecord)
        if found is None:
            log.warning("%s accepted neither mono nor stereo — trying mono anyway", device.name)
            found = 1
        elif found != 1:
            log.info("%s does not support mono; recording %d channels", device.name, found)
        self._channels = found
        return found

    def detect(self) -> CaptureDevice | None:
        if self.device is None:
            self.device = select_device(self.preference, self.arecord)
            if self.device is not None:
                log.info("audio input: %s", self.device)
                if not self.device.is_audiomoth and not self._warned_substitute:
                    self._warned_substitute = True
                    log.warning(
                        "recording from %s, which is NOT an AudioMoth — this audio is a "
                        "bench substitute and is tagged as such in the analysis output",
                        self.device.name,
                    )
        return self.device

    def usable_rate(self, requested: int) -> int:
        """The nearest rate the hardware genuinely supports, without resampling.

        Checks the requested rate on its own first. Probing all eight rates
        costs a second each and would be paid before every single recording;
        the overwhelmingly common case is that the configured rate simply
        works, so that case is one short trial capture.
        """
        device = self.detect()
        if device is None:
            return requested
        if requested in self._good_rates:
            return requested

        if requested not in self._bad_rates:
            binary = shutil.which(self.arecord)
            if binary is None:
                return requested
            error = _try_capture(
                binary, device, fmt="S16_LE",
                channels=self.usable_channels(), rate=requested, seconds=0.4,
            )
            if error is None:
                self._good_rates.add(requested)
                return requested
            self._bad_rates.add(requested)
            log.warning("%d Hz refused by %s: %s", requested, device.name, error)

        # The configured rate genuinely does not work, so find the nearest that
        # does rather than silently resampling.
        if self._supported is None:
            self._supported = probe_sample_rates(
                device, arecord=self.arecord, channels=self.usable_channels()
            )
            working = [r for r, err in self._supported.items() if err is None]
            self._good_rates.update(working)
            log.info(
                "%s supports: %s",
                device.name,
                ", ".join(f"{r / 1000:g}k" for r in working) or "no tested rate",
            )
        working = [r for r, err in self._supported.items() if err is None]
        if not working:
            return requested
        nearest = min(working, key=lambda r: abs(r - requested))
        log.warning(
            "%d Hz is not supported natively by %s — recording at %d Hz instead "
            "(resampling would fake bandwidth that was never captured)",
            requested, device.name, nearest,
        )
        return nearest

    def record(self, *, sample_rate: int, seconds: float, channels: int | None = None) -> Path:
        """Record one file. Blocks for the duration, then returns its path."""
        device = self.detect()
        if device is None:
            raise RecorderError("no AudioMoth capture device found — is it in USB/OFF mode?")

        binary = shutil.which(self.arecord)
        if not binary:
            raise RecorderError("arecord not found — install alsa-utils")

        rate = self.usable_rate(sample_rate)
        channels = channels if channels is not None else self.usable_channels(rate)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        started = datetime.now()
        # Same naming the AudioMoth itself uses, so both sources sort together
        # and the uploader's date parsing works unchanged.
        final = self.output_dir / f"{started:%Y%m%d_%H%M%S}.WAV"
        partial = final.with_suffix(".WAV.part")

        cmd = [
            binary, "-D", device.hw, "-f", "S16_LE", "-c", str(channels),
            "-r", str(rate), "-d", str(int(seconds)), "-t", "wav", str(partial),
        ]
        log.info(
            "recording %.0fs at %d Hz, %d ch, from %s", seconds, rate, channels, device.name
        )
        self.active_partial, self.active_rate, self.active_channels = partial, rate, channels
        try:
            self._process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            _, stderr = self._process.communicate(timeout=seconds + 60)
            returncode = self._process.returncode
        except subprocess.TimeoutExpired:
            self.stop()
            raise RecorderError(f"arecord overran its {seconds:.0f}s recording") from None
        except OSError as exc:
            raise RecorderError(f"could not start arecord: {exc}") from exc
        finally:
            self._process = None
            self.active_partial = None

        if returncode != 0:
            partial.unlink(missing_ok=True)
            detail = (stderr or b"").decode(errors="replace").strip().splitlines()
            raise RecorderError(f"arecord failed: {detail[-1] if detail else returncode}")

        if not partial.is_file() or partial.stat().st_size < 1024:
            partial.unlink(missing_ok=True)
            raise RecorderError("arecord produced no audio")

        os.replace(partial, final)
        log.info("recorded %s (%.1f MiB)", final.name, final.stat().st_size / 1048576)
        return final

    def stop(self) -> None:
        """Interrupt a recording in progress (used on shutdown)."""
        process = self._process
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

    def cleanup_partials(self) -> None:
        """Remove any .part files left behind by an interrupted recording."""
        if not self.output_dir.is_dir():
            return
        for stale in self.output_dir.glob("*.part"):
            try:
                stale.unlink()
                log.info("removed interrupted recording %s", stale.name)
            except OSError:
                pass


def wav_duration_seconds(path: Path) -> float | None:
    """Duration from the WAV header, for the master log. None if unreadable."""
    import wave

    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate()
            return handle.getnframes() / rate if rate else None
    except (OSError, EOFError, wave.Error):
        return None


def wait_until(deadline: float, stop_check: object | None = None) -> None:  # pragma: no cover
    """Sleep until a monotonic deadline (kept for callers that need it)."""
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)
