"""
Read the Paint.NET templates and turn their shapes into resolution-independent
definitions in shapes.json.

Run this again any time you redraw a template in Paint.NET.

How it works: a .pdn file is "PDN3" + a 3-byte little-endian header length + an
XML header (canvas size) + .NET-serialized layer data, where each layer's BGRA
pixels are stored as gzip-compressed 256 KB chunks. We find the gzip streams,
reassemble each layer, and read the shape out of the alpha channel.

Template convention (both templates follow it):
  layer 0 = sample photo
  layer 1 = white matte with the shape punched out  <-- the shape lives here
  layer 2 = guide outline / shape fill
"""

import json
import re
import struct
import zlib
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).parent
OUT_JSON = HERE / "shapes.json"

# Which layer holds the authoritative shape, and whether the shape is the
# transparent hole (True) or the opaque region (False).
#
# print_percent is the scale Jim used in Paint.NET's print dialog to get the
# right physical size. Paint.NET records no DPI in either template, so it falls
# back to its 96 DPI default: template_px / 96 * print_percent = finished
# inches. That becomes the template's remembered size so the percentage is
# never needed again. If a ruler says it is off, correct the number here (or
# just type the right size in the app - it remembers per template).
ASSUMED_TEMPLATE_DPI = 96.0

TEMPLATES = {
    "head": {"file": "headtemp.pdn", "layer": 1, "shape_is_hole": True,
             "label": "Head (oval)", "symmetric": True, "print_percent": 0.15},
    "shield": {"file": "shieldtemp.pdn", "layer": 2, "shape_is_hole": False,
               "label": "Shield", "symmetric": True, "print_percent": 0.10},
}


# Shapes defined by their measurements rather than traced from a .pdn. A circle
# needs no template - it is exact at any size - so it is described here and the
# finished size is the real-world one, not something worked back from a
# print percentage.
MM_PER_IN = 25.4

SYNTHETIC = {
    "circle12": {"label": "Circle 12mm", "type": "ellipse",
                 "size_mm": [12.0, 12.0]},
}


def default_size_in(shape_w_px, shape_h_px, print_percent):
    """Finished size implied by the old print-at-a-percentage workflow."""
    k = print_percent / ASSUMED_TEMPLATE_DPI
    # 6 dp: 4 would round 12 mm to 11.999 mm
    return [round(shape_w_px * k, 6), round(shape_h_px * k, 6)]


def read_pdn_layers(path):
    """Return (width, height, [RGBA numpy array per layer])."""
    data = path.read_bytes()
    if data[:4] != b"PDN3":
        raise ValueError(f"{path.name} is not a Paint.NET PDN3 file")
    hdr_len = struct.unpack("<I", data[4:7] + b"\x00")[0]
    xml = data[7:7 + hdr_len].decode("utf-8", "replace")
    w = int(re.search(r'width="(\d+)"', xml).group(1))
    h = int(re.search(r'height="(\d+)"', xml).group(1))
    body = data[7 + hdr_len:]

    need = w * h * 4
    layers, buf = [], b""
    for m in re.finditer(rb"\x1f\x8b\x08", body):
        dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            raw = dec.decompress(body[m.start():])
        except zlib.error:
            continue  # a false-positive gzip magic inside compressed bytes
        buf += raw
        if len(buf) == need:
            # stored BGRA, little-endian
            arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)
            layers.append(arr[:, :, [2, 1, 0, 3]].copy())  # -> RGBA
            buf = b""
        elif len(buf) > need:
            buf = b""
    return w, h, layers


def alpha_to_mask(alpha, shape_is_hole):
    """Coverage in 0..1 where 1 = inside the shape."""
    cov = alpha.astype(np.float64) / 255.0
    return (1.0 - cov) if shape_is_hole else cov


def is_ellipse(mask, tol=0.005):
    """If the mask is an ellipse, return (cx, cy, rx, ry); else None."""
    solid = mask > 0.5
    if not solid.any():
        return None
    ys, xs = np.nonzero(solid)
    x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    rx, ry = (x1 - x0 + 1) / 2.0, (y1 - y0 + 1) / 2.0
    yy, xx = np.mgrid[0:mask.shape[0], 0:mask.shape[1]]
    model = (((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2) <= 1.0
    bad = int((model ^ solid).sum())
    if bad / float(model.sum()) <= tol:
        return cx, cy, rx, ry, bad
    return None


def _row_profile(mask):
    """Sub-pixel (row, width, centre) for every row the shape touches."""
    h, w = mask.shape
    xs = np.arange(w, dtype=np.float64)
    ys, widths, centres = [], [], []
    for y in range(h):
        total = mask[y].sum()
        if total > 0.5:
            ys.append(float(y))
            widths.append(float(total))
            centres.append(float((mask[y] * xs).sum() / total))
    return np.asarray(ys), np.asarray(widths), np.asarray(centres)


def _quad_bezier(p0, p1, p2, n=800):
    t = np.linspace(0.0, 1.0, n)[:, None]
    return (1 - t) ** 2 * p0 + 2 * (1 - t) * t * p1 + t ** 2 * p2


def fit_shield(mask, tol_px=6.0):
    """
    Fit a heraldic shield: flat top, straight vertical sides down to a
    shoulder, then a quadratic Bezier tapering to a point at bottom centre.

    Hand-drawn template edges carry a couple of pixels of wobble, which turns
    into very visible waviness once scaled up to print size. Fitting the
    intended curve removes it. Returns (points, info) or None if the mask is
    not this kind of shape.
    """
    ys, widths, _ = _row_profile(mask)
    if len(ys) < 20:
        return None
    wmax = widths.max()
    solid = ys[widths >= 0.5 * wmax]          # ignore antialiased sliver rows
    if len(solid) < 10:
        return None
    y0, y1 = solid[0], ys[-1] + 1.0           # the tip still sets the bottom
    tn = (ys - y0) / (y1 - y0)
    hn = (widths / 2.0) / wmax
    keep = (tn >= 0.0) & (tn <= 1.0)
    tn, hn = tn[keep], hn[keep]

    at_full = np.where(hn >= 0.4995)[0]
    if len(at_full) == 0:
        return None
    shoulder = float(tn[at_full[-1]])
    if not 0.15 < shoulder < 0.9:
        return None

    taper = tn >= shoulder
    ty, tx = tn[taper], 0.5 - hn[taper]       # measured left edge
    p0 = np.array([0.0, shoulder])
    p2 = np.array([0.5, 1.0])

    def rmse(p1x, p1y):
        curve = _quad_bezier(p0, np.array([p1x, p1y]), p2)
        if curve[:, 1].max() < 0.999:
            return np.inf
        return float(np.sqrt(np.mean((np.interp(ty, curve[:, 1], curve[:, 0]) - tx) ** 2)))

    best, span = (np.inf, 0.0, shoulder), 0.30
    lo_x, hi_x, lo_y, hi_y = 0.0, 0.30, shoulder, 1.02
    for _ in range(3):                        # coarse-to-fine search
        for p1x in np.linspace(lo_x, hi_x, 61):
            for p1y in np.linspace(lo_y, hi_y, 61):
                e = rmse(p1x, p1y)
                if e < best[0]:
                    best = (e, p1x, p1y)
        span /= 8.0
        lo_x, hi_x = max(0.0, best[1] - span), best[1] + span
        lo_y, hi_y = max(shoulder, best[2] - span), min(1.05, best[2] + span)

    err, p1x, p1y = best
    if err * wmax > tol_px:
        return None

    curve = _quad_bezier(p0, np.array([p1x, p1y]), p2, n=240)
    left = [(0.0, 0.0)] + [(float(x), float(y)) for x, y in curve]
    right = [(1.0 - x, y) for x, y in reversed(left)]
    info = {"shoulder": round(shoulder, 5),
            "control": [round(p1x, 5), round(p1y, 5)],
            "fit_rmse_px": round(err * wmax, 2),
            "aspect": wmax / (y1 - y0),
            "bbox_px": [round(v, 2) for v in (0.0, y0, wmax, y1)]}
    return left + right, info


def trace_profile(mask, symmetric=True, smooth=5):
    """
    Turn a row-convex mask into a closed polygon in normalized 0..1 coords.

    Each row's horizontal run is measured with sub-pixel precision from the
    coverage values (width = sum of coverage, centre = coverage centroid),
    which is what keeps the traced outline smooth instead of stair-stepped.
    """
    h, w = mask.shape
    rows = np.where(mask.sum(axis=1) > 0.5)[0]
    if len(rows) == 0:
        raise ValueError("empty mask")
    y_top, y_bot = int(rows[0]), int(rows[-1])

    xs = np.arange(w, dtype=np.float64)
    widths, centres, keep = [], [], []
    for y in range(y_top, y_bot + 1):
        row = mask[y]
        total = row.sum()
        if total <= 0.5:
            continue
        widths.append(total)
        centres.append(float((row * xs).sum() / total))
        keep.append(y)
    widths = np.asarray(widths)
    centres = np.asarray(centres)
    keep = np.asarray(keep, dtype=np.float64)

    if symmetric:
        # An emblem is meant to be symmetric; lock every row to one axis so
        # small hand-drawing wobble does not survive into the print.
        axis = float(np.average(centres, weights=widths))
        centres = np.full_like(centres, axis)

    if smooth > 1 and len(widths) > smooth:
        k = np.ones(smooth) / smooth
        pad = smooth // 2
        widths = np.convolve(np.pad(widths, pad, mode="edge"), k, mode="valid")[:len(keep)]

    left = np.clip(centres - widths / 2.0, 0, w)
    right = np.clip(centres + widths / 2.0, 0, w)

    # Normalise against the shape's own bounding box.
    bx0, bx1 = float(min(left.min(), right.min())), float(max(left.max(), right.max()))
    by0, by1 = float(keep[0]), float(keep[-1] + 1)
    sx, sy = (bx1 - bx0), (by1 - by0)

    def nx(v):
        return float(np.clip((v - bx0) / sx, 0.0, 1.0))

    def ny(v):
        return float(np.clip((v - by0) / sy, 0.0, 1.0))

    pts = [(nx(l), ny(y)) for l, y in zip(left, keep)]
    pts += [(nx(r), ny(y)) for r, y in reversed(list(zip(right, keep)))]
    return pts, (bx0, by0, bx1, by1)


def main():
    shapes = {}
    for key, cfg in TEMPLATES.items():
        path = HERE / cfg["file"]
        w, h, layers = read_pdn_layers(path)
        if len(layers) <= cfg["layer"]:
            raise SystemExit(f"{path.name}: expected layer {cfg['layer']}, found {len(layers)}")
        mask = alpha_to_mask(layers[cfg["layer"]][:, :, 3], cfg["shape_is_hole"])

        ell = is_ellipse(mask)
        if ell:
            cx, cy, rx, ry, bad = ell
            shapes[key] = {
                "label": cfg["label"],
                "type": "ellipse",
                "aspect": (2 * rx) / (2 * ry),
                "default_size_in": default_size_in(2 * rx, 2 * ry,
                                                   cfg["print_percent"]),
                "source": {"canvas": [w, h], "size_px": [2 * rx, 2 * ry],
                           "centre_px": [cx, cy],
                           "print_percent": cfg["print_percent"],
                           "assumed_template_dpi": ASSUMED_TEMPLATE_DPI},
            }
            print(f"{key:8s} ellipse  {2*rx:.0f}x{2*ry:.0f}px  "
                  f"aspect {(2*rx)/(2*ry):.4f}  (fit off by {bad} px)  "
                  f"-> {shapes[key]['default_size_in'][0]:.3f} x "
                  f"{shapes[key]['default_size_in'][1]:.3f} in "
                  f"at {cfg['print_percent']:.0%}")
            continue

        fitted = fit_shield(mask)
        if fitted:
            pts, info = fitted
            bx0, by0, bx1, by1 = info["bbox_px"]
            shapes[key] = {
                "label": cfg["label"],
                "type": "polygon",
                "aspect": info["aspect"],
                "default_size_in": default_size_in(bx1 - bx0, by1 - by0,
                                                   cfg["print_percent"]),
                "points": [[round(x, 5), round(y, 5)] for x, y in pts],
                "source": {"canvas": [w, h], "bbox_px": info["bbox_px"],
                           "print_percent": cfg["print_percent"],
                           "assumed_template_dpi": ASSUMED_TEMPLATE_DPI,
                           "shield_fit": {"shoulder": info["shoulder"],
                                          "control": info["control"],
                                          "rmse_px": info["fit_rmse_px"]}},
            }
            print(f"{key:8s} shield   aspect {info['aspect']:.4f}  "
                  f"shoulder {info['shoulder']:.3f}  "
                  f"fit within {info['fit_rmse_px']:.2f}px  {len(pts)} points  "
                  f"-> {shapes[key]['default_size_in'][0]:.3f} x "
                  f"{shapes[key]['default_size_in'][1]:.3f} in "
                  f"at {cfg['print_percent']:.0%}")
        else:
            pts, bbox = trace_profile(mask, symmetric=cfg["symmetric"])
            bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
            shapes[key] = {
                "label": cfg["label"],
                "type": "polygon",
                "aspect": bw / bh,
                "points": [[round(x, 5), round(y, 5)] for x, y in pts],
                "source": {"canvas": [w, h], "bbox_px": [round(v, 2) for v in bbox]},
            }
            print(f"{key:8s} polygon  {bw:.0f}x{bh:.0f}px  aspect {bw/bh:.4f}  "
                  f"{len(pts)} points")

    for key, cfg in SYNTHETIC.items():
        wmm, hmm = cfg["size_mm"]
        shapes[key] = {
            "label": cfg["label"],
            "type": cfg["type"],
            "aspect": wmm / hmm,
            "default_size_in": [round(wmm / MM_PER_IN, 6), round(hmm / MM_PER_IN, 6)],
            "source": {"defined_mm": [wmm, hmm]},
        }
        print(f"{key:8s} {cfg['type']:8s} aspect {wmm / hmm:.4f}  "
              f"-> {wmm:g} x {hmm:g} mm "
              f"({wmm / MM_PER_IN:.4f} x {hmm / MM_PER_IN:.4f} in) exact")

    OUT_JSON.write_text(json.dumps(shapes, indent=2), encoding="utf-8")
    print(f"\nwrote {OUT_JSON}")


if __name__ == "__main__":
    main()
