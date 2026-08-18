"""A device that accepts every rate and honours only one.

The AudioMoth USB Microphone is fixed at 384 kHz in its USB descriptor. Ask
arecord for 48 kHz on it and you get exit status 0, a warning on stderr, and a
file whose header says 384000 — eight times the size that was configured.

Judging support by exit status therefore declares all eight rates native. That
mistake survived a first round of fixing because it lived in the *fallback*
probe rather than the fast path: the first recording of a session came back
correctly reduced, the fallback then ran once, cached every rate as good, and
every recording afterwards was written at 384 kHz while the log claimed 48 kHz.
A single call could never see it, so these tests call twice.
"""

from __future__ import annotations

import subprocess

import pytest

from iot_node.audio.recorder import (
    CaptureDevice,
    Recorder,
    probe_sample_rates,
)

NATIVE_HZ = 384_000

DEVICE = CaptureDevice(card=3, device=0, card_id="Microphone", name="384kHz AudioMoth")


@pytest.fixture(autouse=True)
def _arecord_on_path(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")


def substituting_device(monkeypatch, native: int = NATIVE_HZ):
    """Stand in for hardware that quietly ignores the rate it is given."""
    calls: list[list[str]] = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        asked = int(cmd[cmd.index("-r") + 1]) if "-r" in cmd else native
        if asked == native:
            stderr = f"Recording raw data '/dev/null' : Signed 16 bit, Rate {native} Hz, Mono\n"
        else:
            # Exactly what arecord prints: a warning, then success anyway.
            stderr = (
                f"Warning: rate is not accurate (requested = {asked}Hz, got = {native}Hz)\n"
                f"         please, try the plug plugin\n"
                f"Recording raw data '/dev/null' : Signed 16 bit, Rate {native} Hz, Mono\n"
            )
        return subprocess.CompletedProcess(cmd, 0, "", stderr)

    monkeypatch.setattr(subprocess, "run", run)
    return calls


class TestProbeTellsTheTruth:
    def test_only_the_native_rate_counts_as_supported(self, monkeypatch) -> None:
        substituting_device(monkeypatch)
        results = probe_sample_rates(DEVICE, rates=(16_000, 48_000, NATIVE_HZ), channels=1)

        assert results[NATIVE_HZ] is None, "the rate the device really gives must pass"
        for rate in (16_000, 48_000):
            assert results[rate] is not None, (
                f"{rate} Hz was reported as natively supported, but the device "
                f"delivers {NATIVE_HZ} Hz — this is what produced oversized files"
            )
            assert str(NATIVE_HZ) in results[rate]


class TestRateIsStableAcrossCalls:
    def test_second_call_agrees_with_the_first(self, monkeypatch) -> None:
        """The regression: call once, get 384000; call again, get 48000."""
        substituting_device(monkeypatch)
        recorder = Recorder(output_dir=None, device=DEVICE)  # type: ignore[arg-type]

        first = recorder.usable_rate(48_000)
        second = recorder.usable_rate(48_000)
        third = recorder.usable_rate(48_000)

        assert first == NATIVE_HZ
        assert second == NATIVE_HZ, (
            "the second call returned the requested rate, so the recording would "
            "be written at 384 kHz while claiming 48 kHz"
        )
        assert third == NATIVE_HZ

    def test_the_fallback_probe_does_not_poison_the_cache(self, monkeypatch) -> None:
        substituting_device(monkeypatch)
        recorder = Recorder(output_dir=None, device=DEVICE)  # type: ignore[arg-type]

        recorder.usable_rate(48_000)  # populates the caches
        recorder.usable_rate(48_000)  # runs the full fallback probe

        assert 48_000 not in recorder._good_rates, (
            "a rate the device never delivers was cached as good"
        )
        assert NATIVE_HZ in recorder._good_rates

    def test_a_device_that_honours_the_request_is_left_alone(self, monkeypatch) -> None:
        """Honest hardware must not be dragged through the substitution path."""
        substituting_device(monkeypatch, native=48_000)
        recorder = Recorder(output_dir=None, device=DEVICE)  # type: ignore[arg-type]

        assert recorder.usable_rate(48_000) == 48_000
        assert recorder.usable_rate(48_000) == 48_000
        assert 48_000 in recorder._good_rates
