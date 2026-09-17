#!/usr/bin/env python3
"""Generate `custom_components/alwaysfull/brand/icon.png`.

The icon is committed, so this script is not run by CI or by anything at
runtime. It is here because "generated programmatically" is only a
checkable claim if the program is in the repository: anyone can re-run it,
see that it reproduces the committed bytes, and change the drawing without
reverse-engineering a PNG.

Requires Pillow (`requirements-dev.txt`). Run from the repository root:

    python scripts/make_brand_icon.py

Design notes:

- 512x512 RGBA with a transparent background, which is what
  home-assistant/brands asks for. No brands PR is open -- that path closed
  to custom components in 2026.3 -- but producing the shape it wants costs
  nothing and means the file is submittable if it reopens.
- Everything is drawn at 4x and downsampled with LANCZOS. Pillow's
  `ImageDraw` has no antialiasing of its own, so drawing straight to 512
  gives visibly stepped curves on the bowl rim.
- The bowl is drawn as a half-ellipse (the body) plus a full ellipse (the
  rim), which is the same trick a bowl seen in three-quarter view uses.
  The droplet is a circle plus the triangle formed by the two tangents
  from the apex, so the join is exactly smooth rather than approximately
  smooth -- the triangle's base is a chord of the circle, so the union has
  no seam at any scale.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

# Supersampling factor. 4x is enough that no stair-stepping survives the
# downsample; 8x is indistinguishable and four times slower.
SCALE = 4
SIZE = 512

# Deep blue for the vessel, bright blue for the water it holds. Two tones
# only: an icon is read at 32 pixels in a sidebar, where a third colour
# becomes mud.
BOWL = (11, 79, 122, 255)
RIM = (20, 119, 181, 255)
WATER = (79, 195, 247, 255)
DROPLET = (79, 195, 247, 255)
HIGHLIGHT = (255, 255, 255, 62)

OUTPUT = Path(__file__).resolve().parent.parent / "custom_components/alwaysfull/brand/icon.png"


def _box(x0: float, y0: float, x1: float, y1: float) -> tuple[float, float, float, float]:
    """Scale a 512-space bounding box into supersampled space."""
    return (x0 * SCALE, y0 * SCALE, x1 * SCALE, y1 * SCALE)


def _teardrop(
    draw: ImageDraw.ImageDraw,
    centre_x: float,
    centre_y: float,
    radius: float,
    apex_y: float,
) -> None:
    """Draw a teardrop: a circle, plus the triangle of its two tangents.

    The tangent from the apex touches the circle at an angle `phi` from
    straight up, where `cos(phi) = radius / height`. Using the tangent
    points rather than eyeballed ones is what makes the straight sides meet
    the curve without a visible corner.
    """
    height = centre_y - apex_y
    phi = math.acos(radius / height)
    offset_x = radius * math.sin(phi)
    offset_y = radius * math.cos(phi)

    draw.ellipse(
        _box(
            centre_x - radius,
            centre_y - radius,
            centre_x + radius,
            centre_y + radius,
        ),
        fill=DROPLET,
    )
    draw.polygon(
        [
            (centre_x * SCALE, apex_y * SCALE),
            ((centre_x + offset_x) * SCALE, (centre_y - offset_y) * SCALE),
            ((centre_x - offset_x) * SCALE, (centre_y - offset_y) * SCALE),
        ],
        fill=DROPLET,
    )
    # One glint, upper left, where a light source above and to the left
    # would put it. Kept small and faint on purpose: a larger or more
    # opaque highlight stops reading as a reflection and starts reading as
    # a hole punched through the droplet, which is exactly what it looked
    # like at 32 pixels before these numbers were cut.
    draw.ellipse(
        _box(
            centre_x - radius * 0.62,
            centre_y - radius * 0.66,
            centre_x - radius * 0.20,
            centre_y - radius * 0.10,
        ),
        fill=HIGHLIGHT,
    )


def build() -> Image.Image:
    """Return the finished 512x512 icon."""
    canvas = Image.new("RGBA", (SIZE * SCALE, SIZE * SCALE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    _teardrop(draw, centre_x=256, centre_y=196, radius=66, apex_y=48)

    # Bowl body: the LOWER half of an ellipse. Pillow measures pieslice
    # angles clockwise from 3 o'clock, so 0..180 is the bottom half; the
    # flat edge lands on the ellipse's horizontal centreline, at y=325.
    draw.pieslice(_box(64, 190, 448, 460), start=0, end=180, fill=BOWL)

    # Rim: a full ellipse on that same centreline, so the bowl reads as an
    # open vessel seen slightly from above rather than as a half-disc.
    draw.ellipse(_box(64, 285, 448, 365), fill=RIM)

    # The water surface inside the rim.
    draw.ellipse(_box(100, 297, 412, 353), fill=WATER)

    return canvas.resize((SIZE, SIZE), Image.Resampling.LANCZOS)


def main() -> None:
    """Write the icon, creating its directory if needed."""
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    build().save(OUTPUT, format="PNG", optimize=True)


if __name__ == "__main__":
    main()
