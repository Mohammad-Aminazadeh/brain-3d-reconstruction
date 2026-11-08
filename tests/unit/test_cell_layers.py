"""M7-07b: Neuroglancer precomputed ellipsoid layers (C17), checked by decoding them again."""

import numpy as np
import pytest

from brainrec.cells.layers import (
    PROPERTIES,
    RECORD,
    _morton,
    _read_shard,
    read_by_id,
    read_layer,
    write_layer,
    write_shard,
)
from brainrec.cells.place import Placed


def _cells(n: int, seed: int = 0, extent=(2000.0, 1500.0, 50.0)) -> Placed:
    rng = np.random.default_rng(seed)
    centre = (rng.uniform(0, 1, (n, 3)) * extent).astype(np.float32)
    r = rng.uniform(1.5, 6, n).astype(np.float32)
    return Placed(
        cell_id=(np.uint64(3) << np.uint64(32)) + rng.permutation(n * 3)[:n].astype(np.uint64),
        class_id=np.ones(n, np.uint8),
        centre=centre,
        radii=np.repeat(r[:, None], 3, 1),
        radius_um=r,
        confidence=rng.uniform(0.3, 1, n).astype(np.float32),
        det_conf=np.full(n, np.nan, np.float32),
        slice_id=rng.integers(1, 141, n).astype(np.uint16),
        n_profiles=np.ones(n, np.uint16),
        nucleolus=rng.integers(0, 2, n).astype(np.uint8),
        blurred=rng.integers(0, 2, n).astype(np.uint8),
    )


@pytest.mark.parametrize("bits", [0, 2, 6])
def test_shard_round_trip(tmp_path, bits):
    rng = np.random.default_rng(bits)
    keys = rng.choice(10**6, 300, replace=False).astype(np.uint64)
    fixed = rng.integers(0, 256, (300, 12)).astype(np.uint8)
    spec = write_shard(tmp_path / "a.shard", keys, fixed, bits)
    assert spec["minishard_bits"] == bits and spec["hash"] == "identity"
    back = _read_shard(tmp_path / "a.shard", bits)
    assert back == {int(k): fixed[i].tobytes() for i, k in enumerate(keys)}
    blobs = [bytes(rng.integers(0, 256, rng.integers(1, 40)).astype(np.uint8)) for _ in keys]
    write_shard(tmp_path / "b.shard", keys, blobs, bits)
    assert _read_shard(tmp_path / "b.shard", bits) == {int(k): b for k, b in zip(keys, blobs)}


def test_morton_interleaves_x_then_y():
    pos = np.array([[1, 0, 0], [0, 1, 0], [1, 1, 0], [2, 0, 0], [3, 3, 0]])
    assert _morton(pos, [4, 4, 1]).tolist() == [1, 2, 3, 4, 15]
    assert _morton(np.array([[0, 0, 0]]), [1, 1, 1]).tolist() == [0]


def test_every_cell_once_with_its_record(tmp_path):
    cells = _cells(5000)
    write_layer(tmp_path / "layer_x", cells, limit=300)
    _info, levels = read_layer(tmp_path / "layer_x")
    ids = np.concatenate([lv[2] for lv in levels])
    assert len(ids) == len(cells) and set(ids.tolist()) == set(cells.cell_id.tolist())
    recs = np.concatenate([lv[1] for lv in levels])
    order = {int(c): i for i, c in enumerate(cells.cell_id)}
    i = np.array([order[int(c)] for c in ids])
    assert np.allclose(recs["geom"][:, :3], cells.centre[i])
    assert np.allclose(recs["geom"][:, 3:], cells.radii[i])
    assert np.array_equal(recs["slice"], cells.slice_id[i])
    assert np.array_equal(recs["nucleolus"], cells.nucleolus[i])
    assert np.allclose(recs["confidence"], cells.confidence[i])
    assert np.isnan(recs["det_conf"]).all() and (recs["n_profiles"] == 1).all()
    assert RECORD.itemsize % 4 == 0


def test_spatial_levels_respect_the_limit_and_the_grid(tmp_path):
    cells = _cells(5000)
    write_layer(tmp_path / "layer_x", cells, limit=300)
    info, levels = read_layer(tmp_path / "layer_x")
    assert len(levels) >= 2
    lo = np.array(info["lower_bound"])
    for i, (keys, recs, _) in enumerate(levels):
        sp = info["spatial"][i]
        assert sp["grid_shape"] == [2**i, 2**i, 1] and sp["limit"] == 300
        _, counts = np.unique(keys, return_counts=True)
        if i < len(levels) - 1:
            assert counts.max() <= 300
        pos = ((recs["geom"][:, :3] - lo) / np.array(sp["chunk_size"])).astype(int)
        pos = np.clip(pos, 0, np.array(sp["grid_shape"]) - 1)
        assert np.array_equal(_morton(pos, sp["grid_shape"]), keys)  # each cell in its chunk


def test_info_by_id_and_files(tmp_path):
    cells = _cells(20000, seed=1)
    out = tmp_path / "layer_neuron"
    write_layer(out, cells, bounds=(np.zeros(3), np.array([2100.0, 1600, 60])), limit=500)
    info, _ = read_layer(out)
    assert info["@type"] == "neuroglancer_annotations_v1"
    assert info["annotation_type"] == "ELLIPSOID" and info["properties"] == PROPERTIES
    assert info["dimensions"] == {d: [1e-6, "m"] for d in "xyz"}
    assert info["lower_bound"] == [0, 0, 0] and info["upper_bound"] == [2100, 1600, 60]
    assert info["by_id"]["sharding"]["minishard_bits"] == 12
    files = [p for p in out.rglob("*") if p.is_file()]
    assert len(files) <= 20
    for k in (0, 777, 19999):
        rec = read_by_id(out, int(cells.cell_id[k]))
        assert rec is not None and np.allclose(rec["geom"][:3], cells.centre[k])
    assert read_by_id(out, 12345) is None


def test_empty_layer_and_rewrite(tmp_path):
    out = tmp_path / "layer_other"
    write_layer(out, _cells(10).subset(np.zeros(10, bool)))
    _info, levels = read_layer(out)
    assert len(levels) == 1 and len(levels[0][2]) == 0
    write_layer(out, _cells(50, seed=2))  # replaces the layer
    _, levels = read_layer(out)
    assert sum(len(lv[2]) for lv in levels) == 50
    assert not list(tmp_path.glob("*.tmp"))


def test_duplicate_ids_are_refused(tmp_path):
    cells = _cells(10)
    cells.cell_id[3] = cells.cell_id[4]
    with pytest.raises(ValueError, match="unique"):
        write_layer(tmp_path / "layer_x", cells)


def test_coarse_levels_are_a_random_sample(tmp_path):
    cells = _cells(5000)
    cells = cells.subset(np.argsort(cells.centre[:, 0]))  # stored from left to right
    write_layer(tmp_path / "layer_x", cells, limit=300)
    _, levels = read_layer(tmp_path / "layer_x")
    first = levels[0][1]["geom"][:, 0]
    assert len(first) == 300 and first.min() < 300 and first.max() > 1700  # not the left edge


def test_the_finest_level_keeps_dense_clusters_whole(tmp_path):
    cells = _cells(20)
    cells.centre[:] = cells.centre[0]  # all at one spot: no grid level separates them
    write_layer(tmp_path / "layer_x", cells, limit=1)
    info, levels = read_layer(tmp_path / "layer_x")
    assert len(info["spatial"]) == 13  # levels 0 to MAX_LEVEL (12), one cell each before
    assert sum(len(lv[2]) for lv in levels) == 20 and len(levels[-1][2]) == 20 - 12
