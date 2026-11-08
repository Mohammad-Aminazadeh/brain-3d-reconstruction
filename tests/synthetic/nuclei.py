"""Synthetic nuclei and synthetic 3D cells with known truth (M7-03), for the cell tasks M7-05 to
M7-15. Classes and looks follow decision 0006 and the appearance groups of M7-18.

(a) `make_nuclei`: an RGB image at the 2x level (0.441 µm/px, contract C13) with nuclei of the
    six classes, touching pairs, cells across tile borders, a glass strip with debris (never a
    cell, outside the tissue mask), and the truth per nucleus (centre and equivalent-area radius
    in µm, class, nucleolus).
(b) `make_cell_field` + `cut_sections`: spheres in a block of tissue cut into consecutive
    sections. Each section is moved on its slide by a known similarity transform (contract C2,
    slice -> reference) and deformed by a known smooth field; the truth per profile is its cell.

Coordinates follow C1: pixel (i, j) has its centre at ((i + 0.5) d, (j + 0.5) d) µm.
"""

from dataclasses import dataclass, field

import cv2
import numpy as np

from brainrec.detect.labels import CLASSES
from brainrec.registration import Transform2D

UM_PER_PX_2X = 2 * 0.2205
NEUROPIL = np.array([208, 196, 226], float)
GLASS = np.array([236, 233, 236], float)
DEBRIS = np.array([80, 60, 95], float)
LUMEN = np.array([240, 236, 242], float)
HALO = np.array([234, 228, 242], float)
BODY = np.array([158, 130, 194], float)  # stained neuron cell body (Nissl-like)
NUCLEOLUS = np.array([70, 45, 110], float)
DARK = np.array([95, 70, 140], float)
MEDIUM = np.array([130, 105, 172], float)
PALE = np.array([168, 148, 200], float)
NEURON_NUCLEUS = np.array([196, 182, 222], float)  # paler than its cell body (M7-01)

# class -> (radius range µm, axis ratio range, nucleus colour, nucleolus, cell body, halo, lumen)
LOOKS: dict[str, dict] = {
    "Neuron": {"r": (4.0, 5.5), "ratio": (1.0, 1.15), "colour": NEURON_NUCLEUS, "nucleolus": True,
               "body": True, "halo": False, "lumen": False},
    "Astrocyte-like": {"r": (3.3, 4.2), "ratio": (1.15, 1.4), "colour": PALE, "nucleolus": False,
                       "body": False, "halo": False, "lumen": False},
    "Oligodendrocyte-like": {"r": (2.4, 3.0), "ratio": (1.0, 1.1), "colour": DARK,
                             "nucleolus": False, "body": False, "halo": True, "lumen": False},
    "Microglia-like": {"r": (2.4, 3.2), "ratio": (2.2, 3.0), "colour": DARK, "nucleolus": False,
                       "body": False, "halo": False, "lumen": False},
    "Vessel-wall": {"r": (2.4, 3.2), "ratio": (2.5, 3.2), "colour": MEDIUM, "nucleolus": False,
                    "body": False, "halo": False, "lumen": True},
    "Other": {"r": (2.8, 3.4), "ratio": (1.0, 1.2), "colour": MEDIUM, "nucleolus": False,
              "body": False, "halo": False, "lumen": False},
}  # fmt: skip
assert tuple(LOOKS) == CLASSES
SHIFT = 4  # cv2 sub-pixel drawing: coordinates in 1/16 px


@dataclass
class NucleusTruth:
    """Truth per nucleus: centre (µm, C1), equivalent-area radius (µm), axis ratio, angle (deg),
    class, nucleolus drawn, touching-pair id (-1 = none), lies across a tile border."""

    x_um: np.ndarray
    y_um: np.ndarray
    r_um: np.ndarray
    ratio: np.ndarray
    angle_deg: np.ndarray
    cls: np.ndarray
    nucleolus: np.ndarray
    pair: np.ndarray
    on_border: np.ndarray

    def __len__(self) -> int:
        return len(self.x_um)


@dataclass(frozen=True)
class SyntheticNuclei:
    rgb: np.ndarray  # (h, w, 3) uint8
    mask: np.ndarray  # uint8 {0, 255}: tissue; the glass strip on the left is 0
    truth: NucleusTruth
    um_per_px: float
    tile_px: int  # tile size whose borders some nuclei cross
    debris: np.ndarray = field(repr=False)  # uint8 {0, 255}: dark specks on the glass


def _ellipse(img, centre_px, r_px: float, ratio: float, angle: float, colour, thickness=-1):
    a, b = r_px * np.sqrt(ratio), r_px / np.sqrt(ratio)
    c = (round(centre_px[0] * 2**SHIFT), round(centre_px[1] * 2**SHIFT))
    axes = (round(a * 2**SHIFT), round(b * 2**SHIFT))
    cv2.ellipse(img, c, axes, angle, 0, 360, tuple(float(v) for v in colour), thickness,
                cv2.LINE_AA, SHIFT)  # fmt: skip


def make_nuclei(
    shape: tuple[int, int] = (1024, 1024),
    seed: int = 0,
    n_per_class: int = 8,
    touching_pairs: int = 3,
    tile_px: int = 512,
    n_on_border: int = 4,
    glass_px: int = 120,
    um_per_px: float = UM_PER_PX_2X,
) -> SyntheticNuclei:
    """Nuclei of every class (`n_per_class` each) at random, non-overlapping places inside the
    tissue; `touching_pairs` pairs of oligodendrocyte-like nuclei that touch; `n_on_border`
    nuclei whose outline crosses a line between tiles of `tile_px`. The left `glass_px` columns
    are glass with dark debris. Deterministic for `seed`."""
    rng = np.random.default_rng(seed)
    h, w = shape
    mask = np.zeros(shape, np.uint8)
    mask[:, glass_px:] = 255
    margin = 12.0 / um_per_px  # keeps cells and their bodies inside the image and the tissue

    specs: list[tuple[str, float, float, float, int]] = []  # class, r, ratio, angle, pair
    for cls in CLASSES:
        look = LOOKS[cls]
        for _ in range(n_per_class):
            specs.append((cls, rng.uniform(*look["r"]), rng.uniform(*look["ratio"]),
                          rng.uniform(0, 180), -1))  # fmt: skip

    centres: list[np.ndarray] = []
    reach: list[float] = []  # radius of everything drawn around a nucleus, px

    def reach_px(cls: str, r_um: float, ratio: float) -> float:
        look = LOOKS[cls]
        extent = r_um * np.sqrt(ratio) * (2.6 if look["body"] else 1.6 if look["lumen"] else 1.4)
        return extent / um_per_px

    def free(c: np.ndarray, rr: float) -> bool:
        return all(np.hypot(*(c - o)) >= rr + ro + 2 for o, ro in zip(centres, reach, strict=True))

    def place(rr: float, near_border: bool) -> np.ndarray:
        for _ in range(5000):
            c = np.array(
                [rng.uniform(glass_px + margin, w - margin), rng.uniform(margin, h - margin)]
            )
            if near_border:
                lines = np.arange(tile_px, w, tile_px)
                c[0] = rng.choice(lines) - 0.5 + rng.uniform(-0.6, 0.6) * rr / 1.4
            if free(c, rr):
                return c
        raise RuntimeError("no free place for a synthetic nucleus; lower the number of cells")

    border_idx = set(rng.choice(len(specs), min(n_on_border, len(specs)), replace=False).tolist())
    truth_rows: list[tuple] = []
    for i, (cls, r, ratio, angle, _) in enumerate(specs):
        rr = reach_px(cls, r, ratio)
        c = place(rr, i in border_idx)
        centres.append(c)
        reach.append(rr)
        truth_rows.append((c, r, ratio, angle, cls, -1))
    for p in range(touching_pairs):  # two round dark nuclei side by side, outlines touching
        r1, r2 = rng.uniform(2.6, 3.0, 2)
        rr = (r1 + r2 + 1) / um_per_px * 1.4
        c = place(rr, False)
        d = rng.uniform(0, np.pi)
        off = np.array([np.cos(d), np.sin(d)]) * (r1 + r2) / um_per_px / 2
        for rad, sign in ((r1, -1), (r2, 1)):
            truth_rows.append((c + sign * off, rad, 1.0, 0.0, "Oligodendrocyte-like", p))
        centres.append(c)
        reach.append(rr)

    rgb = np.empty((h, w, 3), float)
    rgb[:] = NEUROPIL
    rgb[:, :glass_px] = GLASS
    for c, r, ratio, angle, cls, _ in truth_rows:  # surroundings first
        look = LOOKS[cls]
        r_px = r / um_per_px
        if look["body"]:
            _ellipse(rgb, c, r_px * 2.3, 1.3, angle + 90, BODY)
        if look["halo"]:
            _ellipse(rgb, c, r_px * 1.35, ratio, angle, HALO)
        if look["lumen"]:  # a lumen along the long side of the nucleus
            a = np.radians(angle + 90)
            lc = c + np.array([np.cos(a), np.sin(a)]) * r_px / np.sqrt(ratio) * 1.9
            _ellipse(rgb, lc, r_px * 1.2, 5.0, angle, LUMEN)
    for c, r, ratio, angle, cls, _ in truth_rows:
        look = LOOKS[cls]
        _ellipse(rgb, c, r / um_per_px, ratio, angle, look["colour"])
        if look["nucleolus"]:
            _ellipse(rgb, c, 0.7 / um_per_px, 1.0, 0.0, NUCLEOLUS)
    debris = np.zeros(shape, np.uint8)
    for _ in range(12):
        c = (int(rng.uniform(10, glass_px - 10)), int(rng.uniform(10, h - 10)))
        cv2.circle(debris, c, int(rng.integers(3, 8)), 255, -1)
    rgb[debris > 0] = DEBRIS
    rgb += rng.normal(0, 4.0, rgb.shape)
    rgb = np.clip(cv2.GaussianBlur(rgb, (0, 0), 0.6), 0, 255).astype(np.uint8)

    xs = np.array([(c[0] + 0.5) * um_per_px for c, *_ in truth_rows])
    ys = np.array([(c[1] + 0.5) * um_per_px for c, *_ in truth_rows])
    r_all = np.array([t[1] for t in truth_rows])
    ratio_all = np.array([t[2] for t in truth_rows])
    half_long = r_all * np.sqrt(ratio_all) / um_per_px
    cx = xs / um_per_px - 0.5
    lines = np.arange(tile_px, w, tile_px) - 0.5
    on_border = np.array(
        [bool(np.any(np.abs(x - lines) < hl)) for x, hl in zip(cx, half_long, strict=True)]
    )
    truth = NucleusTruth(
        xs, ys, r_all, ratio_all, np.array([t[3] for t in truth_rows]),
        np.array([t[4] for t in truth_rows], dtype=object),
        np.array([LOOKS[t[4]]["nucleolus"] for t in truth_rows]),
        np.array([t[5] for t in truth_rows]), on_border,
    )  # fmt: skip
    return SyntheticNuclei(rgb, mask, truth, um_per_px, tile_px, debris)


# --- (b) 3D cells cut into sections ----------------------------------------------------------


@dataclass
class CellField:
    """Spheres in a block: centre (µm; x, y in the reference frame, z = depth), radius, class."""

    x_um: np.ndarray
    y_um: np.ndarray
    z_um: np.ndarray
    r_um: np.ndarray
    cls: np.ndarray

    def __len__(self) -> int:
        return len(self.x_um)


def make_cell_field(
    extent_um: tuple[float, float] = (400.0, 400.0),
    depth_um: float = 40.0,
    n_cells: int = 300,
    seed: int = 0,
) -> CellField:
    """`n_cells` non-overlapping spheres (classes in turn, radii from LOOKS) in a block of
    `extent_um` x `depth_um`. Deterministic for `seed`."""
    rng = np.random.default_rng(seed)
    pts: list[np.ndarray] = []
    radii: list[float] = []
    classes: list[str] = []
    for i in range(n_cells):
        cls = CLASSES[i % len(CLASSES)]
        r = float(rng.uniform(*LOOKS[cls]["r"]))
        for _ in range(5000):
            p = rng.uniform([r, r, 0.0], [extent_um[0] - r, extent_um[1] - r, depth_um])
            if all(np.linalg.norm(p - q) >= r + rq + 0.5 for q, rq in zip(pts, radii, strict=True)):
                break
        else:
            raise RuntimeError("block too full; lower n_cells")
        pts.append(p)
        radii.append(r)
        classes.append(cls)
    a = np.array(pts)
    return CellField(a[:, 0], a[:, 1], a[:, 2], np.array(radii), np.array(classes, dtype=object))


@dataclass
class Deformation:
    """Smooth in-plane displacement (µm) of one section: a sum of two sine waves per axis."""

    amp: np.ndarray  # (2, 2): axis x/y, wave 1/2
    wavelength: np.ndarray  # (2, 2) µm
    phase: np.ndarray  # (2, 2)

    def __call__(self, xy: np.ndarray) -> np.ndarray:
        x, y = xy[:, 0], xy[:, 1]
        dx = sum(self.amp[0, i] * np.sin(2 * np.pi * (y / self.wavelength[0, i]) + self.phase[0, i])
                 for i in range(2))  # fmt: skip
        dy = sum(self.amp[1, i] * np.sin(2 * np.pi * (x / self.wavelength[1, i]) + self.phase[1, i])
                 for i in range(2))  # fmt: skip
        return np.stack([dx, dy], axis=1)


@dataclass
class Profiles:
    """Cut surfaces of cells in one section (slice frame, µm), with the truth."""

    x_um: np.ndarray
    y_um: np.ndarray
    r_um: np.ndarray  # largest cut radius within the section, in slice µm
    cls: np.ndarray
    cell_id: np.ndarray  # index into the CellField

    def __len__(self) -> int:
        return len(self.x_um)


@dataclass
class SectionStack:
    thickness_um: float
    transforms: list[Transform2D]  # slice -> reference (C2), one per section
    deformations: list[Deformation]  # in the reference frame, before the transform
    sections: list[Profiles]

    def to_slice(self, k: int, ref_xy: np.ndarray) -> np.ndarray:
        """Where a reference point lies in section k's slice frame."""
        return self.transforms[k].inverse().apply(ref_xy + self.deformations[k](ref_xy))


def cut_sections(
    cells: CellField,
    n_sections: int,
    thickness_um: float,
    seed: int = 0,
    max_angle_deg: float = 5.0,
    max_shift_um: float = 200.0,
    scale_range: tuple[float, float] = (0.97, 1.03),
    deform_um: float = 10.0,
    min_profile_r_um: float = 1.6,
) -> SectionStack:
    """Cut `cells` into consecutive sections k = 0 .. n-1 covering depth [k t, (k + 1) t).

    A cell gives a profile in section k if its largest cut radius within the section,
    sqrt(R² - dz²) with dz the distance from its centre to the section, is at least
    `min_profile_r_um` (as seen in a brightfield section, which shows its whole thickness).
    Each section is deformed by a smooth field of amplitude up to `deform_um` and moved by a
    random similarity; profiles are given in the slice frame, radii in slice µm."""
    rng = np.random.default_rng(seed)
    transforms, deformations, sections = [], [], []
    ref_xy = np.stack([cells.x_um, cells.y_um], axis=1)
    for k in range(n_sections):
        t = Transform2D.similarity(
            np.radians(rng.uniform(-max_angle_deg, max_angle_deg)),
            float(rng.uniform(*scale_range)),
            float(rng.uniform(-max_shift_um, max_shift_um)),
            float(rng.uniform(-max_shift_um, max_shift_um)),
        )
        d = Deformation(
            rng.uniform(-deform_um / 2, deform_um / 2, (2, 2)),
            rng.uniform(150.0, 600.0, (2, 2)),
            rng.uniform(0, 2 * np.pi, (2, 2)),
        )
        lo, hi = k * thickness_um, (k + 1) * thickness_um
        dz = np.where((cells.z_um >= lo) & (cells.z_um <= hi), 0.0,
                      np.minimum(np.abs(cells.z_um - lo), np.abs(cells.z_um - hi)))  # fmt: skip
        rho = np.sqrt(np.clip(cells.r_um**2 - dz**2, 0.0, None))
        keep = np.nonzero(rho >= min_profile_r_um)[0]
        q = t.inverse().apply(ref_xy[keep] + d(ref_xy[keep]))
        sections.append(Profiles(q[:, 0], q[:, 1], rho[keep] / t.scale, cells.cls[keep], keep))
        transforms.append(t)
        deformations.append(d)
    return SectionStack(thickness_um, transforms, deformations, sections)


# --- (c) a stand-in detector for the synthetic images -------------------------------------------

_NUCLEUS_COLOURS = np.array([NEURON_NUCLEUS, PALE, DARK, MEDIUM, NUCLEOLUS, DEBRIS])
_OTHER_COLOURS = np.array([NEUROPIL, GLASS, LUMEN, HALO, BODY])


def segment_by_colour(rgb: np.ndarray, um_per_px: float) -> tuple[np.ndarray, None]:
    """Labels of the synthetic nuclei (and debris). A stand-in for the real detector (same
    signature as `brainrec.detect.nuclei.Segment`), so that tiling and measurements can be
    tested against the truth without a deep-learning model.

    Pixels are first sorted by their nearest drawing colour. Each object is then cut again at
    half way between its own colour and the background around it (so its edge is not biased
    by intermediate colours), and objects with a neck (solidity below 0.9: touching nuclei) are
    split by a distance watershed."""
    from scipy import ndimage as ndi
    from skimage import measure
    from skimage.morphology import h_maxima
    from skimage.segmentation import watershed

    img = rgb.astype(float)
    palette = np.vstack([_NUCLEUS_COLOURS, _OTHER_COLOURS])
    nearest = ((img[:, :, None, :] - palette[None, None]) ** 2).sum(axis=3).argmin(axis=2)
    # thin rims of intermediate colour (edges of cell bodies) are not nuclei
    rough = ndi.binary_opening(nearest < len(_NUCLEUS_COLOURS), np.ones((3, 3)))
    comp, n = ndi.label(ndi.binary_fill_holes(rough))
    fg = np.zeros(rough.shape, bool)
    for k, sl in enumerate(ndi.find_objects(comp), start=1):
        sl = tuple(slice(max(0, s.start - 3), s.stop + 3) for s in sl)
        own = comp[sl] == k
        ring = ndi.binary_dilation(own, iterations=2) & ~ndi.binary_dilation(own) & ~rough[sl]
        inside = nearest[sl][own & rough[sl]]
        if not ring.any() or not inside.size:
            fg[sl] |= own
            continue
        colour = palette[np.bincount(inside).argmax()]
        bg = palette[np.bincount(nearest[sl][ring], minlength=len(palette)).argmax()]
        x = img[sl]
        near_own = ((x - colour) ** 2).sum(axis=2) < ((x - bg) ** 2).sum(axis=2)
        fg[sl] |= ndi.binary_fill_holes(near_own & ndi.binary_dilation(own, iterations=2))
    labels, n = ndi.label(fg)
    dist = ndi.distance_transform_edt(fg)
    for p in measure.regionprops(labels):
        if p.solidity >= 0.9:  # one convex object
            continue
        sl = p.slice
        own = labels[sl] == p.label
        markers, _ = ndi.label(h_maxima(dist[sl] * own, 1.0 / um_per_px), structure=np.ones((3, 3)))
        parts = watershed(-dist[sl], markers, mask=own)
        labels[sl][parts > 1] = n + parts[parts > 1] - 1
        n += max(int(parts.max()) - 1, 0)
    return labels.astype(np.int32), None
