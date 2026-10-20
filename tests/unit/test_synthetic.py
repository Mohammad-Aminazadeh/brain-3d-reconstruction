import cv2
import numpy as np
import pytest

from brainrec.registration import Transform2D
from tests.synthetic.fake_slide import FakeSlide
from tests.synthetic.tissue import UM_PER_PX, apply_similarity, make_tissue, perturb

MM2_PER_PX = UM_PER_PX**2 / 1e6


@pytest.fixture(scope="module")
def sample():
    return make_tissue(seed=3)


def reg_like(rgb, mask):
    """The registration image of contract C4 (inverted luminance, CLAHE, 0 outside the mask)."""
    inv = 255 - cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    img = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(inv)
    img[mask == 0] = 0
    return img


def components(mask):
    _, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    return stats[1:, cv2.CC_STAT_AREA]


def test_deterministic_per_seed(sample):
    again = make_tissue(seed=3)
    assert np.array_equal(sample.rgb, again.rgb) and np.array_equal(sample.mask, again.mask)
    assert not np.array_equal(sample.rgb, make_tissue(seed=4).rgb)


def test_shapes_and_types(sample):
    assert sample.rgb.shape == (800, 1000, 3) and sample.rgb.dtype == np.uint8
    for m in (sample.mask, sample.fragments, sample.debris):
        assert m.shape == (800, 1000) and set(np.unique(m)) <= {0, 255}


def test_fragments_are_detached_tissue(sample):
    areas = components(sample.fragments)
    assert len(areas) == 2
    assert (areas * MM2_PER_PX >= 0.25).all()  # above the mask's minimum component size (C9)
    assert len(components(sample.mask)) == 3  # main section + 2 fragments, all separate
    assert (sample.mask[sample.fragments > 0] == 255).all()


def test_debris_is_small_and_not_tissue(sample):
    areas = components(sample.debris)
    assert len(areas) >= 10
    assert (areas * MM2_PER_PX < 0.2).all()
    assert not (sample.debris & sample.mask).any()
    assert sample.rgb[sample.debris > 0].mean() < 120  # dark specks, clearly not glass


def test_colours_resemble_the_real_slides(sample):
    sat = cv2.cvtColor(sample.rgb, cv2.COLOR_RGB2HSV)[..., 1]
    glass = (sample.mask == 0) & (sample.debris == 0)
    assert np.median(sat[glass]) < 12  # real glass: about 9 to 10
    main = (sample.mask > 0) & (sample.fragments == 0)
    assert 25 <= np.median(sat[main]) <= 45  # real tissue: about 27 to 30


def test_pale_rim_is_tissue_but_paler(sample):
    sat = cv2.cvtColor(sample.rgb, cv2.COLOR_RGB2HSV)[..., 1].astype(float)
    core = cv2.erode(sample.mask, np.ones((41, 41), np.uint8)) > 0
    edge = (sample.mask > 0) & ~core & (sample.fragments == 0)
    # some of the edge is clearly paler than the core: the rim that needs hysteresis (C4)
    assert np.percentile(sat[edge], 10) < np.median(sat[core]) - 8


def test_orb_finds_enough_keypoints_inside_tissue(sample):
    kp = cv2.ORB_create(5000).detect(reg_like(sample.rgb, sample.mask), sample.mask)
    assert len(kp) >= 100


@pytest.mark.parametrize(
    "angle, scale, tx, ty", [(30.0, 1.0, 0.0, 0.0), (-170.0, 0.92, 300.0, -150.0)]
)
def test_apply_similarity_truth_maps_moved_back(sample, angle, scale, tx, ty):
    moved, moved_mask, truth = apply_similarity(sample.rgb, sample.mask, angle, scale, tx, ty)
    assert truth.angle_deg == pytest.approx(-angle if abs(angle) < 180 else angle, abs=1e-9)
    assert truth.scale == pytest.approx(1 / scale)
    # warping the moved section with the truth restores the original (interpolation error only)
    m = truth.to_pixels(1, 1, (UM_PER_PX, UM_PER_PX))[:2]
    size = moved.shape[1::-1]
    restored = cv2.warpAffine(moved, m, size, flags=cv2.INTER_LINEAR)
    restored_mask = cv2.warpAffine(moved_mask, m, size, flags=cv2.INTER_NEAREST)
    # only original pixels that stayed inside the moved frame can come back (corners rotate out)
    stayed = cv2.warpAffine(
        np.full(moved_mask.shape, 255, np.uint8), m, size, flags=cv2.INTER_NEAREST
    )
    stayed = cv2.erode(stayed, np.ones((5, 5), np.uint8)) > 0
    a, b = (sample.mask > 0) & stayed, (restored_mask > 0) & stayed
    assert (a & b).sum() / (a | b).sum() > 0.97
    core = (cv2.erode(sample.mask, np.ones((9, 9), np.uint8)) > 0) & stayed
    diff = np.abs(restored.astype(int) - sample.rgb.astype(int))[core]
    assert np.median(diff) <= 4


def test_perturb_keeps_geometry_and_changes_content(sample):
    p = perturb(sample.rgb, sample.mask, seed=1)
    assert np.array_equal(p, perturb(sample.rgb, sample.mask, seed=1))  # deterministic
    inside = sample.mask > 0
    assert np.array_equal(p[~inside], sample.rgb[~inside])  # glass untouched
    a = sample.rgb[inside].astype(float).ravel()
    b = p[inside].astype(float).ravel()
    assert np.abs(a - b).mean() > 1.0  # different section ...
    assert np.corrcoef(a, b)[0, 1] > 0.8  # ... of the same tissue


def test_neighbouring_sections_share_structure_not_cells():
    a, b = make_tissue(seed=5, detail_seed=1), make_tissue(seed=5, detail_seed=2)
    for m in ("mask", "fragments", "debris"):
        assert np.array_equal(getattr(a, m), getattr(b, m))  # same geometry: identity truth
    inside = cv2.erode(a.mask, np.ones((15, 15), np.uint8)) > 0
    ga, gb = (cv2.cvtColor(x.rgb, cv2.COLOR_RGB2GRAY).astype(float) for x in (a, b))
    coarse = [cv2.GaussianBlur(g, (0, 0), 8)[inside] for g in (ga, gb)]
    fine = [(g - cv2.GaussianBlur(g, (0, 0), 2))[inside] for g in (ga, gb)]
    assert np.corrcoef(*coarse)[0, 1] > 0.8  # stain bands, vessels and lumens continue
    assert np.corrcoef(*fine)[0, 1] < 0.5  # cells differ


def test_neighbouring_sections_defeat_global_matching(sample):
    """Like the real slides: few global matches survive, so guided matching (M3-07) is needed."""
    same, _ = _global_orb_inliers(
        sample.rgb, sample.mask, perturb(sample.rgb, sample.mask, 1), sample.mask
    )
    nxt = make_tissue(seed=3, detail_seed=99)
    other, _ = _global_orb_inliers(sample.rgb, sample.mask, nxt.rgb, nxt.mask)
    assert other < 0.1 * same


def _global_orb_inliers(fixed, fmask, moving, mmask):
    orb = cv2.ORB_create(5000)
    kf, df = orb.detectAndCompute(reg_like(fixed, fmask), fmask)
    km, dm = orb.detectAndCompute(reg_like(moving, mmask), mmask)
    pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(dm, df, k=2)
    good = [m for m, n in (p for p in pairs if len(p) == 2) if m.distance < 0.8 * n.distance]
    if len(good) < 3:
        return 0, None
    src = np.float32([km[g.queryIdx].pt for g in good])
    dst = np.float32([kf[g.trainIdx].pt for g in good])
    m, inl = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=3.0)
    return (0 if inl is None else int(inl.sum())), m


def test_moved_perturbed_pair_is_recoverable_by_orb(sample):
    """Same section, moved and perturbed: features must match (prerequisite for AC4)."""
    moved, moved_mask, truth = apply_similarity(sample.rgb, sample.mask, 12.0, 1.05, 200.0, -100.0)
    moved = perturb(moved, moved_mask, seed=2)
    orb = cv2.ORB_create(5000)
    kf, df = orb.detectAndCompute(reg_like(sample.rgb, sample.mask), sample.mask)
    km, dm = orb.detectAndCompute(reg_like(moved, moved_mask), moved_mask)
    pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(dm, df, k=2)
    good = [m for m, n in (p for p in pairs if len(p) == 2) if m.distance < 0.8 * n.distance]
    src = np.float32([km[g.queryIdx].pt for g in good])
    dst = np.float32([kf[g.trainIdx].pt for g in good])
    m, inl = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=3.0)
    assert inl.sum() >= 30
    est = Transform2D.from_pixels(m, 1, 1, (UM_PER_PX, UM_PER_PX))
    assert est.angle_deg == pytest.approx(truth.angle_deg, abs=0.5)
    assert est.scale == pytest.approx(truth.scale, rel=0.005)
    centre = np.array([[500.0, 400.0]]) * UM_PER_PX
    assert np.linalg.norm(est.apply(centre) - truth.apply(centre)) < 2 * UM_PER_PX


def test_fake_slide_from_image(sample):
    s = FakeSlide.from_image(sample.rgb, downsample=32, coarser=(2,), mpp=(0.2205, 0.2205))
    lvl = s.level_for_downsample(32)
    assert (s.levels[lvl].width, s.levels[lvl].height) == (1000, 800)
    assert np.array_equal(s.read_region(lvl, 0, 0, 1000, 800), sample.rgb)
    coarse = s.levels[s.level_for_downsample(64)]
    assert (coarse.width, coarse.height) == (500, 400)
    assert s.mpp == (0.2205, 0.2205) and s.reads == 1
