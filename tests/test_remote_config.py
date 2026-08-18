from __future__ import annotations

from iot_node.remote_config import (
    DEFAULT_CONFIG_INI,
    HOURS,
    RemoteConfig,
    parse_config_ini,
    parse_hours,
)


def hours_on(flags: list[bool]) -> list[int]:
    return [h for h, flag in enumerate(flags) if flag]


# --- timetable parsing -------------------------------------------------------


def test_comma_separated_timetable():
    flags = parse_hours("1,0,1,0" + ",0" * 20, label="t", warnings=[])
    assert flags is not None
    assert hours_on(flags) == [0, 2]


def test_twenty_four_digit_shorthand():
    flags = parse_hours("000001111000000000111000", label="t", warnings=[])
    assert hours_on(flags) == [5, 6, 7, 8, 18, 19, 20]


def test_space_separated_and_braces_are_tolerated():
    """The spec was written with braces, so accept them."""
    raw = "{" + " ".join(["1"] * 24) + "}"
    flags = parse_hours(raw, label="t", warnings=[])
    assert flags == [True] * HOURS


def test_short_timetable_is_padded_with_a_warning():
    warnings: list[str] = []
    flags = parse_hours("1,1,1", label="sensors", warnings=warnings)

    assert flags is not None and len(flags) == HOURS
    assert hours_on(flags) == [0, 1, 2]
    assert any("only 3 of 24" in w for w in warnings)


def test_overlong_timetable_is_truncated_with_a_warning():
    warnings: list[str] = []
    flags = parse_hours(",".join(["1"] * 30), label="mic", warnings=warnings)

    assert flags is not None and len(flags) == HOURS
    assert any("using the first 24" in w for w in warnings)


def test_junk_entries_are_skipped_not_fatal():
    warnings: list[str] = []
    flags = parse_hours("1,x,1" + ",0" * 21, label="t", warnings=warnings)

    assert flags is not None
    assert hours_on(flags) == [0, 1]  # 'x' dropped, the rest shift down
    assert any("'x'" in w for w in warnings)


def test_yes_no_words_work_too():
    flags = parse_hours("yes,no," + ",".join(["off"] * 22), label="t", warnings=[])
    assert hours_on(flags) == [0]


# --- whole files -------------------------------------------------------------


def test_the_shipped_default_parses_cleanly():
    cfg = parse_config_ini(DEFAULT_CONFIG_INI)

    assert cfg.warnings == []
    assert cfg.sensor_hours == [True] * HOURS
    # One hour of recording in every three, sampling the whole daily cycle.
    assert hours_on(cfg.microphone_hours) == [0, 3, 6, 9, 12, 15, 18, 21]
    assert cfg.sample_rate == 16000
    assert cfg.recording_minutes == 5
    assert cfg.sensor_interval_s == 180
    assert cfg.upload_frequency == "6h"
    assert cfg.upload_interval_hours == 6
    assert cfg.analysis_enabled is True
    assert cfg.top_peaks == 5
    assert (cfg.analysis_min_hz, cfg.analysis_max_hz) == (20, 2000)
    assert cfg.calibration_db_offset == 0
    assert cfg.input_device == "auto"
    assert cfg.delete_after_upload is True
    assert cfg.min_free_percent == 15
    assert cfg.max_local_days == 7


def test_the_example_section_is_documentation_not_configuration():
    """Values under [example] must never leak into the live settings."""
    text = """
[schedule]
microphone = 111111111111111111111111

[example]
microphone = 000000000000000000000000
sample_rate = 384000
frequency = 12h
"""
    cfg = parse_config_ini(text)

    assert cfg.microphone_hours == [True] * HOURS
    assert cfg.sample_rate == 16000  # the default, not the example's 384000
    assert cfg.upload_frequency == "daily"


def test_upload_frequency_spellings():
    for raw in ("daily", "Daily", "24h", "every day"):
        assert parse_config_ini(f"[upload]\nfrequency = {raw}").upload_frequency == "daily"
    for raw in ("12h", "12 hours", "twice daily", "every_12_hours"):
        assert parse_config_ini(f"[upload]\nfrequency = {raw}").upload_frequency == "12h"


def test_arbitrary_hourly_cadences_are_understood():
    """'every few hours' has to be sayable several ways."""
    for raw, hours in [("6h", 6), ("6", 6), ("every 6 hours", 6), ("3h", 3), ("hourly", 1)]:
        cfg = parse_config_ini(f"[upload]\nfrequency = {raw}")
        assert cfg.upload_interval_hours == hours, raw
        assert cfg.warnings == [], raw


def test_unknown_frequency_falls_back_and_warns():
    cfg = parse_config_ini("[upload]\nfrequency = whenever")
    assert cfg.upload_frequency == "daily"
    assert any("whenever" in w for w in cfg.warnings)


def test_out_of_range_frequency_falls_back():
    cfg = parse_config_ini("[upload]\nfrequency = 900h")
    assert cfg.upload_interval_hours == 24
    assert cfg.warnings


def test_unsupported_sample_rate_snaps_to_the_nearest_supported():
    cfg = parse_config_ini("[audio]\nsample_rate = 44100")
    assert cfg.sample_rate == 48000
    assert any("44100" in w for w in cfg.warnings)


def test_every_audiomoth_rate_is_accepted():
    for rate in (8000, 16000, 32000, 48000, 96000, 192000, 250000, 384000):
        cfg = parse_config_ini(f"[audio]\nsample_rate = {rate}")
        assert cfg.sample_rate == rate
        assert cfg.warnings == []


def test_sensor_interval_is_read():
    cfg = parse_config_ini("[sensors]\nsensor_interval_seconds = 10")
    assert cfg.sensor_interval_s == 10


def test_storage_policy_is_read():
    cfg = parse_config_ini("""
[storage]
delete_after_upload = no
min_free_percent = 25
max_local_days = 2
drop_oldest_when_full = no
""")
    assert cfg.delete_after_upload is False
    assert cfg.min_free_percent == 25
    assert cfg.max_local_days == 2
    assert cfg.drop_oldest_when_full is False


def test_nonsense_values_never_raise():
    """A phone-edited file must degrade, not crash the node."""
    cfg = parse_config_ini("""
[schedule]
sensors = banana
[audio]
sample_rate = fast
recording_minutes = -3
[storage]
min_free_percent = 900
""")
    assert cfg.sensor_hours == [True] * HOURS  # untouched default
    assert cfg.sample_rate == 16000
    assert cfg.recording_minutes == 5
    assert cfg.min_free_percent == 15
    assert len(cfg.warnings) >= 3


def test_a_completely_broken_file_falls_back_to_defaults():
    cfg = parse_config_ini("this is not ini at all ][")
    assert cfg.sensor_hours == [True] * HOURS
    assert cfg.warnings


# --- behaviour ---------------------------------------------------------------


def test_schedule_lookups():
    cfg = parse_config_ini("[schedule]\nmicrophone = 000001111000000000111000")
    assert not cfg.records_audio_at(4)
    assert cfg.records_audio_at(5)
    assert cfg.records_audio_at(20)
    assert not cfg.records_audio_at(21)
    assert cfg.records_audio_at(29)  # wraps, so an out-of-range hour is safe


def test_upload_interval_matches_the_frequency():
    assert RemoteConfig(upload_interval_hours=24).upload_interval_s == 86400
    assert RemoteConfig(upload_interval_hours=12).upload_interval_s == 43200
    assert RemoteConfig(upload_interval_hours=6).upload_frequency == "6h"
    assert RemoteConfig(upload_interval_hours=24).upload_frequency == "daily"


def test_summary_collapses_runs_of_hours():
    cfg = parse_config_ini("[schedule]\nmicrophone = 000001111000000000111000")
    assert "05-08" in cfg.summary()
    assert "18-20" in cfg.summary()
    assert "sensors 24h" in cfg.summary()
