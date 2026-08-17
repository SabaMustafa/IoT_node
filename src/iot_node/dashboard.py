"""BEE NODE — the always-on screen.

A fullscreen Tk dashboard showing what the node is doing right now. It reads
``var/state/status.json`` and nothing else: it never touches the sensors, the
spool or the network, so it cannot slow the node down or lose data if it
crashes. If the snapshot goes stale the screen says so plainly rather than
displaying old numbers as if they were current — a dashboard that lies is worse
than no dashboard.

Tk is used because it is in the standard library and runs on a bare Pi desktop
with no browser, no packaging and no network. The whole thing is drawn on a
canvas so it scales to whatever screen is attached.

    iot-node-screen               # an ordinary, minimisable window
    iot-node-screen --fullscreen  # kiosk mode, for a dedicated display

Esc or q closes it; F11 toggles fullscreen. It is a normal window — never on
top, never grabbing focus — so it can be minimised and ignored like any other.
"""

from __future__ import annotations

import argparse
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import load_config
from .status import Status, read_status, request_refresh

REFRESH_MS = 2000

# Warm, high-contrast palette: readable across a shed at a glance, and easy on
# the eyes as a screen that is on permanently.
BG = "#12100c"
PANEL = "#1c1913"
INK = "#f5edda"
MUTED = "#8a8172"
HONEY = "#ffc53d"
AMBER = "#f0932b"
GOOD = "#7bc86c"
WARN = "#e8a33d"
BAD = "#e05c4a"
LINE = "#2e2a22"


class Dashboard:
    """An ordinary window by default.

    Fullscreen is opt-in (``--fullscreen``) rather than the default: a kiosk
    window that cannot be minimised is a nuisance on a Pi you also use for
    other things. Nothing here grabs focus or sits above other windows, so it
    behaves like any application — minimise it, move it, put it on another
    workspace.
    """

    def __init__(self, status_path: Path, *, fullscreen: bool = False) -> None:
        self.status_path = status_path
        self.root = tk.Tk()
        self.root.title("Bee Node")
        self.root.configure(bg=BG)
        self.root.minsize(720, 480)
        self._fullscreen = False
        self._closing = False
        self._after_id: str | None = None
        self._notice = ""
        self._notice_until = 0.0
        self._button: tuple[float, float, float, float] | None = None

        self.root.geometry("1280x720")
        if fullscreen:
            self._set_fullscreen(True)

        for sequence in ("<Escape>", "<q>", "<Q>"):
            self.root.bind(sequence, self._on_quit)
        self.root.bind("<F11>", self._toggle_fullscreen)
        for sequence in ("<r>", "<R>"):
            self.root.bind(sequence, self._request_refresh)
        # Closing via the window manager must take the same tidy path.
        self.root.protocol("WM_DELETE_WINDOW", self._on_quit)

        self.canvas = tk.Canvas(self.root, bg=BG, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Button-1>", self._on_click)

    def _set_fullscreen(self, on: bool) -> None:
        self._fullscreen = on
        try:
            self.root.attributes("-fullscreen", on)
            # Hide the pointer only while genuinely in kiosk mode.
            self.root.config(cursor="none" if on else "")
        except tk.TclError:
            pass

    def _toggle_fullscreen(self, _event: Any = None) -> None:
        self._set_fullscreen(not self._fullscreen)

    def _request_refresh(self, _event: Any = None) -> None:
        """Ask the node to re-read config.ini now.

        Writes a small file the node watches. The dashboard still knows nothing
        about the node beyond a directory path — no socket, no import, no way
        to disturb it beyond asking this one question.
        """
        ok = request_refresh(self.status_path.parent)
        self._notice = (
            "config refresh requested…" if ok else "could not request a refresh"
        )
        self._notice_until = time.monotonic() + 8
        self.tick_now()

    def tick_now(self) -> None:
        """Redraw immediately rather than waiting for the next timer tick."""
        try:
            self.render(read_status(self.status_path))
        except tk.TclError:
            pass

    def _on_quit(self, _event: Any = None) -> None:
        """Shut down without the refresh timer firing into a dead window."""
        if self._closing:
            return
        self._closing = True
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None
        try:
            self.root.quit()      # leave mainloop first…
            self.root.destroy()   # …then tear the widgets down
        except tk.TclError:
            pass

    # --- drawing helpers -----------------------------------------------------

    def _panel(self, x: int, y: int, w: int, h: int, title: str) -> None:
        self.canvas.create_rectangle(x, y, x + w, y + h, fill=PANEL, outline=LINE, width=1)
        self.canvas.create_text(
            x + 16, y + 14, text=title.upper(), anchor="nw", fill=MUTED,
            font=("DejaVu Sans", 10, "bold"),
        )

    def _text(
        self, x: float, y: float, text: str, *, size: int, fill: str,
        bold: bool = False, anchor: str = "nw", mono: bool = False,
    ) -> tuple[float, float]:
        """Draw text and return the bottom-right of what was actually drawn.

        Positions are measured from the rendered glyphs rather than guessed
        from character counts — the fonts here are proportional, so guessing
        makes units collide with the numbers they belong to.
        """
        family = "DejaVu Sans Mono" if mono else "DejaVu Sans"
        item = self.canvas.create_text(
            (x, y), text=text, anchor=anchor, fill=fill,  # type: ignore[call-overload]
            font=(family, size, "bold" if bold else "normal"),
        )
        box = self.canvas.bbox(item)
        return (box[2], box[3]) if box else (x, y)

    def _reading(
        self, x: int, y: int, w: int, h: int,
        label: str, value: str, unit: str, colour: str = INK,
    ) -> None:
        self._text(x + 16, y + 14, label.upper(), size=11, fill=MUTED, bold=True)
        # Scale the number to the panel so it stays readable on any screen.
        size = max(24, min(52, int(h * 0.34)))
        right, bottom = self._text(x + 16, y + 40, value, size=size, fill=colour, bold=True)
        if unit:
            self._text(right + 10, bottom - 20, unit, size=13, fill=MUTED)

    def _refresh_button(self, x: float, y: float) -> None:
        """A clickable 'refresh config' control, with a transient confirmation."""
        w, h = 174.0, 30.0
        showing_notice = time.monotonic() < self._notice_until
        edge = GOOD if showing_notice else MUTED
        self.canvas.create_rectangle(x, y, x + w, y + h, outline=edge, width=1, fill=PANEL)
        self.canvas.create_text(
            x + w / 2, y + h / 2,
            text="✓ REQUESTED" if showing_notice else "↻  REFRESH CONFIG  (r)",
            fill=GOOD if showing_notice else INK, font=("DejaVu Sans", 10, "bold"),
        )
        # Remember where it is so clicks can be matched against it.
        self._button = (x, y, x + w, y + h)

    def _on_click(self, event: Any) -> None:
        if self._button is None:
            return
        x0, y0, x1, y1 = self._button
        if x0 <= event.x <= x1 and y0 <= event.y <= y1:
            self._request_refresh()

    def _sparkline(
        self, x: int, y: int, w: int, h: int, values: list[float], colour: str
    ) -> None:
        """A minimal trend line. Flat when there is nothing to say."""
        if len(values) < 2:
            self.canvas.create_line(x, y + h / 2, x + w, y + h / 2, fill=LINE, width=1)
            return
        low, high = min(values), max(values)
        span = high - low
        if span <= 0:
            span = 1.0
            low -= 0.5
        step = w / (len(values) - 1)
        points: list[float] = []
        for index, value in enumerate(values):
            points.append(x + index * step)
            points.append(y + h - ((value - low) / span) * h)
        self.canvas.create_line(*points, fill=colour, width=2, smooth=True)

    def _timetable(self, x: int, y: int, w: int, hours: list[int], now: int, colour: str) -> None:
        """24 blocks, one per hour, with the current hour outlined."""
        cell = w / 24.0
        for hour in range(24):
            on = hour < len(hours) and bool(hours[hour])
            left = x + hour * cell
            self.canvas.create_rectangle(
                left + 1, y, left + cell - 1, y + 18,
                fill=colour if on else LINE, outline="",
            )
            if hour == now:
                self.canvas.create_rectangle(
                    left + 1, y - 3, left + cell - 1, y + 21, outline=INK, width=2,
                )
            if hour % 6 == 0:
                self.canvas.create_text(
                    left + cell / 2, y + 30, text=f"{hour:02d}", fill=MUTED,
                    font=("DejaVu Sans", 8),
                )

    # --- the screen ----------------------------------------------------------

    def render(self, status: Status | None) -> None:
        self.canvas.delete("all")
        width = self.canvas.winfo_width() or 1024
        height = self.canvas.winfo_height() or 600
        margin = 16
        column = (width - margin * 5) / 4

        header_h = 84
        footer_h = 76
        self._header(status, width, margin, header_h)

        if status is None:
            self._text(width / 2, height / 2, "waiting for the node to publish its first status…",
                       size=16, fill=MUTED, anchor="center")
            return

        stale = status.is_stale()

        # Proportional rows so the screen fills whatever display is attached,
        # rather than hugging the top of a 1080p panel.
        top = header_h + margin
        body_h = height - top - footer_h - margin
        env_h = int(body_h * 0.36)
        lower_y = top + env_h + margin
        lower_h = int(body_h - env_h - margin)

        # --- environment row ---
        readings = [
            ("temperature", status.temperature_c, "°C", "{:.1f}", HONEY,
             status.history_temperature),
            ("humidity", status.humidity_pct, "%RH", "{:.1f}", "#6fc3df",
             status.history_humidity),
            ("pressure", status.pressure_hpa, "hPa", "{:.0f}", "#b39ddb", []),
            ("air quality", status.voc_index, "VOC", "{:.0f}", GOOD, status.history_voc),
        ]
        for index, (label, value, unit, fmt, colour, history) in enumerate(readings):
            x = int(margin + index * (column + margin))
            self._panel(x, top, int(column), env_h, "")
            text = "—" if value is None else fmt.format(value)
            self._reading(
                x, top, int(column), env_h, label, text, unit, MUTED if stale else colour
            )
            spark_h = max(24, int(env_h * 0.28))
            self._sparkline(
                x + 16, top + env_h - spark_h - 16, int(column) - 32, spark_h,
                history[-60:], colour,
            )

        # --- acoustics ---
        panel_w = int(column * 2 + margin)
        self._panel(margin, lower_y, panel_w, lower_h, "hive acoustics")
        if status.rms_dbfs is None:
            self._text(margin + 16, lower_y + 52, "no recording analysed yet",
                       size=14, fill=MUTED)
        else:
            dominant = f"{status.dominant_hz:.0f}" if status.dominant_hz else "—"
            self._text(margin + 16, lower_y + 40, dominant, size=34, fill=AMBER, bold=True)
            self._text(margin + 16, lower_y + 88, "Hz dominant", size=11, fill=MUTED)

            level = (
                status.spl_estimate_db
                if status.spl_estimate_db is not None
                else status.rms_dbfs
            )
            level_x = margin + int(panel_w * 0.28)
            self._text(level_x, lower_y + 40, f"{level:.1f}", size=34, fill=INK, bold=True)
            self._text(
                level_x, lower_y + 88,
                "dB (est.)" if status.spl_estimate_db != status.rms_dbfs
                else "dBFS (uncalibrated)",
                size=11, fill=MUTED,
            )
            if status.audio_device:
                self._text(margin + 16, lower_y + lower_h - 22,
                           status.audio_device[:60], size=9, fill=MUTED)

            # Peak table, sized to whatever room the panel has.
            rows_top = lower_y + 118
            row_h = max(16, min(28, int((lower_h - 150) / max(1, len(status.peaks)))))
            for index, peak in enumerate(status.peaks[:5]):
                y = rows_top + index * row_h
                hz = peak.get("hz", 0)
                db = peak.get("db", 0)
                self._text(margin + 16, y, f"{index + 1}.", size=10, fill=MUTED)
                self._text(margin + 44, y, f"{hz:>7.0f} Hz", size=11, fill=INK, mono=True)
                self._text(margin + 140, y, f"{db:>6.1f} dB", size=11, fill=MUTED, mono=True)
                # Bar proportional to level, floored at -90 dB.
                bar = max(0.0, min(1.0, (db + 90) / 90))
                bar_x = margin + 240
                self.canvas.create_rectangle(
                    bar_x, y + 3, bar_x + bar * (panel_w - 270), y + row_h - 6,
                    fill=AMBER, outline="",
                )

        # --- schedule ---
        schedule_x = int(margin * 3 + column * 2)
        schedule_w = int(column * 2 + margin)
        self._panel(schedule_x, lower_y, schedule_w, lower_h, "timetable (local hours)")
        self._text(schedule_x + 16, lower_y + 42, "sensors", size=11, fill=MUTED)
        self._timetable(
            schedule_x + 16, lower_y + 62, schedule_w - 32,
            status.sensor_hours, status.hour, GOOD,
        )
        self._text(schedule_x + 16, lower_y + 116, "microphone", size=11, fill=MUTED)
        self._timetable(
            schedule_x + 16, lower_y + 136, schedule_w - 32,
            status.microphone_hours, status.hour, AMBER,
        )
        self._text(
            schedule_x + 16, lower_y + lower_h - 46,
            f"{status.sample_rate} Hz   ·   upload every {status.upload_frequency}   ·   "
            f"config from {status.config_source}",
            size=10, fill=MUTED,
        )
        self._refresh_button(schedule_x + schedule_w - 190, lower_y + lower_h - 52)
        if status.sensor_error:
            self._text(schedule_x + 16, lower_y + lower_h - 26,
                       status.sensor_error[:70], size=9, fill=BAD)

        self._footer(status, width, height, margin)

    def _header(self, status: Status | None, width: int, margin: int, bottom: int) -> None:
        # Each piece is placed after the previous one's measured extent, so the
        # wordmark and the device name cannot run into each other.
        right, _ = self._text(margin, 22, "BEE", size=32, fill=HONEY, bold=True)
        right, base = self._text(right + 4, 22, "NODE", size=32, fill=INK, bold=True)
        if status is not None:
            subtitle = status.device_id + (f" · {status.site}" if status.site else "")
            self._text(right + 20, base - 26, subtitle, size=13, fill=MUTED)

        now = datetime.now()
        self._text(width - margin, 20, now.strftime("%H:%M:%S"),
                   size=28, fill=INK, bold=True, anchor="ne")
        self._text(width - margin, 60, now.strftime("%A %d %B %Y"),
                   size=11, fill=MUTED, anchor="ne")

        if status is not None:
            self._status_pills(status, width, margin)
        self.canvas.create_line(margin, bottom, width - margin, bottom, fill=LINE)

    def _status_pills(self, status: Status, width: int, margin: int) -> None:
        stale = status.is_stale()
        pills: list[tuple[str, str]] = []
        if stale:
            pills.append((f"NODE SILENT {status.age_s() / 60:.0f}m", BAD))
        elif not status.healthy:
            pills.append(("UNHEALTHY", BAD))
        else:
            pills.append(("RUNNING", GOOD))
        pills.append(("REC" if status.recording_now else "idle",
                      BAD if status.recording_now else MUTED))
        pills.append(("online" if status.online else "OFFLINE",
                      GOOD if status.online else WARN))

        x = width - margin - 320
        for text, colour in pills:
            w = 8 + len(text) * 8
            self.canvas.create_rectangle(x, 8, x + w, 28, outline=colour, width=1)
            self.canvas.create_text(
                x + w / 2, 18, text=text, fill=colour, font=("DejaVu Sans", 9, "bold")
            )
            x += w + 8

    def _footer(self, status: Status, width: int, height: int, margin: int) -> None:
        y = height - 52
        self.canvas.create_line(margin, y - 12, width - margin, y - 12, fill=LINE)

        disk_colour = GOOD if status.disk_free_pct > 20 else (
            WARN if status.disk_free_pct > 10 else BAD
        )
        cells = [
            (f"{status.samples_today}", "readings today", INK),
            (f"{status.recordings_today}", "recordings today", INK),
            (f"{status.queue_pending}", "queued to upload",
             WARN if status.queue_pending > 20 else INK),
            (f"{status.disk_free_gb:.1f} GB", f"card free ({status.disk_free_pct:.0f}%)",
             disk_colour),
        ]
        if status.cloud_free_gb is not None:
            cells.append((f"{status.cloud_free_gb:.1f} GB", "cloud free",
                          GOOD if status.cloud_free_gb > 2 else WARN))

        step = (width - margin * 2) / len(cells)
        for index, (value, label, colour) in enumerate(cells):
            x = margin + index * step
            self.canvas.create_text(
                x, y, text=value, anchor="nw", fill=colour, font=("DejaVu Sans", 16, "bold")
            )
            self.canvas.create_text(
                x, y + 24, text=label, anchor="nw", fill=MUTED, font=("DejaVu Sans", 9)
            )

        note = status.notes or (f"last upload {status.last_upload_utc}"
                                if status.last_upload_utc else "")
        if note:
            self.canvas.create_text(
                width - margin, height - 8, text=note[:90], anchor="se", fill=MUTED,
                font=("DejaVu Sans", 8),
            )

    # --- loop ----------------------------------------------------------------

    def tick(self) -> None:
        if self._closing:
            return
        try:
            self.render(read_status(self.status_path))
        except tk.TclError:
            return  # the window went away mid-render; nothing to report
        except Exception as exc:  # a broken screen must never take the node down
            try:
                self.canvas.delete("all")
                self._text(20, 20, f"dashboard error: {exc}", size=11, fill=BAD)
            except tk.TclError:
                return
        if not self._closing:
            self._after_id = self.root.after(REFRESH_MS, self.tick)

    def run(self) -> int:
        self._after_id = self.root.after(100, self.tick)
        try:
            self.root.mainloop()
        finally:
            self._on_quit()
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="iot-node-screen", description=__doc__)
    parser.add_argument("-c", "--config", type=Path, help="path to config.yaml")
    parser.add_argument(
        "--fullscreen", action="store_true", help="start as a fullscreen kiosk window"
    )
    parser.add_argument(
        "--windowed", action="store_true", help="(default) start as an ordinary window"
    )
    parser.add_argument("--status", type=Path, help="path to status.json")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    status_path = args.status or (cfg.resolve("var/state") / "status.json")

    try:
        dashboard = Dashboard(status_path, fullscreen=args.fullscreen and not args.windowed)
    except tk.TclError as exc:
        print(f"cannot open a display: {exc}", file=__import__("sys").stderr)
        return 1
    return dashboard.run()


if __name__ == "__main__":
    raise SystemExit(main())


# ---------------------------------------------------------------------------
#  Bee Node — developed by Mohammad (Amin) BarzegarTabrizi <mabiran@gmail.com>
# ---------------------------------------------------------------------------
