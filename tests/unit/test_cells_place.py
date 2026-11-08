"""M7-07a: placement of cells in the volume frame (C16), and acceptance AC12 on synthetic data."""

import cv2
import numpy as np
import pytest
from scipy.spatial import cKDTree

from brainrec.catalog import DEFAULT_CELL_TYPES, ProjectConfig
from brainrec.cells.place import Placed, VolumeFrame, place_profiles
from brainrec.detect.classify import classify
from brainrec.detect.nuclei import Detector, detect_slice
from brainrec.registration import Transform2D
from tests.synthetic.fake_slide import FakeSlide
from tests.synthetic.nuclei import make_nuclei, segment_by_colour
from tests.unit.test_classify import _model

UM0 = 0.2205
NAMES = tuple(t.name for t in DEFAULT_CELL_TYPES)
SPHERES = np.ones((6, 3))


def _frame(transforms: dict[int, Transform2D], z: float = 5.0) -> VolumeFrame:
    return VolumeFrame(tuple(transforms), (-120.0, 35.0), z, transforms)


def _profiles(n: int = 4, class_id=None):
    from tests.unit.test_classify import _nuclei

    feats = np.zeros((n, 9))
    feats[:, 0] = 6.0
    p = classify(_nuclei(feats), _model(), NAMES)
    p.x_um[:] = np.arange(n) * 10.0
    p.y_um[:] = 5.0
    p.r_um[:] = np.linspace(2, 5, n)
    if class_id is not None:
        p.class_id[:] = class_id
    return p


def test_position_radius_and_z_follow_the_volume_frame():
    t = Transform2D.similarity(np.radians(30), 1.1, 200.0, -50.0)
    frame = _frame({3: Transform2D.identity(), 7: t, 9: Transform2D.identity()})
    p = _profiles()
    placed = place_profiles(p, 7, frame, SPHERES)
    expect = t.apply(np.c_[p.x_um, p.y_um]) - [-120.0, 35.0]
    assert np.allclose(placed.centre[:, :2], expect, atol=1e-3)
    assert np.allclose(placed.centre[:, 2], 7.5)  # second plane of 5 µm: centre at 1.5 x 5
    assert np.allclose(placed.radius_um, p.r_um * 1.1) and np.allclose(
        placed.radii[:, 0], p.r_um * 1.1
    )
    assert placed.centre.dtype == np.float32 and placed.radii.shape == (4, 3)
    assert frame.z_um(3) == 2.5 and frame.z_um(9) == 12.5


def test_cell_ids_classes_and_properties():
    p = _profiles()
    p.nucleolus_seen[1] = 1
    p.blurred[2] = 1
    placed = place_profiles(p, 7, _frame({7: Transform2D.identity()}), SPHERES)
    assert placed.cell_id.tolist() == [(7 << 32) + i for i in range(4)]
    assert (placed.class_id == p.class_id).all() and np.allclose(placed.confidence, p.confidence)
    assert placed.nucleolus.tolist() == [0, 1, 0, 0] and placed.blurred.tolist() == [0, 0, 1, 0]
    assert (placed.n_profiles == 1).all() and (placed.slice_id == 7).all()
    with pytest.raises(KeyError):
        place_profiles(p, 8, _frame({7: Transform2D.identity()}), SPHERES)


def test_class_proportions_shape_the_ellipsoid():
    props = np.ones((6, 3))
    props[3] = (2.0, 1.0, 0.5)  # class id 4
    p = _profiles(class_id=4)
    placed = place_profiles(p, 7, _frame({7: Transform2D.identity()}), props)
    assert np.allclose(placed.radii, p.r_um[:, None] * [2.0, 1.0, 0.5])
    assert np.allclose(placed.radius_um, p.r_um)  # the sphere radius stays a property


def test_concat_and_subset():
    a = place_profiles(_profiles(), 7, _frame({7: Transform2D.identity()}), SPHERES)
    both = Placed.concat([a, a.subset(np.array([True, False, True, False]))])
    assert len(both) == 6 and both.cell_id[4] == a.cell_id[0]


def test_ac12_blobs_sit_on_the_transformed_true_centres():
    """AC12: blobs within 0.88 µm of the transformed true centres, radii within 10%, no centre
    outside the mask, no duplicates at tile borders (detection with the stand-in detector)."""
    syn = make_nuclei(shape=(2048, 2048), seed=11, um_per_px=UM0, tile_px=2 * 422, glass_px=256,
                      n_on_border=8)  # fmt: skip
    slide = FakeSlide.from_image(syn.rgb, 1, (2,), (UM0, UM0))
    mask = np.where(cv2.resize(syn.mask, (64, 64), interpolation=cv2.INTER_AREA) >= 128, 255, 0)
    cfg = ProjectConfig(data_dir=".", work_dir=".", z_spacing_um=5.0, detect_tile_px=512)
    nuclei = detect_slice(slide, mask.astype(np.uint8), 32, cfg,
                          Detector("colour", "test", segment_by_colour))  # fmt: skip
    profiles = classify(nuclei, _model(), NAMES)
    t = Transform2D.similarity(np.radians(-12), 0.97, 340.0, 125.0)
    frame = _frame({4: Transform2D.identity(), 5: t})
    placed = place_profiles(profiles, 5, frame, SPHERES)

    truth = t.apply(np.c_[syn.truth.x_um, syn.truth.y_um]) - [-120.0, 35.0]
    d, j = cKDTree(placed.centre[:, :2]).query(truth)
    assert d.max() <= 0.88 and len(set(j.tolist())) == len(truth) == len(placed)
    assert np.allclose(placed.radius_um[j], syn.truth.r_um * 0.97, rtol=0.10)
    assert np.allclose(placed.centre[:, 2], 7.5)
    assert syn.truth.on_border.sum() >= 4  # nuclei across tile borders are among them, once
    back = t.inverse().apply(placed.centre[:, :2] + [-120.0, 35.0])  # slice frame again
    px = np.floor(back / (32 * UM0)).astype(int)
    assert (mask[px[:, 1], px[:, 0]] > 0).all()  # no centre outside the mask
