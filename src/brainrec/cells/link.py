"""Linking profiles into 3D cells (M7-13; contracts C15, C16).

A nucleus cut by several consecutive sections leaves one profile in each. After the local
alignment (M7-12), profiles of slices k and k-1 are linked when their centres are closer than
max(`link_tolerance_um`, 0.5 x the smaller radius); one-to-one, closest first; never across the
classes Neuron (1) and Oligodendrocyte-like (3) unless both confidences are below 0.5. Linked
profiles form chains through the slice range; a chain longer than `link_max_sections` is split at
its weakest (longest) link until it fits. A chain is one cell:

- representative = its largest profile (closest to the equator): position, slice, cell id;
- class = argmax of the summed class probabilities of its profiles; confidence = their mean
  probability of that class;
- nucleolus seen in any profile; 3D radius fitted from the chain's radii once
  `section_thickness_um` is set (otherwise NaN).
"""

from dataclasses import dataclass

import numpy as np
from scipy.spatial import cKDTree

from brainrec.cells.place import Placed

NEURON, OLIGODENDROCYTE = 1, 3  # class ids (C14) that never link unless both are doubtful
DOUBTFUL = 0.5  # confidence below which a profile's class is doubtful
Z0_STEPS = 11  # candidate equator depths within the representative's section (3D radius)


@dataclass
class Links:
    """Links of one pair: profile indices in slice k (`moving`) and k-1 (`fixed`)."""

    moving: np.ndarray  # int32
    fixed: np.ndarray  # int32
    dist_um: np.ndarray  # float32

    def __len__(self) -> int:
        return len(self.moving)

    @classmethod
    def empty(cls) -> "Links":
        return cls(np.zeros(0, np.int32), np.zeros(0, np.int32), np.zeros(0, np.float32))

    def arrays(self) -> dict[str, np.ndarray]:
        """The C15 arrays: `links` (m, 2) = (index in k, index in k-1), `link_dist_um`."""
        return {"links": np.c_[self.moving, self.fixed].astype(np.int32).reshape(-1, 2),
                "link_dist_um": self.dist_um.astype(np.float32)}  # fmt: skip

    @classmethod
    def from_arrays(cls, a: dict[str, np.ndarray]) -> "Links":
        pairs = np.asarray(a["links"]).reshape(-1, 2)
        return cls(pairs[:, 0].astype(np.int32), pairs[:, 1].astype(np.int32),
                   np.asarray(a["link_dist_um"], np.float32))  # fmt: skip


@dataclass(frozen=True)
class LinkSide:
    """What linking needs of one slice's profiles; `xy` in the frame of slice k-1 (µm)."""

    xy: np.ndarray
    r_um: np.ndarray
    class_id: np.ndarray
    confidence: np.ndarray


def link_profiles(fixed: LinkSide, moving: LinkSide, tolerance_um: float) -> Links:
    """Link the profiles of slice k (`moving`, already through the pair transform and the local
    field) to those of k-1 (`fixed`) by the C15 rule."""
    if not len(fixed.xy) or not len(moving.xy):
        return Links.empty()
    reach = max(tolerance_um, 0.5 * float(max(fixed.r_um.max(), moving.r_um.max())))
    lists = cKDTree(fixed.xy).query_ball_point(moving.xy, reach)
    counts = np.fromiter((len(x) for x in lists), int, len(lists))
    mi = np.repeat(np.arange(len(moving.xy)), counts)
    fi = np.fromiter((j for x in lists for j in x), int, int(counts.sum()))
    d = np.hypot(*(moving.xy[mi] - fixed.xy[fi]).T)
    ok = d < np.maximum(tolerance_um, 0.5 * np.minimum(fixed.r_um[fi], moving.r_um[mi]))
    cf, cm = fixed.class_id[fi], moving.class_id[mi]
    clash = ((cf == NEURON) & (cm == OLIGODENDROCYTE)) | ((cf == OLIGODENDROCYTE) & (cm == NEURON))
    doubtful = (fixed.confidence[fi] < DOUBTFUL) & (moving.confidence[mi] < DOUBTFUL)
    ok &= ~clash | doubtful
    mi, fi, d = mi[ok], fi[ok], d[ok]
    order = np.argsort(d, kind="stable")  # closest first
    used_m = np.zeros(len(moving.xy), bool)
    used_f = np.zeros(len(fixed.xy), bool)
    keep = []
    for i in order:
        if not used_m[mi[i]] and not used_f[fi[i]]:
            used_m[mi[i]] = used_f[fi[i]] = True
            keep.append(i)
    k = np.array(keep, int)
    return Links(mi[k].astype(np.int32), fi[k].astype(np.int32), d[k].astype(np.float32))


def build_chains(sizes: list[int], links: list[Links], max_sections: int) -> tuple[np.ndarray, int]:
    """Chains through a slice range. `sizes[s]`: profiles of the s-th slice; `links[s - 1]`:
    links of slice s onto slice s - 1. Profiles are numbered globally, slice after slice.
    Returns (chain label per profile = global index of the chain's first profile, number of
    splits of over-long chains)."""
    if len(links) != max(len(sizes) - 1, 0):
        raise ValueError(f"{len(sizes)} slices need {len(sizes) - 1} link sets, got {len(links)}")
    off = np.concatenate([[0], np.cumsum(sizes)]).astype(np.int64)
    label = np.arange(off[-1], dtype=np.int64)
    prev_dist = np.full(off[-1], np.nan)
    for s, lk in enumerate(links, start=1):
        if not len(lk):
            continue
        if lk.moving.max() >= sizes[s] or lk.fixed.max() >= sizes[s - 1]:
            raise ValueError(f"links of slice position {s} point past its profiles")
        idx = off[s] + lk.moving
        label[idx] = label[off[s - 1] + lk.fixed]  # the previous slice is final already
        prev_dist[idx] = lk.dist_um
    n_split = 0
    while True:
        counts = np.bincount(label)
        long_ = np.nonzero(counts > max_sections)[0]
        if not len(long_):
            return label, n_split
        idx = np.nonzero(np.isin(label, long_))[0]  # one pass over all profiles
        idx = idx[np.argsort(label[idx], kind="stable")]  # grouped by chain, in slice order
        bounds = np.flatnonzero(np.diff(label[idx])) + 1
        for members in np.split(idx, bounds):  # first member = the chain's label
            cut = members[1 + int(np.nanargmax(prev_dist[members[1:]]))]
            label[members[members >= cut]] = cut
            prev_dist[cut] = np.nan
            n_split += 1


@dataclass
class Cells:
    """3D cells of a slice range (C16), one row per chain."""

    rep: np.ndarray  # global profile index of the representative (largest profile)
    class_id: np.ndarray  # uint8
    confidence: np.ndarray  # float32
    n_profiles: np.ndarray  # uint16
    nucleolus: np.ndarray  # uint8: seen in any profile
    radius_3d_um: np.ndarray  # float32, NaN while the section thickness is unknown
    class_prob: np.ndarray  # (n_cells, n_classes) float32: mean over the chain's profiles
    profile_cell: np.ndarray  # (n_profiles,) int64: the cell (row) of every profile

    def __len__(self) -> int:
        return len(self.rep)


def sphere_radius(
    r_um: np.ndarray, section: np.ndarray, rep_section: np.ndarray, groups: np.ndarray,
    n_groups: int, thickness_um: float,
) -> np.ndarray:  # fmt: skip
    """3D radius per chain from its profiles: each profile shows the sphere's largest cut within
    its section [j t, (j + 1) t), so R² = ρ² + d², d the distance from the equator to that
    section. The equator is searched within the representative's section (the largest cut);
    R is the mean over the profiles at the depth where they agree best."""
    best_r = np.zeros(n_groups)
    best_var = np.full(n_groups, np.inf)
    n = np.bincount(groups, minlength=n_groups).astype(float)
    for f in np.linspace(0.0, 1.0, Z0_STEPS):
        z0 = (rep_section[groups] + f) * thickness_um
        lo, hi = section * thickness_um, (section + 1) * thickness_um
        d = np.where(z0 < lo, lo - z0, np.where(z0 > hi, z0 - hi, 0.0))
        rr = np.sqrt(r_um**2 + d**2)
        mean = np.bincount(groups, rr, n_groups) / n
        var = np.bincount(groups, (rr - mean[groups]) ** 2, n_groups) / n
        better = var < best_var
        best_r[better], best_var[better] = mean[better], var[better]
    return best_r


def build_cells(
    label: np.ndarray,
    r_um: np.ndarray,
    class_prob: np.ndarray,
    nucleolus: np.ndarray,
    section: np.ndarray,
    thickness_um: float | None,
) -> Cells:
    """Cells from chain labels and the profile columns (global order); `section` = position of
    each profile's slice in the range."""
    _, groups = np.unique(label, return_inverse=True)
    groups = groups.reshape(-1)
    n_cells = int(groups.max()) + 1 if len(groups) else 0
    order = np.lexsort((-r_um, groups))  # per chain, largest profile first
    first = np.ones(len(order), bool)
    first[1:] = groups[order][1:] != groups[order][:-1]
    rep = order[first]
    n = np.bincount(groups, minlength=n_cells)
    sums = np.zeros((n_cells, class_prob.shape[1]))
    np.add.at(sums, groups, class_prob)
    class_id = sums.argmax(axis=1) + 1 if n_cells else np.zeros(0, int)
    confidence = sums[np.arange(n_cells), class_id - 1] / np.maximum(n, 1)
    seen = np.zeros(n_cells, np.uint8)
    np.maximum.at(seen, groups, nucleolus.astype(np.uint8))
    if thickness_um is None:
        radius = np.full(n_cells, np.nan)
    else:
        radius = sphere_radius(r_um.astype(float), section, section[rep], groups, n_cells,
                               thickness_um)  # fmt: skip
    mean = sums / np.maximum(n, 1)[:, None]
    return Cells(rep.astype(np.int64), class_id.astype(np.uint8), confidence.astype(np.float32),
                 n.astype(np.uint16), seen, radius.astype(np.float32), mean.astype(np.float32),
                 groups.astype(np.int64))  # fmt: skip


def place_cells(profiles: Placed, cells: Cells, proportions: np.ndarray) -> Placed:
    """Cells in the volume frame: the representatives of `profiles` (every profile placed on
    its own, global order) with the chain's class, confidence, profile count and nucleolus flag;
    radii = representative sphere radius x the chain class's proportions (C14, C16)."""
    out = profiles.subset(cells.rep)
    out.class_id = cells.class_id.copy()
    out.confidence = cells.confidence.copy()
    out.n_profiles = cells.n_profiles.copy()
    out.nucleolus = cells.nucleolus.copy()
    k = cells.class_id.astype(int) - 1
    out.radii = (out.radius_um[:, None] * np.asarray(proportions, float)[k]).astype(np.float32)
    out.radii = out.radii.reshape(len(out), 3)
    return out
