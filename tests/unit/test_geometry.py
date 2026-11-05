"""Centre weighting (M3-13, decision 0004): depth weights, weighted fit, weighted correlation."""

import cv2
import numpy as np
import pytest

from brainrec.registration.geometry import apply_px, depth_weights, masked_ncc, umeyama_similarity


def _disc(shape=(201, 201), radius=80) -> np.ndarray:
    mask = np.zeros(shape, np.uint8)
    cv2.circle(mask, (shape[1] // 2, shape[0] // 2), radius, 255, -1)
    return mask


def test_depth_weights_rise_from_edge_to_centre():
    mask = _disc()
    w = depth_weights(mask, 0.2)
    assert w[100, 100] == pytest.approx(1.0, abs=0.02)  # centre
    assert w[100, 20] == pytest.approx(0.2, abs=0.02)  # edge pixel
    assert not w[mask == 0].any()  # outside: 0
    row = w[100, 20:101]
    assert np.all(np.diff(row) >= 0)  # rising towards the centre
    assert w[mask > 0].min() >= 0.2 - 1e-6 and w.max() <= 1.0 + 1e-6


def test_depth_weights_of_empty_mask():
    assert not depth_weights(np.zeros((10, 10), np.uint8), 0.2).any()


def test_weighted_fit_recovers_exact_transform():
    rng = np.random.default_rng(0)
    src = rng.uniform(0, 500, (50, 2))
    truth = np.array([[0.98, -0.17, 12.0], [0.17, 0.98, -7.0]])
    w = rng.uniform(0.1, 1.0, 50)
    assert np.allclose(umeyama_similarity(src, apply_px(truth, src), w), truth)
    assert np.allclose(umeyama_similarity(src, apply_px(truth, src)), truth)  # unweighted


def test_weighted_fit_follows_the_heavy_points():
    """Centre points move by one transform, edge points by another: the weighted fit stays close
    to the centre's transform, the unweighted fit compromises."""
    rng = np.random.default_rng(1)
    centre = rng.uniform(200, 300, (40, 2))
    edge = np.vstack([rng.uniform(0, 60, (20, 2)), rng.uniform(440, 500, (20, 2))])
    shift = np.array([[1.0, 0, 0], [0, 1, 0]])
    off = np.array([[1.0, 0, 6.0], [0, 1, 0]])  # edge displaced by 6 px
    src = np.vstack([centre, edge])
    dst = np.vstack([apply_px(shift, centre), apply_px(off, edge)])
    w = np.r_[np.ones(40), np.full(40, 0.05)]

    def centre_error(m):
        return np.linalg.norm(apply_px(m, centre) - centre, axis=1).mean()

    assert centre_error(umeyama_similarity(src, dst, w)) < 0.2 * centre_error(
        umeyama_similarity(src, dst)
    )


def test_weighted_ncc_of_identical_images_is_one():
    mask = _disc()
    img = np.where(mask > 0, np.random.default_rng(2).integers(50, 250, mask.shape), 0)
    img = img.astype(np.uint8)
    assert masked_ncc(img, mask, img, mask, np.eye(3), edge_weight=0.2) == pytest.approx(1.0)


def test_weighted_ncc_counts_the_centre_most():
    """Images that agree in the centre and disagree at the edge score higher with weights."""
    rng = np.random.default_rng(3)
    mask = _disc()
    a = cv2.GaussianBlur(rng.uniform(0, 255, mask.shape).astype(np.float32), (0, 0), 4)
    b = a.copy()
    rim = (mask > 0) & (depth_weights(mask, 0.0) < 0.4)
    b[rim] = cv2.GaussianBlur(rng.uniform(0, 255, mask.shape).astype(np.float32), (0, 0), 4)[rim]
    a8 = cv2.normalize(a, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
    b8 = cv2.normalize(b, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
    a8[mask == 0] = 0
    b8[mask == 0] = 0
    plain = masked_ncc(a8, mask, b8, mask, np.eye(3))
    weighted = masked_ncc(a8, mask, b8, mask, np.eye(3), edge_weight=0.2)
    assert weighted > plain + 0.05  # measured 0.64 vs 0.55
