"""M7-12: local alignment at cell scale (C15) on synthetic sections cut from 3D cells."""

import json

import numpy as np
import pytest

from brainrec.catalog import DEFAULT_CELL_TYPES, Catalog, ProjectConfig
from brainrec.cells.align import (
    MIN_NUCLEI,
    AlignParams,
    Field,
    _drop_outliers,
    _fill,
    align_pair,
    smooth,
    vote,
)
from brainrec.cells.stage import align_and_link_pairs
from brainrec.detect.classify import classify
from brainrec.registration import PairResult, Transform2D
from brainrec.store import Manifest, StageExistsError, WorkDir, write_atomic
from brainrec.store.cells import load_cell_pair, save_cell_pair, save_profiles
from tests.synthetic.nuclei import CellField, SectionStack, cut_sections
from tests.unit.test_classify import _at, _model, _nuclei

NAMES = tuple(t.name for t in DEFAULT_CELL_TYPES)
PARAMS = AlignParams(grid_um=100.0, window_um=150.0, search_um=100.0, min_peak_ratio=2.0)
EXTENT = 1200.0  # µm, square block
ERROR = Transform2D.similarity(np.radians(0.3), 1.002, 22.0, -15.0)  # leftover registration error


def _cells(seed: int, depth: float = 25.0, density: float = 100.0) -> CellField:
    """Nuclei as spheres at random places (`density` per mm² and µm of depth), radius 2.5 to
    4.5 µm, like the real tissue (about 1100 profiles per mm² in a 5 µm section)."""
    rng = np.random.default_rng(seed)
    n = int(density * (EXTENT / 1000) ** 2 * depth)
    xyz = rng.uniform([0, 0, 0], [EXTENT, EXTENT, depth], (n, 3))
    cls = np.array(["neuron"] * n, dtype=object)
    return CellField(xyz[:, 0], xyz[:, 1], xyz[:, 2], rng.uniform(2.5, 4.5, n), cls)


def _stack(seed: int = 0, n: int = 4) -> SectionStack:
    return cut_sections(_cells(seed), n, 5.0, seed=seed, deform_um=15.0,
                        wavelength_um=(4000.0, 10000.0))  # fmt: skip


def _observed(stack: SectionStack, k: int, seed: int, miss: float = 0.2, noise: float = 0.3):
    """Detected centres of section k: some nuclei missed, centres with detection noise."""
    rng = np.random.default_rng(100 + seed + k)
    s = stack.sections[k]
    keep = rng.random(len(s)) >= miss
    xy = np.c_[s.x_um, s.y_um][keep] + rng.normal(0, noise, (keep.sum(), 2))
    return xy, s.r_um[keep]


def _pair_transform(stack: SectionStack, fixed: int, moving: int) -> Transform2D:
    """The registration result of the pair: the true similarity k -> k-1 with an error."""
    true = stack.transforms[fixed].inverse().matrix3 @ stack.transforms[moving].matrix3
    return Transform2D.from_matrix(ERROR.matrix3 @ true)


def _truth(stack: SectionStack, fixed: int, moving: int, t: Transform2D, g: np.ndarray):
    """The true displacement at points g of the fixed slice: where the tissue at g lies in the
    moving slice, through `t`, subtracted from g."""
    x = stack.transforms[fixed].apply(g)
    for _ in range(30):  # solve t_f^-1(X + d_f(X)) = g for the reference point X
        x = stack.transforms[fixed].apply(g) - stack.deformations[fixed](x)
    q = stack.to_slice(moving, x)
    return g - t.apply(q)


def test_vote_finds_the_shift_among_clutter():
    rng = np.random.default_rng(1)
    fixed = rng.uniform(0, 300, (90, 2))
    shift = np.array([17.0, -8.0])  # on bin edges: only the refinement gets it exactly
    moving = np.vstack([fixed[:30] - shift + rng.normal(0, 0.3, (30, 2)),
                        rng.uniform(-100, 400, (300, 2))])  # fmt: skip
    from scipy.spatial import cKDTree

    v = vote(fixed, moving, cKDTree(moving), np.zeros(2), 100.0)
    assert v is not None and np.allclose(v.shift, shift, atol=0.2) and v.locked(2.0)
    assert v.n_peak >= 25 and v.n_second < 10
    other = rng.uniform(-100, 400, (300, 2))  # no partners at all
    w = vote(fixed, other, cKDTree(other), np.zeros(2), 100.0)
    assert w is not None and not w.locked(2.0)
    assert vote(fixed, other + 1e4, cKDTree(other + 1e4), np.zeros(2), 100.0) is None
    near = vote(fixed, moving, cKDTree(moving), np.array([15.0, -10.0]), 10.0)  # fine search
    assert near is not None and np.allclose(near.shift, shift, atol=0.2)


def test_field_interpolates_bilinearly_and_clamps():
    xs, ys = np.array([0.0, 100.0, 200.0]), np.array([0.0, 100.0])
    gx, gy = np.meshgrid(xs, ys)
    f = Field(xs, ys, 0.01 * gx + 2.0, -0.02 * gy, np.ones_like(gx))
    d = f.displacement(np.array([[50.0, 25.0], [150.0, 75.0], [-50.0, 500.0]]))
    assert np.allclose(d, [[2.5, -0.5], [3.5, -1.5], [2.0, -2.0]])
    one = Field(np.array([5.0]), np.array([7.0]), np.array([[1.5]]), np.array([[-2.0]]),
                np.ones((1, 1)))  # fmt: skip
    assert np.allclose(one.displacement(np.zeros((3, 2))), [[1.5, -2.0]] * 3)


def test_outliers_are_dropped_and_gaps_filled():
    shift = np.zeros((5, 5, 2))
    shift[..., 0] = 3.0
    shift[2, 2] = [40.0, 0.0]  # one wrong node among consistent ones
    locked = np.ones((5, 5), bool)
    kept = _drop_outliers(shift, locked, 2.0)
    assert not kept[2, 2] and kept.sum() == 24
    two = np.zeros((5, 5), bool)
    two[0, :2] = True  # two locked nodes 9 µm apart: one neighbour each is too few to judge
    apart = np.zeros((5, 5, 2))
    apart[0, 1] = [9.0, 0.0]
    assert _drop_outliers(apart, two, 2.0)[0, :2].all()
    filled = _fill(shift, kept, np.full((5, 5, 2), -9.0))
    assert np.allclose(filled[..., 0], 3.0) and np.allclose(filled[..., 1], 0.0)
    none = _fill(shift, np.zeros((5, 5), bool), np.full((5, 5, 2), -9.0))
    assert (none == -9.0).all()
    field, kept = smooth(shift, locked, 2.0)  # both steps, as each stage runs them
    assert not kept[2, 2] and np.allclose(field[2, 2], [3.0, 0.0])
    assert np.allclose(smooth(shift, np.zeros((5, 5), bool), 2.0)[0], 0.0)


@pytest.mark.parametrize(("seed", "miss", "min_recurrence"), [(0, 0.2, 0.3), (1, 0.5, 0.15)])
def test_known_deformation_is_recovered_within_1_um_at_90_percent(seed, miss, min_recurrence):
    """Acceptance of M7-12 (synthetic): the field matches the true local displacement within
    1 µm at 90% of the supported grid nodes; also with half of the nuclei missed, which brings
    the recurrence down to the real 15 to 30%."""
    stack = _stack(seed)
    t = _pair_transform(stack, 1, 2)
    fixed, _ = _observed(stack, 1, seed, miss)
    moving, _ = _observed(stack, 2, seed, miss)
    a = align_pair(fixed, t.apply(moving), PARAMS)
    assert a.status == "aligned" and a.n_locked >= 0.8 * a.n_supported
    gx, gy = np.meshgrid(a.field.grid_x_um, a.field.grid_y_um)
    g = np.c_[gx.ravel(), gy.ravel()]
    from scipy.spatial import cKDTree

    supported = np.array([len(x) >= MIN_NUCLEI for x in cKDTree(fixed).query_ball_point(g, 150.0)])
    err = np.hypot(
        *(np.c_[a.field.dx_um.ravel(), a.field.dy_um.ravel()] - _truth(stack, 1, 2, t, g)).T
    )
    assert supported.sum() == a.n_supported > 50
    assert np.mean(err[supported] <= 1.0) >= 0.90, np.percentile(err[supported], [50, 90, 99])
    truth = _truth(stack, 1, 2, t, g[supported])
    assert np.median(np.hypot(*truth.T)) > 5  # the pair transform alone is several µm off
    assert a.recurrence > min_recurrence and a.recurrence_chance < 0.05
    conf = a.field.node_conf.ravel()
    assert conf.max() <= 1 and (conf[~supported] == 0).all()


def test_sections_apart_share_no_nuclei():
    """Sections 15 µm apart (two missing in between) give no recurrence and a zero field."""
    stack = _stack(2)
    t = _pair_transform(stack, 0, 3)
    a = align_pair(_observed(stack, 0, 2)[0], t.apply(_observed(stack, 3, 2)[0]), PARAMS)
    assert a.status == "no_recurrence" and a.n_locked == 0 and a.coarse_share_locked < 0.1
    assert (a.field.dx_um == 0).all() and (a.field.node_conf == 0).all()
    assert a.recurrence < 0.05 and a.n_supported > 50
    empty = align_pair(np.zeros((0, 2)), np.zeros((0, 2)), PARAMS)
    assert empty.status == "no_recurrence" and empty.n_supported == 0


def test_pair_file_round_trip_and_checks(tmp_path):
    work = WorkDir(tmp_path)
    f = Field(np.arange(3.0), np.arange(2.0), np.ones((2, 3)), np.zeros((2, 3)), np.ones((2, 3)))
    arrays = {**f.arrays(), "links": np.zeros((0, 2), np.int32), "link_dist_um": np.zeros(0)}
    save_cell_pair(work, 4, arrays, {"fixed": 3, "status": "aligned"})
    back, meta = load_cell_pair(work, 4)
    assert meta == {"schema": "brainrec.cellpair/1", "moving": 4, "fixed": 3, "status": "aligned"}
    assert np.array_equal(Field.from_arrays(back).dx_um, f.dx_um)
    with pytest.raises(ValueError, match="dx_um has shape"):
        save_cell_pair(work, 4, {**arrays, "dx_um": np.ones((3, 3))}, {"fixed": 3})
    with pytest.raises(ValueError, match="fixed"):
        save_cell_pair(work, 4, arrays, {})
    with pytest.raises(ValueError, match="miss"):
        save_cell_pair(work, 4, {k: v for k, v in arrays.items() if k != "links"}, {"fixed": 3})
    with pytest.raises(FileNotFoundError, match="3<-4|run cells"):
        load_cell_pair(work, 5)


def _profiles_of(xy: np.ndarray, r: np.ndarray):
    """Profiles at `xy` with radius `r` and round outlines of 16 vertices (C13 units)."""
    p = classify(_nuclei(np.array([_at(0)] * len(xy)).reshape(-1, 9)), _model(), NAMES)
    p.x_um[:], p.y_um[:], p.r_um[:] = xy[:, 0], xy[:, 1], r
    a = np.linspace(0, 2 * np.pi, 16, endpoint=False)
    ring = np.c_[np.cos(a), np.sin(a)]
    p.outline_offsets = np.arange(len(xy) + 1, dtype=np.int64) * 16
    p.outline_xy = np.round(np.repeat(np.asarray(r, float), 16)[:, None] * np.tile(ring, (len(xy), 1))
                            / 0.1).astype(np.int16).reshape(-1, 2)  # fmt: skip
    return p


def _meta() -> dict:
    return {"source": "x.ndpi", "detector": "test", "detector_version": "1", "downsample": 2,
            "mpp_um": [0.25, 0.25], "classifier": "rules-v1", "class_names": list(NAMES)}  # fmt: skip


def make_align_setup(tmp_path):
    """Three consecutive synthetic sections with cell files and registration results."""
    stack = _stack(3, n=3)
    data = tmp_path / "data"
    data.mkdir()
    cfg = ProjectConfig(data_dir=data, work_dir=tmp_path / "work", z_spacing_um=5.0)
    work = WorkDir(cfg.work_dir)
    for k in (1, 2, 3):
        (data / f"s_{k}_H.ndpi").write_bytes(b"x")
        xy, r = _observed(stack, k - 1, 3)
        save_profiles(work, k, _profiles_of(xy, r), _meta())
    for k in (2, 3):
        t = _pair_transform(stack, k - 2, k - 1)
        pr = PairResult(k - 1, k, t, "features", 100, 80, 10.0, 0.95)
        write_atomic(work.pair_transform(k), json.dumps(pr.to_dict()).encode())
    return cfg, work, Catalog.scan(data), Manifest(work.manifest), stack


def test_align_and_link_pairs_writes_fields_and_resumes(tmp_path):
    cfg, work, catalog, manifest, _ = make_align_setup(tmp_path)
    assert align_and_link_pairs(catalog, work, cfg, manifest).done == [2, 3]
    _, meta = load_cell_pair(work, 3)
    assert meta["fixed"] == 2 and meta["status"] == "aligned" and meta["method"] == "nuclei-votes/1"
    assert meta["recurrence"] > 0.3 and meta["share_locked"] > 0.8
    state = manifest.get("cells")["pairs"]
    assert state["signature"]["align_window_um"] == 150.0 and set(state["pairs"]) == {"2", "3"}
    assert align_and_link_pairs(catalog, work, cfg, manifest).skipped == [2, 3]
    sparse = np.c_[np.arange(30) * 37.0, np.arange(30) * 23.0]  # detection redone: other cells
    save_profiles(work, 3, _profiles_of(sparse, np.full(30, 3.0)), _meta())
    again = align_and_link_pairs(catalog, work, cfg, manifest)
    assert again.done == [3] and again.skipped == [2]  # only the pair with the new cell file
    assert load_cell_pair(work, 3)[1]["status"] == "no_recurrence"
    other = cfg.model_copy(update={"align_window_um": 200.0})
    with pytest.raises(StageExistsError, match="align_window_um"):
        align_and_link_pairs(catalog, work, other, manifest)
    assert align_and_link_pairs(catalog, work, other, manifest, overwrite=True).done == [2, 3]
    work.pair_transform(2).unlink()
    with pytest.raises(FileNotFoundError, match="1<-2"):
        align_and_link_pairs(catalog, work, other, manifest)


def test_config_rejects_impossible_alignment_settings():
    with pytest.raises(ValueError):
        ProjectConfig(data_dir=".", work_dir=".", z_spacing_um=5.0, align_min_peak_ratio=1.0)
    with pytest.raises(ValueError):
        ProjectConfig(data_dir=".", work_dir=".", z_spacing_um=5.0, align_grid_um=0.0)
