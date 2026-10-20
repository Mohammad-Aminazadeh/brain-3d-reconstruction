"""Tissue mask: what counts as tissue for every later stage (F12, contract C4).

Stained tissue is coloured, glass is grey or white, so the mask is a threshold on colour.
Colour is measured as chroma, max(R, G, B) - min(R, G, B), not HSV saturation: saturation
divides by max(R, G, B) and explodes for near-black pixels, which made the black scanner edge
of slice 10 look like tissue. For bright pixels the two agree closely (spike, saturation: glass
about 9 to 10, tissue about 27 to 30, pale fragments about 18).
"""

import cv2
import numpy as np

from brainrec.catalog import ProjectConfig

CLOSE_UM = 100.0  # bridges thin gaps such as vessel lines crossing the tissue
OPEN_UM = 35.0  # removes isolated glass noise along the edge
BLUR_UM = 15.0  # smooths saturation before thresholding
MIN_THRESHOLD = (
    15  # Otsu always splits, even pure glass; real glass is 9 to 10, real thresholds 18 to 19
)


def _disc(diameter_um: float, um_per_px: float) -> np.ndarray:
    d = max(1, round(diameter_um / um_per_px) | 1)  # odd size, at least 1 px
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (d, d))


def _fill_holes(mask: np.ndarray) -> np.ndarray:
    """Everything not reachable from the (padded) border belongs to the tissue."""
    padded = cv2.copyMakeBorder(mask, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=0)
    outside = 255 - padded
    flood = np.zeros((padded.shape[0] + 2, padded.shape[1] + 2), np.uint8)
    cv2.floodFill(outside, flood, (0, 0), 0)
    return np.bitwise_or(padded, outside)[1:-1, 1:-1]


def _hysteresis(strong: np.ndarray, weak: np.ndarray) -> np.ndarray:
    """Weak regions are kept only where they connect to at least one strong pixel."""
    n, labels = cv2.connectedComponents(weak.astype(np.uint8), connectivity=8)
    keep = np.zeros(n, bool)
    keep[np.unique(labels[strong])] = True
    keep[0] = False
    return keep[labels]


def make_mask(rgb: np.ndarray, um_per_px: float, cfg: ProjectConfig) -> np.ndarray:
    """uint8 {0, 255} tissue mask of an RGB image with `um_per_px` µm per pixel."""
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError(f"expected an RGB image, got shape {rgb.shape}")
    chroma = (rgb.max(axis=2).astype(np.int16) - rgb.min(axis=2)).astype(np.uint8)
    blurred = cv2.GaussianBlur(chroma, (0, 0), max(0.5, BLUR_UM / um_per_px / 2))
    otsu, _ = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    threshold = max(otsu, MIN_THRESHOLD)
    tissue = _hysteresis(blurred >= threshold, blurred >= cfg.mask_hysteresis * threshold)
    m: np.ndarray = np.where(tissue, 255, 0).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, _disc(CLOSE_UM, um_per_px))
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, _disc(OPEN_UM, um_per_px))
    m = _fill_holes(m)
    # keep every piece of tissue above the minimum size: detached fragments stay (decision 14)
    _, labels, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    min_px = cfg.mask_min_component_mm2 * 1e6 / um_per_px**2
    keep = np.flatnonzero(stats[1:, cv2.CC_STAT_AREA] >= min_px) + 1
    return np.where(np.isin(labels, keep), 255, 0).astype(np.uint8)
