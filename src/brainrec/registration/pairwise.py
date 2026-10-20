"""Register one slice onto its neighbour (M3-07): boundary estimate, guided ORB matching
coarse-to-fine, similarity fit, outline fallback and the C12 acceptance rules."""

import logging

from brainrec.catalog import ProjectConfig
from brainrec.registration.boundary import align_outlines, initial_from_masks
from brainrec.registration.features import Fit, detect_features, fit_similarity, guided_match
from brainrec.registration.geometry import mask_iou
from brainrec.registration.transform import Transform2D
from brainrec.registration.types import PairResult, RegImage, Status

log = logging.getLogger(__name__)


def _guided_pass(
    fixed: RegImage, moving: RegImage, estimate: Transform2D, window_um: float, cfg: ProjectConfig
) -> Fit | None:
    """One guided matching pass at the images' level, around `estimate` (moving -> fixed)."""
    mpp = fixed.mpp_um
    predict = estimate.to_pixels(moving.downsample, fixed.downsample, mpp)
    ff = detect_features(fixed, cfg.reg_max_features, cfg.reg_detector)
    fm = detect_features(moving, cfg.reg_max_features, cfg.reg_detector)
    src, dst = guided_match(ff, fm, predict, window_um / fixed.um_per_px, cfg.reg_ratio_test)
    return fit_similarity(src, dst, cfg.reg_ransac_px)


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
    estimate = Transform2D.from_pixels(init_px, mc.downsample, fc.downsample, mpp)
    boundary_estimate = estimate

    # pass 1 (coarse, wide window), pass 2 (fine, narrow window, once more to refine)
    fit = _guided_pass(fc, mc, estimate, cfg.reg_window_um[0], cfg)
    if fit is not None:
        estimate = _to_um(fit, fc, mc)
    for _ in range(2):
        fine = _guided_pass(fixed, moving, estimate, cfg.reg_window_um[1], cfg)
        if fine is None:
            break
        fit, estimate = fine, _to_um(fine, fixed, moving)

    def result(t: Transform2D, status: Status, f: Fit | None) -> PairResult:
        m = t.to_pixels(moving.downsample, fixed.downsample, mpp)
        return PairResult(
            fixed.slice_id, moving.slice_id, t, status,
            f.n_matches if f else 0, f.n_inliers if f else 0,
            round(f.rms_px * fixed.um_per_px, 3) if f else None,
            round(mask_iou(fixed.mask, moving.mask, m), 4),
        )  # fmt: skip

    if (
        fit is not None
        and fit.n_inliers >= cfg.reg_min_inliers
        and fit.rms_px * fixed.um_per_px <= cfg.reg_max_rms_um
        and result(estimate, "features", fit).mask_iou >= cfg.boundary_min_iou  # C12: plausible
    ):
        return result(estimate, "features", fit)

    # fallback: align the outlines, starting from the boundary estimate (C12)
    start = boundary_estimate.to_pixels(moving.downsample, fixed.downsample, mpp)
    m_out, iou = align_outlines(fixed.mask, moving.mask, start)
    outline = Transform2D.from_pixels(m_out, moving.downsample, fixed.downsample, mpp)
    if iou >= cfg.boundary_min_iou:
        log.warning(
            "pair %d<-%d: features insufficient, using outline alignment",
            fixed.slice_id,
            moving.slice_id,
        )
        return result(outline, "boundary", fit)
    log.warning("pair %d<-%d: registration failed", fixed.slice_id, moving.slice_id)
    return result(estimate if fit is not None else outline, "failed", fit)
