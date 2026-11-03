import itertools
import json
import math

import cv2
import numpy as np
import pytest

import brainrec.registration.chain as chain_mod
from brainrec.catalog import Catalog, ProjectConfig
from brainrec.preprocess import run_preprocess
from brainrec.registration import PairResult, Transform2D
from brainrec.registration.chain import (
    RegistrationFailedError,
    chain,
    load_chained,
    run_register,
)
from brainrec.store import Manifest, StageExistsError, WorkDir, write_atomic
from tests.synthetic.fake_slide import FakeSlide
from tests.synthetic.tissue import UM_PER_PX, apply_similarity, make_tissue, perturb

MPP = (0.2205, 0.2205)
# per slice: motion of the section on its slide (angle °, scale, tx µm, ty µm)
MOTIONS = {
    1: (0.0, 1.0, 0.0, 0.0),
    2: (6.0, 1.03, 120.0, -60.0),
    3: (-4.0, 0.98, -90.0, 150.0),
    4: (10.0, 1.05, 200.0, 40.0),
}


def test_chain_composes_in_the_documented_order():
    p = {k: Transform2D.similarity(0.1 * k, 1 + 0.01 * k, 10.0 * k, -5.0 * k) for k in (2, 3, 4)}
    c = chain(p, [1, 2, 3, 4])
    assert c[1].allclose(Transform2D.identity())
    assert c[4].allclose(p[2].compose(p[3]).compose(p[4]))  # T(4->1) = T(2->1) ∘ T(3->2) ∘ T(4->3)
    pts = np.array([[100.0, 200.0]])
    assert np.allclose(c[3].apply(pts), p[2].apply(p[3].apply(pts)))
    with pytest.raises(KeyError, match="slice 3"):
        chain({2: p[2]}, [1, 2, 3])


def _project(tmp_path, motions=MOTIONS, unrelated=()):
    """Slices of one synthetic section, each moved on its slide by a known similarity."""
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    base = make_tissue(seed=5)
    images, truths = {}, {}
    for k, (a, s, tx, ty) in motions.items():
        (data / f"s_{k}_H.ndpi").write_bytes(f"slice {k}".encode())
        if k in unrelated:  # a clearly different section
            other = make_tissue(seed=9)
            img = np.full_like(other.rgb, 236)
            img[160:640] = cv2.resize(other.rgb, (1000, 480))
            images[k] = img
            continue
        moved, mm, truth = apply_similarity(
            base.rgb, base.mask, a, s, tx, ty
        )  # truth: moved -> base
        images[k], truths[k] = perturb(moved, mm, seed=k), truth
    cfg = ProjectConfig(data_dir=data, work_dir=tmp_path / "work", z_spacing_um=5.0)

    def factory(path):
        return FakeSlide.from_image(images[int(path.name.split("_")[1])], 32, (2,), MPP)

    return cfg, factory, truths, base.mask


def _run(cfg, factory, ids, **kw):
    work = WorkDir(cfg.work_dir)
    cat = Catalog.scan(cfg.data_dir, ids)
    run_preprocess(cat, work, cfg, Manifest(work.manifest), factory)
    return run_register(cat, work, cfg, Manifest(work.manifest), **kw), work


@pytest.fixture
def calls(monkeypatch):
    seen = []
    real = chain_mod.register_pair

    def spy(fixed, moving, cfg):
        seen.append(moving.slice_id)
        return real(fixed, moving, cfg)

    monkeypatch.setattr(chain_mod, "register_pair", spy)
    return seen


def _error_px(est: Transform2D, truth: Transform2D, mask: np.ndarray) -> float:
    ys, xs = np.nonzero(mask)
    pts = np.stack([xs, ys], 1)[::40] * UM_PER_PX
    return float(np.linalg.norm(est.apply(pts) - truth.apply(pts), axis=1).max() / UM_PER_PX)


def test_register_writes_pairs_and_chain(tmp_path, calls):
    cfg, factory, truths, mask = _project(tmp_path)
    result, work = _run(cfg, factory, (1, 4))
    assert result.done == [2, 3, 4] and calls == [2, 3, 4]
    for k in (2, 3, 4):
        pr = PairResult.from_dict(json.loads(work.pair_transform(k).read_text()))
        assert pr.fixed == k - 1 and pr.moving == k and pr.status == "features"
        pair_truth = truths[k - 1].inverse().compose(truths[k])  # k -> k-1
        assert _error_px(pr.transform, pair_truth, mask) < 1.0
        chained = Transform2D.load(work.chained_transform(k))
        chain_truth = truths[1].inverse().compose(truths[k])  # k -> reference (slice 1)
        assert _error_px(chained, chain_truth, mask) < 1.5 * (k - 1)  # errors may add up
    assert Transform2D.load(work.chained_transform(1)).allclose(Transform2D.identity())
    assert math.isclose(Transform2D.load(work.chained_transform(4)).scale, 1 / 1.05, rel_tol=0.005)


def test_resume_and_extend(tmp_path, calls):
    cfg, factory, _, _ = _project(tmp_path)
    _run(cfg, factory, (1, 3))
    again, _ = _run(cfg, factory, (1, 3))
    assert again.done == [] and again.skipped == [2, 3]
    wider, work = _run(cfg, factory, (1, 4))
    assert wider.done == [4] and wider.skipped == [2, 3] and calls == [2, 3, 4]
    assert work.chained_transform(4).exists()


def test_new_reference_rechains_without_reregistering(tmp_path, calls):
    cfg, factory, truths, mask = _project(tmp_path)
    _run(cfg, factory, (1, 4))
    _, work = _run(cfg, factory, (2, 4))  # same pairs 3 and 4, reference now slice 2
    assert calls == [2, 3, 4]
    assert Transform2D.load(work.chained_transform(2)).allclose(Transform2D.identity())
    truth = truths[2].inverse().compose(truths[4])
    assert _error_px(Transform2D.load(work.chained_transform(4)), truth, mask) < 3.0


def test_upstream_change_redoes_affected_pairs(tmp_path, calls):
    cfg, factory, _, _ = _project(tmp_path)
    _run(cfg, factory, (1, 3))
    (cfg.data_dir / "s_3_H.ndpi").write_bytes(b"slice 3, rescanned")  # new source file
    r, _ = _run(cfg, factory, (1, 3))
    assert r.done == [3] and r.skipped == [2] and calls == [2, 3, 3]


def test_changed_settings_need_overwrite(tmp_path):
    cfg, factory, _, _ = _project(tmp_path, {k: MOTIONS[k] for k in (1, 2)})
    _run(cfg, factory, (1, 2))
    changed = cfg.model_copy(update={"reg_min_inliers": 60})
    with pytest.raises(StageExistsError, match="other settings"):
        _run(changed, factory, (1, 2))
    r, _ = _run(changed, factory, (1, 2), overwrite=True)
    assert r.done == [2]


def test_failed_pair_stops_the_chain(tmp_path):
    cfg, factory, _, _ = _project(tmp_path, unrelated=(3,))
    work = WorkDir(cfg.work_dir)
    stale = work.chained_transform(4)
    stale.parent.mkdir(parents=True, exist_ok=True)
    Transform2D.identity().save(stale)  # left over from an earlier run
    with pytest.raises(RegistrationFailedError, match="2<-3"):
        _run(cfg, factory, (1, 4))
    assert PairResult.from_dict(json.loads(work.pair_transform(3).read_text())).status == "failed"
    assert work.chained_transform(2).exists()
    assert not work.chained_transform(3).exists() and not stale.exists()
    assert not work.pair_transform(4).exists()  # stopped at the failure
    with pytest.raises(RegistrationFailedError):  # a rerun reports it again, without recomputing
        run_register(Catalog.scan(cfg.data_dir, (1, 4)), work, cfg, Manifest(work.manifest))


def _write_run(work, ids):
    """Pair and chained files as one register run over `ids` leaves them (no images needed)."""
    pairs = {k: Transform2D.similarity(0.01 * k, 1.0, 5.0 * k, -3.0 * k) for k in ids[1:]}
    for (prev, k), t in zip(itertools.pairwise(ids), pairs.values(), strict=True):
        pr = PairResult(prev, k, t, "features", 100, 80, 15.0, 0.95)
        write_atomic(work.pair_transform(k), json.dumps(pr.to_dict()).encode())
    for k, t in chain(pairs, ids).items():
        t.save(work.chained_transform(k))


def test_load_chained_returns_one_chain(tmp_path):
    work = WorkDir(tmp_path)
    _write_run(work, [1, 2, 3, 4])
    for ids in ([1, 2, 3, 4], [2, 3], [4]):  # any part of one run
        got = load_chained(work, ids)
        assert list(got) == ids
        assert all(got[k].allclose(Transform2D.load(work.chained_transform(k))) for k in ids)


def test_load_chained_names_unregistered_slices(tmp_path):
    work = WorkDir(tmp_path)
    _write_run(work, [1, 2])
    with pytest.raises(FileNotFoundError, match=r"slices \[3, 4\]"):
        load_chained(work, [1, 2, 3, 4])


@pytest.mark.parametrize(
    ("runs", "ids", "seam"),
    [
        ([[1, 2, 3, 4], [3, 4]], [1, 2, 3, 4], "2 and 3"),  # second run: reference slice 3
        ([[1, 2], [3, 4]], [1, 2, 3, 4], "2 and 3"),  # no pair between the runs
        ([[1, 2, 3, 4], [2, 3, 4]], [1, 2], "1 and 2"),
    ],
)
def test_load_chained_refuses_separate_runs(tmp_path, runs, ids, seam):
    work = WorkDir(tmp_path)
    for run in runs:
        _write_run(work, run)
    with pytest.raises(ValueError, match=f"slices {seam} were not registered in the same run"):
        load_chained(work, ids)


def test_missing_preprocessing_is_reported(tmp_path):
    cfg, _, _, _ = _project(tmp_path, {k: MOTIONS[k] for k in (1, 2)})
    work = WorkDir(cfg.work_dir)
    with pytest.raises(FileNotFoundError, match="preprocess"):
        run_register(Catalog.scan(cfg.data_dir, (1, 2)), work, cfg, Manifest(work.manifest))
