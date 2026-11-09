"""Detection QC (M7-16): where is the detection doubtful? Per slice of a range:

- counts and density (profiles per mm² of tissue, total and per class; per mm² of section until
  the section thickness is known);
- blur: share of blurred tiles and of blurred profiles (from the cell file, C13);
- folds: regions of the slide's registration level whose optical density is at least
  `qc_fold_od_ratio` x the slice's median tissue density (a fold doubles the tissue), and the
  profiles inside them;
- flags: empty slice, density or class share off the neighbouring slices, many blurred tiles,
  folds;
- one sheet of review crops with the detected outlines (`qc/cells_{id}.jpg`).

Reads the cell files (C13), the tissue masks (C4) and the slide; never detects again. Resumable
per slice; the thresholds of the flags are re-evaluated on every run. Report schema
`brainrec.cellsqc/1` in `qc/cells_report.json`.
"""

import hashlib
import json
import logging
from typing import Any

import cv2
import numpy as np

from brainrec.catalog import Catalog, ProjectConfig, SliceRef
from brainrec.slides import ReaderFactory, SlideReader
from brainrec.store import Manifest, StageExistsError, StageResult, WorkDir, fingerprint
from brainrec.store.cells import Profiles, load_profiles
from brainrec.store.stage import write_atomic

log = logging.getLogger(__name__)
STAGE = "qc_cells"
SCHEMA = "brainrec.cellsqc/1"
VERSION = 1  # bump when the per-slice numbers or crops change
NEIGHBOURS = 2  # slices compared on each side
MIN_CLASS_SHARE = 0.02  # classes rarer than this in the neighbours are not compared
CROP_PX = 512  # review crop side at the detection level (2x: about 226 µm)
CROPS_PER_KIND = 2
CAPTION_PX = 22
SUBPIXEL_BITS = 4  # outlines are drawn at 1/16 pixel
FOLD_LINE = (0, 0, 0)  # fold boundary in the crops (RGB)


def find_folds(
    rgb: np.ndarray, mask: np.ndarray, um_px: float, od_ratio: float, min_area_mm2: float
) -> np.ndarray:
    """Folds of a low-resolution slide image (bool, image shape): tissue pixels whose optical
    density -log10(gray / glass) is at least `od_ratio` x the median of the tissue, after a
    3 x 3 opening, in connected regions of at least `min_area_mm2`. The glass level is the
    median gray outside the mask (255 if there is hardly any glass). `um_px`: µm per pixel."""
    if rgb.shape[:2] != mask.shape:
        raise ValueError(f"image {rgb.shape} and mask {mask.shape} do not match")
    tissue = mask > 0
    out = np.zeros(mask.shape, bool)
    if not tissue.any():
        return out
    gray = rgb.astype(np.float32).mean(axis=2)
    glass = float(np.median(gray[~tissue])) if (~tissue).sum() >= 100 else 255.0
    od = -np.log10(np.clip(gray, 1.0, None) / max(glass, 1.0))
    median = float(np.median(od[tissue]))
    if median <= 0:
        return out
    raw = ((od >= od_ratio * median) & tissue).astype(np.uint8)
    cand = cv2.morphologyEx(raw, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(cand, connectivity=8)
    min_px = min_area_mm2 * 1e6 / (um_px * um_px)
    keep = [i for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] >= min_px]
    return np.isin(labels, keep)


def _lookup(grid: np.ndarray, x_um: np.ndarray, y_um: np.ndarray, um_px: float) -> np.ndarray:
    """Values of a low-resolution grid at points in µm (C1: pixel j covers [j, j+1) x um_px);
    points outside the grid give False."""
    j = np.floor(np.asarray(x_um, float) / um_px).astype(int)
    i = np.floor(np.asarray(y_um, float) / um_px).astype(int)
    ok = (j >= 0) & (i >= 0) & (j < grid.shape[1]) & (i < grid.shape[0])
    out = np.zeros(j.shape, bool)
    out[ok] = grid[i[ok], j[ok]] > 0
    return out


def slice_stats(
    profiles: Profiles,
    meta: dict[str, Any],
    mask: np.ndarray,
    folds: np.ndarray,
    um_px: float,
) -> dict[str, Any]:
    """Numbers of one slice. `mask` and `folds` share the grid of `um_px` µm per pixel; profile
    centres are in µm of the slice (C1). Densities are per mm² of tissue."""
    px_mm2 = um_px * um_px / 1e6
    tissue_mm2 = float((mask > 0).sum()) * px_mm2
    n = len(profiles)
    names = list(meta["class_names"])
    counts = np.bincount(profiles.class_id.astype(int), minlength=len(names) + 1)[1:]
    n_tiles = len(meta.get("tile_sharpness", []))
    n_blurred_tiles = int(meta.get("n_blurred_tiles", 0))
    in_folds = _lookup(folds, profiles.x_um, profiles.y_um, um_px)
    n_fold_regions = cv2.connectedComponents(folds.astype(np.uint8), connectivity=8)[0] - 1
    fold_mm2 = float(folds.sum()) * px_mm2

    def density(k: float) -> float:
        return k / tissue_mm2 if tissue_mm2 > 0 else 0.0

    classes = {}
    for name, c in zip(names, counts.tolist(), strict=True):
        share = round(c / n, 4) if n else 0.0
        classes[name] = {"n": c, "density_mm2": round(density(c), 2), "share": share}
    return {
        "tissue_mm2": round(tissue_mm2, 3),
        "n_profiles": n,
        "density_mm2": round(density(n), 2),
        "classes": classes,
        "n_tiles": n_tiles,
        "n_blurred_tiles": n_blurred_tiles,
        "blurred_tile_share": round(n_blurred_tiles / n_tiles, 4) if n_tiles else 0.0,
        "blurred_cell_share": round(float(profiles.blurred.mean()), 4) if n else 0.0,
        "fold_mm2": round(fold_mm2, 3),
        "fold_share": round(fold_mm2 / tissue_mm2, 4) if tissue_mm2 > 0 else 0.0,
        "n_fold_regions": int(n_fold_regions),
        "cells_in_folds": int(in_folds.sum()),
    }


def _neighbour_median(rows: list[dict], i: int, value) -> float | None:
    """Median of `value(row)` over up to NEIGHBOURS non-empty slices on each side of row i."""
    lo, hi = max(0, i - NEIGHBOURS), min(len(rows), i + NEIGHBOURS + 1)
    vals = [value(rows[j]) for j in range(lo, hi) if j != i and rows[j]["n_profiles"] > 0]
    return float(np.median(vals)) if vals else None


def _share(row: dict, name: str) -> float:
    return float(row["classes"].get(name, {}).get("share", 0.0))


def _off(ratio: float | None, limit: float) -> bool:
    return ratio is not None and (ratio > limit or ratio < 1.0 / limit)


def flag_slices(rows: list[dict], cfg: ProjectConfig) -> list[dict]:
    """Flags per slice, rows in slice order (`slice_stats` plus `slice_id`). Adds
    `neighbour_density_mm2`, `density_ratio`, `class_ratios` (compared classes only) and
    `flags`: "empty", "density_outlier", "class_outlier", "blurred", "folds"."""
    out = []
    for i, row in enumerate(rows):
        r = dict(row)
        flags = []
        if row["n_profiles"] == 0:
            flags.append("empty")
        ref = _neighbour_median(rows, i, lambda x: x["density_mm2"])
        ratio = row["density_mm2"] / ref if ref else None
        if _off(ratio, cfg.qc_cells_density_ratio):
            flags.append("density_outlier")
        ratios: dict[str, float] = {}
        if row["n_profiles"] > 0:
            for name, c in row["classes"].items():
                share = _neighbour_median(rows, i, lambda x, k=name: _share(x, k))
                if share is not None and share >= MIN_CLASS_SHARE:
                    ratios[name] = round(c["share"] / share, 3)
        if any(_off(v, cfg.qc_cells_class_ratio) for v in ratios.values()):
            flags.append("class_outlier")
        if row["blurred_tile_share"] > cfg.qc_cells_max_blurred_share:
            flags.append("blurred")
        if row["fold_mm2"] > 0 and row["fold_share"] >= cfg.qc_fold_max_share:
            flags.append("folds")
        r.update(
            neighbour_density_mm2=None if ref is None else round(ref, 2),
            density_ratio=None if ratio is None else round(ratio, 3),
            class_ratios=ratios,
            flags=flags,
        )
        out.append(r)
    return out


def _thresholds(cfg: ProjectConfig) -> dict[str, float]:
    return {
        "qc_cells_density_ratio": cfg.qc_cells_density_ratio,
        "qc_cells_class_ratio": cfg.qc_cells_class_ratio,
        "qc_cells_max_blurred_share": cfg.qc_cells_max_blurred_share,
        "qc_fold_od_ratio": cfg.qc_fold_od_ratio,
        "qc_fold_min_area_mm2": cfg.qc_fold_min_area_mm2,
        "qc_fold_max_share": cfg.qc_fold_max_share,
    }


def build_cells_report(rows: list[dict], cfg: ProjectConfig) -> dict[str, Any]:
    """The report of a range (schema `brainrec.cellsqc/1`) from per-slice rows in slice order."""
    flagged = flag_slices(rows, cfg)
    return {
        "schema": SCHEMA,
        "slice_ids": [r["slice_id"] for r in rows],
        "density_unit": "profiles/mm2 of section",  # per volume needs the section thickness
        "neighbours": NEIGHBOURS,
        "min_class_share": MIN_CLASS_SHARE,
        "thresholds": _thresholds(cfg),
        "slices": flagged,
        "flagged": [r["slice_id"] for r in flagged if r["flags"]],
    }


# --- review crops ---------------------------------------------------------------------------


def _rgb(hex_colour: str) -> tuple[int, int, int]:
    h = hex_colour.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def crop_centres(
    profiles: Profiles,
    mask: np.ndarray,
    folds: np.ndarray,
    um_px: float,
    crop_um: float,
    seed: int,
) -> list[tuple[str, float, float]]:
    """Where to look, as (kind, x µm, y µm): up to CROPS_PER_KIND of the densest clusters of
    blurred profiles (grid of `crop_um`), of the largest folds and of random sharp profiles
    outside folds (random tissue if there are none)."""
    out: list[tuple[str, float, float]] = []
    blurred = profiles.blurred > 0
    if blurred.any():
        x, y = profiles.x_um[blurred], profiles.y_um[blurred]
        grid = np.floor(np.c_[x, y] / crop_um).astype(np.int64)
        _, inverse, counts = np.unique(grid, axis=0, return_inverse=True, return_counts=True)
        inverse = inverse.reshape(-1)
        for k in np.argsort(-counts, kind="stable")[:CROPS_PER_KIND]:
            sel = inverse == k
            out.append(("blurred", float(x[sel].mean()), float(y[sel].mean())))
    if folds.any():
        _, labels, stats, centroids = cv2.connectedComponentsWithStats(
            folds.astype(np.uint8), connectivity=8
        )
        for k in (np.argsort(-stats[1:, cv2.CC_STAT_AREA], kind="stable") + 1)[:CROPS_PER_KIND]:
            ii, jj = np.nonzero(labels == k)  # the fold pixel nearest the centroid
            m = np.argmin((jj - centroids[k][0]) ** 2 + (ii - centroids[k][1]) ** 2)
            out.append(("fold", (jj[m] + 0.5) * um_px, (ii[m] + 0.5) * um_px))
    rng = np.random.default_rng(seed)
    sharp = np.nonzero(~blurred & ~_lookup(folds, profiles.x_um, profiles.y_um, um_px))[0]
    if len(sharp):
        for i in rng.choice(sharp, min(CROPS_PER_KIND, len(sharp)), replace=False):
            out.append(("sharp", float(profiles.x_um[i]), float(profiles.y_um[i])))
    elif (mask > 0).any():
        ii, jj = np.nonzero(mask > 0)
        for m in rng.choice(len(ii), min(CROPS_PER_KIND, len(ii)), replace=False):
            out.append(("tissue", (jj[m] + 0.5) * um_px, (ii[m] + 0.5) * um_px))
    return out


def render_crop(
    reader,
    level: int,
    level_um_px: float,
    centre_um: tuple[float, float],
    profiles: Profiles,
    colours: list[tuple[int, int, int]],
    folds: np.ndarray,
    folds_um_px: float,
) -> np.ndarray:
    """RGB crop (at most CROP_PX square) around `centre_um` at `level`, with the outlines of the
    profiles in their class colours and the fold boundary in black."""
    lv = reader.levels[level]
    w, h = min(CROP_PX, lv.width), min(CROP_PX, lv.height)
    cx, cy = centre_um[0] / level_um_px - 0.5, centre_um[1] / level_um_px - 0.5  # C1 px
    x0 = int(np.clip(round(cx - w / 2), 0, lv.width - w))
    y0 = int(np.clip(round(cy - h / 2), 0, lv.height - h))
    img = np.ascontiguousarray(reader.read_region(level, x0, y0, w, h))
    if folds.any():  # level pixel centre (i + 0.5) x level_um_px -> fold pixel
        rows = np.floor((np.arange(y0, y0 + h) + 0.5) * level_um_px / folds_um_px).astype(int)
        cols = np.floor((np.arange(x0, x0 + w) + 0.5) * level_um_px / folds_um_px).astype(int)
        ok_r = (rows >= 0) & (rows < folds.shape[0])
        ok_c = (cols >= 0) & (cols < folds.shape[1])
        sub = np.zeros((h, w), np.uint8)
        sub[np.ix_(ok_r, ok_c)] = folds[np.ix_(rows[ok_r], cols[ok_c])]
        contours, _ = cv2.findContours(sub, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(img, contours, -1, FOLD_LINE, 2)
    margin = 40.0  # µm: outlines of profiles just outside the crop still show
    x_lo, x_hi = (x0 + 0.5) * level_um_px - margin, (x0 + w + 0.5) * level_um_px + margin
    y_lo, y_hi = (y0 + 0.5) * level_um_px - margin, (y0 + h + 0.5) * level_um_px + margin
    near = np.nonzero((profiles.x_um >= x_lo) & (profiles.x_um < x_hi)
                      & (profiles.y_um >= y_lo) & (profiles.y_um < y_hi))[0]  # fmt: skip
    for i in near:
        pts = profiles.outline(int(i)) / level_um_px - 0.5 - [x0, y0]
        colour = colours[int(profiles.class_id[i]) - 1]
        sub_px = np.round(pts * (1 << SUBPIXEL_BITS)).astype(np.int32)  # sub-pixel accurate
        cv2.polylines(img, [sub_px], True, colour, 1, cv2.LINE_AA, SUBPIXEL_BITS)
    return img


def review_sheet(crops: list[tuple[str, np.ndarray]], slice_id: int) -> np.ndarray:
    """Crops side by side, three per row, each with a caption (RGB)."""
    cols = 3
    if not crops:
        sheet = np.full((CAPTION_PX * 2, CROP_PX, 3), 255, np.uint8)
        cv2.putText(sheet, f"slice {slice_id}: no tissue, no crops", (6, 28),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 1, cv2.LINE_AA)  # fmt: skip
        return sheet
    rows = -(-len(crops) // cols)
    cell_h = CROP_PX + CAPTION_PX
    sheet = np.full((rows * cell_h, cols * CROP_PX, 3), 255, np.uint8)
    for k, (caption, img) in enumerate(crops):
        r, c = divmod(k, cols)
        y, x = r * cell_h, c * CROP_PX
        cv2.putText(sheet, caption, (x + 6, y + 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1,
                    cv2.LINE_AA)  # fmt: skip
        sheet[y + CAPTION_PX : y + CAPTION_PX + img.shape[0], x : x + img.shape[1]] = img
    return sheet


# --- stage ----------------------------------------------------------------------------------


def _signature(cfg: ProjectConfig) -> dict[str, Any]:
    # the flag thresholds are left out: they are re-evaluated on every run
    return {
        "version": VERSION,
        "qc_fold_od_ratio": cfg.qc_fold_od_ratio,
        "qc_fold_min_area_mm2": cfg.qc_fold_min_area_mm2,
        "crop_px": CROP_PX,
        "colours": [t.colour for t in cfg.cell_types],
    }


def _inputs(ref: SliceRef, work: WorkDir) -> dict[str, Any]:
    return {
        "source": fingerprint(ref.path),
        "mask_sha256": hashlib.sha256(work.mask(ref.id).read_bytes()).hexdigest(),
        "cells": fingerprint(work.cell_profiles(ref.id)),
        "cell_meta": fingerprint(work.cell_meta(ref.id)),
    }


def qc_one(
    ref: SliceRef,
    work: WorkDir,
    cfg: ProjectConfig,
    reader_factory: ReaderFactory = SlideReader.open,
) -> dict[str, Any]:
    """Numbers and review crops of one slice; writes `qc/cells_{id}.jpg`."""
    profiles, meta = load_profiles(work, ref.id)
    mask = cv2.imread(str(work.mask(ref.id)), cv2.IMREAD_GRAYSCALE)
    if mask is None:
        raise OSError(f"{work.mask(ref.id)} is not a readable PNG: run preprocess again")
    reg_ds = float(json.loads(work.reg_meta(ref.id).read_text())["downsample"])
    det_ds = float(meta["downsample"])
    mpp = float(meta["mpp_um"][0])
    colours = [_rgb(t.colour) for t in cfg.cell_types]
    reader = reader_factory(ref.path)
    try:
        lv = reader.level_for_downsample(reg_ds)
        info = reader.levels[lv]
        rgb = reader.read_region(lv, 0, 0, info.width, info.height)
        folds = find_folds(rgb, mask, reg_ds * mpp, cfg.qc_fold_od_ratio, cfg.qc_fold_min_area_mm2)
        del rgb
        stats = slice_stats(profiles, meta, mask, folds, reg_ds * mpp)
        level = reader.level_for_downsample(det_ds)
        crop_um = CROP_PX * det_ds * mpp
        crops = []
        seen: dict[str, int] = {}
        for kind, x, y in crop_centres(profiles, mask, folds, reg_ds * mpp, crop_um, ref.id):
            seen[kind] = seen.get(kind, 0) + 1
            img = render_crop(reader, level, det_ds * mpp, (x, y), profiles, colours, folds,
                              reg_ds * mpp)  # fmt: skip
            crops.append((f"{kind} {seen[kind]}: x {x:.0f} um, y {y:.0f} um", img))
    finally:
        reader.close()
    sheet = review_sheet(crops, ref.id)
    ok, jpg = cv2.imencode(".jpg", cv2.cvtColor(sheet, cv2.COLOR_RGB2BGR),
                           [cv2.IMWRITE_JPEG_QUALITY, 85])  # fmt: skip
    if not ok:
        raise OSError(f"could not encode the review crops of slice {ref.id}")
    write_atomic(work.qc_cells_crops(ref.id), jpg.tobytes())
    return {"slice_id": ref.id, **stats, "crops": work.qc_cells_crops(ref.id).name}


def run_qc_cells(
    catalog: Catalog,
    work: WorkDir,
    cfg: ProjectConfig,
    manifest: Manifest,
    reader_factory: ReaderFactory = SlideReader.open,
    overwrite: bool = False,
) -> StageResult:
    """Detection QC of the slices of `catalog`: numbers and review crops per slice (redone only
    when the slide, the mask or the cell file changed), then `qc/cells_report.json` for the
    range with the current thresholds. FileNotFoundError naming slices without a cell file or a
    mask; StageExistsError if earlier results used other fold or crop settings, unless
    `overwrite`."""
    no_cells = [s.id for s in catalog.slices if not work.cell_meta(s.id).exists()]
    if no_cells:
        raise FileNotFoundError(f"slices {no_cells} have no cell file: run `brainrec detect`")
    no_mask = [s.id for s in catalog.slices if not work.mask(s.id).exists()]
    if no_mask:
        raise FileNotFoundError(f"slices {no_mask} have no tissue mask: run `brainrec preprocess`")
    signature = _signature(cfg)
    state = manifest.get(STAGE)
    if state is not None and state["signature"] != signature:
        if not overwrite:
            changed = sorted(k for k in signature if state["signature"].get(k) != signature[k])
            raise StageExistsError(
                f"{STAGE} results were made with other settings ({', '.join(changed)}); "
                "use overwrite to redo them"
            )
        state = None
    slices: dict[str, dict] = dict(state["slices"]) if state else {}
    result = StageResult()
    rows = []
    for ref in catalog.slices:
        inputs = _inputs(ref, work)
        done = slices.get(str(ref.id))
        if done is not None and done["inputs"] == inputs and work.qc_cells_crops(ref.id).exists():
            result.skipped.append(ref.id)
            rows.append(done["stats"])
            continue
        stats = qc_one(ref, work, cfg, reader_factory)
        slices[str(ref.id)] = {"inputs": inputs, "stats": stats}
        manifest.set(STAGE, {"signature": signature, "slices": slices})
        result.done.append(ref.id)
        rows.append(stats)
        log.info("qc-cells: slice %d, %d profiles, %.0f per mm², folds %.2f%%", ref.id,
                 stats["n_profiles"], stats["density_mm2"], 100 * stats["fold_share"])  # fmt: skip
    report = build_cells_report(rows, cfg)
    write_atomic(work.qc_cells_report, json.dumps(report, indent=2).encode())
    return result
