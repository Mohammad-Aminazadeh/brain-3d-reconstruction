import numpy as np
import pytest
from skimage import measure

from brainrec.detect.labels import CLASSES
from tests.synthetic.nuclei import (
    LOOKS,
    NEUROPIL,
    cut_sections,
    make_cell_field,
    make_nuclei,
)


@pytest.fixture(scope="module")
def syn():
    return make_nuclei(seed=3)


def _px(syn, i):
    t = syn.truth
    return round(t.x_um[i] / syn.um_per_px - 0.5), round(t.y_um[i] / syn.um_per_px - 0.5)


def test_every_class_with_its_look(syn):
    t = syn.truth
    for cls in CLASSES:
        sel = (t.cls == cls) & (t.pair == -1)
        assert sel.sum() == 8
        lo, hi = LOOKS[cls]["r"]
        assert ((t.r_um[sel] >= lo) & (t.r_um[sel] <= hi)).all()
    elongated = t.ratio[t.cls == "Microglia-like"]
    assert (elongated >= 2.2).all() and (t.ratio[t.cls == "Neuron"] <= 1.15).all()
    assert list(t.nucleolus) == [cls == "Neuron" for cls in t.cls]


def test_deterministic_for_the_seed(syn):
    again = make_nuclei(seed=3)
    assert np.array_equal(again.rgb, syn.rgb) and np.allclose(again.truth.x_um, syn.truth.x_um)
    assert not np.allclose(make_nuclei(seed=4).truth.x_um, syn.truth.x_um)


def test_nuclei_lie_in_the_tissue_and_are_darker_than_neuropil(syn):
    neuropil = NEUROPIL.mean()
    for i in range(len(syn.truth)):
        x, y = _px(syn, i)
        assert syn.mask[y, x] == 255
        assert syn.rgb[y, x].mean() < neuropil - 15  # nucleus (or nucleolus) at the centre


def test_radius_is_the_equivalent_area_radius_of_the_drawn_nucleus(syn):
    t = syn.truth
    gray = syn.rgb.mean(axis=2)
    for i in np.nonzero(
        (t.pair == -1) & np.isin(t.cls, ["Oligodendrocyte-like", "Microglia-like"])
    )[0]:
        x, y = _px(syn, i)
        r = int(3 * t.r_um[i] / syn.um_per_px)
        win = (
            gray[y - r : y + r + 1, x - r : x + r + 1] < (NEUROPIL.mean() + 110) / 2
        )  # dark nucleus
        lab = measure.label(win)
        area = (lab == lab[r, r]).sum() * syn.um_per_px**2
        assert np.sqrt(area / np.pi) == pytest.approx(t.r_um[i], rel=0.1)


def test_touching_pairs_touch(syn):
    t = syn.truth
    for p in range(3):
        a, b = np.nonzero(t.pair == p)[0]
        d = np.hypot(t.x_um[a] - t.x_um[b], t.y_um[a] - t.y_um[b])
        assert d == pytest.approx(t.r_um[a] + t.r_um[b], abs=0.05)


def test_some_nuclei_cross_tile_borders_and_are_listed_once(syn):
    t = syn.truth
    assert t.on_border.sum() >= 4
    lines = np.arange(syn.tile_px, syn.rgb.shape[1], syn.tile_px) - 0.5
    for i in np.nonzero(t.on_border)[0]:
        cx = t.x_um[i] / syn.um_per_px - 0.5
        assert np.min(np.abs(cx - lines)) < t.r_um[i] * np.sqrt(t.ratio[i]) / syn.um_per_px
    centres = np.round(np.stack([t.x_um, t.y_um], 1), 3)
    assert len(np.unique(centres, axis=0)) == len(t)  # no nucleus listed twice


def test_debris_is_on_the_glass_only(syn):
    assert syn.debris.any() and not (syn.debris & syn.mask).any()
    assert syn.rgb[syn.debris > 0].mean() < 120  # dark enough to tempt a detector


@pytest.fixture(scope="module")
def stack():
    cells = make_cell_field(n_cells=200, seed=1)
    return cells, cut_sections(cells, n_sections=8, thickness_um=5.0, seed=2)


def test_profile_radius_follows_the_cut_height(stack):
    cells, st = stack
    for k, prof in enumerate(st.sections):
        lo, hi = k * 5.0, (k + 1) * 5.0
        z, r = cells.z_um[prof.cell_id], cells.r_um[prof.cell_id]
        dz = np.where((z >= lo) & (z <= hi), 0.0, np.minimum(abs(z - lo), abs(z - hi)))
        assert np.allclose(prof.r_um * st.transforms[k].scale, np.sqrt(r**2 - dz**2))
        assert (prof.r_um * st.transforms[k].scale <= r + 1e-9).all()
        assert (prof.r_um * st.transforms[k].scale >= 1.6 - 1e-9).all()
        assert len(set(prof.cell_id.tolist())) == len(prof)  # one profile per cell and section


def test_a_cell_appears_in_consecutive_sections(stack):
    _cells, st = stack
    seen: dict[int, list[int]] = {}
    for k, prof in enumerate(st.sections):
        for c in prof.cell_id:
            seen.setdefault(int(c), []).append(k)
    spans = list(seen.values())
    assert all(s == list(range(s[0], s[-1] + 1)) for s in spans)  # no gaps
    assert max(len(s) for s in spans) >= 2  # cells larger than a section span several
    assert np.mean([len(s) for s in spans]) > 1.2


def test_slice_positions_map_back_to_the_deformed_reference(stack):
    cells, st = stack
    for k, prof in enumerate(st.sections):
        ref = np.stack([cells.x_um[prof.cell_id], cells.y_um[prof.cell_id]], 1)
        q = np.stack([prof.x_um, prof.y_um], 1)
        assert np.allclose(st.transforms[k].apply(q), ref + st.deformations[k](ref))
        assert np.allclose(st.to_slice(k, ref), q)
        assert np.abs(st.deformations[k](ref)).max() <= 10.0 + 1e-9


def test_sections_are_moved_differently(stack):
    _, st = stack
    angles = [t.angle_deg for t in st.transforms]
    assert len(set(np.round(angles, 6))) == len(angles) and max(map(abs, angles)) <= 5.0
