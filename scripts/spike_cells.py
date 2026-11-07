"""M7-01 real-data spike for cells in 3D (throwaway; no library code, no tests).

Parts (run separately):
  detect  - (a) four nucleus detectors on crops of slices 1, 5, 70, 140: counts, sizes, speed,
            agreement between detectors, side-by-side figures; (b) gallery of large nuclei
            (nucleolus visible?); (c) stain and focus per slice
  recur   - (d) do nuclei recur in the next section after a cell-scale local alignment?
  viewer  - (e) prototype sharded ellipsoid layer with millions of test cells over the volume

Sources are only read (pyramid levels 0 and 1, crops; the 32x level for masks of slices without
a preprocessed mask). Everything is written to <work_dir>/spike/M7-01/.

    .venv/bin/python scripts/spike_cells.py --config config/project.yaml detect
"""

import argparse
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path

import cv2
import numpy as np
from scipy import ndimage as ndi
from scipy.optimize import linear_sum_assignment
from scipy.spatial import cKDTree
from skimage import measure, segmentation
from skimage.color import rgb2hed
from skimage.feature import peak_local_max

from brainrec.catalog import Catalog, ProjectConfig
from brainrec.preprocess.mask import make_mask
from brainrec.registration import px_to_um
from brainrec.slides import SlideReader
from brainrec.store import WorkDir

log = logging.getLogger("spike_cells")
CROP_UM = 250.0
DEPTH_UM = 400.0  # crops at least this far inside the tissue
MATCH_UM = 3.0  # two detections are the same nucleus if their centres are this close

# --- shared helpers ------------------------------------------------------------------------


def tissue_mask(work: WorkDir, reader: SlideReader, slice_id: int, cfg: ProjectConfig):
    """Tissue mask at 32x: the preprocessed one if it exists, else computed in memory."""
    if work.mask(slice_id).exists():
        return cv2.imread(str(work.mask(slice_id)), cv2.IMREAD_GRAYSCALE), 32
    lvl = reader.level_for_downsample(32)
    lv = reader.levels[lvl]
    rgb = reader.read_region(lvl, 0, 0, lv.width, lv.height)
    return make_mask(rgb, 32 * reader.mpp[0], cfg), 32


def crop_centres(mask: np.ndarray, ds: int, mpp: float, n: int, seed: int) -> list[np.ndarray]:
    """`n` crop centres (µm, slice frame) at least DEPTH_UM inside the tissue."""
    depth = cv2.distanceTransform((mask > 0).astype(np.uint8), cv2.DIST_L2, 5) * ds * mpp
    ys, xs = np.nonzero(depth > DEPTH_UM)
    pick = np.random.default_rng(seed).choice(len(xs), n, replace=False)
    return [px_to_um(np.array([[xs[i], ys[i]]], float), ds, (mpp, mpp))[0] for i in pick]


def read_crop(reader: SlideReader, centre_um: np.ndarray, size_um: float, downsample: int):
    lvl = reader.level_for_downsample(downsample)
    d = downsample * reader.mpp[0]
    size = round(size_um / d)
    x0 = round(centre_um[0] / d - 0.5 - size / 2)
    y0 = round(centre_um[1] / d - 0.5 - size / 2)
    return reader.read_region(lvl, x0, y0, size, size)


def hematoxylin(rgb: np.ndarray) -> np.ndarray:
    return rgb2hed(rgb)[..., 0].astype(np.float32)


def objects(labels: np.ndarray, um_px: float, min_um2: float = 8.0) -> np.ndarray:
    """(n, 3): centre x, y (µm, crop frame, pixel centres at (i + 0.5) * um_px) and area (µm²)."""
    rows = [
        ((p.centroid[1] + 0.5) * um_px, (p.centroid[0] + 0.5) * um_px, p.area * um_px**2)
        for p in measure.regionprops(labels)
        if p.area * um_px**2 >= min_um2
    ]
    return np.array(rows, float).reshape(-1, 3)


# --- detectors: RGB crop and pixel size (µm) -> label image --------------------------------


def det_baseline(rgb: np.ndarray, um_px: float) -> np.ndarray:
    """Hematoxylin colour separation, Otsu threshold, distance-transform watershed."""
    h = cv2.GaussianBlur(hematoxylin(rgb), (0, 0), 0.4 / um_px)
    from skimage.filters import threshold_otsu

    fg = h > threshold_otsu(h)
    fg = ndi.binary_opening(fg, iterations=1)
    fg = ndi.binary_fill_holes(fg)
    dist = ndi.distance_transform_edt(fg)
    peaks = peak_local_max(dist, min_distance=max(1, int(2.0 / um_px)), labels=measure.label(fg))
    markers = np.zeros(fg.shape, np.int32)
    markers[tuple(peaks.T)] = np.arange(1, len(peaks) + 1)
    return segmentation.watershed(-dist, markers, mask=fg)


_models: dict[str, object] = {}


def det_stardist(rgb: np.ndarray, um_px: float) -> np.ndarray:
    """StarDist 2D_versatile_he (trained on H&E at about 0.25 µm/px); input rescaled to it."""
    from csbdeep.utils import normalize
    from stardist.models import StarDist2D

    if "stardist" not in _models:
        _models["stardist"] = StarDist2D.from_pretrained("2D_versatile_he")
    model = _models["stardist"]
    f = um_px / 0.25
    img = cv2.resize(rgb, None, fx=f, fy=f, interpolation=cv2.INTER_LINEAR) if f != 1 else rgb
    labels, _ = model.predict_instances(normalize(img, 1, 99.8, axis=(0, 1, 2)), verbose=False)
    return cv2.resize(labels.astype(np.int32), rgb.shape[1::-1], interpolation=cv2.INTER_NEAREST)


def det_instanseg(rgb: np.ndarray, um_px: float) -> np.ndarray:
    """InstanSeg brightfield_nuclei (rescales internally from the given pixel size)."""
    from instanseg import InstanSeg

    if "instanseg" not in _models:
        _models["instanseg"] = InstanSeg("brightfield_nuclei", verbosity=0)
    labels, _ = _models["instanseg"].eval_small_image(rgb, pixel_size=um_px)
    return np.asarray(labels.squeeze().cpu().numpy(), np.int32)


def det_cellpose(rgb: np.ndarray, um_px: float) -> np.ndarray:
    """Cellpose 4 (cpsam, generalist), on the Apple GPU."""
    from cellpose import models

    if "cellpose" not in _models:
        _models["cellpose"] = models.CellposeModel(gpu=True)
    masks, _, _ = _models["cellpose"].eval(rgb, batch_size=16)
    return np.asarray(masks, np.int32)


DETECTORS: dict[str, Callable[[np.ndarray, float], np.ndarray]] = {
    "baseline": det_baseline,
    "stardist": det_stardist,
    "instanseg": det_instanseg,
    "cellpose": det_cellpose,
}
COLOURS = {  # BGR for the figures
    "baseline": (0, 200, 255),
    "stardist": (0, 255, 0),
    "instanseg": (255, 80, 255),
    "cellpose": (255, 200, 0),
}


def agreement(a: np.ndarray, b: np.ndarray) -> float:
    """F1 of one-to-one matching of two centre sets within MATCH_UM."""
    if not len(a) or not len(b):
        return 0.0
    d = np.linalg.norm(a[:, None, :2] - b[None, :, :2], axis=2)
    d[d > MATCH_UM] = 1e6
    r, c = linear_sum_assignment(d)
    tp = int((d[r, c] <= MATCH_UM).sum())
    return 2 * tp / (len(a) + len(b))


def outline(img: np.ndarray, labels: np.ndarray, colour, scale: int = 1) -> np.ndarray:
    out = img.copy()
    b = segmentation.find_boundaries(labels, mode="inner")
    if scale != 1:
        b = cv2.resize(b.astype(np.uint8), img.shape[1::-1], interpolation=cv2.INTER_NEAREST) > 0
    out[b] = colour
    return out


# --- part (a)-(c): detection ----------------------------------------------------------------


def run_detect(cfg: ProjectConfig, out: Path, slices: list[int], n_crops: int) -> None:
    work = WorkDir(cfg.work_dir)
    paths = {s.id: s.path for s in Catalog.scan(cfg.data_dir).slices}
    results: dict = {"crops": [], "timing_s_per_mm2": {}}
    times: dict[str, list[float]] = {}
    gallery: list[np.ndarray] = []
    for sid in slices:
        reader = SlideReader.open(paths[sid])
        try:
            mpp = reader.mpp[0]
            mask, ds = tissue_mask(work, reader, sid, cfg)
            tissue_mm2 = float((mask > 0).sum() * (ds * mpp) ** 2 / 1e6)
            for ci, centre in enumerate(crop_centres(mask, ds, mpp, n_crops, seed=sid)):
                rgb1 = read_crop(reader, centre, CROP_UM, 1)  # 0.22 µm/px
                rgb2 = read_crop(reader, centre, CROP_UM, 2)  # 0.44 µm/px
                h1 = hematoxylin(rgb1)
                gray = cv2.cvtColor(rgb1, cv2.COLOR_RGB2GRAY)
                crop = {
                    "slice": sid, "crop": ci, "centre_um": [float(c) for c in centre],
                    "tissue_mm2": tissue_mm2, "h_mean": float(h1.mean()),
                    "sharpness": float(cv2.Laplacian(gray, cv2.CV_64F).var()),
                    "detectors": {},
                }  # fmt: skip
                found = {}
                panels = [cv2.cvtColor(rgb1, cv2.COLOR_RGB2BGR)]
                for name, fn in DETECTORS.items():
                    for level, rgb, um_px in (("1x", rgb1, mpp), ("2x", rgb2, 2 * mpp)):
                        t0 = time.perf_counter()
                        labels = fn(rgb, um_px)
                        dt = time.perf_counter() - t0
                        if not (sid == slices[0] and ci == 0):  # skip the model-loading run
                            times.setdefault(f"{name}@{level}", []).append(
                                dt / (CROP_UM / 1000) ** 2
                            )
                        obj = objects(labels, um_px)
                        found[f"{name}@{level}"] = obj
                        diam = 2 * np.sqrt(obj[:, 2] / np.pi) if len(obj) else np.zeros(1)
                        crop["detectors"][f"{name}@{level}"] = {
                            "n": len(obj), "per_mm2": len(obj) / (CROP_UM / 1000) ** 2,
                            "diam_um_p10_50_90": [float(np.percentile(diam, q)) for q in (10, 50, 90)],
                            "seconds": dt,
                        }  # fmt: skip
                        if level == "2x":
                            panel = outline(cv2.cvtColor(rgb1, cv2.COLOR_RGB2BGR), labels,
                                            COLOURS[name], scale=2)  # fmt: skip
                            cv2.putText(panel, f"{name} @2x: {len(obj)}", (12, 40),
                                        cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 0), 3, cv2.LINE_AA)  # fmt: skip
                            panels.append(panel)
                        if name == "stardist" and level == "1x":
                            sd_labels = labels
                keys = list(found)
                crop["agreement_f1"] = {
                    f"{a}|{b}": agreement(found[a], found[b])
                    for i, a in enumerate(keys) for b in keys[i + 1:]
                }  # fmt: skip
                results["crops"].append(crop)
                fig = np.hstack(panels)
                cv2.imwrite(
                    str(out / f"detect_s{sid}_c{ci}.jpg"), fig, [cv2.IMWRITE_JPEG_QUALITY, 88]
                )
                # zoomed centre (100 µm) of every panel, 2x enlarged
                c0, w = fig.shape[0] // 2 - 227, 454
                zoom = np.hstack([p[c0 : c0 + w, c0 : c0 + w] for p in panels])
                zoom = cv2.resize(zoom, None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST)
                cv2.imwrite(
                    str(out / f"zoom_s{sid}_c{ci}.jpg"), zoom, [cv2.IMWRITE_JPEG_QUALITY, 90]
                )
                gallery += large_nuclei(rgb1, sd_labels, mpp)
                log.info("slice %d crop %d: %s", sid, ci,
                         {k: v["n"] for k, v in crop["detectors"].items()})  # fmt: skip
        finally:
            reader.close()
    results["timing_s_per_mm2"] = {k: float(np.median(v)) for k, v in times.items()}
    (out / "detect.json").write_text(json.dumps(results, indent=1))
    if gallery:
        gallery = sorted(gallery, key=lambda g: -g.shape[0])[:48]
        tiles = [cv2.resize(g, (180, 180), interpolation=cv2.INTER_CUBIC) for g in gallery]
        tiles += [np.full_like(tiles[0], 255)] * (-len(tiles) % 8)
        rows = [np.hstack(tiles[i : i + 8]) for i in range(0, len(tiles), 8)]
        cv2.imwrite(
            str(out / "gallery_large_nuclei.jpg"), np.vstack(rows), [cv2.IMWRITE_JPEG_QUALITY, 92]
        )


def large_nuclei(rgb: np.ndarray, labels: np.ndarray, um_px: float, min_um2: float = 40.0):
    """Tiles (12 µm, 1x) around the largest nuclei of a crop, for the nucleolus check."""
    half = int(6.0 / um_px)
    tiles = []
    props = sorted(measure.regionprops(labels), key=lambda p: -p.area)
    for p in props[:6]:
        if p.area * um_px**2 < min_um2:
            break
        cy, cx = map(int, p.centroid)
        if half <= cy < rgb.shape[0] - half and half <= cx < rgb.shape[1] - half:
            t = cv2.cvtColor(rgb[cy - half : cy + half, cx - half : cx + half], cv2.COLOR_RGB2BGR)
            tiles.append(t)
    return tiles


# --- part (d): recurrence of nuclei in the next section ----------------------------------


def _shift(v) -> np.ndarray:
    return np.array([[1, 0, v[0]], [0, 1, v[1]], [0, 0, 1]], float)


def _pair_transform(cfg, work, paths, a: int, b: int):
    """Transform b -> a (µm): from the chained transforms if both were registered together,
    else registered here in memory (nothing written)."""
    from brainrec.registration.chain import load_chained

    try:
        ch = load_chained(work, [a, b])
        return ch[a].inverse().compose(ch[b])
    except (FileNotFoundError, ValueError):
        from brainrec.preprocess.regimage import make_reg_image
        from brainrec.registration import RegImage
        from brainrec.registration.pairwise import register_pair

        imgs = {}
        for k in (a, b):
            r = SlideReader.open(paths[k])
            try:
                lvl = r.level_for_downsample(cfg.reg_downsample)
                lv = r.levels[lvl]
                rgb = r.read_region(lvl, 0, 0, lv.width, lv.height)
                mask = make_mask(rgb, cfg.reg_downsample * r.mpp[0], cfg)
                imgs[k] = RegImage(
                    k, make_reg_image(rgb, mask), mask, float(cfg.reg_downsample), r.mpp
                )
            finally:
                r.close()
        pr = register_pair(imgs[a], imgs[b], cfg)
        log.info("registered %d<-%d in memory: %s, %d inliers", a, b, pr.status, pr.n_inliers)
        return pr.transform


def _window_pair(ra, rb, t_ba, p_a, search_a, w_um, m_um, um_px):
    """Window of a (level 0) at p_a, and b warped into a's frame around search_a (with margin)."""
    w, m = int(w_um / um_px), int(m_um / um_px)
    win_a = read_crop(ra, p_a, w * um_px, 1)
    p_b = t_ba.inverse().apply(search_a[None])[0]
    crop_b = read_crop(rb, p_b, (w + 2 * m) * um_px * 1.5, 1)
    big = crop_b.shape[0]
    o_b = np.round(p_b / um_px - 0.5 - big / 2)
    o_ext = np.round(search_a / um_px - 0.5 - (w / 2 + m))
    m_crop = _shift(-o_ext) @ t_ba.to_pixels(1, 1, ra.mpp) @ _shift(o_b)
    ext_b = cv2.warpAffine(crop_b, m_crop[:2], (w + 2 * m, w + 2 * m), flags=cv2.INTER_LINEAR,
                           borderValue=(255, 255, 255))  # fmt: skip
    return win_a, ext_b, m


def _fine_fit(na: np.ndarray, nb: np.ndarray, um_px: float):
    """Similarity mapping nb -> na fitted to nearest-neighbour nucleus pairs, tightening the
    tolerance (3, 2, 1.5 µm); returns the moved nb (µm)."""
    cur = nb.copy()
    for tol in (3.0, 2.0, 1.5):
        if len(cur) < 6 or len(na) < 6:
            break
        d, j = cKDTree(cur).query(na)
        ok = d <= tol
        if ok.sum() < 6:
            break
        m, _ = cv2.estimateAffinePartial2D(cur[j[ok]].astype(np.float32), na[ok].astype(np.float32),
                                             method=cv2.RANSAC, ransacReprojThreshold=1.0)  # fmt: skip
        if m is None:
            break
        cur = cur @ m[:, :2].T + m[:, 2]
    return cur


def _recurrence(win_a, ext_b, m, um_px, detector):
    ha = cv2.GaussianBlur(hematoxylin(win_a), (0, 0), 1.0 / um_px)
    hb = cv2.GaussianBlur(hematoxylin(ext_b), (0, 0), 1.0 / um_px)
    res = cv2.matchTemplate(hb, ha, cv2.TM_CCOEFF_NORMED)
    _, peak, _, (dx, dy) = cv2.minMaxLoc(res)
    w = win_a.shape[0]
    b_al = ext_b[dy : dy + w, dx : dx + w]
    na = objects(detector(win_a, um_px), um_px)[:, :2]
    nb = objects(detector(b_al, um_px), um_px)[:, :2]
    out = {"ncc": float(peak), "offset_um": [float((dx - m) * um_px), float((dy - m) * um_px)],
           "n_a": len(na), "n_b": len(nb)}  # fmt: skip
    if len(na) and len(nb):
        moved = _fine_fit(na, nb, um_px)
        d, _ = cKDTree(moved).query(na)
        out.update({f"shared_{t}um": float((d <= t).mean()) for t in (1.0, 1.5, 2.5)})
        out["median_match_um"] = float(np.median(d[d <= 2.5])) if (d <= 2.5).any() else None
        d0, _ = cKDTree(nb).query(na)
        out["shared_1.5um_before_fit"] = float((d0 <= 1.5).mean())
    return out, b_al


def run_recur(cfg: ProjectConfig, out: Path, pairs: list[tuple[int, int]], n_win: int) -> None:
    work = WorkDir(cfg.work_dir)
    paths = {s.id: s.path for s in Catalog.scan(cfg.data_dir).slices}
    detector = det_instanseg
    rows = []
    for a, b in pairs:
        t_ba = _pair_transform(cfg, work, paths, a, b)
        ra, rb = SlideReader.open(paths[a]), SlideReader.open(paths[b])
        try:
            um_px = ra.mpp[0]
            mask, ds = tissue_mask(work, ra, a, cfg)
            centres = crop_centres(mask, ds, um_px, 4 * n_win, seed=100 + a)
            used = 0
            for p in centres:
                if used == n_win:
                    break
                probe = read_crop(ra, p, 150, 1)
                if cv2.Laplacian(cv2.cvtColor(probe, cv2.COLOR_RGB2GRAY), cv2.CV_64F).var() < 100:
                    continue  # blurred window: no reliable nuclei (part a)
                used += 1
                win_a, ext_b, m = _window_pair(ra, rb, t_ba, p, p, 150, 100, um_px)
                true, b_al = _recurrence(win_a, ext_b, m, um_px, detector)
                ctl_a = p + np.array([1500.0, 0.0])
                _, ext_c, _ = _window_pair(ra, rb, t_ba, p, ctl_a, 150, 100, um_px)
                ctrl, _ = _recurrence(win_a, ext_c, m, um_px, detector)
                rows.append(
                    {
                        "pair": [a, b],
                        "window_um": [float(x) for x in p],
                        "true": true,
                        "control": ctrl,
                    }
                )
                fig = np.hstack(
                    [cv2.cvtColor(win_a, cv2.COLOR_RGB2BGR), cv2.cvtColor(b_al, cv2.COLOR_RGB2BGR)]
                )
                cv2.imwrite(
                    str(out / f"recur_{a}_{b}_{used}.jpg"), fig, [cv2.IMWRITE_JPEG_QUALITY, 88]
                )
                log.info("%d->%d win %d: true %s | control %s", a, b, used,
                         {k: (round(v, 3) if isinstance(v, float) else v) for k, v in true.items()},
                         {k: (round(v, 3) if isinstance(v, float) else v) for k, v in ctrl.items() if k.startswith("shared")})  # fmt: skip
        finally:
            ra.close()
            rb.close()
    (out / "recur.json").write_text(json.dumps(rows, indent=1))


# --- part (e): viewer load test with a prototype sharded ellipsoid layer -------------------

TEST_TYPES = {"glia": (0.70, "#4dabf7"), "neuron": (0.20, "#ff922b"), "vessel": (0.10, "#e64980")}


def _morton(pos: np.ndarray, grid: list[int]) -> np.ndarray:
    """Compressed Morton code of grid positions (n, rank), as in Neuroglancer's spec."""
    bits = [max(0, int(np.ceil(np.log2(g)))) for g in grid]
    code = np.zeros(len(pos), np.uint64)
    out_bit = 0
    for i in range(max(bits) if bits else 0):
        for d, nb in enumerate(bits):
            if i < nb:
                code |= ((pos[:, d].astype(np.uint64) >> np.uint64(i)) & np.uint64(1)) << np.uint64(
                    out_bit
                )
                out_bit += 1
    return code


def _write_shard(path: Path, keys: np.ndarray, chunks: list[bytes], minishard_bits: int) -> dict:
    """One shard file (shard_bits 0, identity hash, raw encodings) holding all `chunks`."""
    n_mini = 1 << minishard_bits
    order = np.argsort(keys, kind="stable")
    keys = keys[order]
    mini = (keys & np.uint64(n_mini - 1)).astype(np.int64)
    data, offsets, sizes, pos = (
        [],
        np.zeros(len(keys), np.uint64),
        np.zeros(len(keys), np.uint64),
        0,
    )
    for k, i in enumerate(order):
        b = chunks[i]
        data.append(b)
        offsets[k], sizes[k] = pos, len(b)
        pos += len(b)
    index_parts, shard_index = [], np.zeros((n_mini, 2), np.uint64)
    for m in range(n_mini):
        sel = np.nonzero(mini == m)[0]
        if not len(sel):
            shard_index[m] = (pos, pos)
            continue
        ids, off, size = keys[sel], offsets[sel], sizes[sel]
        d_ids = np.diff(ids, prepend=np.uint64(0))
        prev_end = np.concatenate([[np.uint64(0)], off[:-1] + size[:-1]]).astype(np.uint64)
        d_off = off - prev_end
        arr = np.stack([d_ids, d_off, size]).astype("<u8").tobytes()
        shard_index[m] = (pos, pos + len(arr))
        index_parts.append(arr)
        pos += len(arr)
    with open(path, "wb") as f:
        f.write(shard_index.astype("<u8").tobytes())
        for b in data:
            f.write(b)
        for b in index_parts:
            f.write(b)
    return {"@type": "neuroglancer_uint64_sharded_v1", "preshift_bits": 0, "hash": "identity",
            "minishard_bits": minishard_bits, "shard_bits": 0,
            "minishard_index_encoding": "raw", "data_encoding": "raw"}  # fmt: skip


ANN_DTYPE = np.dtype(
    [("geom", "<f4", 6), ("confidence", "<f4"), ("nucleolus", "u1"), ("pad", "u1", 3)]
)


def write_ellipsoid_layer(
    out: Path,
    centres: np.ndarray,
    radii: np.ndarray,
    conf: np.ndarray,
    nucleolus: np.ndarray,
    ids: np.ndarray,
    limit: int = 4000,
) -> None:
    """Prototype of contract C17: ellipsoids in µm, multi-level sharded spatial index plus a
    sharded by-id index; one shard file per index."""
    out.mkdir(parents=True, exist_ok=True)
    rec = np.zeros(len(ids), ANN_DTYPE)
    rec["geom"][:, :3], rec["geom"][:, 3:] = centres, radii
    rec["confidence"], rec["nucleolus"] = conf, nucleolus
    lo, hi = (centres - radii).min(0), (centres + radii).max(0)
    info = {
        "@type": "neuroglancer_annotations_v1",
        "dimensions": {d: [1e-6, "m"] for d in ("x", "y", "z")},
        "lower_bound": lo.tolist(), "upper_bound": hi.tolist(),
        "annotation_type": "ELLIPSOID",
        "properties": [{"id": "confidence", "type": "float32"},
                       {"id": "nucleolus", "type": "uint8", "enum_values": [0, 1],
                        "enum_labels": ["no", "yes"]}],
        "relationships": [],
        "by_id": {"key": "by_id", "sharding": None}, "spatial": [],
    }  # fmt: skip
    by_id_chunks = [r.tobytes() for r in rec]
    info["by_id"]["sharding"] = _write_shard(
        out / "by_id.shard.tmp", ids.astype(np.uint64), by_id_chunks, 12
    )
    (out / "by_id").mkdir(exist_ok=True)
    (out / "by_id.shard.tmp").replace(out / "by_id" / "0.shard")
    remaining = np.random.default_rng(0).permutation(len(ids))
    extent = np.maximum(hi - lo, 1e-3)
    level = 0
    while len(remaining):
        g = [2**level, 2**level, 1]
        cell = extent / np.array(g)
        pos = np.minimum(((centres[remaining] - lo) / cell).astype(np.int64), np.array(g) - 1)
        key = _morton(pos, g)
        finest = (np.bincount(key.astype(np.int64)).max() <= limit) or level >= 12
        take = np.zeros(len(remaining), bool)
        keys, chunks = [], []
        for k in np.unique(key):
            members = np.nonzero(key == k)[0]
            sel = members if finest else members[:limit]
            take[sel] = True
            idx = remaining[sel]
            keys.append(k)
            chunks.append(
                np.uint64(len(idx)).tobytes()
                + rec[idx].tobytes()
                + ids[idx].astype("<u8").tobytes()
            )
        name = f"spatial{level}"
        (out / name).mkdir(exist_ok=True)
        sh = _write_shard(out / name / "0.shard", np.array(keys, np.uint64), chunks, 6)
        info["spatial"].append({"key": name, "grid_shape": g, "chunk_size": cell.tolist(),
                                "limit": limit, "sharding": sh})  # fmt: skip
        remaining = remaining[~take]
        level += 1
    (out / "info").write_text(json.dumps(info))


def read_by_id(layer: Path, ann_id: int) -> np.void | None:
    """Test reader for the by-id shard (prototype check of the writer)."""
    info = json.loads((layer / "info").read_text())
    m_bits = info["by_id"]["sharding"]["minishard_bits"]
    raw = (layer / "by_id" / "0.shard").read_bytes()
    base = 16 << m_bits
    m = ann_id & ((1 << m_bits) - 1)
    start, end = np.frombuffer(raw, "<u8", 2, 16 * m)
    if start == end:
        return None
    arr = np.frombuffer(raw[base + int(start) : base + int(end)], "<u8").reshape(3, -1)
    ids, offs = np.cumsum(arr[0]), np.cumsum(arr[1]) + np.concatenate([[0], np.cumsum(arr[2])[:-1]])
    hit = np.nonzero(ids == ann_id)[0]
    if not len(hit):
        return None
    o = base + int(offs[hit[0]])
    return np.frombuffer(raw[o : o + ANN_DTYPE.itemsize], ANN_DTYPE)[0]


def run_viewer_layers(cfg: ProjectConfig, out: Path, density_mm2: float) -> None:
    """Test cells (random positions inside the tissue masks, through the chained transforms),
    one layer per test type, in the volume's frame (µm)."""
    import zarr

    from brainrec.registration.chain import load_chained

    work = WorkDir(cfg.work_dir)
    attrs = zarr.open_group(str(work.volume), mode="r").attrs["brainrec"]
    ids_s = attrs["slice_ids"]
    ox, oy = attrs["canvas_origin_um"]
    chained = load_chained(work, ids_s)
    rng = np.random.default_rng(7)
    pts_all, z_all = [], []
    for zi, k in enumerate(ids_s):
        mask = cv2.imread(str(work.mask(k)), cv2.IMREAD_GRAYSCALE)
        meta = json.loads(work.reg_meta(k).read_text())
        um = meta["downsample"] * meta["mpp_um"][0]
        ys, xs = np.nonzero(mask)
        n = rng.poisson(density_mm2 * um**2 / 1e6, len(xs))
        px = np.repeat(np.stack([xs, ys], 1).astype(float), n, 0) + rng.uniform(
            -0.5, 0.5, (n.sum(), 2)
        )
        p_um = (px + 0.5) * um
        ref = chained[k].apply(p_um) - [ox, oy]
        pts_all.append(ref)
        z_all.append(np.full(len(ref), (zi + 0.5) * cfg.z_spacing_um))
        log.info("slice %d: %d test cells", k, len(ref))
    xy, z = np.vstack(pts_all), np.concatenate(z_all)
    n = len(z)
    ttype = rng.choice(list(TEST_TYPES), n, p=[v[0] for v in TEST_TYPES.values()])
    r = np.clip(rng.normal(3.2, 0.7, n), 1.5, 9) * np.where(ttype == "neuron", 1.6, 1.0)
    conf = rng.uniform(0.5, 1.0, n).astype(np.float32)
    nucl = (rng.uniform(size=n) < 0.3).astype(np.uint8)
    ids = np.arange(1, n + 1, dtype=np.uint64)
    root = out / "cells_test"
    for name in TEST_TYPES:
        sel = ttype == name
        c = np.column_stack([xy[sel], z[sel]]).astype(np.float32)
        rad = np.repeat(r[sel, None], 3, 1).astype(np.float32)
        t0 = time.perf_counter()
        write_ellipsoid_layer(root / f"layer_{name}", c, rad, conf[sel], nucl[sel], ids[sel])
        check = read_by_id(root / f"layer_{name}", int(ids[sel][123]))
        assert check is not None and np.allclose(check["geom"][:3], c[123]), "by-id round trip"
        log.info("layer %s: %d cells, %.0f s", name, sel.sum(), time.perf_counter() - t0)
    log.info("total %d test cells", n)


def serve_viewer(cfg: ProjectConfig, out: Path, port: int) -> None:
    """Volume plus the test cell layers in a local Neuroglancer (like `brainrec serve`)."""
    import uvicorn
    import zarr

    from brainrec.serve import create_app, launch_local_viewer, neuroglancer_state

    work = WorkDir(cfg.work_dir)
    ome = zarr.open_group(str(work.volume), mode="r").attrs["ome"]
    state = neuroglancer_state(f"http://127.0.0.1:{port}/{work.volume.name}", ome, layout="xy-3d")
    # the tissue layer fades with Neuroglancer's own "Opacity" slider (layer panel, Rendering)
    rel = (out / "cells_test").relative_to(work.root).as_posix()
    for name, (_, colour) in TEST_TYPES.items():
        state["layers"].append({
            "type": "annotation", "name": f"test {name}",
            "source": f"precomputed://http://127.0.0.1:{port}/{rel}/layer_{name}",
            "shader": (f'#uicontrol vec3 colour color(default="{colour}")\n'
                       "#uicontrol float min_confidence slider(min=0, max=1, default=0)\n"
                       "void main() {\n  if (prop_confidence() < min_confidence) discard;\n"
                       "  setColor(colour);\n}\n"),
        })  # fmt: skip
    viewer = launch_local_viewer(state, port + 1)
    print("Viewer (open in Chrome):", viewer, flush=True)
    uvicorn.run(create_app(work.root), host="127.0.0.1", port=port)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", type=Path, required=True)
    sub = p.add_subparsers(dest="part", required=True)
    d = sub.add_parser("detect")
    d.add_argument("--slices", type=int, nargs="+", default=[1, 5, 70, 140])
    d.add_argument("--crops", type=int, default=3)
    r = sub.add_parser("recur")
    r.add_argument("--pairs", nargs="+", default=["1-2", "6-7", "8-9", "69-70"])
    r.add_argument("--windows", type=int, default=4)
    v = sub.add_parser("viewer")
    v.add_argument("--write", action="store_true", help="write the test layers")
    v.add_argument("--density", type=float, default=1000.0, help="test cells per mm²")
    v.add_argument("--port", type=int, default=8000)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    cfg = ProjectConfig.load(args.config)
    out = Path(cfg.work_dir) / "spike" / "M7-01"
    out.mkdir(parents=True, exist_ok=True)
    if args.part == "detect":
        run_detect(cfg, out, args.slices, args.crops)
    elif args.part == "recur":
        pairs = [tuple(int(x) for x in t.split("-")) for t in args.pairs]
        run_recur(cfg, out, pairs, args.windows)  # type: ignore[arg-type]
    elif args.part == "viewer":
        if args.write:
            run_viewer_layers(cfg, out, args.density)
        else:
            serve_viewer(cfg, out, args.port)


if __name__ == "__main__":
    main()
