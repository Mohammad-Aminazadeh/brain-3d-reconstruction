"""M7-16: detection QC on synthetic slides (glass, tissue, a fold) and synthetic cell files."""

import json

import cv2
import numpy as np
import pytest

from brainrec.catalog import DEFAULT_CELL_TYPES, Catalog, ProjectConfig
from brainrec.detect.classify import classify
from brainrec.qc.cells import (
    build_cells_report,
    crop_centres,
    find_folds,
    flag_slices,
    render_crop,
    run_qc_cells,
    slice_stats,
)
from brainrec.store import Manifest, StageExistsError, WorkDir, write_atomic
from brainrec.store.cells import Profiles, save_profiles
from tests.synthetic.fake_slide import FakeSlide
from tests.unit.test_classify import _at, _model, _nuclei

NAMES = tuple(t.name for t in DEFAULT_CELL_TYPES)
MPP = (0.25, 0.25)  # level 0; the 2x level has 0.5 µm/px, the 32x level 8 µm/px
SIDE = 2048  # 2x pixels: 1024 µm
TISSUE = (16, 112)  # 32x pixels, rows and columns: 768 µm of tissue, 0.59 mm²
FOLD = (40, 72)  # 32x pixels: a 256 µm square fold, 0.066 mm²
GLASS, TISSUE_RGB, FOLD_RGB = (240, 240, 240), (200, 185, 215), (150, 120, 175)


def _slide_rgb(fold: bool, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    img = np.empty((SIDE, SIDE, 3), np.float32)
    img[:] = GLASS
    a, b = TISSUE[0] * 16, TISSUE[1] * 16
    img[a:b, a:b] = TISSUE_RGB
    if fold:
        f0, f1 = FOLD[0] * 16, FOLD[1] * 16
        img[f0:f1, f0:f1] = FOLD_RGB
    img += rng.normal(0, 4, img.shape)
    return np.clip(img, 0, 255).astype(np.uint8)


def _mask() -> np.ndarray:
    m = np.zeros((128, 128), np.uint8)
    m[TISSUE[0] : TISSUE[1], TISSUE[0] : TISSUE[1]] = 255
    return m


def _profiles(n: int, seed: int, blurred: bool = False) -> Profiles:
    groups = np.arange(n) % 8  # the same class mix on every slice
    p = classify(_nuclei(np.array([_at(int(g), nucleolus=0.6) for g in groups]).reshape(-1, 9)),
                 _model(), NAMES)  # fmt: skip
    rng = np.random.default_rng(seed)
    p.x_um[:] = rng.uniform(140, 880, n)
    p.y_um[:] = rng.uniform(140, 880, n)
    p.blurred[:] = 1 if blurred else 0
    return p


def _meta(n_tiles: int = 10, n_blurred: int = 2) -> dict:
    return {"source": "x.ndpi", "detector": "test", "detector_version": "1", "downsample": 2,
            "mpp_um": list(MPP), "classifier": "rules-v1", "class_names": list(NAMES),
            "n_blurred_tiles": n_blurred, "tile_sharpness": [50.0] * n_tiles}  # fmt: skip


# slice -> (cells, blurred cells and tiles, fold)
SLICES = {1: (400, False, False), 2: (400, False, True), 3: (400, True, False),
          4: (0, False, False), 5: (400, False, False)}  # fmt: skip


def make_qc_setup(tmp_path, **settings):
    """Five slices: normal, with a fold, blurred, empty, normal. Returns (cfg, work, catalog,
    manifest, factory)."""
    data = tmp_path / "data"
    data.mkdir()
    cfg = ProjectConfig(data_dir=data, work_dir=tmp_path / "work", z_spacing_um=5.0, **settings)
    work = WorkDir(cfg.work_dir)
    images = {}
    for k, (n, blurred, fold) in SLICES.items():
        (data / f"s_{k}_H.ndpi").write_bytes(f"slice {k}".encode())
        images[k] = _slide_rgb(fold, k)
        write_atomic(work.mask(k), cv2.imencode(".png", _mask())[1].tobytes())
        write_atomic(work.reg_meta(k), json.dumps({"downsample": 32.0}).encode())
        meta = _meta(10, 10 if blurred else 2)
        save_profiles(work, k, _profiles(n, k, blurred) if n else Profiles.empty(6), meta)

    def factory(path):
        return FakeSlide.from_image(images[int(path.name.split("_")[1])], 2, (16,), MPP)

    return cfg, work, Catalog.scan(data), Manifest(work.manifest), factory


def test_find_folds_marks_the_dark_doubled_tissue_only():
    rgb = cv2.resize(_slide_rgb(True, 0), (128, 128), interpolation=cv2.INTER_AREA)
    folds = find_folds(rgb, _mask(), 8.0, 2.0, 0.05)
    expect = np.zeros((128, 128), bool)
    expect[FOLD[0] : FOLD[1], FOLD[0] : FOLD[1]] = True
    assert (folds == expect).mean() > 0.995 and folds[50:60, 50:60].all()
    plain = cv2.resize(_slide_rgb(False, 0), (128, 128), interpolation=cv2.INTER_AREA)
    assert not find_folds(plain, _mask(), 8.0, 2.0, 0.05).any()
    assert not find_folds(rgb, _mask(), 8.0, 2.0, 0.1).any()  # the fold is smaller than this
    assert not find_folds(rgb, _mask(), 8.0, 3.0, 0.05).any()  # and not three times as dark
    assert not find_folds(rgb, np.zeros((128, 128), np.uint8), 8.0, 2.0, 0.05).any()
    with pytest.raises(ValueError, match="do not match"):
        find_folds(rgb, np.zeros((64, 64), np.uint8), 8.0, 2.0, 0.05)


def test_slice_stats_counts_densities_and_cells_in_folds():
    p = _profiles(400, 1)
    folds = np.zeros((128, 128), bool)
    folds[FOLD[0] : FOLD[1], FOLD[0] : FOLD[1]] = True
    s = slice_stats(p, _meta(10, 3), _mask(), folds, 8.0)
    assert s["tissue_mm2"] == pytest.approx(96 * 96 * 64 / 1e6, abs=1e-3)
    assert s["density_mm2"] == pytest.approx(400 / s["tissue_mm2"], rel=1e-3)
    inside = ((p.x_um >= 320) & (p.x_um < 576) & (p.y_um >= 320) & (p.y_um < 576)).sum()
    assert s["cells_in_folds"] == inside > 0
    assert s["fold_mm2"] == pytest.approx(32 * 32 * 64 / 1e6, abs=1e-3) and s["n_fold_regions"] == 1
    assert s["blurred_tile_share"] == 0.3 and s["blurred_cell_share"] == 0.0
    assert sum(c["n"] for c in s["classes"].values()) == 400
    assert s["classes"]["Neuron"]["share"] == pytest.approx((p.class_id == 1).mean(), abs=1e-4)
    empty = slice_stats(Profiles.empty(6), _meta(0, 0), np.zeros((8, 8), np.uint8),
                        np.zeros((8, 8), bool), 8.0)  # fmt: skip
    assert empty["density_mm2"] == 0 and empty["blurred_tile_share"] == 0


def _row(k: int, density=1000.0, blurred=0.2, fold=0.0, shares=(0.2, 0.35, 0.3, 0.06, 0.08, 0.01)):
    n = int(density) if density else 0
    return {"slice_id": k, "n_profiles": n, "density_mm2": density, "blurred_tile_share": blurred,
            "fold_mm2": fold * 400, "fold_share": fold,
            "classes": {name: {"share": s} for name, s in zip(NAMES, shares, strict=True)}}  # fmt: skip


def test_flags_against_the_neighbours():
    cfg = ProjectConfig(data_dir=".", work_dir=".", z_spacing_um=5.0)
    rows = [_row(1), _row(2, density=1300), _row(3, density=600), _row(4, blurred=0.6),
            _row(5, fold=0.012), _row(6, density=0), _row(7, fold=0.009),
            _row(8, shares=(0.2, 0.35, 0.3, 0.06, 0.08, 0.019)),  # rare class: not compared
            _row(9, shares=(0.31, 0.29, 0.25, 0.06, 0.08, 0.01)), _row(10)]  # fmt: skip
    out = {r["slice_id"]: r for r in flag_slices(rows, cfg)}
    assert {k: r["flags"] for k, r in out.items()} == {
        1: [], 2: [], 3: ["density_outlier"], 4: ["blurred"], 5: ["folds"],
        6: ["empty", "density_outlier"], 7: [], 8: [], 9: ["class_outlier"], 10: [],
    }  # fmt: skip
    assert out[3]["neighbour_density_mm2"] == 1000 and out[3]["density_ratio"] == 0.6
    assert out[2]["density_ratio"] == pytest.approx(1.3)  # below 1.33: not flagged
    assert out[9]["class_ratios"]["Neuron"] == 1.55 and "Other" not in out[9]["class_ratios"]
    assert out[6]["class_ratios"] == {}
    # the empty slice 6 is not a neighbour: slice 7 compares with 5, 8 and 9
    assert out[7]["neighbour_density_mm2"] == 1000
    single = flag_slices([_row(1)], cfg)[0]
    assert single["density_ratio"] is None and single["flags"] == []


def test_report_lists_flagged_slices_and_thresholds():
    cfg = ProjectConfig(data_dir=".", work_dir=".", z_spacing_um=5.0, qc_cells_density_ratio=1.2)
    report = build_cells_report([_row(1), _row(2, density=1250), _row(3)], cfg)
    assert report["schema"] == "brainrec.cellsqc/1" and report["slice_ids"] == [1, 2, 3]
    assert report["flagged"] == [2] and report["thresholds"]["qc_cells_density_ratio"] == 1.2
    assert report["thresholds"]["qc_fold_max_share"] == 0.01


COLOUR = (255, 0, 255)


def _ring(x_um: float, y_um: float) -> Profiles:
    """One profile with a circular outline of radius 5 µm."""
    p = _profiles(1, 0)
    p.x_um[:], p.y_um[:] = x_um, y_um
    a = np.linspace(0, 2 * np.pi, 16, endpoint=False)
    p.outline_offsets = np.array([0, 16], np.int64)
    p.outline_xy = np.round(np.c_[np.cos(a), np.sin(a)] * 5.0 / 0.1).astype(np.int16)
    return p


def _drawn(img: np.ndarray, centre_yx) -> tuple[np.ndarray, float]:
    """Mean position (row, column) of the outline pixels, weighted by how strongly they were
    drawn, and their median distance from `centre_yx`."""
    w = np.clip(1 - np.abs(img.astype(float) - COLOUR).sum(2) / 382, 0, None)
    w[w < 0.2] = 0
    yy, xx = np.nonzero(w)
    yx = np.c_[yy, xx].astype(float)
    mean = (yx * w[yy, xx, None]).sum(0) / w[yy, xx].sum()
    return mean, float(np.median(np.hypot(*(yx - centre_yx).T)))


def test_crop_draws_outlines_in_class_colour_and_fold_boundary():
    slide = FakeSlide.from_image(np.full((1200, 1600, 3), 128, np.uint8), 2, (16,), MPP)
    img = render_crop(slide, 0, 0.5, (600.0, 300.0), _ring(600.0, 300.0), [COLOUR] * 6,
                      np.zeros((10, 10), bool), 8.0)  # fmt: skip
    assert img.shape == (512, 512, 3)
    # C1: the centre 600 µm is level px 1199.5; the crop starts at round(1199.5 - 256) = 944
    mean, radius = _drawn(img, (599.5 - 344, 1199.5 - 944))
    assert np.allclose(mean, (255.5, 255.5), atol=0.2) and radius == pytest.approx(10.0, abs=1.0)
    # near the corner the crop is clipped into the level; the fold edge is drawn in black
    folds = np.zeros((80, 100), bool)
    folds[:, 25:] = True  # fold from x = 200 µm on (8 µm pixels)
    img = render_crop(slide, 0, 0.5, (100.0, 80.0), _ring(100.0, 80.0), [COLOUR] * 6, folds, 8.0)
    mean, _ = _drawn(img, (159.5, 199.5))
    assert np.allclose(mean, (159.5, 199.5), atol=0.2)
    black = np.nonzero((img[300] == 0).all(1))[0]  # one row: the fold edge at 200 µm = px 400
    assert black.min() >= 398 and np.isin([399, 400], black).any()


def test_crop_centres_pick_blur_folds_and_sharp_cells():
    p = _profiles(400, 3)
    p.blurred[:] = 0
    hot = (p.x_um < 300) & (p.y_um < 300)
    p.blurred[hot] = 1
    p.blurred[np.argmax(p.x_um + p.y_um)] = 1  # one lone blurred cell far away: not chosen
    folds = np.zeros((128, 128), bool)
    folds[FOLD[0] : FOLD[1], FOLD[0] : FOLD[1]] = True
    got = crop_centres(p, _mask(), folds, 8.0, 256.0, seed=3)
    kinds = [k for k, _, _ in got]
    assert kinds == ["blurred", "blurred", "fold", "sharp", "sharp"]  # one fold region only
    assert all(x < 300 and y < 300 for _, x, y in got[:2])
    assert 320 <= got[2][1] < 576 and 320 <= got[2][2] < 576
    assert crop_centres(p, _mask(), folds, 8.0, 256.0, 3) == got  # reproducible
    empty = crop_centres(Profiles.empty(6), _mask(), np.zeros((128, 128), bool), 8.0, 256.0, 1)
    assert [k for k, _, _ in empty] == ["tissue", "tissue"]


def test_blurred_empty_and_folded_slices_are_flagged(tmp_path):
    """Acceptance of M7-16: a synthetic blurred or empty slice is flagged (and a fold)."""
    cfg, work, catalog, manifest, factory = make_qc_setup(tmp_path)
    result = run_qc_cells(catalog, work, cfg, manifest, reader_factory=factory)
    assert result.done == [1, 2, 3, 4, 5]
    report = json.loads(work.qc_cells_report.read_text())
    flags = {r["slice_id"]: r["flags"] for r in report["slices"]}
    assert flags == {1: [], 2: ["folds"], 3: ["blurred"], 4: ["empty", "density_outlier"], 5: []}
    assert report["flagged"] == [2, 3, 4]
    rows = {r["slice_id"]: r for r in report["slices"]}
    assert (
        rows[2]["fold_share"] == pytest.approx(0.111, abs=0.005) and rows[2]["cells_in_folds"] > 0
    )
    assert rows[1]["fold_mm2"] == 0 and rows[3]["blurred_cell_share"] == 1.0
    for k in SLICES:
        img = cv2.imread(str(work.qc_cells_crops(k)))
        assert img is not None and img.shape[1] == 3 * 512
    assert cv2.imread(str(work.qc_cells_crops(2))).shape[0] == 512 + 22  # 1 fold + 2 sharp
    # slice 3: 2 blurred + 2 tissue crops (no sharp cells)
    assert cv2.imread(str(work.qc_cells_crops(3))).shape[0] == 2 * (512 + 22)


def test_resume_thresholds_and_settings(tmp_path):
    cfg, work, catalog, manifest, factory = make_qc_setup(tmp_path)
    run_qc_cells(catalog, work, cfg, manifest, reader_factory=factory)
    assert run_qc_cells(catalog, work, cfg, manifest, reader_factory=factory).skipped == [1, 2, 3,
                                                                                          4, 5]  # fmt: skip
    save_profiles(work, 5, _profiles(390, 5), _meta())  # detection redone for slice 5
    assert run_qc_cells(catalog, work, cfg, manifest, reader_factory=factory).done == [5]
    work.qc_cells_crops(1).unlink()
    assert run_qc_cells(catalog, work, cfg, manifest, reader_factory=factory).done == [1]
    strict = cfg.model_copy(update={"qc_fold_max_share": 0.2})  # flags only: nothing redone
    assert run_qc_cells(catalog, work, strict, manifest, reader_factory=factory).done == []
    assert json.loads(work.qc_cells_report.read_text())["flagged"] == [3, 4]
    other = cfg.model_copy(update={"qc_fold_od_ratio": 2.5})
    with pytest.raises(StageExistsError, match="qc_fold_od_ratio"):
        run_qc_cells(catalog, work, other, manifest, reader_factory=factory)
    redo = run_qc_cells(catalog, work, other, manifest, reader_factory=factory, overwrite=True)
    assert redo.done == [1, 2, 3, 4, 5]


def test_missing_inputs_are_named(tmp_path):
    cfg, work, catalog, manifest, factory = make_qc_setup(tmp_path)
    work.cell_meta(3).unlink()
    with pytest.raises(FileNotFoundError, match=r"slices \[3\] have no cell file"):
        run_qc_cells(catalog, work, cfg, manifest, reader_factory=factory)
    save_profiles(work, 3, _profiles(10, 3), _meta())
    work.mask(4).unlink()
    with pytest.raises(FileNotFoundError, match=r"slices \[4\] have no tissue mask"):
        run_qc_cells(catalog, work, cfg, manifest, reader_factory=factory)


def test_config_rejects_impossible_thresholds():
    with pytest.raises(ValueError):
        ProjectConfig(data_dir=".", work_dir=".", z_spacing_um=5.0, qc_cells_density_ratio=0.8)
    with pytest.raises(ValueError):
        ProjectConfig(data_dir=".", work_dir=".", z_spacing_um=5.0, qc_fold_max_share=1.5)
