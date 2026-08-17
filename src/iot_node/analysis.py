"""Frequency analysis of hive recordings.

For each recording we report the loudest spectral peaks and an overall sound
level. A honeybee colony is an acoustically informative thing: wingbeat
fundamentals sit near 200-250 Hz, worker piping and whooping around 300-600 Hz,
and shifts in those bands track swarming, queenlessness and stress. So the
useful signal is almost entirely below 1 kHz, which is why the default analysis
window stops at 2 kHz — going wider mostly measures wind, rain and road noise.

## About "dB"

A plain USB microphone cannot report absolute sound pressure. What it gives is
a number relative to digital full scale — **dBFS** — which is always negative
and depends on the microphone's own gain. This module reports dBFS honestly and
separately reports an SPL *estimate*, which is simply dBFS plus the
``calibration_db_offset`` from config.ini.

That offset is zero until somebody measures it: put a calibrated sound level
meter beside the microphone, note the real dB SPL and the reported dBFS at the
same moment, and set the offset to the difference. Until then, treat the SPL
column as uncalibrated and compare recordings only against each other — which
is fine for spotting change over time, and wrong for quoting absolute loudness.

The peak amplitudes are reported the same way, in dB relative to full scale.
"""

from __future__ import annotations

import logging
import math
import wave
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

# Analysis window. 4096 samples at 48 kHz is ~85 ms, giving ~11.7 Hz resolution
# — fine enough to separate a 220 Hz wingbeat from a 300 Hz pipe.
FFT_SIZE = 4096

# Peaks closer together than this are the same acoustic event seen twice
# through the window's skirts, so only the taller one is reported.
MIN_PEAK_SEPARATION_HZ = 15.0

# Cap on how much audio is read into memory at once. A 5-minute 384 kHz file is
# ~230 MB as float64; on a Pi that is worth avoiding.
MAX_SAMPLES = 8_000_000


class AnalysisError(RuntimeError):
    """A recording could not be analysed. Logged; never fatal to the node."""


@dataclass
class Peak:
    frequency_hz: float
    amplitude_db: float  # dB relative to full scale

    def as_dict(self, index: int) -> dict[str, Any]:
        return {
            f"peak{index}_hz": round(self.frequency_hz, 1),
            f"peak{index}_db": round(self.amplitude_db, 1),
        }


@dataclass
class AnalysisResult:
    path: Path
    sample_rate: int
    duration_s: float
    rms_dbfs: float
    peak_dbfs: float
    spl_estimate_db: float
    calibration_db_offset: float
    band_min_hz: float
    band_max_hz: float
    dominant_hz: float
    peaks: list[Peak] = field(default_factory=list)
    analysed_at: str = ""
    device: str = ""

    def as_row(self, top_peaks: int) -> dict[str, Any]:
        """Flatten to one CSV row."""
        row: dict[str, Any] = {
            "filename": self.path.name,
            "analysed_at_utc": self.analysed_at,
            "sample_rate_hz": self.sample_rate,
            "duration_s": round(self.duration_s, 2),
            "rms_dbfs": round(self.rms_dbfs, 1),
            "peak_dbfs": round(self.peak_dbfs, 1),
            "spl_estimate_db": round(self.spl_estimate_db, 1),
            "calibration_db_offset": round(self.calibration_db_offset, 1),
            "dominant_hz": round(self.dominant_hz, 1),
            "band_hz": f"{self.band_min_hz:g}-{self.band_max_hz:g}",
            "device": self.device,
        }
        for index in range(1, top_peaks + 1):
            peak = self.peaks[index - 1] if index <= len(self.peaks) else None
            row.update(
                peak.as_dict(index) if peak else {f"peak{index}_hz": "", f"peak{index}_db": ""}
            )
        return row

    def summary(self) -> str:
        peaks = ", ".join(f"{p.frequency_hz:.0f}Hz@{p.amplitude_db:.0f}dB" for p in self.peaks[:3])
        return f"{self.rms_dbfs:.1f} dBFS, dominant {self.dominant_hz:.0f} Hz [{peaks}]"


def analysis_columns(top_peaks: int) -> list[str]:
    """CSV header for the analysis file, in a fixed order."""
    columns = [
        "filename", "analysed_at_utc", "sample_rate_hz", "duration_s",
        "rms_dbfs", "peak_dbfs", "spl_estimate_db", "calibration_db_offset",
        "dominant_hz", "band_hz", "device",
    ]
    for index in range(1, top_peaks + 1):
        columns += [f"peak{index}_hz", f"peak{index}_db"]
    return columns


def _read_wav(path: Path) -> tuple[np.ndarray, int]:
    """Read a WAV as mono float in [-1, 1], plus its sample rate."""
    try:
        with wave.open(str(path), "rb") as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            rate = handle.getframerate()
            frames = handle.getnframes()
            if rate <= 0 or frames <= 0:
                raise AnalysisError(f"{path.name}: empty or malformed WAV")
            frames = min(frames, MAX_SAMPLES // max(1, channels))
            raw = handle.readframes(frames)
    except (OSError, EOFError, wave.Error) as exc:
        raise AnalysisError(f"{path.name}: cannot read WAV ({exc})") from exc

    if width == 1:
        # 8-bit WAV is unsigned, centred on 128.
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 3:
        # 24-bit packed little-endian; widen to 32-bit preserving sign.
        trimmed = np.frombuffer(raw, dtype=np.uint8)
        usable = (len(trimmed) // 3) * 3
        triples = trimmed[:usable].reshape(-1, 3).astype(np.int32)
        values = triples[:, 0] | (triples[:, 1] << 8) | (triples[:, 2] << 16)
        values = np.where(values & 0x800000, values - 0x1000000, values)
        data = values.astype(np.float32) / 8388608.0
    elif width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise AnalysisError(f"{path.name}: unsupported sample width {width * 8}-bit")

    if channels > 1:
        usable = (len(data) // channels) * channels
        data = data[:usable].reshape(-1, channels).mean(axis=1)
    return data, rate


def _to_db(amplitude: float | np.ndarray) -> Any:
    """Amplitude to dB relative to full scale, floored to avoid log(0)."""
    return 20.0 * np.log10(np.maximum(amplitude, 1e-12))


def find_peaks(
    spectrum: np.ndarray,
    freqs: np.ndarray,
    *,
    count: int,
    min_separation_hz: float = MIN_PEAK_SEPARATION_HZ,
) -> list[Peak]:
    """Pick the highest local maxima, keeping them meaningfully far apart.

    Greedy rather than clever: take the tallest remaining bin, suppress its
    neighbourhood, repeat. That avoids reporting one broad peak five times,
    which is what a naive top-N over bins would do.
    """
    if spectrum.size == 0:
        return []

    order = np.argsort(spectrum)[::-1]
    chosen: list[int] = []
    for index in order:
        if len(chosen) >= count:
            break
        if any(abs(freqs[index] - freqs[taken]) < min_separation_hz for taken in chosen):
            continue
        chosen.append(int(index))

    chosen.sort(key=lambda i: -spectrum[i])
    return [
        Peak(
            frequency_hz=_interpolate_peak(spectrum, freqs, index),
            amplitude_db=float(_to_db(spectrum[index])),
        )
        for index in chosen
    ]


def _interpolate_peak(spectrum: np.ndarray, freqs: np.ndarray, index: int) -> float:
    """Refine a peak's frequency by fitting a parabola to its neighbours.

    Without this a peak is only located to the nearest bin (~12 Hz at the
    default settings), which is coarse when the point is to notice a colony's
    fundamental drifting by a few hertz.
    """
    if index <= 0 or index >= spectrum.size - 1:
        return float(freqs[index])
    alpha, beta, gamma = (float(_to_db(spectrum[index + offset])) for offset in (-1, 0, 1))
    denominator = alpha - 2 * beta + gamma
    if denominator == 0:
        return float(freqs[index])
    shift = 0.5 * (alpha - gamma) / denominator
    shift = max(-0.5, min(0.5, shift))
    bin_width = float(freqs[1] - freqs[0]) if freqs.size > 1 else 0.0
    return float(freqs[index] + shift * bin_width)


def analyse_wav(
    path: Path,
    *,
    top_peaks: int = 5,
    min_hz: float = 20.0,
    max_hz: float = 2000.0,
    calibration_db_offset: float = 0.0,
    device: str = "",
) -> AnalysisResult:
    """Analyse one recording. Raises AnalysisError if it cannot be read."""
    samples, rate = _read_wav(path)
    if samples.size == 0:
        raise AnalysisError(f"{path.name}: no audio samples")

    duration = samples.size / rate
    rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
    peak_amplitude = float(np.max(np.abs(samples)))
    rms_dbfs = float(_to_db(rms))

    # Welch-style average: split into overlapping windows and mean the spectra.
    # A single FFT over five minutes would smear transients into the noise;
    # averaging gives a stable picture of the colony's steady hum.
    window_size = min(FFT_SIZE, samples.size)
    if window_size < 16:
        raise AnalysisError(f"{path.name}: too short to analyse ({samples.size} samples)")
    window = np.hanning(window_size)
    # Normalise so window loss does not bias the reported amplitudes.
    window_gain = float(np.sum(window)) / 2.0

    step = max(1, window_size // 2)
    starts = range(0, max(1, samples.size - window_size + 1), step)
    accumulator = np.zeros(window_size // 2 + 1, dtype=np.float64)
    count = 0
    for start in starts:
        chunk = samples[start : start + window_size]
        if chunk.size < window_size:
            break
        accumulator += np.abs(np.fft.rfft(chunk * window))
        count += 1
    if count == 0:
        raise AnalysisError(f"{path.name}: not enough audio for one analysis window")

    spectrum = accumulator / (count * window_gain)
    freqs = np.fft.rfftfreq(window_size, d=1.0 / rate)

    # Restrict to the band of interest before picking peaks.
    band = (freqs >= min_hz) & (freqs <= min(max_hz, rate / 2))
    if not band.any():
        raise AnalysisError(
            f"{path.name}: analysis band {min_hz:g}-{max_hz:g} Hz is empty at {rate} Hz"
        )
    band_spectrum = spectrum[band]
    band_freqs = freqs[band]

    peaks = find_peaks(band_spectrum, band_freqs, count=top_peaks)
    dominant = peaks[0].frequency_hz if peaks else float(band_freqs[int(np.argmax(band_spectrum))])

    return AnalysisResult(
        path=path,
        sample_rate=rate,
        duration_s=duration,
        rms_dbfs=rms_dbfs,
        peak_dbfs=float(_to_db(peak_amplitude)),
        spl_estimate_db=rms_dbfs + calibration_db_offset,
        calibration_db_offset=calibration_db_offset,
        band_min_hz=min_hz,
        band_max_hz=min(max_hz, rate / 2),
        dominant_hz=dominant,
        peaks=peaks,
        analysed_at=datetime.now(UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        device=device,
    )


def clipping_fraction(path: Path, threshold: float = 0.99) -> float:
    """Fraction of samples at full scale — a clipped recording is unusable."""
    try:
        samples, _ = _read_wav(path)
    except AnalysisError:
        return math.nan
    if samples.size == 0:
        return math.nan
    return float(np.mean(np.abs(samples) >= threshold))
