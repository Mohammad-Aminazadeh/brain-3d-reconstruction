import cv2
import numpy as np
import pytest

from brainrec.qc.metrics import mask_dice, score_pair
from brainrec.registration import PairResult, RegImage, Transform2D
from tests.synthetic.tissue import DEBRIS, UM_PER_PX, apply_similarity, as_reg_image, make_tissue

SEEDS = [0, 1, 2, 3]


def _result(t: Transform2D, fixed: int = 1, moving: int = 2) -> PairResult:
    return PairResult(fixed, moving, t, "features", 500, 120, 17.5, 0.9)


def _shift(px: float) -> Transform2D:
    return Transform2D.similarity(0.0, 1.0, px * UM_PER_PX, 0.0)


def _section(seed: int, slice_id: int, detail_seed: int | None = None) -> RegImage:
    t = make_tissue(seed=seed, detail_seed=detail_seed)
    return as_reg_image(t.rgb, t.mask, slice_id)


@pytest.mark.parametrize("seed", SEEDS)
def test_identical_images_score_one(seed):
    s = score_pair(_section(seed, 1), _section(seed, 2), _result(Transform2D.identity()))
    assert (s.ncc, s.dice) == (1.0, 1.0)


def test_registration_numbers_are_passed_through():
    s = score_pair(_section(0, 1), _section(0, 2), _result(Transform2D.identity()))
    assert (s.n_inliers, s.rms_um) == (120, 17.5)
    no_fit = PairResult(1, 2, Transform2D.identity(), "boundary", 0, 0, None, 0.9)
    assert score_pair(_section(0, 1), _section(0, 2), no_fit).rms_um is None


@pytest.mark.parametrize("seed", SEEDS)
def test_ten_pixel_misalignment_scores_clearly_lower(seed):
    a = make_tissue(seed=seed)
    moved, moved_mask, truth = apply_similarity(a.rgb, a.mask, 0.0, 1.0, 10 * UM_PER_PX, 0.0)
    fixed, moving = as_reg_image(a.rgb, a.mask, 1), as_reg_image(moved, moved_mask, 2)
    right = score_pair(fixed, moving, _result(truth))
    wrong = score_pair(fixed, moving, _result(Transform2D.identity()))  # off by 10 px
    assert right.ncc > 0.99 and right.dice > 0.99
    assert wrong.ncc < right.ncc - 0.3  # measured 0.54 to 0.59
    assert wrong.dice < right.dice - 0.02  # measured 0.964 to 0.968


@pytest.mark.parametrize("seed", SEEDS)
def test_scores_follow_the_transform(seed):
    """The transform is applied: a moved, rotated and scaled section scores high with its true
    transform."""
    a = make_tissue(seed=seed)
    moved, moved_mask, truth = apply_similarity(a.rgb, a.mask, 30.0, 1.05, 100.0, -50.0)
    s = score_pair(
        as_reg_image(a.rgb, a.mask, 1), as_reg_image(moved, moved_mask, 2), _result(truth)
    )
    assert s.ncc > 0.95 and s.dice > 0.98


@pytest.mark.parametrize("seed", SEEDS)
def test_neighbouring_sections_rank_between_misaligned_and_unrelated(seed):
    """Neighbouring sections (same geometry, different cells): aligned > 10 px off > unrelated."""
    fixed, neighbour = _section(seed, 1), _section(seed, 2, detail_seed=7)
    aligned = score_pair(fixed, neighbour, _result(Transform2D.identity()))
    off = score_pair(fixed, neighbour, _result(_shift(10)))
    unrelated = score_pair(fixed, _section(seed + 10, 2), _result(Transform2D.identity()))
    assert aligned.ncc > off.ncc + 0.1 > unrelated.ncc + 0.2
    assert aligned.dice > off.dice > unrelated.dice


@pytest.mark.parametrize("seed", SEEDS)
def test_unrelated_images_score_lowest(seed):
    fixed = _section(seed, 1)
    ident = Transform2D.identity()
    unrelated = score_pair(fixed, _section(seed + 10, 2), _result(ident))
    others = [
        score_pair(fixed, _section(seed, 2), _result(ident)),
        score_pair(fixed, _section(seed, 2), _result(_shift(10))),
        score_pair(fixed, _section(seed, 2, detail_seed=7), _result(ident)),
    ]
    assert all(unrelated.ncc < s.ncc and unrelated.dice < s.dice for s in others)
    assert unrelated.ncc < 0.2


def test_only_tissue_counts():
    """F12: debris on the glass changes nothing (the registration image is 0 outside the mask)."""
    a = make_tissue(seed=0, n_debris=0)
    dirty = a.rgb.copy()
    glass = np.argwhere(cv2.dilate(a.mask, np.ones((31, 31), np.uint8)) == 0)
    for y, x in glass[:: len(glass) // 40][:40]:
        cv2.circle(dirty, (int(x), int(y)), 4, DEBRIS.tolist(), -1)
    assert (dirty != a.rgb).any()
    fixed = as_reg_image(a.rgb, a.mask, 1)
    t = _result(_shift(3))
    clean = score_pair(fixed, as_reg_image(a.rgb, a.mask, 2), t)
    assert score_pair(fixed, as_reg_image(dirty, a.mask, 2), t) == clean


def test_mask_dice_known_values():
    a = np.zeros((20, 20), np.uint8)
    a[:, :10] = 255
    ident = np.eye(3)
    assert mask_dice(a, a, ident) == 1.0
    shift5 = np.array([[1.0, 0, 5], [0, 1, 0], [0, 0, 1]])  # half of the moving tissue overlaps
    assert mask_dice(a, a, shift5) == pytest.approx(0.5)
    assert mask_dice(a, np.zeros_like(a), ident) == 0.0
    assert mask_dice(np.zeros_like(a), np.zeros_like(a), ident) == 0.0


def test_rejects_images_of_another_pair():
    with pytest.raises(ValueError, match="not pair 1<-2"):
        score_pair(_section(0, 1), _section(0, 3), _result(Transform2D.identity()))


def test_rejects_mismatched_levels():
    fixed, moving = _section(0, 1), _section(0, 2)
    coarse = RegImage(2, moving.image, moving.mask, 64.0, moving.mpp_um)
    with pytest.raises(ValueError, match="share pixel size and level"):
        score_pair(fixed, coarse, _result(Transform2D.identity()))
