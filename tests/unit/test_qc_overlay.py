import cv2
import numpy as np
import pytest

from brainrec.qc.overlay import overlay_rgb, render_overlay
from brainrec.registration import RegImage, Transform2D
from tests.synthetic.tissue import apply_similarity, as_reg_image, make_tissue

MPP = (0.2205, 0.2205)
IDENT = Transform2D.identity()
GREEN, MAGENTA = (0, 255, 0), (255, 0, 255)  # colour code of results/README.md


def _square(shape, x0, x1, y0=40, y1=120, value=200, slice_id=1) -> RegImage:
    mask = np.zeros(shape, np.uint8)
    mask[y0:y1, x0:x1] = 255
    return RegImage(slice_id, np.where(mask > 0, value, 0).astype(np.uint8), mask, 32.0, MPP)


def test_channels_follow_the_colour_code():
    fixed = _square((160, 300), 20, 120)  # left
    moving = _square((160, 300), 180, 280, value=150, slice_id=2)  # right, disjoint
    rgb = overlay_rgb(fixed, moving, IDENT)
    assert tuple(rgb[80, 70]) == (0, 200, 0)  # fixed only: green
    assert tuple(rgb[80, 230]) == (150, 0, 150)  # moving only: magenta
    assert tuple(rgb[80, 150]) == (0, 0, 0)  # background: black
    both = overlay_rgb(fixed, _square((160, 300), 20, 120, slice_id=2), IDENT)
    assert tuple(both[80, 70]) == (200, 200, 200)  # agreement: grey


def test_outlines_in_the_slice_colours():
    fixed = _square((160, 300), 20, 120)
    moving = _square((160, 300), 180, 280, slice_id=2)
    rgb = overlay_rgb(fixed, moving, IDENT)
    assert tuple(rgb[80, 20]) == GREEN and tuple(rgb[40, 70]) == GREEN
    assert tuple(rgb[80, 279]) == MAGENTA and tuple(rgb[119, 230]) == MAGENTA
    assert tuple(rgb[80, 60]) != GREEN  # interior is image, not outline


def test_moving_is_warped_into_the_fixed_frame():
    fixed = _square((160, 300), 20, 120)
    moving = _square((160, 300), 180, 280, slice_id=2)
    t = Transform2D.similarity(0.0, 1.0, -160 * 32 * MPP[0], 0.0)  # 160 px to the left
    rgb = overlay_rgb(fixed, moving, t)
    assert tuple(rgb[80, 70]) == (200, 200, 200)  # now on top of the fixed square
    assert tuple(rgb[80, 230]) == (0, 0, 0)


@pytest.mark.parametrize("seed", [0, 1])
def test_aligned_tissue_looks_grey(seed):
    a = make_tissue(seed=seed)
    moved, moved_mask, truth = apply_similarity(a.rgb, a.mask, 25.0, 1.05, 80.0, -40.0)
    fixed, moving = as_reg_image(a.rgb, a.mask, 1), as_reg_image(moved, moved_mask, 2)
    inner = cv2.erode(a.mask, np.ones((15, 15), np.uint8)) > 0

    def mismatch(t: Transform2D) -> float:
        rgb = overlay_rgb(fixed, moving, t).astype(int)
        return float(np.abs(rgb[..., 0] - rgb[..., 1])[inner].mean())

    assert mismatch(truth) < 5  # magenta and green cancel to grey
    assert mismatch(IDENT) > 4 * mismatch(truth)


def test_size_is_the_fixed_frame():
    fixed = _square((160, 300), 20, 120)
    moving = _square((200, 400), 180, 280, slice_id=2)  # larger scan
    assert overlay_rgb(fixed, moving, IDENT).shape == (160, 300, 3)


def test_render_writes_png(tmp_path):
    fixed = _square((160, 300), 20, 120)
    moving = _square((160, 300), 180, 280, slice_id=2)
    out = tmp_path / "qc" / "overlay_2.png"  # directory does not exist yet
    assert render_overlay(fixed, moving, IDENT, out) == out
    written = cv2.cvtColor(cv2.imread(str(out)), cv2.COLOR_BGR2RGB)
    assert np.array_equal(written, overlay_rgb(fixed, moving, IDENT))
    assert [p.name for p in out.parent.iterdir()] == ["overlay_2.png"]  # no temporary file left


def test_rejects_mismatched_levels():
    fixed = _square((160, 300), 20, 120)
    m = _square((160, 300), 180, 280, slice_id=2)
    coarse = RegImage(2, m.image, m.mask, 64.0, MPP)
    with pytest.raises(ValueError, match="share pixel size and level"):
        overlay_rgb(fixed, coarse, IDENT)


def test_background_stays_black():
    """Registration images are 0 outside the mask (C4): glass and debris never show (F12)."""
    a = make_tissue(seed=0)
    rgb = overlay_rgb(as_reg_image(a.rgb, a.mask, 1), as_reg_image(a.rgb, a.mask, 2), IDENT)
    outside = cv2.dilate(a.mask, np.ones((9, 9), np.uint8)) == 0
    assert not rgb[outside].any()
