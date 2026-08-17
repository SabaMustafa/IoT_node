from __future__ import annotations

import subprocess

import pytest

from iot_node.audio.recorder import (
    CaptureDevice,
    Recorder,
    RecorderError,
    find_audiomoth,
    list_capture_devices,
    probe_sample_rates,
)

ARECORD_L = """\
**** List of CAPTURE Hardware Devices ****
card 2: CAMERA [USB CAMERA], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
card 3: AudioMoth [AudioMoth USB Microphone], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
"""


@pytest.fixture(autouse=True)
def _arecord_on_path(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")


def fake_run(monkeypatch, handler):
    calls: list[list[str]] = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return handler(cmd)

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def ok(stdout: str = "", stderr: str = ""):
    return lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout, stderr)


# --- device discovery --------------------------------------------------------


def test_parses_arecord_listing(monkeypatch):
    fake_run(monkeypatch, ok(ARECORD_L))
    devices = list_capture_devices()

    assert [d.name for d in devices] == ["USB CAMERA", "AudioMoth USB Microphone"]
    assert devices[1].card == 3
    assert devices[1].hw == "hw:3,0"


def test_finds_the_audiomoth_among_others(monkeypatch):
    fake_run(monkeypatch, ok(ARECORD_L))
    device = find_audiomoth()

    assert device is not None
    assert device.card == 3


def test_never_falls_back_to_another_microphone(monkeypatch):
    """Recording the webcam mic and calling it bat data would be worse than nothing."""
    listing = "card 2: CAMERA [USB CAMERA], device 0: USB Audio [USB Audio]\n"
    fake_run(monkeypatch, ok(listing))

    assert find_audiomoth() is None


def test_no_devices_at_all(monkeypatch):
    fake_run(monkeypatch, ok("**** List of CAPTURE Hardware Devices ****\n"))
    assert list_capture_devices() == []


def test_missing_arecord_is_reported(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _name: None)
    with pytest.raises(RecorderError, match="alsa-utils"):
        list_capture_devices()


def test_device_identification_is_case_insensitive():
    assert CaptureDevice(1, 0, "audiomoth", "some usb thing").is_audiomoth
    assert CaptureDevice(1, 0, "X", "AudioMoth USB Microphone").is_audiomoth
    assert not CaptureDevice(1, 0, "CAMERA", "USB CAMERA").is_audiomoth


# --- sample-rate probing -----------------------------------------------------


def test_probe_reports_which_rates_work(monkeypatch):
    device = CaptureDevice(3, 0, "AudioMoth", "AudioMoth USB Microphone")
    supported = {"48000", "192000"}

    def handler(cmd):
        rate = cmd[cmd.index("-r") + 1]
        if rate in supported:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 1, "", "Sample rate not available")

    fake_run(monkeypatch, handler)
    results = probe_sample_rates(device, rates=(8000, 48000, 192000))

    assert results[48000] is None
    assert results[192000] is None
    assert "not available" in (results[8000] or "")


def test_probe_uses_hw_not_plughw(monkeypatch):
    """plughw would resample and report every rate as supported."""
    device = CaptureDevice(3, 0, "AudioMoth", "AudioMoth USB Microphone")
    calls = fake_run(monkeypatch, ok())

    probe_sample_rates(device, rates=(48000,))

    assert "hw:3,0" in calls[0]
    assert not any("plughw" in str(arg) for arg in calls[0])


# --- recording ---------------------------------------------------------------


def test_unsupported_rate_falls_back_to_the_nearest_native(monkeypatch):
    device = CaptureDevice(3, 0, "AudioMoth", "AudioMoth USB Microphone")
    recorder = Recorder(output_dir=None, device=device)  # type: ignore[arg-type]

    def handler(cmd):
        rate = int(cmd[cmd.index("-r") + 1])
        code = 0 if rate in (48000, 96000) else 1
        return subprocess.CompletedProcess(cmd, code, "", "Sample rate not available")

    fake_run(monkeypatch, handler)

    # 192000 is unavailable; 96000 is the closest rate the hardware really has.
    assert recorder.usable_rate(192000) == 96000
    assert recorder.usable_rate(48000) == 48000


def test_record_without_a_device_is_a_clear_error(monkeypatch, tmp_path):
    fake_run(monkeypatch, ok("**** List of CAPTURE Hardware Devices ****\n"))
    recorder = Recorder(tmp_path / "audio")

    with pytest.raises(RecorderError, match="USB/OFF"):
        recorder.record(sample_rate=48000, seconds=1)


def test_partial_files_are_cleaned_up(tmp_path):
    output = tmp_path / "audio"
    output.mkdir()
    stale = output / "20260814_120000.WAV.part"
    stale.write_bytes(b"x" * 100)

    Recorder(output).cleanup_partials()

    assert not stale.exists()


def test_recording_writes_a_part_file_then_renames(monkeypatch, tmp_path):
    """A half-written recording must never be visible to the uploader."""
    device = CaptureDevice(3, 0, "AudioMoth", "AudioMoth USB Microphone")
    output = tmp_path / "audio"
    recorder = Recorder(output, device=device)
    recorder._supported = {48000: None}
    recorder._good_rates = {48000}   # skip the probe; it would need real arecord
    recorder._channels = 1

    class FakePopen:
        returncode = 0

        def __init__(self, cmd, **kwargs):
            # arecord writes to the .part path it was given.
            target = tmp_path / cmd[-1]
            assert str(target).endswith(".part")
            target.write_bytes(b"RIFF" + b"\0" * 4096)

        def communicate(self, timeout=None):
            return b"", b""

        def poll(self):
            return 0

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    path = recorder.record(sample_rate=48000, seconds=1)

    assert path.suffix == ".WAV"
    assert not path.name.endswith(".part")
    assert list(output.glob("*.part")) == []


def test_failed_arecord_leaves_nothing_behind(monkeypatch, tmp_path):
    device = CaptureDevice(3, 0, "AudioMoth", "AudioMoth USB Microphone")
    output = tmp_path / "audio"
    recorder = Recorder(output, device=device)
    recorder._supported = {48000: None}
    recorder._good_rates = {48000}
    recorder._channels = 1

    class FailingPopen:
        returncode = 1

        def __init__(self, cmd, **kwargs):
            pass

        def communicate(self, timeout=None):
            return b"", b"arecord: device busy"

        def poll(self):
            return 1

    monkeypatch.setattr(subprocess, "Popen", FailingPopen)
    with pytest.raises(RecorderError, match="device busy"):
        recorder.record(sample_rate=48000, seconds=1)

    assert list(output.glob("*")) == []


# --- "is it really the AudioMoth?" -------------------------------------------


def test_auto_refuses_a_substitute_device(monkeypatch):
    """The guarantee: an AudioMoth or nothing, never a stand-in.

    Recording a webcam for a week and filing it as hive audio is worse than
    recording nothing, because nothing is obviously wrong and bad data is not.
    """
    from iot_node.audio.recorder import select_device

    listing = "card 2: CAMERA [USB CAMERA], device 0: USB Audio [USB Audio]\n"
    fake_run(monkeypatch, ok(listing))

    assert select_device("auto") is None
    # 'any' is the explicit bench-testing opt-in, and does substitute.
    assert select_device("any") is not None


def test_any_still_prefers_a_real_audiomoth(monkeypatch):
    from iot_node.audio.recorder import select_device

    fake_run(monkeypatch, ok(ARECORD_L))
    device = select_device("any")
    assert device is not None and device.is_audiomoth


def test_explicit_alsa_name_is_honoured(monkeypatch):
    from iot_node.audio.recorder import select_device

    fake_run(monkeypatch, ok(ARECORD_L))
    device = select_device("hw:2,0")
    assert device is not None and device.card == 2


def test_diagnosis_distinguishes_absent_from_wrong_mode(monkeypatch):
    """Two failures look identical to ALSA and need different fixes."""
    import iot_node.audio.recorder as rec

    fake_run(monkeypatch, ok("card 2: CAMERA [USB CAMERA], device 0: USB Audio [x]\n"))

    monkeypatch.setattr(rec, "audiomoth_usb_present", lambda: False)
    absent = rec.diagnose_missing_audiomoth()
    assert "on the USB bus at all" in absent
    assert "charge-only" in absent  # the usual culprit when it is plugged in

    monkeypatch.setattr(rec, "audiomoth_usb_present", lambda: True)
    message = rec.diagnose_missing_audiomoth()
    # Observed on real hardware: the AudioMoth USB Microphone enumerates as
    # 10c4:0002 with interface class 03 (HID) in the USB/OFF position, which is
    # the configuration mode — so the advice must be to move the switch, not to
    # select USB/OFF.
    assert "DEFAULT" in message and "CUSTOM" in message
    assert "firmware" in message

    monkeypatch.setattr(rec, "audiomoth_usb_present", lambda: None)
    assert "USB CAMERA" in rec.diagnose_missing_audiomoth()


def test_usb_detection_knows_both_audiomoth_identities(tmp_path, monkeypatch):
    """Observed on real hardware — the ids differ by switch position.

    10c4:0002 is the configuration (HID) mode; 16d0:06f3 is the microphone.
    Matching only the first reported "not plugged in" while it was plugged in.
    """
    import iot_node.audio.recorder as rec

    def fake_bus(entries: list[tuple[str, str]]):
        root = tmp_path / f"bus{len(entries)}{entries[0][0]}"
        for index, (vendor, product) in enumerate(entries):
            device = root / f"3-{index}"
            device.mkdir(parents=True)
            (device / "idVendor").write_text(vendor + "\n")
            if product:
                (device / "product").write_text(product + "\n")
        monkeypatch.setattr(rec, "Path", lambda _p: root)
        return root

    fake_bus([("16d0", "384kHz AudioMoth USB Microphone")])
    assert rec.audiomoth_usb_present() is True

    fake_bus([("10c4", "AudioMoth")])
    assert rec.audiomoth_usb_present() is True

    fake_bus([("0bda", "USB CAMERA")])
    assert rec.audiomoth_usb_present() is False
