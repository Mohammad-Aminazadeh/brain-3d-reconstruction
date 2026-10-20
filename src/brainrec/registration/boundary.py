"""Alignment from the tissue outlines alone (M3-07a initial estimate, M3-07d fallback).

All matrices here are pixel maps moving -> fixed at the images' own level; callers convert to
µm with `Transform2D.from_pixels` (contract C1).
"""

import math

import cv2
import numpy as np

from brainrec.registration.geometry import apply_px, mask_iou, umeyama_similarity


def _moments(mask: np.ndarray) -> tuple[np.ndarray, float, float]:
    mo = cv2.moments(mask, binaryImage=True)
    if mo["m00"] == 0:
        raise ValueError("empty mask")
    c = np.array([mo["m10"] / mo["m00"], mo["m01"] / mo["m00"]])
    theta = 0.5 * math.atan2(2 * mo["mu11"], mo["mu20"] - mo["mu02"])
    return c, theta, mo["m00"]


def _similarity_px(angle: float, scale: float, c_from: np.ndarray, c_to: np.ndarray) -> np.ndarray:
    rot = scale * np.array(
        [[math.cos(angle), -math.sin(angle)], [math.sin(angle), math.cos(angle)]]
    )
    return np.hstack([rot, (c_to - rot @ c_from)[:, None]])


def initial_from_masks(fixed: np.ndarray, moving: np.ndarray) -> tuple[np.ndarray, float]:
    """Centroids, area-ratio scale and the best of several angles (principal axes, both
    directions, plus a 10° sweep for nearly round shapes), refined in 1° steps."""
    cf, tf, af = _moments(fixed)
    cm, tm, am = _moments(moving)
    scale = math.sqrt(af / am)
    angles = [tf - tm, tf - tm + math.pi] + [math.radians(a) for a in range(0, 360, 10)]

    def score(a: float) -> float:
        return mask_iou(fixed, moving, _similarity_px(a, scale, cm, cf))

    best = max(angles, key=score)
    best = max((best + math.radians(d) for d in range(-5, 6)), key=score)
    m = _similarity_px(best, scale, cm, cf)
    return m, mask_iou(fixed, moving, m)


def _outline(mask: np.ndarray, n: int = 800) -> np.ndarray:
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    pts = np.vstack([c.reshape(-1, 2) for c in contours]).astype(float)
    step = max(1, len(pts) // n)
    return pts[::step]


def align_outlines(
    fixed: np.ndarray, moving: np.ndarray, init: np.ndarray, iterations: int = 150
) -> tuple[np.ndarray, float]:
    """Iterative closest point on the two outlines with a trimmed similarity fit."""
    pf, pm = _outline(fixed), _outline(moving)
    m = init[:2].copy()
    for _ in range(iterations):
        moved = apply_px(m, pm)
        d2 = ((moved[:, None, :] - pf[None, :, :]) ** 2).sum(2)
        nearest = d2.argmin(1)
        dist = d2[np.arange(len(pm)), nearest]
        keep = dist <= np.quantile(dist, 0.8)  # trim the worst 20% (torn or folded edges)
        new = umeyama_similarity(pm[keep], pf[nearest[keep]])
        if np.abs(new - m).max() < 1e-4:
            m = new
            break
        m = new
    return m, mask_iou(fixed, moving, m)
