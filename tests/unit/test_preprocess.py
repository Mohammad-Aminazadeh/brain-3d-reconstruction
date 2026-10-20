import json

import cv2
import numpy as np
import pytest

from brainrec.catalog import Catalog, ProjectConfig
from brainrec.preprocess import make_mask, make_reg_image, preprocess_slice, run_preprocess
from brainrec.registration import RegImage
from brainrec.store import Manifest, StageExistsError, WorkDir
from tests.synthetic.fake_slide import FakeSlide
from tests.synthetic.tissue import UM_PER_PX, make_tissue

MPP = (0.2205, 0.2205)


@pytest.fixture(scope="module")
def sample():
    return make_tissue(seed=6)


def test_reg_image_is_inverted_and_zero_outside(sample):
    img = make_reg_image(sample.rgb, sample.mask)
    assert img.shape == sample.mask.shape and img.dtype == np.uint8
    assert (img[sample.mask == 0] == 0).all()
    gray = cv2.cvtColor(sample.rgb, cv2.COLOR_RGB2GRAY).astype(float)
    inside = sample.mask > 0
    assert np.corrcoef(gray[inside], img[inside].astype(float))[0, 1] < -0.5  # dark -> bright


def test_reg_image_ignores_the_glass(sample):
    other = sample.rgb.copy()
    other[sample.mask == 0] = (40, 30, 60)  # very different glass and debris
    a, b = make_reg_image(sample.rgb, sample.mask), make_reg_image(other, sample.mask)
    assert np.array_equal(a, b)


def test_reg_image_rejects_mismatched_shapes(sample):
    with pytest.raises(ValueError, match="match"):
        make_reg_image(sample.rgb, sample.mask[:-1])


class SpySlide(FakeSlide):
    """FakeSlide that records which levels were read."""

    def read_region(self, level, x, y, w, h):
        self.levels_read = [*getattr(self, "levels_read", []), level]
        return super().read_region(level, x, y, w, h)


def _spy(rgb):
    s = FakeSlide.from_image(rgb, 32, (2,), MPP)
    s.__class__ = SpySlide
    return s


def _cfg(tmp_path, **kw):
    return ProjectConfig(
        data_dir=tmp_path / "data", work_dir=tmp_path / "work", z_spacing_um=5.0, **kw
    )


def test_preprocess_slice_writes_the_c4_files(tmp_path, sample):
    cfg = _cfg(tmp_path)
    work = WorkDir(cfg.work_dir)
    slide = _spy(sample.rgb)
    ref = type("Ref", (), {"id": 7, "path": tmp_path / "x_7_H.ndpi"})()
    reg = preprocess_slice(ref, work, cfg, lambda p: slide)
    assert slide.levels_read == [slide.level_for_downsample(32)]  # one read, 32x level only
    meta = json.loads(work.reg_meta(7).read_text())
    assert meta == {
        "schema": "brainrec.regimage/1", "slice_id": 7, "source": "x_7_H.ndpi",
        "downsample": 32.0, "mpp_um": [0.2205, 0.2205], "width": 1000, "height": 800,
    }  # fmt: skip
    assert np.array_equal(reg.mask, make_mask(sample.rgb, UM_PER_PX, cfg))
    assert np.array_equal(reg.image, make_reg_image(sample.rgb, reg.mask))
    loaded = RegImage.load(work, 7)
    assert np.array_equal(loaded.image, reg.image) and np.array_equal(loaded.mask, reg.mask)
    assert loaded.downsample == 32.0 and loaded.mpp_um == MPP
    assert not list(work.root.rglob("*.tmp"))  # atomic writes leave no temporary files


def test_load_rejects_bad_metadata(tmp_path, sample):
    cfg = _cfg(tmp_path)
    work = WorkDir(cfg.work_dir)
    ref = type("Ref", (), {"id": 3, "path": tmp_path / "x_3_H.ndpi"})()
    preprocess_slice(ref, work, cfg, lambda p: FakeSlide.from_image(sample.rgb, 32, (2,), MPP))
    meta = json.loads(work.reg_meta(3).read_text())
    work.reg_meta(3).write_text(json.dumps({**meta, "width": 999}))
    with pytest.raises(ValueError, match="sizes"):
        RegImage.load(work, 3)
    work.reg_meta(3).write_text(json.dumps({**meta, "schema": "other"}))
    with pytest.raises(ValueError, match="schema"):
        RegImage.load(work, 3)


def _project(tmp_path, ids):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    for i in ids:
        (data / f"s_{i}_H.ndpi").write_bytes(b"x")
    images = {i: make_tissue(seed=i).rgb for i in ids}
    opened = []

    def factory(path):
        sid = int(path.name.split("_")[1])
        s = FakeSlide.from_image(images[sid], 32, (2,), MPP)
        opened.append(sid)
        return s

    return factory, opened


def test_run_preprocess_resumes_and_extends(tmp_path):
    cfg = _cfg(tmp_path)
    work, manifest = WorkDir(cfg.work_dir), Manifest(WorkDir(cfg.work_dir).manifest)
    factory, opened = _project(tmp_path, (1, 2, 3))
    first = run_preprocess(Catalog.scan(cfg.data_dir, (1, 2)), work, cfg, manifest, factory)
    assert first.done == [1, 2] and first.skipped == []
    again = run_preprocess(
        Catalog.scan(cfg.data_dir, (1, 2)), work, cfg, Manifest(work.manifest), factory
    )
    assert again.done == [] and again.skipped == [1, 2] and opened == [1, 2]  # no reads
    wider = run_preprocess(
        Catalog.scan(cfg.data_dir, (1, 3)), work, cfg, Manifest(work.manifest), factory
    )
    assert wider.done == [3] and wider.skipped == [1, 2]  # extending the range keeps done slices
    assert Manifest(work.manifest).get("preprocess")["done"] == [1, 2, 3]


def test_run_preprocess_redoes_a_changed_source_or_missing_file(tmp_path):
    cfg = _cfg(tmp_path)
    work = WorkDir(cfg.work_dir)
    factory, _ = _project(tmp_path, (1, 2))
    cat = Catalog.scan(cfg.data_dir, (1, 2))
    run_preprocess(cat, work, cfg, Manifest(work.manifest), factory)
    (cfg.data_dir / "s_1_H.ndpi").write_bytes(b"changed source")
    work.mask(2).unlink()
    r = run_preprocess(cat, work, cfg, Manifest(work.manifest), factory)
    assert r.done == [1, 2] and r.skipped == []


def test_changed_settings_need_overwrite(tmp_path):
    cfg = _cfg(tmp_path)
    work = WorkDir(cfg.work_dir)
    factory, _ = _project(tmp_path, (1,))
    cat = Catalog.scan(cfg.data_dir, (1, 1))
    run_preprocess(cat, work, cfg, Manifest(work.manifest), factory)
    changed = _cfg(tmp_path, mask_min_component_mm2=0.5)
    with pytest.raises(StageExistsError, match="other settings"):
        run_preprocess(cat, work, changed, Manifest(work.manifest), factory)
    r = run_preprocess(cat, work, changed, Manifest(work.manifest), factory, overwrite=True)
    assert r.done == [1]
    assert Manifest(work.manifest).get("preprocess")["signature"]["mask_min_component_mm2"] == 0.5
