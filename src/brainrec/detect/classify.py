"""Classification of detected nuclei by appearance (decision 0006, contracts C13, C14).

`rules-v1` is the interim classifier until M7-11 trains one on expert labels. It is the rule set
of the M7-19 draft labels:
- each nucleus is placed among the 8 appearance groups of M7-18 by its 9 features
  (standardised, distance to the group centres);
- each group maps to a class by fixed rules (`rule_classes`).
The reference (standardisation, group centres, two thresholds) comes from the M7-18 cells
measured again at 2x, as the detection measures them (`scripts/rules_v1_reference.py`, stored
in `rules_v1.json` next to this module).

Probabilities: group membership is soft, with weights exp(-d² / (2 T)) normalised over the
groups (d = distance in the standardised space, T = `temperature`). The probability of a class
is the summed weight of the groups whose rule gives that class. Nuclei of 10.5 µm or more are
neurons with probability 1. "Other" is never given (it is the expert's choice). These
probabilities are not calibrated against labels; the confidence is only a ranking within
rules-v1 (Proposed until M7-11).
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from brainrec.detect.nuclei import Nuclei
from brainrec.store.cells import FEATURES, Profiles

RULES_V1_PATH = Path(__file__).with_name("rules_v1.json")
RULES_SCHEMA = "brainrec.rules/1"
NEURON_MIN_DIAMETER_UM = 10.5  # any nucleus this large is a neuron (M7-19)
SMALL_PLAIN_DIAMETER_UM = 9.0  # group 7 nuclei below this, without a clear nucleolus: astrocyte
VESSEL_MIN_ELONGATION = 1.8
VESSEL_LUMEN_GAP_UM = 2.0  # a lumen within this distance of the nucleus border
RULE_CLASSES = (
    "Neuron",
    "Astrocyte-like",
    "Oligodendrocyte-like",
    "Microglia-like",
    "Vessel-wall",
    "Other",
)


@dataclass(frozen=True)
class RulesV1:
    """Reference of rules-v1: standardisation (median, IQR per transformed feature), the group
    centres (k, 9), the group-1 darkness median, the group-7 nucleolus lower quartile, and the
    temperature of the soft group membership."""

    median: np.ndarray
    iqr: np.ndarray
    centroids: np.ndarray
    g1_darkness: float
    g7_nucleolus_q25: float
    temperature: float
    sha256: str = ""
    name: str = "rules-v1"

    @classmethod
    def load(cls, path: Path = RULES_V1_PATH) -> "RulesV1":
        """The reference from its JSON file. ValueError on another schema or feature list."""
        raw = path.read_bytes()
        d = json.loads(raw)
        if d.get("schema") != RULES_SCHEMA or tuple(d.get("features", ())) != FEATURES:
            raise ValueError(f"{path}: not a {RULES_SCHEMA} reference for the C13 features")
        return cls(
            np.array(d["median"], float), np.array(d["iqr"], float),
            np.array(d["centroids"], float), float(d["g1_darkness"]),
            float(d["g7_nucleolus_q25"]), float(d["temperature"]),
            hashlib.sha256(raw).hexdigest(), d.get("name", "rules-v1"),
        )  # fmt: skip


def transform(features: np.ndarray) -> np.ndarray:
    """Features (n, 9) in FEATURES order -> the space of the reference: log of diameter and
    elongation, log(1 + x) of the vessel distance."""
    t = np.array(features, float, copy=True).reshape(-1, len(FEATURES))
    for j, name in enumerate(FEATURES):
        if name in ("diameter_um", "elongation"):
            t[:, j] = np.log(np.maximum(t[:, j], 1e-6))
        elif name == "vessel_um":
            t[:, j] = np.log1p(np.maximum(t[:, j], 0.0))
    return t


def group_weights(features: np.ndarray, model: RulesV1) -> np.ndarray:
    """(n, k) soft membership of each nucleus in the k appearance groups (rows sum to 1)."""
    z = (transform(features) - model.median) / model.iqr
    d2 = ((z[:, None, :] - model.centroids[None]) ** 2).sum(axis=2)
    logits = -(d2 - d2.min(axis=1, keepdims=True)) / (2 * model.temperature)
    w = np.exp(logits)
    return w / w.sum(axis=1, keepdims=True)


def rule_classes(features: np.ndarray, model: RulesV1) -> np.ndarray:
    """(n, k) class name each group's rule gives each nucleus (the rules of M7-19):
    groups 0, 3 Oligodendrocyte-like; 1 Oligodendrocyte-like if at least as dark as the group's
    median, else Astrocyte-like; 2, 4 Vessel-wall if elongated and next to a lumen, else
    Microglia-like; 5 Neuron; 6 Astrocyte-like; 7 Neuron unless small and without a clear
    nucleolus (Astrocyte-like)."""
    f = np.asarray(features, float).reshape(-1, len(FEATURES))
    col = {name: f[:, j] for j, name in enumerate(FEATURES)}
    n, k = len(f), len(model.centroids)
    if k != 8:
        raise ValueError(f"rules-v1 needs the 8 groups of M7-18, the reference has {k}")
    out = np.empty((n, k), dtype=object)
    lumen = col["vessel_um"] <= col["diameter_um"] / 2 + VESSEL_LUMEN_GAP_UM
    vessel = lumen & (col["elongation"] >= VESSEL_MIN_ELONGATION)
    small_plain = (col["diameter_um"] < SMALL_PLAIN_DIAMETER_UM) & (
        col["nucleolus"] < model.g7_nucleolus_q25
    )
    out[:, 0] = out[:, 3] = "Oligodendrocyte-like"
    out[:, 1] = np.where(col["darkness"] >= model.g1_darkness, "Oligodendrocyte-like",
                         "Astrocyte-like")  # fmt: skip
    out[:, 2] = out[:, 4] = np.where(vessel, "Vessel-wall", "Microglia-like")
    out[:, 5] = "Neuron"
    out[:, 6] = "Astrocyte-like"
    out[:, 7] = np.where(small_plain, "Astrocyte-like", "Neuron")
    return out


def class_probabilities(
    features: np.ndarray, model: RulesV1, class_names: tuple[str, ...]
) -> np.ndarray:
    """(n, len(class_names)) float32 probabilities in the order of `class_names` (C14).
    ValueError if a class of the rules is missing from `class_names`."""
    missing = [c for c in RULE_CLASSES if c not in class_names]
    if missing:
        raise ValueError(f"rules-v1 needs the classes {missing}; the type table lacks them")
    f = np.asarray(features, float).reshape(-1, len(FEATURES))
    prob = np.zeros((len(f), len(class_names)))
    if len(f) == 0:
        return prob.astype(np.float32)
    w = group_weights(f, model)
    cls = rule_classes(f, model)
    index = {c: i for i, c in enumerate(class_names)}
    rows = np.arange(len(f))
    for g in range(w.shape[1]):
        cols = np.array([index[c] for c in cls[:, g]])
        np.add.at(prob, (rows, cols), w[:, g])
    big = f[:, FEATURES.index("diameter_um")] >= NEURON_MIN_DIAMETER_UM
    prob[big] = 0.0
    prob[big, index["Neuron"]] = 1.0
    return prob.astype(np.float32)


def classify(nuclei: Nuclei, model: RulesV1, class_names: tuple[str, ...]) -> Profiles:
    """The C13 table of `nuclei`: class probabilities from `model`, class id = most probable
    class (1-based, C14 order), confidence = its probability (decision 0006)."""
    prob = class_probabilities(nuclei.features, model, class_names)
    class_id = prob.argmax(axis=1) + 1 if len(prob) else np.zeros(0)
    confidence = prob.max(axis=1) if len(prob) else np.zeros(0)
    return Profiles(
        nuclei.x_um, nuclei.y_um, nuclei.r_um, nuclei.area_um2, nuclei.outline_offsets,
        nuclei.outline_xy, nuclei.features, nuclei.nucleolus_seen, nuclei.det_conf,
        nuclei.blurred, class_id, prob, confidence,
    )  # fmt: skip
