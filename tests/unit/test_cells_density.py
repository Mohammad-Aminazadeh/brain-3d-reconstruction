"""M7-15: density maps per class (C18) on synthetic profiles, masks and sections."""

import numpy as np
import pytest
import zarr

from brainrec.cells.density import (
    UNIT_AREA,
    UNIT_VOLUME,
    DensityMeta,
    abercrombie,
    section_density,
    tissue_fraction,
    write_density,
)
from brainrec.cells.stage import run_cells
from brainrec.registration import Transform2D
from brainrec.store import StageExistsError
from tests.synthetic.nuclei import cut_sections
from tests.unit.test_cells_align import _cells
from tests.unit.test_cells_link import make_link_setup

G = 50.0  # grid µm


def _square_mask(n: int = 100, a: int = 20, b: int = 80) -> np.ndarray:
    m = np.zeros((n, n), np.uint8)
    m[a:b, a:b] = 255
    return m


def test_tissue_fraction_keeps_the_area_under_any_transform():
    mask = _square_mask()  # 10 µm pixels: tissue 200..800 µm, 0.36 mm²
    frac = tissue_fraction(mask, 10.0, Transform2D.identity(), (20, 20), G)
    # mask pixels 20..79 cover [200, 800) µm (C1): squares 4..15 are tissue, exactly
    assert np.allclose(frac[4:16, 4:16], 1.0, atol=1e-6)
    assert frac[:4].max() <= 1e-6 and frac[16:].max() <= 1e-6 and frac[:, 3].max() <= 1e-6
    assert frac.sum() * G**2 == pytest.approx(600.0**2, rel=0.01)
    moved = Transform2D.similarity(np.radians(30), 1.0, 500.0, 70.0)  # stays inside the grid
    frac2 = tissue_fraction(mask, 10.0, moved, (30, 30), G)
    assert frac2.sum() * G**2 == pytest.approx(600.0**2, rel=0.02)
    centre = moved.apply(np.array([[500.0, 500.0]]))[0]  # tissue centre lands here
    i, j = (centre // G).astype(int)
    assert frac2[j, i] == pytest.approx(1.0)


def _uniform(n_per_mm2: float, lo: float, hi: float, seed: int, cls: int = 1):
    rng = np.random.default_rng(seed)
    n = rng.poisson(n_per_mm2 * ((hi - lo) / 1000) ** 2)
    return rng.uniform(lo, hi, (n, 2)), np.full(n, cls)


def test_a_known_density_is_recovered_within_5_percent():
    """Acceptance (synthetic): uniform profiles at a known density inside the tissue."""
    tissue = tissue_fraction(_square_mask(200, 20, 180), 10.0, Transform2D.identity(), (40, 40), G)
    xy1, c1 = _uniform(3000.0, 200.0, 1800.0, 1, cls=1)
    xy2, c2 = _uniform(800.0, 200.0, 1800.0, 2, cls=2)
    d = section_density(np.vstack([xy1, xy2]), np.r_[c1, c2], 3, tissue, G)
    inside = tissue >= 1.0
    for c, truth in ((0, 3000.0), (1, 800.0)):
        assert d[c][inside].mean() == pytest.approx(truth, rel=0.05)
    assert np.mean(np.abs(d[0][inside] / 3000.0 - 1) <= 0.25) >= 0.9  # square by square
    edge = inside & (tissue_fraction(_square_mask(200, 30, 170), 10.0, Transform2D.identity(),
                                     (40, 40), G) == 0)  # fmt: skip
    assert d[0][edge].mean() == pytest.approx(3000.0, rel=0.10)  # not diluted at the edge
    assert d[:, tissue == 0].max() == 0 and d[2].max() == 0


def test_a_density_gradient_is_followed():
    rng = np.random.default_rng(3)
    xy = rng.uniform(0, 2000, (60000, 2))
    keep = rng.random(len(xy)) < xy[:, 0] / 2000  # density rising linearly in x
    tissue = np.ones((40, 40))
    d = section_density(xy[keep], np.ones(keep.sum(), int), 1, tissue, G)[0]
    expect = 15000.0 * (np.arange(40) + 0.5) / 40  # 60,000 over 4 mm² times x/2000
    cols = d[5:35].mean(axis=0)
    assert np.allclose(cols[3:37], expect[3:37], rtol=0.08)


def test_abercrombie_turns_profiles_per_area_into_cells_per_volume():
    per_mm2 = np.array([[[1000.0]], [[600.0]]])
    out = abercrombie(per_mm2, 5.0, np.array([7.0, 3.0]))
    assert np.allclose(out[:, 0, 0], [1000 / 0.012, 600 / 0.008])
    # spheres at a known density, cut into 5 µm sections: profiles of the middle section per mm²
    field = _cells(4, depth=30.0, density=100.0)  # 100 per mm² and µm = 100,000 per mm³
    stack = cut_sections(field, 6, 5.0, seed=4, min_profile_r_um=0.01)
    ids = stack.sections[3].cell_id  # slab 15..20 µm: far from the block's top and bottom
    x, y = field.x_um[ids], field.y_um[ids]
    inner = (x > 100) & (x < 1100) & (y > 100) & (y < 1100)
    n_a = inner.sum() / 1.0  # 1 mm²
    n_v = abercrombie(np.array([n_a]), 5.0, np.array([2 * field.r_um.mean()]))[0]
    true_v = len(field) / (1.2 * 1.2 * 0.030)
    assert n_v == pytest.approx(true_v, rel=0.05)


def test_density_file_metadata_and_replacement(tmp_path):
    out = tmp_path / "density.zarr"
    values = np.zeros((2, 3, 4, 5), np.float32)
    values[0] = 100.0
    values[0, 1, 2, 3] = 1500.0  # one outlier among 60 squares: above the 99th percentile
    meta = DensityMeta(UNIT_AREA, ["A", "B"], [4, 5, 6], 50.0, 5.0, None, None)
    write_density(out, values, meta)
    g = zarr.open_group(str(out), mode="r")
    assert np.array_equal(g["0"][:], values)
    assert g["0"].chunks[0] == 2  # all classes in every chunk, or Neuroglancer shows nothing
    ds = g.attrs["ome"]["multiscales"][0]["datasets"][0]["coordinateTransformations"]
    assert ds[0]["scale"] == [1.0, 5.0, 50.0, 50.0] and ds[1]["translation"] == [0, 2.5, 25, 25]
    b = g.attrs["brainrec"]
    assert b["unit"] == "profiles/mm2" and b["p99"][1] == 0
    assert b["p99"][0] == pytest.approx(np.percentile(values[0], 99)) and b["p99"][0] < 1500
    write_density(out, values * 0, meta)  # replaced, nothing left over
    assert zarr.open_group(str(out), mode="r")["0"][:].max() == 0
    assert not list(tmp_path.glob("*.tmp"))


def test_stage_writes_density_and_corrects_only_with_a_thickness(tmp_path):
    cfg, work, catalog, manifest = make_link_setup(tmp_path)
    run_cells(catalog, work, cfg, manifest)
    g = zarr.open_group(str(work.density), mode="r")
    values = g["0"][:]
    entry = manifest.get("cells")["layers"]["density"]
    assert entry["unit"] == UNIT_AREA and g.attrs["brainrec"]["unit"] == UNIT_AREA
    assert values.shape == (6, 4, 46, 46)  # canvas 2613 x 0.88 = 2299 µm / 50 µm, rounded up
    assert g.attrs["brainrec"]["diameters_um"] is None
    # the synthetic tissue holds about 1100 profiles per mm² (C13 cells of one class)
    main = int(np.argmax(values.reshape(6, -1).sum(axis=1)))
    core = values[main, 1][values[main, 1] > 0]
    sizes = manifest.get("cells")["layers"]["n_profiles"]
    assert 500 < np.median(core) < 3000 and sizes > 0
    # profiles well inside the synthetic mask (it covers 0..1304 µm of each slice) lie on
    # squares with a density: the masks are placed in the volume frame like the cells
    from brainrec.cells.stage import volume_frame
    from brainrec.store.cells import load_profiles

    frame = volume_frame(work)
    p, _ = load_profiles(work, 2)
    inner = (p.x_um >= 100) & (p.y_um >= 100) & (p.x_um < 1200) & (p.y_um < 1200)
    xy = frame.transforms[2].apply(np.c_[p.x_um, p.y_um][inner]) - frame.origin_um
    ij = np.floor(xy / 50.0).astype(int)
    assert inner.sum() > 500 and (values[:, 1].sum(axis=0)[ij[:, 1], ij[:, 0]] > 0).all()
    with pytest.raises(StageExistsError, match="density_grid_um"):
        run_cells(catalog, work, cfg.model_copy(update={"density_grid_um": 100.0}), manifest)
    thick = cfg.model_copy(update={"section_thickness_um": 3.0})
    run_cells(catalog, work, thick, manifest, overwrite=True)
    g2 = zarr.open_group(str(work.density), mode="r")
    d = g2.attrs["brainrec"]["diameters_um"][main]
    assert g2.attrs["brainrec"]["unit"] == UNIT_VOLUME and 4 < d < 10
    assert np.allclose(g2["0"][:][main], values[main] / ((3.0 + d) / 1000), rtol=1e-4)
