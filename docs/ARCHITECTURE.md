# Bee Node — how the whole thing works

A Raspberry Pi 5 sitting beside a beehive. It measures the environment, records
the colony, works out what it sounds like, shows all that on a screen, and ships
everything to Google Drive. It is meant to be left alone for months, on a link
that comes and goes, and to still be running when you come back.

This document is the map. Read it top to bottom and you should be able to find,
change or fix any part of the system.

---

## 1. The shape of it

```
                       ┌────────────────────────────────────────┐
                       │        Google Drive  (IoT/)            │
                       │                                        │
                       │   config.ini      <── you edit this    │
                       │   master_log.csv  ──> what it did      │
                       │   pi5-node-01/<date>/                  │
                       │       sensors_*.csv                    │
                       │       audio_analysis_*.csv             │
                       │       audio/*.WAV                      │
                       └───────▲──────────────────────┬─────────┘
                    uploads    │                      │  settings
                               │                      ▼
┌──────────────────────────────┴──────────────────────────────────────┐
│  iot-node.service          (systemd, starts at boot, watchdogged)   │
│                                                                     │
│   ┌────────────┐   ┌───────────┐   ┌──────────┐   ┌──────────────┐  │
│   │  sensors   │   │ recorder  │   │ analysis │   │ config sync  │  │
│   │ BME280     │   │ arecord   │──>│   FFT    │   │ fetch/cache/ │  │
│   │ SGP40      │   │ AudioMoth │   │ peaks+dB │   │ backup       │  │
│   └─────┬──────┘   └─────┬─────┘   └────┬─────┘   └──────┬───────┘  │
│         │                │              │                │          │
│         ▼                ▼              ▼                ▼          │
│   ┌───────────────────────────────────────────────────────────┐     │
│   │                     var/spool/                            │     │
│   │        sensors/*.csv          audio/*.WAV                 │     │
│   └───────────────────────────┬───────────────────────────────┘     │
│                               │                                     │
│         ┌─────────────────────▼──────────────────┐                  │
│         │   uploader  ──>  rclone  ──> Drive     │                  │
│         │   then housekeeping purges what landed │                  │
│         └────────────────────────────────────────┘                  │
│                                                                     │
│   state: var/state/iot-node.db   status.json   master_log.csv       │
└───────────────────────────────┬─────────────────────────────────────┘
                                │ status.json (one-way, atomic)
                                ▼
              ┌───────────────────────────────────┐
              │  bee-node-screen.service          │
              │  the fullscreen BEE NODE display  │
              └───────────────────────────────────┘
```

Two processes. The **node** does all the work; the **screen** only reads a JSON
file and draws it. They share no code path, so the screen can crash, be killed,
or never start, and the node will not notice.

---

## 2. Every file, and what it is for

### The daemon

| File | Responsibility |
| --- | --- |
| [`main.py`](../src/iot_node/main.py) | The `Node` class, the scheduling loop, and every CLI subcommand. This is the entry point and the place to start reading. |
| [`config.py`](../src/iot_node/config.py) | `config.yaml` — deployment plumbing (paths, credentials, remote name). Typed with pydantic. Rarely changes. |
| [`remote_config.py`](../src/iot_node/remote_config.py) | `config.ini` — the field settings (timetables, rates, analysis, storage policy). Also holds the shipped default file. Deliberately forgiving: bad input warns, never raises. |
| [`state.py`](../src/iot_node/state.py) | SQLite ledger of what has been ingested and uploaded. Stops duplicate uploads across restarts. |
| [`csv_log.py`](../src/iot_node/csv_log.py) | The three CSV writers: daily sensor readings, daily analysis results, and the master log. |
| [`status.py`](../src/iot_node/status.py) | The `status.json` snapshot: written by the node, read by the screen. |
| [`health.py`](../src/iot_node/health.py) | systemd watchdog and per-task liveness. |
| [`analysis.py`](../src/iot_node/analysis.py) | FFT of a recording: top peaks, levels, dominant frequency. |
| [`dashboard.py`](../src/iot_node/dashboard.py) | The BEE NODE screen. Imports only `status.py` and `config.py`. |
| [`logging_setup.py`](../src/iot_node/logging_setup.py) | Rotating log file plus journald-aware console output. |

### Sensors

| File | Responsibility |
| --- | --- |
| [`sensors/base.py`](../src/iot_node/sensors/base.py) | `Sensor` protocol, `Reading`, `SensorError`. |
| [`sensors/bme280.py`](../src/iot_node/sensors/bme280.py) | Temperature, humidity, pressure. Forced-mode reads; datasheet compensation maths. |
| [`sensors/sgp40.py`](../src/iot_node/sensors/sgp40.py) | VOC. Raw signal plus Sensirion's gas index algorithm, with its learned baseline checkpointed across restarts. |
| [`sensors/manager.py`](../src/iot_node/sensors/manager.py) | Opens what it can, samples everything, writes the daily CSV. A missing sensor is logged, not fatal. |

### Audio

| File | Responsibility |
| --- | --- |
| [`audio/recorder.py`](../src/iot_node/audio/recorder.py) | Device discovery, sample-rate probing, and recording via `arecord`. |
| [`audio/audiomoth.py`](../src/iot_node/audio/audiomoth.py) | The other path: picking up files an AudioMoth wrote to its own SD card. |

### Cloud

| File | Responsibility |
| --- | --- |
| [`cloud/backend.py`](../src/iot_node/cloud/backend.py) | The `Backend` protocol (`check`/`upload`/`download`/`remote_size`) and the provider factory. |
| [`cloud/rclone.py`](../src/iot_node/cloud/rclone.py) | The rclone backend — the one in use. Shells out; maps exit codes to retryable vs fatal. |
| [`cloud/onedrive.py`](../src/iot_node/cloud/onedrive.py) | Microsoft Graph client. Unused (AUT blocks uploads) but kept working. |
| [`cloud/graph_backend.py`](../src/iot_node/cloud/graph_backend.py) | Adapter making the Graph client fit `Backend`. |
| [`cloud/auth.py`](../src/iot_node/cloud/auth.py), [`auth_cli.py`](../src/iot_node/cloud/auth_cli.py) | MSAL auth for the Graph path only. |
| [`cloud/uploader.py`](../src/iot_node/cloud/uploader.py) | Drains the spool, verifies sizes, and runs housekeeping. |
| [`cloud/config_sync.py`](../src/iot_node/cloud/config_sync.py) | Fetches `config.ini`, caches it, keeps a known-good backup, pushes the master log. |

### Around the code

| Path | |
| --- | --- |
| [`systemd/iot-node.service`](../systemd/iot-node.service) | The node. `Type=notify`, `WatchdogSec=90`, restarts forever. |
| [`systemd/bee-node-screen.service`](../systemd/bee-node-screen.service) | The screen, as a user service tied to the graphical session. |
| [`systemd/gdrive-mount.service`](../systemd/gdrive-mount.service) | Mounts Drive at `~/GoogleDrive` for browsing. Not used for uploads. |
| [`scripts/install-service.sh`](../scripts/install-service.sh) | Installs and enables everything for boot. |
| [`scripts/setup-onedrive.sh`](../scripts/setup-onedrive.sh) | rclone install and sign-in. |
| [`scripts/enable-i2c.sh`](../scripts/enable-i2c.sh), [`i2c-scan.py`](../scripts/i2c-scan.py) | I2C bring-up and diagnosis. |

---

## 3. What happens, and when

The loop in `Node.run()` runs six tasks on independent timers. Each is short;
none blocks the others. A task that throws is logged and retried next time.

| Task | Every | What it does |
| --- | --- | --- |
| `sample` | `sensor_interval_seconds` | If this hour is on the sensor timetable, read BME280 + SGP40, append a row to today's CSV, update the status snapshot. |
| `record` | 20 s (a check, not a recording) | If this hour is on the microphone timetable and nothing is already recording, start one in a worker thread. |
| `ingest` | 5 min | Pick up any recordings an AudioMoth wrote to a mounted SD card. |
| `upload` | checked every 5 min, acts on `frequency` | Drain the spool to Drive, then purge what landed. |
| `status` | 5 s | Publish `status.json` for the screen. |
| `config` | 1 h | Re-fetch `config.ini`; push `master_log.csv`. |

Between tasks the loop wakes at least every 15 seconds to feed the watchdog.

### Recording, in detail

Recording happens on a **worker thread**, because a 5-minute recording would
otherwise block sampling and uploading for 5 minutes. The main loop starts it
and moves on; the worker records, analyses, and queues the file.

```
task_record  ──> thread ──> arecord (hw:, native rate)
                              │
                              ▼  .WAV.part → atomic rename → .WAV
                          analyse_wav()  ──> audio_analysis_<date>.csv
                              │                    └──> status.json (screen)
                              ▼
                          store.enqueue()  ──> uploaded next pass
```

Analysis runs **before** the file is queued, deliberately: housekeeping may
delete the audio soon after upload, and the analysis is the part that cannot be
recreated afterwards.

---

## 4. The data

### On the Pi

```
var/
├── current/                        the day in progress
│   ├── sensors_<device>_<date>.csv
│   └── audio_analysis_<device>_<date>.csv
├── spool/                          finished, waiting to upload
│   ├── sensors/*.csv
│   └── audio/*.WAV
└── state/
    ├── iot-node.db                 upload ledger (SQLite)
    ├── config.ini                  last fetched
    ├── config.ini.backup           last that parsed cleanly
    ├── status.json                 what the screen draws
    ├── master_log.csv              one row per day
    └── voc_state.json              SGP40 learned baseline
```

The `current` → `spool` split is what makes uploads safe: a file only enters the
spool when it is complete, so the uploader can never ship a half-written day and
then ship it again.

### On Drive

```
IoT/
├── config.ini
├── master_log.csv
└── pi5-node-01/
    └── 2026-08-17/
        ├── sensors_pi5-node-01_2026-08-17.csv
        ├── audio_analysis_pi5-node-01_2026-08-17.csv
        └── audio/
            └── 20260817_120000.WAV
```

Files are filed under the day they were **recorded**, parsed from the filename —
not the day they happened to be uploaded.

### The three CSVs

**`sensors_<device>_<date>.csv`** — one row per reading:

`timestamp_utc, timestamp_local, device_id, site, temperature_c, humidity_pct,
pressure_hpa, voc_raw, voc_index, errors`

**`audio_analysis_<device>_<date>.csv`** — one row per recording:

`filename, analysed_at_utc, sample_rate_hz, duration_s, rms_dbfs, peak_dbfs,
spl_estimate_db, calibration_db_offset, dominant_hz, band_hz, device,
peak1_hz, peak1_db, … peak5_hz, peak5_db`

**`master_log.csv`** — one row per day, the deployment at a glance:

`date, sensor_hours, mic_hours, sensor_samples, audio_files, audio_seconds,
audio_mib, sample_rate_hz, analysed_files, mean_dbfs, dominant_hz,
uploaded_files, upload_frequency, restarts, last_updated_utc, notes`

`restarts` is worth watching. A node quietly restarting every ten minutes looks
healthy in the sensor data and is badly broken.

---

## 5. Settings: two files, different jobs

**`config.yaml`** lives on the Pi and is deployment plumbing — paths, which
cloud provider, the rclone remote name. Changing it means touching the device.

**`config.ini`** lives on Google Drive and is what a beekeeper actually wants to
change: which hours to record, how often to sample, what to analyse, when to
upload, how aggressively to purge. Edit it from a laptop or a phone; the node
picks it up within the hour.

### Offline behaviour

Three layers, tried in order:

1. **The drive copy** — authoritative whenever reachable.
2. **`var/state/config.ini`** — the last copy actually fetched.
3. **`var/state/config.ini.backup`** — the last copy that *parsed cleanly*.

The distinction between 2 and 3 is the point. If someone saves a malformed
config.ini, the node fetches it, fails to make sense of it, and falls back to the
last version that worked — rather than reverting to built-in defaults and
silently abandoning the operator's timetable. Only a clean parse is promoted to
the backup.

With no network at all the node runs indefinitely on layer 2 or 3. Recording,
sampling and analysis are entirely local; only uploads need the link, and they
queue until it returns.

---

## 6. Staying alive

Three independent mechanisms, because they catch different failures.

**Crash → restart.** `Restart=always`, `RestartSec=15`, and no start limit. A
node in a field must keep trying however many times it takes.

**Hang → watchdog.** `Type=notify` with `WatchdogSec=90`. The loop pings every
~15 s. If the process wedges — stuck I2C read, blocked socket, deadlock — the
pings stop and systemd kills and restarts it. This catches the failures a
process cannot notice about itself.

**Stall → deliberate silence.** A process can be perfectly responsive while
doing nothing useful. Every task stamps its last success; if one falls more than
8× its own interval behind (floor: 5 minutes), `HealthMonitor.pump()` *stops
pinging on purpose* so systemd restarts the node. Silence is treated as failure
rather than as health.

The thresholds are deliberately generous — a false restart loses data, so a task
that is merely late is not treated as broken.

**Power loss** is handled by the data layout rather than by software: SQLite in
WAL mode, atomic renames for every file the uploader might see, and `.part`
suffixes while writing. An interrupted recording leaves a `.part` file that is
cleaned up on the next start.

---

## 7. The acoustics

For each recording, `analyse_wav()` reports:

- **Top N spectral peaks** — frequency and amplitude, default 5.
- **`dominant_hz`** — the loudest peak.
- **`rms_dbfs`** and **`peak_dbfs`** — overall level.
- **`spl_estimate_db`** — `rms_dbfs + calibration_db_offset`.

Method: the file is split into overlapping 4096-sample Hann windows and the
spectra averaged (Welch-style). A single FFT over five minutes would smear
transients into the noise; averaging gives a stable picture of the colony's
steady hum. Peaks are picked greedily with a 15 Hz exclusion zone, so one broad
peak is not reported five times, and each peak's frequency is refined by
parabolic interpolation — without that, a peak is only located to the nearest
bin (~12 Hz), which is too coarse to notice a fundamental drifting by a few
hertz.

The default band is 20–2000 Hz because that is where a colony lives: wingbeat
fundamentals near 200–250 Hz, worker piping and whooping around 300–600 Hz.
Analysing wider mostly measures wind, rain and passing traffic.

### Read this before quoting any dB figure

**A plain microphone cannot measure absolute loudness.** What it reports is
level relative to digital full scale — **dBFS** — which is always negative and
depends entirely on that microphone's own gain. There is no way to recover true
sound pressure from it without calibration.

So `spl_estimate_db` is *not* a sound pressure level until you calibrate it. To
do that: put a calibrated sound level meter beside the microphone, note the real
dB SPL and the reported `rms_dbfs` at the same moment, and set
`calibration_db_offset` to the difference. Until then the column is honest but
uncalibrated — perfectly good for comparing one recording against another, and
wrong for quoting an absolute figure in a report.

---

## 8. The screen

`iot-node-screen` draws a fullscreen Tk dashboard: live environment readings
with trend lines, the acoustic peaks with level bars, both timetables with the
current hour marked, and a footer of counts and disk.

It reads `status.json` and nothing else — no sensors, no network, no database.
That isolation is deliberate in both directions: the screen cannot slow the node
down or corrupt anything, and a node restart does not take the screen with it.

If the snapshot stops updating, the screen greys the readings and shows
**NODE SILENT** with the age. A dashboard that keeps showing old numbers as if
they were current is worse than no dashboard.

`Esc` or `q` quits; `F11` toggles fullscreen.

---

## 9. Running it

```bash
scripts/install-service.sh      # installs both services, enables boot start
journalctl -u iot-node -f       # watch the node
iot-node status                 # what is it doing right now?
iot-node config                 # what timetable is in force?
```

| Command | |
| --- | --- |
| `iot-node run` | The service itself |
| `iot-node sample` | Read the sensors once |
| `iot-node record --seconds 10` | Record one file now |
| `iot-node audio-test` | Probe every sample rate the hardware supports |
| `iot-node analyse <file.wav>` | Analyse one recording |
| `iot-node scan` | Ingest from a mounted AudioMoth card |
| `iot-node upload` | Drain the spool once |
| `iot-node config` / `status` | Inspect |
| `iot-node-screen --windowed` | The dashboard, in a window |

## 10. Testing

```bash
.venv/bin/python -m pytest      # no hardware, no network needed
.venv/bin/ruff check src tests
.venv/bin/mypy
```

The suite fakes the hardware rather than mocking the code: a fake SMBus replays
real register maps, the analysis tests synthesise WAVs with known tones and
assert the peaks come back at the right frequencies, the rclone tests replay real
exit codes, and the config-sync tests simulate a drive going offline mid-
deployment. Nothing touches the network or the sensors.
