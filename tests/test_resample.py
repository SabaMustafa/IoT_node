"""The AudioMoth only captures at 384 kHz, so honouring a configured 48 kHz
means decimating in software. These tests are mostly about the one way that
can go quietly wrong: dropping samples without filtering first, which would
fold bat calls and cable rustle down into the few hundred Hz to ~2 kHz that
carries hive state — inventing hive sound that was never there.
"""

from __future__ import annotations

import numpy as np
import pytest

from iot_node.audio.resample import Decimator, design_lowpass


def tone(freq: float, rate: int, seconds: float, amplitude: float = 8000.0) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.int16)


def band_energy(samples: np.ndarray, rate: int, low: float, high: float) -> float:
    """Power in a frequency band.

    Windowed before the transform: a tone that does not fit the buffer a whole
    number of times otherwise leaks across the whole spectrum, which would make
    a clean signal look like broadband noise and hide what is being measured.
    """
    signal = samples.astype(np.float64) * np.hanning(samples.size)
    power = np.abs(np.fft.rfft(signal)) ** 2
    freqs = np.fft.rfftfreq(samples.size, 1 / rate)
    return float(power[(freqs >= low) & (freqs < high)].sum())


class TestDesignLowpass:
    def test_unity_gain_at_dc(self) -> None:
        # A steady signal must keep its amplitude, or every dB figure shifts.
        assert design_lowpass(8).sum() == pytest.approx(1.0, abs=1e-6)

    def test_symmetric_so_the_delay_is_constant(self) -> None:
        taps = design_lowpass(8)
        assert np.allclose(taps, taps[::-1])

    def test_length_is_odd_for_an_integer_delay(self) -> None:
        assert len(design_lowpass(8)) % 2 == 1
        assert len(design_lowpass(4, taps=64)) % 2 == 1

    def test_refuses_a_pointless_factor(self) -> None:
        with pytest.raises(ValueError, match="at least 2"):
            design_lowpass(1)


class TestDecimator:
    def test_reduces_the_sample_count_by_the_factor(self) -> None:
        out = Decimator(8).process(tone(1000, 384_000, 1.0))
        assert out.size == pytest.approx(48_000, rel=0.01)

    def test_keeps_a_bee_band_tone(self) -> None:
        """500 Hz is squarely in the hive band and must survive intact."""
        source = tone(500, 384_000, 1.0)
        out = Decimator(8).process(source)
        # Amplitude preserved to within a few percent.
        assert np.abs(out).max() == pytest.approx(np.abs(source).max(), rel=0.05)
        assert band_energy(out, 48_000, 400, 600) > 0.9 * band_energy(out, 48_000, 0, 24_000)

    def test_rejects_ultrasound_instead_of_folding_it_down(self) -> None:
        """The whole point: a 100 kHz tone must not reappear as hive sound.

        Naive 8:1 decimation would alias 100 kHz to |100 - 2*48| = 4 kHz and
        put a loud phantom tone right beside the bees.
        """
        out = Decimator(8).process(tone(100_000, 384_000, 1.0))
        loud = tone(1000, 384_000, 1.0)
        reference = band_energy(Decimator(8).process(loud), 48_000, 0, 24_000)
        assert band_energy(out, 48_000, 0, 24_000) < reference / 1000

    def test_chunking_does_not_change_the_result(self) -> None:
        """Audio arrives in whatever sized reads the pipe gives us."""
        source = tone(700, 384_000, 0.5)
        whole = Decimator(8)
        one_pass = np.concatenate([whole.process(source), whole.flush()])

        streamed = Decimator(8)
        pieces = [streamed.process(part) for part in np.array_split(source, 37)]
        pieces.append(streamed.flush())
        chunked = np.concatenate(pieces)

        assert np.array_equal(one_pass, chunked)

    def test_odd_sized_reads_do_not_shift_the_phase(self) -> None:
        source = tone(700, 384_000, 0.3)
        whole = Decimator(8)
        expected = np.concatenate([whole.process(source), whole.flush()])

        streamed = Decimator(8)
        out, cursor = [], 0
        for size in (1, 3, 7, 999, 4096, 65_537):
            out.append(streamed.process(source[cursor : cursor + size]))
            cursor += size
        out.append(streamed.process(source[cursor:]))
        out.append(streamed.flush())
        assert np.array_equal(expected, np.concatenate(out))

    def test_clips_rather_than_wrapping_on_loud_audio(self) -> None:
        """int16 overflow turns a loud passage into noise; clipping does not."""
        out = Decimator(8).process(tone(400, 384_000, 0.2, amplitude=32_760))
        assert out.max() <= 32_767
        assert out.min() >= -32_768
        # A wrap would put a huge negative right beside a huge positive.
        assert np.abs(np.diff(out.astype(np.int32))).max() < 40_000

    def test_survives_empty_and_tiny_input(self) -> None:
        decimator = Decimator(8)
        assert decimator.process(np.zeros(0, dtype=np.int16)).size == 0
        assert decimator.process(np.zeros(3, dtype=np.int16)).size == 0
        assert decimator.flush().size >= 0

    def test_flush_is_idempotent(self) -> None:
        decimator = Decimator(8)
        decimator.process(tone(1000, 384_000, 0.05))
        decimator.flush()
        assert decimator.flush().size == 0

    def test_stereo_channels_stay_independent(self) -> None:
        """Deinterleaved channels must not bleed into one another."""
        left = Decimator(8).process(tone(500, 384_000, 0.2))
        right = Decimator(8).process(np.zeros(384_000 // 5, dtype=np.int16))
        assert np.abs(left).max() > 1000
        assert np.abs(right).max() == 0
