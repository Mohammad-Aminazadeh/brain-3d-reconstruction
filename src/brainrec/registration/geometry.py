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


def mask_iou(fixed: np.ndarray, moving: np.ndarray, m_moving_to_fixed: np.ndarray) -> float:
    """IoU of the fixed mask and the moving mask warped into the fixed frame."""
    warped = cv2.warpAffine(
        moving, m_moving_to_fixed[:2], fixed.shape[::-1], flags=cv2.INTER_NEAREST
    )
    a, b = fixed > 0, warped > 0
    union = int((a | b).sum())
    return float((a & b).sum() / union) if union else 0.0
