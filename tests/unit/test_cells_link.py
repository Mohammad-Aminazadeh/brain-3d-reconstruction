"""M7-13: linking profiles into 3D cells (C15, C16), and acceptance AC13 on synthetic data."""

import json

import numpy as np
import pytest
import zarr

from brainrec.catalog import DEFAULT_CELL_TYPES, Catalog, ProjectConfig
from brainrec.cells.align import align_pair
from brainrec.cells.layers import read_layer
from brainrec.cells.link import (
    Links,
    LinkSide,
    build_cells,
    build_chains,
    link_profiles,
    place_cells,
    sphere_radius,
)
from brainrec.cells.place import VolumeFrame, place_profiles
from brainrec.cells.stage import run_cells
from brainrec.registration import PairResult, Transform2D
from brainrec.store import Manifest, StageExistsError, WorkDir, write_atomic
from brainrec.store.cells import save_profiles
from tests.synthetic.nuclei import SectionStack, cut_sections
from tests.unit.test_cells_align import PARAMS, _cells, _meta, _pair_transform, _profiles_of
from tests.unit.test_cells_stage import add_canvas, add_masks

NAMES = tuple(t.name for t in DEFAULT_CELL_TYPES)


def _side(xy, r=3.0, cls=2, conf=0.9) -> LinkSide:
    xy = np.asarray(xy, float).reshape(-1, 2)
    n = len(xy)
    return LinkSide(xy, np.broadcast_to(np.asarray(r, float), n).copy(),
                    np.broadcast_to(np.asarray(cls), n).astype(np.uint8),
                    np.broadcast_to(np.asarray(conf, float), n).copy())  # fmt: skip


def test_link_rule_tolerance_one_to_one_and_closest_first():
    fixed = _side([[0, 0], [10, 0], [20, 0], [30, 0]], r=[2.0, 6.0, 2.0, 2.0])
    moving = _side([[1.4, 0], [12.5, 0], [21.6, 0], [30.9, 0], [30.3, 0]], r=[2.0, 6.0, 2.0, 2, 2])
    lk = link_profiles(fixed, moving, 1.5)
    got = sorted(zip(lk.moving.tolist(), lk.fixed.tolist(), strict=True))
    # 1.4 µm < 1.5: linked; 2.5 µm < 0.5 x 6: linked; 1.6 µm > 1.5 (radius 2): not linked;
    # two candidates for the last fixed one: the closer (0.3 µm, listed second) wins
    assert got == [(0, 0), (1, 1), (4, 3)]
    assert np.allclose(sorted(lk.dist_um), [0.3, 1.4, 2.5], atol=1e-5)
    assert len(link_profiles(_side(np.zeros((0, 2))), moving, 1.5)) == 0


def test_link_rule_keeps_neurons_and_oligodendrocytes_apart_unless_doubtful():
    def linked(c1, c2, conf1, conf2):
        return len(link_profiles(_side([[0, 0]], cls=c1, conf=conf1),
                                 _side([[0.5, 0]], cls=c2, conf=conf2), 1.5))  # fmt: skip

    assert linked(1, 3, 0.9, 0.9) == 0 and linked(3, 1, 0.9, 0.4) == 0
    assert linked(1, 3, 0.4, 0.45) == 1  # both doubtful
    assert linked(1, 2, 0.9, 0.9) == 1 and linked(3, 3, 0.9, 0.9) == 1


def _links(pairs, dist=None):
    a = np.array(pairs, np.int32).reshape(-1, 2)
    d = np.ones(len(a), np.float32) if dist is None else np.asarray(dist, np.float32)
    return Links(a[:, 0], a[:, 1], d)


def test_chains_follow_the_links_through_the_range():
    # slices of 3, 2, 3 profiles; global numbers 0-2 | 3-4 | 5-7
    label, n_split = build_chains([3, 2, 3], [_links([(0, 2), (1, 0)]), _links([(1, 0)])], 8)
    assert label.tolist() == [0, 1, 2, 2, 0, 5, 2, 7] and n_split == 0  # 2 -> 3 -> 6
    with pytest.raises(ValueError, match="link sets"):
        build_chains([3, 2], [], 8)
    with pytest.raises(ValueError, match="point past"):
        build_chains([3, 2], [_links([(2, 0)])], 8)


def test_long_chains_are_split_at_their_weakest_link():
    n = 10  # one profile per slice, all linked: a chain over 10 sections
    dist = [0.2, 0.3, 0.9, 0.1, 0.2, 1.4, 0.3, 0.2, 0.4]  # links of slices 1..9
    links = [_links([(0, 0)], [d]) for d in dist]
    label, n_split = build_chains([1] * n, links, 4)
    # 1.4 (before slice 6) splits 10 into 6 + 4; 0.9 (before slice 3) splits the 6 into 3 + 3
    assert label.tolist() == [0, 0, 0, 3, 3, 3, 6, 6, 6, 6] and n_split == 2
    assert build_chains([1] * n, links, 10)[1] == 0


def test_cells_take_the_largest_profile_and_the_summed_class():
    label = np.array([0, 0, 0, 3])
    r = np.array([2.0, 4.0, 3.0, 5.0])
    prob = np.array([[0.1, 0.9], [0.6, 0.4], [0.1, 0.9], [0.7, 0.3]])  # the largest says class 1
    seen = np.array([0, 0, 1, 0])
    cells = build_cells(label, r, prob, seen, np.array([0, 1, 2, 0]), None)
    assert cells.rep.tolist() == [1, 3] and cells.n_profiles.tolist() == [3, 1]
    assert cells.class_id.tolist() == [2, 1]  # summed: 0.8 vs 2.2 for chain 0
    assert np.allclose(cells.confidence, [2.2 / 3, 0.7]) and cells.nucleolus.tolist() == [1, 0]
    assert np.isnan(cells.radius_3d_um).all()


@pytest.mark.parametrize(
    ("radius", "z0"), [(4.0, 7.3), (3.2, 4.5), (5.0, 0.4), (3.2, 6.05), (4.5, 8.9)]
)
def test_sphere_radius_from_the_chain(radius, z0):
    t = 3.0
    sections = np.arange(6)
    lo, hi = sections * t, (sections + 1) * t
    d = np.where(z0 < lo, lo - z0, np.where(z0 > hi, z0 - hi, 0.0))
    keep = d < radius - 0.5
    rho = np.sqrt(radius**2 - d[keep] ** 2)  # largest cut within each section (C13)
    s = sections[keep]
    groups = np.zeros(len(s), int)
    rep = np.full(1, s[np.argmax(rho)])
    got = sphere_radius(rho, s, rep, groups, 1, t)
    assert got[0] == pytest.approx(radius, rel=0.01)  # exact geometry: within 1%
    cells = build_cells(np.zeros(len(s), int), rho, np.ones((len(s), 1)), np.zeros(len(s)), s, t)
    assert cells.radius_3d_um[0] == pytest.approx(radius, rel=0.01)


def test_cells_are_placed_at_their_representative():
    p = _profiles_of(np.array([[0.0, 0.0], [10.0, 5.0], [20.0, 5.0]]), np.array([2.0, 3.0, 4.0]))
    frame = VolumeFrame((7,), (0.0, 0.0), 5.0, {7: Transform2D.identity()})
    placed = place_profiles(p, 7, frame, np.ones((6, 3)))
    cells = build_cells(np.array([0, 0, 2]), p.r_um, p.class_prob, p.nucleolus_seen,
                        np.zeros(3, int), None)  # fmt: skip
    props = np.ones((6, 3))
    props[cells.class_id[0] - 1] = (2.0, 1.0, 1.0)
    out = place_cells(placed, cells, props)
    assert out.cell_id.tolist() == [(7 << 32) + 1, (7 << 32) + 2]  # the representatives' ids
    assert np.allclose(out.centre[0], [10.0, 5.0, 2.5]) and out.n_profiles.tolist() == [2, 1]
    assert np.allclose(out.radii[0], [6.0, 3.0, 3.0])


# --- AC13 -------------------------------------------------------------------------------------

THICK = 3.0  # µm: nuclei of 2.5 to 4.5 µm radius are cut by 2 to 4 sections


def _ac13_stack(seed: int) -> SectionStack:
    return cut_sections(_cells(seed, depth=18.0), 6, THICK, seed=seed, deform_um=15.0,
                        wavelength_um=(4000.0, 10000.0))  # fmt: skip


def _detected(stack: SectionStack, k: int, seed: int):
    s = stack.sections[k]
    noise = np.random.default_rng(seed * 10 + k).normal(0, 0.3, (len(s), 2))
    return np.c_[s.x_um, s.y_um] + noise, s.r_um, s.cell_id


@pytest.mark.parametrize("seed", [0, 1])
def test_ac13_spheres_cut_by_several_sections_give_one_cell_each(seed):
    """AC13 (synthetic): at least 95% of the true links found, at most 2% false merges; each
    cell's centre within 1 µm of its true position in the representative's section, radius
    within 10% of its largest profile."""
    stack = _ac13_stack(seed)
    det = [_detected(stack, k, seed) for k in range(6)]
    links = []
    for k in range(1, 6):
        t = _pair_transform(stack, k - 1, k)
        fxy, moving = det[k - 1][0], t.apply(det[k][0])
        a = align_pair(fxy, moving, PARAMS)
        assert a.status == "aligned"
        moved = moving + a.field.displacement(moving)
        links.append(link_profiles(_side(fxy, det[k - 1][1]), _side(moved, det[k][1]), 1.5))
    true_links = false_links = found_true = 0
    for k, lk in enumerate(links, start=1):
        ids_k, ids_prev = det[k][2], det[k - 1][2]
        same = ids_k[lk.moving] == ids_prev[lk.fixed]
        found_true += int(same.sum())
        false_links += int((~same).sum())
        true_links += len(np.intersect1d(ids_k, ids_prev))
    assert found_true / true_links >= 0.95, (found_true, true_links)
    assert false_links / (found_true + false_links) <= 0.02, false_links

    sizes = [len(d[0]) for d in det]
    label, _ = build_chains(sizes, links, 8)
    r = np.concatenate([d[1] for d in det])
    truth = np.concatenate([d[2] for d in det])
    section = np.concatenate([np.full(n, k) for k, n in enumerate(sizes)])
    cells = build_cells(label, r, np.ones((len(r), 1)), np.zeros(len(r)), section, THICK)
    # one 3D cell each: the chain of a true cell holds exactly its profiles
    whole = [len(np.unique(label[truth == c])) == 1 and (truth[label == label[truth == c][0]]
             == c).all() for c in np.unique(truth)]  # fmt: skip
    assert np.mean(whole) >= 0.95
    xy = np.concatenate([d[0] for d in det])
    rep = cells.rep
    field = _cells(seed, depth=18.0)
    true_xy = np.zeros((len(rep), 2))
    for k in range(6):  # the true centre as it lies in the representative's section
        sel = section[rep] == k
        ref = np.c_[field.x_um[truth[rep][sel]], field.y_um[truth[rep][sel]]]
        true_xy[sel] = stack.to_slice(k, ref)
    assert np.percentile(np.hypot(*(xy[rep] - true_xy).T), 99) <= 1.0
    largest = np.zeros(truth.max() + 1)
    np.maximum.at(largest, truth, r)
    built = np.isin(truth[rep], np.unique(truth)[np.array(whole)])  # cells rebuilt whole
    assert np.all(np.abs(r[rep][built] / largest[truth[rep][built]] - 1) <= 0.10)
    multi = cells.n_profiles >= 2
    assert multi.mean() > 0.5  # most nuclei span several sections
    true_r = field.r_um[truth[rep]]
    assert np.median(np.abs(cells.radius_3d_um[multi] / true_r[multi] - 1)) <= 0.10


def test_sections_without_recurrence_leave_every_profile_its_own_cell():
    stack = _ac13_stack(2)
    a, b = _detected(stack, 0, 2), _detected(stack, 5, 2)  # 15 µm apart
    t = _pair_transform(stack, 0, 5)
    aligned = align_pair(a[0], t.apply(b[0]), PARAMS)
    assert aligned.status == "no_recurrence"
    label, _ = build_chains([len(a[0]), len(b[0])], [Links.empty()], 8)
    assert len(np.unique(label)) == len(a[0]) + len(b[0])


# --- the stage --------------------------------------------------------------------------------


def make_link_setup(tmp_path):
    """Four consecutive synthetic sections, registered, with cell files and a volume frame."""
    stack = _ac13_stack(3)
    data = tmp_path / "data"
    data.mkdir()
    cfg = ProjectConfig(data_dir=data, work_dir=tmp_path / "work", z_spacing_um=5.0)
    work = WorkDir(cfg.work_dir)
    chained = {1: Transform2D.identity()}
    for k in (1, 2, 3, 4):
        (data / f"s_{k}_H.ndpi").write_bytes(b"x")
        xy, r, _ = _detected(stack, k - 1, 3)
        save_profiles(work, k, _profiles_of(xy, r), _meta())
        if k > 1:
            t = _pair_transform(stack, k - 2, k - 1)
            pr = PairResult(k - 1, k, t, "features", 100, 80, 10.0, 0.95)
            write_atomic(work.pair_transform(k), json.dumps(pr.to_dict()).encode())
            chained[k] = chained[k - 1].compose(t)
        chained[k].save(work.chained_transform(k))
    g = zarr.open_group(str(work.volume), mode="w")
    add_canvas(g, 4, 2300.0)
    add_masks(work, (1, 2, 3, 4), 1300.0, 0.25)
    g.attrs["ome"] = {"multiscales": [{"datasets": [{"path": "0", "coordinateTransformations": [
        {"type": "scale", "scale": [1.0, 5.0, 0.88, 0.88]}]}]}]}  # fmt: skip
    g.attrs["brainrec"] = {"mode": "registered", "slice_ids": [1, 2, 3, 4],
                           "canvas_origin_um": [-500.0, -500.0],
                           "slice_to_reference_um": {str(k): chained[k].matrix.tolist()
                                                     for k in chained}}  # fmt: skip
    return cfg, work, Catalog.scan(data), Manifest(work.manifest)


def test_stage_places_one_blob_per_chain(tmp_path):
    cfg, work, catalog, manifest = make_link_setup(tmp_path)
    assert run_cells(catalog, work, cfg, manifest).done == [1, 2, 3, 4]
    entry = manifest.get("cells")["layers"]
    assert entry["n_cells"] < 0.7 * entry["n_profiles"]  # most nuclei are linked
    assert sum(entry["counts"].values()) == entry["n_cells"] and entry["n_split_chains"] == 0
    assert sum(int(n) * c for n, c in entry["chain_lengths"].items()) == entry["n_profiles"]
    pairs = manifest.get("cells")["pairs"]["pairs"]
    for k in ("2", "3", "4"):
        s = pairs[k]["summary"]
        assert s["share_linked"] > 0.5 and s["share_linked_chance"] < 0.02
        assert 0 < s["median_link_um"] < 1.0
    name = max(entry["counts"], key=entry["counts"].get)  # the synthetic nuclei share a class
    _, levels = read_layer(work.cell_layer(name))
    recs = np.concatenate([lv[1] for lv in levels])
    assert len(recs) == entry["counts"][name] and recs["n_profiles"].max() >= 3
    assert run_cells(catalog, work, cfg, manifest).skipped == [1, 2, 3, 4]
    shorter = cfg.model_copy(update={"link_max_sections": 2})
    with pytest.raises(StageExistsError, match="link_max_sections"):
        run_cells(catalog, work, shorter, manifest)
    run_cells(catalog, work, shorter, manifest, overwrite=True)
    assert manifest.get("cells")["layers"]["n_split_chains"] > 0
    assert max(map(int, manifest.get("cells")["layers"]["chain_lengths"])) <= 2
    looser = cfg.model_copy(update={"link_tolerance_um": 2.0})
    with pytest.raises(StageExistsError, match="link_tolerance_um"):
        run_cells(catalog, work, looser, manifest)


def test_config_rejects_impossible_link_settings():
    with pytest.raises(ValueError):
        ProjectConfig(data_dir=".", work_dir=".", z_spacing_um=5.0, link_max_sections=0)
    with pytest.raises(ValueError):
        ProjectConfig(data_dir=".", work_dir=".", z_spacing_um=5.0, section_thickness_um=0.0)


def test_pairs_without_recurrence_get_no_links(tmp_path):
    """15 nuclei far apart, the same in both slices: too few to align (no recurrence), so no
    links, although the link rule alone would link all of them."""
    from brainrec.cells.stage import align_and_link_pairs
    from brainrec.store.cells import load_cell_pair

    data = tmp_path / "data"
    data.mkdir()
    cfg = ProjectConfig(data_dir=data, work_dir=tmp_path / "work", z_spacing_um=5.0)
    work = WorkDir(cfg.work_dir)
    xy = np.c_[np.arange(15) * 400.0, np.arange(15) * 300.0]
    for k in (1, 2):
        (data / f"s_{k}_H.ndpi").write_bytes(b"x")
        save_profiles(work, k, _profiles_of(xy, np.full(15, 3.0)), _meta())
    pr = PairResult(1, 2, Transform2D.identity(), "features", 100, 80, 10.0, 0.95)
    write_atomic(work.pair_transform(2), json.dumps(pr.to_dict()).encode())
    align_and_link_pairs(Catalog.scan(data), work, cfg, Manifest(work.manifest))
    arrays, meta = load_cell_pair(work, 2)
    assert meta["status"] == "no_recurrence" and meta["n_links"] == 0 and len(arrays["links"]) == 0
