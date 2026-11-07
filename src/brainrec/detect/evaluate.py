"""Accuracy of detected and classified nuclei against labelled crops (M7-02; gates of AC10).

Per crop, detections and labels are matched one-to-one (Hungarian assignment on centre
distance). A pair counts if its centres lie within max(`tol_um`, `rel` x detected radius).
Unmatched labels are misses, unmatched detections false alarms, except within `border_um` of
the crop edge, where a nucleus cut by the edge may be labelled on one side only.
"""

from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy.optimize import linear_sum_assignment

from brainrec.detect.labels import Crop, Labels

SPLITS = ("train", "test", "all")


@dataclass
class Detections:
    """Detected nuclei: centre (µm, slice frame C1), radius (µm) and class name per detection;
    `cls` is None for detection-only results (before classification)."""

    slice_id: np.ndarray
    x_um: np.ndarray
    y_um: np.ndarray
    radius_um: np.ndarray
    cls: np.ndarray | None = None


def match(
    truth_xy: np.ndarray,
    det_xy: np.ndarray,
    det_r: np.ndarray,
    tol_um: float = 3.0,
    rel: float = 0.5,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """One-to-one matching of label centres (n, 2) and detection centres (m, 2), µm.

    Returns (pairs (k, 2) of [truth index, detection index], unmatched truth indices,
    unmatched detection indices). The total distance of the accepted pairs is minimal.
    """
    n, m = len(truth_xy), len(det_xy)
    if n == 0 or m == 0:
        return np.empty((0, 2), int), np.arange(n), np.arange(m)
    d = np.linalg.norm(truth_xy[:, None, :] - det_xy[None, :, :], axis=2)
    limit = np.maximum(tol_um, rel * det_r)[None, :]
    allowed = d <= limit
    cost = np.where(allowed, d, 1e9)
    r, c = linear_sum_assignment(cost)
    ok = allowed[r, c]
    pairs = np.stack([r[ok], c[ok]], axis=1)
    return (
        pairs,
        np.setdiff1d(np.arange(n), pairs[:, 0]),
        np.setdiff1d(np.arange(m), pairs[:, 1]),
    )


def _ratio(a: float, b: float) -> float | None:
    return a / b if b else None


def _f1(p: float | None, r: float | None) -> float | None:
    if p is None or r is None:
        return None  # nothing labelled or nothing detected: undefined
    return 2 * p * r / (p + r) if p + r else 0.0


class _Tally:
    def __init__(self, classes: tuple[str, ...]) -> None:
        self.tp = self.fp = self.fn = 0
        self.centre: list[float] = []
        self.radius: list[float] = []
        self.classes = classes
        self.confusion = np.zeros((len(classes), len(classes)), int)  # [truth, detected]

    def report(self, classified: bool) -> dict[str, Any]:
        p, r = _ratio(self.tp, self.tp + self.fp), _ratio(self.tp, self.tp + self.fn)
        out: dict[str, Any] = {
            "tp": self.tp, "fp": self.fp, "fn": self.fn,
            "precision": p, "recall": r, "f1": _f1(p, r),
            "centre_error_um_median": float(np.median(self.centre)) if self.centre else None,
            "radius_error_median": float(np.median(self.radius)) if self.radius else None,
            "n_radius": len(self.radius),
        }  # fmt: skip
        if classified:
            cm = self.confusion
            per_class = {}
            for i, name in enumerate(self.classes):
                cp, cr = _ratio(cm[i, i], cm[:, i].sum()), _ratio(cm[i, i], cm[i].sum())
                per_class[name] = {
                    "precision": cp, "recall": cr, "f1": _f1(cp, cr), "support": int(cm[i].sum()),
                }  # fmt: skip
            f1s = [v["f1"] for v in per_class.values() if v["support"] and v["f1"] is not None]
            out["class_accuracy"] = _ratio(float(np.trace(cm)), float(cm.sum()))
            out["class_macro_f1"] = float(np.mean(f1s)) if f1s else None
            out["per_class"] = per_class
            out["confusion"] = {
                t: {d: int(cm[i, j]) for j, d in enumerate(self.classes)}
                for i, t in enumerate(self.classes)
            }
        return out


def evaluate(
    crops: list[Crop],
    labels: Labels,
    detections: Detections,
    classes: tuple[str, ...],
    tol_um: float = 3.0,
    rel: float = 0.5,
    border_um: float = 3.0,
) -> dict[str, Any]:
    """Scores per split ("train", "test", "all") and per crop.

    Per split: tp, fp, fn, precision, recall, F1; median centre error (µm) of matched pairs;
    median relative radius error |r_det - r_label| / r_label over outlined labels; and, when
    detections carry classes, the confusion table (rows = labelled class, columns = detected
    class), accuracy, macro F1 and precision/recall/F1 per class over the matched pairs.
    Labels outside every crop (extra examples) are not used here.
    """
    classified = detections.cls is not None
    index = {c: i for i, c in enumerate(classes)}
    tallies = {s: _Tally(classes) for s in SPLITS}
    per_crop = []
    for crop in crops:
        t_sel = np.nonzero((labels.crop == crop.id) & (labels.slice_id == crop.slice_id))[0]
        d_in = (detections.slice_id == crop.slice_id) & crop.inside(
            detections.x_um, detections.y_um
        )
        d_sel = np.nonzero(d_in)[0]
        t_xy = np.stack([labels.x_um[t_sel], labels.y_um[t_sel]], axis=1)
        d_xy = np.stack([detections.x_um[d_sel], detections.y_um[d_sel]], axis=1)
        pairs, miss, extra = match(t_xy, d_xy, detections.radius_um[d_sel], tol_um, rel)
        # near the edge a nucleus may be labelled on one side only: unmatched ones there don't count
        miss = miss[crop.inside(t_xy[miss, 0], t_xy[miss, 1], border_um)]
        extra = extra[crop.inside(d_xy[extra, 0], d_xy[extra, 1], border_um)]
        crop_tally = _Tally(classes)
        for tally in (tallies[crop.split], tallies["all"], crop_tally):
            tally.tp += len(pairs)
            tally.fn += len(miss)
            tally.fp += len(extra)
            for ti, di in pairs:
                t, d = t_sel[ti], d_sel[di]
                tally.centre.append(
                    float(np.hypot(labels.x_um[t] - detections.x_um[d], labels.y_um[t] - detections.y_um[d]))
                )  # fmt: skip
                if not np.isnan(labels.radius_um[t]):
                    rt = labels.radius_um[t]
                    tally.radius.append(abs(float(detections.radius_um[d]) - rt) / rt)
                if classified:
                    assert detections.cls is not None
                    tally.confusion[index[labels.cls[t]], index[detections.cls[d]]] += 1
        per_crop.append({"crop": crop.id, "slice": crop.slice_id, "split": crop.split,
                         **crop_tally.report(classified)})  # fmt: skip
    return {
        "match": {"tol_um": tol_um, "rel": rel, "border_um": border_um},
        "classes": list(classes),
        **{s: tallies[s].report(classified) for s in SPLITS},
        "per_crop": per_crop,
    }
