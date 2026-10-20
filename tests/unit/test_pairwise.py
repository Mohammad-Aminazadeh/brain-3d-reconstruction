import json

import cv2
import numpy as np
import pytest

from brainrec.catalog import ProjectConfig
from brainrec.registration import PairResult, RegImage, Transform2D
from brainrec.registration.pairwise import register_pair
from tests.synthetic.tissue import UM_PER_PX, apply_similarity, as_reg_image, make_tissue, perturb

CFG = ProjectConfig(data_dir=".", work_dir=".", z_spacing_um=5.0)


def _errors(result: PairResult, truth: Transform2D, mask: np.ndarray):
    ys, xs = np.nonzero(mask)
    pts = np.stack([xs, ys], 1)[::40] * UM_PER_PX
    d = np.linalg.norm(result.transform.apply(pts) - truth.apply(pts), axis=1) / UM_PER_PX
    angle = abs((result.transform.angle_deg - truth.angle_deg + 180) % 360 - 180)
    return angle, abs(result.transform.scale / truth.scale - 1), d


def _pair(seed, angle, scale, neighbour=False, **kw):
    a = make_tissue(seed=seed, detail_seed=1, **kw)
    b = make_tissue(seed=seed, detail_seed=2, **kw) if neighbour else a
    moved, mm, truth = apply_similarity(b.rgb, b.mask, angle, scale, 150.0, -90.0)
    fixed = as_reg_image(a.rgb, a.mask, 1)
    return fixed, as_reg_image(perturb(moved, mm, seed), mm, 2), truth, mm


# AC4: 5 seeded pairs (same section, moved and perturbed): 1 px, 0.5°, 0.3% scale
@pytest.mark.parametrize(
    "seed, angle, scale",
    [(0, 45.0, 1.10), (1, -170.0, 0.90), (2, 8.0, 1.0), (3, 170.0, 1.05), (4, -45.0, 0.95)],
)
def test_ac4_recovers_known_similarity(seed, angle, scale):
    fixed, moving, truth, mm = _pair(seed, angle, scale)
    r = register_pair(fixed, moving, CFG)
    da, ds, d = _errors(r, truth, mm)
    assert r.status == "features" and r.n_inliers >= CFG.reg_min_inliers
    assert da < 0.5 and ds < 0.003 and d.max() < 1.0


# robustness: neighbouring sections (different cells); tolerance from the real-data residual
@pytest.mark.parametrize("seed, angle, scale", [(0, 45.0, 1.10), (1, -170.0, 0.90), (4, 8.0, 1.0)])
def test_neighbouring_sections_within_real_residual(seed, angle, scale):
    fixed, moving, truth, mm = _pair(seed, angle, scale, neighbour=True)
    r = register_pair(fixed, moving, CFG)
    da, ds, d = _errors(r, truth, mm)
    assert r.status == "features" and r.rms_um is not None and r.rms_um <= CFG.reg_max_rms_um
    assert d.mean() < 2.5 and da < 1.0 and ds < 0.01


def test_debris_does_not_change_the_result():
    with_debris = register_pair(*_pair(2, 30.0, 1.02, n_debris=40)[:2], CFG)
    without = register_pair(*_pair(2, 30.0, 1.02, n_debris=0)[:2], CFG)
    assert with_debris.transform.allclose(without.transform, atol_um=1e-9)


def test_texture_free_pair_falls_back_to_outlines():
    fixed, _, truth, mm = _pair(3, 20.0, 1.03)
    blank = RegImage(2, np.where(mm > 0, 128, 0).astype(np.uint8), mm, 32.0, (0.2205, 0.2205))
    r = register_pair(fixed, blank, CFG)
    _, _, d = _errors(r, truth, mm)
    assert r.status == "boundary" and d.mean() < 3.0 and r.mask_iou >= CFG.boundary_min_iou


def test_unrelated_pair_is_reported_failed():
    a, b = make_tissue(seed=0), make_tissue(seed=9)
    squashed = cv2.resize(b.rgb, (1000, 480))  # a clearly different shape
    sq_mask = cv2.resize(b.mask, (1000, 480), interpolation=cv2.INTER_NEAREST)
    rgb = np.vstack(
        [np.full((160, 1000, 3), 236, np.uint8), squashed, np.full((160, 1000, 3), 236, np.uint8)]
    )
    mask = np.vstack([np.zeros((160, 1000), np.uint8), sq_mask, np.zeros((160, 1000), np.uint8)])
    r = register_pair(as_reg_image(a.rgb, a.mask, 1), as_reg_image(rgb, mask, 2), CFG)
    assert r.status == "failed"


def test_mismatched_levels_rejected():
    fixed, moving, _, _ = _pair(0, 0.0, 1.0)
    other = RegImage(2, moving.image, moving.mask, 64.0, moving.mpp_um)
    with pytest.raises(ValueError, match="level"):
        register_pair(fixed, other, CFG)


def test_pair_result_json_round_trip():
    r = PairResult(4, 5, Transform2D.similarity(0.1, 1.01, 3.0, 4.0), "boundary", 12, 0, None, 0.91)
    back = PairResult.from_dict(json.loads(json.dumps(r.to_dict())))
    assert (
        back.transform.allclose(r.transform) and back.status == "boundary" and back.rms_um is None
    )
    with pytest.raises(ValueError, match="schema"):
        PairResult.from_dict({**r.to_dict(), "schema": "x"})


def test_coarse_image_halves_resolution():
    fixed, _, _, _ = _pair(0, 0.0, 1.0)
    c = fixed.coarse(2)
    assert c.image.shape == (400, 500) and c.downsample == 64.0
    assert set(np.unique(c.mask)) <= {0, 255} and (c.image[c.mask == 0] == 0).all()
