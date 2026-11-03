"""Overlay image of one registered pair for visual checks (M4-02).

Colour code (as in `results/README.md`): fixed slice green, moving slice magenta after alignment,
grey/white where both agree; background black. Tissue outlines in the same colours.
"""

from pathlib import Path

import cv2
import numpy as np

from brainrec.registration import RegImage, Transform2D
from brainrec.store import write_atomic

FIXED_RGB = (0, 255, 0)  # green
MOVING_RGB = (255, 0, 255)  # magenta
OUTLINE_PX = 3


def overlay_rgb(fixed: RegImage, moving: RegImage, t: Transform2D) -> np.ndarray:
    """(H, W, 3) uint8 RGB image in the fixed frame: fixed in green, `moving` warped by `t`
    (moving -> fixed, µm) in red and blue, tissue outlines on top."""
    if fixed.mpp_um != moving.mpp_um or fixed.downsample != moving.downsample:
        raise ValueError("fixed and moving must share pixel size and level")
    m = t.to_pixels(moving.downsample, fixed.downsample, fixed.mpp_um)[:2]
    size = fixed.image.shape[::-1]
    image = cv2.warpAffine(moving.image, m, size, flags=cv2.INTER_LINEAR)
    mask = cv2.warpAffine(moving.mask, m, size, flags=cv2.INTER_NEAREST)
    rgb = np.dstack([image, fixed.image, image])
    for outline, colour in ((fixed.mask, FIXED_RGB), (mask, MOVING_RGB)):
        contours, _ = cv2.findContours(outline, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        cv2.drawContours(rgb, contours, -1, colour, OUTLINE_PX)
    return rgb


def render_overlay(fixed: RegImage, moving: RegImage, t: Transform2D, out: Path) -> Path:
    """Write the overlay of the pair (see `overlay_rgb`) as PNG to `out`, atomically, at the
    registration images' size and level. Returns `out`."""
    ok, png = cv2.imencode(".png", cv2.cvtColor(overlay_rgb(fixed, moving, t), cv2.COLOR_RGB2BGR))
    if not ok:
        raise OSError(f"could not encode overlay {out}")
    write_atomic(out, png.tobytes())
    return out
