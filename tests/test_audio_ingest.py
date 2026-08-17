from __future__ import annotations

import os
import time

from iot_node.audio.audiomoth import AudioIngest, find_audiomoth_mounts, parse_audiomoth_config
from iot_node.state import Store


def make_recording(directory, name: str, *, size: int = 4096, age_s: float = 120.0):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"\0" * size)
    old = time.time() - age_s
    os.utime(path, (old, old))
    return path


def ingest_for(cfg, source_dir):
    cfg.audio.source_dirs = [source_dir]
    cfg.audio.auto_detect = False
    return AudioIngest(cfg, Store(cfg.state_db))


def test_stages_a_finished_recording(cfg, tmp_path):
    source = tmp_path / "cards"
    make_recording(source, "20260814_155418.WAV")
    ingest = ingest_for(cfg, source)

    staged = ingest.scan()

    assert [p.name for p in staged] == ["20260814_155418.WAV"]
    assert (cfg.spool_dir / "audio" / "20260814_155418.WAV").is_file()


def test_does_not_stage_the_same_file_twice(cfg, tmp_path):
    source = tmp_path / "cards"
    make_recording(source, "20260814_155418.WAV")
    ingest = ingest_for(cfg, source)

    assert len(ingest.scan()) == 1
    assert ingest.scan() == []  # copy leaves the original in place


def test_skips_a_recording_that_is_still_being_written(cfg, tmp_path):
    source = tmp_path / "cards"
    make_recording(source, "20260814_162000.WAV", age_s=0.0)
    ingest = ingest_for(cfg, source)

    assert ingest.scan() == []


def test_skips_files_below_the_size_floor(cfg, tmp_path):
    source = tmp_path / "cards"
    make_recording(source, "truncated.WAV", size=10)
    ingest = ingest_for(cfg, source)

    assert ingest.scan() == []


def test_move_action_clears_the_source(cfg, tmp_path):
    source = tmp_path / "cards"
    path = make_recording(source, "20260814_155418.WAV")
    cfg.audio.action = "move"
    ingest = ingest_for(cfg, source)

    ingest.scan()

    assert not path.exists()
    assert (cfg.spool_dir / "audio" / "20260814_155418.WAV").is_file()


def test_identical_names_from_two_cards_do_not_collide(cfg, tmp_path):
    card_a, card_b = tmp_path / "a", tmp_path / "b"
    make_recording(card_a, "20260814_155418.WAV", size=4096)
    make_recording(card_b, "20260814_155418.WAV", size=8192)

    cfg.audio.source_dirs = [card_a, card_b]
    cfg.audio.auto_detect = False
    ingest = AudioIngest(cfg, Store(cfg.state_db))

    staged = ingest.scan()

    assert len(staged) == 2
    assert {p.name for p in staged} == {"20260814_155418.WAV", "20260814_155418_1.WAV"}


def test_uploads_are_queued_for_staged_files(cfg, tmp_path):
    source = tmp_path / "cards"
    make_recording(source, "20260814_155418.WAV")
    store = Store(cfg.state_db)
    cfg.audio.source_dirs = [source]
    cfg.audio.auto_detect = False

    AudioIngest(cfg, store).scan()

    pending = store.pending()
    assert len(pending) == 1
    assert pending[0]["kind"] == "audio"


def test_partial_files_are_not_left_behind(cfg, tmp_path):
    source = tmp_path / "cards"
    make_recording(source, "20260814_155418.WAV")
    ingest_for(cfg, source).scan()

    leftovers = list((cfg.spool_dir / "audio").glob("*.part"))
    assert leftovers == []


# --- AudioMoth card detection ------------------------------------------------


def test_finds_a_card_by_its_config_file(tmp_path):
    volume = tmp_path / "media" / "saba" / "AUDIOMOTH"
    volume.mkdir(parents=True)
    (volume / "CONFIG.TXT").write_text("Device ID : 249BC5045C7A9B12\n")

    assert find_audiomoth_mounts((tmp_path / "media",)) == [volume]


def test_ignores_volumes_without_a_config_file(tmp_path):
    (tmp_path / "media" / "USBSTICK").mkdir(parents=True)
    assert find_audiomoth_mounts((tmp_path / "media",)) == []


def test_parses_the_audiomoth_config(tmp_path):
    volume = tmp_path / "AUDIOMOTH"
    volume.mkdir()
    (volume / "CONFIG.TXT").write_text(
        "Device ID : 249BC5045C7A9B12\n"
        "Firmware : AudioMoth-Firmware-Basic (1.10.0)\n"
        "Sample rate (Hz) : 48000\n"
        "Recording duration (s) : 300\n"
    )

    settings = parse_audiomoth_config(volume)

    assert settings["Device ID"] == "249BC5045C7A9B12"
    assert settings["Sample rate (Hz)"] == "48000"
    assert settings["Recording duration (s)"] == "300"


def test_missing_config_file_gives_an_empty_dict(tmp_path):
    assert parse_audiomoth_config(tmp_path) == {}
