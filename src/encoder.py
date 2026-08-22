"""Video encoders for the renderer.

A study time-lapse is the easiest thing in the world to compress: the same
room, the same camera, the same light, and a subject who barely moves. Almost
everything in a frame is already in the frame before it, so an encoder that is
allowed to spend effort on inter-frame prediction can throw most of the data
away. The bit that fights back is webcam sensor noise, which changes in every
pixel of every frame and looks like real detail to an encoder - a light
temporal denoise before encoding is worth more than any codec setting.

Two paths, in order of preference:

* FFmpeg (x264/x265), driven over a pipe. Constant-quality encoding, long GOPs
  and denoising - roughly 10-30x smaller than what we could write before.
* OpenCV's own VideoWriter, as a fallback when no FFmpeg is available. It has
  no quality control at all: on Windows it usually lands on the Media
  Foundation H.264 encoder, which writes 720p at a flat ~28 Mbit/s no matter
  how still the picture is.
"""
import os
import subprocess
import shutil
import sys
import tempfile

# Light temporal+spatial denoise. Sensor noise is what stops a static scene
# from compressing; this removes it without visibly softening the picture.
DENOISE_FILTER = "hqdn3d=4:3:6:4.5"

# Frames must have even dimensions for yuv420p. Pad rather than scale, so a
# camera with an odd height is not resampled.
PAD_FILTER = "pad=ceil(iw/2)*2:ceil(ih/2)*2"

H264 = "H.264 - plays everywhere"
H265 = "H.265 - about half the size"
CODEC_CHOICES = [H264, H265]

# label -> (x264 crf, x265 crf). CRF is constant quality: the encoder spends
# whatever bitrate a given visual quality needs, which is exactly right here
# because a still scene then costs almost nothing.
QUALITY_CHOICES = ["Smallest file", "Balanced", "High quality"]
CRF = {
    "Smallest file": (30, 33),
    "Balanced": (25, 28),
    "High quality": (20, 24),
}
DEFAULT_QUALITY = "Balanced"

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

_ffmpeg_cache = None
_encoders_cache = {}


# --------------------------------------------------------------------------
# finding ffmpeg
# --------------------------------------------------------------------------
def find_ffmpeg():
    """Path to a usable ffmpeg binary, or None. Cached after the first call."""
    global _ffmpeg_cache
    if _ffmpeg_cache is not None:
        return _ffmpeg_cache or None

    for candidate in _ffmpeg_candidates():
        if candidate and os.path.isfile(candidate):
            _ffmpeg_cache = candidate
            return candidate
    _ffmpeg_cache = ""
    return None


def _ffmpeg_candidates():
    yield os.environ.get("STUDY_TIMELAPSE_FFMPEG")
    # A binary dropped next to the app wins over anything installed.
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    yield os.path.join(root, "bin", "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
    try:
        import imageio_ffmpeg
        yield imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    yield shutil.which("ffmpeg")


def has_encoder(ffmpeg, name):
    """Whether this ffmpeg build can encode with `name` (e.g. libx264)."""
    if ffmpeg not in _encoders_cache:
        try:
            out = subprocess.run([ffmpeg, "-hide_banner", "-encoders"],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 creationflags=_CREATE_NO_WINDOW, timeout=20)
            _encoders_cache[ffmpeg] = out.stdout.decode("utf-8", "replace")
        except Exception:
            _encoders_cache[ffmpeg] = ""
    return name in _encoders_cache[ffmpeg]


def describe_backend():
    """One line for the UI about what will do the encoding."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return ("no FFmpeg found - files will be large "
                "(pip install imageio-ffmpeg to fix)")
    return "FFmpeg: %s" % os.path.basename(ffmpeg)


# --------------------------------------------------------------------------
# encoders
# --------------------------------------------------------------------------
class FfmpegEncoder:
    """Raw BGR frames in over a pipe, an mp4 out."""

    def __init__(self, path, fps, size, quality, codec, denoise=True):
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            raise RuntimeError("No FFmpeg binary was found.")
        crf_h264, crf_h265 = CRF.get(quality, CRF[DEFAULT_QUALITY])

        use_h265 = codec == H265 and has_encoder(ffmpeg, "libx265")
        if codec == H265 and not use_h265:
            codec = H264
        if not use_h265 and not has_encoder(ffmpeg, "libx264"):
            raise RuntimeError("This FFmpeg build has no x264 or x265 encoder.")

        chain = ([DENOISE_FILTER] if denoise else []) + [PAD_FILTER]
        command = [
            ffmpeg, "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", "%dx%d" % (size[0], size[1]), "-r", "%g" % fps, "-i", "-",
            "-vf", ",".join(chain),
        ]
        if use_h265:
            self.label = "H.265 (x265, crf %d)" % crf_h265
            command += ["-c:v", "libx265", "-preset", "medium",
                        "-crf", str(crf_h265),
                        # Silence x265's own banner, and tag the track hvc1 so
                        # QuickTime and Windows players recognise it.
                        "-x265-params", "log-level=error",
                        "-tag:v", "hvc1"]
        else:
            self.label = "H.264 (x264, crf %d)" % crf_h264
            # A long GOP with scene detection off is the whole point: nothing
            # here is a real cut, so every keyframe after the first is waste.
            # More B-frames and reference frames let a returning pose be
            # predicted from a frame further back.
            command += ["-c:v", "libx264", "-preset", "medium",
                        "-crf", str(crf_h264),
                        "-g", "600", "-sc_threshold", "0",
                        "-bf", "5", "-refs", "6"]
        command += ["-pix_fmt", "yuv420p", "-movflags", "+faststart", path]

        # A file rather than a pipe for the log: with stderr on a pipe nobody
        # drains, a chatty failure would deadlock the render.
        handle, self._log_path = tempfile.mkstemp(suffix=".log")
        self._log = os.fdopen(handle, "w+b")
        self.process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=self._log, stderr=self._log,
            creationflags=_CREATE_NO_WINDOW)

    def write(self, image):
        try:
            self.process.stdin.write(image.tobytes())
        except (BrokenPipeError, OSError):
            # ffmpeg died; its own message is the useful one.
            raise RuntimeError(self._failure_text())

    def close(self):
        """Finish the file. Raises if ffmpeg reported a problem."""
        try:
            self.process.stdin.close()
        except OSError:
            pass
        code = self.process.wait()
        text = self._read_log()
        self._cleanup()
        if code != 0:
            raise RuntimeError(text or "FFmpeg exited with code %d." % code)

    def abort(self):
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.kill()
            self.process.wait(timeout=5)
        except Exception:
            pass
        self._cleanup()

    def _failure_text(self):
        self.process.wait()
        return self._read_log() or "FFmpeg stopped unexpectedly."

    def _read_log(self):
        try:
            self._log.flush()
            self._log.seek(0)
            text = self._log.read().decode("utf-8", "replace").strip()
        except Exception:
            return ""
        # Only the tail is ever interesting, and only a few lines fit a dialog.
        return "\n".join(text.splitlines()[-6:])

    def _cleanup(self):
        try:
            self._log.close()
        except Exception:
            pass
        try:
            os.unlink(self._log_path)
        except OSError:
            pass


# avc1 (H.264) first because it is what browsers, phones and chat apps can
# actually play; mp4v falls back to MPEG-4 Part 2, which many of them refuse.
# OpenCV logs a scary-looking OpenH264 failure on the way and then recovers,
# so trust the fourcc read back from the finished file, not the request.
FOURCC_CANDIDATES = ("avc1", "mp4v")


class OpenCvEncoder:
    """Fallback with no quality control - large files, but it always works."""

    def __init__(self, path, fps, size, quality=None, codec=None, denoise=False):
        import cv2
        self.path = path
        self.writer = None
        for name in FOURCC_CANDIDATES:
            writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*name), fps, size)
            if writer.isOpened():
                self.writer = writer
                break
            writer.release()
        if self.writer is None:
            raise RuntimeError("No usable video encoder was available.")
        self.label = "OpenCV (uncompressed settings)"

    def write(self, image):
        self.writer.write(image)

    def close(self):
        self.writer.release()
        self.writer = None
        self.label = actual_fourcc(self.path)

    def abort(self):
        if self.writer is not None:
            self.writer.release()
            self.writer = None


def open_encoder(path, fps, size, quality=DEFAULT_QUALITY, codec=H264,
                 denoise=True):
    """Best available encoder for `path`, falling back to OpenCV."""
    if find_ffmpeg():
        return FfmpegEncoder(path, fps, size, quality, codec, denoise)
    return OpenCvEncoder(path, fps, size)


def actual_fourcc(path):
    """The codec really present in a written file."""
    import cv2
    cap = cv2.VideoCapture(path)
    value = int(cap.get(cv2.CAP_PROP_FOURCC))
    cap.release()
    tag = "".join(chr((value >> (8 * i)) & 0xFF) for i in range(4)).strip()
    return tag or "unknown"
