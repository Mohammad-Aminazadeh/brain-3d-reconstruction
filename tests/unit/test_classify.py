"""M7-06: the interim classifier rules-v1 (decision 0006, C13, C14)."""

import json

import numpy as np
import pytest

from brainrec.catalog import DEFAULT_CELL_TYPES
from brainrec.detect.classify import (
    RULE_CLASSES,
    RULES_SCHEMA,
    RULES_V1_PATH,
    RulesV1,
    class_probabilities,
    classify,
    group_weights,
    rule_classes,
    transform,
)
from brainrec.detect.nuclei import Nuclei
from brainrec.store.cells import FEATURES

NAMES = tuple(t.name for t in DEFAULT_CELL_TYPES)
J = {name: j for j, name in enumerate(FEATURES)}


def _model(temperature: float = 1.0) -> RulesV1:
    """8 groups, standardisation = identity; group g sits at 3 g on the darkness axis."""
    centroids = np.zeros((8, len(FEATURES)))
    centroids[:, J["darkness"]] = 3.0 * np.arange(8)
    centroids[:, J["diameter_um"]] = np.log(6.0)
    centroids[:, J["elongation"]] = np.log(1.2)
    centroids[:, J["vessel_um"]] = np.log1p(20.0)
    return RulesV1(np.zeros(9), np.ones(9), centroids, g1_darkness=3.0, g7_nucleolus_q25=0.5,
                   temperature=temperature)  # fmt: skip


def _at(group: int, **set_) -> np.ndarray:
    """Features of a nucleus at the centre of `group` of `_model`, with overrides."""
    f = np.zeros(len(FEATURES))
    f[J["darkness"]] = 3.0 * group
    f[J["diameter_um"]], f[J["elongation"]], f[J["vessel_um"]] = 6.0, 1.2, 20.0
    for k, v in set_.items():
        f[J[k]] = v
    return f


def test_transform_logs_sizes_and_vessel_distance():
    f = _at(0, diameter_um=np.e, elongation=np.e**2, vessel_um=np.e - 1)
    t = transform(f)[0]
    assert t[J["diameter_um"]] == pytest.approx(1) and t[J["elongation"]] == pytest.approx(2)
    assert t[J["vessel_um"]] == pytest.approx(1) and t[J["darkness"]] == 0


def test_group_weights_favour_the_nearest_centre():
    f = np.array([_at(g) for g in range(8)])
    w = group_weights(f, _model())
    assert np.allclose(w.sum(axis=1), 1) and (w.argmax(axis=1) == np.arange(8)).all()
    sharp = group_weights(f, _model(temperature=0.01))
    assert np.allclose(sharp, np.eye(8), atol=1e-6)
    halfway = group_weights(np.array([_at(0, darkness=1.5)]), _model())[0]
    assert halfway[0] == pytest.approx(halfway[1])


def test_rules_of_m7_19():
    m = _model()
    rows = {
        "pale group 1": _at(1, darkness=2.9),
        "dark group 1": _at(1, darkness=3.0),
        "vessel": _at(2, elongation=2.0, vessel_um=4.0),  # lumen within r + 2 µm
        "elongated, no lumen": _at(2, elongation=2.0, vessel_um=5.1),
        "round, by a lumen": _at(4, elongation=1.5, vessel_um=1.0),
        "small plain group 7": _at(7, diameter_um=8.9, nucleolus=0.4),
        "small with nucleolus": _at(7, diameter_um=8.9, nucleolus=0.5),
        "large plain group 7": _at(7, diameter_um=9.0, nucleolus=0.4),
    }
    cls = dict(zip(rows, rule_classes(np.array(list(rows.values())), m), strict=True))
    assert cls["pale group 1"][1] == "Astrocyte-like"
    assert cls["dark group 1"][1] == "Oligodendrocyte-like"
    assert cls["vessel"][2] == cls["vessel"][4] == "Vessel-wall"
    assert cls["elongated, no lumen"][2] == "Microglia-like"
    assert cls["round, by a lumen"][4] == "Microglia-like"
    assert cls["small plain group 7"][7] == "Astrocyte-like"
    assert cls["small with nucleolus"][7] == cls["large plain group 7"][7] == "Neuron"
    fixed = rule_classes(np.array([_at(0)]), m)[0]
    assert (fixed[0], fixed[3], fixed[5], fixed[6]) == (
        "Oligodendrocyte-like", "Oligodendrocyte-like", "Neuron", "Astrocyte-like",
    )  # fmt: skip
    assert "Other" not in rule_classes(np.array(list(rows.values())), m)


def test_probabilities_sum_group_weights_by_class():
    m = _model()
    f = np.array([_at(0, darkness=1.5), _at(5), _at(3, diameter_um=10.5)])
    p = class_probabilities(f, m, NAMES)
    assert p.dtype == np.float32 and p.shape == (3, 6) and np.allclose(p.sum(axis=1), 1)
    w = group_weights(f, m)
    # between groups 0 and 1 (group 1 rule: darkness 1.5 < 3 -> Astrocyte-like)
    assert p[0, NAMES.index("Oligodendrocyte-like")] == pytest.approx(w[0, 0] + w[0, 3], abs=1e-6)
    assert p[0, NAMES.index("Astrocyte-like")] == pytest.approx(w[0, 1] + w[0, 6], abs=1e-6)
    assert p[1].argmax() == NAMES.index("Neuron")
    assert p[2, NAMES.index("Neuron")] == 1.0  # 10.5 µm or more: a neuron
    assert (p[:, NAMES.index("Other")] == 0).all()


def test_column_order_follows_the_type_table():
    f = np.array([_at(g) for g in range(8)])
    names = tuple(reversed(NAMES))
    a, b = class_probabilities(f, _model(), NAMES), class_probabilities(f, _model(), names)
    assert np.array_equal(a, b[:, ::-1])
    with pytest.raises(ValueError, match="Vessel-wall"):
        class_probabilities(f, _model(), tuple(n for n in NAMES if n != "Vessel-wall"))
    assert class_probabilities(np.zeros((0, 9)), _model(), NAMES).shape == (0, 6)


def _nuclei(features: np.ndarray) -> Nuclei:
    n = len(features)
    return Nuclei(np.arange(n, dtype=float), np.zeros(n), np.full(n, 3.0), np.full(n, 28.3),
                  np.arange(n + 1) * 3, np.ones((3 * n, 2)), features, np.zeros(n),
                  np.full(n, np.nan), np.zeros(n))  # fmt: skip


def test_classify_gives_a_valid_c13_table():
    f = np.array([_at(g, nucleolus=g / 7) for g in range(8)] + [_at(0, darkness=1.5)])
    p = classify(_nuclei(f), _model(), NAMES)
    p.validate(len(NAMES))  # class = most probable, confidence = its probability, ids 1..6
    assert p.class_id.min() >= 1 and len(p) == 9
    assert NAMES[p.class_id[5] - 1] == "Neuron"
    empty = classify(_nuclei(np.zeros((0, 9))), _model(), NAMES)
    empty.validate(len(NAMES))
    assert len(empty) == 0


def test_reference_file(tmp_path):
    shipped = RulesV1.load()
    assert shipped.centroids.shape == (8, len(FEATURES)) and (shipped.iqr > 0).all()
    assert shipped.name == "rules-v1" and len(shipped.sha256) == 64
    d = json.loads(RULES_V1_PATH.read_text())
    assert d["schema"] == RULES_SCHEMA and d["features"] == list(FEATURES)
    d["temperature"] = 2.0
    other = tmp_path / "r.json"
    other.write_text(json.dumps(d))
    assert RulesV1.load(other).sha256 != shipped.sha256  # the stage signature notices a change
    d["features"] = d["features"][::-1]
    other.write_text(json.dumps(d))
    with pytest.raises(ValueError, match="reference"):
        RulesV1.load(other)
    assert set(RULE_CLASSES) <= set(NAMES)
