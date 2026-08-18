"""A recording must be analysed all the way to its end.

The reader used to stop after 8,000,000 frames. At 48 kHz that is 166.7s, so
the last 2.2 minutes of every five-minute recording went unexamined — and the
CSV recorded the truncated span as the recording's duration, misstating the
data rather than merely limiting it. At the 384 kHz the AudioMoth actually
delivers, only the first 20.8s of a five-minute file was ever seen.

Nothing in the suite noticed, because every fixture was shorter than the cap.
These tests are deliberately longer than it.
"""

from __future__ import annotations

import wave

import numpy as np
import pytest

from iot_node.analysis import analyse_wav as analyse

RATE = 48_000
# Longer than the old 8M-frame cap (166.7s at this rate), so a truncating
# reader and a complete one cannot produce the same answer.
DURATION_S = 300
EARLY_HZ = 400.0
LATE_HZ = 900.0
LATE_STARTS_AT_S = 240


@pytest.fixture
def recording(tmp_path):
    """Five minutes: one tone throughout, a second only in the final minute."""
    t = np.arange(RATE * DURATION_S) / RATE
    audio = 0.30 * np.sin(2 * np.pi * EARLY_HZ * t)
    tail = t >= LATE_STARTS_AT_S
    # Louder than the early tone, so if the tail is read at all it cannot be
    # mistaken for noise.
    audio[tail] += 0.55 * np.sin(2 * np.pi * LATE_HZ * t[tail])

    path = tmp_path / "20260818_120000.WAV"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes((audio * 32000).astype("<i2").tobytes())
    return path


def test_reports_the_recordings_true_duration(recording) -> None:
    result = analyse(recording)
    assert result.duration_s == pytest.approx(DURATION_S, abs=0.5), (
        "duration_s must describe the recording, not how much of it was read"
    )


def test_finds_a_tone_present_only_in_the_final_minute(recording) -> None:
    """The whole point: audio after the old cut-off is really analysed."""
    result = analyse(recording, top_peaks=5, min_hz=20.0, max_hz=2000.0)
    found = [peak.frequency_hz for peak in result.peaks]
    assert any(abs(hz - LATE_HZ) < 15.0 for hz in found), (
        f"the {LATE_HZ:.0f} Hz tone from the last minute is missing from {found}; "
        "the tail of the recording was not analysed"
    )


def test_still_finds_the_tone_that_runs_throughout(recording) -> None:
    result = analyse(recording, top_peaks=5, min_hz=20.0, max_hz=2000.0)
    found = [peak.frequency_hz for peak in result.peaks]
    assert any(abs(hz - EARLY_HZ) < 15.0 for hz in found), found


def test_levels_cover_every_sample(recording) -> None:
    """RMS and peak describe the whole file, not the first part of it.

    The final minute is the loudest, so a truncated read reports a quieter
    recording than the one actually made.
    """
    result = analyse(recording)
    with wave.open(str(recording)) as handle:
        everything = np.frombuffer(
            handle.readframes(handle.getnframes()), dtype="<i2"
        ).astype(np.float64) / 32768.0
    expected_rms_db = 20 * np.log10(np.sqrt((everything**2).mean()))
    expected_peak_db = 20 * np.log10(np.abs(everything).max())

    assert result.rms_dbfs == pytest.approx(expected_rms_db, abs=0.1)
    assert result.peak_dbfs == pytest.approx(expected_peak_db, abs=0.1)


def test_block_size_does_not_change_the_result(recording, monkeypatch) -> None:
    """Windows that straddle a read boundary must still be counted once."""
    import iot_node.analysis as analysis

    baseline = analyse(recording, top_peaks=5)
    # An awkward block size, deliberately not a multiple of the window step.
    monkeypatch.setattr(analysis, "READ_BLOCK_FRAMES", 7919)
    awkward = analyse(recording, top_peaks=5)

    assert awkward.duration_s == pytest.approx(baseline.duration_s, abs=0.01)
    assert awkward.rms_dbfs == pytest.approx(baseline.rms_dbfs, abs=0.01)
    assert awkward.dominant_hz == pytest.approx(baseline.dominant_hz, abs=1.0)
