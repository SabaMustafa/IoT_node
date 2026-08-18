# Bee Node — User Manual

Hive environmental and acoustic monitoring on a Raspberry Pi.
Developed by Mohammad (Amin) BarzegarTabrizi <mabiran@gmail.com>

This manual assumes no prior knowledge of the machine. If you only ever read
one section, read **Everyday commands**.

---

## 1. What this thing does

A Raspberry Pi sits by the hive and, on a timetable you control from Google
Drive:

- reads **temperature, humidity, pressure and air quality** every 3 minutes,
- **records the hive** through an AudioMoth microphone for ten hours a day, in
  five-minute files,
- runs a **frequency analysis** on each recording and writes the loudest peaks
  to a CSV,
- **uploads** everything to Google Drive every 6 hours,
- shows a **live screen** on the attached display,
- restarts itself if it ever locks up, and keeps going if the network drops.

It starts by itself when the Pi is powered on. Nobody needs to log in.

---

## 2. Getting to a terminal

Everything in this manual is typed into a terminal.

**On the Pi itself** — click the black `>_` icon in the top bar of the desktop,
or press `Ctrl` + `Alt` + `T`.

**From another computer on the same network:**

```bash
ssh saba@raspberrypi.local
```

If that name is not found, use the Pi's IP address, which the screen shows, or
find it on the Pi with `hostname -I`.

### Terminal survival kit

| You want to | Type |
| --- | --- |
| Stop whatever is running now | `Ctrl` + `C` |
| Repeat the last command | `Up arrow`, then `Enter` |
| Finish a half-typed name | `Tab` |
| Go to the project | `cd ~/iot-node` |
| See what is in a folder | `ls` |
| Read a file | `cat filename` |
| Leave a scrolling view (`-f`, `less`) | `q`, or `Ctrl` + `C` |
| Clear the screen | `clear` |

Commands are case-sensitive. A command that prints nothing has usually
succeeded — silence is good news.

---

## 3. Do I need to activate the virtual environment?

**No.** The commands are installed system-wide, so they work in any terminal:

```bash
iot-node status
```

You only need the virtual environment if you are **changing the code** or
running the tests:

```bash
cd ~/iot-node
source .venv/bin/activate     # prompt gains a (.venv) prefix
python -m pytest              # run the test suite
deactivate                    # leave it again
```

If a command is ever "not found", the full path always works:
`/home/saba/iot-node/.venv/bin/iot-node`

---

## 4. Everyday commands

### Is it working?

```bash
iot-node status              # queue, disk, schedule, microphone — start here
scripts/verify-boot.sh       # 12-point check of the whole system
```

`verify-boot.sh` lives in `~/iot-node`, so `cd ~/iot-node` first.

### Watch it live

```bash
journalctl -u iot-node -f    # follow the node's log; Ctrl+C to stop watching
journalctl -u iot-node -n 50 # the last 50 lines and exit
journalctl -u iot-node --since "1 hour ago"
```

### Start / stop / restart

The node is a **system** service, so it needs `sudo`:

```bash
sudo systemctl start   iot-node     # start it
sudo systemctl stop    iot-node     # stop it (sensors and recording pause)
sudo systemctl restart iot-node     # stop then start — use after a code change
sudo systemctl status  iot-node     # is it running, and why not
```

The screen and the Drive mount are **user** services — no `sudo`:

```bash
systemctl --user restart bee-node-screen   # the display
systemctl --user restart gdrive-mount      # the ~/GoogleDrive folder
```

### Start at boot, or not

Already enabled. To change it:

```bash
sudo systemctl enable  iot-node            # start at boot (already set)
sudo systemctl disable iot-node            # do not start at boot
systemctl --user enable bee-node-screen
```

Stopping a service does **not** disable it — after a reboot it comes back.
That is deliberate.

---

## 5. Every command, in full

Run `iot-node <command>`. Anything not listed as "asks the running node" works
whether or not the service is running.

| Command | What it does |
| --- | --- |
| `iot-node run` | The whole node on its timetable. **This is what the service runs** — do not run it by hand while the service is up. |
| `iot-node status` | Queue depth, disk, credentials, today's schedule, which microphone. |
| `iot-node sample` | Read the sensors once and print the result. Good for checking wiring. |
| `iot-node record` | Record one file now, ignoring the timetable. |
| `iot-node analyse FILE...` | Frequency-analyse one or more WAV files and print the peaks. |
| `iot-node audio-test` | Probe the microphone at every supported sample rate. |
| `iot-node mic-test` | Ask the **running** node to record a few seconds and report the input level. |
| `iot-node upload` | Push everything waiting in the spool to Drive now, then stop. |
| `iot-node scan` | Stage recordings found on a mounted AudioMoth SD card. |
| `iot-node config` | Show the settings actually in force, fetching config.ini if it can. |
| `iot-node refresh` | Ask the **running** node to re-read config.ini immediately. |

### Options

| Option | Applies to | Meaning |
| --- | --- | --- |
| `--seconds N` | `record`, `mic-test` | Length in seconds. |
| `--rate N` | `record` | Sample rate for this one recording. |
| `-v` | any | Verbose — show DEBUG detail when something is wrong. |
| `-c FILE` | any | Use a different `config.yaml` (deployment settings). |
| `-h` | any | Help. |

### Examples

```bash
iot-node record --seconds 30            # a 30-second test recording
iot-node record --seconds 10 --rate 48000
iot-node analyse var/spool/audio/*.WAV  # analyse everything waiting
iot-node mic-test --seconds 5           # check the mic without interrupting
iot-node upload                         # drain the queue now
iot-node status -v                      # status with detail
```

---

## 6. Changing what it records

**Do not edit anything on the Pi.** The timetable lives in Google Drive at
`IoT/config.ini`. Open it in Drive, edit, save. The node re-reads it **every 15
minutes**; `iot-node refresh` makes it read immediately.

The file explains every setting inside itself. The ones people change:

| Setting | Meaning |
| --- | --- |
| `microphone` | 24 digits, one per hour. `1` = record that hour. |
| `sensors` | Same, for the environmental sensors. |
| `sample_rate` | Audio quality vs disk. See the table in the file — **not every value is usable**. |
| `recording_minutes` | Length of each audio file. |
| `frequency` | How often to upload: `6h`, `12h`, `daily`. |
| `min_frequency` / `max_frequency` | The band the analysis searches, in Hz. |

Confirm a change landed:

```bash
iot-node refresh
journalctl -u iot-node -n 5      # look for "settings in force:"
```

---

## 7. Where everything is

**On the Pi** (`~/iot-node`):

| Path | Contents |
| --- | --- |
| `var/current/` | Today's CSVs, still being written |
| `var/spool/audio/` | Recordings waiting to upload |
| `var/spool/sensors/` | CSVs waiting to upload |
| `var/state/config.ini` | Last known-good copy of the timetable |
| `var/state/iot-node.db` | Upload bookkeeping |
| `docs/` | This manual, architecture, operating notes |
| `~/GoogleDrive/` | The Drive, mounted for browsing |

**On Google Drive:**

| Path | Contents |
| --- | --- |
| `IoT/config.ini` | **The timetable — edit this one** |
| `IoT/pi5-node-01/<date>/audio/` | Recordings |
| `IoT/pi5-node-01/<date>/*.csv` | Sensor and analysis data |
| `IoT/releases/` | Installable zips, and this manual |
| `IoT/master_log.csv` | One row per day: what the node did |

Local recordings are deleted **only after** they are safely uploaded and
size-checked.

---

## 8. Common jobs

**Check the last night's data**

```bash
iot-node status
ls -lh var/current/
```

**Free up space**

```bash
df -h /                      # how full is the card
iot-node upload              # uploading is what frees it
```

**Move to a different Google account** — see `GOOGLE-DRIVE-SETUP.md`, which
covers the client ID, the secret, and swapping rclone over.

**Reboot the Pi**

```bash
sudo reboot
```

Wait a minute, then `cd ~/iot-node && scripts/verify-boot.sh`. Everything
should come back by itself.

**Shut down safely** — pulling the power can corrupt the card:

```bash
sudo shutdown -h now
```

Wait for the green light to stop blinking before unplugging.

---

## 9. When something looks wrong

| Symptom | Check | Likely fix |
| --- | --- | --- |
| Screen is blank or frozen | `systemctl --user status bee-node-screen` | `systemctl --user restart bee-node-screen` |
| No new data | `iot-node status` | `sudo systemctl restart iot-node` |
| "no AudioMoth capture device found" | Switch position and cable | Set the switch to **DEFAULT**, replug, use a **data** cable, not charge-only |
| Uploads failing | `journalctl -u iot-node -n 30` | `storageQuotaExceeded` means Drive is full — free space or add storage |
| Queue keeps growing | `iot-node status` | Usually Drive full or network down; data waits on the card, it is not lost |
| Card filling up | `df -h /` | Lower `sample_rate` or record fewer hours in config.ini |
| Timetable edits ignored | `iot-node config` | Confirm you edited `IoT/config.ini` on **Drive** |
| Command not found | — | Use `/home/saba/iot-node/.venv/bin/iot-node` |

Nothing here is fixed by deleting files by hand. If in doubt, capture the
evidence and stop:

```bash
journalctl -u iot-node --since "2 hours ago" > ~/node-log.txt
```

---

## 10. Rules worth keeping

1. **Never run `iot-node run` while the service is running.** Two copies fight
   over the microphone and the database. Check with `iot-node status`.
2. **Edit the timetable on Drive, not on the Pi.** The Pi's copy is a cache and
   gets overwritten.
3. **Shut down properly.** `sudo shutdown -h now`.
4. **Stopping is not disabling.** A stopped service returns after a reboot.
5. **A full Drive stops uploads; a full card stops everything.** Watch both.
6. `rclone.conf` holds a live key to the Google account. Never copy it, commit
   it, or photograph it.

---

## 11. Installing on a fresh Pi

```bash
unzip bee-node-latest.zip
cd bee-node-*/
./install.sh
```

`install.sh` does the lot — system packages, Python environment, I2C, rclone,
the Drive mount, and both services set to start at boot. It is safe to re-run,
so it is also the upgrade path. Options: `--no-cloud`, `--no-service`, `--dev`.

---

## 12. Getting help

```bash
iot-node --help
scripts/verify-boot.sh
```

Fuller documentation is in `docs/` on the Pi: `OPERATING.md` for running it,
`ARCHITECTURE.md` for how it is built and why, `GOOGLE-DRIVE-SETUP.md` for
moving Google accounts.
