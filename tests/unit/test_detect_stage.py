"""M7-06: the detect stage (C6, C7, C13, C14) on synthetic slides with a stand-in detector."""

import json
import os
from dataclasses import replace

import cv2
import numpy as np
import pytest

from brainrec.catalog import DEFAULT_CELL_TYPES, Catalog, ProjectConfig
from brainrec.detect.nuclei import Detector
from brainrec.detect.stage import _subset, run_detect
from brainrec.store import Manifest, StageExistsError, WorkDir, write_atomic
from brainrec.store.cells import Profiles, load_profiles
from tests.synthetic.fake_slide import FakeSlide
from tests.synthetic.nuclei import make_nuclei, segment_by_colour
from tests.unit.test_classify import _model

UM0 = 0.2205
DETECTOR = Detector("colour stand-in", "test 1", segment_by_colour)
MODEL = replace(_model(), sha256="a" * 64)


@pytest.fixture(scope="module")
def images():
    return {k: make_nuclei(shape=(1024, 1024), seed=k, um_per_px=UM0, n_per_class=3,
                           touching_pairs=1, tile_px=600, glass_px=128, n_on_border=2)
            for k in (1, 2)}  # fmt: skip


class Factory:
    """Synthetic slides (level 0 and 2x) by slice id; counts the slides opened."""

    def __init__(self, images):
        self.images = images
        self.opened: list[int] = []

    def __call__(self, path):
        k = int(path.name.split("_")[1])
        self.opened.append(k)
        return FakeSlide.from_image(self.images[k].rgb, 1, (2,), (UM0, UM0))


def _setup(tmp_path, images, **settings):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    for k in images:  # written once: a rewritten file is a changed source
        if not (data / f"s_{k}_H.ndpi").exists():
            (data / f"s_{k}_H.ndpi").write_bytes(f"slice {k}".encode())
    cfg = ProjectConfig(data_dir=data, work_dir=tmp_path / "work", z_spacing_um=5.0,
                        detect_tile_px=300, **settings)  # fmt: skip
    work = WorkDir(cfg.work_dir)
    for k, syn in images.items():  # what preprocess writes (C4): mask and metadata at 32x
        small = cv2.resize(syn.mask, (32, 32), interpolation=cv2.INTER_AREA)
        if not work.mask(k).exists():
            write_atomic(work.mask(k), cv2.imencode(".png", np.where(small >= 128, 255, 0)
                                                    .astype(np.uint8))[1].tobytes())  # fmt: skip
            write_atomic(work.reg_meta(k), json.dumps({"downsample": 32.0}).encode())
    return cfg, work, Catalog.scan(data), Manifest(work.manifest)


def _run(cfg, work, catalog, manifest, factory, **kw):
    kw = {"detector": DETECTOR, "model": MODEL, **kw}
    return run_detect(catalog, work, cfg, manifest, reader_factory=factory, **kw)


def test_writes_classified_cell_files(tmp_path, images):
    cfg, work, catalog, manifest = _setup(tmp_path, images)
    result = _run(cfg, work, catalog, manifest, Factory(images))
    assert result.done == [1, 2] and result.skipped == []
    names = [t.name for t in DEFAULT_CELL_TYPES]
    state = manifest.get("detect")
    for k, syn in images.items():
        p, meta = load_profiles(work, k)  # validates the C13 table
        row = state["slices"][str(k)]
        assert len(p) == meta["n_profiles"] == row["n_profiles"]
        assert len(p) + row["n_dropped_radius"] == len(syn.truth)
        assert meta["source"] == f"s_{k}_H.ndpi" and meta["class_names"] == names
        assert (meta["detector"], meta["detector_version"]) == ("colour stand-in", "test 1")
        assert (meta["downsample"], meta["classifier"]) == (2, "rules-v1")
        assert meta["mpp_um"] == [UM0, UM0] and meta["schema"] == "brainrec.cells/1"
        assert len(meta["tile_sharpness"]) == row["n_tiles"] and row["n_tiles"] > 1
        # the smooth synthetic image is below the sharpness limit everywhere
        assert meta["n_blurred_tiles"] == row["n_blurred_tiles"] == row["n_tiles"]
        assert p.blurred.all()
        assert np.isnan(p.det_conf).all() and p.class_prob.shape == (len(p), 6)
        assert row["source"][0] == f"s_{k}_H.ndpi" and row["seconds"] >= 0
    assert not list(work.root.rglob("*.tmp"))


def test_second_run_reads_no_pixels(tmp_path, images):
    cfg, work, catalog, manifest = _setup(tmp_path, images)
    _run(cfg, work, catalog, manifest, Factory(images))
    again = Factory(images)
    result = _run(cfg, work, catalog, manifest, again)
    assert result.done == [] and result.skipped == [1, 2] and again.opened == []


def test_changed_inputs_redo_only_that_slice(tmp_path, images):
    cfg, work, catalog, manifest = _setup(tmp_path, images)
    _run(cfg, work, catalog, manifest, Factory(images))
    mask = cv2.imread(str(work.mask(2)), cv2.IMREAD_GRAYSCALE)
    mask[:, :8] = 0  # preprocess was redone with another mask
    write_atomic(work.mask(2), cv2.imencode(".png", mask)[1].tobytes())
    f = Factory(images)
    assert _run(cfg, work, catalog, manifest, f).done == [2] and f.opened == [2]
    work.cell_profiles(1).unlink()  # a lost cell file
    assert _run(cfg, work, catalog, manifest, Factory(images)).done == [1]
    src = catalog.slices[1].path  # a changed source file
    os.utime(src, ns=(src.stat().st_atime_ns, src.stat().st_mtime_ns + 10**9))
    assert _run(cfg, work, catalog, manifest, Factory(images)).done == [2]


@pytest.mark.parametrize(
    ("settings", "kw", "field"),
    [
        ({"detect_min_area_um2": 10.0}, {}, "detect_min_area_um2"),
        ({}, {"detector": Detector("colour stand-in", "test 2", segment_by_colour)},
         "detector_version"),
        ({}, {"model": replace(MODEL, sha256="b" * 64)}, "classifier_sha256"),
    ],
)  # fmt: skip
def test_other_settings_need_overwrite(tmp_path, images, settings, kw, field):
    cfg, work, catalog, manifest = _setup(tmp_path, images)
    _run(cfg, work, catalog, manifest, Factory(images))
    cfg2, *_ = _setup(tmp_path, images, **settings)
    with pytest.raises(StageExistsError, match=field):
        _run(cfg2, work, catalog, manifest, Factory(images), **kw)
    result = _run(cfg2, work, catalog, manifest, Factory(images), overwrite=True, **kw)
    assert result.done == [1, 2]


def test_missing_mask_means_preprocess_first(tmp_path, images):
    cfg, work, catalog, manifest = _setup(tmp_path, images)
    work.mask(1).unlink()
    with pytest.raises(FileNotFoundError, match="preprocess"):
        _run(cfg, work, catalog, manifest, Factory(images))


def test_profiles_outside_the_radius_limits_are_dropped(tmp_path, images):
    types = [t.model_copy(update={"radius_um": (1.5, 3.0)}) for t in DEFAULT_CELL_TYPES]
    cfg, work, catalog, manifest = _setup(tmp_path, images, cell_types=types)
    _run(cfg, work, catalog, manifest, Factory(images))
    for k, syn in images.items():
        p, _ = load_profiles(work, k)
        row = manifest.get("detect")["slices"][str(k)]
        assert row["n_dropped_radius"] > 0 and len(p) + row["n_dropped_radius"] == len(syn.truth)
        assert (p.r_um <= 3.0).all() and (p.r_um >= 1.5).all()


def test_subset_reindexes_the_outlines():
    n = 4
    off = np.array([0, 3, 7, 10, 14])
    xy = np.arange(28).reshape(14, 2)
    prob = np.eye(6)[[0, 1, 2, 3]].astype(np.float32)
    p = Profiles(np.arange(n), np.zeros(n), np.ones(n), np.ones(n), off, xy, np.zeros((n, 9)),
                 np.zeros(n), np.zeros(n), np.zeros(n), np.arange(1, 5), prob, np.ones(n))  # fmt: skip
    q = _subset(p, np.array([False, True, False, True]))
    q.validate(6)
    assert q.x_um.tolist() == [1, 3] and q.outline_offsets.tolist() == [0, 4, 8]
    assert np.array_equal(q.outline(0), p.outline(1)) and np.array_equal(q.outline(1), p.outline(3))
    assert len(_subset(p, np.zeros(n, bool))) == 0


def test_asks_before_each_next_slice_and_continues_later(tmp_path, images):
    cfg, work, catalog, manifest = _setup(tmp_path, images)
    asked = []

    def stop(finished, counts, rest):
        asked.append((finished, counts["n_profiles"], rest))
        return False

    f = Factory(images)
    result = _run(cfg, work, catalog, manifest, f, proceed=stop)
    assert result.done == [1] and result.left == [2] and f.opened == [1]
    assert asked == [(1, manifest.get("detect")["slices"]["1"]["n_profiles"], [2])]
    result = _run(cfg, work, catalog, manifest, Factory(images), proceed=stop)
    assert result.skipped == [1] and result.done == [2] and result.left == []
    assert len(asked) == 1  # nothing left after slice 2: no question


def test_answer_yes_runs_on(tmp_path, images):
    cfg, work, catalog, manifest = _setup(tmp_path, images)
    result = _run(cfg, work, catalog, manifest, Factory(images), proceed=lambda *a: True)
    assert result.done == [1, 2] and result.left == []
