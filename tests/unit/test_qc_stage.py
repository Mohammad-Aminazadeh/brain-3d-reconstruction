import json

import cv2
import numpy as np
import pytest

import brainrec.qc.stage as stage_mod
from brainrec.catalog import Catalog, ProjectConfig
from brainrec.preprocess import run_preprocess
from brainrec.qc import run_qc, score_pair
from brainrec.registration import PairResult, RegImage, Transform2D
from brainrec.store import Manifest, StageExistsError, WorkDir, write_atomic
from tests.synthetic.fake_slide import FakeSlide
from tests.synthetic.tissue import apply_similarity, make_tissue, perturb

MPP = (0.2205, 0.2205)
# per slice: motion of the section on its slide (angle °, scale, tx µm, ty µm)
MOTIONS = {1: (0.0, 1.0, 0.0, 0.0), 2: (6.0, 1.03, 120.0, -60.0), 3: (-4.0, 0.98, -90.0, 150.0)}


def _project(tmp_path):
    """Three preprocessed slices of one synthetic section, each moved on its slide, and pair
    files holding the true transforms (as a perfect `register` would write them)."""
    data = tmp_path / "data"
    data.mkdir()
    base = make_tissue(seed=5)
    images, truths = {}, {}
    for k, (a, s, tx, ty) in MOTIONS.items():
        (data / f"s_{k}_H.ndpi").write_bytes(f"slice {k}".encode())
        moved, mm, truths[k] = apply_similarity(base.rgb, base.mask, a, s, tx, ty)
        images[k] = perturb(moved, mm, seed=k)

    def factory(path):
        return FakeSlide.from_image(images[int(path.name.split("_")[1])], 32, (2,), MPP)

    cfg = ProjectConfig(data_dir=data, work_dir=tmp_path / "work", z_spacing_um=5.0)
    work = WorkDir(cfg.work_dir)
    run_preprocess(Catalog.scan(data, (1, 3)), work, cfg, Manifest(work.manifest), factory)
    for k in (2, 3):
        _write_pair(work, k, truths[k - 1].inverse().compose(truths[k]))
    return cfg, work


def _write_pair(work, k, transform, status="features"):
    pr = PairResult(k - 1, k, transform, status, 900, 300, 2.0, 0.95)
    write_atomic(work.pair_transform(k), json.dumps(pr.to_dict()).encode())


def _qc(cfg, work, ids=(1, 3), **kw):
    return run_qc(Catalog.scan(cfg.data_dir, ids), work, cfg, Manifest(work.manifest), **kw)


def _report(work):
    return json.loads(work.qc_report.read_text())


def _fail(*args, **kwargs):
    raise AssertionError("recomputed")


@pytest.fixture
def no_compute(monkeypatch):
    """Call the returned function to make image reads, scores and overlays fail from then on:
    for runs that must only reuse earlier results."""

    def arm():
        monkeypatch.setattr(stage_mod.RegImage, "load", _fail)
        monkeypatch.setattr(stage_mod, "score_pair", _fail)
        monkeypatch.setattr(stage_mod, "render_overlay", _fail)

    return arm


def test_scores_overlays_and_report(tmp_path):
    cfg, work = _project(tmp_path)
    result = _qc(cfg, work)
    assert result.done == [2, 3] and result.skipped == []
    rows = _report(work)["pairs"]
    assert [(r["fixed"], r["moving"]) for r in rows] == [(1, 2), (2, 3)]
    for r in rows:
        k = r["moving"]
        pr = PairResult.from_dict(json.loads(work.pair_transform(k).read_text()))
        expected = score_pair(RegImage.load(work, k - 1), RegImage.load(work, k), pr, 0.2)
        assert (r["n_inliers"], r["rms_um"], r["ncc"], r["dice"]) == (
            expected.n_inliers, expected.rms_um, expected.ncc, expected.dice,
        )  # fmt: skip
        assert r["ncc"] > 0.5 and r["dice"] > 0.95 and not r["flagged"]  # aligned
        overlay = cv2.imread(str(work.qc_overlay(k)))
        assert overlay.shape[:2] == RegImage.load(work, k - 1).image.shape
    assert Manifest(work.manifest).get("qc")["done"] == [2, 3]


def test_wrong_transform_is_flagged(tmp_path):
    cfg, work = _project(tmp_path)
    _write_pair(work, 3, Transform2D.identity())  # slice 3 left where it lies on its slide
    _qc(cfg.model_copy(update={"qc_min_ncc": 0.4}), work)
    rows = {r["moving"]: r for r in _report(work)["pairs"]}
    assert not rows[2]["flagged"] and rows[3]["flagged"]
    assert rows[3]["ncc"] < 0.4 < rows[2]["ncc"]


def test_failed_pair_is_always_flagged(tmp_path):
    cfg, work = _project(tmp_path)
    t = PairResult.from_dict(json.loads(work.pair_transform(2).read_text())).transform
    _write_pair(work, 2, t, status="failed")  # good transform, but registration gave up
    _qc(cfg, work)
    assert [r["flagged"] for r in _report(work)["pairs"]] == [True, False]


def test_second_run_reuses_everything(tmp_path, no_compute):
    cfg, work = _project(tmp_path)
    _qc(cfg, work)
    first = _report(work)
    stamps = [work.qc_overlay(k).stat().st_mtime_ns for k in (2, 3)]
    no_compute()
    result = _qc(cfg, work)
    assert result.done == [] and result.skipped == [2, 3]
    assert _report(work) == first
    assert [work.qc_overlay(k).stat().st_mtime_ns for k in (2, 3)] == stamps


def test_new_registration_result_redoes_that_pair(tmp_path):
    cfg, work = _project(tmp_path)
    _qc(cfg, work)
    _write_pair(work, 3, Transform2D.identity())  # register redone with another result
    result = _qc(cfg, work)
    assert result.done == [3] and result.skipped == [2]
    assert _report(work)["pairs"][1]["ncc"] < 0.4


def test_new_registration_image_redoes_both_pairs_of_that_slice(tmp_path):
    cfg, work = _project(tmp_path)
    _qc(cfg, work)
    image = cv2.imread(str(work.reg_image(2)), cv2.IMREAD_GRAYSCALE)
    image[image > 0] = np.maximum(image[image > 0], 1) - 1  # slightly darker tissue
    write_atomic(work.reg_image(2), cv2.imencode(".png", image)[1].tobytes())
    assert _qc(cfg, work).done == [2, 3]


def test_missing_overlay_is_drawn_again(tmp_path):
    cfg, work = _project(tmp_path)
    _qc(cfg, work)
    work.qc_overlay(2).unlink()
    result = _qc(cfg, work)
    assert result.done == [2] and result.skipped == [3] and work.qc_overlay(2).exists()


def test_extending_the_range_scores_only_the_new_pair(tmp_path):
    cfg, work = _project(tmp_path)
    assert _qc(cfg, work, (1, 2)).done == [2]
    result = _qc(cfg, work, (1, 3))
    assert result.done == [3] and result.skipped == [2]
    assert len(_report(work)["pairs"]) == 2


def test_new_thresholds_change_flags_without_recomputing(tmp_path, no_compute):
    cfg, work = _project(tmp_path)
    _qc(cfg, work)
    no_compute()
    strict = cfg.model_copy(update={"qc_min_ncc": 0.99, "qc_min_dice": None})
    assert _qc(strict, work).skipped == [2, 3]
    report = _report(work)
    assert report["thresholds"] == {"qc_min_ncc": 0.99, "qc_min_dice": None}
    assert [r["flagged"] for r in report["pairs"]] == [True, True]


def test_other_edge_weight_needs_overwrite(tmp_path):
    cfg, work = _project(tmp_path)
    _qc(cfg, work)
    first = _report(work)
    other = cfg.model_copy(update={"reg_edge_weight": 1.0})
    with pytest.raises(StageExistsError, match="other settings"):
        _qc(other, work)
    assert _report(work) == first  # nothing replaced
    assert _qc(other, work, overwrite=True).done == [2, 3]
    assert _report(work)["pairs"][0]["ncc"] != first["pairs"][0]["ncc"]


def test_unregistered_pairs_are_named(tmp_path):
    cfg, work = _project(tmp_path)
    work.pair_transform(3).unlink()
    with pytest.raises(FileNotFoundError, match=r"pairs 2<-3 are not registered; run register"):
        _qc(cfg, work)
    assert not work.qc_report.exists()


def test_single_slice_gives_an_empty_report(tmp_path):
    cfg, work = _project(tmp_path)
    assert _qc(cfg, work, (2, 2)).done == []
    assert _report(work)["pairs"] == []
