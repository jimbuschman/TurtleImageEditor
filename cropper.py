"""
Template Cropper - align images to a shape template and export at an exact
physical size, so you print at 100% instead of guessing a percentage.

Run:  python cropper.py

Mouse:  drag a handle = resize (keeps proportions, opposite side stays put)
        drag anywhere else = move      wheel = zoom (anchored at the cursor)
Keys:   arrows = nudge   [ ] = rotate   f = fill   c = fit   r = reset
"""

import json
import math
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageTk

import shapes as SH

HERE = Path(__file__).parent
SETTINGS = HERE / "settings.json"
SETUP_EXT = ".cropset"
AUTOSAVE = HERE / ("autosave" + SETUP_EXT)
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff", ".webp"}

MM_PER_IN = 25.4
PAGES_IN = {"Letter (8.5 x 11)": (8.5, 11.0), "A4 (210 x 297mm)": (8.2677, 11.6929)}
PREVIEW_SRC_MAX = 1400          # working copy size for a responsive preview
MIN_ZOOM, MAX_ZOOM = 0.05, 30.0


# ---------------------------------------------------------------- transform ---

class Item:
    """One image plus how it is positioned inside the shape box."""

    def __init__(self, path):
        self.path = Path(path)
        self._full = None
        self._preview = None
        # zoom 1.0 == exactly covers the box; pan is in box-height units
        self.zoom = 1.0
        self.pan = [0.0, 0.0]
        self.rotation = 0.0

    @property
    def full(self):
        if self._full is None:
            im = Image.open(self.path)
            self._full = im.convert("RGBA")
        return self._full

    @property
    def preview_src(self):
        if self._preview is None:
            im = self.full
            k = min(1.0, PREVIEW_SRC_MAX / max(im.size))
            self._preview = (im if k >= 1.0 else
                             im.resize((max(1, int(im.width * k)),
                                        max(1, int(im.height * k))), Image.LANCZOS))
        return self._preview

    def reset(self):
        self.zoom, self.pan, self.rotation = 1.0, [0.0, 0.0], 0.0

    def rotated(self, source):
        if abs(self.rotation) <= 0.01:
            return source
        return source.rotate(-self.rotation, resample=Image.BICUBIC, expand=True)

    def draw(self, surface, box_origin, box_size, source,
             resample=Image.LANCZOS, into=None):
        """
        Paint the image onto a `surface`-sized RGBA canvas and report the
        rectangle it occupies, in surface coordinates.

        The image is positioned relative to the shape box, but may be drawn
        well outside it - that is what lets the preview show the whole photo
        with grab handles on its edges.

        Only the part that lands on the surface is ever scaled. Scaling the
        whole source first would allocate gigabytes at high zoom.
        """
        sw, sh = surface
        bx, by = box_origin
        bw, bh = box_size
        rot = self.rotated(source)
        k = max(bw / rot.width, bh / rot.height) * self.zoom      # cover * zoom
        dw, dh = rot.width * k, rot.height * k
        ox = bx + bw / 2.0 + self.pan[0] * bh - dw / 2.0
        oy = by + bh / 2.0 + self.pan[1] * bh - dh / 2.0
        rect = (ox, oy, ox + dw, oy + dh)

        canvas = into if into is not None else Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
        # source-space window that lands on the surface, plus filter support
        pad = math.ceil(4.0 / k) + 2
        cx0 = max(0, math.floor((0 - ox) / k) - pad)
        cy0 = max(0, math.floor((0 - oy) / k) - pad)
        cx1 = min(rot.width, math.ceil((sw - ox) / k) + pad)
        cy1 = min(rot.height, math.ceil((sh - oy) / k) + pad)
        if cx1 <= cx0 or cy1 <= cy0:
            return canvas, rect                                   # fully off-surface

        piece = rot.crop((cx0, cy0, cx1, cy1))
        tw = max(1, int(round((cx1 - cx0) * k)))
        th = max(1, int(round((cy1 - cy0) * k)))
        piece = piece.resize((tw, th), resample)
        canvas.paste(piece, (int(round(ox + cx0 * k)), int(round(oy + cy0 * k))), piece)
        return canvas, rect

    def render(self, box_w, box_h, source, resample=Image.LANCZOS):
        """The image as it sits inside the shape box - used for export."""
        return self.draw((box_w, box_h), (0, 0), (box_w, box_h), source,
                         resample)[0]

    def rotated_size(self, source):
        """Bounding size of the source once rotated - no pixels moved."""
        t = math.radians(self.rotation)
        c, s = abs(math.cos(t)), abs(math.sin(t))
        w, h = source.size
        return w * c + h * s, w * s + h * c

    def fit_zoom(self, box_w, box_h, source):
        """Zoom at which the whole image is visible inside the box."""
        rw, rh = self.rotated_size(source)
        cover = max(box_w / rw, box_h / rh)
        contain = min(box_w / rw, box_h / rh)
        return contain / cover

    def source_px_per_output_px(self, box_w, box_h, source):
        """>= 1 means there is enough original detail for a sharp print."""
        rw, rh = self.rotated_size(source)
        cover = max(box_w / rw, box_h / rh)
        return 1.0 / max(1e-9, cover * self.zoom)


# -------------------------------------------------------------------- export ---

# Grab handles: name -> (point on the rect, the point that stays put).
# Each is expressed as (x-fraction, y-fraction) across the rect, so a corner
# anchors the opposite corner and an edge anchors the opposite edge.
HANDLES = {
    "nw": ((0.0, 0.0), (1.0, 1.0)), "n": ((0.5, 0.0), (0.5, 1.0)),
    "ne": ((1.0, 0.0), (0.0, 1.0)), "e": ((1.0, 0.5), (0.0, 0.5)),
    "se": ((1.0, 1.0), (0.0, 0.0)), "s": ((0.5, 1.0), (0.5, 0.0)),
    "sw": ((0.0, 1.0), (1.0, 0.0)), "w": ((0.0, 0.5), (1.0, 0.5)),
}
HANDLE_CURSOR = {"nw": "size_nw_se", "se": "size_nw_se", "ne": "size_ne_sw",
                 "sw": "size_ne_sw", "n": "size_ns", "s": "size_ns",
                 "e": "size_we", "w": "size_we"}
GRAB = 8            # pixels of slop around a handle


def handle_points(rect):
    """Canvas position of every grab handle for an image rectangle."""
    x0, y0, x1, y1 = rect
    return {name: (x0 + fx * (x1 - x0), y0 + fy * (y1 - y0))
            for name, ((fx, fy), _) in HANDLES.items()}


def hit_handle(rect, x, y):
    """Which handle is under the cursor, nearest first, or None."""
    best, best_d = None, GRAB * GRAB
    for name, (hx, hy) in handle_points(rect).items():
        d = (hx - x) ** 2 + (hy - y) ** 2
        if d <= best_d:
            best, best_d = name, d
    return best


def resize_factor(name, anchor, start, cursor):
    """
    Uniform scale factor implied by dragging `name` from `start` to `cursor`,
    with `anchor` held fixed. Always one factor for both axes, so the image
    keeps its proportions - it scales, it never stretches.
    """
    ax, ay = anchor
    sx, sy = start
    cx, cy = cursor
    vx, vy = sx - ax, sy - ay
    if name in ("e", "w"):
        return (cx - ax) / vx if abs(vx) > 1e-6 else 1.0
    if name in ("n", "s"):
        return (cy - ay) / vy if abs(vy) > 1e-6 else 1.0
    d2 = vx * vx + vy * vy                      # corner: project onto diagonal
    if d2 < 1e-9:
        return 1.0
    return ((cx - ax) * vx + (cy - ay) * vy) / d2


def masked(item, box_w, box_h, shape_key, source=None):
    """The cropped image: shape applied, everything outside transparent."""
    src = source if source is not None else item.full
    img = item.render(box_w, box_h, src)
    mask = SH.shape_mask(shape_key, box_w, box_h)
    # transparent wherever the shape OR the image itself is absent
    out = img.copy()
    out.putalpha(ImageChops.darker(img.getchannel("A"), mask))
    return out


def draw_cut_line(img, shape_key, offset=(0, 0), size=None, width=1,
                  colour=(120, 120, 120)):
    size = size or img.size
    d = ImageDraw.Draw(img)
    pts = SH.outline_points(shape_key, size[0], size[1], offset=offset)
    d.line(pts + [pts[0]], fill=colour, width=width)


def _font(px):
    """A TrueType face scaled to the output DPI; PIL's default font would stay
    a fixed pixel size and shrink to nothing on a 300 DPI page."""
    for name in ("arial.ttf", "segoeui.ttf", "calibri.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, max(8, int(px)))
        except Exception:
            continue
    return ImageFont.load_default()


def draw_ruler_check(img, dpi, origin):
    """A 1-inch reference bar, so a ruler confirms the print came out at 100%."""
    d = ImageDraw.Draw(img)
    x, y = origin
    inch = dpi
    ink = (90, 90, 90)
    w = max(1, dpi // 150)
    d.line([(x, y), (x + inch, y)], fill=ink, width=w)
    for i in (0, 1):
        d.line([(x + i * inch, y - dpi // 12), (x + i * inch, y + dpi // 12)],
               fill=ink, width=w)
    d.line([(x + inch / 2, y - dpi // 24), (x + inch / 2, y + dpi // 24)],
           fill=(150, 150, 150), width=max(1, dpi // 200))
    d.text((x + inch + dpi // 10, y), "1 inch — measure this line. If it is not "
           "exactly 1\", reprint at 100% / Actual Size.",
           fill=ink, font=_font(dpi * 0.10), anchor="lm")


def grid_layout(page_px, cell, gap, margin, bottom_reserve=0):
    """Top-left positions for as many cells as fit on one page, centred."""
    pw, ph = page_px
    cw, ch = cell
    avail_w = pw - 2 * margin
    avail_h = ph - 2 * margin - bottom_reserve
    cols = max(1, int((avail_w + gap) // (cw + gap)))
    rows = max(1, int((avail_h + gap) // (ch + gap)))
    total_w = cols * cw + (cols - 1) * gap
    total_h = rows * ch + (rows - 1) * gap
    ox = max(margin, (pw - total_w) // 2)
    oy = max(margin, (ph - bottom_reserve - total_h) // 2)
    spots = [(ox + c * (cw + gap), oy + r * (ch + gap))
             for r in range(rows) for c in range(cols)]
    fits = cw <= avail_w and ch <= avail_h
    return spots, fits


DEFAULT_OUTDIR = HERE / "output"


def foreign_profile(p):
    """
    True when `p` sits inside a different user's profile folder.

    Setups and settings are portable files that get copied or committed between
    machines, so they routinely arrive holding an absolute path from whoever
    saved them. Writing there is not merely missing - Windows denies the whole
    parents=True chain at C:\\Users, which is a baffling 'Access is denied' for
    a folder the user never typed.
    """
    try:
        home = Path.home().resolve()
        users = home.parent
        p = Path(p).resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    if not p.is_relative_to(users) or p.is_relative_to(home):
        return False
    return p != users


def resolve_outdir(raw, probe=False):
    """
    The folder to actually write to, given whatever 'Save to' holds.

    Anything blank, relative, or belonging to another machine's user resolves to
    an `output` folder beside the script, which is always writable. With
    `probe`, the folder is created and test-written now so the fallback happens
    before a long export rather than after it.
    """
    raw = str(raw or "").strip().strip('"')
    if not raw:
        return DEFAULT_OUTDIR
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = HERE / p
    if foreign_profile(p):
        return DEFAULT_OUTDIR
    if not probe:
        return p
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe_file = p / ".cropper-write-test"
        probe_file.touch()
        probe_file.unlink()
        return p
    except OSError:
        return DEFAULT_OUTDIR


def store_outdir(p):
    """Save the path relative to the script when it lives inside the project, so
    the setup stays usable on the next machine that opens it."""
    p = Path(p)
    try:
        rel = p.relative_to(HERE)
    except ValueError:
        return str(p)
    return str(rel) if rel.parts else "."


def explain(exc, target=None):
    """
    A plain-language reason for a failed write, with the traceback kept below.

    The raw traceback alone is no help at the printer: the three ways this
    actually fails in practice - the file is open in a viewer, the folder is
    gone or read-only, the finished size is too big to fit in memory - all read
    as inscrutable OSErrors unless they are spelled out.
    """
    where = f"\n\n{target}" if target else ""
    if isinstance(exc, PermissionError):
        why = ("Windows would not let this file be written.\n\n"
               "It is almost always already open somewhere - close it in your "
               "PDF viewer or image editor and export again. If the folder "
               "itself is read-only, choose a different one under 'Save to'.")
    elif isinstance(exc, FileNotFoundError):
        why = ("That folder does not exist and could not be created.\n\n"
               "Pick a new one with the '...' button next to 'Save to'.")
    elif isinstance(exc, MemoryError):
        why = ("The finished size needs more memory than is available.\n\n"
               "Lower the DPI or the finished size and try again.")
    elif isinstance(exc, OSError):
        why = (f"The file could not be written ({exc.strerror or exc}).\n\n"
               "Check that the drive is connected and has free space, then "
               "try a different folder under 'Save to'.")
    else:
        why = "Something unexpected went wrong."
    return f"{why}{where}\n\n----- details -----\n{traceback.format_exc()}"


# ----------------------------------------------------------------------- app ---

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Template Cropper")
        self.geometry("1180x760")
        self.minsize(980, 640)

        self.items = []
        self.index = -1
        self.shape_labels = SH.labels()
        self.keys = list(self.shape_labels)
        self._photo = None
        self._drag = None
        self._box = (0, 0, 0, 0)     # x, y, w, h of the shape box on canvas
        self._rect = None            # where the image sits, for the handles

        cfg = self._load_settings()
        # Each template remembers its own finished size - the two shapes are
        # printed at very different sizes, so one shared field would be wrong
        # every time you switched.
        self.sizes = {}                      # key -> finished width in INCHES
        saved = cfg.get("sizes") or {}
        for k in self.keys:
            try:
                self.sizes[k] = float(saved[k])
            except (KeyError, TypeError, ValueError):
                self.sizes[k] = float(SH.default_width_in(k))
        self.shape_key = tk.StringVar(value=cfg.get("shape", self.keys[0]))
        if self.shape_key.get() not in self.keys:
            self.shape_key.set(self.keys[0])
        self.shape_label = tk.StringVar(
            value=self.shape_labels[self.shape_key.get()])
        self.units = tk.StringVar(value=cfg.get("units", "in"))
        self._entry_units = self.units.get()   # unit the size boxes are showing
        self.size_w = tk.StringVar()
        self._show_size()
        self.size_h = tk.StringVar(value="")
        self.lock_aspect = tk.BooleanVar(value=cfg.get("lock_aspect", True))
        self.dpi = tk.StringVar(value=cfg.get("dpi", "600"))
        self.fmt = tk.StringVar(value=cfg.get("fmt", "PDF (exact size, best for printing)"))
        self.page = tk.StringVar(value=cfg.get("page", "Letter (8.5 x 11)"))
        self.copies = tk.StringVar(value=cfg.get("copies", "1"))
        self.combine = tk.BooleanVar(value=cfg.get("combine", False))
        self.cut_line = tk.BooleanVar(value=cfg.get("cut_line", True))
        self.ruler = tk.BooleanVar(value=cfg.get("ruler", True))
        self.outdir = tk.StringVar(
            value=str(resolve_outdir(cfg.get("outdir"))))

        self._build_ui()
        self._sync_height()
        self._bind_keys()
        if AUTOSAVE.exists():            # pick up where the last session ended
            self._load_setup(AUTOSAVE)
        self.after(60, self.redraw)

    # ---- settings
    def _load_settings(self):
        try:
            return json.loads(SETTINGS.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_settings(self):
        self._remember_size()
        data = {"shape": self.shape_key.get(), "units": self.units.get(),
                "sizes": {k: round(v, 5) for k, v in self.sizes.items()},
                "lock_aspect": self.lock_aspect.get(),
                "dpi": self.dpi.get(), "fmt": self.fmt.get(), "page": self.page.get(),
                "copies": self.copies.get(), "combine": self.combine.get(),
                "cut_line": self.cut_line.get(),
                "ruler": self.ruler.get(),
                "outdir": store_outdir(self.outdir.get())}
        try:
            SETTINGS.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    # ---- ui
    def _build_ui(self):
        bar = ttk.Frame(self, padding=(8, 6))
        bar.pack(side="top", fill="x")
        ttk.Button(bar, text="Add images...", command=self.add_images).pack(side="left")
        ttk.Button(bar, text="Remove", command=self.remove_current).pack(side="left", padx=(4, 12))
        ttk.Label(bar, text="Template:").pack(side="left")
        cb = ttk.Combobox(bar, width=16, state="readonly",
                          textvariable=self.shape_label,
                          values=[self.shape_labels[k] for k in self.keys])
        cb.pack(side="left", padx=(4, 12))
        cb.bind("<<ComboboxSelected>>", lambda e: self._on_shape_pick())
        for txt, cmd in (("Fill", self.do_fill), ("Fit", self.do_fit), ("Reset", self.do_reset)):
            ttk.Button(bar, text=txt, width=6, command=cmd).pack(side="left", padx=2)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(bar, text="Rotate L", width=8,
                   command=lambda: self.rotate_by(-90)).pack(side="left", padx=2)
        ttk.Button(bar, text="Rotate R", width=8,
                   command=lambda: self.rotate_by(90)).pack(side="left", padx=2)
        self.rot_var = tk.DoubleVar(value=0.0)
        ttk.Scale(bar, from_=-180, to=180, variable=self.rot_var, length=150,
                  command=self._on_rot_slider).pack(side="left", padx=(8, 2))
        self.rot_lbl = ttk.Label(bar, text="0.0 deg", width=9)
        self.rot_lbl.pack(side="left")

        body = ttk.Frame(self)
        body.pack(side="top", fill="both", expand=True)

        left = ttk.Frame(body, padding=(8, 4))
        left.pack(side="left", fill="y")
        ttk.Label(left, text="Images").pack(anchor="w")
        self.listbox = tk.Listbox(left, width=26, exportselection=False)
        self.listbox.pack(fill="y", expand=True)
        self.listbox.bind("<<ListboxSelect>>", self._on_select)
        ttk.Separator(left, orient="horizontal").pack(fill="x", pady=(8, 6))
        ttk.Button(left, text="Save setup...", command=self.save_setup).pack(fill="x")
        ttk.Button(left, text="Open setup...",
                   command=self.open_setup).pack(fill="x", pady=(4, 0))
        self.setup_lbl = ttk.Label(left, text="setup: (unsaved)",
                                   foreground="#777", wraplength=170)
        self.setup_lbl.pack(anchor="w", pady=(6, 0))

        self.canvas = tk.Canvas(body, bg="#2b2b2b", highlightthickness=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self.redraw())
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Motion>", self._hover)
        self.canvas.bind("<MouseWheel>", self._wheel)

        right = ttk.Frame(body, padding=(10, 4))
        right.pack(side="left", fill="y")
        self._build_output_panel(right)

        self.status = ttk.Label(self, anchor="w", padding=(10, 4),
                                relief="sunken", text="Add images to begin.")
        self.status.pack(side="bottom", fill="x")

    def _build_output_panel(self, p):
        ttk.Label(p, text="Finished size", font=("", 10, "bold")).pack(anchor="w")
        row = ttk.Frame(p)
        row.pack(anchor="w", pady=(4, 0))
        ttk.Label(row, text="W").pack(side="left")
        e = ttk.Entry(row, width=7, textvariable=self.size_w)
        e.pack(side="left", padx=(2, 6))
        e.bind("<KeyRelease>", lambda ev: self._sync_height())
        ttk.Label(row, text="H").pack(side="left")
        self.h_entry = ttk.Entry(row, width=7, textvariable=self.size_h)
        self.h_entry.pack(side="left", padx=(2, 6))
        self.h_entry.bind("<KeyRelease>", lambda ev: self._sync_width())
        u = ttk.Combobox(row, width=4, state="readonly", textvariable=self.units,
                         values=["in", "mm"])
        u.pack(side="left")
        u.bind("<<ComboboxSelected>>", lambda ev: self._on_units())
        ttk.Checkbutton(p, text="Keep template proportions", variable=self.lock_aspect,
                        command=self._sync_height).pack(anchor="w", pady=(4, 8))

        ttk.Label(p, text="Output", font=("", 10, "bold")).pack(anchor="w")
        row2 = ttk.Frame(p)
        row2.pack(anchor="w", pady=(4, 0))
        ttk.Label(row2, text="DPI").pack(side="left")
        d = ttk.Combobox(row2, width=6, textvariable=self.dpi,
                         values=["150", "300", "600", "1200"])
        d.pack(side="left", padx=(2, 0))
        d.bind("<<ComboboxSelected>>", lambda ev: self.redraw())
        d.bind("<KeyRelease>", lambda ev: self.redraw())
        ttk.Combobox(p, width=34, state="readonly", textvariable=self.fmt,
                     values=["PDF (exact size, best for printing)",
                             "PNG (transparent background)",
                             "PNG (white background)"]).pack(anchor="w", pady=(6, 0))
        ttk.Label(p, text="PDF page").pack(anchor="w", pady=(8, 0))
        ttk.Combobox(p, width=34, state="readonly", textvariable=self.page,
                     values=list(PAGES_IN) + ["Trim to shape (no margin)"]).pack(anchor="w")
        row3 = ttk.Frame(p)
        row3.pack(anchor="w", pady=(6, 0))
        ttk.Label(row3, text="Copies of each").pack(side="left")
        ttk.Spinbox(row3, from_=1, to=500, width=5, textvariable=self.copies,
                    command=self._update_export_labels).pack(side="left", padx=(6, 0))
        ttk.Checkbutton(p, text="Put every image on one sheet",
                        variable=self.combine,
                        command=self._update_export_labels).pack(anchor="w",
                                                                 pady=(6, 0))
        ttk.Checkbutton(p, text="Print faint cut line",
                        variable=self.cut_line).pack(anchor="w", pady=(8, 0))
        ttk.Checkbutton(p, text="Add 1-inch ruler check",
                        variable=self.ruler).pack(anchor="w")

        ttk.Label(p, text="Save to", font=("", 10, "bold")).pack(anchor="w", pady=(12, 0))
        row4 = ttk.Frame(p)
        row4.pack(anchor="w")
        ttk.Entry(row4, width=26, textvariable=self.outdir).pack(side="left")
        ttk.Button(row4, text="...", width=3, command=self._pick_outdir).pack(side="left")

        ttk.Separator(p, orient="horizontal").pack(fill="x", pady=12)
        self.btn_one = ttk.Button(p, text="Export current",
                                  command=self.export_current)
        self.btn_one.pack(fill="x")
        self.btn_all = ttk.Button(p, text="Export all images",
                                  command=self.export_all)
        self.btn_all.pack(fill="x", pady=(6, 0))
        self.px_lbl = ttk.Label(p, text="", foreground="#555")
        self.px_lbl.pack(anchor="w", pady=(10, 0))
        self._update_export_labels()

    def _update_export_labels(self):
        """Say what the buttons will actually produce."""
        if not hasattr(self, "btn_all"):
            return
        if self.combine.get():
            self.btn_all.config(text="Export all onto one sheet")
        else:
            self.btn_all.config(text="Export all images")
        self._update_status()

    def _bind_keys(self):
        self.bind("<Left>", lambda e: self.nudge(-0.01, 0))
        self.bind("<Right>", lambda e: self.nudge(0.01, 0))
        self.bind("<Up>", lambda e: self.nudge(0, -0.01))
        self.bind("<Down>", lambda e: self.nudge(0, 0.01))
        self.bind("f", lambda e: self.do_fill())
        self.bind("c", lambda e: self.do_fit())
        self.bind("r", lambda e: self.do_reset())
        self.bind("[", lambda e: self.rotate_by(-1))
        self.bind("]", lambda e: self.rotate_by(1))
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        self._save_settings()
        # keep the work in progress so the next launch resumes it
        try:
            if self.items:
                self._write_setup(AUTOSAVE)
            elif AUTOSAVE.exists():
                AUTOSAVE.unlink()
        except Exception:
            pass
        self.destroy()

    # ---- size maths
    def _num(self, var, default=0.0):
        try:
            return float(str(var.get()).strip())
        except Exception:
            return default

    def _to_inches(self, v):
        return v / MM_PER_IN if self.units.get() == "mm" else v

    def _from_inches(self, v):
        return v * MM_PER_IN if self.units.get() == "mm" else v

    def _entry_to_inches(self, v):
        """
        Convert what is typed in the size boxes, using the unit those boxes are
        actually showing - NOT whatever `units` currently holds. Reading the
        live variable would silently reinterpret the number if the unit changed
        before the boxes were redrawn, turning 0.75 in into 0.75 mm.
        """
        return v / MM_PER_IN if self._entry_units == "mm" else v

    def _show_size(self):
        """Put the current template's remembered width into the entry."""
        inches = self.sizes.get(self.shape_key.get(), 3.0)
        self._entry_units = self.units.get()
        self.size_w.set(f"{self._from_inches(inches):g}")

    def _remember_size(self):
        w = self._entry_to_inches(self._num(self.size_w))
        if w > 0:
            self.sizes[self.shape_key.get()] = w

    def _sync_shape_label(self):
        """Keep the dropdown's friendly text in step with the selected key."""
        self.shape_label.set(self.shape_labels.get(self.shape_key.get(),
                                                   self.shape_key.get()))

    def _on_shape_pick(self):
        """Dropdown shows labels; translate the chosen one back to its key."""
        chosen = self.shape_label.get()
        for k, lbl in self.shape_labels.items():
            if lbl == chosen:
                self.shape_key.set(k)
                break
        self._on_shape_change()

    def _on_shape_change(self):
        self._sync_shape_label()
        self._show_size()
        self._sync_height()

    def _sync_height(self, *_):
        self._remember_size()
        w = self._num(self.size_w)
        a = SH.aspect(self.shape_key.get())
        if self.lock_aspect.get():
            if w > 0 and a > 0:
                self.size_h.set(f"{w / a:.4g}")
            self.h_entry.state(["disabled"])
        else:
            # Unlocking hands the box to the user, but it has to start from a
            # real number: an empty H reads as a zero height and blocks export.
            if self._num(self.size_h) <= 0 and w > 0 and a > 0:
                self.size_h.set(f"{w / a:.4g}")
            self.h_entry.state(["!disabled"])
        self.redraw()

    def _sync_width(self, *_):
        if not self.lock_aspect.get():
            self.redraw()

    def _on_units(self):
        # the stored size is in inches, so just re-display it in the new unit
        self._show_size()
        self._sync_height()

    def out_px(self):
        wi = self._entry_to_inches(self._num(self.size_w))
        hi = self._entry_to_inches(self._num(self.size_h))
        dpi = max(1, int(self._num(self.dpi, 300)))
        if self.lock_aspect.get() and wi > 0:
            hi = wi / SH.aspect(self.shape_key.get())
        return max(1, int(round(wi * dpi))), max(1, int(round(hi * dpi))), dpi, wi, hi

    # ---- setups (save your work in progress and come back to it)
    def _setup_data(self):
        self._remember_size()
        return {
            "version": 1,
            "shape": self.shape_key.get(), "units": self.units.get(),
            "sizes": {k: round(v, 5) for k, v in self.sizes.items()},
            "lock_aspect": self.lock_aspect.get(), "dpi": self.dpi.get(),
            "fmt": self.fmt.get(), "page": self.page.get(),
            "copies": self.copies.get(), "combine": self.combine.get(),
            "cut_line": self.cut_line.get(), "ruler": self.ruler.get(),
            "outdir": store_outdir(self.outdir.get()), "selected": self.index,
            "items": [{"path": str(it.path), "name": it.path.name,
                       "zoom": round(it.zoom, 6),
                       "pan": [round(it.pan[0], 6), round(it.pan[1], 6)],
                       "rotation": round(it.rotation, 3)} for it in self.items],
        }

    def _write_setup(self, path):
        Path(path).write_text(json.dumps(self._setup_data(), indent=2),
                              encoding="utf-8")

    def save_setup(self):
        p = filedialog.asksaveasfilename(
            title="Save setup", defaultextension=SETUP_EXT,
            initialdir=str(HERE), initialfile="setup" + SETUP_EXT,
            filetypes=[("Cropper setup", "*" + SETUP_EXT), ("All files", "*.*")])
        if not p:
            return
        try:
            self._write_setup(p)
            self.setup_lbl.config(text=f"setup: {Path(p).name}")
            messagebox.showinfo(
                "Setup saved",
                f"Saved {len(self.items)} image(s) with their positions to:\n{p}\n\n"
                "Open it later to carry on where you left off.")
        except Exception as exc:
            messagebox.showerror("Could not save setup", explain(exc, p))

    def open_setup(self):
        p = filedialog.askopenfilename(
            title="Open setup", initialdir=str(HERE),
            filetypes=[("Cropper setup", "*" + SETUP_EXT), ("All files", "*.*")])
        if p:
            self._load_setup(p, announce=True)

    def _load_setup(self, path, announce=False):
        path = Path(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            if announce:
                messagebox.showerror("Could not open setup", traceback.format_exc())
            return False

        for var, keyname in ((self.units, "units"), (self.dpi, "dpi"),
                             (self.fmt, "fmt"), (self.page, "page"),
                             (self.copies, "copies")):
            if data.get(keyname) is not None:
                var.set(data[keyname])
        # a setup saved elsewhere carries that machine's output path
        if data.get("outdir") is not None:
            self.outdir.set(str(resolve_outdir(data["outdir"])))
        for var, keyname in ((self.lock_aspect, "lock_aspect"),
                             (self.combine, "combine"),
                             (self.cut_line, "cut_line"), (self.ruler, "ruler")):
            if data.get(keyname) is not None:
                var.set(bool(data[keyname]))
        for k, v in (data.get("sizes") or {}).items():
            try:
                self.sizes[k] = float(v)
            except (TypeError, ValueError):
                pass
        if data.get("shape") in self.keys:
            self.shape_key.set(data["shape"])

        self.items, self.index = [], -1
        self.listbox.delete(0, "end")
        missing = []
        for rec in data.get("items") or []:
            src = Path(rec.get("path", ""))
            if not src.exists():
                # tolerate the whole folder having moved
                alt = path.parent / (rec.get("name") or src.name)
                if alt.exists():
                    src = alt
                else:
                    missing.append(src.name or str(src))
                    continue
            it = Item(src)
            it.zoom = float(rec.get("zoom", 1.0))
            pan = rec.get("pan") or [0.0, 0.0]
            it.pan = [float(pan[0]), float(pan[1])]
            it.rotation = float(rec.get("rotation", 0.0))
            self.items.append(it)
            self.listbox.insert("end", src.name)
        if self.items:
            self.index = min(max(0, int(data.get("selected", 0))),
                             len(self.items) - 1)
            self.listbox.selection_clear(0, "end")
            self.listbox.selection_set(self.index)
            self.rot_var.set(self.items[self.index].rotation)

        self.setup_lbl.config(text=f"setup: {path.name}")
        self._update_export_labels()
        self._sync_shape_label()
        self._show_size()
        self._sync_height()
        if missing and announce:
            messagebox.showwarning(
                "Some images are missing",
                "These files could not be found and were skipped:\n\n"
                + "\n".join(missing[:15]))
        if announce:
            messagebox.showinfo(
                "Setup opened",
                f"Restored {len(self.items)} image(s) with their positions.")
        return True

    # ---- items
    def add_images(self):
        paths = filedialog.askopenfilenames(
            title="Choose images", initialdir=str(HERE),
            filetypes=[("Images", "*.jpg *.jpeg *.png *.bmp *.gif *.tif *.tiff *.webp"),
                       ("All files", "*.*")])
        added = 0
        for p in paths:
            if Path(p).suffix.lower() in IMAGE_EXT:
                self.items.append(Item(p))
                self.listbox.insert("end", Path(p).name)
                added += 1
        if added and self.index < 0:
            self.index = 0
            self.listbox.selection_set(0)
        self.redraw()

    def remove_current(self):
        if self.index < 0:
            return
        self.items.pop(self.index)
        self.listbox.delete(self.index)
        self.index = min(self.index, len(self.items) - 1)
        if self.index >= 0:
            self.listbox.selection_set(self.index)
        self.redraw()

    def _on_select(self, _):
        sel = self.listbox.curselection()
        if sel:
            self.index = sel[0]
            self.rot_var.set(self.current.rotation)
            self.redraw()

    @property
    def current(self):
        return self.items[self.index] if 0 <= self.index < len(self.items) else None

    # ---- interaction
    def do_reset(self):
        if self.current:
            self.current.reset()
            self.rot_var.set(0.0)
            self.redraw()

    def do_fill(self):
        if self.current:
            self.current.zoom = 1.0
            self.current.pan = [0.0, 0.0]
            self.redraw()

    def do_fit(self):
        it = self.current
        if it:
            bw, bh = max(1, self._box[2]), max(1, self._box[3])
            it.zoom = it.fit_zoom(bw, bh, it.preview_src)
            it.pan = [0.0, 0.0]
            self.redraw()

    def rotate_by(self, deg):
        if self.current:
            self.current.rotation = (self.current.rotation + deg + 180) % 360 - 180
            self.rot_var.set(self.current.rotation)
            self.redraw()

    def _on_rot_slider(self, _):
        if self.current:
            self.current.rotation = round(self.rot_var.get(), 1)
            self.redraw()

    def nudge(self, dx, dy):
        if self.current:
            self.current.pan[0] += dx
            self.current.pan[1] += dy
            self.redraw()

    def _press(self, e):
        it = self.current
        if not it:
            return
        name = hit_handle(self._rect, e.x, e.y) if self._rect else None
        if name:
            x0, y0, x1, y1 = self._rect
            (fx, fy), (ax, ay) = HANDLES[name]
            self._drag = {
                "mode": "resize", "handle": name,
                "anchor": (x0 + ax * (x1 - x0), y0 + ay * (y1 - y0)),
                "start": (x0 + fx * (x1 - x0), y0 + fy * (y1 - y0)),
                "centre0": ((x0 + x1) / 2.0, (y0 + y1) / 2.0),
                "zoom0": it.zoom,
            }
        else:
            self._drag = {"mode": "pan", "handle": None,
                          "from": (e.x, e.y), "pan0": list(it.pan)}
        self.redraw()

    def _motion(self, e):
        it = self.current
        if not (it and self._drag):
            return
        bx, by, bw, bh = self._box
        if self._drag["mode"] == "pan":
            fx, fy = self._drag["from"]
            p0 = self._drag["pan0"]
            it.pan = [p0[0] + (e.x - fx) / bh, p0[1] + (e.y - fy) / bh]
        else:
            d = self._drag
            f = resize_factor(d["handle"], d["anchor"], d["start"], (e.x, e.y))
            zoom = d["zoom0"] * f
            zoom = max(MIN_ZOOM, min(MAX_ZOOM, zoom))
            f = zoom / d["zoom0"]                 # re-derive after clamping
            ax, ay = d["anchor"]
            c0x, c0y = d["centre0"]
            # scaling about a fixed anchor moves the centre with it
            cx, cy = ax + (c0x - ax) * f, ay + (c0y - ay) * f
            it.zoom = zoom
            it.pan = [(cx - bx - bw / 2.0) / bh, (cy - by - bh / 2.0) / bh]
        self.redraw()

    def _hover(self, e):
        """Show a resize cursor over a handle, a move cursor elsewhere."""
        if self._drag or not self.current:
            return
        name = hit_handle(self._rect, e.x, e.y) if self._rect else None
        self.canvas.config(cursor=HANDLE_CURSOR.get(name, "fleur" if name is None
                                                    else ""))

    def _release(self, _):
        self._drag = None
        self.redraw()

    def _wheel(self, e):
        it = self.current
        if not it:
            return
        bx, by, bw, bh = self._box
        old = it.zoom
        new = max(MIN_ZOOM, min(MAX_ZOOM, old * (1.1 ** (e.delta / 120.0))))
        if new == old:
            return
        # keep the point under the cursor put
        qx, qy = e.x - bx, e.y - by
        cx = bw / 2.0 + it.pan[0] * bh
        cy = bh / 2.0 + it.pan[1] * bh
        f = new / old
        it.pan = [(qx - (qx - cx) * f - bw / 2.0) / bh,
                  (qy - (qy - cy) * f - bh / 2.0) / bh]
        it.zoom = new
        self.redraw()

    # ---- drawing
    def redraw(self):
        self.canvas.delete("all")
        cw = max(1, self.canvas.winfo_width())
        ch = max(1, self.canvas.winfo_height())
        key = self.shape_key.get()
        a = SH.aspect(key)
        pad = 46          # room around the box so edge handles stay reachable
        bh = ch - 2 * pad
        bw = int(round(bh * a))
        if bw > cw - 2 * pad:
            bw = cw - 2 * pad
            bh = int(round(bw / a))
        bw, bh = max(40, bw), max(40, bh)
        bx, by = (cw - bw) // 2, (ch - bh) // 2
        self._box = (bx, by, bw, bh)

        it = self.current
        if it is None:
            self.canvas.create_text(cw // 2, ch // 2, fill="#888",
                                    text="Add images, then drag to position "
                                         "and scroll to zoom.")
            self._update_status()
            return

        try:
            full, rect = it.draw((cw, ch), (bx, by), (bw, bh), it.preview_src,
                                 resample=Image.BILINEAR)
        except Exception as exc:
            self.canvas.create_text(cw // 2, ch // 2, fill="#c66",
                                    text=f"Could not read image:\n{exc}")
            return

        # Whole photo faint, the part inside the shape at full brightness, so
        # you can see what you are dragging and what will survive the crop.
        board = Image.new("RGB", (cw, ch), (43, 43, 43))
        board.paste(full, (0, 0), full)
        faint = board.point(lambda v: int(v * 0.32))
        inside = Image.new("L", (cw, ch), 0)
        inside.paste(SH.shape_mask(key, bw, bh), (bx, by))
        shown = Image.composite(board, faint, inside)
        self._photo = ImageTk.PhotoImage(shown)
        self.canvas.create_image(0, 0, image=self._photo, anchor="nw")

        # the shape edge, then the image rectangle with its grab handles
        pts = SH.outline_points(key, bw, bh, offset=(bx, by))
        self.canvas.create_line(*[c for p in pts + [pts[0]] for c in p],
                                fill="#ffd250", width=2)
        self._rect = rect
        x0, y0, x1, y1 = [int(round(v)) for v in rect]
        self.canvas.create_rectangle(x0, y0, x1, y1, outline="#7fb2ff", dash=(4, 3))
        for name, (hx, hy) in handle_points(rect).items():
            active = (self._drag or {}).get("handle") == name
            self.canvas.create_rectangle(
                hx - 4, hy - 4, hx + 4, hy + 4,
                fill="#ffd250" if active else "#7fb2ff", outline="#1b1b1b")
        self.rot_lbl.config(text=f"{it.rotation:.1f} deg")
        self._update_status()

    def _update_status(self):
        if not hasattr(self, "status"):
            return                      # still building the window
        wpx, hpx, dpi, wi, hi = self.out_px()
        u = self.units.get()
        wd = self._from_inches(wi)
        hd = self._from_inches(hi)
        self.px_lbl.config(text=f"{wpx} x {hpx} px at {dpi} DPI")
        it = self.current
        extra = ""
        if it:
            src = it.full.size
            ratio = it.source_px_per_output_px(wpx, hpx, it.full)
            quality = ("sharp" if ratio >= 0.9 else
                       "slightly soft" if ratio >= 0.6 else
                       f"TOO LOW for {dpi} DPI - zoom out, lower DPI, or print smaller")
            extra = (f"   |   {it.path.name}  {src[0]}x{src[1]}px  "
                     f"zoom {it.zoom:.2f}x  -  detail {ratio:.2f}x ({quality})")
        self.status.config(text=f"Finished size {wd:.4g} x {hd:.4g} {u}  "
                                f"= {wpx} x {hpx} px{extra}")

    # ---- export
    def _pick_outdir(self):
        p = filedialog.askdirectory(initialdir=self.outdir.get() or str(HERE))
        if p:
            self.outdir.set(p)

    def export_current(self):
        if not self.current:
            messagebox.showinfo("Nothing to export", "Add an image first.")
            return
        self._export([self.current])

    def export_all(self):
        if not self.items:
            messagebox.showinfo("Nothing to export", "Add some images first.")
            return
        self._export(list(self.items))

    def _export(self, items):
        try:
            wpx, hpx, dpi, wi, hi = self.out_px()
            if wi <= 0 or hi <= 0:
                # Name the box that is actually wrong - saying "width" when the
                # height is the empty one sends you hunting in the wrong place.
                bad = "width" if wi <= 0 else "height"
                messagebox.showerror(
                    "Check the size",
                    f"Enter a finished {bad} greater than zero.")
                return
            asked = self.outdir.get()
            outdir = resolve_outdir(asked, probe=True)
            redirected = ""
            if outdir != Path(asked).expanduser():
                self.outdir.set(str(outdir))
                redirected = (f"\n\nNote: '{asked}' could not be written to, so "
                              "this went to the project's own output folder.")
            key = self.shape_key.get()
            copies = max(1, int(self._num(self.copies, 1)))
            size_tag = f"{wi:.3g}x{hi:.3g}in"

            crops = [(it, masked(it, wpx, hpx, key)) for it in items]
            if self.combine.get():
                # every image on one sheet, each repeated `copies` times and
                # kept together so they are easy to find when cutting
                jobs = [(f"sheet_{key}_{len(items)}imgs_{size_tag}",
                         [c for _, c in crops for _ in range(copies)])]
            else:
                jobs = [(f"{it.path.stem}_{key}_{size_tag}", [c] * copies)
                        for it, c in crops]

            written = []
            for stem, sheet_crops in jobs:
                written += self._write_job(stem, sheet_crops, key, dpi, outdir)

            self._save_settings()
            msg = "\n".join(p.name for p in written[:12])
            more = "" if len(written) <= 12 else f"\n...and {len(written) - 12} more"
            note = ""
            if self.combine.get():
                per_page = len(self._spots(dpi, crops[0][1].size) or [1])
                note = (f"\n{len(items)} image(s) x {copies} = "
                        f"{len(items) * copies} shapes, {per_page} per page.")
            messagebox.showinfo(
                "Exported",
                f"Wrote {len(written)} file(s) to:\n{outdir}\n\n{msg}{more}{note}"
                f"{redirected}\n\n"
                "When printing, choose 100% / Actual Size - not Fit to Page.")
        except Exception as exc:
            messagebox.showerror("Export failed", explain(exc, self.outdir.get()))

    # ---- layout helpers
    def _page_px(self, dpi):
        """Page size in pixels, or None when trimming to the shape itself."""
        page = self.page.get()
        if page.startswith("Trim"):
            return None
        pw_in, ph_in = PAGES_IN[page]
        return int(round(pw_in * dpi)), int(round(ph_in * dpi))

    def _spots(self, dpi, cell):
        page_px = self._page_px(dpi)
        if page_px is None:
            return None
        return grid_layout(page_px, cell, int(round(0.12 * dpi)),
                           int(round(0.35 * dpi)),
                           int(round(0.45 * dpi)) if self.ruler.get() else 0)[0]

    def _write_job(self, stem, crops, key, dpi, outdir):
        """
        One export job: a stem plus the shapes that belong in it.

        A sheet is used whenever there is a real page to tile onto, or whenever
        more than one shape has to share a file. Otherwise the shape is written
        on its own at exact size.
        """
        fmt = self.fmt.get()
        as_png = fmt.startswith("PNG")
        page_px = self._page_px(dpi)
        sheet_wanted = page_px is not None and (not as_png or self.combine.get())

        if not sheet_wanted:
            return self._write_bare(stem, crops, key, dpi, outdir, as_png, fmt)

        if as_png and page_px is None:                # can't tile without a page
            page_px = (int(round(8.5 * dpi)), int(round(11 * dpi)))
        clear = as_png and "transparent" in fmt
        pages, fits = self._tile(crops, page_px, key, dpi, transparent=clear)
        if not fits:
            messagebox.showwarning(
                "Bigger than the page",
                f"At {self._num(self.size_w):g} {self.units.get()} the shape does "
                f"not fit inside the printable area of {self.page.get()}.\n\n"
                "It will be written anyway but the edges will be clipped. Use a "
                "smaller finished size, or 'Trim to shape (no margin)'.")
        if as_png:
            out = []
            for i, sheet in enumerate(pages, 1):
                suffix = "" if len(pages) == 1 else f"_p{i}"
                p = outdir / f"{stem}_sheet{suffix}.png"
                sheet.save(p, dpi=(dpi, dpi))
                out.append(p)
            return out
        p = outdir / f"{stem}.pdf"
        pages[0].save(p, "PDF", resolution=float(dpi),
                      save_all=len(pages) > 1, append_images=pages[1:])
        return [p]

    def _write_bare(self, stem, crops, key, dpi, outdir, as_png, fmt):
        """The shape on its own, at exact size - no page around it."""
        line_w = max(1, dpi // 300)
        if as_png:
            crop = crops[0]                    # a lone PNG holds a single shape
            if "white" in fmt:
                out = Image.new("RGB", crop.size, (255, 255, 255))
                out.paste(crop, (0, 0), crop)
                out = out.convert("RGBA")
            else:
                out = crop.copy()
            if self.cut_line.get():
                draw_cut_line(out, key, width=line_w)
            p = outdir / f"{stem}.png"
            out.save(p, dpi=(dpi, dpi))
            return [p]
        pages = []
        for crop in crops:                     # trimmed PDF: one shape per page
            sheet = Image.new("RGB", crop.size, (255, 255, 255))
            sheet.paste(crop, (0, 0), crop)
            if self.cut_line.get():
                draw_cut_line(sheet, key, width=line_w)
            pages.append(sheet)
        p = outdir / f"{stem}.pdf"
        pages[0].save(p, "PDF", resolution=float(dpi),
                      save_all=len(pages) > 1, append_images=pages[1:])
        return [p]

    def _tile(self, crops, page_px, key, dpi, transparent=False):
        """Lay the shapes out across as many pages as they need."""
        margin = int(round(0.35 * dpi))
        gap = int(round(0.12 * dpi))
        reserve = int(round(0.45 * dpi)) if self.ruler.get() else 0
        cell = crops[0].size
        spots, fits = grid_layout(page_px, cell, gap, margin, reserve)
        line_w = max(1, dpi // 300)
        mode, bg = (("RGBA", (0, 0, 0, 0)) if transparent
                    else ("RGB", (255, 255, 255)))
        pages, i = [], 0
        while i < len(crops):
            sheet = Image.new(mode, page_px, bg)
            for spot in spots:
                if i >= len(crops):
                    break
                crop = crops[i]
                sheet.paste(crop, spot, crop)
                if self.cut_line.get():
                    draw_cut_line(sheet, key, offset=spot, size=crop.size,
                                  width=line_w)
                i += 1
            if self.ruler.get():
                draw_ruler_check(sheet, dpi, (margin, page_px[1] - margin // 2))
            pages.append(sheet)
            if not spots:
                break
        return pages, fits


def main():
    try:
        SH.load_shapes()
    except SystemExit as exc:
        messagebox.showerror("Missing shapes.json", str(exc))
        raise
    App().mainloop()


if __name__ == "__main__":
    main()
