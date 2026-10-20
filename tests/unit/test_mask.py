import cv2
import numpy as np
import pytest

from brainrec.catalog import ProjectConfig
from brainrec.preprocess import make_mask
from tests.synthetic.tissue import GLASS, PALE, TISSUE, UM_PER_PX, make_tissue

CFG = ProjectConfig(data_dir=".", work_dir=".", z_spacing_um=5.0)


@pytest.fixture(scope="module", params=[0, 3])
def sample(request):
    s = make_tissue(seed=request.param)
    return s, make_mask(s.rgb, UM_PER_PX, CFG)


def test_dice_and_recall(sample):
    s, m = sample
    a, b = m > 0, s.mask > 0
    assert 2 * (a & b).sum() / (a.sum() + b.sum()) >= 0.95
    assert (a & b).sum() / b.sum() >= 0.99  # cutting tissue is worse than keeping some glass


def test_values_and_shape(sample):
    s, m = sample
    assert m.shape == s.mask.shape and m.dtype == np.uint8 and set(np.unique(m)) <= {0, 255}


def test_detached_fragments_are_kept(sample):
    s, m = sample
    assert (m[s.fragments > 0] > 0).mean() > 0.98
    assert cv2.connectedComponents(m)[0] - 1 == 3  # section + 2 fragments


def test_debris_specks_are_dropped(sample):
    s, m = sample
    assert not (m[s.debris > 0] > 0).any()


def _canvas(h=600, w=800):
    rng = np.random.default_rng(0)
    return np.clip(GLASS + rng.normal(0, 1.5, (h, w, 3)), 0, 255)


def _paint(img, shape_mask, colour):
    img[shape_mask > 0] = colour
    return img


def _disc(h, w, centre, r):
    m = np.zeros((h, w), np.uint8)
    cv2.circle(m, centre, r, 255, -1)
    return m


def test_holes_inside_tissue_are_filled():
    img = _paint(_canvas(), _disc(600, 800, (400, 300), 200), TISSUE)
    hole = _disc(600, 800, (400, 300), 40)
    img = _paint(img, hole, GLASS)  # e.g. a large vessel lumen
    m = make_mask(img.astype(np.uint8), UM_PER_PX, CFG)
    assert (m[hole > 0] > 0).all()


def test_hysteresis_keeps_attached_pale_tissue_only():
    h, w = 600, 800
    stained = _disc(h, w, (300, 300), 150)
    attached = _disc(h, w, (470, 300), 60) & ~stained  # pale region touching the tissue
    isolated = _disc(h, w, (680, 120), 60)  # same pale colour, on its own on the glass
    img = _paint(_paint(_paint(_canvas(h, w), stained, TISSUE), attached, PALE), isolated, PALE)
    img = img.astype(np.uint8)
    m = make_mask(img, UM_PER_PX, CFG)
    assert (m[attached > 0] > 0).mean() > 0.95
    assert not (m[isolated > 0] > 0).any()
    # without hysteresis the pale region would be lost: the parameter does the work
    strict = make_mask(img, UM_PER_PX, CFG.model_copy(update={"mask_hysteresis": 1.0}))
    assert (strict[attached > 0] > 0).mean() < 0.5


def test_pure_glass_gives_an_empty_mask():
    m = make_mask(_canvas().astype(np.uint8), UM_PER_PX, CFG)
    assert not m.any()


def test_minimum_component_size_is_respected():
    h, w = 600, 800
    big = _disc(h, w, (300, 300), 150)
    side = 0.5e3 / UM_PER_PX  # 0.5 mm -> 0.25 mm² square edge in px
    small = np.zeros((h, w), np.uint8)
    cv2.rectangle(small, (650, 100), (650 + int(side * 0.8), 100 + int(side * 0.8)), 255, -1)
    img = _paint(_paint(_canvas(h, w), big, TISSUE), small, TISSUE).astype(np.uint8)
    assert not make_mask(img, UM_PER_PX, CFG)[small > 0].any()  # 0.16 mm²: dropped
    keep_small = CFG.model_copy(update={"mask_min_component_mm2": 0.1})
    assert (make_mask(img, UM_PER_PX, keep_small)[small > 0] > 0).mean() > 0.9  # corners rounded


def test_non_rgb_rejected():
    with pytest.raises(ValueError, match="RGB"):
        make_mask(np.zeros((10, 10), np.uint8), UM_PER_PX, CFG)
