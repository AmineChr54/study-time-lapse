"""Dark UI theme for the capture window.

Tk's canvas draws shapes without antialiasing, which looks harsh for circles
and rings. Everything round here is therefore rendered with Pillow at 4x and
downsampled, which costs about 1.6 MB of memory and gives clean edges. Text is
left to Tk, whose font rendering is already antialiased by Windows.

Palette sampled from the Windows 11 Clock focus-session widget.
"""
import ctypes
import os
import sys

from PIL import Image, ImageDraw, ImageTk

# -- palette ---------------------------------------------------------------
BG = "#202020"          # window background
SURFACE = "#2B2B2B"     # inner disc of the ring
BUTTON = "#2D2D2D"      # secondary button
BUTTON_HOVER = "#3A3A3A"
TRACK = "#383838"        # unlit ring dots
ACCENT = "#F38064"       # coral - progress and primary button
ACCENT_HOVER = "#F79079"
TEXT = "#FFFFFF"
TEXT_DIM = "#9D9D9D"
TEXT_FAINT = "#6E6E6E"
WARN = "#F2C14E"
ERROR = "#F3746A"

FONTS = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
ICON_FONT = os.path.join(FONTS, "segmdl2.ttf")

# Segoe MDL2 Assets glyphs, chosen by rendering the candidates and looking:
# E8B7 is a document rather than a folder in this font, and E718 is a flatter
# pin than E840.
ICON_STOP = "\uE71A"
ICON_FOLDER = "\uE838"
ICON_PIN = "\uE840"
ICON_CHECK = "\uE73E"

SCALE = 4               # supersampling factor for every rendered shape


def ui_font(size, weight="normal"):
    """A Tk font tuple. Segoe UI Light matches the Clock widget's big digits."""
    family = {"light": "Segoe UI Light", "semilight": "Segoe UI Semilight",
              "semibold": "Segoe UI Semibold", "normal": "Segoe UI"}[weight]
    return (family, size)


# -- DPI ------------------------------------------------------------------
def enable_dpi_awareness():
    """Must run before the first Tk window exists.

    Without it Windows bitmap-scales the whole app on a high-DPI display, which
    is why the type looks soft next to native windows. Once aware, Tk pixels are
    real pixels, so all geometry below is written in logical units and
    multiplied by dpi_scale().
    """
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # per-monitor aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def dpi_scale(window):
    """Pixels per logical unit for the display this window is on."""
    if sys.platform != "win32":
        return 1.0
    try:
        dpi = ctypes.windll.user32.GetDpiForWindow(_hwnd(window))
        if dpi:
            return dpi / 96.0
    except Exception:
        pass
    try:
        return window.winfo_fpixels("1i") / 96.0
    except Exception:
        return 1.0


def tune_tk_scaling(window, scale):
    """Make point-sized fonts land at the right physical size."""
    try:
        window.tk.call("tk", "scaling", scale * 96.0 / 72.0)
    except Exception:
        pass


# -- window chrome ---------------------------------------------------------
def _hwnd(window):
    window.update_idletasks()
    handle = ctypes.windll.user32.GetParent(window.winfo_id())
    return handle or window.winfo_id()


def _colorref(hex_color):
    """#RRGGBB -> Windows COLORREF (0x00BBGGRR)."""
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return (b << 16) | (g << 8) | r


def apply_dark_chrome(window, caption=BG):
    """Dark, rounded, seamless title bar via DWM.

    Setting the caption colour to the window background makes the native title
    bar blend into the content instead of sitting on top as a light strip, so
    we keep minimise/close and taskbar behaviour without the mismatched frame.
    """
    if sys.platform != "win32":
        return False
    try:
        dwm = ctypes.windll.dwmapi
        handle = _hwnd(window)
        value = ctypes.c_int(1)
        # DWMWA_USE_IMMERSIVE_DARK_MODE
        dwm.DwmSetWindowAttribute(handle, 20, ctypes.byref(value), 4)
        # DWMWA_WINDOW_CORNER_PREFERENCE = DWMWCP_ROUND
        dwm.DwmSetWindowAttribute(handle, 33, ctypes.byref(ctypes.c_int(2)), 4)
        # DWMWA_CAPTION_COLOR / DWMWA_BORDER_COLOR (Windows 11 only; ignored below)
        dwm.DwmSetWindowAttribute(handle, 35,
                                  ctypes.byref(ctypes.c_int(_colorref(caption))), 4)
        dwm.DwmSetWindowAttribute(handle, 34,
                                  ctypes.byref(ctypes.c_int(_colorref(caption))), 4)
        return True
    except Exception:
        return False


def round_window(window, radius):
    """Rounded corners for a borderless window.

    DWM's corner preference only applies to windows that have a frame, so an
    overrideredirect window needs an explicit region instead.
    """
    if sys.platform != "win32":
        return False
    try:
        window.update_idletasks()
        handle = _hwnd(window)
        width = window.winfo_width() or window.winfo_reqwidth()
        height = window.winfo_height() or window.winfo_reqheight()
        region = ctypes.windll.gdi32.CreateRoundRectRgn(
            0, 0, width + 1, height + 1, radius * 2, radius * 2)
        ctypes.windll.user32.SetWindowRgn(handle, region, True)
        return True
    except Exception:
        return False


# -- rendered shapes -------------------------------------------------------
def _hex_to_rgb(value):
    return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))


def _blend(fg, bg, alpha):
    f, b = _hex_to_rgb(fg), _hex_to_rgb(bg)
    return tuple(int(round(f[i] * alpha + b[i] * (1 - alpha))) for i in range(3))


def ring_image(size, fraction, dots=60, bg=BG, disc=SURFACE,
               track=TRACK, accent=ACCENT, dot_ratio=0.915, dot_px=0.022):
    """The progress ring: a dotted circle whose dots light up as it fills.

    Proportions measured off the Windows 11 Clock focus-session widget, where
    the dots sit just inside the filled disc rather than orbiting outside it.
    Returns a PIL image; the caller keeps a reference via ImageTk.
    """
    import math
    big = size * SCALE
    img = Image.new("RGB", (big, big), _hex_to_rgb(bg))
    draw = ImageDraw.Draw(img)

    centre = big / 2.0
    disc_radius = big * 0.5 - SCALE          # the filled inner circle
    dot_radius = disc_radius * dot_ratio     # dots sit inside the disc
    dot_size = big * dot_px / 2.0

    draw.ellipse([centre - disc_radius, centre - disc_radius,
                  centre + disc_radius, centre + disc_radius],
                 fill=_hex_to_rgb(disc))

    lit = int(round(max(0.0, min(1.0, fraction)) * dots))
    for i in range(dots):
        angle = math.radians(-90 + (360.0 * i / dots))   # 12 o'clock, clockwise
        x = centre + dot_radius * math.cos(angle)
        y = centre + dot_radius * math.sin(angle)
        colour = _hex_to_rgb(accent) if i < lit else _hex_to_rgb(track)
        radius = dot_size * (1.35 if i < lit else 1.0)
        draw.ellipse([x - radius, y - radius, x + radius, y + radius], fill=colour)

    return img.resize((size, size), Image.LANCZOS)


def _icon_font(px):
    from PIL import ImageFont
    try:
        return ImageFont.truetype(ICON_FONT, px)
    except OSError:
        return None


def button_image(size, glyph, fill, glyph_colour=TEXT, bg=BG):
    """A flat circular icon button."""
    big = size * SCALE
    img = Image.new("RGB", (big, big), _hex_to_rgb(bg))
    draw = ImageDraw.Draw(img)
    draw.ellipse([0, 0, big - 1, big - 1], fill=_hex_to_rgb(fill))

    font = _icon_font(int(big * 0.42))
    if font is not None:
        box = draw.textbbox((0, 0), glyph, font=font)
        draw.text((big / 2 - (box[0] + box[2]) / 2, big / 2 - (box[1] + box[3]) / 2),
                  glyph, font=font, fill=_hex_to_rgb(glyph_colour))
    else:
        # Segoe MDL2 missing: fall back to a plain square so the button still reads
        side = big * 0.3
        draw.rectangle([big / 2 - side / 2, big / 2 - side / 2,
                        big / 2 + side / 2, big / 2 + side / 2],
                       fill=_hex_to_rgb(glyph_colour))
    return img.resize((size, size), Image.LANCZOS)


def photo(image):
    return ImageTk.PhotoImage(image)


_icon_cache = {}


def apply_icon(window):
    """Window and taskbar icon.

    iconbitmap takes the .ico (Windows uses it for the title bar and Alt-Tab);
    iconphoto covers the taskbar and anything that wants a bitmap. The
    PhotoImage is cached because Tk keeps only a weak reference to it.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assets = os.path.join(root, "assets")
    ico = os.path.join(assets, "logo.ico")
    png = os.path.join(assets, "logo.png")
    try:
        if os.path.exists(ico):
            window.iconbitmap(default=ico)
    except Exception:
        pass
    try:
        if os.path.exists(png):
            if png not in _icon_cache:
                image = Image.open(png)
                image.thumbnail((256, 256), Image.LANCZOS)
                _icon_cache[png] = ImageTk.PhotoImage(image)
            window.iconphoto(True, _icon_cache[png])
    except Exception:
        pass


# -- ttk styling for the setup screen --------------------------------------
def style_widgets(root):
    """Dark ttk styling, so the setup screen matches the recording view."""
    from tkinter import ttk
    style = ttk.Style(root)
    style.theme_use("clam")

    style.configure(".", background=BG, foreground=TEXT,
                    fieldbackground=SURFACE, bordercolor=TRACK,
                    lightcolor=SURFACE, darkcolor=SURFACE,
                    focuscolor=ACCENT, font=ui_font(10))
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=TEXT)
    style.configure("Dim.TLabel", foreground=TEXT_DIM)
    style.configure("Faint.TLabel", foreground=TEXT_FAINT)
    style.configure("Warn.TLabel", foreground=WARN)
    style.configure("Error.TLabel", foreground=ERROR)
    style.configure("Title.TLabel", font=ui_font(16, "light"), foreground=TEXT)
    style.configure("Head.TLabel", font=ui_font(10, "semibold"), foreground=TEXT)

    style.configure("TButton", background=BUTTON, foreground=TEXT,
                    borderwidth=0, focusthickness=0, padding=(14, 7))
    style.map("TButton",
              background=[("active", BUTTON_HOVER), ("disabled", "#262626")],
              foreground=[("disabled", TEXT_FAINT)])
    style.configure("Accent.TButton", background=ACCENT, foreground="#1A1A1A",
                    font=ui_font(10, "semibold"))
    style.map("Accent.TButton",
              background=[("active", ACCENT_HOVER), ("disabled", "#3A2E2A")],
              foreground=[("disabled", TEXT_FAINT)])

    for widget in ("TEntry", "TSpinbox", "TCombobox"):
        style.configure(widget, fieldbackground=SURFACE, background=SURFACE,
                        foreground=TEXT, arrowcolor=TEXT_DIM,
                        bordercolor=TRACK, insertcolor=TEXT, padding=4)
    style.map("TCombobox", fieldbackground=[("readonly", SURFACE)],
              selectbackground=[("readonly", SURFACE)],
              selectforeground=[("readonly", TEXT)])
    root.option_add("*TCombobox*Listbox.background", SURFACE)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", "#1A1A1A")

    style.configure("TCheckbutton", background=BG, foreground=TEXT_DIM)
    style.map("TCheckbutton", foreground=[("active", TEXT)],
              background=[("active", BG)])
    style.configure("TRadiobutton", background=BG, foreground=TEXT_DIM)
    style.map("TRadiobutton", foreground=[("active", TEXT), ("selected", TEXT)],
              background=[("active", BG)])
    style.configure("TSeparator", background=TRACK)
    style.configure("Horizontal.TScale", background=BG, troughcolor=SURFACE)
    style.configure("Horizontal.TProgressbar", background=ACCENT,
                    troughcolor=SURFACE, borderwidth=0, thickness=6)
    return style


class CanvasButton:
    """A flat circular icon button drawn onto a Canvas, with a hover state.

    Images are held on the instance because Tk keeps only a weak reference to
    canvas images and would otherwise let them be collected mid-session.
    """

    def __init__(self, canvas, x, y, glyph, command, size=52, fill=BUTTON,
                 hover=BUTTON_HOVER, glyph_colour=TEXT, label=""):
        self.canvas = canvas
        self.command = command
        self.enabled = True
        self._images = {
            "normal": photo(button_image(size, glyph, fill, glyph_colour)),
            "hover": photo(button_image(size, glyph, hover, glyph_colour)),
            "off": photo(button_image(size, glyph, "#262626", TEXT_FAINT)),
        }
        self.item = canvas.create_image(x, y, image=self._images["normal"])
        self.label_item = None
        if label:
            self.label_item = canvas.create_text(
                x, y + size / 2 + 13, text=label, fill=TEXT_FAINT, font=ui_font(8))
        for event, handler in (("<Enter>", self._enter), ("<Leave>", self._leave),
                               ("<Button-1>", self._click)):
            canvas.tag_bind(self.item, event, handler)

    def _show(self, key):
        self.canvas.itemconfigure(self.item, image=self._images[key])

    def _enter(self, _event):
        if self.enabled:
            self._show("hover")
            self.canvas.configure(cursor="hand2")

    def _leave(self, _event):
        self._show("normal" if self.enabled else "off")
        self.canvas.configure(cursor="")

    def _click(self, _event):
        if self.enabled and self.command:
            self.command()

    def set_enabled(self, enabled):
        self.enabled = enabled
        self._show("normal" if enabled else "off")
        if self.label_item is not None:
            self.canvas.itemconfigure(
                self.label_item, fill=TEXT_FAINT if enabled else "#4A4A4A")

    def set_images(self, glyph, size=52, fill=BUTTON, hover=BUTTON_HOVER,
                   glyph_colour=TEXT, label=None):
        """Re-skin in place, for toggles such as always-on-top."""
        self._images["normal"] = photo(button_image(size, glyph, fill, glyph_colour))
        self._images["hover"] = photo(button_image(size, glyph, hover, glyph_colour))
        self._show("normal")
        if label is not None and self.label_item is not None:
            self.canvas.itemconfigure(self.label_item, text=label)
