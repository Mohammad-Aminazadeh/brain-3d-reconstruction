"""Measurements of detected nuclei (M7-05d; contract C13).

`measure_nuclei` gives, per labelled nucleus of a window at the detection level, the centre,
area, outline and the 9 appearance features of M7-18 (store.cells.FEATURES). `refine_patch`
measures one larger nucleus again on a level-0 patch: exact outline and size, and whether a
nucleolus is visible (at 2x a nucleolus is only 2 to 4 px, M7-01). `sharpness` scores focus.

Positions are window pixels (OpenCV convention, pixel centres at integers); sizes are in µm.
Hematoxylin `h` is optical density from colour separation (higher = darker); `gray` is the mean
of R, G and B (lower = darker).

Changes against the M7-18 features (improved in M7-05d):
- `cell_body`: the upper quartile instead of the mean of the ring around the nucleus, because a
  stained cell body usually lies on one side of the nucleus and the mean dilutes it;
- `vessel_um`: lumens are bright gaps (gray > 0.85 x 255) of at least 15 µm² inside the tissue,
  as in the M7-19 rules (M7-18: 0.93 and 20 µm², which found few lumens); the distance is
  capped at VESSEL_CAP_UM, which is half the tile overlap, so it does not depend on the tiling;
- background levels come from tissue pixels only (the windows may include glass).
"""

from dataclasses import dataclass

import cv2
import numpy as np
from scipy import ndimage as ndi
from skimage import measure
from skimage.color import rgb2hed

from brainrec.store.cells import FEATURES

VESSEL_CAP_UM = 20.0
LUMEN_GRAY = 0.85 * 255
LUMEN_MIN_UM2 = 15.0
# nucleolus at level 0 (calibrated by eye on 343 real nuclei of slices 1, 60 and 140 in M7-05;
# Proposed until checked against expert labels): one compact spot in the interior, clearly darker
# than the nucleus both absolutely and relative to its chromatin texture. Chromatin clumps are
# weaker, several, or lie at the nuclear border.
NUCLEOLUS_MIN_CONTRAST = 60.0  # gray levels below the nucleus median
NUCLEOLUS_MIN_MADS = 4.5  # and this many median absolute deviations of the nucleus
NUCLEOLUS_AREA_UM2 = (0.6, 5.0)  # area of the spot (darkest 40% of the range)
NUCLEOLUS_BORDER_UM = 1.0  # the spot lies at least this far inside the nucleus
NUCLEOLUS_MAX_SPOTS = 2  # spots reaching 60% of the darkest contrast; more = clumped chromatin
NUCLEOLUS_SIGMA_UM = 0.25
REFINE_MIN_CONTRAST = 6.0  # gray levels between nucleus and ring; below, keep the 2x outline
REFINE_AREA_RATIO = (0.6, 1.6)  # refined area against the 2x area; outside, keep the 2x outline
REFINE_SMOOTH_UM = 0.3  # smoothing of the gray image and of the refined region


@dataclass
class Measured:
    """Nuclei of one window: label, centre (window px), area (µm²), outline (window px, (k, 2)
    x, y), features (n, 9) in FEATURES order."""

    label: np.ndarray
    cx: np.ndarray
    cy: np.ndarray
    area_um2: np.ndarray
    outlines: list[np.ndarray]
    features: np.ndarray

    def __len__(self) -> int:
        return len(self.label)


def hematoxylin(rgb: np.ndarray) -> np.ndarray:
    """Hematoxylin optical density (float32) by colour separation."""
    return rgb2hed(rgb)[..., 0].astype(np.float32)


def gray_level(rgb: np.ndarray) -> np.ndarray:
    return rgb.astype(np.float32).mean(axis=2)


def _disk(r_px: float) -> np.ndarray:
    r = max(1, round(r_px))
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))


def _dilate(m: np.ndarray, r_px: float) -> np.ndarray:
    return cv2.dilate(m.astype(np.uint8), _disk(r_px)) > 0


def _erode(m: np.ndarray, r_px: float) -> np.ndarray:
    return cv2.erode(m.astype(np.uint8), _disk(r_px), borderValue=0) > 0


def outline_px(own: np.ndarray, x0: int = 0, y0: int = 0) -> np.ndarray:
    """Outer outline of a binary region as (k, 2) x, y pixel centres (largest contour), offset
    by (x0, y0)."""
    contours, _ = cv2.findContours(own.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return np.zeros((0, 2))
    c = max(contours, key=cv2.contourArea).reshape(-1, 2).astype(float)
    return c + [x0, y0]


def measure_nuclei(
    rgb: np.ndarray,
    labels: np.ndarray,
    um_px: float,
    keep: np.ndarray,
    tissue: np.ndarray | None = None,
) -> Measured:
    """Centre, area, outline and appearance features of the nuclei `keep` (label values) in a
    window. `tissue` (bool, window shape) limits the background and lumens to tissue."""
    h = hematoxylin(rgb)
    gray = gray_level(rgb)
    tissue = np.ones(labels.shape, bool) if tissue is None else tissue
    nuclei = labels > 0
    near = _dilate(nuclei, 3 / um_px)
    bg = tissue & ~near
    if not bg.any():
        bg = ~near if (~near).any() else np.ones(labels.shape, bool)
    h_bg, g_bg = float(np.median(h[bg])), float(np.median(gray[bg]))
    _, lumen, stats, _ = cv2.connectedComponentsWithStats(
        ((gray > LUMEN_GRAY) & tissue).astype(np.uint8), connectivity=8
    )
    big = stats[:, cv2.CC_STAT_AREA] * um_px**2 >= LUMEN_MIN_UM2
    big[0] = False  # background
    lumen_mask = big[lumen]
    if lumen_mask.any():
        dist_lumen = cv2.distanceTransform((~lumen_mask).astype(np.uint8), cv2.DIST_L2, 5) * um_px
    else:
        dist_lumen = np.full(labels.shape, VESSEL_CAP_UM, np.float32)
    hs = cv2.GaussianBlur(h, (0, 0), 0.35 / um_px)

    pad = int(np.ceil(3.5 / um_px)) + 1
    rows: list[list[float]] = []
    ids: list[int] = []
    centres: list[tuple[float, float]] = []
    areas: list[float] = []
    outlines: list[np.ndarray] = []
    wanted = {int(v) for v in keep}
    for p in measure.regionprops(labels):
        if p.label not in wanted:
            continue
        r0, c0, r1, c1 = p.bbox
        r0, c0 = max(0, r0 - pad), max(0, c0 - pad)
        r1, c1 = min(labels.shape[0], r1 + pad), min(labels.shape[1], c1 + pad)
        lab = labels[r0:r1, c0:c1]
        own = lab == p.label
        others = (lab > 0) & ~own
        hw, gw = h[r0:r1, c0:c1], gray[r0:r1, c0:c1]
        inner = _erode(own, 1)
        h_in = hw[own]
        body = _dilate(own, 3 / um_px) & ~_dilate(own, 0.5 / um_px) & ~others
        halo = _dilate(own, 1 / um_px) & ~own & ~others
        cy, cx = p.centroid
        rows.append([
            2 * np.sqrt(p.area / np.pi) * um_px,
            p.axis_major_length / max(p.axis_minor_length, 1e-6),
            p.solidity,
            float(h_in.mean() - h_bg),
            float(h_in.std()),
            float(hs[r0:r1, c0:c1][inner].max() - np.median(h_in)) if inner.any() else 0.0,
            float(np.percentile(hw[body], 75) - h_bg) if body.any() else 0.0,
            float(gw[halo].mean() - g_bg) if halo.any() else 0.0,
            float(min(dist_lumen[round(cy), round(cx)], VESSEL_CAP_UM)),
        ])  # fmt: skip
        ids.append(p.label)
        centres.append((cx, cy))
        areas.append(p.area * um_px**2)
        outlines.append(outline_px(own, c0, r0))
    xy = np.array(centres, float).reshape(-1, 2)
    return Measured(
        np.array(ids, np.int64), xy[:, 0], xy[:, 1], np.array(areas, float), outlines,
        np.array(rows, np.float32).reshape(-1, len(FEATURES)),
    )  # fmt: skip


@dataclass
class Refined:
    """Result of a level-0 patch: centre and outline in patch px, area in µm², nucleolus seen,
    and whether the outline was refined (False: the upsampled 2x outline was kept)."""

    cx: float
    cy: float
    area_um2: float
    outline: np.ndarray
    nucleolus: bool
    refined: bool


def refine_patch(rgb: np.ndarray, own: np.ndarray, others: np.ndarray, um_px: float) -> Refined:
    """Measure one nucleus again on a level-0 patch. `own` is its detection-level region
    upsampled to the patch, `others` the other nuclei there (both bool, patch shape).

    Outline: pixels near the coarse border are given to the nucleus if their gray level is
    closer to the nucleus interior than to the ring around it (works for nuclei darker and
    paler than their surroundings); the coarse region is kept when the contrast is too low or
    the refined area is implausible. The gray image and the result are smoothed over
    REFINE_SMOOTH_UM so that chromatin texture does not make the outline jagged. Nucleolus:
    see `has_nucleolus`."""
    sigma = REFINE_SMOOTH_UM / um_px
    gray = cv2.GaussianBlur(gray_level(rgb), (0, 0), sigma)
    inner = _erode(own, 0.6 / um_px)
    ring = _dilate(own, 1.2 / um_px) & ~_dilate(own, 0.4 / um_px) & ~others
    region = own
    refined = False
    if inner.any() and ring.any():
        v_in, v_out = float(np.median(gray[inner])), float(np.median(gray[ring]))
        if abs(v_in - v_out) >= REFINE_MIN_CONTRAST:
            band = _dilate(own, 0.8 / um_px) & ~_erode(own, 0.8 / um_px) & ~others
            closer = np.abs(gray - v_in) < np.abs(gray - v_out)
            cand = _erode(own, 0.8 / um_px) | (band & closer)
            cand = cv2.GaussianBlur(cand.astype(np.float32), (0, 0), sigma) >= 0.5
            cand = ndi.binary_fill_holes(cand & ~others)
            lab = measure.label(cand)
            ids, counts = np.unique(lab[inner & (lab > 0)], return_counts=True)
            if len(ids):
                best = lab == ids[np.argmax(counts)]
                ratio = best.sum() / max(own.sum(), 1)
                if REFINE_AREA_RATIO[0] <= ratio <= REFINE_AREA_RATIO[1]:
                    region, refined = best, True
    ys, xs = np.nonzero(region)
    return Refined(
        float(xs.mean()), float(ys.mean()), float(region.sum() * um_px**2), outline_px(region),
        has_nucleolus(rgb, region, um_px), refined,
    )  # fmt: skip


def nucleolus_spot(rgb: np.ndarray, region: np.ndarray, um_px: float) -> dict[str, float]:
    """Measures of the darkest spot inside a nucleus (level 0): `contrast` (gray levels below the
    nucleus median), `mads` (the same in median absolute deviations), `area_um2` (pixels within
    the darkest 40% of the range, connected to the minimum), `n_spots` (spots reaching 60% of
    the contrast). All 0 if the interior is too small."""
    inner = _erode(region, NUCLEOLUS_BORDER_UM / um_px)
    zero = {"contrast": 0.0, "mads": 0.0, "area_um2": 0.0, "n_spots": 0.0}
    if inner.sum() * um_px**2 < 2.0:
        return zero
    g = cv2.GaussianBlur(gray_level(rgb), (0, 0), NUCLEOLUS_SIGMA_UM / um_px)
    vals = g[region]
    med = float(np.median(vals))
    mad = float(np.median(np.abs(vals - med))) + 1e-6
    lo = float(g[inner].min())
    if med - lo <= 0:
        return zero
    dark = measure.label(inner & (g < lo + 0.4 * (med - lo)))
    props = measure.regionprops(dark, intensity_image=g)
    darkest = min(props, key=lambda p: p.intensity_min)
    n_spots = sum(1 for p in props if med - p.intensity_min >= 0.6 * (med - lo))
    return {"contrast": med - lo, "mads": (med - lo) / mad,
            "area_um2": float(darkest.area * um_px**2), "n_spots": float(n_spots)}  # fmt: skip


def has_nucleolus(rgb: np.ndarray, region: np.ndarray, um_px: float) -> bool:
    """Whether a nucleolus is visible: the darkest interior spot (`nucleolus_spot`) passes the
    NUCLEOLUS_* limits."""
    s = nucleolus_spot(rgb, region, um_px)
    return bool(
        s["contrast"] >= NUCLEOLUS_MIN_CONTRAST
        and s["mads"] >= NUCLEOLUS_MIN_MADS
        and NUCLEOLUS_AREA_UM2[0] <= s["area_um2"] <= NUCLEOLUS_AREA_UM2[1]
        and s["n_spots"] <= NUCLEOLUS_MAX_SPOTS
    )


def sharpness(rgb: np.ndarray, tissue: np.ndarray | None = None) -> float:
    """Variance of the Laplacian of the gray image over tissue pixels away from the tissue
    edge (the edge to the glass would dominate). NaN if there is no tissue."""
    lap = cv2.Laplacian(gray_level(rgb), cv2.CV_32F)
    if tissue is None:
        return float(lap.var())
    core = _erode(tissue, 8)
    sel = core if core.sum() >= 1000 else tissue
    return float(lap[sel].var()) if sel.any() else float("nan")
