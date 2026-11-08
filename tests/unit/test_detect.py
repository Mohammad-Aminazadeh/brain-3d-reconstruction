"""M7-05: tiles, nucleus detection over a slice, measurements (synthetic data, contract C13)."""

import importlib.util

import cv2
import numpy as np
import pytest
from scipy.spatial import cKDTree

from brainrec.catalog import ProjectConfig
from brainrec.detect.measure import (
    NUCLEOLUS_MIN_CONTRAST,
    has_nucleolus,
    measure_nuclei,
    refine_patch,
    sharpness,
)
from brainrec.detect.nuclei import Detector, _kept, detect_slice, load_detector, px_to_um
from brainrec.detect.tiles import Tile, half_overlap_px, in_mask, plan_tiles, window_mask
from brainrec.store.cells import FEATURES, MAX_OUTLINE_VERTICES, OUTLINE_UNIT_UM
from tests.synthetic.fake_slide import FakeSlide
from tests.synthetic.nuclei import make_nuclei, segment_by_colour

UM0 = 0.2205  # level-0 pixel size of the real slides
UM2 = 2 * UM0


def _cfg(**kw) -> ProjectConfig:
    return ProjectConfig(data_dir=".", work_dir=".", z_spacing_um=5.0, **kw)


# --- tiles ---------------------------------------------------------------------------------


def test_cores_partition_the_level_and_windows_add_the_margin():
    w, h, tile, half = 1000, 700, 256, 20
    tiles = plan_tiles(w, h, tile, half)
    cover = np.zeros((h, w), int)
    for t in tiles:
        cover[t.y0 : t.y1, t.x0 : t.x1] += 1
        assert (t.rx0, t.ry0) == (max(0, t.x0 - half), max(0, t.y0 - half))
        assert (t.rx1, t.ry1) == (min(w, t.x1 + half), min(h, t.y1 + half))
        assert max(t.window_shape) <= tile
    assert (cover == 1).all()


def test_every_centre_is_owned_by_exactly_one_tile():
    tiles = plan_tiles(1000, 700, 256, 20)
    rng = np.random.default_rng(0)
    x, y = rng.uniform(-0.5, 999.5, 5000), rng.uniform(-0.5, 699.5, 5000)
    x[:4], y[:4] = [215.5, 215.4999, 431.5, -0.5], [0.0, 215.5, 431.5, -0.5]  # on core edges
    owners = np.sum([t.owns(x, y) for t in tiles], axis=0)
    assert (owners == 1).all()


def test_tiles_without_tissue_are_skipped():
    mask = np.zeros((44, 63), np.uint8)  # 16 level px per mask px: 1000 x 700 level px
    mask[2:5, 3:6] = 255  # tissue at level px 48..96 x 32..80
    tiles = plan_tiles(1000, 700, 256, 20, mask, 16)
    assert len(tiles) == 1 and (tiles[0].x0, tiles[0].y0) == (0, 0)
    assert plan_tiles(1000, 700, 256, 20, np.zeros_like(mask), 16) == []


def test_a_tile_needs_a_core():
    with pytest.raises(ValueError, match="no core"):
        plan_tiles(100, 100, 40, 20)


def test_overlap_margin_in_pixels():
    assert half_overlap_px(40.0, UM2) == 45
    assert half_overlap_px(0.1, UM2) == 1


def test_in_mask_follows_pixel_centres():
    mask = np.zeros((2, 2), np.uint8)
    mask[0, 1] = 255  # level px 16..31 in x, 0..15 in y (16 level px per mask px)
    x = np.array([15.4, 15.6, 31.4, 31.6, -20.0, 100.0])
    assert in_mask(mask, 16, x, np.zeros(6)).tolist() == [False, True, True, False, False, False]
    win = window_mask(mask, 16, Tile(0, 0, 32, 32, 10, 0, 22, 4))
    assert win.shape == (4, 12) and win[:, 6:].all() and not win[:, :6].any()


def test_cut_objects_count_only_at_the_slide_border():
    labels = np.zeros((20, 20), np.int32)
    labels[8:12, 15:20] = 1  # touches the window's right edge
    labels[8:12, 2:6] = 2
    mask = np.full((1, 1), 255, np.uint8)
    inner = Tile(0, 0, 20, 20, 0, 0, 20, 20)  # right edge is not the slide border
    assert _kept(labels, inner, (100, 20), mask, 100, 1.0, 1.0).tolist() == [2]
    assert _kept(labels, inner, (20, 20), mask, 100, 1.0, 1.0).tolist() == [1, 2]
    assert _kept(labels, inner, (20, 20), mask, 100, 1.0, 17.0).tolist() == [1]  # area limit
    assert _kept(np.zeros((5, 5), np.int32), inner, (5, 5), mask, 100, 1.0, 1.0).size == 0


# --- a whole synthetic slice ---------------------------------------------------------------


class _CountingSlide(FakeSlide):
    def read_region(self, level, x, y, w, h):
        self.sizes.append((level, w, h))
        return super().read_region(level, x, y, w, h)


def _slide(syn) -> _CountingSlide:
    s = _CountingSlide.from_image(syn.rgb, 1, (2,), (UM0, UM0))
    s.sizes = []
    return s


def _mask32(syn) -> np.ndarray:
    h, w = syn.mask.shape
    small = cv2.resize(syn.mask, (w // 32, h // 32), interpolation=cv2.INTER_AREA)
    return np.where(small >= 128, 255, 0).astype(np.uint8)


STEP = 512 - 2 * half_overlap_px(40.0, UM2)  # core width of 512 px tiles at 2x


@pytest.fixture(scope="module")
def syn():
    # level 0 at the real pixel size; nuclei sit across the 2x core borders (every STEP px)
    return make_nuclei(shape=(2048, 2048), seed=3, um_per_px=UM0, tile_px=2 * STEP,
                       glass_px=256, n_on_border=8)  # fmt: skip


@pytest.fixture(scope="module")
def run(syn):
    slide = _slide(syn)
    det = Detector("colour", "test", segment_by_colour)
    nu = detect_slice(slide, _mask32(syn), 32, _cfg(detect_tile_px=512), det)
    return slide, nu


def _match(syn, nu):
    d, j = cKDTree(np.c_[nu.x_um, nu.y_um]).query(np.c_[syn.truth.x_um, syn.truth.y_um])
    return d, j


def test_every_nucleus_once_with_centre_and_radius(syn, run):
    _, nu = run
    d, j = _match(syn, nu)
    assert len(nu) == len(syn.truth) and len(set(j.tolist())) == len(nu)
    assert d.max() <= 0.5
    assert d[syn.truth.cls == "Neuron"].max() < 0.1  # refined on level 0: no half-pixel shift
    assert np.allclose(nu.r_um[j], syn.truth.r_um, rtol=0.10)
    assert np.allclose(nu.area_um2, np.pi * nu.r_um**2)


def test_nuclei_across_tile_borders_are_counted_once(syn, run):
    _, nu = run
    t = syn.truth
    assert t.on_border.sum() >= 4
    for i in np.nonzero(t.on_border)[0]:
        near = np.hypot(nu.x_um - t.x_um[i], nu.y_um - t.y_um[i]) < t.r_um[i]
        assert near.sum() == 1
    # one big tile gives the same nuclei
    one = detect_slice(_slide(syn), _mask32(syn), 32, _cfg(detect_tile_px=1200),
                       Detector("colour", "test", segment_by_colour))  # fmt: skip
    assert len(one.tile_sharpness) == 1 and len(one) == len(nu)
    d, _ = cKDTree(np.c_[one.x_um, one.y_um]).query(np.c_[nu.x_um, nu.y_um])
    assert d.max() < 1e-6


def test_nothing_outside_the_tissue_mask(syn, run):
    _, nu = run
    mask = _mask32(syn)
    px = nu.x_um / (32 * UM0) - 0.5, nu.y_um / (32 * UM0) - 0.5
    assert in_mask(mask, 1, *px).all()
    # the stand-in detector finds the debris on the glass (specks below the default minimum
    # area, so the limit is lowered here): the mask is what removes them
    det = Detector("colour", "test", segment_by_colour)
    cfg = _cfg(detect_tile_px=512, detect_min_area_um2=1.0)
    masked = detect_slice(_slide(syn), mask, 32, cfg, det)
    full = detect_slice(_slide(syn), np.full_like(mask, 255), 32, cfg, det)
    on_glass = full.x_um < 256 * UM0
    assert on_glass.sum() >= 5 and not (masked.x_um < 256 * UM0).any()
    assert len(full) == len(masked) + on_glass.sum()


def test_nucleolus_flag_on_synthetic_nuclei(syn, run):
    _, nu = run
    _, j = _match(syn, nu)
    seen = nu.nucleolus_seen[j].astype(bool)
    assert (seen == syn.truth.nucleolus).mean() >= 0.9
    small = nu.features[:, FEATURES.index("diameter_um")] < 8.0
    assert not nu.nucleolus_seen[small].any()  # no level-0 patch, no flag
    assert nu.n_refined >= syn.truth.nucleolus.sum()


def test_outlines_are_compact_and_around_the_centre(run):
    _, nu = run
    off = nu.outline_offsets
    assert off[0] == 0 and off[-1] == len(nu.outline_xy) and len(off) == len(nu) + 1
    assert (np.diff(off) >= 3).all() and (np.diff(off) <= MAX_OUTLINE_VERTICES).all()
    for i in range(len(nu)):
        rel = nu.outline_xy[off[i] : off[i + 1]] * OUTLINE_UNIT_UM
        mean_dist = np.hypot(rel[:, 0], rel[:, 1]).mean()
        assert 0.6 * nu.r_um[i] < mean_dist < 1.8 * nu.r_um[i]


def test_reads_tiles_and_small_patches_only(syn, run):
    slide, nu = run
    level0 = [s for s in slide.sizes if s[0] == 0]
    level2 = [s for s in slide.sizes if s[0] == 1]
    assert len(level2) == len(nu.tile_sharpness) and all(max(w, h) <= 512 for _, w, h in level2)
    assert len(level0) >= nu.n_refined > 0
    assert all(max(w, h) <= 2 * 16 / UM0 for _, w, h in level0)  # patches of a few nuclei


def test_blurred_tiles_mark_their_nuclei(syn):
    det = Detector("colour", "test", segment_by_colour)
    sharp = detect_slice(_slide(syn), _mask32(syn), 32, _cfg(detect_tile_px=512,
                         detect_min_sharpness=0.0), det)  # fmt: skip
    blur = detect_slice(_slide(syn), _mask32(syn), 32, _cfg(detect_tile_px=512,
                        detect_min_sharpness=1e9), det)  # fmt: skip
    assert sharp.n_blurred_tiles == 0 and not sharp.blurred.any()
    assert blur.n_blurred_tiles == len(blur.tile_sharpness) and blur.blurred.all()


def test_detection_confidence_is_passed_on_or_nan(syn):
    def with_conf(rgb, um_px):
        labels, _ = segment_by_colour(rgb, um_px)
        return labels, np.linspace(0, 1, labels.max() + 1)

    nu = detect_slice(_slide(syn), _mask32(syn), 32, _cfg(detect_tile_px=512),
                      Detector("conf", "test", with_conf))  # fmt: skip
    assert np.isfinite(nu.det_conf).all() and (nu.det_conf > 0).all()
    plain = detect_slice(_slide(syn), _mask32(syn), 32, _cfg(detect_tile_px=512),
                         Detector("colour", "test", segment_by_colour))  # fmt: skip
    assert np.isnan(plain.det_conf).all()


def test_pixel_to_micrometre_convention():
    assert np.allclose(px_to_um(np.array([[0, 0], [9, 4]]), 2, (0.25, 0.5)),
                       [[0.25, 0.5], [4.75, 4.5]])  # fmt: skip


# --- measurements --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def measured():
    syn = make_nuclei(seed=5)
    labels, _ = segment_by_colour(syn.rgb, syn.um_per_px)
    m = measure_nuclei(syn.rgb, labels, syn.um_per_px, np.unique(labels[labels > 0]),
                       syn.mask > 0)  # fmt: skip
    t = syn.truth
    xy = np.c_[(m.cx + 0.5) * syn.um_per_px, (m.cy + 0.5) * syn.um_per_px]
    d, j = cKDTree(xy).query(np.c_[t.x_um, t.y_um])
    assert d.max() < 0.5
    return t, m.features[j]


def _f(measured, name, cls):
    t, feats = measured
    return feats[t.cls == cls, FEATURES.index(name)]


def test_features_follow_the_looks(measured):
    neuron, oligo, astro = "Neuron", "Oligodendrocyte-like", "Astrocyte-like"
    micro, vessel = "Microglia-like", "Vessel-wall"
    assert _f(measured, "elongation", micro).min() > _f(measured, "elongation", neuron).max()
    assert _f(measured, "darkness", oligo).min() > _f(measured, "darkness", astro).max()
    assert _f(measured, "halo", oligo).min() > 0 > _f(measured, "halo", neuron).max()
    assert _f(measured, "cell_body", neuron).min() > _f(measured, "cell_body", astro).max()
    assert np.median(_f(measured, "vessel_um", vessel)) < 3.0
    assert np.median(_f(measured, "vessel_um", micro)) > 10.0
    t, feats = measured
    assert np.allclose(feats[:, FEATURES.index("diameter_um")], 2 * t.r_um, rtol=0.1)
    assert np.isfinite(feats).all()


def _disk_patch(r_px=20, size=64, nucleus=(170, 160, 205), around=(205, 195, 225), spots=()):
    rgb = np.empty((size, size, 3), np.float32)
    rgb[:] = around
    c = size // 2
    cv2.circle(rgb, (c, c), r_px, nucleus, -1, cv2.LINE_AA)
    for dx, dy, rs in spots:
        cv2.circle(rgb, (c + dx, c + dy), rs, (80, 55, 120), -1, cv2.LINE_AA)
    return np.clip(rgb, 0, 255).astype(np.uint8)


def _coarse(r_px, size=64, grow=0):
    own = np.zeros((size, size), np.uint8)
    cv2.circle(own, (size // 2, size // 2), r_px + grow, 1, -1)
    return own > 0


def test_patch_refines_a_coarse_outline():
    rgb = _disk_patch(r_px=20)
    for grow in (-3, 3):  # the 2x region was too small or too large
        r = refine_patch(rgb, _coarse(20, grow=grow), np.zeros((64, 64), bool), UM0)
        assert r.refined
        assert r.area_um2 == pytest.approx(np.pi * (20 * UM0) ** 2, rel=0.07)
        assert (r.cx, r.cy) == (pytest.approx(32, abs=0.3), pytest.approx(32, abs=0.3))


def test_patch_keeps_the_coarse_outline_without_contrast():
    rgb = _disk_patch(r_px=20, nucleus=(205, 195, 225))  # nucleus as pale as its surroundings
    own = _coarse(20, grow=3)
    r = refine_patch(rgb, own, np.zeros((64, 64), bool), UM0)
    assert not r.refined and r.area_um2 == pytest.approx(own.sum() * UM0**2)


def test_nucleolus_is_one_dark_interior_spot():
    region = _coarse(20)
    assert has_nucleolus(_disk_patch(spots=[(0, 0, 4)]), region, UM0)
    assert not has_nucleolus(_disk_patch(), region, UM0)  # plain nucleus
    assert not has_nucleolus(_disk_patch(spots=[(18, 0, 4)]), region, UM0)  # at the border
    many = [(-9, -9, 3), (9, -9, 3), (-9, 9, 3), (9, 9, 3)]
    assert not has_nucleolus(_disk_patch(spots=many), region, UM0)  # clumped chromatin
    assert not has_nucleolus(_disk_patch(spots=[(0, 0, 13)]), region, UM0)  # too large
    faint = _disk_patch(nucleus=(170, 160, 205), spots=())
    cv2.circle(faint, (32, 32), 4, (150, 140, 185), -1)  # below NUCLEOLUS_MIN_CONTRAST
    assert 170 - 150 < NUCLEOLUS_MIN_CONTRAST and not has_nucleolus(faint, region, UM0)


def test_sharpness_is_lower_when_blurred_and_uses_tissue_only():
    rng = np.random.default_rng(0)
    img = rng.integers(60, 200, (200, 200, 3)).astype(np.uint8)
    blurred = cv2.GaussianBlur(img, (0, 0), 3)
    assert sharpness(blurred) < sharpness(img) / 10
    tissue = np.zeros((200, 200), bool)
    tissue[:, :100] = True
    half = img.copy()
    half[:, 100:] = 255  # glass
    assert sharpness(half, tissue) == pytest.approx(sharpness(img[:, :100]), rel=0.15)
    assert np.isnan(sharpness(img, np.zeros((200, 200), bool)))


# --- the detector --------------------------------------------------------------------------


def test_unknown_detector_is_refused():
    with pytest.raises(ValueError, match="unknown detection method"):
        load_detector("cellpose")


@pytest.mark.skipif(importlib.util.find_spec("instanseg") is None, reason="InstanSeg missing")
def test_instanseg_finds_the_synthetic_nuclei():
    """The real model behind `load_detector`. The synthetic neuron (a pale nucleus inside a
    darker drawn cell body) is not found by InstanSeg, while real neurons are (M7-05 galleries);
    the other five looks are."""
    syn = make_nuclei(seed=3)
    det = load_detector("instanseg")
    assert det.version.startswith("instanseg-torch ")
    labels, conf = det.segment(syn.rgb, syn.um_per_px)
    assert conf is None and labels.shape == syn.rgb.shape[:2] and labels.dtype == np.int32
    from skimage import measure

    ps = [p for p in measure.regionprops(labels) if p.area * syn.um_per_px**2 >= 8]
    c = np.array([(p.centroid[1] + 0.5, p.centroid[0] + 0.5) for p in ps]) * syn.um_per_px
    d, _ = cKDTree(c).query(np.c_[syn.truth.x_um, syn.truth.y_um])
    glia = syn.truth.cls != "Neuron"
    assert np.mean(d[glia] < 1.5) >= 0.9
