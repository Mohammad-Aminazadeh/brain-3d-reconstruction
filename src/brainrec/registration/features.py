"""Keypoint features inside the tissue and guided matching between neighbouring slices (F13).

Spike result (docs/REQUIREMENTS.md section 7): a global ratio test fails on this uniform tissue,
so candidates are only compared near the position predicted by the current estimate.
"""

from dataclasses import dataclass

import cv2
import numpy as np

from brainrec.registration.geometry import apply_px, rms, umeyama_similarity
from brainrec.registration.types import RegImage

PLAUSIBLE_SCALE = (0.5, 2.0)  # sections differ by about 10%; outside this the fit is degenerate


@dataclass(frozen=True)
class Features:
    xy: np.ndarray  # (N, 2) keypoint positions, pixels of the image's level (OpenCV convention)
    descriptors: np.ndarray | None  # (N, 32) uint8 for ORB


@dataclass(frozen=True)
class Fit:
    matrix: np.ndarray  # 2x3 pixel map moving -> fixed
    n_matches: int
    n_inliers: int
    rms_px: float


def detect_features(img: RegImage, max_features: int, detector: str = "orb") -> Features:
    """Keypoints and descriptors, detected only where the tissue mask is set (F12)."""
    if detector != "orb":
        raise ValueError(f"unsupported detector {detector!r}")
    orb = cv2.ORB.create(max_features)
    kp, desc = orb.detectAndCompute(img.image, img.mask)
    # ORB maps keypoints of pyramid level l back by multiplying with s = factor**l, which shifts
    # them by 0.5 * (s - 1) px against the pixel-centre convention (C1); undo that bias here.
    s = orb.getScaleFactor() ** np.array([k.octave for k in kp], float)
    xy = np.array([k.pt for k in kp], float).reshape(-1, 2) + 0.5 * (s[:, None] - 1)
    return Features(xy, desc)


def guided_match(
    fixed: Features,
    moving: Features,
    predict: np.ndarray,
    window_px: float,
    ratio: float,
    k: int = 30,
) -> tuple[np.ndarray, np.ndarray]:
    """Matched (moving, fixed) positions: per moving keypoint, the best descriptor among fixed
    keypoints within `window_px` of the predicted position, kept if it passes the ratio test
    against the second best in the window (or is the only one there)."""
    if fixed.descriptors is None or moving.descriptors is None or len(fixed.xy) < 2:
        return np.empty((0, 2)), np.empty((0, 2))
    pred = apply_px(predict, moving.xy)
    knn = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(
        moving.descriptors, fixed.descriptors, k=min(k, len(fixed.xy))
    )
    src, dst = [], []
    for cands in knn:
        if not cands:
            continue
        q = cands[0].queryIdx
        near = [c for c in cands if np.hypot(*(fixed.xy[c.trainIdx] - pred[q])) < window_px]
        if len(near) == 1 or (len(near) >= 2 and near[0].distance < ratio * near[1].distance):
            src.append(moving.xy[q])
            dst.append(fixed.xy[near[0].trainIdx])
    return np.array(src, float).reshape(-1, 2), np.array(dst, float).reshape(-1, 2)


def fit_similarity(src: np.ndarray, dst: np.ndarray, ransac_px: float) -> Fit | None:
    """RANSAC similarity fit, then a least-squares refit on the inliers (precision step)."""
    if len(src) < 3:
        return None
    m, inl = cv2.estimateAffinePartial2D(
        src.astype(np.float32), dst.astype(np.float32), method=cv2.RANSAC,
        ransacReprojThreshold=ransac_px, maxIters=10000, confidence=0.999,
    )  # fmt: skip
    if m is None or inl is None or inl.sum() < 3:
        return None
    keep = inl.ravel().astype(bool)
    for _ in range(3):  # refit on inliers; re-select inliers with the refined model
        m = umeyama_similarity(src[keep], dst[keep])
        err = np.hypot(*(apply_px(m, src) - dst).T)
        new = err < ransac_px
        if new.sum() < 3 or np.array_equal(new, keep):
            break
        keep = new
    scale = float(np.hypot(m[0, 0], m[1, 0]))
    if not np.all(np.isfinite(m)) or not PLAUSIBLE_SCALE[0] <= scale <= PLAUSIBLE_SCALE[1]:
        return None  # degenerate point set (e.g. all matches on one line)
    return Fit(m, len(src), int(keep.sum()), rms(m, src[keep], dst[keep]))
