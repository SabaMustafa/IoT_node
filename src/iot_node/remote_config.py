"""The operator-facing config.ini that lives in the cloud.

This is deliberately separate from config.yaml. config.yaml is deployment
plumbing — paths, credentials, remote names — and changing it means touching the
Pi. config.ini answers the questions a field researcher actually asks ("record
the dawn chorus, log sensors all day, ship it once a night"), and it lives in
the IoT folder on Google Drive so it can be edited from a laptop or a phone.

The node re-reads it periodically, so an edit on Drive takes effect within one
refresh interval without anyone logging into the node.

Robustness matters more than strictness here: this file is hand-edited by
people, often on a phone, sometimes badly. Every field falls back to a sane
default and every problem is reported as a warning rather than an exception —
a typo in the timetable must never stop the node from recording.
"""

from __future__ import annotations

import configparser
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

HOURS = 24

# Rates config.ini will accept. Note what this is NOT: the AudioMoth USB
# Microphone captures at 384000 Hz and nothing else, so every other value here
# is one the recorder produces by reducing that natively-captured audio.
#
# 250000 is the odd one out — the AudioMoth family advertises it, but
# 384000/250000 = 1.536, so it cannot be reached by whole-number reduction and
# the recorder keeps the native rate instead. It is accepted rather than
# rejected because rejecting it would silently substitute a rate the operator
# did not choose; the recorder logs plainly what it is doing instead.
SUPPORTED_SAMPLE_RATES = (8000, 16000, 32000, 48000, 96000, 192000, 250000, 384000)

# Matches the shipped config.ini. Only used when the key is missing or
# unreadable, and a wrong guess there costs disk on every recording, so it
# tracks the template rather than sitting at an older value.
DEFAULT_SAMPLE_RATE = 16000
DEFAULT_RECORDING_MINUTES = 5.0
DEFAULT_SENSOR_INTERVAL_S = 60.0
DEFAULT_MIN_FREE_PERCENT = 15.0
DEFAULT_MAX_LOCAL_DAYS = 7.0

# Bee colonies live acoustically below ~1 kHz: wingbeat fundamentals sit near
# 200-250 Hz, worker piping and whooping around 300-600 Hz. Analysing a wider
# band mostly measures wind and traffic, so the default window is deliberately
# narrow. Widen it if you are chasing something else.
DEFAULT_ANALYSIS_MIN_HZ = 20.0
DEFAULT_ANALYSIS_MAX_HZ = 2000.0
DEFAULT_TOP_PEAKS = 5


@dataclass
class RemoteConfig:
    """One parsed config.ini."""

    sensor_hours: list[bool] = field(default_factory=lambda: [True] * HOURS)
    microphone_hours: list[bool] = field(default_factory=lambda: [True] * HOURS)
    sample_rate: int = DEFAULT_SAMPLE_RATE
    recording_minutes: float = DEFAULT_RECORDING_MINUTES
    sensor_interval_s: float = DEFAULT_SENSOR_INTERVAL_S
    upload_interval_hours: float = 24.0
    # Which capture device to record from. 'auto' accepts only a real
    # AudioMoth; 'any' permits whatever capture hardware is present (useful for
    # bench testing, and always recorded in the data so it cannot be mistaken
    # for hive audio); or an explicit ALSA name such as 'hw:3,0'.
    input_device: str = "auto"
    # Storage policy — a full SD card stops sensors as well as recording, so
    # these are what keep the node alive unattended.
    delete_after_upload: bool = True
    min_free_percent: float = DEFAULT_MIN_FREE_PERCENT
    max_local_days: float = DEFAULT_MAX_LOCAL_DAYS
    drop_oldest_when_full: bool = True
    # Frequency analysis of each recording.
    analysis_enabled: bool = True
    analysis_hours: list[bool] = field(default_factory=lambda: [True] * HOURS)
    top_peaks: int = DEFAULT_TOP_PEAKS
    analysis_min_hz: float = DEFAULT_ANALYSIS_MIN_HZ
    analysis_max_hz: float = DEFAULT_ANALYSIS_MAX_HZ
    # dBFS is what a microphone can report without calibration. Adding a
    # measured offset turns it into an approximate SPL; see docs/ARCHITECTURE.md.
    calibration_db_offset: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def logs_sensors_at(self, hour: int) -> bool:
        return self.sensor_hours[hour % HOURS]

    def records_audio_at(self, hour: int) -> bool:
        return self.microphone_hours[hour % HOURS]

    def analyses_audio_at(self, hour: int) -> bool:
        return self.analysis_enabled and self.analysis_hours[hour % HOURS]

    @property
    def upload_frequency(self) -> str:
        """Human-readable label for the upload cadence."""
        if self.upload_interval_hours >= 24:
            return "daily"
        return f"{self.upload_interval_hours:g}h"

    @property
    def upload_interval_s(self) -> float:
        return max(0.0, self.upload_interval_hours) * 3600.0

    def summary(self) -> str:
        return (
            f"sensors {_hours_summary(self.sensor_hours)} every "
            f"{self.sensor_interval_s:g}s, "
            f"mic {_hours_summary(self.microphone_hours)} "
            f"@ {self.sample_rate} Hz x {self.recording_minutes:g}min, "
            f"upload {self.upload_frequency}"
        )


def _hours_summary(flags: list[bool]) -> str:
    on = [h for h, flag in enumerate(flags) if flag]
    if not on:
        return "off"
    if len(on) == HOURS:
        return "24h"
    # Collapse runs of consecutive hours: "05-08,17-20".
    runs: list[str] = []
    start = previous = on[0]
    for hour in on[1:]:
        if hour == previous + 1:
            previous = hour
            continue
        runs.append(f"{start:02d}" if start == previous else f"{start:02d}-{previous:02d}")
        start = previous = hour
    runs.append(f"{start:02d}" if start == previous else f"{start:02d}-{previous:02d}")
    return ",".join(runs)


def parse_hours(raw: str, *, label: str, warnings: list[str]) -> list[bool] | None:
    """Read an hour timetable in whichever form the operator typed it.

    Accepts ``1,0,1,...``, ``1 0 1 ...``, or a bare 24-character run of digits.
    Braces and brackets are tolerated because the spec was written with them.
    """
    text = raw.strip().strip("{}[]()").strip()
    if not text:
        return None

    if "," in text or " " in text:
        tokens = [t for t in text.replace(",", " ").split() if t]
    else:
        # Undelimited means the 24-digit shorthand, so it must be digits. Without
        # this check a stray word gets split into letters and any 'n' in it
        # silently reads as "no" — "banana" would parse as two off-hours.
        if not all(character in "01" for character in text):
            warnings.append(f"{label}: {text!r} is not a timetable, keeping the default")
            return None
        tokens = list(text)

    flags: list[bool] = []
    for token in tokens:
        lowered = token.lower()
        if lowered in ("1", "y", "yes", "true", "on"):
            flags.append(True)
        elif lowered in ("0", "n", "no", "false", "off"):
            flags.append(False)
        else:
            warnings.append(f"{label}: ignoring unrecognised entry {token!r}")

    if not flags:
        warnings.append(f"{label}: no usable values, keeping the default")
        return None
    if len(flags) < HOURS:
        warnings.append(
            f"{label}: only {len(flags)} of {HOURS} hours given, padding the rest with 0"
        )
        flags += [False] * (HOURS - len(flags))
    elif len(flags) > HOURS:
        warnings.append(f"{label}: {len(flags)} values given, using the first {HOURS}")
        flags = flags[:HOURS]
    return flags


def _nearest_supported_rate(value: int, warnings: list[str]) -> int:
    if value in SUPPORTED_SAMPLE_RATES:
        return value
    nearest = min(SUPPORTED_SAMPLE_RATES, key=lambda r: abs(r - value))
    warnings.append(
        f"sample_rate {value} is not one the AudioMoth supports "
        f"({', '.join(str(r) for r in SUPPORTED_SAMPLE_RATES)}) — using {nearest}"
    )
    return nearest


def parse_config_ini(text: str) -> RemoteConfig:
    """Parse a config.ini. Never raises: bad input degrades to defaults."""
    cfg = RemoteConfig()
    warnings = cfg.warnings

    parser = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    try:
        parser.read_string(text)
    except configparser.Error as exc:
        warnings.append(f"could not parse config.ini ({exc}) — using defaults")
        return cfg

    def get(section: str, *names: str) -> str | None:
        # The example section is documentation, never configuration.
        if section.lower().startswith("example"):
            return None
        for name in names:
            if parser.has_option(section, name):
                return parser.get(section, name).strip()
        return None

    for section in parser.sections():
        if section.lower().startswith("example"):
            continue

        raw_sensors = get(section, "sensors", "sensor", "sensor_hours")
        if raw_sensors is not None:
            hours = parse_hours(raw_sensors, label="sensors", warnings=warnings)
            if hours:
                cfg.sensor_hours = hours

        raw_mic = get(section, "microphone", "mic", "audio", "microphone_hours")
        if raw_mic is not None:
            hours = parse_hours(raw_mic, label="microphone", warnings=warnings)
            if hours:
                cfg.microphone_hours = hours

        raw_rate = get(section, "sample_rate", "sampling_rate", "samplerate")
        if raw_rate is not None:
            try:
                cfg.sample_rate = _nearest_supported_rate(
                    int(float(raw_rate.replace("_", "").replace("Hz", "").strip())), warnings
                )
            except ValueError:
                warnings.append(f"sample_rate {raw_rate!r} is not a number — using defaults")

        raw_minutes = get(section, "recording_minutes", "record_minutes", "duration_minutes")
        if raw_minutes is not None:
            try:
                minutes = float(raw_minutes)
                if minutes <= 0:
                    raise ValueError
                cfg.recording_minutes = minutes
            except ValueError:
                warnings.append(f"recording_minutes {raw_minutes!r} is not valid — using default")

        raw_interval = get(section, "sensor_interval_seconds", "sensor_interval", "interval")
        if raw_interval is not None:
            try:
                interval = float(raw_interval)
                if interval <= 0:
                    raise ValueError
                cfg.sensor_interval_s = interval
            except ValueError:
                warnings.append(f"sensor_interval {raw_interval!r} is not valid — using default")

        raw_freq = get(section, "frequency", "upload_frequency", "upload", "every_hours")
        if raw_freq is not None:
            cfg.upload_interval_hours = _parse_frequency(raw_freq, warnings)

        raw_device = get(section, "input_device", "device", "capture_device")
        if raw_device is not None and raw_device:
            cfg.input_device = raw_device

        raw_analysis = get(section, "analysis_enabled", "analyse", "analyze", "enabled")
        if raw_analysis is not None and section.lower() == "analysis":
            cfg.analysis_enabled = _parse_bool(raw_analysis, True, "analysis enabled", warnings)

        raw_analysis_hours = get(section, "analysis_hours", "analyse_hours")
        if raw_analysis_hours is not None:
            hours = parse_hours(raw_analysis_hours, label="analysis", warnings=warnings)
            if hours:
                cfg.analysis_hours = hours

        raw_peaks = get(section, "top_peaks", "peaks", "num_peaks")
        if raw_peaks is not None:
            try:
                peaks = int(float(raw_peaks))
                cfg.top_peaks = max(1, min(20, peaks))
                if peaks != cfg.top_peaks:
                    warnings.append(f"top_peaks {peaks} clamped to {cfg.top_peaks}")
            except ValueError:
                warnings.append(f"top_peaks {raw_peaks!r} is not a number — using default")

        for names, attr, label in (
            (("min_frequency", "min_hz", "analysis_min_hz"), "analysis_min_hz", "min_frequency"),
            (("max_frequency", "max_hz", "analysis_max_hz"), "analysis_max_hz", "max_frequency"),
        ):
            raw_band = get(section, *names)
            if raw_band is not None:
                try:
                    value = float(raw_band)
                    if value < 0:
                        raise ValueError
                    setattr(cfg, attr, value)
                except ValueError:
                    warnings.append(f"{label} {raw_band!r} is not valid — using default")

        raw_cal = get(section, "calibration_db_offset", "db_offset", "calibration_db")
        if raw_cal is not None:
            try:
                cfg.calibration_db_offset = float(raw_cal)
            except ValueError:
                warnings.append(f"calibration_db_offset {raw_cal!r} is not a number — using 0")

        raw_delete = get(section, "delete_after_upload", "delete_local")
        if raw_delete is not None:
            cfg.delete_after_upload = _parse_bool(raw_delete, True, "delete_after_upload", warnings)

        raw_free = get(section, "min_free_percent", "min_free_pct", "keep_free_percent")
        if raw_free is not None:
            cfg.min_free_percent = _parse_percent(
                raw_free, DEFAULT_MIN_FREE_PERCENT, "min_free_percent", warnings
            )

        raw_days = get(section, "max_local_days", "keep_days", "retention_days")
        if raw_days is not None:
            try:
                days = float(raw_days)
                cfg.max_local_days = days if days > 0 else 0.0
            except ValueError:
                warnings.append(f"max_local_days {raw_days!r} is not a number — using default")

        raw_drop = get(section, "drop_oldest_when_full", "drop_when_full")
        if raw_drop is not None:
            cfg.drop_oldest_when_full = _parse_bool(
                raw_drop, True, "drop_oldest_when_full", warnings
            )

    # Cross-field validation, once every section has been read.
    if cfg.analysis_min_hz >= cfg.analysis_max_hz:
        warnings.append(
            f"min_frequency {cfg.analysis_min_hz:g} is not below max_frequency "
            f"{cfg.analysis_max_hz:g} — using the defaults"
        )
        cfg.analysis_min_hz = DEFAULT_ANALYSIS_MIN_HZ
        cfg.analysis_max_hz = DEFAULT_ANALYSIS_MAX_HZ

    return cfg


def _parse_bool(raw: str, default: bool, label: str, warnings: list[str]) -> bool:
    value = raw.strip().lower()
    if value in ("1", "yes", "y", "true", "on", "enabled"):
        return True
    if value in ("0", "no", "n", "false", "off", "disabled"):
        return False
    warnings.append(f"{label} {raw!r} is not yes/no — using {'yes' if default else 'no'}")
    return default


def _parse_percent(raw: str, default: float, label: str, warnings: list[str]) -> float:
    try:
        value = float(raw.strip().rstrip("%"))
    except ValueError:
        warnings.append(f"{label} {raw!r} is not a number — using {default:g}")
        return default
    if not 0 <= value < 100:
        warnings.append(f"{label} {value:g} is out of range — using {default:g}")
        return default
    return value


def _parse_frequency(raw: str, warnings: list[str]) -> float:
    """Read an upload cadence as a number of hours.

    Accepts words ('daily', 'hourly', 'twice daily') and any 'Nh' / bare number,
    so 'every few hours' can be said as '6h', '6', or 'every 6 hours'.
    """
    value = raw.strip().lower().replace(" ", "").replace("_", "")
    words = {
        "daily": 24.0, "day": 24.0, "everyday": 24.0, "once": 24.0, "oncedaily": 24.0,
        "twicedaily": 12.0, "halfdaily": 12.0,
        "hourly": 1.0, "everyhour": 1.0,
    }
    if value in words:
        return words[value]

    # 'every6hours', '6hours', '6h', '6'
    match = re.search(r"(\d+(?:\.\d+)?)", value)
    if match and ("h" in value or value.replace(".", "").isdigit()):
        hours = float(match.group(1))
        if 0 < hours <= 168:
            return hours
        warnings.append(f"upload frequency {raw!r} is out of range — using daily")
        return 24.0

    warnings.append(f"upload frequency {raw!r} not understood — using daily")
    return 24.0


def load_config_ini(path: Path) -> RemoteConfig:
    """Read a config.ini from disk, falling back to defaults if unreadable."""
    try:
        return parse_config_ini(path.read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:
        cfg = RemoteConfig()
        cfg.warnings.append(f"could not read {path}: {exc} — using defaults")
        return cfg


DEFAULT_CONFIG_INI = """\
; ============================================================================
;  iot-node — field settings
;
;  Edit this file right here in Google Drive. The node re-reads it every 15
;  minutes, so a change here reaches the device without anyone logging into it.
;  Watch it arrive with:  journalctl -u iot-node -f
;
;  TIMETABLES: one value per hour of the day, 00:00 through 23:00, local time.
;      1 = do it during that hour        0 = skip that hour
;  Write them comma-separated, or as one run of 24 digits — both work.
; ============================================================================

[schedule]

; Hour:      0 1 2 3 4 5 6 7 8 9 . . . . . . . . . . . . 23
sensors    = 1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1
microphone = 1,0,0,1,0,0,1,0,0,1,0,0,1,0,0,1,0,0,1,0,0,1,0,0

; Above: temperature / humidity / pressure / air quality are logged around the
; clock, while the microphone records for one hour in every three — 00:00,
; 03:00, 06:00 and so on. That samples the whole daily cycle evenly instead of
; over-representing whichever hours someone happened to pick, and comes to
; eight hours of audio a day.


[audio]

; Samples per second.
;
; The AudioMoth USB Microphone captures at ONE rate only: 384000 Hz. That is
; fixed in its USB descriptor and no setting or switch position changes it —
; DEFAULT/CUSTOM controls its standalone SD-card recording, not what it offers
; over USB. Every other rate below is produced by the node, which captures at
; 384000 and reduces it, filtering out everything above the new Nyquist first
; so that ultrasound cannot fold down into the hive band.
;
; So the usable rates are 384000 divided by a WHOLE number:
;
;     rate     divisor   per 5-min file   per recording hour   Nyquist
;     ------------------------------------------------------------------
;      8000      48          4.6 MiB           54.9 MiB          4 kHz
;     16000      24          9.2 MiB          109.9 MiB          8 kHz
;     32000      12         18.3 MiB          219.7 MiB         16 kHz
;     48000       8         27.5 MiB          329.6 MiB         24 kHz
;     96000       4         54.9 MiB          659.2 MiB         48 kHz
;    192000       2        109.9 MiB            1.29 GiB        96 kHz
;    384000       1        219.7 MiB            2.58 GiB       192 kHz
;
; Multiply the hourly figure by the number of hours switched on in [schedule]
; to get the daily cost, and check it against the free space on the drive.
;
; DO NOT USE 250000. The AudioMoth family advertises it, but 384000/250000 =
; 1.536 is not a whole number, so the node cannot produce it and keeps the
; native 384000 instead — you would ask for 250 kHz and get the largest files
; possible. The journal says so, but nothing else will.
;
; Choosing: the analysis band further down is 20-2000 Hz, so anything above
; 8000 Hz of Nyquist is recorded and uploaded but never examined unless
; max_frequency is raised to match. 16000 leaves 4x headroom over that band;
; 8000 still leaves 2x and halves the storage again. Use 96000 or above only
; for ultrasound — bats, wingbeat harmonics.
sample_rate = 16000

; Length of each audio file, in minutes. Shorter files upload more reliably
; over a poor link and lose less if one is interrupted.
recording_minutes = 5


; Which microphone to record from.
;   auto      only a real AudioMoth (refuses to record from anything else)
;   any       whatever capture device is present — for bench testing only;
;             the device name is written into every analysis row so test audio
;             can never be mistaken for hive audio
;   hw:3,0    a specific ALSA device
input_device = auto


[sensors]

; How often to read temperature / humidity / pressure / air quality, in
; seconds, during an hour switched on above. 180 gives a reading every three
; minutes, 480 readings and about 70 KB of CSV per day — small enough to
; ignore beside the audio. Drop it to 10 for fine-grained work, raise it to
; 600 to save power on batteries.
sensor_interval_seconds = 180


[upload]

; How often the day's data is pushed to Google Drive. Accepts 'daily',
; 'hourly', or any number of hours: '6h', '12h', '3h'.
; Recordings are kept locally until they have been uploaded, so a longer
; interval costs disk space. It costs DATA too if the card then fills and
; drop_oldest_when_full below is 'yes' — the two settings work together, so
; read them as a pair.
frequency = 6h


[analysis]

; Frequency analysis of each recording. Produces one row per file with the
; loudest spectral peaks and an overall level, written to a daily CSV that is
; uploaded alongside the audio.
enabled = yes

; Which hours to analyse. Usually the same as the microphone timetable; set it
; narrower to save CPU on a busy node.
analysis_hours = 111111111111111111111111

; How many spectral peaks to report per recording.
top_peaks = 5

; Band to search, in Hz. A honeybee colony lives below ~1 kHz — wingbeats near
; 200-250 Hz, worker piping around 300-600 Hz — so looking wider mostly
; measures wind and traffic. Widen it if you are chasing something else.
min_frequency = 20
max_frequency = 2000

; A plain microphone cannot report absolute loudness, so levels are recorded as
; dBFS (relative to digital full scale, always negative). To turn that into an
; approximate dB SPL, put a calibrated sound meter beside the microphone, note
; the real reading and the reported dBFS at the same moment, and set the
; difference here. Leave it at 0 and treat the SPL column as uncalibrated —
; still perfectly good for comparing one recording with another.
calibration_db_offset = 0


[storage]

; Keeping the SD card from filling matters more than it sounds: a full card
; stops the sensors too, not just recording.

; Delete the local copy once a file is safely uploaded and size-checked.
delete_after_upload = yes

; Always keep at least this much of the card free.
min_free_percent = 15

; Never keep local data older than this, even if it uploaded fine.
max_local_days = 7

; If free space still falls below min_free_percent after deleting everything
; already uploaded, throw away the OLDEST recordings that have NOT been
; uploaded yet, to keep the node running. This loses data, so set it to 'no'
; if you would rather the node stop recording than drop anything. Sensor CSVs
; are never dropped — they are a few KB a day.
drop_oldest_when_full = yes


; ============================================================================
;  EXAMPLE — this whole section is ignored by the node. Copy the lines you
;  want up into the real sections above.
; ============================================================================

[example]

; --- Round-the-clock hive sampling (this is what is running now) -----------
; One hour of recording every three hours, so every part of the daily cycle
; gets sampled evenly — day and night foraging, and the quiet hours between.
; Eight hours of audio a day.
;
;   sensors    = 111111111111111111111111
;   microphone = 100100100100100100100100
;   sensor_interval_seconds = 180
;   sample_rate = 48000
;   recording_minutes = 5
;   frequency = 6h

; --- Swarm watch, daytime intensive ----------------------------------------
; Swarming is a daytime event preceded by hours of piping, so listen densely
; through the warm part of the day and analyse everything.
;
;   microphone = 000000011111111111000000
;   recording_minutes = 10
;   frequency = 3h
;   max_frequency = 1000

; --- Dawn and dusk chorus survey -------------------------------------------
; Microphone on for the hours either side of sunrise and sunset.
;
;   microphone = 0,0,0,0,1,1,1,0,0,0,0,0,0,0,0,0,0,1,1,1,1,0,0,0
;   sample_rate = 48000
;   frequency = daily

; --- Bat survey, night only ------------------------------------------------
; Ultrasonic, so a high sample rate and a much wider analysis band. Note this
; fills storage roughly four times faster than the 48 kHz examples.
;
;   microphone = 1,1,1,1,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,1,1,1,1
;   sample_rate = 192000
;   recording_minutes = 2
;   max_frequency = 96000

; --- Sensors only, microphone off ------------------------------------------
;   sensors    = 111111111111111111111111
;   microphone = 000000000000000000000000
"""
