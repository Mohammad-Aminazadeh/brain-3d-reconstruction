"""M7-18: let the cells' measured looks propose the classes (throwaway; decision 0006).

Nuclei are detected (InstanSeg, level 0) on the 20 label crops of M7-02 and on 20 more crops
across the block. Each nucleus gets 9 appearance features; the cells are grouped by k-means on
the standardised features, the number of groups chosen by stability (bootstrap agreement), and
a gallery of example cells is drawn per group.

Sources are only read. Writes to <work_dir>/spike/M7-18/: cells.csv, groups.json,
group_<g>.jpg, overview.jpg.

    .venv/bin/python scripts/appearance_groups.py --config config/project.yaml
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
from spike_cells import crop_centres, det_instanseg, hematoxylin, tissue_mask

from brainrec.catalog import Catalog, ProjectConfig
from brainrec.detect.labels import load_crops
from brainrec.slides import SlideReader
from brainrec.store import WorkDir

log = logging.getLogger("appearance_groups")
EXTRA_SLICES = [8, 22, 37, 52, 67, 82, 97, 112, 127, 135]
FEATURES = ["diameter_um", "elongation", "solidity", "darkness", "texture", "nucleolus",
            "cell_body", "halo", "vessel_um"]  # fmt: skip
TILE_UM = 20.0


def read_um(reader: SlideReader, x0_um: float, y0_um: float, size_um: float) -> np.ndarray:
    d = reader.mpp[0]
    n = round(size_um / d)
    return reader.read_region(
        reader.level_for_downsample(1), round(x0_um / d), round(y0_um / d), n, n
    )


def features(rgb: np.ndarray, labels: np.ndarray, um_px: float) -> list[dict]:
    """Appearance features of every nucleus at least 8 µm² large and 3 µm from the crop edge."""
    h = hematoxylin(rgb)
    gray = rgb.mean(axis=2)
    nuclei = labels > 0
    near_nuclei = ndi.binary_dilation(nuclei, iterations=max(1, round(3 / um_px)))
    h_bg = float(np.median(h[~near_nuclei])) if (~near_nuclei).any() else float(np.median(h))
    g_bg = float(np.median(gray[~near_nuclei])) if (~near_nuclei).any() else float(np.median(gray))
    lumen = gray > 0.93 * 255
    lumen = measure.label(lumen)
    keep = [p.label for p in measure.regionprops(lumen) if p.area * um_px**2 >= 20]
    lumen_mask = np.isin(lumen, keep)
    dist_lumen = ndi.distance_transform_edt(~lumen_mask) * um_px if lumen_mask.any() else None
    hs = cv2.GaussianBlur(h, (0, 0), 0.35 / um_px)
    rows = []
    edge = 3 / um_px
    for p in measure.regionprops(labels):
        if p.area * um_px**2 < 8:
            continue
        cy, cx = p.centroid
        if min(cy, cx, rgb.shape[0] - cy, rgb.shape[1] - cx) < edge:
            continue
        own = labels == p.label
        inner = ndi.binary_erosion(own, iterations=1)
        h_in = h[own]
        ring_body = (
            ndi.binary_dilation(own, iterations=round(3 / um_px))
            & ~ndi.binary_dilation(own, iterations=max(1, round(0.5 / um_px)))
            & ~nuclei
        )
        ring_halo = ndi.binary_dilation(own, iterations=max(1, round(1 / um_px))) & ~nuclei
        rows.append({
            "cy": cy, "cx": cx,
            "diameter_um": 2 * np.sqrt(p.area / np.pi) * um_px,
            "elongation": p.major_axis_length / max(p.minor_axis_length, 1e-6),
            "solidity": p.solidity,
            "darkness": float(h_in.mean() - h_bg),
            "texture": float(h_in.std()),
            "nucleolus": float(hs[inner].max() - np.median(h_in)) if inner.any() else 0.0,
            "cell_body": float(h[ring_body].mean() - h_bg) if ring_body.any() else 0.0,
            "halo": float(gray[ring_halo].mean() - g_bg) if ring_halo.any() else 0.0,
            "vessel_um": float(min(dist_lumen[int(cy), int(cx)], 30.0)) if dist_lumen is not None else 30.0,
            "contour": [c.tolist() for c in measure.find_contours(own.astype(float), 0.5)[:1]],
        })  # fmt: skip
    return rows


def standardise(x: np.ndarray) -> np.ndarray:
    x = x.copy()
    for j, name in enumerate(FEATURES):
        if name in ("diameter_um", "elongation"):
            x[:, j] = np.log(x[:, j])
        if name == "vessel_um":
            x[:, j] = np.log1p(x[:, j])
    med = np.median(x, axis=0)
    iqr = np.subtract(*np.percentile(x, [75, 25], axis=0))
    return (x - med) / np.where(iqr > 0, iqr, 1.0)


def choose_k(z: np.ndarray, ks: range, seed: int) -> tuple[dict, dict]:
    from sklearn.cluster import KMeans
    from sklearn.metrics import adjusted_rand_score, silhouette_score

    rng = np.random.default_rng(seed)
    stats, fits = {}, {}
    sample = rng.choice(len(z), min(4000, len(z)), replace=False)
    for k in ks:
        full = KMeans(k, n_init=10, random_state=seed).fit(z)
        aris = []
        for b in range(15):
            sub = rng.choice(len(z), int(0.8 * len(z)), replace=False)
            part = KMeans(k, n_init=5, random_state=seed + b + 1).fit(z[sub])
            aris.append(adjusted_rand_score(full.labels_[sub], part.labels_))
        stats[k] = {
            "stability_ari": float(np.mean(aris)),
            "silhouette": float(silhouette_score(z[sample], full.labels_[sample])),
        }
        fits[k] = full
        log.info("k=%d: stability %.3f, silhouette %.3f", k, *stats[k].values())
    return stats, fits


def gallery(cells: list[dict], group: np.ndarray, g: int, crops_rgb: dict, um_px: float,
            rng: np.random.Generator, title: str) -> np.ndarray:  # fmt: skip
    idx = np.nonzero(group == g)[0]
    pick = rng.choice(idx, min(36, len(idx)), replace=False)
    half = round(TILE_UM / 2 / um_px)
    tiles = []
    for i in pick:
        c = cells[i]
        img = crops_rgb[c["crop_key"]]
        pad = cv2.copyMakeBorder(
            img, half, half, half, half, cv2.BORDER_CONSTANT, value=(255, 255, 255)
        )
        cy, cx = round(c["cy"]) + half, round(c["cx"]) + half
        t = cv2.cvtColor(
            pad[cy - half : cy + half, cx - half : cx + half], cv2.COLOR_RGB2BGR
        ).copy()
        for contour in c["contour"]:
            pts = np.array(contour)[:, ::-1] - [c["cx"] - half, c["cy"] - half]
            cv2.polylines(t, [np.round(pts).astype(np.int32)], True, (0, 220, 255), 1, cv2.LINE_AA)
        tiles.append(cv2.resize(t, (180, 180), interpolation=cv2.INTER_CUBIC))
    tiles += [np.full((180, 180, 3), 255, np.uint8)] * (-len(tiles) % 9)
    grid = np.vstack([np.hstack(tiles[i : i + 9]) for i in range(0, len(tiles), 9)])
    head = np.full((70, grid.shape[1], 3), 40, np.uint8)
    for k, line in enumerate(title.split("\n")):
        cv2.putText(head, line, (10, 28 + 32 * k), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (255, 255, 255),
                    2, cv2.LINE_AA)  # fmt: skip
    return np.vstack([head, grid])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--k", type=int, default=0, help="number of groups (0 = by stability)")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    cfg = ProjectConfig.load(args.config)
    work = WorkDir(cfg.work_dir)
    out = Path(cfg.work_dir) / "spike" / "M7-18"
    out.mkdir(parents=True, exist_ok=True)
    paths = {s.id: s.path for s in Catalog.scan(cfg.data_dir).slices}

    crops, _, _ = load_crops(Path(cfg.work_dir) / "labels" / "crops.json")
    regions = [(c.slice_id, c.x0_um, c.y0_um, c.size_um, f"label crop {c.id:02d}") for c in crops]
    for sid in EXTRA_SLICES:
        r = SlideReader.open(paths[sid])
        try:
            mask, ds = tissue_mask(work, r, sid, cfg)
            got = 0
            for centre in crop_centres(mask, ds, r.mpp[0], 12, seed=500 + sid):
                if got == 2:
                    break
                x0, y0 = centre[0] - 125.0, centre[1] - 125.0
                rgb = read_um(r, x0, y0, 250.0)
                if cv2.Laplacian(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY), cv2.CV_64F).var() < 100:
                    continue
                regions.append((sid, x0, y0, 250.0, f"extra s{sid}"))
                got += 1
        finally:
            r.close()

    cells: list[dict] = []
    crops_rgb: dict[int, np.ndarray] = {}
    um_px = 0.0
    for key, (sid, x0, y0, size, name) in enumerate(regions):
        r = SlideReader.open(paths[sid])
        try:
            um_px = r.mpp[0]
            rgb = read_um(r, x0, y0, size)
        finally:
            r.close()
        rows = features(rgb, det_instanseg(rgb, um_px), um_px)
        for row in rows:
            row.update({"crop_key": key, "slice": sid, "region": name,
                        "x_um": x0 + (row["cx"] + 0.5) * um_px, "y_um": y0 + (row["cy"] + 0.5) * um_px})  # fmt: skip
        cells += rows
        crops_rgb[key] = rgb
        log.info("%s (slice %d): %d nuclei", name, sid, len(rows))

    x = np.array([[c[f] for f in FEATURES] for c in cells], float)
    z = standardise(x)
    stats, fits = choose_k(z, range(3, 11), args.seed)
    if args.k:
        k = args.k
    else:  # the most groups whose grouping is still stable (bootstrap ARI >= 0.8), at least 3
        stable = [kk for kk, s in stats.items() if s["stability_ari"] >= 0.8]
        k = max(stable) if stable else max(stats, key=lambda kk: stats[kk]["stability_ari"])
    group = fits[k].labels_
    # order groups by median diameter, so group numbers read from small to large nuclei
    order = np.argsort([np.median(x[group == g, 0]) for g in range(k)])
    group = np.argsort(order)[group]

    rng = np.random.default_rng(args.seed)
    summary = []
    pages = []
    for g in range(k):
        sel = group == g
        med = {f: float(np.median(x[sel, j])) for j, f in enumerate(FEATURES)}
        summary.append({"group": g, "n": int(sel.sum()), "share": float(sel.mean()), "median": med,
                        "slices": sorted({int(c["slice"]) for c, s in zip(cells, sel, strict=True) if s})})  # fmt: skip
        title = (f"group {g}: {sel.sum()} cells ({100 * sel.mean():.0f}%)  diameter {med['diameter_um']:.1f} um, "
                 f"elongation {med['elongation']:.2f}\n"
                 f"darkness {med['darkness']:.3f}, texture {med['texture']:.3f}, nucleolus {med['nucleolus']:.3f}, "
                 f"body {med['cell_body']:.3f}, halo {med['halo']:.1f}, vessel {med['vessel_um']:.0f} um")  # fmt: skip
        page = gallery(cells, group, g, crops_rgb, um_px, rng, title)
        cv2.imwrite(str(out / f"group_{g}.jpg"), page, [cv2.IMWRITE_JPEG_QUALITY, 90])
        pages.append(page)
        log.info(title.replace("\n", " | "))
    width = max(p.shape[1] for p in pages)
    overview = np.vstack([cv2.copyMakeBorder(p, 0, 10, 0, width - p.shape[1], cv2.BORDER_CONSTANT,
                                             value=(0, 0, 0)) for p in pages])  # fmt: skip
    small = cv2.resize(overview, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
    cv2.imwrite(str(out / "overview.jpg"), small, [cv2.IMWRITE_JPEG_QUALITY, 85])
    (out / "groups.json").write_text(json.dumps(
        {"features": FEATURES, "n_cells": len(cells), "n_regions": len(regions), "k": k,
         "k_stats": {str(kk): s for kk, s in stats.items()}, "groups": summary}, indent=1))  # fmt: skip
    with open(out / "cells.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["slice", "region", "x_um", "y_um", *FEATURES, "group"])
        for c, g in zip(cells, group, strict=True):
            w.writerow([c["slice"], c["region"], f"{c['x_um']:.2f}", f"{c['y_um']:.2f}",
                        *(f"{c[f]:.4f}" for f in FEATURES), int(g)])  # fmt: skip
    log.info("%d cells from %d regions in %d groups -> %s", len(cells), len(regions), k, out)


if __name__ == "__main__":
    main()
