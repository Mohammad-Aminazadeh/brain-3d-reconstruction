"""M7-06: reference of the interim classifier rules-v1, from 2x measurements.

The M7-19 rules used the appearance groups of M7-18, measured at level 0. The detection
(M7-05) measures at 2x, so the reference is rebuilt from the same cells measured as the detection
measures them: the 40 crop regions of M7-18 are detected with `detect_slice` (InstanSeg, 2x),
each M7-18 cell is matched to a 2x nucleus within 1.5 µm, and the standardisation, group centres
and thresholds are computed from the 2x features with the M7-18 group of each cell.

Sources are only read. Writes the reference to src/brainrec/detect/rules_v1.json (a small
model file, part of the code like a calibrated constant) and a check to
<work_dir>/spike/M7-06/rules_v1_check.json:
- how many cells matched;
- whether the 2x reference puts cells in their M7-18 group;
- agreement of the rules-v1 classes with the M7-19 draft classes (both automatic: not accuracy).
Refuses to replace an existing reference unless --overwrite.

    .venv/bin/python scripts/rules_v1_reference.py --config config/project.yaml
"""

import argparse
import csv
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.cluster.hierarchy import fcluster, linkage

sys.path.insert(0, str(Path(__file__).parent))
from spike_cells import tissue_mask

from brainrec.catalog import Catalog, ProjectConfig
from brainrec.detect.classify import RULES_SCHEMA, RULES_V1_PATH, RulesV1, class_probabilities
from brainrec.detect.classify import transform as feature_transform
from brainrec.detect.evaluate import match
from brainrec.detect.labels import load_crops, load_labels
from brainrec.detect.nuclei import detect_slice, load_detector
from brainrec.slides import SlideReader
from brainrec.store import WorkDir
from brainrec.store.cells import FEATURES

log = logging.getLogger("rules_v1_reference")
MATCH_UM = 1.5
BOX_MARGIN_UM = 10.0
CROP_GAP_UM = 100.0  # cells of one crop are closer than this to a neighbour
TEMPERATURE = 1.0  # soft group membership (Proposed; not calibrated against labels)


def regions(rows: list[dict]) -> dict[int, list[tuple[float, float, float, float]]]:
    """Per slice, the bounding boxes (µm) of the M7-18 crops. A region name can hold several
    crops (the extra crops of a slice share one name), so the cells of a region are split into
    clusters: cells closer than CROP_GAP_UM belong to the same crop."""
    by: dict[tuple[int, str], list[tuple[float, float]]] = defaultdict(list)
    for r in rows:
        by[(int(r["slice"]), r["region"])].append((float(r["x_um"]), float(r["y_um"])))
    out: dict[int, list] = defaultdict(list)
    for (sid, _), pts in by.items():
        a = np.array(pts)
        clusters = fcluster(linkage(a, "single"), CROP_GAP_UM, "distance") if len(a) > 1 else [1]
        for c in np.unique(clusters):
            lo = a[clusters == c].min(axis=0) - BOX_MARGIN_UM
            hi = a[clusters == c].max(axis=0) + BOX_MARGIN_UM
            out[sid].append((lo[0], lo[1], hi[0], hi[1]))
    return out


def reference(x: np.ndarray, groups: np.ndarray) -> dict:
    t = feature_transform(x)
    med = np.median(t, axis=0)
    iqr = np.subtract(*np.percentile(t, [75, 25], axis=0))
    iqr = np.where(iqr > 0, iqr, 1.0)
    z = (t - med) / iqr
    k = int(groups.max()) + 1
    return {
        "schema": RULES_SCHEMA, "name": "rules-v1", "features": list(FEATURES),
        "median": med.tolist(), "iqr": iqr.tolist(),
        "centroids": [z[groups == g].mean(axis=0).tolist() for g in range(k)],
        "g1_darkness": float(np.median(x[groups == 1, FEATURES.index("darkness")])),
        "g7_nucleolus_q25": float(np.percentile(x[groups == 7, FEATURES.index("nucleolus")], 25)),
        "temperature": TEMPERATURE,
        "source": "M7-18 cells measured again at 2x by detect_slice (InstanSeg), M7-06",
        "n_cells": len(x),
    }  # fmt: skip


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=RULES_V1_PATH)
    ap.add_argument("--overwrite", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if args.out.exists() and not args.overwrite:
        sys.exit(f"error: {args.out} exists; use --overwrite")
    cfg = ProjectConfig.load(args.config)
    root = Path(cfg.work_dir)
    work = WorkDir(root)
    with open(root / "spike" / "M7-18" / "cells.csv") as f:
        rows = list(csv.DictReader(f))
    paths = {s.id: s.path for s in Catalog.scan(cfg.data_dir).slices}
    detector = load_detector("instanseg")

    feats, groups, det_all = [], [], {}
    for sid, boxes in sorted(regions(rows).items()):
        r = SlideReader.open(paths[sid])
        try:
            mask, ds = tissue_mask(work, r, sid, cfg)
            px = ds * r.mpp[0]
            sub = np.zeros_like(mask)
            for x0, y0, x1, y1 in boxes:
                sl = (slice(int(y0 // px), int(np.ceil(y1 / px))),
                      slice(int(x0 // px), int(np.ceil(x1 / px))))  # fmt: skip
                sub[sl] = mask[sl]
            nu = detect_slice(r, sub, ds, cfg, detector)
        finally:
            r.close()
        det_all[sid] = nu
        mine = [row for row in rows if int(row["slice"]) == sid]
        xy = np.array([[float(row["x_um"]), float(row["y_um"])] for row in mine])
        pairs, _, _ = match(xy, np.c_[nu.x_um, nu.y_um], nu.r_um, tol_um=MATCH_UM, rel=0.0)
        for a, b in pairs:
            feats.append(nu.features[b])
            groups.append(int(mine[a]["group"]))
        log.info("slice %d: %d of %d M7-18 cells matched", sid, len(pairs), len(mine))

    x, g = np.array(feats, float), np.array(groups)
    ref = reference(x, g)
    args.out.write_text(json.dumps(ref, indent=1) + "\n")
    model = RulesV1.load(args.out)

    z = (feature_transform(x) - model.median) / model.iqr
    nearest = np.argmin(((z[:, None] - model.centroids[None]) ** 2).sum(axis=2), axis=1)
    check: dict = {
        "matched": len(x), "m7_18_cells": len(rows),
        "per_group": {str(k): int((g == k).sum()) for k in range(g.max() + 1)},
        "same_group_by_nearest_centre": round(float((nearest == g).mean()), 3),
    }  # fmt: skip

    # agreement with the M7-19 drafts on the label crops (both automatic: NOT accuracy)
    crops, classes, mpp = load_crops(root / "labels" / "crops.json")
    drafts = load_labels(root / "labels" / "draft", crops, classes, mpp)
    names = tuple(t.name for t in cfg.cell_types)
    agree, conf_tab = [], defaultdict(int)
    for sid, nu in det_all.items():
        sel = drafts.slice_id == sid
        if not sel.any():
            continue
        prob = class_probabilities(nu.features, model, names)
        pred = np.array(names)[prob.argmax(axis=1)]
        dxy = np.c_[drafts.x_um[sel], drafts.y_um[sel]]
        pairs, _, _ = match(dxy, np.c_[nu.x_um, nu.y_um], nu.r_um, tol_um=MATCH_UM, rel=0.0)
        for a, b in pairs:
            d_cls, p_cls = drafts.cls[sel][a], pred[b]
            agree.append(d_cls == p_cls)
            conf_tab[f"{d_cls} -> {p_cls}"] += 1
    check["drafts_matched"] = len(agree)
    check["same_class_as_draft"] = round(float(np.mean(agree)), 3) if agree else None
    check["draft_to_rules_v1"] = dict(sorted(conf_tab.items(), key=lambda kv: -kv[1]))
    all_prob = np.vstack(
        [class_probabilities(nu.features, model, names) for nu in det_all.values()]
    )
    top = all_prob.max(axis=1)
    check["confidence_quartiles"] = np.percentile(top, [25, 50, 75]).round(3).tolist()
    check["class_shares"] = {
        n: round(float((all_prob.argmax(axis=1) == i).mean()), 3) for i, n in enumerate(names)
    }
    out = root / "spike" / "M7-06"
    out.mkdir(parents=True, exist_ok=True)
    (out / "rules_v1_check.json").write_text(json.dumps(check, indent=1))
    print(json.dumps({k: v for k, v in check.items() if k != "draft_to_rules_v1"}, indent=1))
    print("top draft -> rules-v1 pairs:", list(check["draft_to_rules_v1"].items())[:12])


if __name__ == "__main__":
    main()
