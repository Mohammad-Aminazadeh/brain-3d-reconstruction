"""M7-07c: the cells stage (C6, C7, C16, C17) on a small registered setup without slides."""

import json

import numpy as np
import pytest
import zarr

from brainrec.catalog import DEFAULT_CELL_TYPES, Catalog, ProjectConfig
from brainrec.cells.layers import read_by_id, read_layer
from brainrec.cells.stage import CellsInputError, run_cells
from brainrec.detect.classify import classify
from brainrec.registration import PairResult, Transform2D
from brainrec.store import Manifest, StageExistsError, WorkDir, write_atomic
from brainrec.store.cells import save_profiles
from tests.unit.test_classify import _at, _model, _nuclei

NAMES = [t.name for t in DEFAULT_CELL_TYPES]
PAIR = Transform2D.similarity(np.radians(4), 1.02, 150.0, -80.0)  # slice 2 -> slice 1
ORIGIN = (-300.0, -200.0)


def _profiles(seed: int):
    rng = np.random.default_rng(seed)
    groups = rng.integers(0, 8, 40)
    feats = np.array([_at(int(g), nucleolus=0.6) for g in groups])
    nu = _nuclei(feats)
    nu.x_um[:] = rng.uniform(0, 800, 40)
    nu.y_um[:] = rng.uniform(0, 600, 40)
    return classify(nu, _model(), tuple(NAMES))


def _meta(k: int, names=NAMES) -> dict:
    return {"source": f"s_{k}_H.ndpi", "detector": "test", "detector_version": "1",
            "downsample": 2, "mpp_um": [0.2205, 0.2205], "classifier": "rules-v1",
            "class_names": names, "n_blurred_tiles": 0, "tile_sharpness": []}  # fmt: skip


def _volume(work: WorkDir, ids, z=5.0, mode="registered", transforms=None):
    transforms = transforms or {1: Transform2D.identity(), 2: PAIR}
    g = zarr.open_group(str(work.volume), mode="w")
    g.attrs["ome"] = {"multiscales": [{"datasets": [{"path": "0", "coordinateTransformations": [
        {"type": "scale", "scale": [1.0, z, 0.88, 0.88]}]}]}]}  # fmt: skip
    g.attrs["brainrec"] = {
        "mode": mode, "slice_ids": ids, "canvas_origin_um": list(ORIGIN),
        "slice_to_reference_um": {str(k): transforms[k].matrix.tolist() for k in ids},
    }  # fmt: skip


def make_setup(tmp_path):
    """Two slices with cell files, chained and pair transforms and a registered volume's
    metadata; returns (cfg, work, catalog, manifest, profiles)."""
    data = tmp_path / "data"
    data.mkdir()
    for k in (1, 2):
        (data / f"s_{k}_H.ndpi").write_bytes(b"x")
    cfg = ProjectConfig(data_dir=data, work_dir=tmp_path / "work", z_spacing_um=5.0)
    work = WorkDir(cfg.work_dir)
    Transform2D.identity().save(work.chained_transform(1))
    PAIR.save(work.chained_transform(2))
    pr = PairResult(1, 2, PAIR, "features", 100, 80, 10.0, 0.95)
    write_atomic(work.pair_transform(2), json.dumps(pr.to_dict()).encode())
    profiles = {k: _profiles(k) for k in (1, 2)}
    for k, p in profiles.items():
        save_profiles(work, k, p, _meta(k))
    _volume(work, [1, 2])
    return cfg, work, Catalog.scan(data), Manifest(work.manifest), profiles


@pytest.fixture
def setup(tmp_path):
    return make_setup(tmp_path)


def test_layers_per_class_in_the_volume_frame(setup):
    cfg, work, catalog, manifest, profiles = setup
    assert run_cells(catalog, work, cfg, manifest).done == [1, 2]
    entry = manifest.get("cells")["layers"]
    counts = {n: 0 for n in NAMES}
    for p in profiles.values():
        for c in p.class_id:
            counts[NAMES[c - 1]] += 1
    assert entry["counts"] == counts and sum(counts.values()) == 80
    for t in DEFAULT_CELL_TYPES:
        info, levels = read_layer(work.cell_layer(t.name))
        assert sum(len(lv[2]) for lv in levels) == counts[t.name]
        assert info["lower_bound"] == read_layer(work.cell_layer(NAMES[0]))[0]["lower_bound"]
    # one cell of slice 2: its centre through the pair transform, minus the canvas origin
    p = profiles[2]
    i = 7
    rec = read_by_id(work.cell_layer(NAMES[p.class_id[i] - 1]), (2 << 32) + i)
    expect = PAIR.apply(np.array([[p.x_um[i], p.y_um[i]]]))[0] - ORIGIN
    assert np.allclose(rec["geom"][:2], expect, atol=1e-3) and rec["geom"][2] == 7.5
    assert rec["geom"][3] == pytest.approx(p.r_um[i] * PAIR.scale, rel=1e-5)


def test_second_run_is_up_to_date_and_changes_need_overwrite(setup):
    cfg, work, catalog, manifest, profiles = setup
    run_cells(catalog, work, cfg, manifest)
    assert run_cells(catalog, work, cfg, manifest).skipped == [1, 2]
    p = profiles[1]
    p.x_um[:] += 1.0
    save_profiles(work, 1, p, _meta(1))  # detection was redone
    with pytest.raises(StageExistsError, match="cells"):
        run_cells(catalog, work, cfg, manifest)
    assert run_cells(catalog, work, cfg, manifest, overwrite=True).done == [1, 2]
    assert run_cells(catalog, work, cfg, manifest).skipped == [1, 2]


def test_missing_or_mismatched_inputs(setup):
    cfg, work, catalog, manifest, _ = setup
    _volume(work, [1])
    with pytest.raises(CellsInputError, match=r"not \[2\]"):
        run_cells(catalog, work, cfg, manifest)
    _volume(work, [1, 2], z=7.0)
    with pytest.raises(CellsInputError, match="z spacing"):
        run_cells(catalog, work, cfg, manifest)
    _volume(work, [1, 2], mode="centred")
    with pytest.raises(CellsInputError, match="not registered"):
        run_cells(catalog, work, cfg, manifest)
    _volume(work, [1, 2], transforms={1: Transform2D.identity(), 2: Transform2D.identity()})
    with pytest.raises(CellsInputError, match="registered again"):
        run_cells(catalog, work, cfg, manifest)
    _volume(work, [1, 2])
    save_profiles(work, 2, _profiles(2), _meta(2, names=[*NAMES[:5], "Glia"]))
    with pytest.raises(CellsInputError, match="detect --overwrite"):
        run_cells(catalog, work, cfg, manifest)
    work.cell_meta(2).unlink()
    with pytest.raises(FileNotFoundError, match="detect"):
        run_cells(catalog, work, cfg, manifest)
    import shutil

    shutil.rmtree(work.volume)
    with pytest.raises(FileNotFoundError, match="volume"):
        run_cells(catalog, work, cfg, manifest)
