import json

import numpy as np
import pytest

from brainrec.detect.labels import (
    CLASSES,
    Crop,
    assign_split,
    candidate_centres,
    crops_geojson,
    far_enough,
    load_crops,
    load_labels,
    read_qupath,
    save_crops,
)

MPP = 0.2205
CROPS = [
    Crop(0, 3, "s_3_H.ndpi", 1000.0, 2000.0, 250.0, "train"),
    Crop(1, 3, "s_3_H.ndpi", 3000.0, 2000.0, 250.0, "test"),
    Crop(2, 4, "s_4_H.ndpi", 1000.0, 2000.0, 250.0, "train"),
]


def _px(x_um, y_um):
    return [x_um / MPP, y_um / MPP]


def _feature(geometry, cls=None, name=None):
    props = {"objectType": "annotation"}
    if cls is not None:
        props["classification"] = {"name": cls, "color": [255, 0, 0]}
    if name is not None:
        props["name"] = name
    return {"type": "Feature", "geometry": geometry, "properties": props}


def test_points_multipoints_and_outlines_are_read_in_um():
    data = {
        "type": "FeatureCollection",
        "features": [
            _feature({"type": "Point", "coordinates": _px(1010.0, 2020.0)}, "Neuron"),
            _feature({"type": "MultiPoint", "coordinates": [_px(1050.0, 2050.0), _px(3100.0, 2100.0)]},
                     "astrocyte-like "),  # case and spaces as typed in QuPath
            _feature({"type": "Polygon", "coordinates": [[_px(1100, 2100), _px(1104, 2100),
                      _px(1104, 2104), _px(1100, 2104), _px(1100, 2100)]]}, "Oligodendrocyte-like"),
        ],
    }  # fmt: skip
    lab = read_qupath(data, 3, MPP, CROPS)
    assert len(lab) == 4
    assert np.allclose(lab.x_um, [1010.0, 1050.0, 3100.0, 1102.0])
    assert np.allclose(lab.y_um, [2020.0, 2050.0, 2100.0, 2102.0])
    assert list(lab.cls) == ["Neuron", "Astrocyte-like", "Astrocyte-like", "Oligodendrocyte-like"]
    assert list(lab.crop) == [0, 0, 1, 0]
    assert np.isnan(lab.radius_um[:3]).all()
    assert lab.radius_um[3] == pytest.approx(np.sqrt(16 / np.pi))  # 4 x 4 µm square
    assert set(lab.slice_id) == {3}


def test_crop_rectangles_are_skipped_and_outside_points_are_extras():
    rects = crops_geojson(CROPS, MPP)["features"]
    extra = _feature({"type": "Point", "coordinates": _px(9000.0, 9000.0)}, "Microglia-like")
    lab = read_qupath(rects + [extra], 3, MPP, CROPS)  # a bare feature list, as also exported
    assert len(lab) == 1 and list(lab.crop) == [-1] and list(lab.cls) == ["Microglia-like"]


def test_crops_of_other_slices_do_not_claim_points():
    point = _feature({"type": "Point", "coordinates": _px(1010.0, 2020.0)}, "Neuron")
    assert list(read_qupath([point], 4, MPP, CROPS).crop) == [2]
    assert list(read_qupath([point], 7, MPP, CROPS).crop) == [-1]


@pytest.mark.parametrize(
    ("features", "message"),
    [
        ([_feature({"type": "Point", "coordinates": [1, 2]})], "1 labels have no class"),
        ([_feature({"type": "MultiPoint", "coordinates": [[1, 2], [3, 4]]}, "Nueron")],
         "unknown classes 'Nueron' (2)"),
        ([_feature({"type": "LineString", "coordinates": [[1, 2], [3, 4]]}, "Neuron")],
         "unsupported geometry 'LineString'"),
    ],
)  # fmt: skip
def test_labelling_slips_are_reported(features, message):
    with pytest.raises(ValueError, match=message.replace("(", r"\(").replace(")", r"\)")):
        read_qupath(features, 3, MPP, CROPS)


def test_crop_rectangles_cover_the_crop_in_level0_pixels():
    (f,) = crops_geojson(CROPS[:1], MPP)["features"]
    ring = np.array(f["geometry"]["coordinates"][0])
    assert np.allclose(ring.min(0) * MPP, [1000.0, 2000.0])
    assert np.allclose(ring.max(0) * MPP, [1250.0, 2250.0])
    assert f["properties"]["name"] == "crop 00" and f["properties"]["isLocked"]
    assert "test" not in json.dumps(crops_geojson(CROPS, MPP))  # split hidden from the labeller


def test_crops_round_trip(tmp_path):
    path = tmp_path / "labels" / "crops.json"
    save_crops(path, CROPS, CLASSES, MPP)
    assert load_crops(path) == (CROPS, CLASSES, MPP)
    assert not list(path.parent.glob("*.tmp"))


def test_load_labels_reads_one_file_per_slide(tmp_path):
    for source, x in (("s_3_H.ndpi", 1010.0), ("s_4_H.ndpi", 1020.0)):
        point = _feature({"type": "Point", "coordinates": _px(x, 2020.0)}, "Neuron")
        (tmp_path / f"{source}.geojson").write_text(json.dumps([point]))
    lab = load_labels(tmp_path, CROPS, CLASSES, MPP)
    assert list(lab.slice_id) == [3, 4] and list(lab.crop) == [0, 2]
    (tmp_path / "s_4_H.ndpi.geojson").unlink()
    with pytest.raises(FileNotFoundError, match="s_4_H.ndpi"):
        load_labels(tmp_path, CROPS, CLASSES, MPP)


def test_candidate_centres_lie_deep_inside_the_tissue():
    mask = np.zeros((100, 100), np.uint8)
    mask[10:90, 10:90] = 255  # 80 px square at 10 µm/px
    pts = candidate_centres(mask, 10.0, min_depth_um=300.0, seed=1)
    # pixels at least 30 px from the edge: 39..60, centres (i + 0.5) x 10 µm (C1)
    assert len(pts) and (pts >= 395).all() and (pts <= 605).all()
    assert np.array_equal(pts, candidate_centres(mask, 10.0, 300.0, seed=1))  # seeded
    assert not np.array_equal(pts, candidate_centres(mask, 10.0, 300.0, seed=2))


def test_far_enough():
    chosen = [np.array([0.0, 0.0])]
    assert far_enough(np.array([0.0, 1000.0]), chosen, 1000.0)
    assert not far_enough(np.array([0.0, 999.0]), chosen, 1000.0)


def test_split_spreads_test_crops_over_the_block():
    slices = [s for s in (1, 15, 30, 45, 60, 75, 90, 105, 120, 140) for _ in range(2)]
    split = assign_split(slices, 6, seed=0)
    test_slices = [s for s, sp in zip(slices, split, strict=True) if sp == "test"]
    assert len(test_slices) == 6 and len(set(test_slices)) == 6  # at most one per slice
    assert test_slices[0] == 1 and test_slices[-1] == 140  # both ends of the block
    assert split == assign_split(slices, 6, seed=0)
    with pytest.raises(ValueError, match="at least 3 slices"):
        assign_split([1, 1, 2, 2], 3, seed=0)
