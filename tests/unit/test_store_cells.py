import json

import numpy as np
import pytest

from brainrec.store import WorkDir
from brainrec.store.cells import (
    FEATURES,
    MAX_OUTLINE_VERTICES,
    Profiles,
    encode_outlines,
    load_profiles,
    save_profiles,
)

CLASSES = [
    "Neuron",
    "Astrocyte-like",
    "Oligodendrocyte-like",
    "Microglia-like",
    "Vessel-wall",
    "Other",
]
META = {"source": "s_3_H.ndpi", "detector": "instanseg", "detector_version": "0.1.2",
        "downsample": 2, "mpp_um": [0.2205, 0.2205], "classifier": "rules-v1",
        "class_names": CLASSES}  # fmt: skip


def _profiles(n: int = 5, seed: int = 0) -> Profiles:
    rng = np.random.default_rng(seed)
    x, y = rng.uniform(0, 30000, n), rng.uniform(0, 20000, n)
    r = rng.uniform(2, 6, n)
    angles = np.linspace(0, 2 * np.pi, 50, endpoint=False)
    outlines = [np.stack([xi + ri * np.cos(angles), yi + ri * np.sin(angles)], 1)
                for xi, yi, ri in zip(x, y, r, strict=True)]  # fmt: skip
    off, xy = encode_outlines(outlines, x, y)
    prob = rng.dirichlet(np.ones(len(CLASSES)), n)
    return Profiles(
        x, y, r, np.pi * r**2, off, xy, rng.normal(size=(n, len(FEATURES))),
        rng.integers(0, 2, n), rng.uniform(0.5, 1, n), rng.integers(0, 2, n),
        prob.argmax(1) + 1, prob, prob.max(1),
    )  # fmt: skip


def test_round_trip_is_exact(tmp_path):
    work, p = WorkDir(tmp_path), _profiles()
    save_profiles(work, 3, p, META)
    q, meta = load_profiles(work, 3)
    for name in ("x_um", "y_um", "r_um", "outline_offsets", "outline_xy", "features", "class_id",
                 "class_prob", "confidence", "blurred", "nucleolus_seen", "det_conf"):  # fmt: skip
        assert np.array_equal(getattr(q, name), getattr(p, name)), name
        assert getattr(q, name).dtype == getattr(p, name).dtype, name
    assert (
        q.x_um.dtype == np.float64 and q.r_um.dtype == np.float32 and q.class_id.dtype == np.uint8
    )
    assert meta == {"schema": "brainrec.cells/1", "slice_id": 3, **META, "n_profiles": 5}
    assert not list(tmp_path.rglob("*.tmp"))


def test_files_follow_the_layout(tmp_path):
    work = WorkDir(tmp_path)
    save_profiles(work, 3, _profiles(), META)
    assert sorted(p.name for p in work.cells_dir.iterdir()) == ["3.json", "3.npz"]
    with np.load(work.cell_profiles(3)) as z:  # features stored by name (C13)
        assert set(FEATURES) <= set(z.files) and "features" not in z.files


def test_empty_slice_round_trips(tmp_path):
    work = WorkDir(tmp_path)
    save_profiles(work, 4, Profiles.empty(len(CLASSES)), META)
    q, meta = load_profiles(work, 4)
    assert len(q) == 0 and meta["n_profiles"] == 0 and q.features.shape == (0, len(FEATURES))


def test_outlines_are_compact_and_accurate():
    p = _profiles(n=3)
    assert np.all(np.diff(p.outline_offsets) == MAX_OUTLINE_VERTICES)  # 50 vertices thinned
    for i in range(3):
        d = np.hypot(*(p.outline(i) - [p.x_um[i], p.y_um[i]]).T)
        assert np.allclose(d, p.r_um[i], atol=0.08)  # 0.1 µm steps
    assert p.feature("halo").shape == (3,)


def test_metadata_is_written_last_and_needed(tmp_path):
    work = WorkDir(tmp_path)
    save_profiles(work, 3, _profiles(), META)
    work.cell_meta(3).unlink()  # as if interrupted before the metadata
    with pytest.raises(FileNotFoundError, match="slice 3 has no cell file"):
        load_profiles(work, 3)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda p: setattr(p, "class_id", np.zeros(len(p), np.uint8)), "no unclassified"),
        (lambda p: setattr(p, "class_id", (p.class_id % 6) + 1), "not the most probable"),
        (lambda p: setattr(p, "confidence", p.confidence * 0.5), "not the top class probability"),
        (lambda p: setattr(p, "class_prob", p.class_prob[:, :5]), "class_prob has shape"),
        (lambda p: setattr(p, "r_um", p.r_um[:3]), "r_um has shape"),
        (lambda p: setattr(p, "features", p.features[:, :8]), "features has shape"),
        (lambda p: setattr(p, "outline_offsets", p.outline_offsets[:-1]), "outline_offsets"),
    ],
)
def test_invalid_tables_are_refused(tmp_path, change, message):
    p = _profiles()
    change(p)
    with pytest.raises(ValueError, match=message):
        save_profiles(WorkDir(tmp_path), 3, p, META)
    assert not WorkDir(tmp_path).cell_meta(3).exists()


def test_missing_metadata_fields_are_refused(tmp_path):
    with pytest.raises(ValueError, match="classifier"):
        save_profiles(
            WorkDir(tmp_path), 3, _profiles(), {k: v for k, v in META.items() if k != "classifier"}
        )


def test_other_schema_or_count_is_refused(tmp_path):
    work = WorkDir(tmp_path)
    save_profiles(work, 3, _profiles(), META)
    meta = json.loads(work.cell_meta(3).read_text())
    work.cell_meta(3).write_text(json.dumps({**meta, "n_profiles": 9}))
    with pytest.raises(ValueError, match="5 profiles, metadata says 9"):
        load_profiles(work, 3)
    work.cell_meta(3).write_text(json.dumps({**meta, "schema": "brainrec.cells/0"}))
    with pytest.raises(ValueError, match="unsupported schema"):
        load_profiles(work, 3)


def test_failed_data_write_leaves_no_metadata(tmp_path, monkeypatch):
    import brainrec.store.cells as cells_mod

    real = cells_mod.write_atomic

    def failing(path, data):
        if path.suffix == ".npz":
            raise OSError("drive disconnected")
        real(path, data)

    monkeypatch.setattr(cells_mod, "write_atomic", failing)
    work = WorkDir(tmp_path)
    with pytest.raises(OSError, match="disconnected"):
        save_profiles(work, 3, _profiles(), META)
    assert not work.cell_meta(3).exists()  # the slice does not count as done
