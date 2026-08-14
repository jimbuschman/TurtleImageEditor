# Template Cropper

Aligns images to the shape templates and exports them at an **exact physical
size**, so you print at 100% instead of hunting for the right percentage.

## Run it

```
python cropper.py
```

Needs Python with Pillow and numpy — both already installed on this machine.

## Why the percentage trick was needed

An image file has no physical size, only a pixel count. Printing it means some
program guesses how many pixels go in an inch, and you were correcting that
guess by hand. This tool removes the guess two ways:

- **PDF export** puts the shape on a real page at real inches. Inches are part
  of the PDF format, so nothing has to be inferred.
- **PNG export** tags the file with the DPI you chose.

Either way, **print at 100% / "Actual Size" — never "Fit to Page"**, which
re-scales the page and undoes the whole thing.

Leave *Add 1-inch ruler check* on for the first print. It draws a labelled
1-inch bar in the bottom margin. Measure it with a ruler: if it is exactly one
inch, the whole page printed at true size, and every shape on it is correct.

PDF is the more reliable of the two. PNG stores resolution in pixels-per-metre,
so 300 DPI comes back as 299.9994 — harmless, but some editors round it oddly.

## The sizes are already calibrated

You don't need to enter a size. Each template remembers its own finished size,
worked back from the percentages you were printing at:

| Template | Shape | Where the size comes from | Finished size |
|---|---|---|---|
| head | 183 × 284 px oval | old 15% print scale | **0.286 × 0.444 in** |
| shield | 378 × 505 px shield | old 10% print scale | **0.393 × 0.527 in** |
| circle12 | circle | measured directly | **12 × 12 mm** (0.4724 in) |

For the two traced templates, Paint.NET stores no DPI in either `.pdn`, so it
used its 96 DPI default: `template_px / 96 × percent = finished inches`.

`circle12` is not traced from anything — a circle is exact at any size, so it is
defined by its measurement in `SYNTHETIC` in `extract_templates.py`. Its size is
a real 12 mm, not a number worked back from a print percentage, so it needs no
ruler check. To add another fixed-size shape (a 20 mm circle, say), copy that
entry and re-run the extractor.

**Check this once with a ruler.** Print one and measure it. If it is off, just
type the correct size in the app — it remembers per template, so you only do it
once. (The calibration itself lives in `print_percent` in
`extract_templates.py` if you'd rather fix it at the source.)

Because each template keeps its own size, switching between head and shield
does not carry the wrong measurement across.

## Resizing does not cost you quality here

The finished pieces are small and the source photos are large, so every export
is a *downscale* — which loses nothing visible. Upscaling is what hurts, and
that never happens. Measured headroom with your samples:

| Template | 300 DPI | 600 DPI | 1200 DPI |
|---|---|---|---|
| head | 86×133 px, 9.9× | 172×266 px, 5.0× | 343×532 px, 2.5× |
| shield | 118×158 px, 7.3× | 236×316 px, 3.7× | 472×632 px, 1.8× |

Even at 1200 DPI there is nearly twice the detail needed. **600 DPI is the
default** and is the sweet spot for pieces this small. There is no reason to go
below it, and 1200 is available if your printer makes use of it.

Two things protect quality beyond that: the image is resampled **once**,
straight from the full-resolution original to the final pixel size with a
Lanczos filter (no intermediate resize, and rotation happens before the
downscale, not after); and the shape edge is drawn at 4× and reduced, so the
outline stays smooth at any size instead of inheriting the template's 378 px
stair-steps.

## Using it

1. **Add images…** — load one or many.
2. Pick the **Template**: `head` (oval) or `shield`. The finished size fills in
   automatically.
3. Position and size the image — see below.
4. The **Finished size** is already set per template — change it only if the
   ruler says so. With *Keep template proportions* ticked, the height follows
   the template and the shape is never distorted. Switching in/mm re-displays
   the same physical size, it does not change it.
5. **Export current** or **Export all images**.

### Sizing the image with the handles

The **blue dashed rectangle** is the image; the **yellow outline** is the shape
that will be cut out. Eight square handles sit on the rectangle — four corners
and four edge midpoints.

- **Drag a handle** to grow or shrink the image. It always scales
  proportionally — one factor for both axes — so the picture can never come out
  stretched or squashed.
- The **opposite side stays put**: pull the right edge and the left edge does
  not move; pull a corner and the far corner is the pivot. Dragging an edge
  grows the other axis symmetrically, so the image stays centred on that line.
- **Drag anywhere else** to slide the image around.
- **Scroll** to zoom about the cursor — quicker than the handles for framing.
- The cursor changes shape over a handle, and the handle you are dragging turns
  yellow.

The whole photo stays visible while you work: the part inside the shape is at
full brightness and everything outside is dimmed, so you can see what you are
losing before you commit. Handles remain reachable even when the image is much
larger than the shape.

Buttons and keys: `Fill` / `f` fills the shape (edges get cropped), `Fit` / `c`
fits the whole image inside it, `Reset` / `r` starts over, `Rotate L/R` and the
slider (or `[` `]`) straighten a crooked photo, arrow keys nudge.

Watch `detail` in the status bar as you enlarge — it tells you when you have
pushed past what the photo can print sharply. Scale is capped at 30× in and 20×
out.

### Watch the status bar

It reads e.g. `detail 0.94x (sharp)`. That is how many original pixels you have
per printed pixel:

- **0.9x and up** — sharp.
- **0.6–0.9x** — slightly soft, usually fine.
- **below 0.6x** — too low; zoom out, print smaller, or drop the DPI.

This is the honest limit of the source photo. `DSC07874-300x248.jpg` is only
300×248 px, so it cannot fill a 3-inch shield at 300 DPI no matter what.

### Getting several onto one sheet

Two controls work together:

- **Copies of each** — how many times every image is repeated.
- **Put every image on one sheet** — gangs *all* the loaded images into a single
  file instead of writing one file per image. The button changes to *Export all
  onto one sheet* so you can see which you are about to get.

Together they multiply: 5 images × 4 copies = 20 shapes, laid out in order and
grouped so each image's copies sit together, which makes them easy to find when
cutting. They flow onto extra pages automatically if they don't all fit, and the
confirmation tells you how many shapes and how many per page.

At these finished sizes a Letter page holds *hundreds* — 323 heads or 225
shields — so one sheet usually covers a whole batch.

All shapes on a sheet use the currently selected template. To mix templates,
export one sheet per template.

With *Trim to shape (no margin)* there is no page to tile onto, so combining
gives you a multi-page PDF with one shape per page instead.

Choose *Trim to shape (no margin)* only if you want a page exactly the size of
the shape. A Letter or A4 page is safer — printers cannot print to the paper
edge and may silently shrink a borderless page to fit, which is the exact
problem you were working around.

## Saving your progress

Positioning a batch is real work, so it can be saved and resumed.

- **Save setup…** writes a `.cropset` file holding every loaded image plus its
  exact zoom, position and rotation, along with the template, sizes, DPI and
  output options.
- **Open setup…** restores all of it and you carry on where you stopped.
- Keep as many `.cropset` files as you like — one per batch or per job.

You do not have to remember to save. On exit the current state is written to
`autosave.cropset` and reloaded automatically next launch, so closing the window
mid-batch loses nothing. (Delete that file to start empty.)

A setup stores *paths* to your images, not the images themselves, so keep the
photos where they are. If a whole folder moves, put the `.cropset` beside the
images and it will find them by name. Anything genuinely missing is reported and
skipped rather than failing the load, so one deleted photo does not cost you the
rest of the batch.

## If you redraw a template

`shapes.json` holds the shapes. Regenerate it after editing a `.pdn`:

```
python extract_templates.py
```

It reads the Paint.NET files directly (no export step) and prints what it found:

```
head     ellipse  183x284px  aspect 0.6444  (fit off by 93 px)
shield   shield   aspect 0.7464  shoulder 0.514  fit within 2.10px  482 points
```

Both templates follow the same convention — layer 0 is a sample photo, layer 1
is a white matte with the shape punched out, layer 2 is a guide outline. If you
add a template, add it to `TEMPLATES` at the top of `extract_templates.py`.

The shapes are stored as maths, not pixels, so they stay smooth at any print
size. The head turned out to be an exact ellipse. The shield is fitted as a
proper heraldic shield — straight sides down to 51.4% of the height, then a
Bézier curve to the point — which matches your drawing to within about two
pixels and removes the hand-drawn wobble that would otherwise show as visibly
wavy edges once enlarged.
