# Bee Node → Queen Bee Watch (Flask)

How the Pi sends its data to the Flask server on the Windows PC, and what the
server needs for that to work. Hand this file to whoever (or whichever Claude)
is working on the Flask side; it is the whole contract.

```
 Raspberry Pi (bee-node)                         Windows PC (Queen Bee Watch, run.py :5000)
 ───────────────────────                         ──────────────────────────────────────────
 sample every 60 s ──┐                           POST /api/v1/hives/1/readings        exists
 analyse recording ──┼─► outbox (SQLite) ──────► POST /api/v1/hives/1/audio-analysis  ADD
 CSVs on the card ───┘   drained every 30 s      PUT  /api/v1/hives/1/recordings/<f>  ADD
 spool/audio/*.WAV ─────────────────────────────►
```

Drive uploads carry on exactly as before. Flask is a second destination.

## What the Pi does

* **Live readings.** Each sensor sample is queued as soon as it is taken.
* **Backfill.** Every pass, the Pi also reads the sensor and analysis CSVs
  still on the card (`var/current/`, `var/spool/sensors/`) and queues any rows
  the server has not had. Days already uploaded to Drive and deleted from the
  card are not recovered.
* **Audio.** Every finished WAV in `var/spool/audio/` is uploaded once. The Drive
  housekeeping will not delete a recording until Flask has it. The one
  exception: when the card drops below its free-space floor, keeping the node
  alive wins.
* **Offline.** If the PC is off or unreachable, everything waits in
  `var/state/flask_outbox.db` and is sent when it comes back, oldest first,
  stamped with when it was measured.
* **Ordering.** Readings always go before audio, so a slow 10 MB upload never
  delays the live numbers.

## The three endpoints

All need the header `X-API-Key: <DEFAULT_HIVE_API_KEY>`. Times are UTC ISO 8601
with a `Z`.

### 1. `POST /api/v1/hives/<id>/readings`: already exists

The Pi sends batches of up to 500:

```json
{"readings": [
  {"recorded_at": "2026-10-24T23:52:08Z", "temperature_c": 20.89, "humidity_pct": 52.25,
   "pressure_hpa": 1021.77, "voc_raw": 30247, "voc_index": 0, "device_id": "pi5-node-01"}
]}
```

`pressure_hpa`, `voc_raw`, `voc_index`, `device_id` (and `errors`, when a sensor
failed) land in `extra`, which the server already supports.

**Needs one change:** the server must skip a reading it already has for that
hive and `recorded_at`. If the Wi-Fi drops after the server saves a batch
but before the Pi hears back, the Pi sends the batch again. At the moment
that would store every reading twice.

If one reading in a batch is refused with 400 (for example a sensor glitch out of
range), the Pi re-sends that batch one reading at a time and drops only the bad
one.

### 2. `POST /api/v1/hives/<id>/audio-analysis`: to add

One row per recording, batches of up to 500, deduplicated by `filename`:

```json
{"results": [
  {"filename": "20261025_125215.WAV",
   "recorded_at": "2026-10-24T23:52:15Z", "analysed_at": "2026-10-24T23:58:33Z",
   "device_id": "pi5-node-01",
   "sample_rate_hz": 16000, "duration_s": 300.0,
   "rms_dbfs": -36.1, "peak_dbfs": 0.0, "spl_estimate_db": -36.1, "calibration_db_offset": 0.0,
   "dominant_hz": 199.9, "band_hz": "20-2000",
   "audio_device": "384kHz AudioMoth USB Microphone (hw:2,0)",
   "peaks": [{"hz": 199.9, "db": -51.3}, {"hz": 100.0, "db": -55.7}]}
]}
```

Answer `201` with `{"saved": n, "duplicates": m}`.

### 3. `PUT /api/v1/hives/<id>/recordings/<filename>`: to add

The request body is the raw WAV file. It is not multipart, so the server can
stream it to disk without holding it in memory.

```
PUT /api/v1/hives/1/recordings/20261025_125215.WAV
X-API-Key: ...
Content-Type: audio/wav
Content-Length: 9600044
X-Recorded-At: 2026-10-24T23:52:15Z
```

Answer `201` when saved, or `200` (or `409`) when that file is already there at
that size. A 5-minute 16 kHz recording is about 9.2 MB.

### How the Pi reads the answer

| Status | Pi does |
| --- | --- |
| 2xx | marks sent |
| 400, 413, 422 | marks that row/file rejected and moves on |
| 404 | logs once that the endpoint is missing; keeps that kind queued, still sends the others |
| 401, 403 | stops the pass: the API key is wrong |
| 5xx, timeout, no connection | stops the pass, retries in 30 s |

So the Pi also works against an older server: readings flow, and analysis and
audio wait on the Pi until the endpoints exist.

## The Flask side

This is in SensorData `main` since pull request #1
(github.com/SabaMustafa/SensorData, merge commit `81e4f24`). It touches:

| File | Change |
| --- | --- |
| `config.py` | `AUDIO_DIR` (default `instance/audio`) and `MAX_AUDIO_MB` (default 500); `TestConfig.AUDIO_DIR` |
| `app/models.py` | new tables `AudioRecording` and `AudioAnalysis`, each unique on (hive, filename) |
| `app/api/routes.py` | `require_api_key()`; duplicate skip in `add_readings`; the endpoints below |
| `tests/test_audio_api.py` | new tests |

New routes:

| Method | Endpoint | Purpose |
| --- | --- | --- |
| POST | `/api/v1/hives/<id>/audio-analysis` | Pi sends analysis (key) |
| GET | `/api/v1/hives/<id>/audio-analysis?hours=24` | for charts |
| PUT | `/api/v1/hives/<id>/recordings/<filename>` | Pi uploads a WAV (key) |
| GET | `/api/v1/hives/<id>/recordings?hours=24` | list |
| GET | `/api/v1/hives/<id>/recordings/<filename>` | download / play in the browser |

The new tables are created automatically on the next start (`db.create_all()`),
and existing data is untouched. The readings change needs no database
migration.

Showing audio on the dashboard (an `<audio>` player, a dB/frequency chart) is
not there yet. The GET routes above are what such a page would read.

## Turning it on

**The network.** AUTwifi stops devices on it from reaching each other, so the Pi
cannot reach the laptop by its Wi-Fi address. Both are on Tailscale instead
(account SabaMustafa@github): the Pi is `raspberrypi`, 100.93.65.11, and the
laptop is `saba`, 100.69.88.45. These addresses do not change, on campus or off.
Key expiry is disabled for the Pi, so it never needs signing in again.

**On the laptop**

1. `git pull` on `main` in SensorData, and run `python run.py`.
2. Allow Python through the Windows firewall on port 5000.
3. Note `DEFAULT_HIVE_API_KEY` from the laptop's `.env`.

From the Pi, `curl http://100.69.88.45:5000/api/v1/health` should answer
`"status":"ok"`.

**On the Pi**, in the node's `.env`:

```
FLASK_URL=http://100.69.88.45:5000
FLASK_API_KEY=<the same DEFAULT_HIVE_API_KEY>
```

Then:

```
iot-node forward        # one pass now, prints what was sent and what is queued
iot-node status         # the flask: lines show the queue at any time
sudo systemctl restart iot-node
```

Other settings (hive id, interval, audio on/off, backfill on/off) are in the
`flask:` block of `config.yaml`.

## Known limits

* Readings are deduplicated on `recorded_at` per hive. Two different sensors on
  the same hive reporting the same second would collide. That is not the case
  today: one Pi, one sample per minute.
* `recorded_at` for a recording comes from its filename, read in the node's
  timezone (`device.timezone`). Files the node recorded itself are named in local
  time, so this is right. Files copied off an AudioMoth card may be named in UTC
  and would then be off by the timezone offset.
* The Pi's `pi/sensor_sender.py` in the Flask repo is a separate, simpler sender.
  Don't run it alongside bee-node on the same hive, or every reading arrives
  twice from two sources.
