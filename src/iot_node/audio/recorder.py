"""Records from the AudioMoth (or any USB capture device) with arecord.

Deliberately captures through ``hw:`` rather than ``plughw:``. plughw would
silently resample whatever the device actually supports up or down to the rate
we asked for, which is fine for voice and wrong for bioacoustics — a 192 kHz
file resampled from a 48 kHz capture looks ultrasonic and contains nothing above
24 kHz.

Capturing through ``hw:`` does not by itself get us the rate we asked for.
arecord opens the device at its own nearest rate, prints ``Warning: rate is not
accurate (requested = 48000Hz, got = 384000Hz)``, exits 0, and writes a file
whose header says 384000. Trusting the exit code therefore yields files eight
times the intended size while every log line claims the configured rate. So we
read back what ALSA actually opened, and when the hardware cannot give us the
configured rate we capture at its native rate and reduce it ourselves as it
arrives (see ``resample``), so the oversized audio never touches the card.

Downsampling is not the thing plughw was rejected for. Discarding bandwidth we
genuinely captured is honest and anti-alias filtered; inventing bandwidth we
never captured is not.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

from ..remote_config import SUPPORTED_SAMPLE_RATES
from .resample import Decimator

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


# Whatever we ask for, arecord reports what it actually opened:
#   Recording raw data '/dev/null' : Signed 16 bit Little Endian, Rate 384000 Hz
# and, when that is not what we asked for, says so first:
#   Warning: rate is not accurate (requested = 48000Hz, got = 384000Hz)
_RATE_SUBSTITUTED = re.compile(r"got\s*=\s*(\d+)\s*Hz")
_RATE_OPENED = re.compile(r"Rate (\d+) Hz")

# A device someone else already has open fails identically to one that cannot
# do the rate. Recording "busy" as "unsupported" would blacklist a perfectly
# good rate for the life of the process — which is exactly how a live recording
# taught this node that its microphone could not do 48 kHz.
_BUSY_MARKERS = ("resource busy", "device busy")


@dataclass(frozen=True)
class CaptureProbe:
    """What one trial capture revealed about the hardware."""

    error: str | None = None
    delivered_rate: int | None = None
    busy: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None


def _capture_probe(
    binary: str, device: CaptureDevice, *, fmt: str, channels: int, rate: int, seconds: float
) -> CaptureProbe:
    """One short trial capture, reporting the rate the hardware really gave."""
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
        return CaptureProbe(str(exc))

    stderr = proc.stderr or ""
    found = _RATE_SUBSTITUTED.search(stderr) or _RATE_OPENED.search(stderr)
    delivered = int(found.group(1)) if found else None

    if proc.returncode == 0:
        return CaptureProbe(delivered_rate=delivered)
    lines = stderr.strip().splitlines()
    lowered = stderr.lower()
    return CaptureProbe(
        error=lines[-1] if lines else f"exit code {proc.returncode}",
        delivered_rate=delivered,
        busy=any(marker in lowered for marker in _BUSY_MARKERS),
    )


def _try_capture(
    binary: str, device: CaptureDevice, *, fmt: str, channels: int, rate: int, seconds: float
) -> str | None:
    """One short trial capture. Returns None on success, else the error line."""
    return _capture_probe(
        binary, device, fmt=fmt, channels=channels, rate=rate, seconds=seconds
    ).error


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
        results[rate] = _probe_one_rate(
            binary, device, rate=rate, candidates=candidates, seconds=seconds
        )
    return results


def _probe_one_rate(
    binary: str,
    device: CaptureDevice,
    *,
    rate: int,
    candidates: tuple[int, ...],
    seconds: float,
) -> str | None:
    """Is this rate genuinely native? None if so, else why not.

    Exit status alone cannot answer this. arecord returns 0 for every rate this
    AudioMoth was asked for, having quietly opened the device at 384 kHz each
    time — so judging by exit code declares all eight rates supported, and the
    caller then believes the one it asked for is fine. That is how five-minute
    recordings came back eight times their intended size while the log claimed
    the configured rate.
    """
    error: str | None = "no supported sample format"
    for count in candidates:
        for fmt in _FORMATS:
            probe = _capture_probe(
                binary, device, fmt=fmt, channels=count, rate=rate, seconds=seconds
            )
            if probe.ok and probe.delivered_rate not in (None, rate):
                # Accepted, but not honoured. No format or channel count
                # changes that, so stop asking.
                return f"device delivers {probe.delivered_rate} Hz, not {rate} Hz"
            error = probe.error
            if error is None:
                return None
    return error


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
            probe = _capture_probe(
                binary, device, fmt="S16_LE",
                channels=self.usable_channels(), rate=requested, seconds=0.4,
            )
            if probe.busy:
                # Nothing was learned about the rate, only that something else
                # holds the device. Answering once is fine; caching it would
                # condemn a good rate for the life of the process.
                log.debug("%s is busy — leaving %d Hz unjudged", device.name, requested)
                return requested
            delivered = probe.delivered_rate
            if probe.ok and delivered in (None, requested):
                self._good_rates.add(requested)
                return requested
            if probe.ok and delivered:
                # arecord exited 0 having quietly opened the device at its own
                # rate. Report what we will really get, so the caller downsamples
                # deliberately instead of mislabelling an oversized file.
                self._bad_rates.add(requested)
                self._good_rates.add(delivered)
                log.info(
                    "%s captures only at %d Hz, not the requested %d Hz",
                    device.name, delivered, requested,
                )
                return delivered
            self._bad_rates.add(requested)
            log.warning("%d Hz refused by %s: %s", requested, device.name, probe.error)

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
        # Not a warning: capturing at the rate the hardware actually offers is
        # the correct thing to do, and the caller reduces it to the configured
        # rate afterwards. What would deserve a warning is pretending the
        # device gave us a rate it never did.
        log.info(
            "%s cannot capture %d Hz natively — capturing its %d Hz instead",
            device.name, requested, nearest,
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

        native = self.usable_rate(sample_rate)
        channels = channels if channels is not None else self.usable_channels(native)

        # Some hardware offers exactly one rate — the AudioMoth USB Microphone
        # is fixed at 384 kHz in its USB descriptor, whatever the mode switch is
        # set to. Writing that rate when 48 kHz was configured costs eight times
        # the card space, so capture natively and reduce it as it arrives; the
        # full-rate audio is never written down.
        factor = 0
        if native != sample_rate:
            if sample_rate > 0 and native > sample_rate and native % sample_rate == 0:
                factor = native // sample_rate
            else:
                log.warning(
                    "%s captures at %d Hz, which is not a whole multiple of the "
                    "configured %d Hz — keeping the native rate rather than "
                    "resampling by an awkward ratio",
                    device.name, native, sample_rate,
                )
        rate = sample_rate if factor >= 2 else native

        self.output_dir.mkdir(parents=True, exist_ok=True)
        started = datetime.now()
        # Same naming the AudioMoth itself uses, so both sources sort together
        # and the uploader's date parsing works unchanged.
        final = self.output_dir / f"{started:%Y%m%d_%H%M%S}.WAV"
        partial = final.with_suffix(".WAV.part")

        # A reduced capture arrives as a raw stream we frame ourselves; an
        # unreduced one lets arecord write the container as before.
        capture = [
            binary, "-D", device.hw, "-f", "S16_LE", "-c", str(channels),
            "-r", str(native), "-d", str(int(seconds)),
            *(("-t", "raw", "-") if factor >= 2 else ("-t", "wav", str(partial))),
        ]

        if factor >= 2:
            log.info(
                "recording %.0fs from %s at %d Hz, %d ch, reduced %d:1 to %d Hz",
                seconds, device.name, native, channels, factor, rate,
            )
        else:
            log.info(
                "recording %.0fs at %d Hz, %d ch, from %s",
                seconds, rate, channels, device.name,
            )

        self.active_partial, self.active_rate, self.active_channels = partial, rate, channels
        try:
            if factor >= 2:
                stderr, returncode = self._record_reduced(
                    capture, partial, factor=factor, channels=channels,
                    out_rate=rate, timeout=seconds + 60,
                )
            else:
                self._process = subprocess.Popen(
                    capture, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
                )
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

    def _record_reduced(
        self,
        capture: list[str],
        partial: Path,
        *,
        factor: int,
        channels: int,
        out_rate: int,
        timeout: float,
    ) -> tuple[bytes, int]:
        """Capture natively and write the reduced file as the audio arrives.

        Nothing larger than one buffer is ever held, so a five-minute 384 kHz
        recording costs a few hundred kilobytes of memory and writes only the
        48 kHz file the configuration asked for.
        """
        decimators = [Decimator(factor) for _ in range(channels)]
        frame = 2 * channels  # 16-bit samples, interleaved
        deadline = time.monotonic() + timeout
        leftover = b""

        self._process = subprocess.Popen(
            capture, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        stream = self._process.stdout
        if stream is None:  # pragma: no cover - Popen always gives us the pipe
            raise RecorderError("could not read from arecord")

        with wave.open(str(partial), "wb") as out:
            out.setnchannels(channels)
            out.setsampwidth(2)
            out.setframerate(out_rate)
            while True:
                if time.monotonic() > deadline:
                    self.stop()
                    raise subprocess.TimeoutExpired(capture[0], timeout)
                block = stream.read(262144)
                if not block:
                    break
                block = leftover + block
                # A read can split an interleaved frame; carry the remainder.
                usable = len(block) - len(block) % frame
                leftover = block[usable:]
                if not usable:
                    continue
                samples = np.frombuffer(block[:usable], dtype="<i2")
                out.writeframes(
                    self._interleave(
                        [d.process(samples[i::channels]) for i, d in enumerate(decimators)]
                    )
                )
            out.writeframes(self._interleave([d.flush() for d in decimators]))

        self._process.wait(timeout=30)
        stderr = self._process.stderr.read() if self._process.stderr else b""
        return stderr or b"", self._process.returncode

    @staticmethod
    def _interleave(planes: list[np.ndarray]) -> bytes:
        """Weave per-channel samples back into one interleaved block."""
        if len(planes) == 1:
            return planes[0].tobytes()
        # Channels can differ by a sample at a chunk boundary; the shortest
        # decides, and the rest is carried by the decimators themselves.
        width = min(plane.size for plane in planes)
        if not width:
            return b""
        return np.column_stack([plane[:width] for plane in planes]).ravel().tobytes()

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
