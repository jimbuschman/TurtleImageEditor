"""Load shapes.json and rasterise a shape mask at any pixel size."""

import json
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).parent
_CACHE = {}


def load_shapes():
    if "shapes" not in _CACHE:
        path = HERE / "shapes.json"
        if not path.exists():
            raise SystemExit("shapes.json is missing - run: python extract_templates.py")
        _CACHE["shapes"] = json.loads(path.read_text(encoding="utf-8"))
    return _CACHE["shapes"]


def shape_mask(key, width, height, supersample=4):
    """
    An 'L' mode mask: 255 inside the shape, 0 outside, antialiased edges.

    Drawn at `supersample`x then downscaled, which is what gives a smooth edge
    at any output size rather than the 378px stair-steps of the template.
    """
    width, height = max(1, int(round(width))), max(1, int(round(height)))
    cache_key = (key, width, height, supersample)
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    spec = load_shapes()[key]
    s = supersample
    big = Image.new("L", (width * s, height * s), 0)
    draw = ImageDraw.Draw(big)

    if spec["type"] == "ellipse":
        draw.ellipse([0, 0, width * s - 1, height * s - 1], fill=255)
    else:
        pts = [(x * (width * s - 1), y * (height * s - 1)) for x, y in spec["points"]]
        draw.polygon(pts, fill=255)

    mask = big.resize((width, height), Image.LANCZOS) if s > 1 else big
    if len(_CACHE) > 40:  # keep the interactive preview from growing unbounded
        for k in [k for k in _CACHE if k != "shapes"][:20]:
            _CACHE.pop(k, None)
    _CACHE[cache_key] = mask
    return mask


def aspect(key):
    """width / height of the shape."""
    return load_shapes()[key]["aspect"]


def outline_points(key, width, height, offset=(0, 0), samples=720):
    """Closed outline of the shape, for on-screen guides and printed cut lines."""
    import math

    spec = load_shapes()[key]
    ox, oy = offset
    w, h = width - 1, height - 1
    if spec["type"] == "ellipse":
        cx, cy, rx, ry = w / 2.0, h / 2.0, w / 2.0, h / 2.0
        pts = [(cx + rx * math.cos(2 * math.pi * i / samples),
                cy + ry * math.sin(2 * math.pi * i / samples)) for i in range(samples)]
    else:
        pts = [(x * w, y * h) for x, y in spec["points"]]
    return [(x + ox, y + oy) for x, y in pts]


def labels():
    return {k: v.get("label", k) for k, v in load_shapes().items()}


def default_width_in(key):
    """Finished width this template was calibrated to, in inches."""
    return load_shapes()[key].get("default_size_in", [3.0, 0])[0]
