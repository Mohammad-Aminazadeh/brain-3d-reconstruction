"""Quality numbers of one registered pair (M4-01), inside the tissue only (F12)."""

from dataclasses import dataclass

import cv2
import numpy as np

from brainrec.registration import PairResult, RegImage
from brainrec.registration.geometry import masked_ncc


@dataclass(frozen=True)
class PairScore:
    """How well `moving` lies on `fixed` after registration.

    `n_inliers` and `rms_um` (residual of the feature fit, µm; None without a fit) come from
    registration. `ncc` is the normalised cross-correlation of the two registration images where
    both tissues overlap (1 = identical, -1 if they hardly overlap); `dice` the Dice coefficient
    of the fixed mask and the warped moving mask (1 = same tissue outline, 0 = disjoint).
    """

    n_inliers: int
    rms_um: float | None
    ncc: float
    dice: float


def mask_dice(fixed: np.ndarray, moving: np.ndarray, m_moving_to_fixed: np.ndarray) -> float:
    """Dice of the fixed mask and the moving mask warped into the fixed frame (pixel matrix)."""
    warped = cv2.warpAffine(
        moving, m_moving_to_fixed[:2], fixed.shape[::-1], flags=cv2.INTER_NEAREST
    )
    a, b = fixed > 0, warped > 0
    total = int(a.sum() + b.sum())
    return float(2 * (a & b).sum() / total) if total else 0.0


def score_pair(fixed: RegImage, moving: RegImage, result: PairResult) -> PairScore:
    """Score `result` (moving -> fixed) on the two registration images and masks (C4).

    Raises ValueError if the images are not the pair of `result` or differ in pixel size or level.
    """
    if (fixed.slice_id, moving.slice_id) != (result.fixed, result.moving):
        raise ValueError(
            f"images {fixed.slice_id}<-{moving.slice_id} are not pair "
            f"{result.fixed}<-{result.moving}"
        )
    if fixed.mpp_um != moving.mpp_um or fixed.downsample != moving.downsample:
        raise ValueError("fixed and moving must share pixel size and level")
    m = result.transform.to_pixels(moving.downsample, fixed.downsample, fixed.mpp_um)
    ncc = masked_ncc(fixed.image, fixed.mask, moving.image, moving.mask, m)
    return PairScore(
        result.n_inliers,
        result.rms_um,
        round(ncc, 4),
        round(mask_dice(fixed.mask, moving.mask, m), 4),
    )
