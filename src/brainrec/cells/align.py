"""Local alignment at cell scale (M7-12; contract C15; method approved by the owner 2026-11-09).

After the pair transform k -> k-1 (C5), neighbouring sections are still off by 5 to 30 µm locally
(M7-01, M7-12 spike), several nucleus diameters. The displacement field of a pair is found from
the nuclei alone (no slide reads), in the frame of slice k-1:

1. **Votes.** Around a node, every nucleus of k-1 within `align_window_um` votes for its offsets
   to the nuclei of k within the search range. Nuclei that recur in both sections vote for the
   same offset; all others spread their votes thinly. The vote histogram (1 µm bins, smoothed)
   peaks at the local shift; the peak is refined to the mean of the votes within 1.5 µm.
2. **Lock.** A node is locked when its peak holds at least MIN_VOTES votes and at least
   `align_min_peak_ratio` x the votes of the best other peak (more than 6 µm away).
3. **Coarse, then fine.** Coarse nodes every COARSE_FACTOR x `align_grid_um` search
   ±`align_search_um`. Then FINE_PASSES fine passes on nodes every `align_grid_um`: each nucleus
   of k is first moved by the current field at its own position (so the field's gradient within a
   window does not smear the votes), and the remainder is voted within ±FINE_SEARCH_UM.
4. **Field.** Locked nodes far from their locked neighbours are dropped as outliers; nodes without
   a lock are filled from the locked ones (normalised Gaussian averaging, wider until all are
   filled) and get confidence 0.
5. **No recurrence.** When fewer than NO_RECURRENCE_SHARE of the coarse nodes lock on (chance is
   1 to 5% on real non-neighbouring slices), the pair shares no recognisable nuclei: the field is
   zero with confidence 0, and linking (M7-13) will find nothing there.

Recurrence is reported per pair: the share of nuclei of k-1 with a nucleus of k within 1.5 µm
after the field, and the same share at a deliberately wrong offset (chance level).
"""

import warnings
from dataclasses import dataclass

import numpy as np
from scipy.interpolate import RegularGridInterpolator
from scipy.ndimage import gaussian_filter
from scipy.spatial import cKDTree

from brainrec.catalog import ProjectConfig

METHOD = "nuclei-votes/1"
COARSE_FACTOR = 4  # coarse node spacing = 4 x align_grid_um (400 µm by default)
FINE_SEARCH_UM = 10.0  # fine search around the current field
FINE_PASSES = 2  # fine passes, each on top of the field of the pass before
PEAK_TOL_UM = 1.5  # votes within this distance of the peak belong to it
BIN_UM = 1.0  # vote histogram bin
EXCLUDE_UM = 6.0  # the best other peak lies further away than this
MIN_NUCLEI = 20  # nuclei of k-1 a window needs to be supported
MIN_VOTES = 3  # a peak with fewer votes never locks
OUTLIER_UM = (5.0, 2.0)  # coarse, fine: largest difference to the median of locked neighbours
NO_RECURRENCE_SHARE = 0.10  # share of locked coarse nodes below which a pair has no recurrence
CHANCE_OFFSET_UM = (30.0, 20.0)  # a wrong offset for the chance level of recurrence


@dataclass(frozen=True)
class AlignParams:
    grid_um: float
    window_um: float
    search_um: float
    min_peak_ratio: float

    @classmethod
    def from_config(cls, cfg: ProjectConfig) -> "AlignParams":
        return cls(cfg.align_grid_um, cfg.align_window_um, cfg.align_search_um,
                   cfg.align_min_peak_ratio)  # fmt: skip


@dataclass(frozen=True)
class Vote:
    shift: np.ndarray  # (2,) µm: add to a nucleus of k to reach its partner in k-1
    n_peak: int
    n_second: int
    n_votes: int

    def locked(self, min_peak_ratio: float) -> bool:
        return self.n_peak >= max(MIN_VOTES, min_peak_ratio * max(self.n_second, 1))


def vote(
    window: np.ndarray, moving: np.ndarray, moving_tree: cKDTree, centre: np.ndarray, search: float
) -> Vote | None:
    """The offset histogram of the nuclei `window` (k-1, µm) to the nuclei `moving` (k, already
    in k-1's frame) within ±`search` µm of `centre`. None without any vote."""
    lists = moving_tree.query_ball_point(window - centre, search)
    counts = np.fromiter((len(x) for x in lists), int, len(lists))
    if counts.sum() == 0:
        return None
    ai = np.repeat(np.arange(len(window)), counts)
    bi = np.fromiter((j for x in lists for j in x), int, int(counts.sum()))
    d = window[ai] - moving[bi]
    nb = max(1, round(2 * search / BIN_UM))
    lo = centre - search
    h, _, _ = np.histogram2d(d[:, 0], d[:, 1], bins=nb,
                             range=[[lo[0], lo[0] + 2 * search], [lo[1], lo[1] + 2 * search]])  # fmt: skip
    h = gaussian_filter(h, 1.0, mode="constant")
    p = np.unravel_index(int(np.argmax(h)), h.shape)
    peak = lo + (np.array(p) + 0.5) * BIN_UM
    for _ in range(3):  # refine: mean of the votes near the peak
        near = np.hypot(*(d - peak).T) < PEAK_TOL_UM
        if not near.any():
            break
        peak = d[near].mean(axis=0)
    n_peak = int((np.hypot(*(d - peak).T) < PEAK_TOL_UM).sum())
    r = round(EXCLUDE_UM / BIN_UM)
    h[max(0, p[0] - r) : p[0] + r + 1, max(0, p[1] - r) : p[1] + r + 1] = 0
    n_second = 0
    if h.max() > 0:
        p2 = np.unravel_index(int(np.argmax(h)), h.shape)
        second = lo + (np.array(p2) + 0.5) * BIN_UM
        n_second = int((np.hypot(*(d - second).T) < PEAK_TOL_UM).sum())
    return Vote(peak, n_peak, n_second, len(d))


@dataclass
class Field:
    """Displacement of slice k's nuclei (after the pair transform) onto slice k-1, on a grid in
    k-1's frame (C15). Arrays (ny, nx); axes in µm."""

    grid_x_um: np.ndarray
    grid_y_um: np.ndarray
    dx_um: np.ndarray
    dy_um: np.ndarray
    node_conf: np.ndarray

    def displacement(self, xy: np.ndarray) -> np.ndarray:
        """Bilinear displacement (n, 2) µm at points of k-1's frame; outside the grid the
        nearest edge value."""
        xy = np.asarray(xy, float).reshape(-1, 2)
        if len(self.grid_x_um) == 1 or len(self.grid_y_um) == 1:
            return np.repeat([[float(self.dx_um.mean()), float(self.dy_um.mean())]], len(xy), 0)
        q = np.c_[np.clip(xy[:, 1], self.grid_y_um[0], self.grid_y_um[-1]),
                  np.clip(xy[:, 0], self.grid_x_um[0], self.grid_x_um[-1])]  # fmt: skip
        axes = (self.grid_y_um, self.grid_x_um)
        return np.c_[RegularGridInterpolator(axes, self.dx_um)(q),
                     RegularGridInterpolator(axes, self.dy_um)(q)]  # fmt: skip

    def arrays(self) -> dict[str, np.ndarray]:
        return {"grid_x_um": self.grid_x_um.astype(np.float64),
                "grid_y_um": self.grid_y_um.astype(np.float64),
                "dx_um": self.dx_um.astype(np.float32), "dy_um": self.dy_um.astype(np.float32),
                "node_conf": self.node_conf.astype(np.float32)}  # fmt: skip

    @classmethod
    def from_arrays(cls, a: dict[str, np.ndarray]) -> "Field":
        return cls(a["grid_x_um"], a["grid_y_um"], a["dx_um"].astype(float),
                   a["dy_um"].astype(float), a["node_conf"].astype(float))  # fmt: skip


@dataclass
class PairAlignment:
    field: Field
    status: str  # "aligned" or "no_recurrence"
    n_nodes: int
    n_supported: int  # nodes with at least MIN_NUCLEI nuclei of k-1 in their window
    n_locked: int
    coarse_share_locked: float
    median_shift_um: float  # of the locked fine nodes
    recurrence: float  # share of k-1's nuclei with a partner in k within 1.5 µm after the field
    recurrence_chance: float  # the same at a wrong offset

    def summary(self) -> dict:
        return {
            "method": METHOD,
            "status": self.status,
            "n_nodes": self.n_nodes,
            "n_supported": self.n_supported,
            "n_locked": self.n_locked,
            "share_locked": round(self.n_locked / self.n_supported, 4) if self.n_supported else 0.0,
            "coarse_share_locked": round(self.coarse_share_locked, 4),
            "median_shift_um": round(self.median_shift_um, 2),
            "recurrence": round(self.recurrence, 4),
            "recurrence_chance": round(self.recurrence_chance, 4),
        }


def _axis(lo: float, hi: float, spacing: float) -> np.ndarray:
    return np.arange(np.floor(lo / spacing) * spacing, hi + spacing, spacing)


def _solve(
    fixed: np.ndarray, fixed_tree: cKDTree, moving: np.ndarray, moving_tree: cKDTree,
    xs: np.ndarray, ys: np.ndarray, centres: np.ndarray, search: float, params: AlignParams,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:  # fmt: skip
    """Vote at every node: shift (ny, nx, 2), locked, confidence, supported."""
    shape = (len(ys), len(xs))
    shift = np.zeros((*shape, 2))
    locked = np.zeros(shape, bool)
    conf = np.zeros(shape)
    supported = np.zeros(shape, bool)
    for i, y in enumerate(ys):
        for j, x in enumerate(xs):
            idx = fixed_tree.query_ball_point((x, y), params.window_um)
            if len(idx) < MIN_NUCLEI:
                continue
            supported[i, j] = True
            v = vote(fixed[idx], moving, moving_tree, centres[i, j], search)
            if v is None:
                continue
            shift[i, j] = v.shift
            if v.locked(params.min_peak_ratio):
                locked[i, j] = True
                conf[i, j] = 1.0 - v.n_second / v.n_peak
    return shift, locked, conf, supported


def _drop_outliers(shift: np.ndarray, locked: np.ndarray, limit: float) -> np.ndarray:
    """Locked nodes further than `limit` µm from the median of their locked 3 x 3 neighbours
    (at least two of them) are no longer locked."""
    ny, nx = locked.shape
    pad = np.full((ny + 2, nx + 2, 2), np.nan)
    pad[1:-1, 1:-1][locked] = shift[locked]
    neigh = np.stack([pad[1 + di : 1 + di + ny, 1 + dj : 1 + dj + nx]
                      for di in (-1, 0, 1) for dj in (-1, 0, 1) if (di, dj) != (0, 0)])  # fmt: skip
    n = np.isfinite(neigh[..., 0]).sum(axis=0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN neighbourhoods
        med = np.nanmedian(neigh, axis=0)
    off = np.hypot(*(shift - med).transpose(2, 0, 1))
    bad = locked & (n >= 2) & (off > limit)
    return locked & ~bad


def _fill(shift: np.ndarray, locked: np.ndarray, fallback: np.ndarray) -> np.ndarray:
    """Shifts of the locked nodes, the others from a normalised Gaussian average of the locked
    ones (σ = 1, 2, 4 ... nodes until each node has support), else `fallback`."""
    out = np.where(locked[..., None], shift, np.nan)
    todo = ~locked
    w = locked.astype(float)
    for sigma in (1, 2, 4, 8, 16, 32):
        if not todo.any() or not locked.any():
            break
        den = gaussian_filter(w, sigma, mode="nearest")
        ok = todo & (den > 1e-3)
        for c in range(2):
            num = gaussian_filter(np.where(locked, shift[..., c], 0.0), sigma, mode="nearest")
            out[..., c][ok] = num[ok] / den[ok]
        todo &= ~ok
    out[todo] = fallback[todo]
    return out


def smooth(
    shift: np.ndarray, locked: np.ndarray, limit: float, fallback: np.ndarray | None = None
) -> tuple[np.ndarray, np.ndarray]:
    """The field of a stage: outliers among the locked nodes dropped (`limit` µm), then every
    other node filled from the locked ones (`_fill`; `fallback` defaults to the median locked
    shift, else 0). Returns (filled shift (ny, nx, 2), locked after the outlier check)."""
    locked = _drop_outliers(shift, locked, limit)
    if fallback is None:
        median = np.median(shift[locked], axis=0) if locked.any() else np.zeros(2)
        fallback = np.broadcast_to(median, shift.shape)
    return _fill(shift, locked, fallback), locked


def _recurrence(fixed: np.ndarray, moved: np.ndarray) -> float:
    if not len(fixed) or not len(moved):
        return 0.0
    d, _ = cKDTree(moved).query(fixed, distance_upper_bound=PEAK_TOL_UM)
    return float(np.isfinite(d).mean())


def align_pair(fixed: np.ndarray, moving: np.ndarray, params: AlignParams) -> PairAlignment:
    """Displacement field of a pair from its nuclei centres: `fixed` of slice k-1 and `moving`
    of slice k already through the pair transform, both (n, 2) µm in k-1's frame."""
    fixed = np.asarray(fixed, float).reshape(-1, 2)
    moving = np.asarray(moving, float).reshape(-1, 2)
    pts = fixed if len(fixed) else np.zeros((1, 2))
    xs = _axis(pts[:, 0].min(), pts[:, 0].max(), params.grid_um)
    ys = _axis(pts[:, 1].min(), pts[:, 1].max(), params.grid_um)
    zero = np.zeros((len(ys), len(xs)))
    if len(fixed) < MIN_NUCLEI or len(moving) == 0:
        return PairAlignment(Field(xs, ys, zero, zero.copy(), zero.copy()), "no_recurrence",
                             zero.size, 0, 0, 0.0, 0.0, 0.0, 0.0)  # fmt: skip
    fixed_tree, moving_tree = cKDTree(fixed), cKDTree(moving)

    # coarse: wide search
    step = COARSE_FACTOR * params.grid_um
    cxs = _axis(fixed[:, 0].min(), fixed[:, 0].max(), step)
    cys = _axis(fixed[:, 1].min(), fixed[:, 1].max(), step)
    c_shift, c_locked, _, c_sup = _solve(fixed, fixed_tree, moving, moving_tree, cxs, cys,
                                         np.zeros((len(cys), len(cxs), 2)), params.search_um,
                                         params)  # fmt: skip
    c_field, c_locked = smooth(c_shift, c_locked, OUTLIER_UM[0])
    coarse_share = float(c_locked.sum() / c_sup.sum()) if c_sup.any() else 0.0
    if coarse_share < NO_RECURRENCE_SHARE:
        n_sup = sum(len(fixed_tree.query_ball_point((x, y), params.window_um)) >= MIN_NUCLEI
                    for y in ys for x in xs)  # fmt: skip
        return PairAlignment(Field(xs, ys, zero, zero.copy(), zero.copy()), "no_recurrence",
                             zero.size, int(n_sup), 0, coarse_share, 0.0,
                             _recurrence(fixed, moving),
                             _recurrence(fixed, moving + CHANCE_OFFSET_UM))  # fmt: skip
    coarse = Field(cxs, cys, c_field[..., 0], c_field[..., 1], np.zeros(c_locked.shape))

    # fine: every nucleus moved by the current field at its own position, so a steep field does
    # not smear the votes within a window; the remainder is voted within ±FINE_SEARCH_UM
    gx, gy = np.meshgrid(xs, ys)
    nodes = np.c_[gx.ravel(), gy.ravel()]
    field = coarse
    for _ in range(FINE_PASSES):
        base = field.displacement(nodes).reshape(len(ys), len(xs), 2)
        moved = moving + field.displacement(moving)
        rest, locked, conf, sup = _solve(fixed, fixed_tree, moved, cKDTree(moved), xs, ys,
                                         np.zeros_like(base), FINE_SEARCH_UM, params)  # fmt: skip
        shift = base + rest
        filled, locked = smooth(shift, locked, OUTLIER_UM[1], base)
        field = Field(xs, ys, filled[..., 0], filled[..., 1], np.where(locked, conf, 0.0))
    moved = moving + field.displacement(moving)
    med_shift = float(np.median(np.hypot(*shift[locked].T))) if locked.any() else 0.0
    return PairAlignment(
        field, "aligned", zero.size, int(sup.sum()), int(locked.sum()), coarse_share, med_shift,
        _recurrence(fixed, moved), _recurrence(fixed, moved + CHANCE_OFFSET_UM),
    )  # fmt: skip
