"""One-shot camera helper. Spawned as a throwaway subprocess per photo.

Running the capture in its own process is what makes the "polite camera"
policy actually reliable: the device handle is released by process exit, so it
cannot leak even if the driver misbehaves, and a hung camera kills a
disposable process instead of a six-hour session. It also keeps cv2 out of the
resident capturer entirely.

    grabber.py shot  --index N --out PATH [--width W --height H --quality Q]
    grabber.py probe --outdir DIR [--max N]

Exit codes are the contract with capture.py; see common.EXIT_*.
"""
import argparse
import json
import os
import sys

# Best effort only: on Windows a DLL loaded afterwards may keep its own copy
# of the environment, so the .bat launchers set these before Python starts.
# Harmless either way - the noise is cosmetic and invisible under pythonw.
os.environ.setdefault("OPENCV_LOG_LEVEL", "SILENT")
os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "-8")

EXIT_OK = 0
EXIT_BUSY = 2
EXIT_NO_FRAME = 3
EXIT_NO_DEVICE = 4
EXIT_WRITE = 5

# Webcams hand back black or badly exposed frames straight after opening,
# while auto-exposure and auto-white-balance settle.
WARMUP_FRAMES = 4


def open_camera(index, width=None, height=None):
    """Try Media Foundation first, then DirectShow. None if unavailable."""
    import cv2
    for backend in (cv2.CAP_MSMF, cv2.CAP_DSHOW):
        try:
            cap = cv2.VideoCapture(index, backend)
        except Exception:
            continue
        if cap is not None and cap.isOpened():
            if width and height:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            return cap
        if cap is not None:
            cap.release()
    return None


def read_settled_frame(cap):
    """Drain the warm-up frames and return the first good one."""
    frame = None
    for _ in range(WARMUP_FRAMES):
        ok, candidate = cap.read()
        if ok and candidate is not None and candidate.size:
            frame = candidate
    return frame


def write_image(path, image, quality=None):
    """Encode in memory, then write bytes.

    cv2.imwrite goes through a narrow-char path and fails on non-ASCII
    directories, which a redirected Pictures folder can easily contain.
    """
    import cv2
    ext = os.path.splitext(path)[1] or ".jpg"
    params = []
    if ext.lower() in (".jpg", ".jpeg") and quality:
        params = [cv2.IMWRITE_JPEG_QUALITY, int(quality)]
    ok, buf = cv2.imencode(ext, image, params)
    if not ok:
        return False
    tmp = path + ".part"
    with open(tmp, "wb") as fh:
        fh.write(buf.tobytes())
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return True


def cmd_shot(args):
    import cv2
    cap = open_camera(args.index, args.width, args.height)
    if cap is None:
        # Either another app holds the device, or the index is gone. Probing
        # tells the two apart so the UI can say something useful.
        probe = cv2.VideoCapture(args.index, cv2.CAP_DSHOW)
        exists = probe is not None and probe.isOpened()
        if probe is not None:
            probe.release()
        return EXIT_BUSY if exists else EXIT_NO_DEVICE

    try:
        frame = read_settled_frame(cap)
    finally:
        cap.release()

    if frame is None:
        return EXIT_NO_FRAME

    # The camera is free to ignore the resolution we asked for, so pin every
    # frame to the session's locked size before saving.
    if args.width and args.height:
        h, w = frame.shape[:2]
        if (w, h) != (args.width, args.height):
            interp = cv2.INTER_AREA if w > args.width else cv2.INTER_LINEAR
            frame = cv2.resize(frame, (args.width, args.height), interpolation=interp)

    try:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        if not write_image(args.out, frame, args.quality):
            return EXIT_WRITE
    except OSError:
        return EXIT_WRITE
    return EXIT_OK


def cmd_probe(args):
    """Grab a thumbnail from every camera index we can open.

    The thumbnails are the point: Windows gives no dependable mapping from an
    OpenCV index to a device name, so the only trustworthy way to avoid
    recording six hours of infrared is to look at the picture.
    """
    import cv2
    os.makedirs(args.outdir, exist_ok=True)
    results = []
    for index in range(args.max):
        cap = open_camera(index)
        if cap is None:
            continue
        try:
            frame = read_settled_frame(cap)
        finally:
            cap.release()
        if frame is None:
            continue
        h, w = frame.shape[:2]
        scale = 240.0 / max(1, w)
        thumb = cv2.resize(frame, (240, max(1, int(h * scale))),
                           interpolation=cv2.INTER_AREA)
        # PNG so plain Tk 8.6 can display it without Pillow.
        path = os.path.join(args.outdir, "cam%d.png" % index)
        try:
            write_image(path, thumb)
        except OSError:
            continue
        results.append({"index": index, "width": w, "height": h, "thumb": path})
    sys.stdout.write(json.dumps(results))
    return EXIT_OK


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    shot = sub.add_parser("shot")
    shot.add_argument("--index", type=int, required=True)
    shot.add_argument("--out", required=True)
    shot.add_argument("--width", type=int)
    shot.add_argument("--height", type=int)
    shot.add_argument("--quality", type=int, default=85)
    shot.set_defaults(func=cmd_shot)

    probe = sub.add_parser("probe")
    probe.add_argument("--outdir", required=True)
    probe.add_argument("--max", type=int, default=5)
    probe.set_defaults(func=cmd_probe)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
