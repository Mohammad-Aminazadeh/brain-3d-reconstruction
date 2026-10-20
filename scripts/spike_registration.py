"""M3-06 spike: orientation, tissue masks and feature matching on real slices.

Throwaway script (not part of the package, no unit tests). Reads only one coarse pyramid level
of each slice (read-only) and writes only to <work_dir>/results/M3-06_registration_spike/.

    python scripts/spike_registration.py --config config/project.yaml --slices 1-10

Outputs: contact_sheet.png, overlay_<k>.png and matches_<k>.png per pair (k = moving slice),
results.json, and a summary table on stdout. Transforms map the moving slice onto the fixed
(previous) slice; angles are in degrees in image coordinates (y down, positive = clockwise).
"""

import argparse
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from brainrec.catalog import Catalog, ProjectConfig
from brainrec.slides import SlideReader

RATIO = {"sift": 0.75, "orb": 0.8}
HIDE = ("rigid", "similarity", "inlier_idx")  # bulky fields kept out of results.json
RANSAC_PX = 4.0
MAX_FEATURES = 50_000


@dataclass
class Slice:
    id: int
    rgb: np.ndarray
    reg: np.ndarray  # inverted luminance, CLAHE, background zeroed: tissue bright
    mask: np.ndarray  # uint8 {0, 255}
    um_per_px: float


def tissue_mask(rgb: np.ndarray) -> np.ndarray:
    """Otsu on saturation (glass is grey/white, stained tissue is coloured), then cleanup."""
    sat = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)[..., 1]
    sat = cv2.GaussianBlur(sat, (5, 5), 0)
    _, m = cv2.threshold(sat, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15)))
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
    # fill holes: everything not reachable from the (padded) border is inside the tissue
    p = cv2.copyMakeBorder(m, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=0)
    outside = 255 - p
    cv2.floodFill(outside, np.zeros((p.shape[0] + 2, p.shape[1] + 2), np.uint8), (0, 0), 0)
    m = (p | outside)[1:-1, 1:-1]
    # drop components below 1% of the tissue area (debris, dust, label remnants)
    _, labels, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    areas = stats[1:, cv2.CC_STAT_AREA]
    keep = np.flatnonzero(areas >= 0.01 * areas.sum()) + 1
    return np.where(np.isin(labels, keep), 255, 0).astype(np.uint8)


def load(ref: Any, downsample: int) -> Slice:
    r = SlideReader.open(ref.path)
    try:
        lvl = r.level_for_downsample(downsample)
        lv = r.levels[lvl]
        rgb = r.read_region(lvl, 0, 0, lv.width, lv.height)
        um = r.mpp[0] * downsample
    finally:
        r.close()
    mask = tissue_mask(rgb)
    inv = 255 - cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    reg = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(inv)
    reg[mask == 0] = 0
    return Slice(ref.id, rgb, reg, mask, um)


def detect(img: np.ndarray, mask: np.ndarray, kind: str) -> tuple[list, np.ndarray | None]:
    det = (
        cv2.SIFT_create(nfeatures=MAX_FEATURES) if kind == "sift" else cv2.ORB_create(MAX_FEATURES)
    )
    kp, desc = det.detectAndCompute(img, mask)
    return list(kp), desc


def match(dm: np.ndarray | None, df: np.ndarray | None, kind: str) -> list:
    if dm is None or df is None or len(dm) < 2 or len(df) < 2:
        return []
    if kind == "sift":
        matcher = cv2.FlannBasedMatcher({"algorithm": 1, "trees": 5}, {"checks": 64})
    else:
        matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
    good = []
    for pair in matcher.knnMatch(dm, df, k=2):
        if len(pair) == 2 and pair[0].distance < RATIO[kind] * pair[1].distance:
            good.append(pair[0])
    return good


def guided_match(
    kpm: list,
    dm: np.ndarray | None,
    kpf: list,
    df: np.ndarray | None,
    init: np.ndarray,
    radius_px: float,
    kind: str,
    k: int = 30,
) -> list:
    """Ratio test only among fixed keypoints near where `init` predicts the moving one lands."""
    if dm is None or df is None or len(dm) < 2 or len(df) < 2:
        return []
    if kind == "sift":
        matcher = cv2.FlannBasedMatcher({"algorithm": 1, "trees": 5}, {"checks": 128})
    else:
        matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
    pred = apply(init, np.float32([p.pt for p in kpm]))
    fpos = np.float32([p.pt for p in kpf])
    good = []
    for cands in matcher.knnMatch(dm, df, k=min(k, len(df))):
        if not cands:
            continue
        near = [c for c in cands if np.linalg.norm(fpos[c.trainIdx] - pred[c.queryIdx]) < radius_px]
        if len(near) == 1 or (len(near) >= 2 and near[0].distance < RATIO[kind] * near[1].distance):
            good.append(near[0])
    return good


def guided_two_pass(
    kpm: list, dm: np.ndarray | None, kpf: list, df: np.ndarray | None,
    init: np.ndarray, um: float, kind: str,
) -> dict[str, Any]:  # fmt: skip
    """Pass 1: wide window (2.5 mm) around the boundary estimate; pass 2: 0.3 mm around pass 1."""
    r1 = fit(kpm, kpf, guided_match(kpm, dm, kpf, df, init, 2500 / um, kind), um)
    if r1["inliers"] < 3:
        return {**r1, "pass1_inliers": r1["inliers"]}
    good2 = guided_match(kpm, dm, kpf, df, np.array(r1["similarity"]), 300 / um, kind)
    r2 = fit(kpm, kpf, good2, um)
    r2["pass1_inliers"] = r1["inliers"]
    return r2


def kabsch(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Best rigid (rotation + translation, no scale, no reflection) 2x3 map src -> dst."""
    cs, cd = src.mean(0), dst.mean(0)
    u, _, vt = np.linalg.svd((src - cs).T @ (dst - cd))
    d = np.sign(np.linalg.det(vt.T @ u.T))
    rot = vt.T @ np.diag([1.0, d]) @ u.T
    return np.hstack([rot, (cd - rot @ cs)[:, None]])


def apply(m: np.ndarray, pts: np.ndarray) -> np.ndarray:
    return pts @ m[:, :2].T + m[:, 2]


def fit(kpm: list, kpf: list, good: list, um: float) -> dict[str, Any]:
    out: dict[str, Any] = {"matches": len(good), "inliers": 0}
    if len(good) < 3:
        return out
    src = np.float32([kpm[g.queryIdx].pt for g in good])
    dst = np.float32([kpf[g.trainIdx].pt for g in good])
    sim, inl = cv2.estimateAffinePartial2D(
        src,
        dst,
        method=cv2.RANSAC,
        ransacReprojThreshold=RANSAC_PX,
        maxIters=10000,
        confidence=0.999,
    )
    if sim is None:
        return out
    inl = inl.ravel().astype(bool)
    s_in, d_in = src[inl].astype(float), dst[inl].astype(float)
    rig = kabsch(s_in, d_in)

    def rms(m: np.ndarray) -> float:
        return float(np.sqrt(((apply(m, s_in) - d_in) ** 2).sum(1).mean())) * um

    out.update(
        inliers=int(inl.sum()),
        inlier_ratio=round(float(inl.mean()), 3),
        angle_deg=round(math.degrees(math.atan2(rig[1, 0], rig[0, 0])), 3),
        shift_um=[round(float(v) * um, 1) for v in rig[:, 2]],
        scale=round(float(math.hypot(sim[0, 0], sim[1, 0])), 5),
        rms_rigid_um=round(rms(rig), 2),
        rms_similarity_um=round(rms(sim), 2),
        rigid=rig.tolist(),
        similarity=sim.tolist(),
        inlier_idx=np.flatnonzero(inl).tolist(),
    )
    return out


def principal(mask: np.ndarray) -> tuple[np.ndarray, float]:
    mo = cv2.moments(mask, binaryImage=True)
    c = np.array([mo["m10"] / mo["m00"], mo["m01"] / mo["m00"]])
    theta = 0.5 * math.atan2(2 * mo["mu11"], mo["mu20"] - mo["mu02"])
    return c, theta


def iou(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.logical_and(a > 0, b > 0).sum() / max(1, np.logical_or(a > 0, b > 0).sum()))


def boundary_align(mov: Slice, fix: Slice) -> dict[str, Any]:
    """Initial alignment from the tissue shapes alone: centroids + principal axes (2 candidates)."""
    cm, tm = principal(mov.mask)
    cf, tf = principal(fix.mask)
    best: dict[str, Any] = {}
    for extra in (0.0, math.pi):
        a = tf - tm + extra
        rot = np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])
        m = np.hstack([rot, (cf - rot @ cm)[:, None]])
        warped = cv2.warpAffine(mov.mask, m, fix.mask.shape[::-1], flags=cv2.INTER_NEAREST)
        score = iou(warped, fix.mask)
        if score > best.get("iou", -1):
            best = {"iou": round(score, 4), "angle_deg": round(math.degrees(a), 2), "m": m}
    return best


def overlay(fix: Slice, mov: Slice, m: np.ndarray, path: Path) -> float:
    size = fix.reg.shape[::-1]
    warped = cv2.warpAffine(mov.reg, m, size)
    wmask = cv2.warpAffine(mov.mask, m, size, flags=cv2.INTER_NEAREST)
    bgr = cv2.merge([warped, fix.reg, warped])  # moving magenta, fixed green
    for msk, colour in ((fix.mask, (0, 255, 0)), (wmask, (255, 0, 255))):
        cs, _ = cv2.findContours(msk, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(bgr, cs, -1, colour, 3)
    save_scaled(bgr, path, 1800)
    return iou(wmask, fix.mask)


def save_scaled(bgr: np.ndarray, path: Path, max_w: int) -> None:
    f = min(1.0, max_w / bgr.shape[1])
    cv2.imwrite(str(path), cv2.resize(bgr, None, fx=f, fy=f, interpolation=cv2.INTER_AREA))


def contact_sheet(slices: list[Slice], path: Path) -> None:
    cw, ch, cols = 700, 520, 5
    rows = math.ceil(len(slices) / cols)
    sheet = np.full((rows * ch, cols * cw, 3), 60, np.uint8)
    for i, s in enumerate(slices):
        bgr = cv2.cvtColor(s.rgb, cv2.COLOR_RGB2BGR)
        cs, _ = cv2.findContours(s.mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(bgr, cs, -1, (0, 200, 0), 8)
        f = min((cw - 20) / bgr.shape[1], (ch - 50) / bgr.shape[0])
        th = cv2.resize(bgr, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
        y, x = (i // cols) * ch + 40, (i % cols) * cw + 10
        sheet[y : y + th.shape[0], x : x + th.shape[1]] = th
        label = f"slice {s.id}  {s.rgb.shape[1]}x{s.rgb.shape[0]} px"
        cv2.putText(sheet, label, (x, y - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
    cv2.imwrite(str(path), sheet)


def register_pair(fix: Slice, mov: Slice, out: Path) -> dict[str, Any]:
    res: dict[str, Any] = {"fixed": fix.id, "moving": mov.id}
    t0 = time.perf_counter()
    b = boundary_align(mov, fix)
    res["boundary"] = {k: v for k, v in b.items() if k != "m"}
    feats = {}
    for kind in ("sift", "orb"):
        t = time.perf_counter()
        kpm, dm = detect(mov.reg, mov.mask, kind)
        kpf, df = detect(fix.reg, fix.mask, kind)
        good = match(dm, df, kind)
        r = fit(kpm, kpf, good, mov.um_per_px)
        r.update(keypoints=[len(kpm), len(kpf)], seconds=round(time.perf_counter() - t, 1))
        feats[kind] = (kpm, dm, kpf, df, good, r)
        res[kind] = {k: v for k, v in r.items() if k not in HIDE}
    # mirrored candidate (SIFT descriptors are not mirror-invariant)
    flipped = Slice(mov.id, mov.rgb, cv2.flip(mov.reg, 1), cv2.flip(mov.mask, 1), mov.um_per_px)
    kpm, dm = detect(flipped.reg, flipped.mask, "sift")
    kpf, df = detect(fix.reg, fix.mask, "sift")
    rm = fit(kpm, kpf, match(dm, df, "sift"), mov.um_per_px)
    res["mirrored_sift_inliers"] = rm["inliers"]
    # guided matching: windows around the boundary estimate, then around the first fit
    guided = {}
    for kind in ("sift", "orb"):
        kpm, dm, kpf, df, _, _ = feats[kind]
        t = time.perf_counter()
        g = guided_two_pass(kpm, dm, kpf, df, b["m"], mov.um_per_px, kind)
        g["seconds"] = round(time.perf_counter() - t, 1)
        guided[kind] = g
        res[f"guided_{kind}"] = {k: v for k, v in g.items() if k not in HIDE}
    gk = max(guided, key=lambda k: guided[k]["inliers"])
    if guided[gk]["inliers"] >= 3:
        res["guided_best"] = gk
        sim = np.array(guided[gk]["similarity"])
        path = out / f"overlay_guided_{mov.id}.png"
        res["guided_overlay_iou"] = round(overlay(fix, mov, sim, path), 4)
    # best global feature result drives the pictures and the comparison with the boundary estimate
    kind = max(feats, key=lambda k: feats[k][5]["inliers"])
    kpm, _, kpf, _, good, r = feats[kind]
    res["best"] = kind
    if r["inliers"] >= 3:
        rig = np.array(r["rigid"])
        res["overlay_iou"] = round(overlay(fix, mov, rig, out / f"overlay_{mov.id}.png"), 4)
        cm, _ = principal(mov.mask)
        d = np.linalg.norm(apply(rig, cm[None]) - apply(b["m"], cm[None])) * mov.um_per_px
        res["boundary_vs_features"] = {
            "angle_diff_deg": round(abs(((b["angle_deg"] - r["angle_deg"]) + 180) % 360 - 180), 2),
            "centroid_diff_um": round(float(d), 1),
        }
        outline, _ = cv2.findContours(mov.mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        rmax = max(float(np.linalg.norm(c.reshape(-1, 2) - cm, axis=1).max()) for c in outline)
        res["scale_edge_displacement_um"] = round(abs(r["scale"] - 1) * rmax * mov.um_per_px, 1)
        inl = [good[i] for i in r["inlier_idx"]]
        step = max(1, len(inl) // 150)
        pic = cv2.drawMatches(
            mov.reg, kpm, fix.reg, kpf, inl[::step], None,
            matchColor=(0, 255, 255), flags=cv2.DrawMatchesFlags_NOT_DRAW_SINGLE_POINTS,
        )  # fmt: skip
        save_scaled(pic, out / f"matches_{mov.id}.png", 2400)
    res["seconds"] = round(time.perf_counter() - t0, 1)
    return res


def level_comparison(refs: list) -> list[dict[str, Any]]:
    """Global and guided matching on two pairs at 16x, 32x and 64x: which resolution suits?"""
    rows = []
    keep = ("matches", "inliers", "angle_deg", "scale", "rms_similarity_um")
    for i_fix, i_mov in ((0, 1), (4, 5)):
        for ds in (16, 32, 64):
            t = time.perf_counter()
            fix, mov = load(refs[i_fix], ds), load(refs[i_mov], ds)
            b = boundary_align(mov, fix)
            row: dict[str, Any] = {"pair": f"{fix.id}<-{mov.id}", "downsample": ds}
            for kind in ("sift", "orb"):
                kpm, dm = detect(mov.reg, mov.mask, kind)
                kpf, df = detect(fix.reg, fix.mask, kind)
                glob = fit(kpm, kpf, match(dm, df, kind), mov.um_per_px)
                gd = guided_two_pass(kpm, dm, kpf, df, b["m"], mov.um_per_px, kind)
                row[f"{kind}_global"] = {k: glob.get(k) for k in keep}
                row[f"{kind}_guided"] = {k: gd.get(k) for k in keep}
            row["seconds"] = round(time.perf_counter() - t, 1)
            rows.append(row)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--slices", default="1-10")
    args = ap.parse_args()
    cfg = ProjectConfig.load(args.config)
    lo, hi = (int(v) for v in args.slices.split("-"))
    refs = Catalog.scan(cfg.data_dir, (lo, hi)).slices
    out = cfg.work_dir / "results" / "M3-06_registration_spike"
    out.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    slices = [load(r, 32) for r in refs]
    per_slice = [
        {
            "id": s.id,
            "size_px": list(s.rgb.shape[1::-1]),
            "um_per_px": round(s.um_per_px, 3),
            "tissue_fraction": round(float((s.mask > 0).mean()), 3),
            "tissue_area_mm2": round(float((s.mask > 0).sum()) * s.um_per_px**2 / 1e6, 1),
            "components": int(cv2.connectedComponents(s.mask)[0] - 1),
        }
        for s in slices
    ]
    print(f"loaded {len(slices)} slices at 32x in {time.perf_counter() - t0:.1f}s")
    contact_sheet(slices, out / "contact_sheet.png")

    pairs = [register_pair(slices[i - 1], slices[i], out) for i in range(1, len(slices))]
    levels = level_comparison(refs)
    results = {"slices": per_slice, "pairs": pairs, "level_comparison": levels}
    (out / "results.json").write_text(json.dumps(results, indent=2))

    print("\nslice  tissue%  area_mm2  parts")
    for s in per_slice:
        print(
            f"{s['id']:>5}  {100 * s['tissue_fraction']:6.1f}  {s['tissue_area_mm2']:8.1f}  {s['components']:5}"
        )
    print(
        "\npair    bnd_iou  SIFT inl/match   ORB inl/match  mirror  angle    scale   "
        "edge_um  rms_rigid  rms_sim  ovl_iou  bnd_vs_feat(deg,um)"
    )
    for p in pairs:
        s, o, b = p["sift"], p["orb"], p["boundary"]
        bf = p.get("boundary_vs_features", {})
        best = p[p["best"]]
        print(
            f"{p['fixed']:>2}<-{p['moving']:<3} {b['iou']:7.3f}  {s['inliers']:5}/{s['matches']:<7}  "
            f"{o['inliers']:5}/{o['matches']:<7} {p['mirrored_sift_inliers']:6}  "
            f"{best.get('angle_deg', float('nan')):6.2f}  {best.get('scale', float('nan')):.4f}  "
            f"{p.get('scale_edge_displacement_um', float('nan')):7.1f}  "
            f"{best.get('rms_rigid_um', float('nan')):8.1f}  {best.get('rms_similarity_um', float('nan')):7.1f}  "
            f"{p.get('overlay_iou', float('nan')):7.3f}  "
            f"({bf.get('angle_diff_deg', float('nan'))}, {bf.get('centroid_diff_um', float('nan'))})"
        )
    print("\nguided matching (inliers pass1 -> pass2, final fit of the better detector)")
    print("pair     SIFT p1->p2    ORB p1->p2   best  angle    scale   rms_sim  ovl_iou")
    for p in pairs:
        gs, go = p["guided_sift"], p["guided_orb"]
        best = p.get(f"guided_{p.get('guided_best', 'sift')}", {})
        print(
            f"{p['fixed']:>2}<-{p['moving']:<3} {gs.get('pass1_inliers', 0):5}->{gs['inliers']:<5}  "
            f"{go.get('pass1_inliers', 0):5}->{go['inliers']:<5}  {p.get('guided_best', '-'):>4}  "
            f"{best.get('angle_deg', float('nan')):6.2f}  {best.get('scale', float('nan')):.4f}  "
            f"{best.get('rms_similarity_um', float('nan')):7.1f}  {p.get('guided_overlay_iou', float('nan')):7.3f}"
        )
    print("\nlevel comparison (inliers; global vs guided):")
    for row in levels:
        cells = "  ".join(
            f"{kind} {row[f'{kind}_global']['inliers']}/{row[f'{kind}_guided']['inliers']} "
            f"(s={row[f'{kind}_guided']['scale']})"
            for kind in ("sift", "orb")
        )
        print(f"  {row['pair']:>6} {row['downsample']:>3}x  {cells}  {row['seconds']}s")
    print(f"\ntotal {time.perf_counter() - t0:.0f}s; outputs in {out}")


if __name__ == "__main__":
    main()
