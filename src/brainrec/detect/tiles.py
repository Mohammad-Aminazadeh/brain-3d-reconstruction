"""Tiles over the tissue for nucleus detection (M7-05a, M7-05c; contract C13).

The detection level is split into cores of `tile_px - 2 * half` pixels that partition it. Each
core is read with a margin of `half` pixels on every side (clipped at the slide border), so
windows are at most `tile_px` wide and neighbouring windows overlap by `2 * half`. A nucleus
belongs to the tile whose core contains its centre: it is counted once, and as long as its
radius is below `half` it lies whole inside that tile's window. Cores without tissue are skipped.

All coordinates here are pixels of the detection level (OpenCV convention, pixel centres at
integers); the mask is a tissue mask whose pixels are `mask_scale` detection pixels wide.
"""

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Tile:
    """Core [x0, x1) x [y0, y1) owned by the tile, and the window [rx0, rx1) x [ry0, ry1) read."""

    x0: int
    y0: int
    x1: int
    y1: int
    rx0: int
    ry0: int
    rx1: int
    ry1: int

    @property
    def window_shape(self) -> tuple[int, int]:
        return self.ry1 - self.ry0, self.rx1 - self.rx0

    def owns(self, x_px: np.ndarray, y_px: np.ndarray) -> np.ndarray:
        """Whether centres (detection px, slide level) lie in the core. Centres are continuous;
        the core of pixels [x0, x1) spans [x0 - 0.5, x1 - 0.5) so that cores tile the plane."""
        x, y = np.asarray(x_px, float), np.asarray(y_px, float)
        return (
            (x >= self.x0 - 0.5) & (x < self.x1 - 0.5) & (y >= self.y0 - 0.5) & (y < self.y1 - 0.5)
        )


def half_overlap_px(overlap_um: float, um_per_px: float) -> int:
    """Margin added on each side of a core: half the overlap, in pixels (at least 1)."""
    return max(1, round(overlap_um / um_per_px / 2))


def plan_tiles(
    width: int,
    height: int,
    tile_px: int,
    half: int,
    mask: np.ndarray | None = None,
    mask_scale: float = 1.0,
) -> list[Tile]:
    """Tiles over a level of `width` x `height` px, row by row. Cores are `tile_px - 2 * half`
    wide; with a `mask` (nonzero = tissue, one pixel = `mask_scale` level pixels) only tiles
    whose core holds tissue are returned. Raises ValueError if the core would be empty."""
    step = tile_px - 2 * half
    if step < 1:
        raise ValueError(f"tile of {tile_px} px leaves no core with an overlap margin of {half} px")
    tiles = []
    for y0 in range(0, height, step):
        for x0 in range(0, width, step):
            x1, y1 = min(x0 + step, width), min(y0 + step, height)
            if mask is not None and not _has_tissue(mask, mask_scale, x0, y0, x1, y1):
                continue
            tiles.append(Tile(x0, y0, x1, y1, max(0, x0 - half), max(0, y0 - half),
                              min(width, x1 + half), min(height, y1 + half)))  # fmt: skip
    return tiles


def _has_tissue(mask: np.ndarray, scale: float, x0: int, y0: int, x1: int, y1: int) -> bool:
    mx0, my0 = int(np.floor(x0 / scale)), int(np.floor(y0 / scale))
    mx1, my1 = int(np.ceil(x1 / scale)), int(np.ceil(y1 / scale))
    return bool(np.any(mask[my0:my1, mx0:mx1]))


def in_mask(mask: np.ndarray, mask_scale: float, x_px: np.ndarray, y_px: np.ndarray) -> np.ndarray:
    """Whether points (level px, pixel centres at integers) lie on tissue pixels of `mask`.
    Points outside the mask image are not tissue."""
    mx = np.floor((np.asarray(x_px, float) + 0.5) / mask_scale).astype(int)
    my = np.floor((np.asarray(y_px, float) + 0.5) / mask_scale).astype(int)
    ok = (mx >= 0) & (my >= 0) & (mx < mask.shape[1]) & (my < mask.shape[0])
    out = np.zeros(mx.shape, bool)
    out[ok] = mask[my[ok], mx[ok]] > 0
    return out


def window_mask(mask: np.ndarray, mask_scale: float, tile: Tile) -> np.ndarray:
    """The tissue mask over the tile's window, at the level's resolution (bool, window shape)."""
    rows = np.floor((np.arange(tile.ry0, tile.ry1) + 0.5) / mask_scale).astype(int)
    cols = np.floor((np.arange(tile.rx0, tile.rx1) + 0.5) / mask_scale).astype(int)
    rows = np.clip(rows, 0, mask.shape[0] - 1)
    cols = np.clip(cols, 0, mask.shape[1] - 1)
    return mask[np.ix_(rows, cols)] > 0
