import numpy as np
import pytest

from brainrec.detect import CLASSES, Crop, Detections, Labels, evaluate, match

CROP = Crop(id=1, slice_id=3, source="s_3_H.ndpi", x0_um=1000.0, y0_um=2000.0, size_um=250.0,
            split="test")  # fmt: skip
RNG = np.random.default_rng(0)


def _grid(n: int = 36, crop: Crop = CROP) -> np.ndarray:
    """Nucleus centres on a jittered grid well inside the crop (spacing about 35 µm)."""
    side = int(np.sqrt(n))
    g = np.linspace(30, crop.size_um - 30, side)
    xy = np.array([[x, y] for x in g for y in g]) + RNG.uniform(-3, 3, (side * side, 2))
    return xy + [crop.x0_um, crop.y0_um]


def _labels(xy, cls=None, radius=None, crop: Crop = CROP) -> Labels:
    n = len(xy)
    return Labels(
        np.full(n, crop.slice_id), np.full(n, crop.id), xy[:, 0], xy[:, 1],
        np.array(cls if cls is not None else ["Neuron"] * n, dtype=object),
        np.array(radius if radius is not None else [np.nan] * n, float),
    )  # fmt: skip


def _dets(xy, r=3.0, cls=None, slice_id=3) -> Detections:
    n = len(xy)
    return Detections(
        np.full(n, slice_id), xy[:, 0], xy[:, 1], np.full(n, r, float),
        None if cls is None else np.array(cls, dtype=object),
    )  # fmt: skip


def test_match_is_one_to_one_and_minimal():
    truth = np.array([[0.0, 0.0], [10.0, 0.0]])
    dets = np.array([[0.5, 0.0], [1.0, 0.0], [10.2, 0.0]])
    pairs, miss, extra = match(truth, dets, np.full(3, 3.0))
    assert sorted(map(tuple, pairs)) == [(0, 0), (1, 2)]  # closest wins, each used once
    assert list(miss) == [] and list(extra) == [1]


def test_match_tolerance_grows_with_radius():
    truth, det = np.array([[0.0, 0.0]]), np.array([[5.0, 0.0]])
    assert len(match(truth, det, np.array([3.0]))[0]) == 0  # 5 µm > max(3, 1.5)
    assert len(match(truth, det, np.array([12.0]))[0]) == 1  # large nucleus: 5 µm <= 6


def test_match_empty_sets():
    pairs, miss, extra = match(np.empty((0, 2)), np.ones((2, 2)), np.ones(2))
    assert pairs.shape == (0, 2) and list(miss) == [] and list(extra) == [0, 1]


def test_perfect_detections():
    xy = _grid()
    rep = evaluate([CROP], _labels(xy), _dets(xy), CLASSES)
    s = rep["test"]
    assert (s["tp"], s["fp"], s["fn"]) == (36, 0, 0)
    assert s["precision"] == s["recall"] == s["f1"] == 1.0
    assert s["centre_error_um_median"] == 0.0
    assert rep["all"]["tp"] == 36 and rep["train"]["tp"] == 0 and rep["train"]["f1"] is None
    assert "confusion" not in s  # detection only: no classes


def test_small_shift_matches_large_shift_does_not():
    xy = _grid()
    near = evaluate([CROP], _labels(xy), _dets(xy + [1.0, 0.0]), CLASSES)["test"]
    assert near["tp"] == 36 and near["centre_error_um_median"] == pytest.approx(1.0)
    far = evaluate([CROP], _labels(xy), _dets(xy + [6.0, 0.0]), CLASSES)["test"]
    assert (far["tp"], far["fp"], far["fn"]) == (0, 36, 36) and far["f1"] == 0.0


def test_missing_and_extra_detections():
    xy = _grid()
    extra = np.array([[1100.0, 2100.0] + RNG.uniform(-1, 1, 2) for _ in range(1)])
    dets = np.vstack([xy[:27], extra])  # 9 missed, 1 false alarm
    s = evaluate([CROP], _labels(xy), _dets(dets), CLASSES)["test"]
    assert (s["tp"], s["fp"], s["fn"]) == (27, 1, 9)
    assert s["precision"] == pytest.approx(27 / 28) and s["recall"] == pytest.approx(27 / 36)
    assert s["f1"] == pytest.approx(2 * 27 / (2 * 27 + 1 + 9))


def test_detections_of_other_slices_and_outside_the_crop_are_ignored():
    xy = _grid()
    dets = Detections(
        np.concatenate([np.full(36, 3), np.full(36, 4), [3]]),
        np.concatenate([xy[:, 0], xy[:, 0], [900.0]]),
        np.concatenate([xy[:, 1], xy[:, 1], [2100.0]]),
        np.full(73, 3.0),
    )
    s = evaluate([CROP], _labels(xy), dets, CLASSES)["test"]
    assert (s["tp"], s["fp"], s["fn"]) == (36, 0, 0)


def test_unmatched_near_the_border_do_not_count():
    edge_label = np.array([[CROP.x0_um + 1.0, CROP.y0_um + 100.0]])  # 1 µm inside the edge
    edge_det = np.array([[CROP.x0_um + 100.0, CROP.y0_um + CROP.size_um - 2.0]])
    xy = _grid()
    s = evaluate([CROP], _labels(np.vstack([xy, edge_label])), _dets(np.vstack([xy, edge_det])),
                 CLASSES)["test"]  # fmt: skip
    assert (s["tp"], s["fp"], s["fn"]) == (36, 0, 0)
    inner = np.array([[CROP.x0_um + 10.0, CROP.y0_um + 100.0]])  # 10 µm inside: counts
    s = evaluate([CROP], _labels(np.vstack([xy, inner])), _dets(xy), CLASSES)["test"]
    assert s["fn"] == 1


def test_radius_error_uses_outlined_labels_only():
    xy = _grid(4)
    radius = [4.0, 5.0, np.nan, np.nan]
    s = evaluate([CROP], _labels(xy, radius=radius), _dets(xy, r=4.4), CLASSES)["test"]
    assert s["n_radius"] == 2
    assert s["radius_error_median"] == pytest.approx(np.median([0.1, 0.12]))


def test_class_confusion_and_scores():
    xy = _grid(9)
    truth = ["Neuron"] * 3 + ["Astrocyte-like"] * 3 + ["Other"] * 3
    # asymmetric on purpose: a neuron called astrocyte-like, an "other" called neuron
    found = ["Neuron", "Neuron", "Astrocyte-like", "Astrocyte-like", "Astrocyte-like",
             "Astrocyte-like", "Neuron", "Other", "Other"]  # fmt: skip
    s = evaluate([CROP], _labels(xy, truth), _dets(xy, cls=found), CLASSES)["test"]
    cm = s["confusion"]  # rows: labelled class, columns: detected class
    assert cm["Neuron"] == {**dict.fromkeys(CLASSES, 0), "Neuron": 2, "Astrocyte-like": 1}
    assert cm["Astrocyte-like"] == {**dict.fromkeys(CLASSES, 0), "Astrocyte-like": 3}
    assert cm["Other"] == {**dict.fromkeys(CLASSES, 0), "Neuron": 1, "Other": 2}
    assert s["class_accuracy"] == pytest.approx(7 / 9)
    pc = s["per_class"]
    assert (pc["Neuron"]["precision"], pc["Neuron"]["recall"]) == (
        pytest.approx(2 / 3),
        pytest.approx(2 / 3),
    )
    assert (pc["Astrocyte-like"]["precision"], pc["Astrocyte-like"]["recall"]) == (
        pytest.approx(3 / 4),
        1.0,
    )
    assert (pc["Other"]["precision"], pc["Other"]["recall"]) == (1.0, pytest.approx(2 / 3))
    assert pc["Microglia-like"]["support"] == 0 and pc["Neuron"]["support"] == 3
    # macro F1 over the classes that occur
    assert s["class_macro_f1"] == pytest.approx((2 / 3 + 6 / 7 + 0.8) / 3)


def test_splits_and_per_crop_are_kept_apart():
    train = Crop(2, 5, "s_5_H.ndpi", 0.0, 0.0, 250.0, "train")
    a, b = _grid(), _grid(crop=train)
    labels = Labels.concat([_labels(a), _labels(b, crop=train)])
    dets = Detections(
        np.concatenate([np.full(36, 3), np.full(30, 5)]),
        np.concatenate([a[:, 0], b[:30, 0]]), np.concatenate([a[:, 1], b[:30, 1]]),
        np.full(66, 3.0),
    )  # fmt: skip
    rep = evaluate([CROP, train], labels, dets, CLASSES)
    assert rep["test"]["recall"] == 1.0 and rep["train"]["recall"] == pytest.approx(30 / 36)
    assert rep["all"]["tp"] == 66 and rep["all"]["fn"] == 6
    assert [(c["crop"], c["split"], c["tp"]) for c in rep["per_crop"]] == [
        (1, "test", 36),
        (2, "train", 30),
    ]


def test_extra_examples_outside_crops_are_not_scored():
    xy = _grid()
    extras = Labels(np.array([3]), np.array([-1]), np.array([5000.0]), np.array([5000.0]),
                    np.array(["Microglia-like"], dtype=object), np.array([np.nan]))  # fmt: skip
    s = evaluate([CROP], Labels.concat([_labels(xy), extras]), _dets(xy), CLASSES)["test"]
    assert (s["tp"], s["fn"]) == (36, 0)
