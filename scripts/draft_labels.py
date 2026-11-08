"""M7-19: draft labels for the 20 label crops, for an expert to correct in QuPath.

NOT ground truth: positions come from the detectors (InstanSeg and StarDist, level 0) and classes
from the appearance groups of M7-18 by fixed rules, so scores against these drafts are circular.
They are written to <work_dir>/labels/draft/, never to labels/annotations/ (expert labels).

Per crop: nuclei of both detectors (StarDist adds those InstanSeg missed); features as in M7-18;
nearest reviewed appearance group; class by the rules in `CLASS_RULES`. Writes per slide
<slide file>.geojson (crop squares plus one point set per class, "source": "draft"), counts.json
and gallery_<class>.jpg. Refuses to replace existing drafts unless --overwrite.

    .venv/bin/python scripts/draft_labels.py --config config/project.yaml
"""

import argparse
import csv
import json
import logging
import sys
from pathlib import Path

import cv2
import numpy as np
from scipy import ndimage as ndi
from skimage import measure

sys.path.insert(0, str(Path(__file__).parent))
from appearance_groups import FEATURES, TILE_UM, features, read_um
from spike_cells import det_instanseg, det_stardist

from brainrec.catalog import Catalog, ProjectConfig
from brainrec.detect.labels import Labels, load_crops, load_labels, points_geojson
from brainrec.slides import SlideReader

log = logging.getLogger("draft_labels")
MARGIN_UM = 15.0  # context around each crop for the features near its edge
CLASS_RULES = """\
group 0 (small, darkest, round)            -> Oligodendrocyte-like
group 1 (medium round, medium dark)        -> Oligodendrocyte-like if darker than the group's median, else Astrocyte-like
group 2, 4 (elongated)                     -> Vessel-wall if elongated (>= 1.8) and a lumen of >= 15 µm² lies within 2 µm, else Microglia-like
group 3 (round, dark, halo)                -> Oligodendrocyte-like
group 5 (nucleus in a stained cell body)   -> Neuron
group 6 (pale oval)                        -> Astrocyte-like
group 7 (large, textured)                  -> Neuron, unless both small (< 9 µm) and without a clear nucleolus -> Astrocyte-like
any nucleus of 10.5 µm or more             -> Neuron
"Other" is never assigned: it is the expert's choice"""


def merged_labels(rgb: np.ndarray, um_px: float) -> np.ndarray:
    """InstanSeg nuclei plus StarDist nuclei that InstanSeg missed (no centre within 3 µm and
    less than 30% overlap)."""
    a = det_instanseg(rgb, um_px).astype(np.int32)
    b = det_stardist(rgb, um_px)
    centres_a = np.array([p.centroid for p in measure.regionprops(a)]).reshape(-1, 2)
    nxt = int(a.max()) + 1
    out = a.copy()
    for p in measure.regionprops(b):
        if len(centres_a) and np.min(np.hypot(*(centres_a - p.centroid).T)) * um_px < 3.0:
            continue
        own = b == p.label
        if (a[own] > 0).mean() >= 0.3:
            continue
        out[own & (out == 0)] = nxt
        nxt += 1
    return out


def touches_lumen(rgb: np.ndarray, labels: np.ndarray, cy: float, cx: float, um_px: float) -> bool:
    """A bright gap (lumen, gray > 0.85 x 255, at least 15 µm², larger than the small holes of the
    tissue) within 2 µm of the nucleus."""
    lab = labels[round(cy), round(cx)]
    if lab == 0:
        return False
    own = labels == lab
    ring = ndi.binary_dilation(own, iterations=max(1, round(2 / um_px))) & ~own
    bright = measure.label(rgb.mean(axis=2) > 0.85 * 255)
    big = [p.label for p in measure.regionprops(bright) if p.area * um_px**2 >= 15]
    return bool(np.isin(bright[ring], big).any())


def reference(cells_csv: Path):
    """Standardisation and group centroids of the reviewed M7-18 cells."""
    with open(cells_csv) as f:
        rows = list(csv.DictReader(f))
    x = np.array([[float(r[k]) for k in FEATURES] for r in rows])
    g = np.array([int(r["group"]) for r in rows])
    t = transform(x)
    med = np.median(t, axis=0)
    iqr = np.subtract(*np.percentile(t, [75, 25], axis=0))
    iqr = np.where(iqr > 0, iqr, 1.0)
    z = (t - med) / iqr
    cents = np.array([z[g == k].mean(axis=0) for k in range(g.max() + 1)])
    return {
        "med": med, "iqr": iqr, "centroids": cents,
        "g1_darkness": float(np.median(x[g == 1, FEATURES.index("darkness")])),
        "g7_nucleolus_q25": float(np.percentile(x[g == 7, FEATURES.index("nucleolus")], 25)),
    }  # fmt: skip


def transform(x: np.ndarray) -> np.ndarray:
    t = x.copy()
    for j, name in enumerate(FEATURES):
        if name in ("diameter_um", "elongation"):
            t[:, j] = np.log(t[:, j])
        if name == "vessel_um":
            t[:, j] = np.log1p(t[:, j])
    return t


def classify(row: dict, group: int, ref: dict, lumen: bool) -> str:
    if row["diameter_um"] >= 10.5:
        return "Neuron"
    if group in (0, 3):
        return "Oligodendrocyte-like"
    if group == 1:
        return "Oligodendrocyte-like" if row["darkness"] >= ref["g1_darkness"] else "Astrocyte-like"
    if group in (2, 4):
        return "Vessel-wall" if lumen and row["elongation"] >= 1.8 else "Microglia-like"
    if group == 5:
        return "Neuron"
    if group == 6:
        return "Astrocyte-like"
    small_plain = row["diameter_um"] < 9.0 and row["nucleolus"] < ref["g7_nucleolus_q25"]
    return "Astrocyte-like" if small_plain else "Neuron"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--overwrite", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    cfg = ProjectConfig.load(args.config)
    root = Path(cfg.work_dir)
    out = root / "labels" / "draft"
    if out.exists() and any(p.suffix == ".geojson" for p in out.iterdir()) and not args.overwrite:
        sys.exit(f"error: drafts exist in {out}; use --overwrite")
    out.mkdir(parents=True, exist_ok=True)
    crops, classes, mpp = load_crops(root / "labels" / "crops.json")
    ref = reference(root / "spike" / "M7-18" / "cells.csv")
    paths = {s.id: s.path for s in Catalog.scan(cfg.data_dir).slices}

    rows_all: list[dict] = []
    tiles: dict[str, list[np.ndarray]] = {c: [] for c in classes}
    for crop in crops:
        r = SlideReader.open(paths[crop.slice_id])
        try:
            um_px = r.mpp[0]
            x0, y0 = crop.x0_um - MARGIN_UM, crop.y0_um - MARGIN_UM
            rgb = read_um(r, x0, y0, crop.size_um + 2 * MARGIN_UM)
        finally:
            r.close()
        labels = merged_labels(rgb, um_px)
        rows = features(rgb, labels, um_px)
        x = np.array([[row[k] for k in FEATURES] for row in rows]).reshape(-1, len(FEATURES))
        z = (transform(x) - ref["med"]) / ref["iqr"]
        groups = np.argmin(((z[:, None, :] - ref["centroids"][None]) ** 2).sum(axis=2), axis=1)
        kept = 0
        for row, g in zip(rows, groups, strict=True):
            row["x_um"] = x0 + (row["cx"] + 0.5) * um_px
            row["y_um"] = y0 + (row["cy"] + 0.5) * um_px
            if not crop.inside(np.array(row["x_um"]), np.array(row["y_um"])):
                continue
            lumen = (
                touches_lumen(rgb, labels, row["cy"], row["cx"], um_px) if g in (2, 4) else False
            )
            row.update({"crop": crop.id, "slice": crop.slice_id, "group": int(g),
                        "cls": classify(row, int(g), ref, lumen)})  # fmt: skip
            rows_all.append(row)
            kept += 1
            if len(tiles[row["cls"]]) < 400:
                half = round(TILE_UM / 2 / um_px)
                pad = cv2.copyMakeBorder(rgb, half, half, half, half, cv2.BORDER_CONSTANT,
                                         value=(255, 255, 255))  # fmt: skip
                cy, cx = round(row["cy"]) + half, round(row["cx"]) + half
                t = cv2.cvtColor(
                    pad[cy - half : cy + half, cx - half : cx + half], cv2.COLOR_RGB2BGR
                )
                cv2.circle(t, (half, half), 3, (0, 220, 255), -1)
                tiles[row["cls"]].append(t)
        log.info("crop %02d (slice %d): %d draft labels", crop.id, crop.slice_id, kept)

    counts = {c: sum(r["cls"] == c for r in rows_all) for c in classes}
    for source in sorted({c.source for c in crops}):
        sid = next(c.slice_id for c in crops if c.source == source)
        mine = [r for r in rows_all if r["slice"] == sid]
        lab = Labels(
            np.full(len(mine), sid), np.array([r["crop"] for r in mine]),
            np.array([r["x_um"] for r in mine]), np.array([r["y_um"] for r in mine]),
            np.array([r["cls"] for r in mine], dtype=object), np.full(len(mine), np.nan),
        )  # fmt: skip
        (out / f"{source}.geojson").write_text(json.dumps(points_geojson(lab, crops, mpp, "draft")))
    check = load_labels(out, crops, classes, mpp)  # the expert-label reader accepts the drafts
    assert len(check) == len(rows_all) and (check.crop >= 0).all()
    rng = np.random.default_rng(0)
    for c, ts in tiles.items():
        if not ts:
            continue
        pick = [ts[i] for i in rng.choice(len(ts), min(36, len(ts)), replace=False)]
        pick = [cv2.resize(t, (180, 180), interpolation=cv2.INTER_CUBIC) for t in pick]
        pick += [np.full((180, 180, 3), 255, np.uint8)] * (-len(pick) % 9)
        grid = np.vstack([np.hstack(pick[i : i + 9]) for i in range(0, len(pick), 9)])
        head = np.full((44, grid.shape[1], 3), 40, np.uint8)
        cv2.putText(head, f"DRAFT {c}: {counts[c]} cells (random examples, dot = nucleus)", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2, cv2.LINE_AA)  # fmt: skip
        cv2.imwrite(
            str(out / f"gallery_{c}.jpg"), np.vstack([head, grid]), [cv2.IMWRITE_JPEG_QUALITY, 90]
        )
    (out / "counts.json").write_text(json.dumps(
        {"source": "draft (automatic, M7-19): NOT ground truth", "rules": CLASS_RULES.splitlines(),
         "total": len(rows_all), "per_class": counts,
         "per_crop": {str(c.id): sum(r["crop"] == c.id for r in rows_all) for c in crops}}, indent=1))  # fmt: skip
    log.info("%d draft labels: %s", len(rows_all), counts)


if __name__ == "__main__":
    main()
