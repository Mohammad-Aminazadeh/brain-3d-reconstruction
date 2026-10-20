"""Small geometric helpers in pixel space: similarity fits and mask overlap."""

import cv2
import numpy as np


def apply_px(m: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """Apply a 2x3 (or 3x3) pixel matrix to (N, 2) points."""
    return np.asarray(pts, float) @ m[:2, :2].T + m[:2, 2]


def umeyama_similarity(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Least-squares similarity (rotation, uniform scale, translation), 2x3, mapping src -> dst."""
    src, dst = np.asarray(src, float), np.asarray(dst, float)
    ms, md = src.mean(0), dst.mean(0)
    s0, d0 = src - ms, dst - md
    u, sig, vt = np.linalg.svd(d0.T @ s0 / len(src))
    d = np.diag([1.0, np.sign(np.linalg.det(u @ vt))])
    rot = u @ d @ vt
    scale = float((sig * np.diag(d)).sum() / s0.var(0).sum())
    return np.hstack([scale * rot, (md - scale * rot @ ms)[:, None]])


def rms(m: np.ndarray, src: np.ndarray, dst: np.ndarray) -> float:
    return float(np.sqrt(((apply_px(m, src) - dst) ** 2).sum(1).mean())) if len(src) else np.inf


def masked_ncc(
    fixed: np.ndarray,
    fixed_mask: np.ndarray,
    moving: np.ndarray,
    moving_mask: np.ndarray,
    m_moving_to_fixed: np.ndarray,
    blur_px: float = 3.0,
) -> float:
    """Normalised cross-correlation of the two images where both tissues overlap, after a light
    blur (cells differ between sections; vessels, bands and edges should correlate)."""
    size = fixed.shape[::-1]
    warped = cv2.warpAffine(moving, m_moving_to_fixed[:2], size, flags=cv2.INTER_LINEAR)
    wmask = cv2.warpAffine(moving_mask, m_moving_to_fixed[:2], size, flags=cv2.INTER_NEAREST)
    sel = (fixed_mask > 0) & (wmask > 0)
    if sel.sum() < 100:
        return -1.0
    a = cv2.GaussianBlur(fixed.astype(np.float32), (0, 0), blur_px)[sel]
    b = cv2.GaussianBlur(warped.astype(np.float32), (0, 0), blur_px)[sel]
    a, b = a - a.mean(), b - b.mean()
    denom = float(np.sqrt((a * a).sum() * (b * b).sum()))
    return float((a * b).sum() / denom) if denom > 0 else -1.0


def mask_iou(fixed: np.ndarray, moving: np.ndarray, m_moving_to_fixed: np.ndarray) -> float:
    """IoU of the fixed mask and the moving mask warped into the fixed frame."""
    warped = cv2.warpAffine(
        moving, m_moving_to_fixed[:2], fixed.shape[::-1], flags=cv2.INTER_NEAREST
    )
    a, b = fixed > 0, warped > 0
    union = int((a | b).sum())
    return float((a & b).sum() / union) if union else 0.0
