"""Synthetic tissue sections with known ground truth, for registration, mask and QC tests.

Images imitate the real 32x registration level (about 7 µm/px), as measured in the spike
(docs/REQUIREMENTS.md section 7): pale lavender tissue on near-white glass (saturation about 30
vs about 5 to 10), bright vessel-like lines and lumens, dark dots ("cells"), large-scale stain
variation, a pale rim attached to the tissue, detached tissue fragments (kept as tissue,
decision 14) and dark debris specks on the glass (not tissue, each below 0.2 mm²).
"""

from dataclasses import dataclass

import cv2
import numpy as np

from brainrec.preprocess import make_reg_image
from brainrec.registration import RegImage, Transform2D

UM_PER_PX = 32 * 0.2205  # the real registration level
GLASS = np.array([236, 233, 236], float)
TISSUE = np.array([204, 190, 222], float)
PALE = np.array([222, 214, 230], float)
CELL = np.array([150, 125, 182], float)
DEBRIS = np.array([95, 80, 105], float)


@dataclass(frozen=True)
class SyntheticTissue:
    rgb: np.ndarray  # (h, w, 3) uint8
    mask: np.ndarray  # ground-truth tissue: main section, pale rim and fragments; uint8 {0, 255}
    fragments: np.ndarray  # detached fragments only (part of `mask`)
    debris: np.ndarray  # debris specks on the glass (not part of `mask`)
    um_per_px: float


def _blob(rng: np.random.Generator, shape: tuple[int, int], centre, radius: float) -> np.ndarray:
    """Filled irregular closed shape: radius modulated by a few random lobes plus fine ripple."""
    t = np.linspace(0, 2 * np.pi, 720, endpoint=False)
    r = np.ones_like(t)
    for k in range(2, 7):
        r += rng.uniform(0.02, 0.12) / (k - 1) * np.cos(k * t + rng.uniform(0, 2 * np.pi))
    r += 0.01 * rng.standard_normal(t.size).cumsum() / np.sqrt(t.size)
    pts = np.stack([centre[0] + radius * r * np.cos(t), centre[1] + radius * r * np.sin(t)], 1)
    out = np.zeros(shape, np.uint8)
    cv2.fillPoly(out, [pts.round().astype(np.int32)], 255)
    return out


def _smooth_field(rng: np.random.Generator, shape: tuple[int, int], cell: int) -> np.ndarray:
    """Low-frequency noise in [-1, 1] (stain bands, layers)."""
    small = rng.standard_normal((shape[0] // cell + 2, shape[1] // cell + 2)).astype(np.float32)
    big = cv2.resize(small, shape[::-1], interpolation=cv2.INTER_CUBIC)
    big = cv2.GaussianBlur(big, (0, 0), cell / 2)
    return big / max(1e-6, float(np.abs(big).max()))


def _random_points_in(rng: np.random.Generator, mask: np.ndarray, n: int) -> np.ndarray:
    ys, xs = np.nonzero(mask)
    idx = rng.integers(0, ys.size, n)
    return np.stack([xs[idx], ys[idx]], 1)


def _curve(rng: np.random.Generator, mask: np.ndarray) -> np.ndarray:
    """Smooth cubic Bezier through four random points inside `mask`."""
    p = _random_points_in(rng, mask, 4).astype(float)
    t = np.linspace(0, 1, 60)[:, None]
    pts = (
        (1 - t) ** 3 * p[0] + 3 * (1 - t) ** 2 * t * p[1] + 3 * (1 - t) * t**2 * p[2] + t**3 * p[3]
    )
    return pts.round().astype(np.int32)


def _texture(
    rng: np.random.Generator,
    detail: np.random.Generator,
    canvas: np.ndarray,
    region: np.ndarray,
    base: np.ndarray,
) -> None:
    """Paint stained tissue into `canvas` inside `region` (in place).

    Large structures (stain bands, vessels, lumens) come from `rng` and continue between
    neighbouring sections; fine detail (cells, small clusters) comes from `detail` and differs
    between sections, as in real serial sections.
    """
    h, w = region.shape
    layer = np.empty_like(canvas)
    layer[:] = base * (1 - 0.10 * _smooth_field(rng, (h, w), 40)[..., None])
    area = int((region > 0).sum())
    for x, y in _random_points_in(detail, region, area // 60):  # cells
        tone = base + detail.uniform(0.5, 1.0) * (CELL - base)
        cv2.circle(layer, (int(x), int(y)), int(detail.integers(1, 3)), tone.tolist(), -1)
    for x, y in _random_points_in(detail, region, max(3, area // 6000)):  # darker cell clusters
        tone = base + 0.5 * (CELL - base)
        cv2.circle(layer, (int(x), int(y)), int(detail.integers(4, 10)), tone.tolist(), -1)
    for _ in range(max(2, area // 40000)):  # vessel-like bright lines
        cv2.polylines(layer, [_curve(rng, region)], False, GLASS.tolist(), int(rng.integers(2, 4)))
    for x, y in _random_points_in(rng, region, max(2, area // 30000)):  # vessel lumens
        axes = (int(rng.integers(3, 8)), int(rng.integers(3, 8)))
        cv2.ellipse(
            layer, (int(x), int(y)), axes, float(rng.uniform(0, 180)), 0, 360, GLASS.tolist(), -1
        )
    inside = region > 0
    canvas[inside] = layer[inside]


def make_tissue(
    shape: tuple[int, int] = (800, 1000),
    seed: int = 0,
    n_fragments: int = 2,
    n_debris: int = 20,
    um_per_px: float = UM_PER_PX,
    detail_seed: int | None = None,
) -> SyntheticTissue:
    """A synthetic section at registration resolution; deterministic for its seeds.

    `seed` fixes the shape, fragments, stain bands, vessels and debris; `detail_seed` (default:
    `seed`) fixes the cells and the noise. Two sections with the same `seed` and different
    `detail_seed` model neighbouring serial sections: same geometry (identity transform between
    them), shared large structures, different cells.
    """
    rng = np.random.default_rng(seed)
    detail = np.random.default_rng([seed, seed if detail_seed is None else detail_seed, 1])
    h, w = shape
    canvas = np.empty((h, w, 3), np.float64)
    canvas[:] = GLASS + detail.normal(0, 1.5, (h, w, 3))

    centre = (w / 2 + rng.uniform(-0.03, 0.03) * w, h / 2 + rng.uniform(-0.03, 0.03) * h)
    main = _blob(rng, shape, centre, 0.30 * min(h, w))

    # pale rim: a band along part of the edge, attached to the stained tissue
    band = cv2.dilate(main, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (37, 37))) & ~main
    ang = np.arctan2(np.arange(h)[:, None] - centre[1], np.arange(w)[None] - centre[0])
    start = rng.uniform(-np.pi, np.pi)
    sector = ((ang - start) % (2 * np.pi)) < np.pi / 2
    rim = np.where(sector, band, 0).astype(np.uint8)

    # detached fragments: separate pieces at least 25 px away from the section
    keep_out = cv2.dilate(main | rim, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (51, 51)))
    fragments = np.zeros(shape, np.uint8)
    for _ in range(n_fragments):
        for _try in range(200):
            r = rng.uniform(40, 55)
            c = (rng.uniform(r + 5, w - r - 5), rng.uniform(r + 5, h - r - 5))
            f = _blob(rng, shape, c, r)
            if not (f & (keep_out | fragments)).any():
                fragments |= f
                keep_out |= cv2.dilate(f, np.ones((25, 25), np.uint8))
                break

    _texture(rng, detail, canvas, main, TISSUE)
    _texture(rng, detail, canvas, fragments, 0.75 * TISSUE + 0.25 * PALE)
    canvas[rim > 0] = PALE * (1 - 0.03 * _smooth_field(rng, shape, 20)[rim > 0][:, None])

    # debris: small dark specks on the glass (each well below 0.2 mm²)
    debris = np.zeros(shape, np.uint8)
    glass_ok = cv2.dilate(keep_out, np.ones((21, 21), np.uint8)) == 0
    for x, y in _random_points_in(rng, glass_ok.astype(np.uint8), n_debris):
        cv2.circle(debris, (int(x), int(y)), int(rng.integers(1, 5)), 255, -1)
    canvas[debris > 0] = DEBRIS

    canvas = cv2.GaussianBlur(canvas, (0, 0), 0.7) + detail.normal(0, 2.0, canvas.shape)
    rgb = np.clip(canvas, 0, 255).astype(np.uint8)
    return SyntheticTissue(rgb, main | rim | fragments, fragments, debris, um_per_px)


def apply_similarity(
    rgb: np.ndarray,
    mask: np.ndarray,
    angle_deg: float,
    scale: float = 1.0,
    tx_um: float = 0.0,
    ty_um: float = 0.0,
    um_per_px: float = UM_PER_PX,
) -> tuple[np.ndarray, np.ndarray, Transform2D]:
    """Move a section: rotate by `angle_deg` and scale about the image centre, then shift.

    Returns the moved image, the moved mask and the **truth for registration**: the transform
    mapping the moved image's µm frame back onto the original's (moving -> fixed, contract C2),
    i.e. what `register_pair(fixed=original, moving=moved)` must recover.

    The frame size stays the same: content moved outside it (for example a fragment near a
    corner after a large rotation) is lost, as on a real slide scanned off-centre.
    """
    h, w = mask.shape
    mpp = (um_per_px, um_per_px)
    cx, cy = w * um_per_px / 2, h * um_per_px / 2  # image centre in µm (C1: corner origin)
    to_c = Transform2D.similarity(0.0, 1.0, -cx, -cy)
    rot = Transform2D.similarity(np.radians(angle_deg), scale)
    back = Transform2D.similarity(0.0, 1.0, cx + tx_um, cy + ty_um)
    forward = back.compose(rot).compose(to_c)  # original -> moved
    m = forward.to_pixels(1, 1, mpp)[:2]
    moved = cv2.warpAffine(
        rgb, m, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT,
        borderValue=GLASS.tolist(),
    )  # fmt: skip
    moved_mask = cv2.warpAffine(mask, m, (w, h), flags=cv2.INTER_NEAREST, borderValue=0)
    return moved, moved_mask, forward.inverse()


def perturb(rgb: np.ndarray, mask: np.ndarray, seed: int, strength: float = 1.0) -> np.ndarray:
    """Section-to-section differences without moving the tissue (same geometry, same mask).

    Slight stain change, noise, a few structures removed (blurred away) and added, and one
    fold-like dark streak, all inside the tissue.
    """
    rng = np.random.default_rng(seed)
    out = rgb.astype(np.float64)
    inside = mask > 0
    out[inside] *= 1 + strength * rng.uniform(-0.04, 0.04, 3)
    blurred = cv2.GaussianBlur(out, (0, 0), 6)
    for x, y in _random_points_in(rng, mask, int(5 * strength) + 1):  # structures removed
        disc = np.zeros(mask.shape, np.uint8)
        cv2.circle(disc, (int(x), int(y)), int(rng.integers(8, 16)), 255, -1)
        sel = (disc > 0) & inside
        out[sel] = blurred[sel]
    extra = np.zeros_like(out)
    painted = np.zeros(mask.shape, np.uint8)
    for x, y in _random_points_in(rng, mask, int(150 * strength)):  # structures added
        cv2.circle(extra, (int(x), int(y)), 1, CELL.tolist(), -1)
        cv2.circle(painted, (int(x), int(y)), 1, 255, -1)
    fold = _curve(rng, mask)  # fold-like streak
    cv2.polylines(extra, [fold], False, (0.6 * CELL + 0.4 * TISSUE).tolist(), 4)
    cv2.polylines(painted, [fold], False, 255, 4)
    sel = (painted > 0) & inside
    out[sel] = extra[sel]
    out[inside] += rng.normal(0, 2.5 * strength, (int(inside.sum()), 3))
    return np.clip(out, 0, 255).astype(np.uint8)


def as_reg_image(rgb: np.ndarray, mask: np.ndarray, slice_id: int = 0) -> RegImage:
    """Synthetic section as a registration image, made by the real `make_reg_image` (C4), at the
    real registration level (32x, mpp 0.2205 µm)."""
    return RegImage(slice_id, make_reg_image(rgb, mask), mask, 32.0, (0.2205, 0.2205))
