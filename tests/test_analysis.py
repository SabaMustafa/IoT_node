from __future__ import annotations

import struct
import wave

import numpy as np
import pytest

from iot_node.analysis import (
    AnalysisError,
    analyse_wav,
    analysis_columns,
    clipping_fraction,
    find_peaks,
)


def write_wav(
    path,
    *,
    tones: list[tuple[float, float]],
    rate: int = 48000,
    seconds: float = 1.0,
    width: int = 2,
    channels: int = 1,
    noise: float = 0.0,
):
    """Synthesise a WAV from a list of (frequency, amplitude) tones."""
    t = np.arange(int(rate * seconds)) / rate
    signal = np.zeros_like(t)
    for frequency, amplitude in tones:
        signal += amplitude * np.sin(2 * np.pi * frequency * t)
    if noise:
        signal += np.random.default_rng(0).normal(0, noise, signal.shape)
    signal = np.clip(signal, -1.0, 1.0)

    if width == 2:
        raw = (signal * 32767).astype("<i2").tobytes()
    elif width == 1:
        raw = ((signal * 127) + 128).astype(np.uint8).tobytes()
    elif width == 4:
        raw = (signal * 2147483647).astype("<i4").tobytes()
    else:
        raise ValueError(width)

    if channels > 1:
        frames = np.frombuffer(raw, dtype=f"<i{width}")
        raw = np.repeat(frames, channels).astype(f"<i{width}").tobytes()

    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(width)
        handle.setframerate(rate)
        handle.writeframes(raw)
    return path


# --- peak detection ----------------------------------------------------------


def test_finds_a_single_known_tone(tmp_path):
    path = write_wav(tmp_path / "tone.wav", tones=[(440.0, 0.5)])
    result = analyse_wav(path, top_peaks=5)

    assert result.peaks
    assert result.peaks[0].frequency_hz == pytest.approx(440, abs=5)
    assert result.dominant_hz == pytest.approx(440, abs=5)


def test_finds_several_tones_in_amplitude_order(tmp_path):
    """A hive spectrum is several simultaneous tones; order must follow level."""
    path = write_wav(
        tmp_path / "hive.wav",
        tones=[(220.0, 0.50), (330.0, 0.25), (505.0, 0.12)],
    )
    result = analyse_wav(path, top_peaks=3)

    found = [p.frequency_hz for p in result.peaks]
    assert found[0] == pytest.approx(220, abs=6)
    assert found[1] == pytest.approx(330, abs=6)
    assert found[2] == pytest.approx(505, abs=6)
    # Louder tones must report higher dB.
    levels = [p.amplitude_db for p in result.peaks]
    assert levels == sorted(levels, reverse=True)


def test_one_broad_peak_is_not_reported_five_times(tmp_path):
    """Naive top-N over bins would return five neighbours of the same peak."""
    path = write_wav(tmp_path / "one.wav", tones=[(300.0, 0.6)])
    result = analyse_wav(path, top_peaks=5)

    frequencies = sorted(p.frequency_hz for p in result.peaks)
    gaps = [b - a for a, b in zip(frequencies, frequencies[1:], strict=False)]
    assert all(gap >= 14.0 for gap in gaps), frequencies


def test_peak_frequency_is_interpolated_between_bins(tmp_path):
    """Bin width is ~11.7 Hz; a peak between bins should still land close."""
    path = write_wav(tmp_path / "offset.wav", tones=[(437.0, 0.5)])
    result = analyse_wav(path, top_peaks=1)
    assert result.peaks[0].frequency_hz == pytest.approx(437, abs=4)


def test_peaks_outside_the_band_are_ignored(tmp_path):
    path = write_wav(tmp_path / "wide.wav", tones=[(200.0, 0.2), (8000.0, 0.9)])
    result = analyse_wav(path, top_peaks=3, min_hz=20, max_hz=1000)

    assert all(p.frequency_hz <= 1000 for p in result.peaks)
    assert result.peaks[0].frequency_hz == pytest.approx(200, abs=6)


def test_find_peaks_on_an_empty_spectrum():
    assert find_peaks(np.array([]), np.array([]), count=5) == []


# --- levels ------------------------------------------------------------------


def test_louder_audio_reports_higher_dbfs(tmp_path):
    quiet = analyse_wav(write_wav(tmp_path / "q.wav", tones=[(300.0, 0.05)]))
    loud = analyse_wav(write_wav(tmp_path / "l.wav", tones=[(300.0, 0.5)]))

    assert loud.rms_dbfs > quiet.rms_dbfs
    # A 10x amplitude increase is about 20 dB.
    assert loud.rms_dbfs - quiet.rms_dbfs == pytest.approx(20, abs=1.5)


def test_dbfs_is_negative_and_peak_exceeds_rms(tmp_path):
    result = analyse_wav(write_wav(tmp_path / "t.wav", tones=[(300.0, 0.5)]))
    assert result.rms_dbfs < 0
    assert result.peak_dbfs > result.rms_dbfs


def test_calibration_offset_shifts_the_spl_estimate(tmp_path):
    path = write_wav(tmp_path / "t.wav", tones=[(300.0, 0.5)])
    plain = analyse_wav(path)
    calibrated = analyse_wav(path, calibration_db_offset=94.0)

    assert calibrated.rms_dbfs == pytest.approx(plain.rms_dbfs)
    assert calibrated.spl_estimate_db == pytest.approx(plain.rms_dbfs + 94.0)
    assert calibrated.calibration_db_offset == 94.0


def test_silence_does_not_divide_by_zero(tmp_path):
    path = write_wav(tmp_path / "silent.wav", tones=[])
    result = analyse_wav(path)
    assert result.rms_dbfs < -100  # floored, not -inf or NaN
    assert np.isfinite(result.rms_dbfs)


# --- formats -----------------------------------------------------------------


@pytest.mark.parametrize("width", [1, 2, 4])
def test_sample_widths_are_handled(tmp_path, width):
    path = write_wav(tmp_path / f"w{width}.wav", tones=[(440.0, 0.5)], width=width)
    result = analyse_wav(path, top_peaks=1)
    assert result.peaks[0].frequency_hz == pytest.approx(440, abs=8)


def test_stereo_is_mixed_to_mono(tmp_path):
    path = write_wav(tmp_path / "stereo.wav", tones=[(440.0, 0.5)], channels=2)
    result = analyse_wav(path, top_peaks=1)
    assert result.peaks[0].frequency_hz == pytest.approx(440, abs=8)
    assert result.duration_s == pytest.approx(1.0, abs=0.05)


@pytest.mark.parametrize("rate", [8000, 16000, 48000, 96000])
def test_various_sample_rates(tmp_path, rate):
    path = write_wav(tmp_path / f"r{rate}.wav", tones=[(400.0, 0.5)], rate=rate, seconds=0.5)
    result = analyse_wav(path, top_peaks=1)
    assert result.sample_rate == rate
    assert result.peaks[0].frequency_hz == pytest.approx(400, abs=15)


def test_band_is_clamped_to_nyquist(tmp_path):
    """Asking for 20 kHz from an 8 kHz recording must not invent bandwidth."""
    path = write_wav(tmp_path / "narrow.wav", tones=[(400.0, 0.5)], rate=8000, seconds=0.5)
    result = analyse_wav(path, max_hz=20000)
    assert result.band_max_hz == 4000


# --- failure modes -----------------------------------------------------------


def test_a_missing_file_raises_analysis_error(tmp_path):
    with pytest.raises(AnalysisError, match="cannot read WAV"):
        analyse_wav(tmp_path / "nope.wav")


def test_a_non_wav_file_raises_analysis_error(tmp_path):
    path = tmp_path / "not.wav"
    path.write_bytes(b"this is not a wav file at all")
    with pytest.raises(AnalysisError):
        analyse_wav(path)


def test_a_truncated_file_raises_rather_than_returning_nonsense(tmp_path):
    path = tmp_path / "tiny.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(48000)
        handle.writeframes(struct.pack("<h", 0) * 4)
    with pytest.raises(AnalysisError, match="too short"):
        analyse_wav(path)


# --- output shape ------------------------------------------------------------


def test_row_has_a_column_for_every_requested_peak(tmp_path):
    path = write_wav(tmp_path / "t.wav", tones=[(300.0, 0.5)])
    result = analyse_wav(path, top_peaks=5)
    row = result.as_row(5)

    for index in range(1, 6):
        assert f"peak{index}_hz" in row
        assert f"peak{index}_db" in row
    assert set(row) == set(analysis_columns(5))


def test_missing_peaks_leave_blank_columns_not_absent_ones(tmp_path):
    """A short row would misalign the CSV, so absent peaks are empty strings."""
    path = write_wav(tmp_path / "silent.wav", tones=[])
    result = analyse_wav(path, top_peaks=5)
    result.peaks = result.peaks[:2]
    row = result.as_row(5)

    assert row["peak5_hz"] == ""
    assert set(row) == set(analysis_columns(5))


def test_device_is_recorded_so_bench_audio_is_identifiable(tmp_path):
    path = write_wav(tmp_path / "t.wav", tones=[(300.0, 0.5)])
    result = analyse_wav(path, device="USB CAMERA (hw:2,0)")
    assert result.as_row(5)["device"] == "USB CAMERA (hw:2,0)"


def test_clipping_is_detected(tmp_path):
    clipped = write_wav(tmp_path / "clipped.wav", tones=[(300.0, 5.0)])
    clean = write_wav(tmp_path / "clean.wav", tones=[(300.0, 0.3)])

    assert clipping_fraction(clipped) > 0.5
    assert clipping_fraction(clean) == 0.0
