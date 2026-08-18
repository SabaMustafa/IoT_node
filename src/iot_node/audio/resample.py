"""Reduce a capture to the configured sample rate, without aliasing.

Some capture hardware offers exactly one rate. The AudioMoth USB Microphone is
fixed at 384 kHz in its USB descriptor — no mode switch or ALSA setting changes
that — so honouring ``sample_rate = 48000`` has to happen in software, and a
384 kHz file is eight times the size of the 48 kHz one that was asked for.

Throwing away seven of every eight samples on its own would be wrong, not just
crude: everything the microphone hears between 24 kHz and 192 kHz would fold
back down into the band the bees actually occupy, and a bat pass or a rubbed
cable would arrive disguised as hive sound. So the ultrasound is filtered out
first, and only then are samples dropped.

This runs on every recording on a Raspberry Pi, so it works in chunks with the
filter state carried across them: memory stays flat regardless of how long the
recording is, and the full-rate audio never has to exist all at once.

Deliberately numpy rather than ffmpeg or sox — numpy is already required for
the frequency analysis, whereas both external tools proved to be absent or
broken on the field hardware.
"""

from __future__ import annotations

import numpy as np

# Enough taps for roughly 90 dB of stopband rejection with a Blackman window,
# scaled to the decimation factor because a steeper reduction needs a sharper
# filter. 8:1 gives 129 taps, which a Pi 5 convolves comfortably faster than
# real time.
_TAPS_PER_FACTOR = 16

# Cut a little below the new Nyquist rather than exactly at it, leaving the
# filter somewhere to roll off. At 48 kHz output this passes everything to
# ~21.6 kHz — far above the few hundred Hz to ~2 kHz that carries hive state.
_CUTOFF_MARGIN = 0.9


def design_lowpass(factor: int, taps: int | None = None) -> np.ndarray:
    """A windowed-sinc low-pass suited to decimating by ``factor``.

    Returns taps normalised to unity gain at DC, so a steady signal keeps its
    amplitude and the dB figures stay comparable with un-decimated recordings.
    """
    if factor < 2:
        raise ValueError(f"decimation factor must be at least 2, got {factor}")
    length = taps if taps is not None else _TAPS_PER_FACTOR * factor + 1
    # An odd length gives a symmetric filter with an exact integer group delay.
    if length % 2 == 0:
        length += 1

    cutoff = _CUTOFF_MARGIN * 0.5 / factor  # cycles per sample
    n = np.arange(length, dtype=np.float64) - (length - 1) / 2
    kernel = 2 * cutoff * np.sinc(2 * cutoff * n) * np.blackman(length)
    return (kernel / kernel.sum()).astype(np.float32)


class Decimator:
    """Streams audio down by an integer factor, one chunk at a time.

    Feed it whatever arrives from the capture; take what comes back. The tail
    of each chunk is retained so the filter sees a continuous signal and no
    click is introduced at the joins.
    """

    def __init__(self, factor: int, taps: np.ndarray | None = None) -> None:
        self.factor = int(factor)
        self.taps = design_lowpass(self.factor) if taps is None else taps.astype(np.float32)
        # Samples carried over: the filter needs the preceding taps-1 samples
        # to produce its next output.
        self._tail = np.zeros(0, dtype=np.float32)

    @property
    def delay(self) -> int:
        """Group delay in output samples — the same for every recording."""
        return (len(self.taps) - 1) // 2 // self.factor

    def process(self, samples: np.ndarray) -> np.ndarray:
        """Filter and decimate one chunk of int16 samples."""
        if samples.size:
            self._tail = np.concatenate([self._tail, samples.astype(np.float32)])
        return self._drain()

    def flush(self) -> np.ndarray:
        """Emit what the filter still holds, padding the end with silence."""
        if self._tail.size == 0:
            return np.zeros(0, dtype=np.int16)
        self._tail = np.concatenate(
            [self._tail, np.zeros(len(self.taps) - 1, dtype=np.float32)]
        )
        out = self._drain()
        self._tail = np.zeros(0, dtype=np.float32)
        return out

    def _drain(self) -> np.ndarray:
        window = len(self.taps)
        usable = self._tail.size - window + 1
        if usable <= 0:
            return np.zeros(0, dtype=np.int16)

        # One output per `factor` inputs. Consuming an exact multiple of the
        # factor keeps every chunk phase-aligned with the last.
        count = 1 + (usable - 1) // self.factor
        views = np.lib.stride_tricks.sliding_window_view(self._tail, window)
        filtered = views[: count * self.factor : self.factor] @ self.taps
        self._tail = self._tail[count * self.factor :]

        # int16 wraps on overflow, turning a loud passage into noise, so clip.
        return np.clip(np.rint(filtered), -32768, 32767).astype(np.int16)
