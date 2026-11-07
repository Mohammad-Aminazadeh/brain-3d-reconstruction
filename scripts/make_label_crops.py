"""Choose the crops for labelling cells (M7-02) and write them for QuPath.

For each slice: tissue mask at 32x (the preprocessed one, or computed in memory), random
candidate centres at least `--depth` µm inside the tissue, each checked for focus at level 0;
`--per-slice` sharp crops at least 1 mm apart are kept. Then a fixed train/test split.

Writes to <work_dir>/labels/ (sources are only read):
    crops.json                     crops, classes, pixel size (brainrec.labelcrops/1)
    qupath/<slide file>.geojson    the crops as locked rectangles, to drop onto the slide in QuPath
    crops_overview.jpg             all crops side by side (2x), numbered
    annotations/                   empty; the labeller saves the QuPath exports here

Refuses to replace an existing crops.json (labels may refer to it) unless --overwrite.

    .venv/bin/python scripts/make_label_crops.py --config config/project.yaml
"""

import argparse
import json
import logging
import sys
from pathlib import Path

import cv2
import numpy as np

from brainrec.catalog import Catalog, ProjectConfig
from brainrec.detect.labels import (
    CLASSES,
    Crop,
    assign_split,
    candidate_centres,
    crops_geojson,
    far_enough,
    save_crops,
)
from brainrec.preprocess.mask import make_mask
from brainrec.slides import SlideReader
from brainrec.store import WorkDir

log = logging.getLogger("make_label_crops")
SHARPNESS_MIN = 100.0  # variance of the Laplacian at level 0; blurred crops scored 11 to 56 (M7-01)


def read_um(reader: SlideReader, x0_um: float, y0_um: float, size_um: float, downsample: int):
    lvl = reader.level_for_downsample(downsample)
    d = downsample * reader.mpp[0]
    n = round(size_um / d)
    return reader.read_region(lvl, round(x0_um / d), round(y0_um / d), n, n)


def slice_mask(work: WorkDir, reader: SlideReader, slice_id: int, cfg: ProjectConfig):
    if work.mask(slice_id).exists():
        return cv2.imread(str(work.mask(slice_id)), cv2.IMREAD_GRAYSCALE)
    lvl = reader.level_for_downsample(32)
    lv = reader.levels[lvl]
    return make_mask(reader.read_region(lvl, 0, 0, lv.width, lv.height), 32 * reader.mpp[0], cfg)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--slices", type=int, nargs="+",
                   default=[1, 15, 30, 45, 60, 75, 90, 105, 120, 140])  # fmt: skip
    p.add_argument("--per-slice", type=int, default=2)
    p.add_argument("--test", type=int, default=6, help="number of test crops")
    p.add_argument("--size", type=float, default=250.0, help="crop side in µm")
    p.add_argument("--depth", type=float, default=300.0, help="minimum depth in the tissue, µm")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--overwrite", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    cfg = ProjectConfig.load(args.config)
    work = WorkDir(cfg.work_dir)
    out = Path(cfg.work_dir) / "labels"
    if (out / "crops.json").exists() and not args.overwrite:
        sys.exit(f"error: {out / 'crops.json'} exists (labels may refer to it); use --overwrite")
    paths = {s.id: s.path for s in Catalog.scan(cfg.data_dir).slices}

    chosen: list[tuple[int, str, float, float]] = []
    previews: list[np.ndarray] = []
    mpps = set()
    for sid in args.slices:
        reader = SlideReader.open(paths[sid])
        try:
            mpp = reader.mpp[0]
            mpps.add(mpp)
            mask = slice_mask(work, reader, sid, cfg)
            here: list[np.ndarray] = []
            tried = 0
            for centre in candidate_centres(mask, 32 * mpp, args.depth, seed=args.seed + sid):
                if len(here) == args.per_slice or tried == 60:
                    break
                if not far_enough(centre, here, 1000.0):
                    continue
                tried += 1
                x0, y0 = centre[0] - args.size / 2, centre[1] - args.size / 2
                rgb = read_um(reader, x0, y0, args.size, 1)
                gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
                sharp = cv2.Laplacian(gray, cv2.CV_64F).var()
                if sharp < SHARPNESS_MIN:
                    log.info("slice %d: candidate at (%.0f, %.0f) µm blurred (%.0f), skipped",
                             sid, *centre, sharp)  # fmt: skip
                    continue
                here.append(centre)
                chosen.append((sid, paths[sid].name, float(x0), float(y0)))
                previews.append(
                    cv2.cvtColor(read_um(reader, x0, y0, args.size, 2), cv2.COLOR_RGB2BGR)
                )
                log.info("slice %d: crop at (%.0f, %.0f) µm, sharpness %.0f", sid, *centre, sharp)
            if len(here) < args.per_slice:
                log.warning("slice %d: only %d sharp crops found", sid, len(here))
        finally:
            reader.close()
    if len(mpps) != 1:
        sys.exit(f"error: slides have different pixel sizes {sorted(mpps)}")
    mpp = mpps.pop()

    split = assign_split([c[0] for c in chosen], args.test, seed=args.seed)
    crops = [
        Crop(i, sid, src, x0, y0, args.size, sp)
        for i, ((sid, src, x0, y0), sp) in enumerate(zip(chosen, split, strict=True))
    ]
    save_crops(out / "crops.json", crops, CLASSES, mpp)
    (out / "qupath").mkdir(exist_ok=True)
    (out / "annotations").mkdir(exist_ok=True)
    for src in sorted({c.source for c in crops}):
        own = [c for c in crops if c.source == src]
        (out / "qupath" / f"{src}.geojson").write_text(
            json.dumps(crops_geojson(own, mpp), indent=1)
        )

    tiles = []
    for c, img in zip(crops, previews, strict=True):
        t = img.copy()
        cv2.putText(t, f"crop {c.id:02d}  slice {c.slice_id}", (10, 32), cv2.FONT_HERSHEY_SIMPLEX,
                    0.9, (0, 0, 0), 2, cv2.LINE_AA)  # fmt: skip
        tiles.append(t)
    tiles += [np.full_like(tiles[0], 255)] * (-len(tiles) % 5)
    sheet = np.vstack([np.hstack(tiles[i : i + 5]) for i in range(0, len(tiles), 5)])
    cv2.imwrite(str(out / "crops_overview.jpg"), sheet, [cv2.IMWRITE_JPEG_QUALITY, 88])
    n_test = sum(c.split == "test" for c in crops)
    log.info("%d crops (%d test) from %d slices written to %s", len(crops), n_test,
             len({c.slice_id for c in crops}), out)  # fmt: skip


if __name__ == "__main__":
    main()
