import numpy as np
import pytest

from brainrec.registration import Transform2D
from brainrec.registration.features import Features, detect_features, fit_similarity, guided_match
from tests.synthetic.tissue import as_reg_image, make_tissue


@pytest.fixture(scope="module")
def reg():
    s = make_tissue(seed=2)
    return as_reg_image(s.rgb, s.mask), s


def test_keypoints_only_inside_tissue(reg):
    img, s = reg
    f = detect_features(img, 5000)
    assert len(f.xy) >= 100 and f.descriptors is not None and len(f.descriptors) == len(f.xy)
    xi, yi = np.clip(f.xy.round().astype(int), 0, [s.mask.shape[1] - 1, s.mask.shape[0] - 1]).T
    assert (s.mask[yi, xi] > 0).mean() > 0.99  # sub-pixel positions may touch the edge
    assert not (s.debris[yi, xi] > 0).any()


def test_unknown_detector_rejected(reg):
    with pytest.raises(ValueError, match="detector"):
        detect_features(reg[0], 100, "sift")


def _feats(xy, desc):
    return Features(np.array(xy, float), np.array(desc, np.uint8))


def test_guided_match_only_considers_the_window():
    d = np.zeros((1, 32), np.uint8)
    moving = _feats([[10.0, 10.0]], d)
    fixed = _feats([[110.0, 10.0], [500.0, 500.0]], np.vstack([d, d]))  # identical descriptors
    shift = Transform2D.similarity(0.0, 1.0, 100.0, 0.0).matrix
    src, dst = guided_match(fixed, moving, shift, window_px=20.0, ratio=0.8)
    assert np.allclose(dst, [[110.0, 10.0]])  # the far twin is outside the window
    src, dst = guided_match(fixed, moving, np.eye(2, 3), window_px=20.0, ratio=0.8)
    assert len(src) == 0  # nothing predicted near either fixed keypoint


def test_guided_match_ratio_test_inside_window():
    d0, d1 = np.zeros((1, 32), np.uint8), np.ones((1, 32), np.uint8)
    moving = _feats([[0.0, 0.0]], d0)
    fixed = _feats([[1.0, 0.0], [2.0, 0.0]], np.vstack([d0, d0]))  # ambiguous: equally good
    assert len(guided_match(fixed, moving, np.eye(2, 3), 10.0, 0.8)[0]) == 0
    fixed = _feats([[1.0, 0.0], [2.0, 0.0]], np.vstack([d0, d1 * 255]))  # clear winner
    assert len(guided_match(fixed, moving, np.eye(2, 3), 10.0, 0.8)[0]) == 1


def test_fit_similarity_with_outliers():
    rng = np.random.default_rng(1)
    src = rng.uniform(0, 800, (300, 2))
    truth = Transform2D.similarity(0.3, 0.94, 25.0, -40.0).matrix
    dst = src @ truth[:, :2].T + truth[:, 2] + rng.normal(0, 0.3, src.shape)
    bad = rng.random(300) < 0.3
    dst[bad] = rng.uniform(0, 800, (int(bad.sum()), 2))
    fit = fit_similarity(src, dst, 3.0)
    assert fit is not None and abs(fit.n_inliers - (~bad).sum()) <= 3
    est = src @ fit.matrix[:, :2].T + fit.matrix[:, 2]
    exact = src @ truth[:, :2].T + truth[:, 2]
    assert np.abs(est - exact).max() < 0.3 and fit.rms_px < 0.6


def test_fit_similarity_needs_three_points():
    assert fit_similarity(np.zeros((2, 2)), np.zeros((2, 2)), 3.0) is None
