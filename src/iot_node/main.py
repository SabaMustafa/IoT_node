"""`iot-node` — the node daemon and its one-shot diagnostic subcommands.

    iot-node run         # the service: sample, record, upload, on the timetable
    iot-node sample      # read the sensors once and print the record
    iot-node record      # record one file now, ignoring the timetable
    iot-node audio-test  # probe the AudioMoth at every supported sample rate
    iot-node scan        # stage any recordings found on a card, then stop
    iot-node upload      # drain the spool once, then stop
    iot-node config      # show the effective config.ini, fetching if possible
    iot-node status      # queue depth, disk, credentials, today's schedule

What the node does each hour is decided by config.ini on the drive, not by
anything on the Pi — see src/iot_node/remote_config.py. The run loop keeps
itself free: recording, uploading and config fetches each run in a worker, so
none of them can stall the loop that answers the systemd watchdog.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import signal
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from .analysis import AnalysisError, analyse_wav, analysis_columns, tail_level_dbfs
from .audio import AudioIngest, find_audiomoth_mounts, parse_audiomoth_config
from .audio.recorder import (
    Recorder,
    RecorderError,
    diagnose_missing_audiomoth,
    list_capture_devices,
    probe_sample_rates,
    select_device,
    wav_duration_seconds,
)
from .cloud.backend import UploadError
from .cloud.config_sync import ConfigSync
from .cloud.uploader import Uploader
from .config import Config, load_config
from .csv_log import AnalysisCsvWriter, MasterLog
from .health import HealthMonitor, SystemdNotifier
from .logging_setup import setup_logging
from .remote_config import RemoteConfig
from .sensors.manager import SensorManager
from .state import Store
from .status import (
    Status,
    StatusWriter,
    request_mic_test,
    request_refresh,
    take_mic_test_request,
    take_refresh_request,
)

log = logging.getLogger("iot_node")

CONFIG_REFRESH_S = 900.0  # how often to re-read config.ini from the drive
STATUS_REFRESH_S = 5.0  # how often the dashboard snapshot is republished


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class Node:
    """Owns the long-lived objects and the periodic schedule."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        cfg.ensure_dirs()
        state_dir = cfg.resolve("var/state")
        self.store = Store(cfg.state_db)
        self.sensors = SensorManager(cfg)
        self.audio = AudioIngest(cfg, self.store)
        self.master = MasterLog(state_dir / "master_log.csv")
        self.settings = RemoteConfig()
        self.recorder = Recorder(
            cfg.spool_dir / "audio",
            device_id=cfg.device.id,
            preference=self.settings.input_device,
        )
        self.analysis = AnalysisCsvWriter(
            current_dir=cfg.resolve("var/current"),
            spool_dir=cfg.spool_dir,
            device_id=cfg.device.id,
            columns=analysis_columns(self.settings.top_peaks),
        )
        self.status = StatusWriter(state_dir / "status.json", cfg.device.id, cfg.device.site)
        self.health = HealthMonitor()
        self._uploader: Uploader | None = None
        self._sync: ConfigSync | None = None
        self._stop = threading.Event()
        self._recording = threading.Event()
        self._uploading = threading.Event()
        self._syncing = threading.Event()
        # Settings fetched by the sync thread, waiting to be applied by the
        # main loop.
        self._pending_settings: RemoteConfig | None = None
        self._last_upload = 0.0
        self._analysed: list[float] = []
        self._recording_started = 0.0

    # Built lazily so a credential problem cannot stop the sensors from running.
    @property
    def uploader(self) -> Uploader | None:
        if self._uploader is None and self.cfg.upload.enabled:
            try:
                self._uploader = Uploader(self.cfg, self.store)
            except UploadError as exc:
                log.error("uploader disabled: %s", exc)
                return None
        return self._uploader

    @property
    def sync(self) -> ConfigSync | None:
        if self._sync is None:
            uploader = self.uploader
            if uploader is None:
                return None
            root = self.cfg.cloud.remote_root.format(
                device_id=self.cfg.device.id, site=self.cfg.device.site or self.cfg.device.id
            )
            self._sync = ConfigSync(uploader.backend, root, self.cfg.resolve("var/state"))
        return self._sync

    def stop(self) -> None:
        self._stop.set()
        self.recorder.stop()

    def close(self) -> None:
        self.health.notifier.stopping()
        self.recorder.stop()
        finished = self.sensors.flush()
        if finished is not None:
            log.info("closed out %s", finished.name)
        self.analysis.flush()
        self.sensors.close()
        if self._uploader is not None:
            self._uploader.close()
        self.store.close()
        self.health.notifier.close()

    @property
    def today(self) -> str:
        return self.sensors.local_now().strftime("%Y-%m-%d")

    # --- tasks ---------------------------------------------------------------

    def task_config(self) -> None:
        """Adopt any fetched config, and start the next fetch in the background.

        Fetching talks to the network, and rclone is allowed up to half an hour
        for a transfer — long enough to stop the watchdog heartbeat and have the
        node killed for a slow link it was handling perfectly well. Only the
        fetch moves off the main loop: the settings themselves are applied here,
        so the writers they reconfigure are still only ever touched by one
        thread.
        """
        sync = self.sync
        if sync is None:
            return

        pending = self._pending_settings
        if pending is not None:
            self._pending_settings = None
            self._apply_settings(pending)
            self.status.update(config_fetched_utc=_utc_now())

        if self._syncing.is_set():
            return
        self._syncing.set()
        threading.Thread(
            target=self._sync_config_once, name="config-sync", daemon=True
        ).start()

    def _sync_config_once(self) -> None:
        """Fetch config.ini and push the master log. Runs off the main loop."""
        sync = self.sync
        if sync is None:  # pragma: no cover - guarded by the caller
            self._syncing.clear()
            return
        try:
            if sync.refresh(min_interval_s=CONFIG_REFRESH_S - 60):
                self._pending_settings = sync.config
            sync.push_master_log(self.master.path)
        except Exception:
            # Losing a config refresh is survivable — the cached copy stands.
            log.exception("config sync failed")
        finally:
            self._syncing.clear()

    def _apply_settings(self, settings: RemoteConfig) -> None:
        """Adopt a new config.ini across every component that cares."""
        self.settings = settings
        # The uploader's storage policy lives in config.ini too.
        if self._uploader is not None:
            self._uploader.settings = settings
        self.recorder.set_preference(settings.input_device)
        self.health.set_interval("sample", settings.sensor_interval_s)
        # Changing top_peaks changes the analysis CSV's columns, so the open
        # day must be closed out rather than gaining a ragged header.
        columns = analysis_columns(settings.top_peaks)
        if columns != self.analysis.columns:
            self.analysis.flush()
            self.analysis.columns = columns
        log.info("settings in force: %s", settings.summary())

    def task_sample(self) -> None:
        hour = self.sensors.local_now().hour
        if not self.settings.logs_sensors_at(hour):
            return
        if not self.sensors.available:
            return
        record = self.sensors.sample()
        self.sensors.write(record)
        # Record what the day was configured to do, not only what happened, so
        # a day with the microphone scheduled but broken is distinguishable
        # from a day with the microphone deliberately switched off.
        self.master.update(
            self.today,
            sensor_samples="+1",
            sensor_hours=sum(self.settings.sensor_hours),
            mic_hours=sum(self.settings.microphone_hours),
            sample_rate_hz=self.settings.sample_rate,
            upload_frequency=self.settings.upload_frequency,
        )
        errors = "; ".join(v for k, v in record.items() if k.endswith("_error"))
        self.status.update(
            temperature_c=record.get("temperature_c"),
            humidity_pct=record.get("humidity_pct"),
            pressure_hpa=record.get("pressure_hpa"),
            voc_index=record.get("voc_index"),
            voc_raw=record.get("voc_raw"),
            sensor_error=errors,
        )
        self.status.push_history(
            temperature=record.get("temperature_c"),
            humidity=record.get("humidity_pct"),
            voc=record.get("voc_index"),
        )

    def task_record(self) -> None:
        """Start a recording if this hour is scheduled and none is running."""
        hour = self.sensors.local_now().hour
        if not self.settings.records_audio_at(hour) or self._recording.is_set():
            return
        if self.recorder.detect() is None:
            return

        self._recording.set()
        self._recording_started = time.monotonic()
        self.status.update(
            recording_total_s=self.settings.recording_minutes * 60,
            recording_elapsed_s=0.0,
        )
        worker = threading.Thread(target=self._record_once, name="recorder", daemon=True)
        worker.start()
        self._start_level_monitor()

    def _start_level_monitor(self) -> None:
        """Publish the input level once a second while recording.

        Reads the tail of the file arecord is writing rather than opening the
        device again — ALSA allows only one reader, so a second stream would
        simply fail.
        """
        def monitor() -> None:
            while self._recording.is_set() and not self._stop.is_set():
                partial = self.recorder.active_partial
                if partial is not None:
                    level = tail_level_dbfs(
                        partial,
                        window_s=1.0,
                        rate=self.recorder.active_rate or self.settings.sample_rate,
                        channels=self.recorder.active_channels or 1,
                    )
                    if level is not None:
                        self.status.update(live_db=round(level, 1))
                        self.status.push_history(live_db=level)
                        self.status.publish()
                self._stop.wait(1.0)
            # Leave the meter empty rather than frozen on the last value.
            self.status.update(live_db=None)
            self.status.publish()

        threading.Thread(target=monitor, name="level-monitor", daemon=True).start()

    def _record_once(self) -> None:
        try:
            path = self.recorder.record(
                sample_rate=self.settings.sample_rate,
                seconds=self.settings.recording_minutes * 60,
            )
        except RecorderError as exc:
            log.error("recording failed: %s", exc)
            self.master.update(self.today, notes=f"recording error: {exc}"[:120])
            return
        finally:
            self._recording.clear()

        size = path.stat().st_size
        self.master.update(
            self.today,
            audio_files="+1",
            audio_seconds=f"+{wav_duration_seconds(path) or 0:.0f}",
            audio_mib=f"+{size / 1048576:.2f}",
            sample_rate_hz=self.settings.sample_rate,
            mic_hours=sum(self.settings.microphone_hours),
        )
        # Analyse before queueing: the file may be deleted by housekeeping the
        # moment it uploads, and the analysis is the part we cannot recreate.
        self.analyse_recording(path)
        self.store.enqueue(path, "audio", size)
        self.health.ok("record")

    def analyse_recording(self, path: Path) -> None:
        """Run frequency analysis on one recording and log the result."""
        hour = self.sensors.local_now().hour
        if not self.settings.analyses_audio_at(hour):
            return
        device = self.recorder.device
        try:
            result = analyse_wav(
                path,
                top_peaks=self.settings.top_peaks,
                min_hz=self.settings.analysis_min_hz,
                max_hz=self.settings.analysis_max_hz,
                calibration_db_offset=self.settings.calibration_db_offset,
                device=str(device) if device else "",
            )
        except AnalysisError as exc:
            log.error("analysis failed for %s: %s", path.name, exc)
            self.master.update(self.today, notes=f"analysis error: {exc}"[:120])
            return

        self.analysis.write(result.as_row(self.settings.top_peaks), day=self.today)
        log.info("analysed %s: %s", path.name, result.summary())

        self._analysed.append(result.rms_dbfs)
        del self._analysed[:-50]
        self.master.update(
            self.today,
            analysed_files="+1",
            mean_dbfs=round(sum(self._analysed) / len(self._analysed), 1),
            dominant_hz=round(result.dominant_hz, 1),
        )
        self.status.update(
            last_recording=path.name,
            last_recording_utc=result.analysed_at,
            dominant_hz=result.dominant_hz,
            rms_dbfs=result.rms_dbfs,
            peak_dbfs=result.peak_dbfs,
            spl_estimate_db=result.spl_estimate_db,
            audio_device=result.device,
            analysis_min_hz=result.band_min_hz,
            analysis_max_hz=result.band_max_hz,
            analysed_today=len(self._analysed),
            peaks=[
                {"hz": round(p.frequency_hz, 1), "db": round(p.amplitude_db, 1)}
                for p in result.peaks
            ],
        )
        self.status.push_history(db=result.rms_dbfs)

    def start_mic_test(self, seconds: float) -> None:
        """Record a few seconds now and report what was heard, and from where.

        The point is to answer "is it really listening to the AudioMoth?" —
        so the result names the device explicitly and flags anything that is
        not one, rather than just showing a level and leaving the operator to
        assume.
        """
        if self._recording.is_set():
            # The microphone can only be opened once, and with an hourly
            # timetable a scheduled recording is often already running. Rather
            # than refuse — which answers nothing — report on the recording
            # that is happening, which is the same device and the same signal
            # the operator wanted to check.
            device = self.recorder.device
            self.status.update(
                mic_test_state="ok" if device else "failed",
                mic_test_message=(
                    "reporting on the scheduled recording already in progress"
                    if device else "a recording is starting; try again in a moment"
                ),
                mic_test_device=str(device) if device else "",
                mic_test_is_audiomoth=bool(device and device.is_audiomoth),
                mic_test_rate=self.recorder.active_rate or self.settings.sample_rate,
                mic_test_channels=self.recorder.active_channels or 1,
                mic_test_rms_dbfs=self.status.status.live_db,
                mic_test_peak_dbfs=None,
                mic_test_dominant_hz=None,
                mic_test_peaks=[],
                mic_test_seconds=self.status.status.recording_elapsed_s,
                mic_test_at=_utc_now(),
                mic_test_epoch=time.time(),
            )
            self.status.publish()
            log.info("mic test: reported on the in-progress recording (%s)", device)
            return

        self._recording.set()
        self.status.update(
            mic_test_state="running",
            mic_test_message=f"recording {seconds:g}s…",
            mic_test_seconds=seconds,
            mic_test_at=_utc_now(),
            mic_test_epoch=time.time(),
        )
        self.status.publish()
        threading.Thread(
            target=self._mic_test, args=(seconds,), name="mic-test", daemon=True
        ).start()
        self._start_level_monitor()

    def _mic_test(self, seconds: float) -> None:
        scratch = self.cfg.resolve("var/state") / "mic-test"
        recorder = Recorder(
            scratch, device_id=self.cfg.device.id, preference=self.settings.input_device
        )
        # The meter watches self.recorder, so point it at this one for the test.
        scheduled, self.recorder = self.recorder, recorder
        try:
            device = recorder.detect()
            if device is None:
                raise RecorderError(diagnose_missing_audiomoth())
            path = recorder.record(sample_rate=self.settings.sample_rate, seconds=seconds)
            result = analyse_wav(
                path,
                top_peaks=self.settings.top_peaks,
                min_hz=self.settings.analysis_min_hz,
                max_hz=self.settings.analysis_max_hz,
                device=str(device),
            )
            silent = result.rms_dbfs < -70
            clipped = result.peak_dbfs >= -0.5
            message = (
                "SILENT — the device is being read but no sound is reaching it"
                if silent
                else "CLIPPING — the input is too hot to analyse"
                if clipped
                else "microphone is working"
            )
            self.status.update(
                mic_test_state="ok",
                mic_test_message=message,
                mic_test_device=str(device),
                mic_test_is_audiomoth=device.is_audiomoth,
                mic_test_rate=result.sample_rate,
                mic_test_channels=recorder.usable_channels(),
                mic_test_rms_dbfs=result.rms_dbfs,
                mic_test_peak_dbfs=result.peak_dbfs,
                mic_test_dominant_hz=result.dominant_hz,
                mic_test_peaks=[
                    {"hz": round(p.frequency_hz, 1), "db": round(p.amplitude_db, 1)}
                    for p in result.peaks
                ],
                mic_test_seconds=result.duration_s,
                mic_test_at=_utc_now(),
                mic_test_epoch=time.time(),
            )
            log.info(
                "mic test: %s via %s — %s", message, device, result.summary()
            )
            path.unlink(missing_ok=True)
        except (RecorderError, AnalysisError, OSError) as exc:
            log.error("mic test failed: %s", exc)
            self.status.update(
                mic_test_state="failed",
                mic_test_message=str(exc)[:160],
                mic_test_at=_utc_now(),
                mic_test_epoch=time.time(),
            )
        finally:
            self.recorder = scheduled
            self._recording.clear()
            self.status.publish()

    def task_audio_ingest(self) -> None:
        """Pick up anything written to a mounted AudioMoth card."""
        staged = self.audio.scan()
        if not staged:
            return
        log.info("staged %d recording(s) from a card", len(staged))
        megabytes = sum(p.stat().st_size for p in staged) / 1048576
        seconds = sum(wav_duration_seconds(p) or 0 for p in staged)
        self.master.update(
            self.today,
            audio_files=f"+{len(staged)}",
            audio_mib=f"+{megabytes:.2f}",
            audio_seconds=f"+{seconds:.0f}",
        )

    def task_upload(self) -> None:
        """Start an upload pass in the background if one is due.

        Uploading is network-bound and effectively unbounded: rclone is allowed
        300s for a single file, and a pass works through the whole backlog. Run
        on the main loop that stops the watchdog heartbeat, and systemd kills
        the node mid-transfer — which is exactly what happened 21 times in one
        night. The loop must stay free to answer, so the pass gets its own
        thread, as recording does.
        """
        uploader = self.uploader
        if uploader is None or self._uploading.is_set():
            return
        now = time.monotonic()
        if self._last_upload and (now - self._last_upload) < self.settings.upload_interval_s:
            return
        self._last_upload = now

        self._uploading.set()
        threading.Thread(target=self._upload_once, name="uploader", daemon=True).start()

    def _upload_once(self) -> None:
        """One upload pass. Runs off the main loop; must never raise into it."""
        uploader = self.uploader
        if uploader is None:  # pragma: no cover - guarded by the caller
            self._uploading.clear()
            return
        try:
            # Anything more frequent than daily wants the day so far, not only
            # finished days, or a 6-hourly cadence would still ship nothing
            # until midnight.
            if self.settings.upload_interval_hours < 24:
                self.sensors.writer.publish_today()
                self.analysis.publish_today()

            uploaded, failed = uploader.run_once()
            if uploaded or failed:
                log.info("upload pass: %d ok, %d failed", uploaded, failed)
                self.master.update(self.today, uploaded_files=f"+{uploaded}")
            sync = self.sync
            if sync is not None:
                sync.push_master_log(self.master.path)
                self.status.update(online=sync.online, config_source=sync.source)
            self.status.update(last_upload_utc=_utc_now())
        except Exception:
            # A failed pass is a logged event, not a reason to lose the node;
            # the next pass retries whatever is still queued.
            log.exception("upload pass failed")
        finally:
            self._uploading.clear()

    def task_status(self) -> None:
        """Publish the snapshot the dashboard renders, and pet the watchdog."""
        # Checked here rather than in task_config because this runs every few
        # seconds: an operator who has just edited config.ini should see the
        # change take effect while they are still looking at the screen.
        requested = take_mic_test_request(self.cfg.resolve("var/state"))
        if requested is not None:
            log.info("microphone test requested (%.0fs)", requested)
            self.start_mic_test(requested)

        if take_refresh_request(self.cfg.resolve("var/state")):
            log.info("config refresh requested")
            sync = self.sync
            if sync is not None:
                sync.refresh(force=True)
                self._apply_settings(sync.config)
                self.status.update(config_fetched_utc=_utc_now())

        now_local = self.sensors.local_now()
        hour = now_local.hour
        counts = self.store.counts()
        row = self.master.read().get(self.today, {})
        sync = self.sync

        self.status.update(
            hour=hour,
            uptime_s=self.health.uptime_s,
            sensors_on_now=self.settings.logs_sensors_at(hour),
            microphone_on_now=self.settings.records_audio_at(hour),
            recording_now=self._recording.is_set(),
            recording_elapsed_s=(
                time.monotonic() - self._recording_started
                if self._recording.is_set() and self._recording_started
                else 0.0
            ),
            sensor_hours=[int(f) for f in self.settings.sensor_hours],
            microphone_hours=[int(f) for f in self.settings.microphone_hours],
            sample_rate=self.settings.sample_rate,
            upload_frequency=self.settings.upload_frequency,
            config_source=sync.source if sync else "local only",
            online=bool(sync and sync.online),
            queue_pending=int(counts.get("pending", 0)) + int(counts.get("failed", 0)),
            samples_today=int(float(row.get("sensor_samples") or 0)),
            recordings_today=int(float(row.get("audio_files") or 0)),
            queue_uploaded_today=int(float(row.get("uploaded_files") or 0)),
            healthy=not self.health.stuck_tasks(),
        )
        self.status.record_disk(self.cfg.spool_dir)
        self.status.publish()

    # --- loop ----------------------------------------------------------------

    def run(self) -> int:
        cfg = self.cfg

        # Settings first: everything below depends on the timetable, and the
        # cached copy means we start working before the network is even up.
        sync = self.sync
        if sync is not None:
            self._apply_settings(sync.load_cached())

        if cfg.sensors.enabled:
            self.sensors.open()

        self.recorder.cleanup_partials()
        device = self.recorder.detect()
        if device is None:
            log.warning(
                "no audio input — set the AudioMoth switch to USB/OFF and replug, or set "
                "input_device = any in config.ini to record from whatever is attached"
            )

        if sync is not None:
            try:
                log.info("cloud target: %s", self.uploader.check_credentials())  # type: ignore[union-attr]
            except UploadError as exc:
                log.error("cloud not reachable yet: %s", exc)
            sync.refresh(force=True)
            self._apply_settings(sync.config)

        # A restart is worth recording: a node that silently restarts every
        # ten minutes looks fine in the data and is badly broken.
        self.master.update(self.today, restarts="+1")

        now = time.monotonic()
        schedule: list[tuple[str, float, float]] = [
            ("sample", self.settings.sensor_interval_s, now),
            ("record", 20.0, now + 5.0),
            ("ingest", 300.0, now + 30.0),
            ("upload", 300.0, now + 60.0),
            ("status", STATUS_REFRESH_S, now + 2.0),
            ("config", CONFIG_REFRESH_S, now + CONFIG_REFRESH_S),
        ]
        tasks = {
            "sample": self.task_sample,
            "record": self.task_record,
            "ingest": self.task_audio_ingest,
            "upload": self.task_upload,
            "status": self.task_status,
            "config": self.task_config,
        }
        for name, interval, _ in schedule:
            self.health.register(name, interval)

        watchdog_s = SystemdNotifier.watchdog_interval_s()
        self.health.notifier.ready(f"running: {self.settings.summary()}")
        log.info(
            "started; %s",
            f"systemd watchdog every {watchdog_s:.0f}s" if watchdog_s else "no systemd watchdog",
        )

        # Wake at least this often so the watchdog is fed even when no task is
        # due — otherwise a long sensor interval would look like a hang.
        heartbeat_s = min(watchdog_s or 15.0, 15.0)

        while not self._stop.is_set():
            due_at = min(entry[2] for entry in schedule)
            timeout = min(due_at - time.monotonic(), heartbeat_s)
            if timeout > 0 and self._stop.wait(timeout):
                break

            now = time.monotonic()
            for index, (name, interval, next_at) in enumerate(schedule):
                if next_at > now:
                    continue
                try:
                    tasks[name]()
                    self.health.ok(name)
                except Exception:
                    # One failing task must never stop the others; persistent
                    # failure is caught by the watchdog rather than here.
                    log.exception("task %s failed", name)
                    self.health.failed(name)
                # The sample interval is operator-controlled and may have changed.
                if name == "sample":
                    interval = self.settings.sensor_interval_s
                # Skip missed slots rather than running a burst to catch up.
                following = next_at + interval
                if following <= now:
                    following = now + interval
                schedule[index] = (name, interval, following)

            self.health.pump(self._health_status())

        log.info("shutting down")
        return 0

    def _health_status(self) -> str:
        status = self.status.status
        parts = [f"{status.samples_today} readings"]
        if status.temperature_c is not None:
            parts.append(f"{status.temperature_c:.1f}C")
        if status.recording_now:
            parts.append("recording")
        parts.append(f"{status.queue_pending} queued")
        return ", ".join(parts)


def _install_signal_handlers(node: Node) -> None:
    def handler(signum: int, _frame: object) -> None:
        log.info("received %s", signal.Signals(signum).name)
        node.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handler)


# --- subcommands -------------------------------------------------------------


def cmd_run(cfg: Config) -> int:
    node = Node(cfg)
    _install_signal_handlers(node)
    try:
        return node.run()
    finally:
        node.close()


def cmd_sample(cfg: Config) -> int:
    manager = SensorManager(cfg)
    manager.open()
    try:
        if not manager.available:
            print(
                f"no sensors responded on /dev/i2c-{cfg.sensors.i2c_bus}\n"
                f"check wiring, and that dtparam=i2c_arm=on is set in "
                f"/boot/firmware/config.txt",
                file=sys.stderr,
            )
            return 1
        print(json.dumps(manager.sample(), indent=2))
        return 0
    finally:
        manager.close()


def cmd_record(cfg: Config, *, seconds: float, rate: int | None) -> int:
    settings = _effective_settings(cfg)
    recorder = Recorder(
        cfg.spool_dir / "audio",
        device_id=cfg.device.id,
        preference=settings.input_device,
    )
    if recorder.detect() is None:
        print(
            "no AudioMoth capture device found.\n"
            "  * set the AudioMoth switch to USB/OFF and reconnect the cable\n"
            "  * it must be running AudioMoth-USB-Microphone firmware; the stock\n"
            "    Basic firmware talks only to the configuration app in that position\n"
            "  * 'iot-node audio-test' lists everything the system can capture from",
            file=sys.stderr,
        )
        return 1
    try:
        path = recorder.record(sample_rate=rate or settings.sample_rate, seconds=seconds)
    except RecorderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    duration = wav_duration_seconds(path)
    print(f"{path}  ({path.stat().st_size / 1048576:.1f} MiB, {duration or 0:.1f}s)")
    return 0


def cmd_analyse(cfg: Config, targets: list[Path]) -> int:
    """Analyse one or more recordings and print the result."""
    settings = _effective_settings(cfg)
    files = [p for p in targets if p.is_file()]
    if not files:
        # No file given: take the most recent recording we still hold.
        spool = sorted(
            (cfg.spool_dir / "audio").glob("*.WAV"), key=lambda p: p.stat().st_mtime
        )
        files = spool[-1:]
    if not files:
        print("no recording to analyse — pass a .WAV path", file=sys.stderr)
        return 1

    failures = 0
    for path in files:
        try:
            result = analyse_wav(
                path,
                top_peaks=settings.top_peaks,
                min_hz=settings.analysis_min_hz,
                max_hz=settings.analysis_max_hz,
                calibration_db_offset=settings.calibration_db_offset,
            )
        except AnalysisError as exc:
            print(f"{path.name}: {exc}", file=sys.stderr)
            failures += 1
            continue

        print(f"\n{path.name}")
        print(f"  {result.duration_s:.1f}s at {result.sample_rate} Hz")
        print(f"  level      {result.rms_dbfs:.1f} dBFS rms, {result.peak_dbfs:.1f} dBFS peak")
        if settings.calibration_db_offset:
            print(f"  estimated  {result.spl_estimate_db:.1f} dB SPL "
                  f"(offset {settings.calibration_db_offset:+.1f})")
        else:
            print("  SPL        uncalibrated — set calibration_db_offset to convert")
        print(f"  dominant   {result.dominant_hz:.1f} Hz")
        print(f"  band       {result.band_min_hz:g}-{result.band_max_hz:g} Hz")
        print("  peaks:")
        for index, peak in enumerate(result.peaks, start=1):
            bar = "█" * max(1, int((peak.amplitude_db + 90) / 4))
            print(f"    {index}. {peak.frequency_hz:>8.1f} Hz  "
                  f"{peak.amplitude_db:>7.1f} dB  {bar}")
    return 1 if failures else 0


def cmd_audio_test(cfg: Config) -> int:
    """Probe every rate the AudioMoth firmware claims to support."""
    devices = list_capture_devices()
    print("capture devices:")
    for device in devices:
        mark = "  <- AudioMoth" if device.is_audiomoth else ""
        print(f"  card {device.card}: {device.name} [{device.hw}]{mark}")
    if not devices:
        print("  (none)")

    target = select_device(_effective_settings(cfg).input_device)
    if target is None:
        sys.stdout.flush()  # keep the device list above the advice
        print(
            "\nNo AudioMoth found. It appears as a capture device only when:\n"
            "  1. it is running AudioMoth-USB-Microphone firmware, and\n"
            "  2. its switch is in the USB/OFF position, and\n"
            "  3. the USB cable is a data cable, not charge-only.\n"
            "With the stock Basic firmware, USB/OFF only talks to the AudioMoth\n"
            "configuration app — it records to its own SD card in CUSTOM/DEFAULT\n"
            "instead, which this node ingests via 'iot-node scan'.",
            file=sys.stderr,
        )
        return 1

    print(f"\nprobing {target.name} at each supported rate (native, no resampling):")
    results = probe_sample_rates(target)
    for rate, error in results.items():
        status = "ok" if error is None else f"unsupported — {error[:60]}"
        print(f"  {rate:>7} Hz  {status}")

    working = [r for r, err in results.items() if err is None]
    print(f"\nusable rates: {', '.join(str(r) for r in working) or 'none'}")
    if working:
        print(f"set 'sample_rate' in config.ini to one of these (currently "
              f"{_effective_settings(cfg).sample_rate}).")
    return 0 if working else 1


def cmd_scan(cfg: Config) -> int:
    with Store(cfg.state_db) as store:
        cfg.ensure_dirs()
        ingest = AudioIngest(cfg, store)
        sources = ingest.source_dirs()
        print(f"source directories: {', '.join(str(s) for s in sources) or '(none found)'}")
        for mount in find_audiomoth_mounts():
            settings = parse_audiomoth_config(mount)
            print(f"AudioMoth card at {mount}: {len(settings)} config setting(s)")
            for key in ("Device ID", "Firmware", "Sample rate (Hz)", "Recording duration (s)"):
                if key in settings:
                    print(f"  {key}: {settings[key]}")
        staged = ingest.scan()
        print(f"staged {len(staged)} new recording(s)")
        return 0


def cmd_upload(cfg: Config) -> int:
    cfg.ensure_dirs()
    with Store(cfg.state_db) as store:
        try:
            uploader = Uploader(cfg, store)
        except UploadError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        try:
            print(f"cloud target: {uploader.check_credentials()}")
            uploaded, failed = uploader.run_once()
            print(f"uploaded {uploaded}, failed {failed}")
            master = MasterLog(cfg.resolve("var/state") / "master_log.csv")
            if uploaded:
                # Same accounting as the daemon, so a manual drain still shows
                # up in the master log rather than silently going missing.
                master.update(datetime.now().strftime("%Y-%m-%d"), uploaded_files=f"+{uploaded}")
            root = cfg.cloud.remote_root.format(
                device_id=cfg.device.id, site=cfg.device.site or cfg.device.id
            )
            sync = ConfigSync(uploader.backend, root, cfg.resolve("var/state"))
            sync.push_master_log(master.path)
            return 1 if failed else 0
        except UploadError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        finally:
            uploader.close()


def _effective_settings(cfg: Config, *, fetch: bool = False) -> RemoteConfig:
    """The timetable currently in force: from the drive if asked, else cached."""
    cfg.ensure_dirs()
    with Store(cfg.state_db) as store:
        try:
            uploader = Uploader(cfg, store)
        except UploadError:
            uploader = None
        root = cfg.cloud.remote_root.format(
            device_id=cfg.device.id, site=cfg.device.site or cfg.device.id
        )
        if uploader is None:
            sync = ConfigSync.__new__(ConfigSync)
            sync.cache_path = cfg.resolve("var/state") / "config.ini"
            return sync.load_cached()
        try:
            sync = ConfigSync(uploader.backend, root, cfg.resolve("var/state"))
            settings = sync.load_cached()
            if fetch:
                # refresh() reports whether anything *changed*; either way
                # sync.config is now authoritative, including after it has
                # seeded the drive with the default file.
                sync.refresh(force=True)
                settings = sync.config
            return settings
        finally:
            uploader.close()


def cmd_refresh(cfg: Config) -> int:
    """Ask a running node to re-read config.ini now."""
    state_dir = cfg.resolve("var/state")
    if not request_refresh(state_dir):
        return 1
    print("config refresh requested — the running node will pick it up within seconds")
    print("(if no node is running, this takes effect when one next starts)")
    return 0


def cmd_mic_test(cfg: Config, *, seconds: float = 5.0) -> int:
    """Ask the running node to record a few seconds and report on the input."""
    from .status import read_status

    state_dir = cfg.resolve("var/state")
    if not request_mic_test(state_dir, seconds):
        return 1
    print(f"microphone test requested ({seconds:g}s) — waiting for the node…")

    status_path = state_dir / "status.json"
    deadline = time.monotonic() + seconds + 60
    seen_running = False
    while time.monotonic() < deadline:
        time.sleep(1.0)
        status = read_status(status_path)
        if status is None or status.mic_test_age_s() > 120:
            continue
        if status.mic_test_state == "running":
            seen_running = True
            continue
        if status.mic_test_state in ("ok", "failed") and seen_running:
            return _print_mic_test(status)

    print(
        "no response from the node. Is it running?  systemctl status iot-node",
        file=sys.stderr,
    )
    return 1


def _print_mic_test(status: Status) -> int:
    if status.mic_test_state != "ok":
        print(f"\nFAILED: {status.mic_test_message}", file=sys.stderr)
        return 1

    print(f"\n  device      {status.mic_test_device}")
    print(
        "  identity    "
        + ("AudioMoth ✓" if status.mic_test_is_audiomoth
           else "NOT an AudioMoth — this is a substitute input")
    )
    print(f"  captured    {status.mic_test_seconds:.1f}s at {status.mic_test_rate} Hz, "
          f"{status.mic_test_channels} ch")
    print(f"  level       {status.mic_test_rms_dbfs:.1f} dBFS rms, "
          f"{status.mic_test_peak_dbfs:.1f} dBFS peak")
    print(f"  dominant    {status.mic_test_dominant_hz:.0f} Hz")
    for index, peak in enumerate(status.mic_test_peaks[:5], start=1):
        bar = "█" * max(1, int((peak["db"] + 90) / 4))
        print(f"    {index}. {peak['hz']:>8.0f} Hz  {peak['db']:>7.1f} dB  {bar}")
    print(f"\n  {status.mic_test_message}")
    return 0


def cmd_config(cfg: Config) -> int:
    settings = _effective_settings(cfg, fetch=True)
    hours = "".join(str(h % 10) for h in range(24))
    print(f"timetable (local time, {cfg.device.timezone}):")
    print(f"  hour        {hours}")
    print(f"  sensors     {''.join('1' if f else '0' for f in settings.sensor_hours)}")
    print(f"  microphone  {''.join('1' if f else '0' for f in settings.microphone_hours)}")
    print(f"\nsample_rate        {settings.sample_rate} Hz")
    print(f"recording_minutes  {settings.recording_minutes:g}")
    print(f"sensor_interval    {settings.sensor_interval_s:g}s")
    print(f"upload frequency   {settings.upload_frequency}")
    print(f"\nsummary: {settings.summary()}")
    for warning in settings.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


def cmd_status(cfg: Config) -> int:
    cfg.ensure_dirs()
    settings = _effective_settings(cfg)
    with Store(cfg.state_db) as store:
        counts = store.counts()
        usage = shutil.disk_usage(cfg.spool_dir)
        spooled = sum(1 for p in cfg.spool_dir.rglob("*") if p.is_file())
        hour = datetime.now().hour

        remote_root = cfg.cloud.remote_root.format(
            device_id=cfg.device.id, site=cfg.device.site or cfg.device.id
        )
        print(f"device:   {cfg.device.id} ({cfg.device.site or 'no site'})")
        print(f"cloud:    {cfg.cloud.provider} → {remote_root}/{cfg.device.id}/<date>/")
        print(f"now:      hour {hour:02d} — sensors "
              f"{'ON' if settings.logs_sensors_at(hour) else 'off'}, microphone "
              f"{'ON' if settings.records_audio_at(hour) else 'off'}")
        device = select_device(settings.input_device)
        print(f"audio in: {device or 'none (AudioMoth in USB/OFF mode?)'}")
        print(f"spool:    {cfg.spool_dir} — {spooled} file(s)")
        print("queue:    " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "empty"))
        print(f"disk:     {usage.free / 1e9:.1f} GB free of {usage.total / 1e9:.1f} GB "
              f"({usage.free / usage.total * 100:.0f}%)")
        print(f"i2c:      /dev/i2c-{cfg.sensors.i2c_bus} "
              f"{'present' if Path(f'/dev/i2c-{cfg.sensors.i2c_bus}').exists() else 'MISSING'}")

        master = MasterLog(cfg.resolve("var/state") / "master_log.csv")
        rows = master.read()
        if rows:
            print("\nmaster log (last 3 days):")
            for day in sorted(rows)[-3:]:
                row = rows[day]
                print(
                    f"  {day}  samples={row.get('sensor_samples', '0'):>5} "
                    f"audio={row.get('audio_files', '0'):>4} files "
                    f"({row.get('audio_mib', '0')} MiB) "
                    f"uploaded={row.get('uploaded_files', '0')}"
                )
    return 0


def build_parser() -> argparse.ArgumentParser:
    """The CLI parser.

    Separate from main() so the argument wiring can be tested without running
    anything. The ordering of the two positionals below is load-bearing:
    argparse matches consecutive optional positionals left to right, so
    declaring `files` before `command` makes it swallow the subcommand name and
    every command silently falls through to the default.
    """
    parser = argparse.ArgumentParser(
        prog="iot-node",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-c", "--config", type=Path, help="path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true", help="log at DEBUG")
    parser.add_argument("--seconds", type=float, default=10.0, help="record: length in seconds")
    parser.add_argument("--rate", type=int, help="record: sample rate override")
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=[
            "run", "sample", "record", "analyse", "audio-test",
            "scan", "upload", "config", "status", "refresh", "mic-test",
        ],
    )
    parser.add_argument("files", nargs="*", type=Path, help="analyse: WAV files to analyse")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    cfg = load_config(args.config)
    if args.verbose:
        cfg.logging.level = "DEBUG"
    setup_logging(cfg)

    if args.command == "record":
        return cmd_record(cfg, seconds=args.seconds, rate=args.rate)
    if args.command == "analyse":
        return cmd_analyse(cfg, args.files)
    if args.command == "refresh":
        return cmd_refresh(cfg)
    if args.command == "mic-test":
        return cmd_mic_test(cfg, seconds=args.seconds)

    handlers = {
        "run": cmd_run,
        "sample": cmd_sample,
        "audio-test": cmd_audio_test,
        "scan": cmd_scan,
        "upload": cmd_upload,
        "config": cmd_config,
        "status": cmd_status,
    }
    return handlers[args.command](cfg)


if __name__ == "__main__":
    raise SystemExit(main())


# ---------------------------------------------------------------------------
#  Bee Node — hive environmental and acoustic monitoring
#
#  Developed by Mohammad (Amin) BarzegarTabrizi
#  mabiran@gmail.com
# ---------------------------------------------------------------------------
