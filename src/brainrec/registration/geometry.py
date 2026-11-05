"""Small geometric helpers in pixel space: similarity fits, mask overlap and depth weights."""

import cv2
import numpy as np


def apply_px(m: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """Apply a 2x3 (or 3x3) pixel matrix to (N, 2) points."""
    return np.asarray(pts, float) @ m[:2, :2].T + m[:2, 2]


def umeyama_similarity(
    src: np.ndarray, dst: np.ndarray, weights: np.ndarray | None = None
) -> np.ndarray:
    """Least-squares similarity (rotation, uniform scale, translation), 2x3, mapping src -> dst.

    With `weights` (one per point, >= 0) the weighted squared error is minimised, so points of
    high weight are fitted more closely.
    """
    src, dst = np.asarray(src, float), np.asarray(dst, float)
    w = np.ones(len(src)) if weights is None else np.asarray(weights, float)
    w = w / w.sum()
    ms, md = w @ src, w @ dst
    s0, d0 = src - ms, dst - md
    u, sig, vt = np.linalg.svd((d0 * w[:, None]).T @ s0)
    d = np.diag([1.0, np.sign(np.linalg.det(u @ vt))])
    rot = u @ d @ vt
    scale = float((sig * np.diag(d)).sum() / (w @ (s0**2).sum(1)))
    return np.hstack([scale * rot, (md - scale * rot @ ms)[:, None]])


def depth_weights(mask: np.ndarray, edge_weight: float) -> np.ndarray:
    """Weight per pixel of `mask`, rising linearly with the depth in the tissue (distance to its
    edge) from `edge_weight` at the edge to 1 at the deepest point, the centre; 0 outside.

    The centre must align best, errors may grow towards the edge, which still counts (owner,
    2026-11-04; decision 0004).
    """
    depth = cv2.distanceTransform((mask > 0).astype(np.uint8), cv2.DIST_L2, 5)
    peak = float(depth.max())
    if peak == 0:
        return np.zeros(mask.shape, np.float32)
    w = edge_weight + (1.0 - edge_weight) * (depth - 1.0) / max(peak - 1.0, 1.0)
    return np.where(mask > 0, w, 0.0).astype(np.float32)


def rms(m: np.ndarray, src: np.ndarray, dst: np.ndarray) -> float:
    return float(np.sqrt(((apply_px(m, src) - dst) ** 2).sum(1).mean())) if len(src) else np.inf


def masked_ncc(
    fixed: np.ndarray,
    fixed_mask: np.ndarray,
    moving: np.ndarray,
    moving_mask: np.ndarray,
    m_moving_to_fixed: np.ndarray,
    blur_px: float = 3.0,
    edge_weight: float | None = None,
) -> float:
    """Normalised cross-correlation of the two images where both tissues overlap, after a light
    blur (cells differ between sections; vessels, bands and edges should correlate).

    With `edge_weight`, pixels are weighted by their depth in the overlap (`depth_weights`), so
    the centre counts most; without it all overlap pixels count equally.
    """
    size = fixed.shape[::-1]
    warped = cv2.warpAffine(moving, m_moving_to_fixed[:2], size, flags=cv2.INTER_LINEAR)
    wmask = cv2.warpAffine(moving_mask, m_moving_to_fixed[:2], size, flags=cv2.INTER_NEAREST)
    overlap = (fixed_mask > 0) & (wmask > 0)
    if overlap.sum() < 100:
        return -1.0
    w = (
        np.ones(int(overlap.sum()))
        if edge_weight is None
        else depth_weights(overlap.astype(np.uint8), edge_weight)[overlap].astype(float)
    )
    a = cv2.GaussianBlur(fixed.astype(np.float32), (0, 0), blur_px)[overlap].astype(float)
    b = cv2.GaussianBlur(warped.astype(np.float32), (0, 0), blur_px)[overlap].astype(float)
    a, b = a - w @ a / w.sum(), b - w @ b / w.sum()
    denom = float(np.sqrt((w @ (a * a)) * (w @ (b * b))))
    return float(w @ (a * b) / denom) if denom > 0 else -1.0


def mask_iou(fixed: np.ndarray, moving: np.ndarray, m_moving_to_fixed: np.ndarray) -> float:
    """IoU of the fixed mask and the moving mask warped into the fixed frame."""
    warped = cv2.warpAffine(
        moving, m_moving_to_fixed[:2], fixed.shape[::-1], flags=cv2.INTER_NEAREST
    )
    a, b = fixed > 0, warped > 0
    union = int((a | b).sum())
    return float((a & b).sum() / union) if union else 0.0
