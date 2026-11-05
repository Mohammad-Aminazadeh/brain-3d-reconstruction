"""Register one slice onto its neighbour (M3-07, M3-13): boundary estimate, guided ORB matching
coarse-to-fine, a centre-weighted similarity fit, a retry with a wider window, and the C12 rules:
a pair that passes no check still gets the transform that fits its tissue centre best."""

import logging

import numpy as np

from brainrec.catalog import ProjectConfig
from brainrec.registration.boundary import align_outlines, initial_from_masks
from brainrec.registration.features import Fit, detect_features, fit_similarity, guided_match
from brainrec.registration.geometry import depth_weights, mask_iou, masked_ncc
from brainrec.registration.transform import Transform2D
from brainrec.registration.types import PairResult, RegImage, Status

log = logging.getLogger(__name__)


def _guided_pass(
    fixed: RegImage,
    moving: RegImage,
    estimate: Transform2D,
    window_um: float,
    ransac_px: float,
    cfg: ProjectConfig,
    weighted: bool = False,
) -> Fit | None:
    """One guided matching pass at the images' level, around `estimate` (moving -> fixed); with
    `weighted`, the refit weights matches by their depth in the tissue (decision 0004)."""
    mpp = fixed.mpp_um
    predict = estimate.to_pixels(moving.downsample, fixed.downsample, mpp)
    ff = detect_features(fixed, cfg.reg_max_features, cfg.reg_detector)
    fm = detect_features(moving, cfg.reg_max_features, cfg.reg_detector)
    src, dst = guided_match(ff, fm, predict, window_um / fixed.um_per_px, cfg.reg_ratio_test)
    weights = _match_weights(fixed, dst, cfg.reg_edge_weight) if weighted else None
    return fit_similarity(src, dst, ransac_px, weights)


def _match_weights(fixed: RegImage, dst: np.ndarray, edge_weight: float) -> np.ndarray:
    """Depth weight (decision 0004) of each match at its position in the fixed slice."""
    w = depth_weights(fixed.mask, edge_weight)
    x = np.clip(np.rint(dst[:, 0]).astype(int), 0, w.shape[1] - 1)
    y = np.clip(np.rint(dst[:, 1]).astype(int), 0, w.shape[0] - 1)
    return np.maximum(w[y, x], edge_weight)  # a match on the outline still counts as edge


def _feature_fits(
    fixed: RegImage,
    moving: RegImage,
    estimate: Transform2D,
    window_um: float,
    cfg: ProjectConfig,
) -> list[tuple[Transform2D, Fit]]:
    """Pass 1 (coarse, `window_um`) twice from `estimate`, then pass 2 (fine, narrow window)
    twice, once with centre-weighted refits and once without (decision 0004). Returns the
    candidates found, each as (transform, fit); empty if no pass found a fit.

    Both are kept because on weak pairs (real pair 3->4, about 80 inliers) the weighted refit can
    lead the second fine pass to a nearby but worse solution; the caller keeps the one that
    matches the tissue centre best.
    """
    fc, mc = fixed.coarse(cfg.reg_coarse_factor), moving.coarse(cfg.reg_coarse_factor)
    # pass 1 twice: the first run corrects the outline estimate with matches near the centre
    # (where the 1 mm window is wide enough); the second then finds matches across the section
    fit = None
    for _ in range(2):
        coarse = _guided_pass(fc, mc, estimate, window_um, cfg.reg_ransac_px, cfg)
        if coarse is None:
            break
        fit, estimate = coarse, _to_um(coarse, fc, mc)
    out = []
    for weighted in (True, False):
        f, t = fit, estimate
        for _ in range(2):
            fine = _guided_pass(
                fixed, moving, t, cfg.reg_window_um[1], cfg.reg_ransac_px, cfg, weighted
            )
            if fine is None:
                break
            f, t = fine, _to_um(fine, fixed, moving)
        if f is not None:
            out.append((t, f))
    return out


def _to_um(fit: Fit, fixed: RegImage, moving: RegImage) -> Transform2D:
    return Transform2D.from_pixels(fit.matrix, moving.downsample, fixed.downsample, fixed.mpp_um)


def register_pair(fixed: RegImage, moving: RegImage, cfg: ProjectConfig) -> PairResult:
    """Transform mapping `moving` onto `fixed` (µm), with status per contract C12."""
    if fixed.mpp_um != moving.mpp_um or fixed.downsample != moving.downsample:
        raise ValueError("fixed and moving must share pixel size and level")
    mpp = fixed.mpp_um
    fc, mc = fixed.coarse(cfg.reg_coarse_factor), moving.coarse(cfg.reg_coarse_factor)

    # boundary-based estimate from the tissue shapes (at the coarse level: cheap)
    init_px, _ = initial_from_masks(fc.mask, mc.mask)
    boundary_estimate = Transform2D.from_pixels(init_px, mc.downsample, fc.downsample, mpp)

    def result(t: Transform2D, status: Status, f: Fit | None) -> PairResult:
        m = t.to_pixels(moving.downsample, fixed.downsample, mpp)
        return PairResult(
            fixed.slice_id, moving.slice_id, t, status,
            f.n_matches if f else 0, f.n_inliers if f else 0,
            round(f.rms_px * fixed.um_per_px, 3) if f else None,
            round(mask_iou(fixed.mask, moving.mask, m), 4),
        )  # fmt: skip

    def ncc(t: Transform2D) -> float:  # centre-weighted (decision 0004)
        m = t.to_pixels(moving.downsample, fixed.downsample, mpp)
        return masked_ncc(
            fixed.image, fixed.mask, moving.image, moving.mask, m, edge_weight=cfg.reg_edge_weight
        )

    def accepted(f: Fit, t: Transform2D) -> bool:
        return (
            f.n_inliers >= cfg.reg_min_inliers
            and f.rms_px * fixed.um_per_px <= cfg.reg_max_rms_um
            and result(t, "features", f).mask_iou >= cfg.boundary_min_iou  # C12: plausible
            # C12: the feature fit must explain the images at least as well as the outline
            # estimate (a precise fit of one region of an unevenly deformed section can be worse)
            and ncc(t) >= ncc(boundary_estimate) - cfg.reg_ncc_tolerance
        )

    # the normal pass-1 window, then once more with a wider one (C12, M3-13)
    attempts: list[tuple[Transform2D, Fit]] = []
    for window_um in dict.fromkeys((cfg.reg_window_um[0], cfg.reg_retry_window_um)):
        found = _feature_fits(fixed, moving, boundary_estimate, window_um, cfg)
        passed = [(t, f) for t, f in found if accepted(f, t)]
        if passed:  # the one that matches the tissue centre best (decision 0004)
            t_feat, f_feat = max(passed, key=lambda c: ncc(c[0]))
            return result(t_feat, "features", f_feat)
        attempts += found

    # no feature fit passed: align the outlines, starting from the boundary estimate (C12), and
    # keep the candidate that matches the tissue best, centre first (decision 0004)
    start = boundary_estimate.to_pixels(moving.downsample, fixed.downsample, mpp)
    m_out, _ = align_outlines(fixed.mask, moving.mask, start)
    outline = Transform2D.from_pixels(m_out, moving.downsample, fixed.downsample, mpp)
    first_fit = attempts[0][1] if attempts else None
    candidates = [*attempts, (outline, first_fit), (boundary_estimate, first_fit)]
    best, best_fit = max(candidates, key=lambda c: ncc(c[0]))  # ties: the earlier candidate
    chosen = result(best, "boundary", best_fit)
    if chosen.mask_iou >= cfg.boundary_min_iou:  # plausible: the tissues overlap as expected
        log.warning(
            "pair %d<-%d: features insufficient, using the best match of the tissue centre",
            fixed.slice_id,
            moving.slice_id,
        )
        return chosen
    log.warning(
        "pair %d<-%d: registration failed; using the best match of the tissue centre anyway",
        fixed.slice_id,
        moving.slice_id,
    )
    return result(best, "failed", best_fit)
