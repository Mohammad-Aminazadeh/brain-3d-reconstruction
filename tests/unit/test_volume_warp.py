import math
from pathlib import Path

import cv2
import numpy as np
import pytest
import zarr

from brainrec.catalog import Catalog
from brainrec.registration import Transform2D
from brainrec.store import Manifest
from brainrec.volume import VolumeExistsError, VolumeSpec, build_volume
from brainrec.volume.warp import source_box
from tests.synthetic.fake_slide import FakeSlide
from tests.synthetic.tissue import apply_similarity, make_tissue

MPP = (0.2205, 0.2205)
PX = 32 * MPP[0]  # µm per pixel of the synthetic images and of volume level 0
# synthetic images play the 32x level; small shards so slices straddle many tiles
SPEC = VolumeSpec(z_spacing_um=5.0, base_downsample=32, n_levels=3, chunk_xy=64, shard_xy=256)
MARGIN_PX = SPEC.mask_margin_um / PX
MARKER = (0, 200, 0)


def _marked_tissue(seed=5, white_square=False):
    t = make_tissue(seed=seed)
    rgb = t.rgb.copy()
    dist = cv2.distanceTransform(t.mask, cv2.DIST_L2, 5)
    dist[:, : int(0.6 * dist.shape[1])] = 0  # marker right of the centre, deep inside tissue
    my, mx = np.unravel_index(int(dist.argmax()), dist.shape)
    rgb[my - 5 : my + 6, mx - 5 : mx + 6] = MARKER
    square = None
    if white_square:
        cy, cx = np.unravel_index(
            int(cv2.distanceTransform(t.mask, cv2.DIST_L2, 5).argmax()), dist.shape
        )
        rgb[cy - 10 : cy + 10, cx - 10 : cx + 10] = 255  # pure white tissue (e.g. a lumen)
        square = (float(cx) - 0.5, float(cy) - 0.5)  # centre of the 20x20 square
    return rgb, t.mask, (float(mx), float(my)), square


class _Factory:
    def __init__(self, images):
        self.images, self.reads = images, 0

    def __call__(self, path):
        slide = FakeSlide.from_image(self.images[int(path.name.split("_")[1])], 32, (2, 4), MPP)
        factory = self

        class Counting(type(slide)):
            def read_region(self, *a):
                factory.reads += 1
                return super().read_region(*a)

        slide.__class__ = Counting
        return slide


def _project(tmp_path, motions, white_square=False, rgb_mask=None):
    """Slices of one section, each moved on its slide by a known similarity (slice 1: none)."""
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    rgb, mask, marker, square = rgb_mask or _marked_tissue(white_square=white_square)
    images, masks, truths = {}, {}, {}
    for k, (a, s, tx, ty) in motions.items():
        (data / f"s_{k}_H.ndpi").write_bytes(b"x")
        moved, mm, truth = apply_similarity(rgb, mask, a, s, tx, ty)  # truth: moved -> slice 1
        images[k], truths[k] = moved, truth
        masks[k] = tmp_path / f"mask_{k}.png"
        cv2.imwrite(str(masks[k]), mm)
    return Catalog.scan(data), _Factory(images), truths, masks, (rgb, mask, marker, square)


def _build(tmp_path, cat, factory, truths, masks, manifest=None, **kw):
    out = tmp_path / "v.zarr"
    m = manifest or Manifest(tmp_path / "m.json")
    build_volume(cat, out, SPEC, m, factory, transforms=truths, masks=masks, **kw)
    return zarr.open_group(str(out), mode="r"), out


def _marker_xy(plane: np.ndarray) -> tuple[float, float]:
    """Centroid of the green marker in a (3, h, w) plane."""
    w = np.clip(plane[1].astype(float) - plane[0].astype(float) - 60, 0, None)
    ys, xs = np.nonzero(w)
    return float(np.average(xs, weights=w[ys, xs])), float(np.average(ys, weights=w[ys, xs]))


def _expected_px(g, slice_px):
    """Canvas level-0 pixel of a pixel of slice 1 (the reference)."""
    ox, oy = g.attrs["brainrec"]["canvas_origin_um"]
    x_um, y_um = (slice_px[0] + 0.5) * PX, (slice_px[1] + 0.5) * PX
    return (x_um - ox) / PX - 0.5, (y_um - oy) / PX - 0.5


IDENTITY = (0.0, 1.0, 0.0, 0.0)


def test_canvas_is_the_tissue_extent(tmp_path):
    cat, factory, _, masks, _ = _project(tmp_path, {1: IDENTITY, 2: IDENTITY})
    for k, (x0, y0, x1, y1) in {1: (300, 200, 700, 500), 2: (350, 250, 750, 550)}.items():
        rect = np.zeros((800, 1000), np.uint8)
        rect[y0:y1, x0:x1] = 255
        cv2.imwrite(str(masks[k]), rect)
    ident = {1: Transform2D.identity(), 2: Transform2D.identity()}
    g, _ = _build(tmp_path, cat, factory, ident, masks)
    r = round(
        MARGIN_PX
    )  # masks grow by the margin: x from 300 - r to 750 + r, y 200 - r to 550 + r
    cell = PX * 4  # coarsest level pixel: origin and size are multiples of it
    ox = math.floor((300 - r) * PX / cell) * cell
    oy = math.floor((200 - r) * PX / cell) * cell
    assert g.attrs["brainrec"]["canvas_origin_um"] == pytest.approx([ox, oy])
    w = math.ceil(((750 + r) * PX - ox) / cell) * 4
    h = math.ceil(((550 + r) * PX - oy) / cell) * 4
    assert g["0"].shape == (3, 2, h, w) and g["2"].shape == (3, 2, h // 4, w // 4)
    assert w < 1000 / 2 and h < 800 / 2  # the canvas is the tissue, not the scanned glass


def test_source_box_follows_a_rotated_tile_and_is_bounded():
    rot = Transform2D.similarity(math.pi / 4, 1 / 0.97, 1000.0, 1000.0).matrix3
    box = source_box(rot, (0, 0, 256, 256), 5000, 5000)
    corners = np.array([[0, 0], [255, 0], [0, 255], [255, 255]], float) @ rot[:2, :2].T + rot[:2, 2]
    assert box == (
        int(np.floor(corners[:, 0].min())) - 2, int(np.floor(corners[:, 1].min())) - 2,
        int(np.ceil(corners[:, 0].max())) + 3, int(np.ceil(corners[:, 1].max())) + 3,
    )  # fmt: skip
    side = 255 * math.sqrt(2) / 0.97
    assert box[2] - box[0] <= side + 6 and box[3] - box[1] <= side + 6  # memory stays bounded
    assert source_box(rot, (0, 0, 256, 256), 1100, 5000)[2] == 1100  # clipped to the source
    assert source_box(rot, (0, 0, 256, 256), 500, 500) is None  # entirely outside


def test_integer_translation_reproduces_pixels_exactly(tmp_path):
    cat, factory, truths, masks, (rgb, _, _, _) = _project(tmp_path, {1: IDENTITY})
    g, _ = _build(tmp_path, cat, factory, truths, masks)
    ox, oy = (round(v / PX) for v in g.attrs["brainrec"]["canvas_origin_um"])
    plane = g["0"][:, 0].transpose(1, 2, 0)
    kept = (plane != 255).any(axis=2)
    h, w = plane.shape[:2]
    src = np.full((h, w, 3), 255, np.uint8)
    sub = rgb[max(0, oy) : oy + h, max(0, ox) : ox + w]
    src[max(0, -oy) : max(0, -oy) + sub.shape[0], max(0, -ox) : max(0, -ox) + sub.shape[1]] = sub
    assert kept.sum() > 100_000 and np.array_equal(plane[kept], src[kept])


@pytest.mark.parametrize("angle", [90.0, -25.0])
def test_rotated_slice_puts_the_marker_back(tmp_path, angle):
    cat, factory, truths, masks, (_, _, marker, _) = _project(
        tmp_path, {1: IDENTITY, 2: (angle, 1.0, 0.0, 0.0)}
    )
    g, _ = _build(tmp_path, cat, factory, truths, masks)
    expected = _expected_px(g, marker)
    for z in (0, 1):
        assert np.hypot(*np.subtract(_marker_xy(g["0"][:, z]), expected)) < 1.0


MOTIONS = {1: IDENTITY, 2: (10.0, 1.03, 150.0, -80.0), 3: (-25.0, 0.96, -120.0, 100.0)}


def test_markers_aligned_across_slices_and_levels(tmp_path):
    cat, factory, truths, masks, (_, _, marker, _) = _project(tmp_path, MOTIONS)
    g, _ = _build(tmp_path, cat, factory, truths, masks)
    expected = np.array(_expected_px(g, marker))
    for z in range(3):
        base = np.array(_marker_xy(g["0"][:, z]))
        assert np.hypot(*(base - expected)) < 1.0  # acceptance: one (x, y) across z within 1 px
        for k in (1, 2):  # same physical position at every level (level px -> level-0 px)
            at_k = (np.array(_marker_xy(g[str(k)][:, z])) + 0.5) * 2**k - 0.5
            assert np.hypot(*(at_k - base)) < max(1.0, 0.5 * 2**k)


def test_tissue_only(tmp_path):
    cat, factory, truths, masks, (_, _, _, square) = _project(tmp_path, MOTIONS, white_square=True)
    g, out = _build(tmp_path, cat, factory, truths, masks)
    ox, oy = g.attrs["brainrec"]["canvas_origin_um"]
    canvas = Transform2D.similarity(0.0, 1.0, -ox, -oy)
    vol = g["0"][:]
    _, _, h, w = vol.shape
    for z, k in enumerate((1, 2, 3)):
        m = canvas.compose(truths[k]).to_pixels(32, 32, MPP)
        tissue = cv2.warpAffine(
            cv2.imread(str(masks[k]), 0), m[:2], (w, h), flags=cv2.INTER_NEAREST
        )
        non_bg = (vol[:, z] != 255).any(axis=0)
        dist = cv2.distanceTransform(np.where(tissue > 0, 0, 255).astype(np.uint8), cv2.DIST_L2, 5)
        assert dist[non_bg].max() <= MARGIN_PX + 2  # nothing outside the tissue + margin
        core = cv2.erode(tissue, np.ones((5, 5), np.uint8)) > 0
        assert non_bg[core].all()  # every tissue voxel kept, and none is pure white
    sx, sy = (round(v) for v in _expected_px(g, square))  # slice 1: identity
    sq = vol[:, 0, sy - 8 : sy + 8, sx - 8 : sx + 8]  # inside slice 1's pure-white square
    assert (sq == 254).sum() > 0 and (sq != 255).all()
    # chunks entirely outside the tissue are not stored (sharded layout c/0/z/ty/tx)
    stored = {
        tuple(int(v) for v in p.relative_to(out / "0" / "c" / "0").parts)
        for p in (out / "0" / "c").rglob("*")
        if p.is_file()
    }
    with_tissue = {
        (z, ty, tx)
        for z in range(3)
        for ty in range(math.ceil(h / 256))
        for tx in range(math.ceil(w / 256))
        if (vol[:, z, ty * 256 : (ty + 1) * 256, tx * 256 : (tx + 1) * 256] != 255).any()
    }
    assert stored == with_tissue and len(stored) < 3 * math.ceil(h / 256) * math.ceil(w / 256)


def test_resume_and_changed_inputs(tmp_path):
    cat, factory, truths, masks, _ = _project(tmp_path, {1: IDENTITY, 2: MOTIONS[2]})
    _build(tmp_path, cat, factory, truths, masks)
    first = factory.reads
    _build(tmp_path, cat, factory, truths, masks)
    assert first > 0 and factory.reads == first  # unchanged inputs: resumed, nothing read
    nudged = {**truths, 2: Transform2D.similarity(0.001, 1.0, 1.0, 0.0).compose(truths[2])}
    with pytest.raises(VolumeExistsError):
        _build(tmp_path, cat, factory, nudged, masks)
    _build(tmp_path, cat, factory, nudged, masks, overwrite=True)
    m = cv2.imread(str(masks[2]), 0)
    m[:10, :10] = 255  # any change to a mask file
    cv2.imwrite(str(masks[2]), m)
    with pytest.raises(VolumeExistsError):
        _build(tmp_path, cat, factory, nudged, masks)


def test_empty_mask_leaves_the_plane_background(tmp_path):
    cat, factory, truths, masks, _ = _project(tmp_path, {1: IDENTITY, 2: IDENTITY})
    cv2.imwrite(str(masks[2]), np.zeros((800, 1000), np.uint8))
    g, _ = _build(tmp_path, cat, factory, truths, masks)
    assert (g["0"][:, 1] == 255).all() and (g["0"][:, 0] != 255).any()


def test_invalid_inputs_rejected(tmp_path):
    cat, factory, truths, masks, _ = _project(tmp_path, {1: IDENTITY, 2: IDENTITY})
    out = tmp_path / "v.zarr"
    with pytest.raises(ValueError, match="together"):
        build_volume(cat, out, SPEC, None, factory, transforms=truths)
    with pytest.raises(ValueError, match="no transform or mask"):
        build_volume(cat, out, SPEC, None, factory, transforms={1: truths[1]}, masks=masks)
    cv2.imwrite(str(masks[2]), np.zeros((123, 77), np.uint8))
    with pytest.raises(ValueError, match="pyramid level"):
        build_volume(cat, out, SPEC, None, factory, transforms=truths, masks=masks)
    with pytest.raises(FileNotFoundError):
        build_volume(
            cat, out, SPEC, None, factory, transforms=truths, masks={**masks, 1: Path("nope.png")}
        )
