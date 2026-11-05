import itertools
import json
from pathlib import Path

import pytest

from brainrec.catalog import ProjectConfig
from brainrec.qc.metrics import PairScore
from brainrec.qc.report import build_report, is_flagged, write_report
from brainrec.registration import PairResult, Transform2D
from brainrec.store import WorkDir

FIELDS = ["fixed", "moving", "status", "n_inliers", "rms_um", "ncc", "dice", "flagged", "overlay"]


def _cfg(**kw) -> ProjectConfig:
    return ProjectConfig(data_dir=".", work_dir=".", z_spacing_um=5.0, **kw)


def _pair(fixed: int, moving: int, ncc: float = 0.6, dice: float = 0.97, status="features"):
    result = PairResult(fixed, moving, Transform2D.identity(), status, 900, 300, 17.0, 0.95)
    return result, PairScore(300, 17.0, ncc, dice)


def _pairs(ids: list[int], scores: dict[int, dict] | None = None) -> dict:
    """Pairs of `ids` keyed by moving id; `scores` overrides ncc/dice of single pairs."""
    scores = scores or {}
    return {k: _pair(prev, k, **scores.get(k, {})) for prev, k in itertools.pairwise(ids)}


def test_every_consecutive_pair_in_order(tmp_path):
    work = WorkDir(tmp_path)
    ids = [3, 4, 5, 6]
    pairs = _pairs(ids)
    report = build_report(work, ids, dict(reversed(pairs.items())), _cfg())
    assert report["schema"] == "brainrec.qc/1"
    rows = report["pairs"]
    assert [(r["fixed"], r["moving"]) for r in rows] == [(3, 4), (4, 5), (5, 6)]
    assert all(list(r) == FIELDS for r in rows)  # C10
    assert rows[0] == {
        "fixed": 3, "moving": 4, "status": "features", "n_inliers": 300, "rms_um": 17.0,
        "ncc": 0.6, "dice": 0.97, "flagged": False, "overlay": "qc/overlay_4.png",
    }  # fmt: skip
    assert (work.root / rows[0]["overlay"]) == work.qc_overlay(4)


def test_thresholds_are_recorded(tmp_path):
    report = build_report(WorkDir(tmp_path), [1, 2], _pairs([1, 2]), _cfg(qc_min_ncc=0.35))
    assert report["thresholds"] == {"qc_min_ncc": 0.35, "qc_min_dice": 0.90}


@pytest.mark.parametrize(
    "ncc, dice, min_ncc, min_dice, flagged",
    [
        (0.60, 0.97, 0.35, 0.90, False),
        (0.30, 0.97, 0.35, 0.90, True),  # ncc below
        (0.60, 0.89, 0.35, 0.90, True),  # dice below
        (0.30, 0.89, 0.35, 0.90, True),  # both below
        (0.35, 0.90, 0.35, 0.90, False),  # at the threshold is not below
        (-1.0, 0.97, None, 0.90, False),  # ncc not checked
        (0.60, 0.10, 0.35, None, False),  # dice not checked
        (-1.0, 0.00, None, None, False),  # nothing checked
    ],
)
def test_flag_exactly_when_a_score_is_below_its_threshold(ncc, dice, min_ncc, min_dice, flagged):
    score = PairScore(300, 17.0, ncc, dice)
    assert is_flagged(score, min_ncc, min_dice) is flagged
    cfg = _cfg(qc_min_ncc=min_ncc, qc_min_dice=min_dice)
    pairs = _pairs([1, 2], {2: {"ncc": ncc, "dice": dice}})
    report = build_report(WorkDir(Path(".")), [1, 2], pairs, cfg)
    assert report["pairs"][0]["flagged"] is flagged


def test_flags_only_the_weak_pair(tmp_path):
    ids = list(range(1, 6))
    pairs = _pairs(ids, {4: {"dice": 0.85}})
    rows = build_report(WorkDir(tmp_path), ids, pairs, _cfg())["pairs"]
    assert [r["moving"] for r in rows if r["flagged"]] == [4]


def test_failed_pair_is_always_flagged():
    """M3-13: a failed pair carries a best-effort transform, so it is flagged whatever its scores."""
    pairs = {2: _pair(1, 2, status="failed"), 3: _pair(2, 3, status="boundary")}
    cfg = _cfg(qc_min_ncc=None, qc_min_dice=None)  # no score is checked at all
    rows = build_report(WorkDir(Path(".")), [1, 2, 3], pairs, cfg)["pairs"]
    assert [r["flagged"] for r in rows] == [True, False]


def test_status_is_reported():
    pairs = {2: _pair(1, 2, status="boundary"), 3: _pair(2, 3, status="failed")}
    rows = build_report(WorkDir(Path(".")), [1, 2, 3], pairs, _cfg())["pairs"]
    assert [r["status"] for r in rows] == ["boundary", "failed"]


def test_single_slice_has_no_pairs():
    assert build_report(WorkDir(Path(".")), [7], {}, _cfg())["pairs"] == []


def test_missing_pair_is_an_error():
    pairs = _pairs([1, 2, 3, 4])
    del pairs[3]
    with pytest.raises(ValueError, match=r"no registered pair for slices \[3\]"):
        build_report(WorkDir(Path(".")), [1, 2, 3, 4], pairs, _cfg())


def test_pair_outside_the_range_is_an_error():
    with pytest.raises(ValueError, match=r"pairs of slices \[4\] are not in the range 1-3"):
        build_report(WorkDir(Path(".")), [1, 2, 3], _pairs([1, 2, 3, 4]), _cfg())


def test_pair_of_another_neighbour_is_an_error():
    pairs = _pairs([1, 2, 3])
    pairs[3] = _pair(1, 3)  # registered onto slice 1, not 2 (another run)
    with pytest.raises(ValueError, match=r"result 1<-3 given for pair 2<-3"):
        build_report(WorkDir(Path(".")), [1, 2, 3], pairs, _cfg())


def test_write_report_round_trips(tmp_path):
    work = WorkDir(tmp_path)
    report = build_report(work, [1, 2, 3], _pairs([1, 2, 3]), _cfg())
    path = write_report(work, report)
    assert path == work.qc_report == tmp_path / "qc" / "report.json"
    assert json.loads(path.read_text()) == report
    assert [p.name for p in path.parent.iterdir()] == ["report.json"]  # no temporary file left
