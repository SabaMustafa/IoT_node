# Operating the Bee Node

Everything you need to start, stop, check and change the system. Nothing here
assumes you remember how it was built.

---

## The one thing to understand first

**You do not start the node by hand. systemd runs it.**

The node runs as a *service*. It starts on boot, restarts itself if it crashes
or hangs, and keeps running when you close the terminal or log out. So the
normal way to control it is `systemctl`, not by typing a command and leaving a
window open.

```bash
sudo systemctl start   iot-node      # start it
sudo systemctl stop    iot-node      # stop it
sudo systemctl restart iot-node      # restart it (after changing code)
systemctl status iot-node            # is it running and healthy?
```

If you type `iot-node run` in a terminal you are starting a **second** copy,
which then fights the service over the same files. Don't. (If you already did,
see *Two copies running* below.)

---

## Why `iot-node run` did nothing

The commands live inside the project's virtual environment, which is not on
your PATH by default. Any of these work:

```bash
iot-node status                       # works in a NEW terminal (now linked
                                      # into ~/.local/bin)

~/iot-node/.venv/bin/iot-node status  # always works, any terminal

cd ~/iot-node && source .venv/bin/activate
iot-node status                       # works for the rest of that session
```

`~/.local/bin` is added to PATH by `~/.profile`, which is only read when a
**new login shell** starts — so open a fresh terminal window, or log out and
back in, before the short form works.

---

## Daily operation

### Is it working?

```bash
iot-node status
```

Shows the device, where it uploads, whether this hour is scheduled for sensors
and microphone, the audio input, spool depth, disk, I2C, and the last few days
from the master log.

```bash
systemctl status iot-node
```

The `Status:` line is written by the node itself and updates live, e.g.
`"42 readings, 20.3C, recording, 0 queued"`.

### What is it doing right now?

```bash
journalctl -u iot-node -f          # live log, Ctrl-C to stop watching
journalctl -u iot-node --since today
journalctl -u iot-node -p err      # errors only
```

Watching the log does not affect the node.

### The screen

```bash
systemctl --user restart bee-node-screen    # bring it back if you closed it
systemctl --user stop    bee-node-screen    # stop it starting again
iot-node-screen                             # or just run it in a terminal
```

It is an ordinary window: minimise it, move it, close it. Closing it does
nothing to the node. `Esc` or `q` closes, `F11` toggles fullscreen, `r`
requests a config refresh.

---

## Changing what it does

**Edit `config.ini` in Google Drive, under `IoT/`.** From the Pi:

```bash
nano ~/GoogleDrive/IoT/config.ini
```

…or from any browser at [drive.google.com](https://drive.google.com), or from
your phone. It controls the two timetables, sample rate, sampling interval,
upload cadence, analysis and storage policy.

The node re-reads it **every 15 minutes**. To apply a change immediately:

```bash
iot-node refresh          # or press 'r' on the screen,
                          # or click "REFRESH CONFIG"
```

Check what is actually in force — this is the authority, not what you think you
saved:

```bash
iot-node config
```

You do **not** need to restart the node after editing config.ini. Restarting is
only for code changes.

---

## One-off commands

These run once and exit. They are safe while the service is running, with one
exception noted below.

```bash
iot-node sample                  # read the sensors once, print JSON
iot-node config                  # show the timetable in force
iot-node status                  # health summary
iot-node refresh                 # make the running node re-read config.ini
iot-node analyse FILE.wav        # frequency analysis of a recording
iot-node audio-test              # probe the AudioMoth at every sample rate
```

⚠️ **`iot-node record` and `iot-node upload` compete with the service.** The
microphone can only be opened by one process, and both write the same spool.
Stop the service first if you want to use them:

```bash
sudo systemctl stop iot-node
iot-node record --seconds 30
sudo systemctl start iot-node
```

---

## Two copies running

If the screen shows values that flip back and forth — a setting that takes
effect and then undoes itself — you almost certainly have a second node running
alongside the service. Check:

```bash
ps -eo pid,etimes,args | grep -E '[i]ot-node (run|record)'
systemctl show iot-node -p MainPID --value
```

Anything listed whose PID is not the `MainPID` is a stray. Kill it:

```bash
kill <PID>
```

Then restart the service so it is the only writer:

```bash
sudo systemctl restart iot-node
```

---

## After changing the code

```bash
cd ~/iot-node
.venv/bin/python -m pytest -q     # check nothing broke
sudo systemctl restart iot-node   # load the new code
journalctl -u iot-node -f         # watch it come up
```

The running process holds the code it was started with, so **edits do nothing
until you restart the service**.

---

## Turning it off

```bash
sudo systemctl stop iot-node        # stop now, still starts at next boot
sudo systemctl disable iot-node     # stop starting at boot
sudo systemctl disable --now iot-node   # both

systemctl --user disable --now bee-node-screen   # the screen
systemctl --user disable --now gdrive-mount      # the Drive mount
```

Data already collected is untouched. Anything not yet uploaded stays in
`var/spool/` and goes up when the node next runs.

---

## Where things are

| | |
| --- | --- |
| Project | `~/iot-node` |
| Settings you edit | `~/GoogleDrive/IoT/config.ini` |
| Data waiting to upload | `~/iot-node/var/spool/` |
| Today in progress | `~/iot-node/var/current/` |
| Logs | `journalctl -u iot-node`, and `~/iot-node/logs/` |
| Uploaded data | `~/GoogleDrive/IoT/pi5-node-01/<date>/` |
| Release zips | `~/GoogleDrive/IoT/releases/` |

---

## Deploying to another Pi

```bash
unzip bee-node-latest.zip
cd bee-node-v0.2.0
./install.sh
```

`install.sh` does the lot: packages, Python environment, I2C, rclone, the Drive
mount, and both services enabled for boot. Re-running it is also the upgrade
path.

---

## Quick reference

| I want to… | Command |
| --- | --- |
| See if it is alive | `systemctl status iot-node` |
| Watch it work | `journalctl -u iot-node -f` |
| See what it is doing | `iot-node status` |
| Change the schedule | edit `~/GoogleDrive/IoT/config.ini` |
| Apply that change now | `iot-node refresh` |
| Confirm the change took | `iot-node config` |
| Restart after a code change | `sudo systemctl restart iot-node` |
| Stop it | `sudo systemctl stop iot-node` |
| Bring the screen back | `systemctl --user restart bee-node-screen` |

---

Bee Node — developed by Mohammad (Amin) BarzegarTabrizi <mabiran@gmail.com>
