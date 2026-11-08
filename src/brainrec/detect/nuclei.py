"""Nuclei of a whole slice (M7-05; contract C13).

`load_detector` puts a segmentation method behind one function; `detect_slice` runs it over the
tissue in overlapping tiles at the detection level (C9 `detect_downsample`, 2x), keeps each
nucleus once (the tile holding its centre) and only on tissue (F12), measures it, reads a
level-0 patch for nuclei of `detect_patch_min_diameter_um` or more (exact size, nucleolus), and
marks nuclei in blurred tiles. Classification is a separate step (C14, decision 0006), so the
result `Nuclei` is the C13 table without its class columns.

Positions are in the slice frame (C1, µm); outlines as in C13 (int16 offsets of 0.1 µm).
"""

import importlib.metadata
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import cv2
import numpy as np

from brainrec.catalog import ProjectConfig
from brainrec.detect.measure import Measured, measure_nuclei, refine_patch, sharpness
from brainrec.detect.tiles import Tile, half_overlap_px, in_mask, plan_tiles, window_mask
from brainrec.slides.source import SliceSource
from brainrec.store.cells import FEATURES, encode_outlines

log = logging.getLogger(__name__)

# Level-0 sharpness of a region is about this factor times its sharpness at 2x (M7-05: 60 real
# windows of 226 µm on slices 1, 5, 70 and 140; median 0.18, quartiles 0.17 and 0.19, log
# correlation 0.98). It puts `detect_min_sharpness` on the level-0 scale of M7-01.
SHARPNESS_2X_TO_1X = 0.18

Segment = Callable[[np.ndarray, float], tuple[np.ndarray, np.ndarray | None]]
"""RGB window (h, w, 3) uint8 and its pixel size (µm) -> (labels int32 (h, w), 0 = background;
confidence per label value (index = label) or None if the method gives none)."""


@dataclass(frozen=True)
class Detector:
    name: str
    version: str
    segment: Segment


def load_detector(method: str) -> Detector:
    """The segmentation method of C9 `detect_method`: "instanseg" (InstanSeg brightfield_nuclei;
    no confidence per nucleus, so `det_conf` is NaN) or "stardist" (StarDist 2D_versatile_he,
    with its object probability). The model is loaded on the first call. ValueError for an
    unknown method."""
    if method == "instanseg":
        return Detector("instanseg brightfield_nuclei",
                        f"instanseg-torch {_version('instanseg-torch')}", _instanseg())  # fmt: skip
    if method == "stardist":
        return Detector("stardist 2D_versatile_he", f"stardist {_version('stardist')}", _stardist())
    raise ValueError(f"unknown detection method {method!r} (instanseg or stardist)")


def px_to_um(xy: np.ndarray, downsample: float, mpp: tuple[float, float]) -> np.ndarray:
    """(n, 2) level pixel positions (pixel centres at integers) -> slice µm (C1)."""
    return (np.asarray(xy, float).reshape(-1, 2) + 0.5) * [downsample * mpp[0], downsample * mpp[1]]


def _version(package: str) -> str:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "not installed"


def _instanseg() -> Segment:
    model: list = []

    def segment(rgb: np.ndarray, um_px: float) -> tuple[np.ndarray, np.ndarray | None]:
        if not model:
            from instanseg import InstanSeg

            model.append(InstanSeg("brightfield_nuclei", verbosity=0))
        labels = model[0].eval_small_image(rgb, pixel_size=um_px, return_image_tensor=False)
        return np.asarray(labels.squeeze().cpu().numpy(), np.int32), None

    return segment


def _stardist() -> Segment:
    model: list = []

    def segment(rgb: np.ndarray, um_px: float) -> tuple[np.ndarray, np.ndarray | None]:
        from csbdeep.utils import normalize

        if not model:
            from stardist.models import StarDist2D

            model.append(StarDist2D.from_pretrained("2D_versatile_he"))
        f = um_px / 0.25  # trained on H&E at about 0.25 µm/px
        img = cv2.resize(rgb, None, fx=f, fy=f, interpolation=cv2.INTER_LINEAR)
        labels, info = model[0].predict_instances(normalize(img, 1, 99.8, axis=(0, 1, 2)),
                                                  verbose=False)  # fmt: skip
        labels = cv2.resize(labels.astype(np.int32), rgb.shape[1::-1],
                            interpolation=cv2.INTER_NEAREST)  # fmt: skip
        conf = np.concatenate([[0.0], np.asarray(info["prob"], float)])
        return labels, conf

    return segment


@dataclass
class Nuclei:
    """Nuclei of one slice: the C13 table without class columns (store.cells.Profiles)."""

    x_um: np.ndarray
    y_um: np.ndarray
    r_um: np.ndarray
    area_um2: np.ndarray
    outline_offsets: np.ndarray
    outline_xy: np.ndarray
    features: np.ndarray  # (n, 9), FEATURES order
    nucleolus_seen: np.ndarray
    det_conf: np.ndarray
    blurred: np.ndarray
    tile_sharpness: list[float] = field(default_factory=list)  # level-0 scale, one per tile
    n_blurred_tiles: int = 0
    n_refined: int = 0  # nuclei whose outline was refined on a level-0 patch

    def __len__(self) -> int:
        return len(self.x_um)


@dataclass
class _Rows:
    x: list[float] = field(default_factory=list)
    y: list[float] = field(default_factory=list)
    area: list[float] = field(default_factory=list)
    outlines: list[np.ndarray] = field(default_factory=list)  # µm, slice frame
    features: list[np.ndarray] = field(default_factory=list)
    nucleolus: list[int] = field(default_factory=list)
    conf: list[float] = field(default_factory=list)
    blurred: list[int] = field(default_factory=list)


def detect_slice(
    reader: SliceSource,
    mask: np.ndarray,
    mask_downsample: float,
    cfg: ProjectConfig,
    detector: Detector,
) -> Nuclei:
    """All nuclei of a slice. `mask` is its tissue mask (nonzero = tissue) at `mask_downsample`
    (C4: the registration level). Reads the detection level tile by tile and level-0 patches
    around the larger nuclei; never a whole level-0 image."""
    ds = float(cfg.detect_downsample)
    mpp = reader.mpp
    lvl = reader.level_for_downsample(ds)
    lvl0 = reader.level_for_downsample(1)
    width, height = reader.levels[lvl].width, reader.levels[lvl].height
    um_px = ds * mpp[0]
    scale = mask_downsample / ds
    half = half_overlap_px(cfg.detect_overlap_um, um_px)
    tiles = plan_tiles(width, height, cfg.detect_tile_px, half, mask, scale)
    rows = _Rows()
    sharp: list[float] = []
    n_refined = 0
    t0 = time.monotonic()
    for i, tile in enumerate(tiles):
        rgb = reader.read_region(lvl, tile.rx0, tile.ry0, *tile.window_shape[::-1])
        tissue = window_mask(mask, scale, tile)
        s = sharpness(rgb, tissue) * SHARPNESS_2X_TO_1X
        sharp.append(s)
        blurred = bool(s < cfg.detect_min_sharpness)
        labels, conf = detector.segment(rgb, um_px)
        keep = _kept(labels, tile, (width, height), mask, scale, um_px, cfg.detect_min_area_um2)
        m = measure_nuclei(rgb, labels, um_px, keep, tissue)
        n_refined += _collect(rows, m, labels, conf, tile, reader, lvl0, ds, mpp, cfg, blurred)
        if (i + 1) % 50 == 0:
            log.info("tile %d/%d, %d nuclei, %.0f s", i + 1, len(tiles), len(rows.x),
                     time.monotonic() - t0)  # fmt: skip
    x, y = np.array(rows.x, float), np.array(rows.y, float)
    offsets, xy = encode_outlines(rows.outlines, x, y)
    area = np.array(rows.area, float)
    return Nuclei(
        x, y, np.sqrt(area / np.pi), area, offsets, xy,
        np.array(rows.features, np.float32).reshape(-1, len(FEATURES)),
        np.array(rows.nucleolus, np.uint8), np.array(rows.conf, np.float32),
        np.array(rows.blurred, np.uint8), sharp,
        int(sum(v < cfg.detect_min_sharpness for v in sharp)), n_refined,
    )  # fmt: skip


def _kept(
    labels: np.ndarray,
    tile: Tile,
    level_size: tuple[int, int],
    mask: np.ndarray,
    scale: float,
    um_px: float,
    min_area_um2: float,
) -> np.ndarray:
    """Label values that count for this tile: large enough, not cut by the window edge (unless
    that edge is the slide border), centre in the tile's core and on tissue (C13, F12)."""
    n = int(labels.max())
    if n == 0:
        return np.zeros(0, np.int64)
    idx = np.arange(1, n + 1)
    area = np.bincount(labels.ravel(), minlength=n + 1)[1:]
    ys, xs = np.indices(labels.shape)
    cx = np.bincount(labels.ravel(), xs.ravel(), n + 1)[1:] / np.maximum(area, 1)
    cy = np.bincount(labels.ravel(), ys.ravel(), n + 1)[1:] / np.maximum(area, 1)
    cut = np.zeros(n + 1, bool)
    w, h = level_size
    edges = []
    if tile.rx0 > 0:
        edges.append(labels[:, 0])
    if tile.rx1 < w:
        edges.append(labels[:, -1])
    if tile.ry0 > 0:
        edges.append(labels[0, :])
    if tile.ry1 < h:
        edges.append(labels[-1, :])
    for e in edges:
        cut[e] = True
    gx, gy = cx + tile.rx0, cy + tile.ry0
    ok = (
        (area > 0)
        & (area * um_px**2 >= min_area_um2)
        & ~cut[1:]
        & tile.owns(gx, gy)
        & in_mask(mask, scale, gx, gy)
    )
    return idx[ok]


def _collect(
    rows: _Rows,
    m: Measured,
    labels: np.ndarray,
    conf: np.ndarray | None,
    tile: Tile,
    reader: SliceSource,
    lvl0: int,
    ds: float,
    mpp: tuple[float, float],
    cfg: ProjectConfig,
    blurred: bool,
) -> int:
    """Append the measured nuclei of one tile in µm; refine the larger ones on level-0 patches.
    Returns the number of refined outlines."""
    n_refined = 0
    for j in range(len(m)):
        feats = m.features[j].copy()
        cx, cy, area, poly = m.cx[j] + tile.rx0, m.cy[j] + tile.ry0, m.area_um2[j], None
        nucleolus = 0
        if feats[FEATURES.index("diameter_um")] >= cfg.detect_patch_min_diameter_um:
            r = _patch(reader, lvl0, labels, int(m.label[j]), m.outlines[j], tile, ds, mpp[0])
            if r is not None:
                ref, x0, y0 = r
                nucleolus = int(ref.nucleolus)
                if ref.refined:
                    n_refined += 1
                    # level-0 px -> detection-level px (pixel centres at integers on both)
                    cx, cy = (x0 + ref.cx + 0.5) / ds - 0.5, (y0 + ref.cy + 0.5) / ds - 0.5
                    area = ref.area_um2
                    poly = px_to_um(ref.outline + [x0, y0], 1.0, mpp)
                    feats[FEATURES.index("diameter_um")] = 2 * np.sqrt(area / np.pi)
        centre = px_to_um(np.array([[cx, cy]]), ds, mpp)[0]
        if poly is None:
            poly = px_to_um(m.outlines[j] + [tile.rx0, tile.ry0], ds, mpp)
        rows.x.append(float(centre[0]))
        rows.y.append(float(centre[1]))
        rows.area.append(float(area))
        rows.outlines.append(poly)
        rows.features.append(feats)
        rows.nucleolus.append(nucleolus)
        rows.conf.append(float(conf[m.label[j]]) if conf is not None else float("nan"))
        rows.blurred.append(int(blurred))
    return n_refined


def _patch(reader: SliceSource, lvl0: int, labels: np.ndarray, label: int, outline: np.ndarray,
           tile: Tile, ds: float, mpp_x: float):  # fmt: skip
    """Read the level-0 patch around one nucleus and refine it. Returns (Refined, x0, y0) with
    the patch origin in level-0 px, or None if the patch would leave the slide."""
    f = round(ds)
    (xmin, ymin), (xmax, ymax) = outline.min(axis=0).astype(int), outline.max(axis=0).astype(int)
    pad = int(np.ceil(2.0 / (ds * mpp_x))) + 1
    r0, r1 = max(0, ymin - pad), min(labels.shape[0], ymax + pad + 1)
    c0, c1 = max(0, xmin - pad), min(labels.shape[1], xmax + pad + 1)
    lab = labels[r0:r1, c0:c1]
    own = np.repeat(np.repeat(lab == label, f, 0), f, 1)
    others = np.repeat(np.repeat((lab > 0) & (lab != label), f, 0), f, 1)
    x0, y0 = (tile.rx0 + c0) * f, (tile.ry0 + r0) * f
    lv0 = reader.levels[lvl0]
    if x0 + own.shape[1] > lv0.width or y0 + own.shape[0] > lv0.height:
        return None
    rgb = reader.read_region(lvl0, x0, y0, own.shape[1], own.shape[0])
    return refine_patch(rgb, own, others, mpp_x), x0, y0
