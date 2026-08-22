"""APP 1 - the recorder.

Configure a session, then sit in the background taking one photo per slot.
This process never imports cv2: every photo is taken by a throwaway
grabber.py subprocess, which keeps the resident footprint at roughly 40 MB and
guarantees the camera is released between shots.
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import common
import theme
from common import (BUSY, NO_DEVICE, NO_FRAME, OK, SKIPPED, STATE_COMPLETED,
                    STATE_RUNNING, STATE_STOPPED, TIMEOUT, Session,
                    fmt_clock, fmt_duration, fmt_size)

HERE = os.path.dirname(os.path.abspath(__file__))
GRABBER = os.path.join(HERE, "grabber.py")

SHOT_TIMEOUT = 15
PROBE_TIMEOUT = 60
WARN_AFTER_FAILURES = 5

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# Before Tk exists, or Windows bitmap-scales the whole app and the type looks
# soft next to native windows.
theme.enable_dpi_awareness()


def _spawn(args, timeout):
    return subprocess.run([sys.executable, GRABBER] + args,
                          capture_output=True, timeout=timeout,
                          creationflags=CREATE_NO_WINDOW)


class CaptureApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Study Time-Lapse")
        self.resizable(False, False)
        self.configure(bg=theme.BG)
        theme.apply_icon(self)
        self.scale = theme.dpi_scale(self)
        theme.tune_tk_scaling(self, self.scale)
        theme.style_widgets(self)
        self.after(10, lambda: theme.apply_dark_chrome(self))

        self.view = "compact"
        self.top_var = tk.BooleanVar(value=True)
        self._ended = None

        self.cameras = []
        self.camera_index = tk.IntVar(value=-1)
        self.thumbs = {}
        self.cam_tiles = {}

        self.session = None
        self.running = False
        self.clock = None
        self.frames_ok = 0
        self.grab_busy = False
        self.consecutive_fail = 0
        self.results = queue.Queue()
        self.ticks = 0

        self.setup_frame = None
        self.run_frame = None
        self._build_setup()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(200, self.offer_resume)

    # ------------------------------------------------------------------
    # setup view
    # ------------------------------------------------------------------
    def _build_setup(self):
        f = ttk.Frame(self, padding=self.px(14))
        f.grid(sticky="nsew")
        self.setup_frame = f

        ttk.Label(f, text="Study session", style="Title.TLabel").grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 14))

        self.v_hours = tk.StringVar(value="6")
        self.v_mins = tk.StringVar(value="0")
        self.v_out = tk.StringVar(value="45")
        self.v_fps = tk.StringVar(value="30")
        self.v_res = tk.StringVar(value="1280 x 720")
        self.v_quality = tk.IntVar(value=85)

        ttk.Label(f, text="Study for").grid(row=1, column=0, sticky="w", pady=4)
        row = ttk.Frame(f)
        row.grid(row=1, column=1, columnspan=3, sticky="w")
        ttk.Spinbox(row, from_=0, to=24, width=4, textvariable=self.v_hours).pack(side="left")
        ttk.Label(row, text="h").pack(side="left", padx=(4, 10))
        ttk.Spinbox(row, from_=0, to=59, width=4, textvariable=self.v_mins).pack(side="left")
        ttk.Label(row, text="m").pack(side="left", padx=4)

        ttk.Label(f, text="Video length").grid(row=2, column=0, sticky="w", pady=4)
        row = ttk.Frame(f)
        row.grid(row=2, column=1, columnspan=3, sticky="w")
        ttk.Spinbox(row, from_=1, to=600, width=6, textvariable=self.v_out).pack(side="left")
        ttk.Label(row, text="seconds").pack(side="left", padx=4)

        ttk.Label(f, text="Frame rate").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Combobox(f, textvariable=self.v_fps, width=6, state="readonly",
                     values=("30", "45", "60")).grid(row=3, column=1, sticky="w")

        ttk.Label(f, text="Resolution").grid(row=4, column=0, sticky="w", pady=4)
        ttk.Combobox(f, textvariable=self.v_res, width=12, state="readonly",
                     values=["%d x %d" % r for r in common.RESOLUTIONS]).grid(
            row=4, column=1, sticky="w")

        ttk.Label(f, text="JPEG quality").grid(row=5, column=0, sticky="w", pady=4)
        row = ttk.Frame(f)
        row.grid(row=5, column=1, columnspan=3, sticky="w")
        ttk.Scale(row, from_=60, to=95, orient="horizontal", length=140,
                  variable=self.v_quality,
                  command=lambda _: self.v_quality.set(int(float(_)))).pack(side="left")
        ttk.Label(row, textvariable=self.v_quality, width=4).pack(side="left", padx=6)

        ttk.Separator(f, orient="horizontal").grid(
            row=6, column=0, columnspan=4, sticky="ew", pady=12)

        head = ttk.Frame(f)
        head.grid(row=7, column=0, columnspan=4, sticky="w")
        ttk.Label(head, text="Camera", style="Head.TLabel").pack(side="left")
        ttk.Label(head, style="Faint.TLabel",
                  text="   pick the one that looks right - index order is not reliable"
                  ).pack(side="left")

        self.cam_row = ttk.Frame(f)
        self.cam_row.grid(row=8, column=0, columnspan=4, sticky="w", pady=8)
        self.cam_status = ttk.Label(self.cam_row, text="Detecting cameras...")
        self.cam_status.pack(side="left")

        self.readout = ttk.Label(f, text="", style="Dim.TLabel")
        self.readout.grid(row=9, column=0, columnspan=4, sticky="w", pady=(10, 2))
        self.warning = ttk.Label(f, text="", style="Warn.TLabel",
                                 wraplength=self.px(420))
        self.warning.grid(row=10, column=0, columnspan=4, sticky="w")

        actions = ttk.Frame(f)
        actions.grid(row=11, column=0, columnspan=4, sticky="ew", pady=(14, 0))
        self.start_btn = ttk.Button(actions, text="Start session",
                                    style="Accent.TButton", command=self.on_start)
        self.start_btn.pack(side="right")
        ttk.Button(actions, text="Open folder",
                   command=lambda: common.open_in_explorer(common.ensure_root())).pack(side="left")
        ttk.Button(actions, text="Rescan cameras",
                   command=self.probe_cameras).pack(side="left", padx=6)

        for var in (self.v_hours, self.v_mins, self.v_out, self.v_fps,
                    self.v_res, self.v_quality, self.camera_index):
            var.trace_add("write", lambda *_: self.refresh_readout())

        self.refresh_readout()
        self.probe_cameras()

    # -- camera probing -------------------------------------------------
    def probe_cameras(self):
        for child in self.cam_row.winfo_children():
            child.destroy()
        self.cam_status = ttk.Label(self.cam_row, text="Detecting cameras...")
        self.cam_status.pack(side="left")
        threading.Thread(target=self._probe_worker, daemon=True).start()

    def _probe_worker(self):
        outdir = os.path.join(os.environ.get("TEMP", HERE), "study_timelapse_thumbs")
        try:
            proc = _spawn(["probe", "--outdir", outdir], PROBE_TIMEOUT)
            found = json.loads(proc.stdout.decode("utf-8", "replace") or "[]")
        except Exception:
            found = []
        names = common.camera_names()
        self.after(0, lambda: self._show_cameras(found, names))

    def _show_cameras(self, found, names):
        # Probing runs on a worker thread and takes a couple of seconds, during
        # which the setup screen can disappear - accepting the resume prompt
        # tears it down. Without this guard the late callback would paint into
        # destroyed widgets and raise TclError.
        if self.setup_frame is None or not self.cam_row.winfo_exists():
            self.cameras = found
            return
        for child in self.cam_row.winfo_children():
            child.destroy()
        self.cameras = found
        if not found:
            ttk.Label(self.cam_row, style="Error.TLabel",
                      text="No camera available. It may be in use by another app - "
                           "close it and press Rescan.").pack(side="left")
            self.camera_index.set(-1)
            self.refresh_readout()
            return

        self.thumbs = {}
        self.cam_tiles = {}
        for cam in found:
            index = cam["index"]
            box = ttk.Frame(self.cam_row, padding=4)
            box.pack(side="left", padx=4)
            try:
                self.thumbs[index] = tk.PhotoImage(file=cam["thumb"])
                image = tk.Label(box, image=self.thumbs[index], borderwidth=2,
                                 relief="solid", bg=theme.BG,
                                 highlightthickness=2, highlightbackground=theme.BG)
            except Exception:
                image = tk.Label(box, text="camera %d" % index, width=20, height=6,
                                 bg=theme.SURFACE, fg=theme.TEXT_DIM)
            image.pack()
            image.bind("<Button-1>", lambda _e, i=index: self.camera_index.set(i))
            self.cam_tiles[index] = image
            ttk.Radiobutton(box, text=self.name_for(index, len(found), names)[:30],
                            value=index, variable=self.camera_index).pack(anchor="w")

        self.camera_index.set(found[0]["index"])
        self.highlight_camera()
        self.refresh_readout()

    def highlight_camera(self):
        chosen = self.camera_index.get()
        for index, tile in getattr(self, "cam_tiles", {}).items():
            if not tile.winfo_exists():
                continue
            tile.configure(highlightbackground=theme.ACCENT if index == chosen
                           else theme.BG)

    @staticmethod
    def name_for(index, found_count, names):
        """WMI device name, but only when the mapping is actually trustworthy.

        Windows does not promise that WMI enumeration order matches OpenCV
        indices. When the two lists are not even the same length - which is the
        normal case here, since the infrared camera is reported by WMI but is
        not openable by OpenCV - guessing would attach the wrong name to a
        camera, so fall back to the bare index and let the thumbnail identify it.
        """
        if found_count == len(names) and index < len(names):
            return names[index]
        return "Camera %d" % index

    def camera_label(self):
        return self.name_for(self.camera_index.get(), len(self.cameras),
                             common.camera_names())

    # -- validation -----------------------------------------------------
    def read_settings(self):
        def num(var, default=0):
            try:
                return int(float(var.get()))
            except (ValueError, TypeError):
                return default

        study = num(self.v_hours) * 3600 + num(self.v_mins) * 60
        output = num(self.v_out)
        fps = num(self.v_fps, 30)
        width, height = (int(p) for p in self.v_res.get().split(" x "))
        return study, output, fps, width, height, int(self.v_quality.get())

    def refresh_readout(self):
        if self.setup_frame is None:
            return
        self.highlight_camera()
        study, output, fps, width, height, quality = self.read_settings()
        if study <= 0 or output <= 0 or fps <= 0:
            self.readout.config(text="")
            self.warning.config(text="Enter a study duration and a video length.")
            self.start_btn.state(["disabled"])
            return

        slots, interval = common.compute_plan(study, output, fps)
        size = common.estimate_bytes(slots, width, height, quality)
        self.readout.config(text="%d photos  -  one every %.1fs  -  ~%s  -  ends around %s"
                                 % (slots, interval, fmt_size(size), common.eta_text(study)))

        problems = []
        if interval < common.MIN_INTERVAL:
            longest = study / (common.MIN_INTERVAL * fps)
            problems.append(
                "One photo every %.1fs is too fast - the camera needs about %.0fs per shot. "
                "At %d fps the longest video this session supports is %.0fs."
                % (interval, common.MIN_INTERVAL, fps, longest))
        if slots > common.MAX_SLOTS_WARN:
            problems.append("%d photos is a lot - expect a large folder." % slots)
        try:
            free = __import__("shutil").disk_usage(common.pictures_dir()).free
            if free < size * 1.5:
                problems.append("Only %s free on the Pictures drive; this needs about %s."
                                % (fmt_size(free), fmt_size(size)))
        except OSError:
            pass
        if self.camera_index.get() < 0:
            problems.append("Select a camera.")

        self.warning.config(text="  ".join(problems))
        blocking = (interval < common.MIN_INTERVAL or self.camera_index.get() < 0
                    or any("free on the Pictures drive" in p for p in problems))
        self.start_btn.state(["disabled"] if blocking else ["!disabled"])

    # ------------------------------------------------------------------
    # resume
    # ------------------------------------------------------------------
    def offer_resume(self):
        for info in reversed(common.scan_sessions()):
            if info.get("state") != STATE_RUNNING:
                continue
            try:
                session = Session.load(info["path"])
            except Exception:
                continue
            done = info["count"]
            total = session.total_slots
            remaining = max(0, session.plan["study_seconds"]
                            - session.data.get("study_elapsed_seconds", 0))
            if messagebox.askyesno(
                    "Resume session",
                    "Session %s was still running.\n\n"
                    "%d of %d photos captured, %s of study left.\n\nResume it?"
                    % (session.data["session_id"], done, total, fmt_duration(remaining))):
                session.log_event("resumed")
                self.begin(session,
                           elapsed=session.data.get("study_elapsed_seconds", 0.0),
                           next_slot=session.last_slot() + 1,
                           frames_ok=done)
            else:
                session.set_state(STATE_STOPPED)
            return

    # ------------------------------------------------------------------
    # run view
    # ------------------------------------------------------------------
    def on_start(self):
        study, output, fps, width, height, quality = self.read_settings()
        try:
            session = Session.create(self.camera_index.get(), self.camera_label(),
                                     study, output, fps, width, height, quality)
        except OSError as exc:
            messagebox.showerror("Could not create session", str(exc))
            return
        self.begin(session)

    def begin(self, session, elapsed=0.0, next_slot=1, frames_ok=0):
        self.session = session
        self.clock = common.SlotClock(session.interval, session.total_slots,
                                      elapsed=elapsed, next_slot=next_slot)
        self.clock.suspended = session.data.get("suspended_seconds", 0.0)
        self.frames_ok = frames_ok
        self.consecutive_fail = 0
        self.grab_busy = False
        self.running = True

        if self.setup_frame is not None:
            self.setup_frame.destroy()
            self.setup_frame = None
        self._build_run()

        common.keep_awake(True)
        self.clock.start(time.time(), time.monotonic())
        self.tick()

    # -- recording views -------------------------------------------------
    # Two layouts driven by the same clock. COMPACT reproduces the Windows 11
    # Clock focus-session mini widget: measured off a screenshot, it is 176x167
    # logical units with a 76-unit disc and the dots sitting inside it.
    # EXPANDED is the same idea with room for the frame count and status lines.
    # The chevron top-left swaps between them; both windows are borderless, so
    # every piece of chrome is drawn on the canvas.
    COMPACT = dict(w=176, h=167, chrome_y=23, ring_cy=83, ring=76,
                   btn_y=136, main_x=74, side_x=102, main_d=20, side_d=16,
                   big_pt=19, unit_pt=9)
    EXPANDED = dict(w=300, h=418, chrome_y=23, ring_cy=140, ring=150,
                    btn_y=352, main_x=108, side_x=152, third_x=196,
                    main_d=44, side_d=36, big_pt=25, unit_pt=10)

    ICON_EXPAND = "\uE740"
    ICON_COLLAPSE = "\uE73F"
    ICON_CLOSE = "\uE8BB"

    def px(self, value):
        return int(round(value * self.scale))

    def icon_font(self, points):
        return ("Segoe MDL2 Assets", points)

    def _build_run(self):
        self.view = getattr(self, "view", "compact")
        self._build_view()

    def _build_view(self):
        for child in self.winfo_children():
            child.destroy()

        spec = self.COMPACT if self.view == "compact" else self.EXPANDED
        self.spec = spec
        width, height = self.px(spec["w"]), self.px(spec["h"])

        # Borderless: a native title bar cannot be made to match the reference,
        # so the window is undecorated and painted end to end.
        self.overrideredirect(True)
        self.attributes("-topmost", self.top_var.get())
        self.geometry("%dx%d" % (width, height))

        canvas = tk.Canvas(self, width=width, height=height, bg=theme.BG,
                           highlightthickness=0, bd=0)
        canvas.pack()
        self.canvas = canvas
        self.run_frame = canvas
        mid = width // 2
        self.after(20, lambda: theme.round_window(self, self.px(8)))

        # -- chrome ------------------------------------------------------
        chrome_y = self.px(spec["chrome_y"])
        self.toggle_item = canvas.create_text(
            self.px(17), chrome_y,
            text=self.ICON_EXPAND if self.view == "compact" else self.ICON_COLLAPSE,
            fill=theme.TEXT_DIM, font=self.icon_font(11))
        canvas.create_text(mid, chrome_y, text="Study session", fill=theme.TEXT,
                           font=theme.ui_font(11))
        self.close_item = canvas.create_text(
            width - self.px(17), chrome_y, text=self.ICON_CLOSE,
            fill=theme.TEXT_DIM, font=self.icon_font(10))
        for item, handler in ((self.toggle_item, self.toggle_view),
                              (self.close_item, self.on_close)):
            canvas.tag_bind(item, "<Button-1>", lambda _e, h=handler: h())
            canvas.tag_bind(item, "<Enter>",
                            lambda _e, i=item: canvas.itemconfigure(i, fill=theme.TEXT))
            canvas.tag_bind(item, "<Leave>",
                            lambda _e, i=item: canvas.itemconfigure(i, fill=theme.TEXT_DIM))

        # -- ring --------------------------------------------------------
        ring_px = self.px(spec["ring"])
        self._ring_fraction = None
        self._ring_photo = theme.photo(theme.ring_image(ring_px, 0.0))
        self.ring_item = canvas.create_image(mid, self.px(spec["ring_cy"]),
                                             image=self._ring_photo)

        cy = self.px(spec["ring_cy"])
        if self.view == "compact":
            # "352 min" on one line, laid out like the reference. Both are
            # anchored west and repositioned once measured, because the value
            # changes width (3 digits to 2 to 1) and the pair has to stay
            # centred in the disc as a group.
            self.big_item = canvas.create_text(
                mid, cy, anchor="w", text="--", fill=theme.TEXT,
                font=theme.ui_font(spec["big_pt"], "light"))
            self.big_sub_item = canvas.create_text(
                mid, cy + self.px(3), anchor="w", text="min",
                fill=theme.TEXT_DIM, font=theme.ui_font(spec["unit_pt"]))
        else:
            self.big_item = canvas.create_text(
                mid, cy - self.px(10), text="--:--", fill=theme.TEXT,
                font=theme.ui_font(spec["big_pt"], "light"))
            self.big_sub_item = canvas.create_text(
                mid, cy + self.px(20), text="left", fill=theme.TEXT_DIM,
                font=theme.ui_font(spec["unit_pt"]))

        # -- detail lines (expanded only) --------------------------------
        self.frames_item = self.detail_item = self.notice_item = self.dot_item = None
        if self.view == "expanded":
            self.frames_item = canvas.create_text(
                mid, self.px(246), text="", fill=theme.TEXT,
                font=theme.ui_font(10, "semilight"))
            self.dot_item = canvas.create_oval(0, 0, 0, 0, fill=theme.ACCENT,
                                               outline="")
            self.detail_item = canvas.create_text(
                mid, self.px(270), text="", fill=theme.TEXT_DIM,
                font=theme.ui_font(9))
            self.notice_item = canvas.create_text(
                mid, self.px(298), text="", fill=theme.WARN,
                font=theme.ui_font(9), width=self.px(spec["w"] - 40),
                justify="center")

        # -- buttons -----------------------------------------------------
        btn_y = self.px(spec["btn_y"])
        expanded = self.view == "expanded"
        self.btn_stop = theme.CanvasButton(
            canvas, self.px(spec["main_x"]), btn_y, theme.ICON_STOP, self.on_stop,
            size=self.px(spec["main_d"]), fill=theme.ACCENT,
            hover=theme.ACCENT_HOVER, glyph_colour="#1A1A1A",
            label="Stop" if expanded else "")
        self.btn_folder = theme.CanvasButton(
            canvas, self.px(spec["side_x"]), btn_y, theme.ICON_FOLDER,
            lambda: common.open_in_explorer(self.session.path),
            size=self.px(spec["side_d"]), label="Folder" if expanded else "")
        self.btn_pin = None
        if expanded:
            self.btn_pin = theme.CanvasButton(
                canvas, self.px(spec["third_x"]), btn_y, theme.ICON_PIN,
                self.toggle_on_top, size=self.px(spec["side_d"]), label="Pin")
            self._sync_pin()

        self._enable_drag(canvas)
        if self.session is not None and self.clock is not None:
            self.refresh_status()
        # Carry the current notice across a view switch; it is only stored
        # while compact, which has nowhere to show it.
        notice = getattr(self, "_notice", None)
        if notice and notice[0]:
            self.set_notice(*notice)
        if getattr(self, "_ended", None):
            self.set_done_view(*self._ended)

    # -- window behaviour --------------------------------------------------
    def _enable_drag(self, canvas):
        """A borderless window has no title bar, so the body drags it."""
        def press(event):
            self._drag = (event.x_root - self.winfo_x(),
                          event.y_root - self.winfo_y())

        def motion(event):
            if getattr(self, "_drag", None):
                self.geometry("+%d+%d" % (event.x_root - self._drag[0],
                                          event.y_root - self._drag[1]))
        canvas.bind("<Button-1>", press)
        canvas.bind("<B1-Motion>", motion)
        canvas.bind("<ButtonRelease-1>", lambda _e: setattr(self, "_drag", None))

    def toggle_view(self):
        """Swap between the compact widget and the full panel, in place."""
        x, y = self.winfo_x(), self.winfo_y()
        self.view = "expanded" if self.view == "compact" else "compact"
        self._build_view()
        self.geometry("+%d+%d" % (x, y))

    def toggle_on_top(self):
        self.top_var.set(not self.top_var.get())
        self.attributes("-topmost", self.top_var.get())
        self._sync_pin()

    def _sync_pin(self):
        if self.btn_pin is None:
            return
        pinned = self.top_var.get()
        self.btn_pin.set_images(
            theme.ICON_PIN, size=self.px(self.spec["side_d"]),
            fill=theme.ACCENT if pinned else theme.BUTTON,
            hover=theme.ACCENT_HOVER if pinned else theme.BUTTON_HOVER,
            glyph_colour="#1A1A1A" if pinned else theme.TEXT,
            label="Pinned" if pinned else "Pin")

    def tick(self):
        if not self.running:
            return
        self.drain_results()

        slept = self.clock.update(time.time(), time.monotonic())
        if slept:
            self.session.data["suspended_seconds"] = round(self.clock.suspended, 1)
            self.session.log_event("gap", seconds=round(slept, 1))
            self.set_notice("Slept for %s - study time paused, end time moved back."
                            % fmt_duration(slept))

        self.maybe_grab()
        self.refresh_status()

        self.ticks += 1
        if self.ticks % 30 == 0:
            self.persist()

        if self.clock.finished and not self.grab_busy:
            self.finish()
            return
        self.after(1000, self.tick)

    def maybe_grab(self):
        if self.grab_busy:
            return
        due = self.clock.due_slot()
        if due is None:
            return
        # Several deadlines can fall due at once after a stall. Record the
        # ones we passed and jump to the current slot rather than burst-firing.
        for slot in range(self.clock.next_slot, due):
            self.session.log(slot, SKIPPED)
        self.clock.next_slot = due
        self.fire_grab(due)

    def fire_grab(self, slot):
        self.grab_busy = True
        width, height = self.session.data["resolution"]
        args = ["shot", "--index", str(self.session.data["camera"]["index"]),
                "--out", self.session.frame_path(slot),
                "--width", str(width), "--height", str(height),
                "--quality", str(self.session.data["jpeg_quality"])]

        def worker():
            try:
                code = _spawn(args, SHOT_TIMEOUT).returncode
                status = common.EXIT_TO_STATUS.get(code, common.NO_FRAME)
            except subprocess.TimeoutExpired:
                status = TIMEOUT
            except Exception:
                status = common.NO_FRAME
            self.results.put((slot, status))

        threading.Thread(target=worker, daemon=True).start()

    def drain_results(self):
        while True:
            try:
                slot, status = self.results.get_nowait()
            except queue.Empty:
                return
            self.grab_busy = False
            self.session.log(slot, status)
            self.clock.next_slot = max(self.clock.next_slot, slot + 1)
            self.last_status = status

            if status == OK:
                self.frames_ok += 1
                self.consecutive_fail = 0
                self.set_notice("")
            else:
                self.consecutive_fail += 1
                if status == common.WRITE_ERROR:
                    self.set_notice("Could not write the photo - the disk may be full. "
                                    "Free some space; capture keeps retrying.")
                elif self.consecutive_fail >= WARN_AFTER_FAILURES:
                    reason = ("another app is using the camera"
                              if status in (BUSY, NO_FRAME) else
                              "the camera is not responding" if status == NO_DEVICE
                              else status)
                    self.set_notice(
                        "%d shots in a row missed - %s. The session keeps running and "
                        "picks up again by itself." % (self.consecutive_fail, reason))
            self.persist()

    def persist(self):
        self.session.data["study_elapsed_seconds"] = round(self.clock.elapsed, 1)
        self.session.data["suspended_seconds"] = round(self.clock.suspended, 1)
        try:
            self.session.save()
        except OSError:
            pass

    def refresh_status(self):
        total = self.session.total_slots
        remaining = max(0.0, self.session.plan["study_seconds"] - self.clock.elapsed)

        if self.view == "compact":
            # One line, like the reference: a count and its unit.
            if remaining >= 90:
                self.set_big("%d" % round(remaining / 60.0), "min")
            else:
                self.set_big("%d" % round(remaining), "sec")
        else:
            self.set_big(common.fmt_compact_clock(remaining), "left")

        self.set_ring(float(self.frames_ok) / max(1, total))

        if self.frames_item is None:
            return          # compact view carries no detail lines

        if self.grab_busy:
            nxt = "taking photo"
        elif self.clock.finished:
            nxt = "done"
        else:
            nxt = "next in %ds" % int(self.clock.seconds_until_next())

        status = getattr(self, "last_status", OK)
        # NO_FRAME is the usual signal that another app owns the camera:
        # Windows lets a second process open the device but starves it of
        # frames, so an outright open failure (BUSY) is the rarer case.
        if status in (BUSY, NO_FRAME):
            camera, tint = "camera busy", theme.WARN
        elif status == NO_DEVICE:
            camera, tint = "camera not found", theme.ERROR
        elif status in (OK, None):
            camera, tint = "camera ok", theme.ACCENT
        else:
            camera, tint = str(status).replace("_", " "), theme.WARN

        self.canvas.itemconfigure(
            self.frames_item,
            text="%d of %d frames  -  %s" % (self.frames_ok, total, nxt))
        detail = "%s   -   ends around %s" % (camera, common.eta_text(remaining))
        if self.clock.suspended > 0:
            detail += "   -   %s slept" % fmt_duration(self.clock.suspended)
        self.canvas.itemconfigure(self.detail_item, text=detail)
        self._place_status_dot(tint)

    def set_big(self, value, sub):
        self.canvas.itemconfigure(self.big_item, text=value)
        self.canvas.itemconfigure(self.big_sub_item, text=sub)
        if self.view == "compact":
            self._centre_value_pair()

    def _centre_value_pair(self):
        """Centre value and unit together inside the disc.

        Measured after the text is set: the value swings between one and three
        digits over a session, so a fixed position would leave it drifting.
        """
        canvas = self.canvas
        mid = self.px(self.spec["w"]) // 2
        cy = self.px(self.spec["ring_cy"])
        value_box = canvas.bbox(self.big_item)
        if not value_box:
            return
        unit_box = canvas.bbox(self.big_sub_item)
        value_w = value_box[2] - value_box[0]
        unit_w = (unit_box[2] - unit_box[0]) if unit_box else 0
        gap = self.px(3) if unit_w else 0
        left = mid - (value_w + gap + unit_w) / 2.0
        canvas.coords(self.big_item, left, cy)
        canvas.coords(self.big_sub_item, left + value_w + gap, cy + self.px(3))

    def set_ring(self, fraction):
        """Re-render only when a dot would actually change, not every tick."""
        dots = 60
        quantised = int(round(max(0.0, min(1.0, fraction)) * dots))
        if quantised == self._ring_fraction:
            return
        self._ring_fraction = quantised
        self._ring_photo = theme.photo(
            theme.ring_image(self.px(self.spec["ring"]), quantised / float(dots)))
        self.canvas.itemconfigure(self.ring_item, image=self._ring_photo)

    def _place_status_dot(self, colour):
        """Small state dot, kept just left of the centred detail line."""
        if self.dot_item is None:
            return
        bounds = self.canvas.bbox(self.detail_item)
        if not bounds:
            return
        x, y = bounds[0] - self.px(9), (bounds[1] + bounds[3]) / 2
        r = self.px(2.5)
        self.canvas.coords(self.dot_item, x - r, y - r, x + r, y + r)
        self.canvas.itemconfigure(self.dot_item, fill=colour)

    def set_notice(self, text, colour=theme.WARN):
        # The compact view has no room for it; the text is kept so that
        # expanding shows whatever is current.
        self._notice = (text, colour)
        if self.run_frame is not None and self.notice_item is not None:
            self.canvas.itemconfigure(self.notice_item, text=text, fill=colour)

    # ------------------------------------------------------------------
    # finishing
    # ------------------------------------------------------------------
    def set_done_view(self, headline, detail):
        """End state: full ring, and Stop swapped for a tick."""
        self._ended = (headline, detail)
        self.set_ring(1.0)
        self.set_big(headline, "" if self.view == "compact" else detail)
        if self.frames_item is not None:
            self.canvas.itemconfigure(self.frames_item, text=detail,
                                      fill=theme.TEXT_DIM)
            self.canvas.itemconfigure(self.detail_item, text=self.session.path,
                                      fill=theme.TEXT_FAINT)
            self.canvas.coords(self.dot_item, 0, 0, 0, 0)
        self.set_notice("")
        self.btn_stop.set_images(theme.ICON_CHECK, size=self.px(self.spec["main_d"]),
                                 fill=theme.BUTTON, hover=theme.BUTTON_HOVER,
                                 glyph_colour=theme.ACCENT,
                                 label="Done" if self.view == "expanded" else "")
        self.btn_stop.command = None

    def finish(self):
        self.running = False
        self.persist()
        self.session.set_state(STATE_COMPLETED)
        self.session.log_event("completed", frames=self.frames_ok)
        common.keep_awake(False)
        self.set_done_view("Done", "%d frames captured" % self.frames_ok)
        if messagebox.askyesno("Session complete",
                               "Captured %d frames.\n\nOpen the renderer now?"
                               % self.frames_ok):
            self.open_renderer()

    def on_stop(self):
        if not messagebox.askyesno("Stop session",
                                   "Stop recording?\n\nThe %d frames already captured stay "
                                   "on disk and can still be rendered."% self.frames_ok):
            return
        self.running = False
        self.persist()
        self.session.set_state(STATE_STOPPED)
        self.session.log_event("stopped", frames=self.frames_ok)
        common.keep_awake(False)
        self.set_done_view("Stopped", "%d frames captured" % self.frames_ok)
        if messagebox.askyesno("Stopped", "Open the renderer?"):
            self.open_renderer()

    def open_renderer(self):
        subprocess.Popen([sys.executable, os.path.join(HERE, "render.py")],
                         creationflags=CREATE_NO_WINDOW)

    def on_close(self):
        if self.running:
            if not messagebox.askyesno(
                    "Close",
                    "A session is recording.\n\nClosing stops the photos, but the session "
                    "stays resumable - reopen this app to pick it up where it left off."):
                return
            self.persist()
            self.session.log_event("closed", frames=self.frames_ok)
        common.keep_awake(False)
        self.destroy()


if __name__ == "__main__":
    import atexit
    atexit.register(common.keep_awake, False)
    CaptureApp().mainloop()
