from __future__ import annotations

import json

import pytest
import requests

from iot_node.cloud.flask_forward import FlaskForwarder
from iot_node.cloud.uploader import Uploader
from iot_node.state import Store


class FakeResponse:
    def __init__(self, status: int, body: dict | None = None) -> None:
        self.status_code = status
        self._body = body if body is not None else {}
        self.text = json.dumps(self._body)

    def json(self) -> dict:
        return self._body


class FakeSession:
    """Answers each request with a handler's response and records the request."""

    def __init__(self, handler=None) -> None:
        self.handler = handler or (lambda method, url, kwargs: FakeResponse(201))
        self.calls: list[dict] = []

    def request(self, method, url, headers=None, **kwargs):
        if "data" in kwargs and hasattr(kwargs["data"], "read"):
            kwargs["data"] = kwargs["data"].read()
        call = {"method": method, "url": url, "headers": headers or {}, **kwargs}
        self.calls.append(call)
        return self.handler(method, url, call)

    def close(self) -> None:
        pass


@pytest.fixture
def flask_cfg(cfg):
    cfg.flask.url = "http://pc:5000/"
    cfg.flask.api_key = "hive-key"
    cfg.device.timezone = "Pacific/Auckland"
    return cfg


def make(cfg, handler=None) -> tuple[FlaskForwarder, FakeSession]:
    session = FakeSession(handler)
    return FlaskForwarder(cfg, session=session), session


def record(ts: str, **values) -> dict:
    return {"ts": ts, "device_id": "test-node", "site": "", **values}


def write_sensor_csv(path, rows: list[tuple[str, str]]) -> None:
    lines = ["timestamp_utc,timestamp_local,device_id,site,temperature_c,humidity_pct,"
             "pressure_hpa,voc_raw,voc_index,errors"]
    lines += [f"{ts},,test-node,,{temp},50.0,1021.5,30000,100," for ts, temp in rows]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


# --- readings ----------------------------------------------------------------


def test_live_reading_is_posted_with_key_and_numbers(flask_cfg):
    forwarder, session = make(flask_cfg)
    forwarder.enqueue_reading(record("2026-10-25T00:00:00Z", temperature_c=20.9,
                                     humidity_pct=52.0, voc_index=206))
    sent = forwarder.run_once()

    assert sent["reading"] == 1
    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "http://pc:5000/api/v1/hives/1/readings"
    assert call["headers"]["X-API-Key"] == "hive-key"
    assert call["json"] == {"readings": [{
        "recorded_at": "2026-10-25T00:00:00Z", "temperature_c": 20.9, "humidity_pct": 52.0,
        "voc_index": 206, "device_id": "test-node",
    }]}
    assert forwarder.counts()["reading"] == {"sent": 1}


def test_a_sample_with_no_values_is_not_sent(flask_cfg):
    forwarder, _ = make(flask_cfg)
    assert not forwarder.enqueue_reading(record("2026-10-25T00:00:00Z", bme280_error="nack"))


def test_backfill_does_not_resend_a_reading_already_sent_live(flask_cfg):
    forwarder, session = make(flask_cfg)
    forwarder.enqueue_reading(record("2026-10-25T00:00:00Z", temperature_c=20.0))
    forwarder.run_once()

    write_sensor_csv(
        flask_cfg.resolve("var/current") / "sensors_test-node_2026-10-25.csv",
        [("2026-10-25T00:00:00Z", "20.0"), ("2026-10-25T00:01:00Z", "21.0")],
    )
    forwarder.run_once()

    second = session.calls[1]["json"]["readings"]
    assert [r["recorded_at"] for r in second] == ["2026-10-25T00:01:00Z"]
    assert second[0]["voc_raw"] == 30000 and isinstance(second[0]["voc_raw"], int)


def test_unchanged_csv_is_not_reread(flask_cfg):
    forwarder, _ = make(flask_cfg)
    path = flask_cfg.spool_dir / "sensors" / "sensors_test-node_2026-10-24.csv"
    write_sensor_csv(path, [("2026-10-24T00:00:00Z", "20.0")])
    assert forwarder.scan_backfill() == 1
    assert forwarder.scan_backfill() == 0


def test_one_bad_reading_does_not_block_the_batch(flask_cfg):
    def handler(method, url, call):
        bad = any(r["temperature_c"] > 85 for r in call["json"]["readings"])
        return FakeResponse(400, {"error": "out of range"}) if bad else FakeResponse(201)

    forwarder, _ = make(flask_cfg, handler)
    for minute, temp in enumerate([20.0, 500.0, 21.0]):
        forwarder.enqueue_reading(record(f"2026-10-25T00:0{minute}:00Z", temperature_c=temp))
    sent = forwarder.run_once()

    assert sent["reading"] == 2
    assert forwarder.counts()["reading"] == {"sent": 2, "rejected": 1}


def test_unreachable_server_keeps_everything_queued(flask_cfg):
    def handler(method, url, call):
        raise requests.ConnectionError("no route")

    forwarder, _ = make(flask_cfg, handler)
    forwarder.enqueue_reading(record("2026-10-25T00:00:00Z", temperature_c=20.0))
    forwarder.run_once()

    assert forwarder.online is False
    assert forwarder.counts()["reading"] == {"pending": 1}


def test_refused_key_stops_the_pass(flask_cfg):
    forwarder, session = make(flask_cfg, lambda m, u, c: FakeResponse(401))
    forwarder.enqueue_reading(record("2026-10-25T00:00:00Z", temperature_c=20.0))
    forwarder.enqueue_analysis({"filename": "20261025_125215.WAV", "rms_dbfs": "-36.1"})
    forwarder.run_once()

    assert len(session.calls) == 1  # nothing else tried with a key that is wrong
    assert forwarder.counts()["reading"] == {"pending": 1}


# --- analysis and audio ------------------------------------------------------


def test_analysis_row_becomes_a_typed_result(flask_cfg):
    forwarder, session = make(flask_cfg)
    forwarder.enqueue_analysis({
        "filename": "20261025_125215.WAV", "analysed_at_utc": "2026-10-24T23:58:33Z",
        "sample_rate_hz": "16000", "duration_s": "300.0", "rms_dbfs": "-36.1",
        "dominant_hz": "199.9", "band_hz": "20-2000", "device": "AudioMoth (hw:2,0)",
        "peak1_hz": "199.9", "peak1_db": "-51.3", "peak2_hz": "", "peak2_db": "",
    })
    forwarder.run_once()

    call = next(c for c in session.calls if c["url"].endswith("/audio-analysis"))
    result = call["json"]["results"][0]
    # 12:52:15 NZDT is 23:52:15 UTC the day before.
    assert result["recorded_at"] == "2026-10-24T23:52:15Z"
    assert result["sample_rate_hz"] == 16000
    assert result["rms_dbfs"] == -36.1
    assert result["peaks"] == [{"hz": 199.9, "db": -51.3}]
    assert result["audio_device"] == "AudioMoth (hw:2,0)"


def test_missing_endpoint_skips_that_kind_only(flask_cfg):
    def handler(method, url, call):
        return FakeResponse(404) if url.endswith("/audio-analysis") else FakeResponse(201)

    forwarder, _ = make(flask_cfg, handler)
    forwarder.enqueue_analysis({"filename": "20261025_125215.WAV", "rms_dbfs": "-36.1"})
    forwarder.enqueue_reading(record("2026-10-25T00:00:00Z", temperature_c=20.0))
    sent = forwarder.run_once()

    assert sent["reading"] == 1
    assert forwarder.counts()["analysis"] == {"pending": 1}


def test_recording_is_put_once(flask_cfg):
    wav = flask_cfg.spool_dir / "audio" / "20261025_125215.WAV"
    wav.write_bytes(b"RIFF....WAVE")
    (flask_cfg.spool_dir / "audio" / "20261025_130000.WAV.part").write_bytes(b"x")

    forwarder, session = make(flask_cfg)
    forwarder.run_once()
    forwarder.run_once()

    puts = [c for c in session.calls if c["method"] == "PUT"]
    assert len(puts) == 1
    assert puts[0]["url"] == "http://pc:5000/api/v1/hives/1/recordings/20261025_125215.WAV"
    assert puts[0]["data"] == b"RIFF....WAVE"
    assert puts[0]["headers"]["X-Recorded-At"] == "2026-10-24T23:52:15Z"
    assert puts[0]["headers"]["Content-Type"] == "audio/wav"


def test_drive_housekeeping_keeps_audio_flask_has_not_had(flask_cfg):
    wav = flask_cfg.spool_dir / "audio" / "20261025_125215.WAV"
    wav.write_bytes(b"RIFF....WAVE")
    forwarder, _ = make(flask_cfg, lambda m, u, c: FakeResponse(503))

    with Store(flask_cfg.state_db) as store:
        store.enqueue(wav, "audio", wav.stat().st_size)
        store.mark_uploaded(wav, "IoT/x.WAV")
        uploader = Uploader(flask_cfg, store, backend=object(), hold=forwarder.holds)

        uploader.housekeep()
        assert wav.exists()  # on Drive, but not yet on the Flask server

        forwarder.session.handler = lambda m, u, c: FakeResponse(201)
        forwarder.run_once()
        uploader.housekeep()
        assert not wav.exists()
