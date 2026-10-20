"""Resampling one slice into the registered, tissue-only volume (M3-09; contracts C1, C8, C11).

Pixel matrices here are 3x3 maps between pixel coordinates in the OpenCV convention (integer =
pixel centre), for example `Transform2D.to_pixels(...)`. A tile is `(x0, y0, x1, y1)` in canvas
pixels of one pyramid level, end-exclusive.
"""

import cv2
import numpy as np

from brainrec.registration import Transform2D, px_to_um

BACKGROUND = 255  # fill value: never stored, shown transparent (C11)
TISSUE_WHITE = 254  # tissue that happens to be pure white is stored as this (C11)

Tile = tuple[int, int, int, int]


def _shift(dx: float, dy: float) -> np.ndarray:
    return np.array([[1.0, 0.0, dx], [0.0, 1.0, dy], [0.0, 0.0, 1.0]])


def _apply(m: np.ndarray, pts: np.ndarray) -> np.ndarray:
    return np.asarray(pts, float) @ m[:2, :2].T + m[:2, 2]


def enlarge_mask(mask: np.ndarray, margin_px: float) -> np.ndarray:
    """Tissue mask grown by `margin_px` (so tissue is never cut off; C11), uint8 {0, 255}."""
    binary = np.where(mask > 0, 255, 0).astype(np.uint8)
    r = round(margin_px)
    if r <= 0:
        return binary
    return cv2.dilate(binary, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1)))


def outline_corners(mask: np.ndarray) -> np.ndarray:
    """(N, 2) pixel corners of the outer outlines of all tissue components, in mask pixels.

    Enough to find the extent of the tissue under any similarity transform.
    """
    contours, _ = cv2.findContours(
        np.where(mask > 0, 255, 0).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    if not contours:
        return np.empty((0, 2))
    pts = np.vstack([c.reshape(-1, 2) for c in contours]).astype(float)
    offsets = np.array([[-0.5, -0.5], [0.5, -0.5], [-0.5, 0.5], [0.5, 0.5]])
    return (pts[:, None, :] + offsets[None]).reshape(-1, 2)


def tissue_extent_um(
    corners_px: np.ndarray, mask_downsample: float, mpp: tuple[float, float], t: Transform2D
) -> tuple[float, float, float, float] | None:
    """(xmin, ymin, xmax, ymax) in the target frame of `t` of the outline corners (mask px)."""
    if not len(corners_px):
        return None
    pts = t.apply(px_to_um(corners_px, mask_downsample, mpp))
    return (
        float(pts[:, 0].min()),
        float(pts[:, 1].min()),
        float(pts[:, 0].max()),
        float(pts[:, 1].max()),
    )


def source_box(
    inv: np.ndarray, tile: Tile, src_w: int, src_h: int, pad: int = 2
) -> tuple[int, int, int, int] | None:
    """Source pixels needed to fill `tile`: the inverse-mapped tile corners, padded for
    bilinear interpolation and clipped to the source. None if the tile maps outside it."""
    x0, y0, x1, y1 = tile
    corners = np.array([[x0, y0], [x1 - 1, y0], [x0, y1 - 1], [x1 - 1, y1 - 1]], float)
    p = _apply(inv, corners)
    sx0 = max(0, int(np.floor(p[:, 0].min())) - pad)
    sy0 = max(0, int(np.floor(p[:, 1].min())) - pad)
    sx1 = min(src_w, int(np.ceil(p[:, 0].max())) + pad + 1)
    sy1 = min(src_h, int(np.ceil(p[:, 1].max())) + pad + 1)
    if sx0 >= sx1 or sy0 >= sy1:
        return None
    return sx0, sy0, sx1, sy1


def warp_tile(
    region: np.ndarray, region_xy: tuple[int, int], inv: np.ndarray, tile: Tile
) -> tuple[np.ndarray, np.ndarray]:
    """Fill `tile` from a source `region` (whose top-left source pixel is `region_xy`).

    `inv` maps canvas pixels -> source pixels. Returns the RGB tile (bilinear; white where no
    source) and a bool array: True where the source region covers the pixel.
    """
    x0, y0, x1, y1 = tile
    a = _shift(-region_xy[0], -region_xy[1]) @ inv @ _shift(x0, y0)  # tile px -> region px
    size = (x1 - x0, y1 - y0)
    flags = cv2.WARP_INVERSE_MAP
    rgb = cv2.warpAffine(
        region, a[:2], size, flags=cv2.INTER_LINEAR | flags,
        borderMode=cv2.BORDER_CONSTANT, borderValue=(BACKGROUND,) * 3,
    )  # fmt: skip
    covered = np.full(region.shape[:2], 255, np.uint8)
    valid = cv2.warpAffine(
        covered, a[:2], size, flags=cv2.INTER_NEAREST | flags,
        borderMode=cv2.BORDER_CONSTANT, borderValue=0,
    )  # fmt: skip
    return rgb, valid > 0


def warp_mask_tile(mask: np.ndarray, mask_to_canvas: np.ndarray, tile: Tile) -> np.ndarray:
    """Bool tissue mask of `tile`: `mask` (its own pixels) warped by `mask_to_canvas`.

    Bilinear then thresholded, which smooths the steps of a coarse mask when upsampling.
    """
    x0, y0, x1, y1 = tile
    a = _shift(-x0, -y0) @ mask_to_canvas
    warped = cv2.warpAffine(
        mask, a[:2], (x1 - x0, y1 - y0), flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT, borderValue=0,
    )  # fmt: skip
    return warped >= 128


def finish_tile(rgb: np.ndarray, keep: np.ndarray) -> np.ndarray:
    """Tissue only (C11): background exactly BACKGROUND, tissue never pure white."""
    out = rgb.copy()
    out[~keep] = BACKGROUND
    out[keep & (out == BACKGROUND).all(axis=2)] = TISSUE_WHITE
    return out


def canvas_tiles(
    extent_px: tuple[float, float, float, float],
    shape_hw: tuple[int, int],
    shard_hw: tuple[int, int],
) -> list[Tile]:
    """Shard-aligned tiles of a level that intersect `extent_px` (xmin, ymin, xmax, ymax)."""
    h, w = shape_hw
    sy, sx = shard_hw
    bx0 = max(0, int(np.floor(extent_px[0])) - 1)
    by0 = max(0, int(np.floor(extent_px[1])) - 1)
    bx1 = min(w, int(np.ceil(extent_px[2])) + 2)
    by1 = min(h, int(np.ceil(extent_px[3])) + 2)
    return [
        (tx, ty, min(tx + sx, w), min(ty + sy, h))
        for ty in range(by0 // sy * sy, by1, sy)
        for tx in range(bx0 // sx * sx, bx1, sx)
    ]
