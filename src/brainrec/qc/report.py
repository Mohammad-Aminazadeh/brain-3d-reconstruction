"""QC report of a slice range (M4-03, contract C10): every consecutive pair, its scores and a flag."""

import itertools
import json
from pathlib import Path
from typing import Any

from brainrec.catalog import ProjectConfig
from brainrec.qc.metrics import PairScore
from brainrec.registration import PairResult
from brainrec.store import WorkDir, write_atomic

SCHEMA = "brainrec.qc/1"


def is_flagged(score: PairScore, min_ncc: float | None, min_dice: float | None) -> bool:
    """True exactly when a score is below its threshold; a threshold of None is not checked."""
    return (min_ncc is not None and score.ncc < min_ncc) or (
        min_dice is not None and score.dice < min_dice
    )


def build_report(
    work: WorkDir,
    ids: list[int],
    pairs: dict[int, tuple[PairResult, PairScore]],
    cfg: ProjectConfig,
) -> dict[str, Any]:
    """Report (C10) of the consecutive pairs of `ids`, in order.

    `pairs` maps the moving slice id k to the registration result of k onto its predecessor and
    its score. A pair is flagged if registration failed (its transform is a best effort, M3-13)
    or a score is below its threshold. Overlay paths are relative to the work directory. Raises
    ValueError if a consecutive pair is missing, belongs to another neighbour, or `pairs` holds
    pairs outside `ids`.
    """
    expected = {k: prev for prev, k in itertools.pairwise(ids)}
    missing = [k for k in expected if k not in pairs]
    if missing:
        raise ValueError(f"no registered pair for slices {missing}")
    extra = sorted(k for k in pairs if k not in expected)
    if extra:
        raise ValueError(f"pairs of slices {extra} are not in the range {ids[0]}-{ids[-1]}")
    for k, prev in expected.items():
        r = pairs[k][0]
        if (r.fixed, r.moving) != (prev, k):
            raise ValueError(f"result {r.fixed}<-{r.moving} given for pair {prev}<-{k}")

    rows = []
    for k, prev in expected.items():
        result, score = pairs[k]
        rows.append(
            {
                "fixed": prev,
                "moving": k,
                "status": result.status,
                "n_inliers": score.n_inliers,
                "rms_um": score.rms_um,
                "ncc": score.ncc,
                "dice": score.dice,
                "flagged": result.status == "failed"
                or is_flagged(score, cfg.qc_min_ncc, cfg.qc_min_dice),
                "overlay": work.qc_overlay(k).relative_to(work.root).as_posix(),
            }
        )
    return {
        "schema": SCHEMA,
        "thresholds": {"qc_min_ncc": cfg.qc_min_ncc, "qc_min_dice": cfg.qc_min_dice},
        "pairs": rows,
    }


def write_report(work: WorkDir, report: dict[str, Any]) -> Path:
    """Write `report` to `qc/report.json` atomically; returns the path."""
    write_atomic(work.qc_report, json.dumps(report, indent=2).encode())
    return work.qc_report
