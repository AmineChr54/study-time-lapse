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
from common import (BUSY, NO_DEVICE, NO_FRAME, OK, SKIPPED, STATE_COMPLETED,
                    STATE_RUNNING, STATE_STOPPED, TIMEOUT, Session,
                    fmt_clock, fmt_duration, fmt_size)

HERE = os.path.dirname(os.path.abspath(__file__))
GRABBER = os.path.join(HERE, "grabber.py")

SHOT_TIMEOUT = 15
PROBE_TIMEOUT = 60
WARN_AFTER_FAILURES = 5

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _spawn(args, timeout):
    return subprocess.run([sys.executable, GRABBER] + args,
                          capture_output=True, timeout=timeout,
                          creationflags=CREATE_NO_WINDOW)


class CaptureApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Study Time-Lapse - Capture")
        self.resizable(False, False)

        self.cameras = []
        self.camera_index = tk.IntVar(value=-1)
        self.thumbs = {}

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
        f = ttk.Frame(self, padding=16)
        f.grid(sticky="nsew")
        self.setup_frame = f

        ttk.Label(f, text="Study session", font=("Segoe UI", 13, "bold")).grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 12))

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
        ttk.Label(head, text="Camera", font=("Segoe UI", 10, "bold")).pack(side="left")
        ttk.Label(head, text="  pick the one that looks right - index order is not reliable",
                  foreground="#777").pack(side="left")

        self.cam_row = ttk.Frame(f)
        self.cam_row.grid(row=8, column=0, columnspan=4, sticky="w", pady=8)
        self.cam_status = ttk.Label(self.cam_row, text="Detecting cameras...")
        self.cam_status.pack(side="left")

        self.readout = ttk.Label(f, text="", font=("Segoe UI", 10))
        self.readout.grid(row=9, column=0, columnspan=4, sticky="w", pady=(8, 2))
        self.warning = ttk.Label(f, text="", foreground="#b00020", wraplength=440)
        self.warning.grid(row=10, column=0, columnspan=4, sticky="w")

        actions = ttk.Frame(f)
        actions.grid(row=11, column=0, columnspan=4, sticky="ew", pady=(14, 0))
        self.start_btn = ttk.Button(actions, text="Start session", command=self.on_start)
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
            ttk.Label(self.cam_row, foreground="#b00020",
                      text="No camera available. It may be in use by another app - "
                           "close it and press Rescan.").pack(side="left")
            self.camera_index.set(-1)
            self.refresh_readout()
            return

        self.thumbs = {}
        for slot, cam in enumerate(found):
            index = cam["index"]
            box = ttk.Frame(self.cam_row, padding=4)
            box.pack(side="left", padx=4)
            try:
                self.thumbs[index] = tk.PhotoImage(file=cam["thumb"])
                image = tk.Label(box, image=self.thumbs[index], borderwidth=2, relief="flat")
            except Exception:
                image = tk.Label(box, text="camera %d" % index, width=20, height=6)
            image.pack()
            image.bind("<Button-1>", lambda _e, i=index: self.camera_index.set(i))
            ttk.Radiobutton(box, text=self.name_for(index, len(found), names)[:30],
                            value=index, variable=self.camera_index).pack(anchor="w")

        self.camera_index.set(found[0]["index"])
        self.refresh_readout()

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

    def _build_run(self):
        self.title("Study Time-Lapse - Recording")
        f = ttk.Frame(self, padding=14)
        f.grid(sticky="nsew")
        self.run_frame = f

        self.status = ttk.Label(f, text="", font=("Segoe UI", 11))
        self.status.grid(row=0, column=0, columnspan=3, sticky="w")

        self.progress = ttk.Progressbar(f, length=380, mode="determinate",
                                        maximum=self.session.total_slots)
        self.progress.grid(row=1, column=0, columnspan=3, sticky="ew", pady=8)

        self.detail = ttk.Label(f, text="", foreground="#555")
        self.detail.grid(row=2, column=0, columnspan=3, sticky="w")
        self.notice = ttk.Label(f, text="", foreground="#b06000", wraplength=380)
        self.notice.grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))

        actions = ttk.Frame(f)
        actions.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        ttk.Button(actions, text="Stop", command=self.on_stop).pack(side="right")
        ttk.Button(actions, text="Folder",
                   command=lambda: common.open_in_explorer(self.session.path)).pack(side="left")
        self.top_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(actions, text="Always on top", variable=self.top_var,
                        command=lambda: self.attributes("-topmost", self.top_var.get())
                        ).pack(side="left", padx=8)

    # ------------------------------------------------------------------
    # the clock
    # ------------------------------------------------------------------
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
        interval = self.session.interval
        remaining = max(0.0, self.session.plan["study_seconds"] - self.clock.elapsed)

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
            camera = "camera busy - retrying"
        elif status == NO_DEVICE:
            camera = "camera not found"
        elif status in (OK, None):
            camera = "camera ok"
        else:
            camera = "last shot: %s" % status

        self.status.config(text="%s left   -   %d/%d frames   -   %s"
                                % (fmt_clock(remaining), self.frames_ok, total, nxt))
        self.progress["value"] = self.frames_ok
        extra = ""
        if self.clock.suspended > 0:
            extra = "   -   %s slept" % fmt_duration(self.clock.suspended)
        self.detail.config(text="%s   -   ends around %s%s"
                                % (camera, common.eta_text(remaining), extra))

    def set_notice(self, text):
        if self.run_frame is not None:
            self.notice.config(text=text)

    # ------------------------------------------------------------------
    # finishing
    # ------------------------------------------------------------------
    def finish(self):
        self.running = False
        self.persist()
        self.session.set_state(STATE_COMPLETED)
        self.session.log_event("completed", frames=self.frames_ok)
        common.keep_awake(False)
        self.status.config(text="Done - %d frames captured" % self.frames_ok)
        self.detail.config(text="Saved to %s" % self.session.path)
        self.set_notice("")
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
        self.status.config(text="Stopped - %d frames captured" % self.frames_ok)
        self.set_notice("")
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
