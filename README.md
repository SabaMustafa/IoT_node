# Bee Node

A Raspberry Pi 5 beside a beehive. It logs the environment with a **BME280**
(temperature, humidity, pressure) and an **SGP40** (VOC), records the colony
through an **AudioMoth**, works out what it sounds like, shows all of it on a
screen, and ships everything to **Google Drive**.

It is built to be left alone for months on a link that comes and goes: sensors
and recording never depend on the network, everything spools locally first, and
a watchdog restarts the node if it ever stops doing its job.

```
sensors ──▶ daily CSV ──┐
                        ├──▶ var/spool/ ──▶ Google Drive ──▶ purge what landed
AudioMoth ──▶ WAV ──┬───┘         │
                    │             └── SQLite ledger of what actually uploaded
                    └──▶ FFT ──▶ top-5 peaks + dB ──▶ analysis CSV
                                        │
                                        └──▶ status.json ──▶ BEE NODE screen
```

**📖 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) is the full map** — every file,
what talks to what, and why each piece is built the way it is. Start there to
change anything.

## The screen

```bash
iot-node-screen               # an ordinary, minimisable window
iot-node-screen --fullscreen  # kiosk mode for a dedicated display
```

Live readings with trend lines, the acoustic peaks, both timetables with the
current hour marked, and counts of everything. It reads `status.json` and
nothing else, so it cannot affect the node — and if the node stops publishing,
the screen says **NODE SILENT** rather than showing stale numbers as current.
`Esc` or `q` closes it, `F11` toggles fullscreen. It starts at boot via
`bee-node-screen.service`.

## Staying alive unattended

Three mechanisms, catching different failures:

* **Crash** → `Restart=always`, no start limit.
* **Hang** → systemd `WatchdogSec=90`; the loop pings every ~15 s, so a wedged
  process is killed and restarted.
* **Silent stall** → a process can be responsive while doing nothing useful, so
  every task stamps its last success. If one falls 8× its own interval behind,
  the node *deliberately stops pinging* and lets systemd restart it.

Restarts are counted in `master_log.csv` — a node quietly restarting every ten
minutes looks healthy in the sensor data and is badly broken.

## Hive acoustics

Every recording is analysed: the **top 5 spectral peaks** (frequency and
amplitude), the dominant frequency, and overall level. Default band is
20–2000 Hz, where a colony actually lives — wingbeats near 200–250 Hz, worker
piping around 300–600 Hz.

> **On dB:** a plain microphone cannot measure absolute loudness. Levels are
> reported as **dBFS** (relative to digital full scale, always negative). The
> `spl_estimate_db` column is only a true SPL once you set
> `calibration_db_offset` by comparing against a calibrated meter. Until then
> it is fine for comparing recordings with each other, and wrong for quoting an
> absolute figure. See [ARCHITECTURE.md §7](docs/ARCHITECTURE.md).

## config.ini — the field settings

Everything the node does hour to hour is decided by **`config.ini` in the `IoT`
folder on Google Drive**, not by anything on the Pi. Edit it from a laptop or a
phone; the node re-reads it hourly and applies the change without anyone logging
in. If it is missing, the node writes a documented default for you to edit.

```ini
[schedule]
; Hour:      0 1 2 3 4 5 6 7 8 9 . . . . . . . . . . . . 23
sensors    = 1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1
microphone = 0,0,0,0,0,1,1,1,1,0,0,0,0,0,0,0,0,0,1,1,1,0,0,0

[audio]
sample_rate = 48000          ; 8000 … 384000, see below
recording_minutes = 5

[sensors]
sensor_interval_seconds = 60 ; how often to read temp/humidity/pressure/VOC

[upload]
frequency = daily            ; daily | 12h

[storage]
delete_after_upload = yes
min_free_percent = 15        ; never let the card fall below this
max_local_days = 7
drop_oldest_when_full = yes  ; discard oldest audio rather than seize up
```

`1` means do it during that hour, `0` means skip; hours are **local time**. A
bare run of 24 digits (`111100001111...`) works too. The shipped file carries a
worked `[example]` section — dawn chorus, bat survey, sensors-only — which the
node ignores, so you can copy lines out of it safely.

Bad input never stops the node: an unparseable value is logged as a warning and
the previous setting stands. `iot-node config` shows what is actually in force.

### Storage policy

A full SD card stops the sensors too, not just recording, so the purge rules
matter. Housekeeping escalates gently: delete what is safely uploaded, then
delete uploaded data older than `max_local_days`, and only if the card is
*still* below `min_free_percent` does it start discarding **un-uploaded** audio,
oldest first and loudly logged. Set `drop_oldest_when_full = no` if you would
rather the node stop than lose anything. Sensor CSVs are never discarded — they
are a few KB a day and are the irreplaceable record.

## What lands on Drive

```
IoT/
├── config.ini                                  <- you edit this
├── master_log.csv                              <- one row per day, what it did
└── pi5-node-01/
    └── 2026-08-14/
        ├── sensors_pi5-node-01_2026-08-14.csv  <- one row per reading
        └── audio/
            └── 20260814_175216.WAV             <- timestamped recordings
```

`master_log.csv` is the deployment at a glance: per day, how many readings, how
many audio files, how many minutes, at what sample rate, how many uploaded, and
any errors. It records what each day was *configured* to do as well as what
happened, so a day with the microphone scheduled but broken is distinguishable
from a day with it deliberately off.

## Quick start

```bash
scripts/setup-onedrive.sh      # install rclone, sign in, mount at boot
.venv/bin/iot-node status      # queue depth, disk, I2C presence
.venv/bin/iot-node sample      # read the sensors once
.venv/bin/iot-node run         # the daemon
```

| Command | What it does |
| --- | --- |
| `iot-node run` | The service: sample, record, upload, housekeep, on the timetable |
| `iot-node sample` | Read the sensors once and print the JSON record |
| `iot-node record` | Record one file now, ignoring the timetable (`--seconds`, `--rate`) |
| `iot-node audio-test` | Probe the AudioMoth at every supported sample rate |
| `iot-node config` | Show the timetable actually in force, fetching from Drive |
| `iot-node scan` | Stage recordings found on a mounted AudioMoth card |
| `iot-node upload` | Drain the spool once, then stop |
| `iot-node status` | Queue depth, disk, I2C, and whether this hour is scheduled |

## The AudioMoth

**Which switch position depends on which firmware is on it**, and the two modes
are genuinely different deployments:

| You want | Firmware | Switch | How data arrives |
| --- | --- | --- | --- |
| The Pi to record on the timetable | **AudioMoth-USB-Microphone** | **USB/OFF** | Live capture over USB; the `microphone` timetable and `sample_rate` in config.ini control it |
| The AudioMoth to record standalone | AudioMoth-Firmware-Basic | CUSTOM or DEFAULT | It writes to its own microSD; the Pi ingests when the card is mounted |

USB/OFF alone is not enough. With the **stock Basic firmware** that position
only talks to the AudioMoth configuration app — it will not appear as a
recording device at all. For the Pi to drive it you must flash the separate
**USB Microphone** firmware first. Also make sure the cable is a data cable;
charge-only USB cables are a common and confusing failure.

Check what the Pi can actually see and record:

```bash
iot-node audio-test
```

It lists every capture device, flags the AudioMoth, then tries each rate
**natively** and reports which genuinely work. That last part matters: capture
goes through ALSA `hw:` rather than `plughw:` on purpose. `plughw` would happily
accept any rate and silently resample, so a file labelled 192 kHz could contain
nothing above 24 kHz — fine for voice, useless for bat work. If a configured
rate is not natively supported, the node says so and drops to the nearest rate
the hardware really offers rather than faking bandwidth.

Supported rates are `8000, 16000, 32000, 48000, 96000, 192000, 250000, 384000`.
Cost scales with rate: 384 kHz fills storage eight times faster than 48 kHz.

## Cloud storage

The node uploads through **rclone**, calling `rclone copyto` against the spool.
The active target is **Google Drive** (`cloud.rclone.remote: gdrive`), using a
private OAuth client rather than rclone's shared one.

Verified working end to end: four recordings uploaded, hash-checked, filed by
recording date, and the spool cleaned by housekeeping afterwards.

```bash
.venv/bin/iot-node upload      # drain the spool once
rclone lsl gdrive:IoT/pi5-node-01/audio/2026/08/14/
```

### Browsing the drive

Google Drive is mounted at `~/GoogleDrive` by a systemd **user** service, with
`loginctl enable-linger` set so it comes up at boot without anyone logging in:

```bash
ls ~/GoogleDrive/IoT/pi5-node-01/audio/     # the node's uploads
systemctl --user status gdrive-mount
journalctl --user -u gdrive-mount -f
```

Uploads do **not** go through the mount — the uploader calls `rclone copyto`
against the spool directly, so a wedged FUSE mount can never strand a recording,
and rclone's hash check confirms each transfer independently. The mount exists
purely for browsing; the same files are at
[drive.google.com](https://drive.google.com) under `My Drive → IoT`.

### ⚠️ Storage is the binding constraint

The Drive account holds **15 GB total with ~5.6 GB free**, against audio
accruing at roughly **8 GB/day**:

> **Under 17 hours of recording before the quota is full.**

Nothing about the node fixes this — it needs either a paid plan, a Google Shared
Drive (separate quota), or a different destination entirely. Until then, treat
continuous recording as something to switch on deliberately, not leave running.
The node degrades safely: uploads fail, files stay spooled, and the queue drains
once space exists — but the local card fills at the same 8 GB/day, and it has
~18 GB free.

### Why not OneDrive

The AUT tenant refuses file uploads to the student's OneDrive:

```
accessDenied: Request was cancelled by event received.
```

Verified scope — authentication and reads work fine, folder creation works, and
**every** file write is refused: at the root, in a subfolder, nested, for `.txt`
and `.WAV` alike, on both the simple and resumable upload paths. That error is a
SharePoint event receiver cancelling the write, meaning a DLP rule, a retention
hold, or a tenant restriction on unmanaged apps. No client-side change gets past
it — `onedriver` was installed and tested for exactly this reason, since it uses
a different app ID, and the block is on the drive rather than the client.

Routes back to OneDrive, if it is ever needed: a different AUT account, a
SharePoint document library instead of personal OneDrive, or an app registration
approved by AUT IT (which is what `cloud.provider: onedrive` is for).

### Redoing the Google Drive setup

The remote uses a private OAuth client, because rclone's shared client ID is
heavily rate-limited. To rebuild it from scratch on another account or machine:

1. [console.cloud.google.com](https://console.cloud.google.com) → new project →
   **APIs & Services → Library** → enable **Google Drive API**.
2. **Google Auth Platform → Audience** → set publishing status to
   **In production**. This is not optional: while an app is in *Testing*, Google
   expires its refresh tokens after **7 days**, so an unattended node would stop
   uploading every week. An unverified production app shows a click-through
   warning at sign-in, which is fine for a single account.
3. **Clients → Create client → Desktop app** → copy the client ID and secret.
4. Create the remote and authorise it:

   ```bash
   rclone config create gdrive drive \
       client_id "<id>" client_secret "<secret>" scope drive --non-interactive
   rclone config reconnect gdrive:      # opens a browser to sign in
   ```

If Google balks at the `drive` scope during publishing, `drive.file` needs no
verification at all and is sufficient here — it grants access only to files the
app itself creates, which is exactly what an uploader does. The tradeoff is that
rclone can then no longer see pre-existing Drive content.

**Account longevity:** the data currently lands in a personal Google account. If
this outlives the project, move it to an account that isn't tied to one student
— a Shared Drive also solves the quota problem above, since it carries its own.

### Remote layout

```
IoT/<device-id>/audio/2026/08/14/20260814_155418.WAV
IoT/<device-id>/sensors/2026/08/14/sensors-<device-id>-2026081415.jsonl
```

Files are filed under the day they were *recorded* — parsed from the AudioMoth
filename — not the day they were copied.

### The Graph API alternative

`cloud.provider: onedrive` swaps rclone for a direct Microsoft Graph client
([cloud/onedrive.py](src/iot_node/cloud/onedrive.py)) using MSAL. It needs an
Azure app registration and `iot-node-auth` to sign in, which is exactly the
hassle rclone avoids — but it removes the external binary, so it is there for if
tenant policy ever forces the issue. Setup lives in [.env.example](.env.example)
and the `cloud.onedrive` block of `config.yaml`.

## Configuration

`config.yaml` holds the tracked defaults and is commented throughout. Two ways
to override it without touching git:

* **`config.local.yaml`** — deep-merged on top, gitignored. Use it for anything
  specific to this physical node (device id, site, source directories).
* **`.env`** — supplies `${...}` values referenced from `config.yaml`. Only
  needed for the Graph provider.

The settings you are most likely to change:

```yaml
device:
  id: pi5-node-01          # appears in filenames and the remote path
sensors:
  sample_interval_s: 60
audio:
  source_dirs: [/home/saba/Recordings]
  action: copy             # 'move' to clear the source as it goes
upload:
  interval_s: 300
  delete_after_upload: true
```

## Hardware

I2C is enabled on the GPIO header (`/dev/i2c-1`, GPIO 2/3). Current state:

| Sensor | Address | Status |
| --- | --- | --- |
| BME280 | `0x76` | working — reading temperature, humidity, pressure |
| SGP40 | `0x59` | **not responding** — absent from the bus |

`i2cdetect -y 1` shows only `0x76`, so the SGP40 is not wired in yet (check 3V3,
GND, and that SDA/SCL reach the module). The node runs fine without it: a
missing sensor is logged once and the other keeps sampling.

| Sensor pin | Pi 5 header |
| --- | --- |
| VIN / VCC | 3V3 (pin 1) — **not** 5V |
| GND | GND (pin 6) |
| SDA | GPIO2 (pin 3) |
| SCL | GPIO3 (pin 5) |

```bash
scripts/i2c-scan.py       # expect 0x76 (BME280) and 0x59 (SGP40)
```

The scan flags a *floating* bus — `i2c-13` and `i2c-14` are the Pi's HDMI DDC
lines, which have no pull-ups and answer at every address. They are not your
sensors.

### About the VOC index

The SGP40 reports a raw resistance, not an air-quality number. Sensirion's gas
index algorithm turns that into a 1–500 VOC Index by learning a slow baseline,
so **the index only becomes meaningful after several hours of continuous
running**. The learned state is checkpointed to `var/state/voc_state.json` and
restored on restart (and discarded if more than a week stale), so a reboot does
not send the baseline back to square one. `voc_raw` is always recorded alongside.

## Running as a service

```bash
scripts/install-service.sh
journalctl -u iot-node -f
```

The unit restarts on failure with no start-limit, so a node that loses power or
network comes back on its own.

## Development

```bash
.venv/bin/python -m pytest      # 77 tests, no hardware or network needed
.venv/bin/ruff check src tests
.venv/bin/mypy
```

The sensor tests drive the drivers through a fake SMBus and check the
compensation maths against the worked examples in the BME280 datasheet; the
rclone tests replay scripted exit codes to pin down which failures are worth
retrying; the Graph tests replay scripted HTTP responses, including an expired
upload session and a resumed transfer.

## Layout

| Path | |
| --- | --- |
| [src/iot_node/config.py](src/iot_node/config.py) | Typed config, YAML overlay, env expansion |
| [src/iot_node/sensors/](src/iot_node/sensors/) | BME280 and SGP40 drivers, sampling manager |
| [src/iot_node/audio/](src/iot_node/audio/) | AudioMoth discovery and spool ingest |
| [src/iot_node/cloud/backend.py](src/iot_node/cloud/backend.py) | Backend interface and provider factory |
| [src/iot_node/cloud/rclone.py](src/iot_node/cloud/rclone.py) | rclone upload backend (default) |
| [src/iot_node/cloud/onedrive.py](src/iot_node/cloud/onedrive.py) | Graph API client (alternative) |
| [src/iot_node/cloud/uploader.py](src/iot_node/cloud/uploader.py) | Drain loop, verification, housekeeping |
| [src/iot_node/state.py](src/iot_node/state.py) | SQLite ledger of ingests and uploads |
| [src/iot_node/main.py](src/iot_node/main.py) | CLI and the scheduling loop |
