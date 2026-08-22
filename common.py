"""Shared helpers for the study time-lapse capturer and renderer.

Deliberately dependency-light: this module is imported by the resident capture
process, so it must never pull in cv2 / numpy at import time.
"""
import ctypes
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta

APP_FOLDER = "StudyTimeLapse"
FRAMES_DIRNAME = "frames"
MANIFEST_NAME = "session.json"
LOG_NAME = "frames.jsonl"

# A shot needs ~1-2s of camera warm-up, so anything below this cannot keep up.
MIN_INTERVAL = 3.0
MAX_SLOTS_WARN = 20000

STATE_RUNNING = "running"
STATE_COMPLETED = "completed"
STATE_STOPPED = "stopped"

# frames.jsonl status values
OK = "ok"
BUSY = "busy"
NO_FRAME = "no_frame"
NO_DEVICE = "no_device"
TIMEOUT = "timeout"
WRITE_ERROR = "write_error"
SKIPPED = "skipped"

# grabber.py exit codes
EXIT_OK = 0
EXIT_BUSY = 2
EXIT_NO_FRAME = 3
EXIT_NO_DEVICE = 4
EXIT_WRITE = 5

EXIT_TO_STATUS = {
    EXIT_OK: OK,
    EXIT_BUSY: BUSY,
    EXIT_NO_FRAME: NO_FRAME,
    EXIT_NO_DEVICE: NO_DEVICE,
    EXIT_WRITE: WRITE_ERROR,
}

RESOLUTIONS = [(640, 480), (1280, 720), (1920, 1080)]


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------
class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong),
                ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)]


def pictures_dir():
    """Real Pictures folder via the known-folder API.

    Must not be guessed from the user profile: Pictures is frequently
    redirected to another drive or to OneDrive.
    """
    if sys.platform == "win32":
        try:
            guid = _GUID(0x33E28130, 0x4E1E, 0x4676,
                         (ctypes.c_ubyte * 8)(0x83, 0x5A, 0x98, 0x39,
                                              0x5C, 0x3B, 0xC3, 0xBB))
            ptr = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(
                    ctypes.byref(guid), 0, None, ctypes.byref(ptr)) == 0 and ptr.value:
                value = ptr.value
                ctypes.windll.ole32.CoTaskMemFree(ptr)
                return value
        except Exception:
            pass
    return os.path.join(os.path.expanduser("~"), "Pictures")


def sessions_root():
    return os.path.join(pictures_dir(), APP_FOLDER)


def ensure_root():
    root = sessions_root()
    os.makedirs(root, exist_ok=True)
    return root


def open_in_explorer(path):
    if sys.platform == "win32":
        os.startfile(path)


# --------------------------------------------------------------------------
# session planning
# --------------------------------------------------------------------------
def compute_plan(study_seconds, output_seconds, fps):
    """How many photos we need, and the gap between them."""
    total_slots = max(1, round(output_seconds * fps))
    interval = study_seconds / total_slots
    return total_slots, interval


# A tick that loses more wall-clock time than this against the monotonic clock
# means the machine was suspended rather than merely busy.
SUSPEND_THRESHOLD = 5.0


class SlotClock:
    """Tracks study time and decides which photo slot is due.

    Kept free of any UI so the awkward parts - suspend detection and catching
    up after a stall - can be tested directly.
    """

    def __init__(self, interval, total_slots, elapsed=0.0, next_slot=1,
                 suspend_threshold=SUSPEND_THRESHOLD):
        self.interval = interval
        self.total_slots = total_slots
        self.elapsed = elapsed
        self.next_slot = next_slot
        self.suspended = 0.0
        self.threshold = suspend_threshold
        self.last_wall = 0.0
        self.last_mono = 0.0

    def start(self, wall, mono):
        self.last_wall, self.last_mono = wall, mono

    def update(self, wall, mono):
        """Advance the clock. Returns seconds slept this tick, else 0.

        QueryPerformanceCounter freezes while the machine is suspended but the
        wall clock keeps running, so the divergence between them is the sleep
        detector. It catches sleep, hibernate and clock jumps in one check,
        with no need for a WM_POWERBROADCAST message pump.
        """
        delta_wall = wall - self.last_wall
        delta_mono = mono - self.last_mono
        self.last_wall, self.last_mono = wall, mono
        gap = delta_wall - delta_mono
        if gap > self.threshold:
            self.suspended += gap
            return gap
        self.elapsed += max(0.0, delta_mono)
        return 0.0

    def due_slot(self):
        """Highest slot whose deadline has passed, or None if none is due.

        Slot n is due at (n-1) * interval, so slot 1 fires immediately. After a
        long stall several deadlines may have passed at once; returning only
        the latest lets the caller skip the rest instead of burst-capturing.
        """
        if self.next_slot > self.total_slots:
            return None
        due = min(self.total_slots, int(self.elapsed / self.interval) + 1)
        return due if due >= self.next_slot else None

    def seconds_until_next(self):
        return max(0.0, (self.next_slot - 1) * self.interval - self.elapsed)

    @property
    def finished(self):
        return self.next_slot > self.total_slots


def estimate_bytes(total_slots, width, height, quality):
    """Rough JPEG size estimate; ~150 KB for 720p at q85."""
    per_frame = width * height * 0.16 * (quality / 85.0)
    return int(total_slots * per_frame)


def fmt_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.0f %s" % (n, unit) if unit in ("B", "KB") else "%.1f %s" % (n, unit)
        n /= 1024.0


def fmt_duration(seconds):
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%dh %02dm" % (h, m)
    if m:
        return "%dm %02ds" % (m, s)
    return "%ds" % s


def fmt_clock(seconds):
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return "%02d:%02d:%02d" % (h, m, s)


def eta_text(remaining_seconds):
    end = datetime.now() + timedelta(seconds=max(0, remaining_seconds))
    return end.strftime("%H:%M")


# --------------------------------------------------------------------------
# sleep prevention
# --------------------------------------------------------------------------
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def keep_awake(enabled):
    """Hold the system awake. The display is deliberately allowed to sleep."""
    if sys.platform != "win32":
        return False
    try:
        flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED if enabled else ES_CONTINUOUS
        return ctypes.windll.kernel32.SetThreadExecutionState(flags) != 0
    except Exception:
        return False


# --------------------------------------------------------------------------
# camera names (best effort - Windows does not guarantee that this order
# matches OpenCV indices, which is why the picker shows thumbnails)
# --------------------------------------------------------------------------
_camera_names_cache = None


def camera_names():
    global _camera_names_cache
    if _camera_names_cache is not None:
        return _camera_names_cache
    names = []
    try:
        import pythoncom
        import win32com.client.dynamic
        # Dynamic dispatch on purpose: the cached-mode path rebuilds win32com's
        # gen_py cache on first use, which is slow and noisy. CoInitialize is
        # required because this also gets called from the probe thread.
        # Not paired with CoUninitialize: tearing COM down here would run while
        # the WMI objects are still referenced, which spams "Win32 exception
        # occurred releasing IUnknown". pywin32 cleans up at thread exit.
        pythoncom.CoInitialize()
        locator = win32com.client.dynamic.Dispatch("WbemScripting.SWbemLocator")
        service = locator.ConnectServer(".", "root\\cimv2")
        rows = service.ExecQuery(
            "SELECT Name FROM Win32_PnPEntity WHERE PNPClass='Camera'")
        names = [row.Name for row in rows]
    except Exception:
        names = []
    _camera_names_cache = names
    return names


# --------------------------------------------------------------------------
# session
# --------------------------------------------------------------------------
def _atomic_write(path, text):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


class Session:
    def __init__(self, path, data):
        self.path = path
        self.data = data

    # -- lifecycle ---------------------------------------------------------
    @classmethod
    def create(cls, camera_index, camera_name, study_seconds, output_seconds,
               fps, width, height, quality):
        root = ensure_root()
        session_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path = os.path.join(root, session_id)
        suffix = 1
        while os.path.exists(path):
            suffix += 1
            path = os.path.join(root, "%s_%d" % (session_id, suffix))
        os.makedirs(os.path.join(path, FRAMES_DIRNAME))

        total_slots, interval = compute_plan(study_seconds, output_seconds, fps)
        data = {
            "version": 1,
            "session_id": os.path.basename(path),
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "camera": {"index": camera_index, "name": camera_name},
            "plan": {
                "study_seconds": study_seconds,
                "target_output_seconds": output_seconds,
                "target_fps": fps,
                "total_slots": total_slots,
                "interval_seconds": round(interval, 3),
            },
            "resolution": [width, height],
            "jpeg_quality": quality,
            "state": STATE_RUNNING,
            "study_elapsed_seconds": 0.0,
            "suspended_seconds": 0.0,
        }
        session = cls(path, data)
        session.save()
        return session

    @classmethod
    def load(cls, path):
        with open(os.path.join(path, MANIFEST_NAME), encoding="utf-8") as fh:
            return cls(path, json.load(fh))

    def save(self):
        _atomic_write(os.path.join(self.path, MANIFEST_NAME),
                      json.dumps(self.data, indent=2))

    # -- accessors ---------------------------------------------------------
    @property
    def frames_dir(self):
        return os.path.join(self.path, FRAMES_DIRNAME)

    @property
    def plan(self):
        return self.data["plan"]

    @property
    def total_slots(self):
        return self.plan["total_slots"]

    @property
    def interval(self):
        return self.plan["interval_seconds"]

    @property
    def state(self):
        return self.data.get("state", STATE_STOPPED)

    def set_state(self, state):
        self.data["state"] = state
        self.save()

    def frame_path(self, slot):
        return os.path.join(self.frames_dir, "%06d.jpg" % slot)

    # -- append-only log ---------------------------------------------------
    def _append(self, rec):
        rec["t"] = datetime.now().isoformat(timespec="seconds")
        with open(os.path.join(self.path, LOG_NAME), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
            fh.flush()

    def log(self, slot, status, **extra):
        rec = {"slot": slot, "status": status}
        if status == OK:
            rec["file"] = "%06d.jpg" % slot
        rec.update(extra)
        self._append(rec)

    def log_event(self, event, **extra):
        rec = {"event": event}
        rec.update(extra)
        self._append(rec)

    def read_log(self):
        path = os.path.join(self.path, LOG_NAME)
        out = []
        if not os.path.exists(path):
            return out
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        return out

    def last_slot(self):
        """Highest slot attempted, from the log and from disk. Crash-safe."""
        highest = 0
        for rec in self.read_log():
            if "slot" in rec:
                highest = max(highest, rec["slot"])
        for slot, _ in list_frames(self.frames_dir):
            highest = max(highest, slot)
        return highest


# --------------------------------------------------------------------------
# frame discovery (works with or without a manifest)
# --------------------------------------------------------------------------
_NUM_RE = re.compile(r"(\d+)")


def list_frames(directory):
    """[(slot, fullpath)] sorted by slot, then name."""
    if not os.path.isdir(directory):
        return []
    out = []
    for name in os.listdir(directory):
        if not name.lower().endswith((".jpg", ".jpeg", ".png")):
            continue
        match = _NUM_RE.search(name)
        slot = int(match.group(1)) if match else 0
        out.append((slot, os.path.join(directory, name)))
    out.sort(key=lambda t: (t[0], t[1]))
    return out


def session_frames_dir(session_path):
    """Frames live in frames/, but tolerate loose images in the folder."""
    sub = os.path.join(session_path, FRAMES_DIRNAME)
    return sub if os.path.isdir(sub) else session_path


def scan_sessions():
    """Every session folder under the root that holds at least one image."""
    root = sessions_root()
    if not os.path.isdir(root):
        return []
    found = []
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        frames = list_frames(session_frames_dir(path))
        if not frames:
            continue
        info = {"path": path, "name": name, "frames": frames,
                "count": len(frames), "studied": None, "camera": "",
                "resolution": None, "missed": 0, "state": ""}
        try:
            session = Session.load(path)
            info["studied"] = (session.data.get("study_elapsed_seconds")
                               or session.plan["study_seconds"])
            info["camera"] = (session.data.get("camera") or {}).get("name", "")
            info["resolution"] = tuple(session.data.get("resolution") or ())
            info["state"] = session.state
            attempted = max((r["slot"] for r in session.read_log() if "slot" in r),
                            default=0)
            info["missed"] = max(0, attempted - len(frames))
        except Exception:
            pass
        found.append(info)
    found.sort(key=lambda i: i["name"])
    return found
