import math

import numpy as np
import pytest

from brainrec.registration import Transform2D
from brainrec.registration.boundary import align_outlines, initial_from_masks
from brainrec.registration.geometry import mask_iou, umeyama_similarity
from tests.synthetic.tissue import UM_PER_PX, apply_similarity, make_tissue

MPP = (UM_PER_PX, UM_PER_PX)


@pytest.fixture(scope="module")
def sample():
    return make_tissue(seed=4)


def _px(truth: Transform2D) -> np.ndarray:
    return truth.to_pixels(1, 1, MPP)


def _error_px(m_px: np.ndarray, truth: Transform2D, mask: np.ndarray) -> float:
    ys, xs = np.nonzero(mask)
    pts = np.stack([xs, ys], 1)[::40].astype(float)
    t = _px(truth)
    return float(
        np.linalg.norm(
            pts @ m_px[:2, :2].T + m_px[:2, 2] - (pts @ t[:2, :2].T + t[:2, 2]), axis=1
        ).mean()
    )


def test_umeyama_recovers_similarity():
    rng = np.random.default_rng(0)
    src = rng.uniform(0, 500, (60, 2))
    truth = Transform2D.similarity(0.7, 1.13, 40.0, -25.0).matrix
    assert np.allclose(umeyama_similarity(src, src @ truth[:, :2].T + truth[:, 2]), truth)


def test_translation_only(sample):
    _, mm, truth = apply_similarity(sample.rgb, sample.mask, 0.0, 1.0, 400.0, -250.0)
    m, iou = initial_from_masks(sample.mask, mm)
    assert iou > 0.97 and _error_px(m, truth, mm) < 2.0


@pytest.mark.parametrize("angle, scale", [(45.0, 1.0), (170.0, 1.0), (-60.0, 1.08), (120.0, 0.92)])
def test_rotation_and_scale(sample, angle, scale):
    _, mm, truth = apply_similarity(sample.rgb, sample.mask, angle, scale, 100.0, 50.0)
    m, iou = initial_from_masks(sample.mask, mm)
    est = Transform2D.from_pixels(m, 1, 1, MPP)
    assert abs((est.angle_deg - truth.angle_deg + 180) % 360 - 180) < 10.0
    assert est.scale == pytest.approx(truth.scale, rel=0.03)
    assert iou > 0.85


def test_empty_mask_rejected(sample):
    with pytest.raises(ValueError, match="empty"):
        initial_from_masks(sample.mask, np.zeros_like(sample.mask))


def test_outline_alignment_refines_a_rough_start(sample):
    _, mm, truth = apply_similarity(sample.rgb, sample.mask, 30.0, 1.05, 200.0, 100.0)
    rough = truth.compose(Transform2D.similarity(math.radians(8), 0.95, 600.0, -400.0))
    start = _px(rough)
    assert _error_px(start, truth, mm) > 20
    m, iou = align_outlines(sample.mask, mm, start)
    assert _error_px(m, truth, mm) < 3.0
    assert iou == pytest.approx(mask_iou(sample.mask, mm, m)) and iou > 0.95
