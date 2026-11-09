"""M7-14: cell table and QuPath export (C18), on four synthetic registered sections."""

import csv
import json
from collections import namedtuple

import numpy as np
import pytest

from brainrec.cells import export
from brainrec.cells.export import (
    TableInput,
    check_space,
    qupath_features,
    table_columns,
    write_table,
)
from brainrec.cells.layers import read_layer
from brainrec.cells.link import build_cells
from brainrec.cells.stage import run_cells
from brainrec.store.cells import FEATURES, load_cell_pair, load_profiles
from tests.unit.test_cells_link import NAMES, make_link_setup


def _table(work):
    with work.cell_table.open() as f:
        return list(csv.DictReader(f))


def _strict(text: str):
    def bad(token):
        raise ValueError(f"{token} in JSON")

    return json.loads(text, parse_constant=bad)


@pytest.fixture
def built(tmp_path):
    cfg, work, catalog, manifest = make_link_setup(tmp_path)
    run_cells(catalog, work, cfg, manifest, qupath=[2, 3])
    return cfg, work, catalog, manifest


def test_every_cell_is_in_the_table_with_its_representative(built):
    cfg, work, _, manifest = built
    rows = _table(work)
    entry = manifest.get("cells")["layers"]
    assert len(rows) == entry["n_cells"] and list(rows[0]) == table_columns(list(NAMES))
    ids = [int(r["cell_id"]) for r in rows]
    assert len(set(ids)) == len(ids)
    layer_ids = set()
    for t in cfg.cell_types:
        _, levels = read_layer(work.cell_layer(t.name))
        layer_ids |= set(np.concatenate([lv[2] for lv in levels]).tolist())
    assert layer_ids == set(ids)  # the same cells as in the viewer
    assert sum(int(r["n_profiles"]) for r in rows) == entry["n_profiles"]
    profiles = {k: load_profiles(work, k) for k in (1, 2, 3, 4)}
    for r in rows[:: max(1, len(rows) // 50)]:
        k, i = int(r["slice"]), int(r["cell_id"]) & 0xFFFFFFFF
        assert k == int(r["cell_id"]) >> 32
        p, meta = profiles[k]
        mpp = meta["mpp_um"][0]
        assert float(r["x_slice_um"]) == pytest.approx(p.x_um[i], abs=0.006)
        assert float(r["x_px0"]) == pytest.approx(p.x_um[i] / mpp, abs=0.06)  # QuPath pixels
        assert float(r["y_px0"]) == pytest.approx(p.y_um[i] / mpp, abs=0.06)
        for j, name in enumerate(FEATURES):
            assert float(r[name]) == pytest.approx(p.features[i, j], rel=1e-3, abs=1e-6)
        probs = np.array([float(r[f"prob_{n}"]) for n in NAMES])
        assert probs.sum() == pytest.approx(1.0, abs=1e-3)
        assert r["class"] == NAMES[int(probs.argmax())]
        assert float(r["confidence"]) == pytest.approx(probs.max(), abs=1e-4)
        assert r["radius_3d_um"] == "nan" and r["det_conf"] == "nan"
    linked = [r for r in rows if int(r["n_profiles"]) >= 2]
    assert linked and float(linked[0]["radius_um"]) > 0


def test_a_known_section_thickness_fills_the_3d_radius(tmp_path):
    cfg, work, catalog, manifest = make_link_setup(tmp_path)
    cfg = cfg.model_copy(update={"section_thickness_um": 3.0})
    run_cells(catalog, work, cfg, manifest)
    rows = _table(work)
    r3 = np.array([float(r["radius_3d_um"]) for r in rows])
    r2 = np.array([float(r["radius_um"]) for r in rows])
    single = np.array([r["n_profiles"] == "1" for r in rows])
    assert np.isfinite(r3).all() and single.any() and (~single).any()
    assert np.allclose(r3[single], r2[single], atol=0.002)  # one profile: its own radius
    assert np.median(np.abs(r3[~single] / r2[~single] - 1)) < 0.05  # both in volume µm


def test_qupath_export_holds_every_profile_with_its_cell(built):
    _, work, _, _ = built
    rows = {int(r["cell_id"]): r for r in _table(work)}
    data = {}
    for k in (2, 3):
        data[k] = _strict(work.qupath(k).read_text())
        p, meta = load_profiles(work, k)
        feats = data[k]["features"]
        assert data[k]["type"] == "FeatureCollection" and len(feats) == len(p)
        mpp = meta["mpp_um"][0]
        for i in (0, len(p) // 2, len(p) - 1):
            f = feats[i]
            ring = np.array(f["geometry"]["coordinates"][0])
            assert f["properties"]["objectType"] == "detection" and (ring[0] == ring[-1]).all()
            assert np.allclose(ring[:-1], p.outline(i) / mpp, atol=0.051)  # level-0 pixels
            m = f["properties"]["measurements"]
            cell = rows[int(m["cell_id"])]
            assert f["properties"]["classification"]["name"] == cell["class"]
            assert m["profiles in cell"] == int(cell["n_profiles"])
            assert "det_conf" not in m and m["radius_um"] == pytest.approx(p.r_um[i])
    # linked profiles of slices 2 and 3 carry the same cell id
    links, _ = load_cell_pair(work, 3)
    same = [data[3]["features"][a]["properties"]["measurements"]["cell_id"]
            == data[2]["features"][b]["properties"]["measurements"]["cell_id"]
            for a, b in links["links"][:200]]  # fmt: skip
    assert len(same) > 50 and all(same)


def test_resume_rebuilds_a_missing_table_and_exports_on_request(tmp_path):
    cfg, work, catalog, manifest = make_link_setup(tmp_path)
    run_cells(catalog, work, cfg, manifest)
    assert not work.qupath(2).exists()
    assert run_cells(catalog, work, cfg, manifest).skipped == [1, 2, 3, 4]
    work.cell_table.unlink()
    assert run_cells(catalog, work, cfg, manifest).done == [1, 2, 3, 4]  # same inputs: no overwrite
    before = work.cell_table.stat().st_mtime_ns
    result = run_cells(catalog, work, cfg, manifest, qupath=[4])
    assert result.skipped == [1, 2, 3, 4] and work.qupath(4).exists()
    assert work.cell_table.stat().st_mtime_ns == before  # only the export was written
    assert manifest.get("cells")["qupath"]["4"]["n_profiles"] == len(load_profiles(work, 4)[0])
    from brainrec.cells.stage import CellsInputError

    with pytest.raises(CellsInputError, match=r"\[7\] are not in the range 1-4"):
        run_cells(catalog, work, cfg, manifest, qupath=[7])
    assert not list(work.cells_dir.rglob("*.tmp"))


def test_space_check_and_input_checks(tmp_path, monkeypatch):
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(export.shutil, "disk_usage", lambda _p: Usage(10**9, 0, 10**6))
    with pytest.raises(OSError, match="not enough free space for table.csv"):
        check_space(tmp_path / "table.csv", 10**6)
    check_space(tmp_path / "table.csv", 10**5)
    cells = build_cells(np.arange(3), np.ones(3), np.ones((3, 1)), np.zeros(3), np.zeros(3), None)
    extra = TableInput(np.zeros(2), np.zeros(2), np.ones(2), np.zeros((2, 9)))
    with pytest.raises(ValueError, match="differ in length"):
        write_table(tmp_path / "t.csv", _placed(3), cells, extra, ["A"])


def _placed(n):
    from brainrec.cells.place import Placed

    z = np.zeros(n, np.float32)
    return Placed(np.arange(n, dtype=np.uint64), np.ones(n, np.uint8), np.zeros((n, 3)),
                  np.ones((n, 3)), z, z, z, np.ones(n, np.uint16), np.ones(n, np.uint16),
                  np.zeros(n, np.uint8), np.zeros(n, np.uint8))  # fmt: skip


def test_profiles_without_an_outline_get_an_octagon():
    from tests.unit.test_cells_align import _profiles_of

    p = _profiles_of(np.array([[100.0, 50.0]]), np.array([4.0]))
    p.outline_offsets = np.array([0, 2], np.int64)
    p.outline_xy = np.zeros((2, 2), np.int16)
    f = next(qupath_features(p, 0.5, np.array([7]), ["Neuron"], np.array([0.8]),
                             np.array([1]), {"Neuron": (1, 2, 3)}))  # fmt: skip
    ring = np.array(f["geometry"]["coordinates"][0])
    assert len(ring) == 9 and np.allclose(np.hypot(*(ring[:-1] - [200, 100]).T), 8.0, atol=0.1)
    assert f["properties"]["classification"] == {"name": "Neuron", "color": [1, 2, 3]}


# outline 4084 of slice 9 (level-0 pixels): QuPath rejected it, "Reduction failed, possible
# invalid input"; it folds back on itself near (70477, 7704)
FOLDED = [[70513.2, 7673.1], [70499.1, 7683.1], [70499.1, 7684.9], [70495.0, 7687.2],
          [70492.8, 7690.8], [70491.0, 7690.8], [70485.1, 7694.9], [70481.0, 7697.2],
          [70476.9, 7704.9], [70476.9, 7703.1], [70482.8, 7699.0], [70491.0, 7694.9],
          [70495.0, 7699.0], [70496.9, 7709.0], [70499.1, 7713.1], [70499.1, 7714.9],
          [70503.2, 7717.1], [70506.8, 7723.0], [70506.8, 7721.2], [70510.9, 7718.9],
          [70519.1, 7717.1], [70523.2, 7713.1], [70529.1, 7709.0], [70530.9, 7707.2],
          [70533.1, 7704.9], [70535.0, 7703.1], [70537.2, 7699.0], [70540.9, 7697.2],
          [70540.9, 7690.8], [70539.0, 7679.0], [70537.2, 7675.0], [70533.1, 7675.0]]  # fmt: skip


def test_simple_polygons_are_recognised():
    from brainrec.cells.export import is_simple

    assert is_simple([[0, 0], [4, 0], [4, 4], [0, 4]])
    assert is_simple([[0, 0], [4, 0], [4, 0], [4, 4], [0, 4], [0, 0]])  # repeats in a row: fine
    assert is_simple([[0, 0], [4, 0], [2, 1], [4, 4], [0, 4]])  # concave
    assert not is_simple([[0, 0], [4, 4], [4, 0], [0, 4]])  # figure eight
    assert not is_simple([[0, 0], [4, 0], [2, 2], [4, 4], [0, 4], [2, 2]])  # vertex visited twice
    assert not is_simple([[0, 0], [4, 0], [1, 0], [1, 2]])  # edges overlapping on a line
    assert not is_simple([[0, 0], [4, 0], [0, 0]]) and not is_simple(FOLDED)


def test_folded_outlines_become_their_convex_hull():
    from brainrec.cells.export import is_simple, valid_ring

    ring, replaced = valid_ring(np.array(FOLDED))
    assert replaced and is_simple(ring) and 3 <= len(ring) < len(FOLDED)
    assert {tuple(v) for v in ring.tolist()} <= {tuple(v) for v in FOLDED}  # hull vertices, 0.1 px
    square = np.array([[10.04, 10.0], [20.0, 10.0], [20.0, 20.0], [10.0, 20.0]])
    same, replaced = valid_ring(square)
    assert not replaced and np.allclose(same, np.round(square, 1))
