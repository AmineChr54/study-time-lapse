"""APP 2 - the video maker.

Turns one or more capture sessions into a single video. Because nothing about
the output is decided at capture time, the same photos can be re-rendered at
any frame rate or length, and several sessions can be concatenated.
"""
import os
import queue
import shutil
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import common
import theme
from common import fmt_duration

# Best effort only: on Windows a DLL loaded afterwards may keep its own copy
# of the environment, so the .bat launchers set these before Python starts.
# Harmless either way - the noise is cosmetic and invisible under pythonw.
os.environ.setdefault("OPENCV_LOG_LEVEL", "SILENT")
os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "-8")

CHECKED = "☑"
UNCHECKED = "☐"

RESOLUTION_CHOICES = ["Source", "1920 x 1080", "1280 x 720", "854 x 480"]
# avc1 (H.264) first because it is what browsers, phones and chat apps can
# actually play; mp4v falls back to MPEG-4 Part 2, which many of them refuse.
# OpenCV logs a scary-looking OpenH264 failure on the way and then recovers,
# so trust the fourcc read back from the finished file, not the request.
FOURCC_CANDIDATES = ("avc1", "mp4v")


def actual_fourcc(path):
    """The codec really present in a written file."""
    import cv2
    cap = cv2.VideoCapture(path)
    value = int(cap.get(cv2.CAP_PROP_FOURCC))
    cap.release()
    tag = "".join(chr((value >> (8 * i)) & 0xFF) for i in range(4)).strip()
    return tag or "unknown"


def read_image(path):
    import cv2
    import numpy as np
    # np.fromfile + imdecode instead of imread, so non-ASCII paths work.
    try:
        data = np.fromfile(path, dtype=np.uint8)
    except OSError:
        return None
    if data.size == 0:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def fit(image, width, height):
    """Scale to fit and letterbox, so mixed sources are never distorted."""
    import cv2
    import numpy as np
    h, w = image.shape[:2]
    if (w, h) == (width, height):
        return image
    scale = min(width / float(w), height / float(h))
    new_w, new_h = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
    resized = cv2.resize(image, (new_w, new_h), interpolation=interp)
    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    y, x = (height - new_h) // 2, (width - new_w) // 2
    canvas[y:y + new_h, x:x + new_w] = resized
    return canvas


def build_sequence(sessions, hold_missing):
    """Flatten the chosen sessions into one ordered list of image paths.

    Slot numbers are the record of what was captured, so a gap in them is a
    missed shot. Holding the previous frame across a gap keeps the motion
    smooth and the running time honest.
    """
    paths = []
    for info in sessions:
        frames = info["frames"]
        if not frames:
            continue
        if not hold_missing:
            paths.extend(path for _, path in frames)
            continue
        by_slot = dict(frames)
        previous = None
        for slot in range(1, max(by_slot) + 1):
            current = by_slot.get(slot)
            if current is not None:
                previous = current
                paths.append(current)
            elif previous is not None:
                paths.append(previous)
    return paths


def resample(paths, count):
    """Even resample to an exact frame count - drops or duplicates as needed."""
    if not paths or count <= 0 or count == len(paths):
        return paths
    return [paths[i * len(paths) // count] for i in range(count)]


def open_writer(path, fps, size):
    import cv2
    for name in FOURCC_CANDIDATES:
        writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*name), fps, size)
        if writer.isOpened():
            return writer, name
        writer.release()
    return None, None


class RenderApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Study Time-Lapse - Render")
        theme.apply_icon(self)
        self.geometry("860x560")
        self.minsize(760, 500)

        self.sessions = []
        self.checked = set()
        self.preview = None
        self.cancel = threading.Event()
        self.progress_q = queue.Queue()
        self.rendering = False

        self._build()
        self.reload()

    # ------------------------------------------------------------------
    def _build(self):
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        head = ttk.Frame(root)
        head.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Label(head, text="Sessions", font=("Segoe UI", 12, "bold")).pack(side="left")
        ttk.Label(head, text="  tick one or several - multiple sessions are joined "
                             "in date order", foreground="#777").pack(side="left")
        ttk.Button(head, text="Refresh", command=self.reload).pack(side="right")

        columns = ("check", "date", "studied", "frames", "missed", "res", "camera")
        self.tree = ttk.Treeview(root, columns=columns, show="headings", height=10)
        for col, text, width, anchor in (
                ("check", "", 34, "center"), ("date", "Session", 190, "w"),
                ("studied", "Studied", 90, "w"), ("frames", "Frames", 70, "e"),
                ("missed", "Missed", 70, "e"), ("res", "Resolution", 100, "w"),
                ("camera", "Camera", 200, "w")):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor,
                             stretch=(col == "camera"))
        self.tree.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(root, orient="vertical", command=self.tree.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<Button-1>", self.on_tree_click)
        self.tree.bind("<space>", lambda _e: self.toggle(self.tree.focus()))
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.show_preview())

        side = ttk.Frame(root, padding=(12, 0, 0, 0))
        side.grid(row=1, column=2, sticky="n")
        self.preview_label = ttk.Label(side, text="no preview", width=30,
                                       anchor="center", relief="groove")
        self.preview_label.pack()
        self.preview_caption = ttk.Label(side, text="", foreground="#777",
                                         wraplength=220, justify="center")
        self.preview_caption.pack(pady=4)

        opts = ttk.LabelFrame(root, text="Output", padding=10)
        opts.grid(row=2, column=0, columnspan=3, sticky="ew", pady=10)

        self.v_fps = tk.StringVar(value="30")
        self.v_mode = tk.StringVar(value="auto")
        self.v_target = tk.StringVar(value="45")
        self.v_hold = tk.BooleanVar(value=True)
        self.v_res = tk.StringVar(value="Source")
        self.v_out = tk.StringVar(value="")

        ttk.Label(opts, text="Frame rate").grid(row=0, column=0, sticky="w")
        ttk.Combobox(opts, textvariable=self.v_fps, width=6,
                     values=("24", "30", "45", "60")).grid(row=0, column=1, sticky="w", padx=6)

        ttk.Label(opts, text="Length").grid(row=0, column=2, sticky="w", padx=(16, 0))
        ttk.Radiobutton(opts, text="All frames", value="auto", variable=self.v_mode,
                        command=self.refresh_summary).grid(row=0, column=3, sticky="w", padx=6)
        ttk.Radiobutton(opts, text="Exactly", value="target", variable=self.v_mode,
                        command=self.refresh_summary).grid(row=0, column=4, sticky="w")
        ttk.Spinbox(opts, from_=1, to=3600, width=6, textvariable=self.v_target
                    ).grid(row=0, column=5, sticky="w", padx=4)
        ttk.Label(opts, text="s").grid(row=0, column=6, sticky="w")

        ttk.Label(opts, text="Resolution").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Combobox(opts, textvariable=self.v_res, width=12, state="readonly",
                     values=RESOLUTION_CHOICES).grid(row=1, column=1, sticky="w",
                                                     padx=6, pady=(8, 0))
        ttk.Checkbutton(opts, text="Hold previous frame across missed shots",
                        variable=self.v_hold, command=self.refresh_summary
                        ).grid(row=1, column=2, columnspan=4, sticky="w",
                               padx=(16, 0), pady=(8, 0))

        ttk.Label(opts, text="Save to").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(opts, textvariable=self.v_out, width=62).grid(
            row=2, column=1, columnspan=5, sticky="ew", padx=6, pady=(8, 0))
        ttk.Button(opts, text="...", width=3, command=self.choose_output).grid(
            row=2, column=6, pady=(8, 0))

        for var in (self.v_fps, self.v_target):
            var.trace_add("write", lambda *_: self.refresh_summary())

        bottom = ttk.Frame(root)
        bottom.grid(row=3, column=0, columnspan=3, sticky="ew")
        self.summary = ttk.Label(bottom, text="", font=("Segoe UI", 10))
        self.summary.pack(side="left")
        self.render_btn = ttk.Button(bottom, text="Render video", command=self.on_render)
        self.render_btn.pack(side="right")
        self.cancel_btn = ttk.Button(bottom, text="Cancel", command=self.on_cancel,
                                     state="disabled")
        self.cancel_btn.pack(side="right", padx=6)

        self.bar = ttk.Progressbar(root, mode="determinate")
        self.bar.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.bar.grid_remove()   # only shown while a render is running

    # ------------------------------------------------------------------
    def reload(self):
        self.tree.delete(*self.tree.get_children())
        self.checked.clear()
        self.sessions = common.scan_sessions()
        if not self.sessions:
            self.summary.config(text="No sessions yet - record one with the capture app.")
            return
        for info in self.sessions:
            resolution = ("%d x %d" % info["resolution"]
                          if info.get("resolution") else "-")
            studied = fmt_duration(info["studied"]) if info["studied"] else "-"
            self.tree.insert("", "end", iid=info["path"],
                             values=(UNCHECKED, info["name"], studied, info["count"],
                                     info["missed"] or "", resolution, info["camera"]))
        last = self.sessions[-1]["path"]
        self.checked.add(last)
        self.tree.set(last, "check", CHECKED)
        self.tree.selection_set(last)
        self.tree.focus(last)
        self.suggest_output()
        self.refresh_summary()
        self.show_preview()

    def on_tree_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        if self.tree.identify_column(event.x) == "#1":
            self.toggle(self.tree.identify_row(event.y))
            return "break"

    def toggle(self, item):
        if not item:
            return
        if item in self.checked:
            self.checked.discard(item)
            self.tree.set(item, "check", UNCHECKED)
        else:
            self.checked.add(item)
            self.tree.set(item, "check", CHECKED)
        self.suggest_output()
        self.refresh_summary()

    def selected_sessions(self):
        return [i for i in self.sessions if i["path"] in self.checked]

    # ------------------------------------------------------------------
    def show_preview(self):
        item = self.tree.focus()
        info = next((i for i in self.sessions if i["path"] == item), None)
        if not info or not info["frames"]:
            return
        path = info["frames"][0][1]
        try:
            from PIL import Image, ImageTk
            image = Image.open(path)
            image.thumbnail((220, 220))
            self.preview = ImageTk.PhotoImage(image)
            self.preview_label.config(image=self.preview, text="")
        except Exception:
            self.preview_label.config(image="", text="no preview")
        self.preview_caption.config(text="%s\nfirst frame" % info["name"])

    def suggest_output(self):
        chosen = self.selected_sessions()
        if not chosen:
            return
        try:
            fps = int(float(self.v_fps.get()))
        except (ValueError, TypeError):
            fps = 30
        name = "timelapse_%dfps.mp4" % fps
        if len(chosen) > 1:
            name = "timelapse_%dsessions_%dfps.mp4" % (len(chosen), fps)
        self.v_out.set(os.path.join(chosen[0]["path"], name))

    def choose_output(self):
        current = self.v_out.get()
        path = filedialog.asksaveasfilename(
            defaultextension=".mp4", filetypes=[("MP4 video", "*.mp4")],
            initialdir=os.path.dirname(current) or common.sessions_root(),
            initialfile=os.path.basename(current) or "timelapse.mp4")
        if path:
            self.v_out.set(path)

    def plan(self):
        """(paths, fps) for the current selection, or (None, reason)."""
        chosen = self.selected_sessions()
        if not chosen:
            return None, "Tick at least one session."
        try:
            fps = int(float(self.v_fps.get()))
        except (ValueError, TypeError):
            return None, "Frame rate must be a number."
        if fps <= 0:
            return None, "Frame rate must be above zero."

        paths = build_sequence(chosen, self.v_hold.get())
        if not paths:
            return None, "The selected sessions have no frames."
        if self.v_mode.get() == "target":
            try:
                target = float(self.v_target.get())
            except (ValueError, TypeError):
                return None, "Length must be a number."
            if target <= 0:
                return None, "Length must be above zero."
            paths = resample(paths, int(round(target * fps)))
        return (paths, fps), None

    def refresh_summary(self):
        if self.rendering:
            return
        result, problem = self.plan()
        if result is None:
            self.summary.config(text=problem, foreground="#b00020")
            self.render_btn.state(["disabled"])
            return
        paths, fps = result
        chosen = self.selected_sessions()
        source = sum(i["count"] for i in chosen)
        note = ""
        if len(paths) != source:
            note = "  (from %d photos)" % source
        self.summary.config(
            text="%d %s  -  %d frames at %d fps  -  %.1fs video%s"
                 % (len(chosen), "session" if len(chosen) == 1 else "sessions",
                    len(paths), fps, len(paths) / float(fps), note),
            foreground="#333")
        self.render_btn.state(["!disabled"])

    # ------------------------------------------------------------------
    def on_render(self):
        result, problem = self.plan()
        if result is None:
            messagebox.showwarning("Cannot render", problem)
            return
        paths, fps = result
        out = self.v_out.get().strip()
        if not out:
            messagebox.showwarning("Cannot render", "Choose where to save the video.")
            return
        if os.path.exists(out) and not messagebox.askyesno(
                "Overwrite", "%s already exists.\n\nReplace it?" % os.path.basename(out)):
            return

        size = None
        if self.v_res.get() != "Source":
            size = tuple(int(p) for p in self.v_res.get().split(" x "))

        self.rendering = True
        self.cancel.clear()
        self.render_btn.state(["disabled"])
        self.cancel_btn.state(["!disabled"])
        self.bar.configure(maximum=len(paths), value=0)
        self.bar.grid()
        threading.Thread(target=self._render_worker,
                         args=(paths, fps, out, size), daemon=True).start()
        self.after(100, self.poll_progress)

    def _render_worker(self, paths, fps, out, size):
        writer = None
        # VideoWriter shares imwrite's narrow-char path problem, so encode to a
        # temp file and move it into place afterwards.
        handle, temp = tempfile.mkstemp(suffix=".mp4")
        os.close(handle)
        os.unlink(temp)
        try:
            if size is None:
                first = read_image(paths[0])
                if first is None:
                    raise RuntimeError("Could not read %s" % paths[0])
                size = (first.shape[1], first.shape[0])

            writer, _requested = open_writer(temp, fps, size)
            if writer is None:
                raise RuntimeError("No usable video encoder was available.")

            written = 0
            last_good = None
            for index, path in enumerate(paths):
                if self.cancel.is_set():
                    self.progress_q.put(("cancelled", None))
                    return
                image = read_image(path)
                if image is None:
                    # A corrupt or half-written file must not abort the render.
                    image = last_good
                    if image is None:
                        continue
                else:
                    image = fit(image, size[0], size[1])
                    last_good = image
                writer.write(image)
                written += 1
                if index % 5 == 0:
                    self.progress_q.put(("progress", index))

            writer.release()
            writer = None
            if written == 0:
                raise RuntimeError("None of the frames could be read.")
            os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
            shutil.move(temp, out)
            temp = None
            self.progress_q.put(("done", (out, written, actual_fourcc(out))))
        except Exception as exc:
            self.progress_q.put(("error", str(exc)))
        finally:
            if writer is not None:
                writer.release()
            if temp and os.path.exists(temp):
                try:
                    os.unlink(temp)
                except OSError:
                    pass

    def poll_progress(self):
        try:
            while True:
                kind, payload = self.progress_q.get_nowait()
                if kind == "progress":
                    self.bar["value"] = payload
                    self.summary.config(text="Rendering... %d frames" % payload,
                                        foreground="#333")
                    continue
                self.finish_render()
                if kind == "done":
                    out, written, fourcc = payload
                    self.bar["value"] = self.bar["maximum"]
                    self.summary.config(
                        text="Done - %d frames, %s codec" % (written, fourcc),
                        foreground="#0a7")
                    if messagebox.askyesno("Render complete",
                                           "Wrote %d frames to\n%s\n\nOpen the folder?"
                                           % (written, out)):
                        common.open_in_explorer(os.path.dirname(out))
                elif kind == "cancelled":
                    self.bar["value"] = 0
                    self.summary.config(text="Cancelled.", foreground="#b06000")
                else:
                    self.bar["value"] = 0
                    messagebox.showerror("Render failed", str(payload))
                self.refresh_summary()
                return
        except queue.Empty:
            pass
        if self.rendering:
            self.after(100, self.poll_progress)

    def finish_render(self):
        self.rendering = False
        self.bar.grid_remove()
        self.cancel_btn.state(["disabled"])
        self.render_btn.state(["!disabled"])

    def on_cancel(self):
        self.cancel.set()


if __name__ == "__main__":
    RenderApp().mainloop()
