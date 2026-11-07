"""The `qc` stage (M4-04): score and draw every consecutive pair of a slice range and write the
report (contracts C6, C7, C10).

Resumes per pair, like `register`: a pair is redone only when its registration result or one of
its two registration images changed. The thresholds only decide the flags, so changing them
re-evaluates the report without recomputing scores or overlays.
"""

import hashlib
import itertools
import json
import logging
from dataclasses import asdict
from typing import Any

from brainrec.catalog import Catalog, ProjectConfig
from brainrec.qc.metrics import PairScore, score_pair
from brainrec.qc.overlay import render_overlay
from brainrec.qc.report import build_report, write_report
from brainrec.registration import PairResult, RegImage
from brainrec.slides import ReaderFactory, SlideReader
from brainrec.store import Manifest, StageExistsError, StageResult, WorkDir

log = logging.getLogger(__name__)
STAGE = "qc"
VERSION = 1  # bump when scores or overlays change in a way that should invalidate old results


def _signature(cfg: ProjectConfig) -> dict[str, Any]:
    # thresholds are left out on purpose: they only decide the flags, re-evaluated every run
    return {"version": VERSION, "reg_edge_weight": cfg.reg_edge_weight}


def _slice_hash(work: WorkDir, slice_id: int) -> str:
    h = hashlib.sha256()
    for path in (work.reg_meta(slice_id), work.reg_image(slice_id), work.mask(slice_id)):
        h.update(path.read_bytes())
    return h.hexdigest()


def _pair_inputs(work: WorkDir, fixed: int, moving: int, slices: dict[int, str]) -> str:
    """Hash of everything a pair's score and overlay depend on: its registration result (C5)
    and both slices' registration images, masks and metadata (C4). `slices` caches the
    per-slice hashes, since consecutive pairs share a slice."""
    h = hashlib.sha256(work.pair_transform(moving).read_bytes())
    for k in (fixed, moving):
        if k not in slices:
            slices[k] = _slice_hash(work, k)
        h.update(slices[k].encode())
    return h.hexdigest()


def run_qc(
    catalog: Catalog,
    work: WorkDir,
    cfg: ProjectConfig,
    manifest: Manifest,
    reader_factory: ReaderFactory = SlideReader.open,  # unused: works on registration files (C7)
    overwrite: bool = False,
) -> StageResult:
    """Score every consecutive pair of `catalog` (`score_pair`), draw its overlay
    (`qc/overlay_{k}.png`) and write `qc/report.json` with the current thresholds (C10).
    `StageResult.done` / `.skipped` list moving-slice ids of pairs.

    Raises FileNotFoundError naming the pairs without a registration result, and
    StageExistsError if earlier results used another `reg_edge_weight`, unless `overwrite`.
    """
    ids = [s.id for s in catalog.slices]
    unregistered = [
        f"{prev}<-{k}" for prev, k in itertools.pairwise(ids) if not work.pair_transform(k).exists()
    ]
    if unregistered:
        raise FileNotFoundError(
            f"pairs {', '.join(unregistered)} are not registered; run register first"
        )
    signature = _signature(cfg)
    state = manifest.get(STAGE)
    if state is not None and state["signature"] != signature:
        if not overwrite:
            raise StageExistsError(
                f"{STAGE} results were made with other settings; use overwrite to redo them"
            )
        state = None
    inputs: dict[str, str] = dict(state["inputs"]) if state else {}
    scores: dict[str, dict[str, Any]] = dict(state["scores"]) if state else {}

    result = StageResult()
    pairs: dict[int, tuple[PairResult, PairScore]] = {}
    slice_hashes: dict[int, str] = {}
    images: dict[int, RegImage] = {}  # the current pair's images; neighbours share one slice
    for fixed, moving in itertools.pairwise(ids):
        pr = PairResult.from_dict(json.loads(work.pair_transform(moving).read_text()))
        key = _pair_inputs(work, fixed, moving, slice_hashes)
        k = str(moving)
        if inputs.get(k) == key and k in scores and work.qc_overlay(moving).exists():
            pairs[moving] = (pr, PairScore(**scores[k]))
            result.skipped.append(moving)
            continue
        images = {i: images[i] if i in images else RegImage.load(work, i) for i in (fixed, moving)}
        score = score_pair(images[fixed], images[moving], pr, cfg.reg_edge_weight)
        render_overlay(images[fixed], images[moving], pr.transform, work.qc_overlay(moving))
        inputs[k], scores[k] = key, asdict(score)
        manifest.set(
            STAGE,
            {
                "signature": signature,
                "done": sorted(map(int, inputs)),
                "inputs": inputs,
                "scores": scores,
            },
        )
        result.done.append(moving)
        pairs[moving] = (pr, score)
        log.info("qc %d<-%d: ncc %.3f, dice %.3f", fixed, moving, score.ncc, score.dice)

    write_report(work, build_report(work, ids, pairs, cfg))
    return result
