"""Detection stage: nuclei of every slice, classified, in `cells/{id}.npz` + `cells/{id}.json`
(M7-06; contracts C6, C7, C13, C14).

One pass over the 2x level per slice (`detect_slice`), level-0 patches only around larger nuclei.
The tissue mask comes from `preprocess` (C4). Resumable per slice: a slice is redone only when
its source file, its mask or the cell files changed. Results of other settings (detection
fields of C9, detector and version, classifier and its reference, type table) are not replaced
unless `overwrite` is given.
"""

import hashlib
import json
import logging
import time
from collections.abc import Callable

import cv2
import numpy as np

from brainrec.catalog import Catalog, ProjectConfig, SliceRef
from brainrec.detect.classify import RulesV1, classify
from brainrec.detect.nuclei import Detector, detect_slice, load_detector
from brainrec.slides import ReaderFactory, SlideReader
from brainrec.store import (
    Manifest,
    StageExistsError,
    StageResult,
    WorkDir,
    fingerprint,
)  # fmt: skip
from brainrec.store.cells import Profiles, save_profiles

log = logging.getLogger(__name__)
STAGE = "detect"
VERSION = 1  # bump when the algorithm changes in a way that should invalidate old results


def _sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _signature(cfg: ProjectConfig, detector: Detector, model: RulesV1) -> dict:
    return {
        "version": VERSION,
        "detect_downsample": cfg.detect_downsample,
        "detect_method": cfg.detect_method,
        "detect_tile_px": cfg.detect_tile_px,
        "detect_overlap_um": cfg.detect_overlap_um,
        "detect_min_area_um2": cfg.detect_min_area_um2,
        "detect_patch_min_diameter_um": cfg.detect_patch_min_diameter_um,
        "detect_min_sharpness": cfg.detect_min_sharpness,
        "detector": detector.name,
        "detector_version": detector.version,
        "classifier": model.name,
        "classifier_sha256": model.sha256,
        "cell_types": [t.model_dump(mode="json") for t in cfg.cell_types],
    }


def _plausible(profiles: Profiles, cfg: ProjectConfig) -> np.ndarray:
    """Profiles whose radius lies within the limits of their class (C14)."""
    lo = np.array([t.radius_um[0] for t in cfg.cell_types])
    hi = np.array([t.radius_um[1] for t in cfg.cell_types])
    k = profiles.class_id.astype(int) - 1
    return (profiles.r_um >= lo[k]) & (profiles.r_um <= hi[k])


def _subset(p: Profiles, keep: np.ndarray) -> Profiles:
    """Rows `keep` of a profile table, with the ragged outlines re-indexed."""
    idx = np.nonzero(keep)[0]
    off = p.outline_offsets
    parts = [p.outline_xy[off[i] : off[i + 1]] for i in idx]
    counts = np.array([len(q) for q in parts], np.int64)
    offsets = np.concatenate([[0], np.cumsum(counts)])
    xy = np.vstack(parts) if parts else np.zeros((0, 2))
    return Profiles(p.x_um[idx], p.y_um[idx], p.r_um[idx], p.area_um2[idx], offsets, xy,
                    p.features[idx], p.nucleolus_seen[idx], p.det_conf[idx], p.blurred[idx],
                    p.class_id[idx], p.class_prob[idx], p.confidence[idx])  # fmt: skip


def detect_one(
    ref: SliceRef,
    work: WorkDir,
    cfg: ProjectConfig,
    detector: Detector,
    model: RulesV1,
    reader_factory: ReaderFactory = SlideReader.open,
) -> dict:
    """Detect, classify and save the nuclei of one slice. Returns counts for the manifest.
    FileNotFoundError if the slice has no tissue mask (run preprocess first)."""
    if not work.mask(ref.id).exists() or not work.reg_meta(ref.id).exists():
        raise FileNotFoundError(f"slice {ref.id} has no tissue mask: run preprocess first")
    mask = cv2.imread(str(work.mask(ref.id)), cv2.IMREAD_GRAYSCALE)
    if mask is None:
        raise OSError(f"{work.mask(ref.id)} is not a readable PNG: run preprocess again")
    mask_downsample = float(json.loads(work.reg_meta(ref.id).read_text())["downsample"])
    names = tuple(t.name for t in cfg.cell_types)
    t0 = time.monotonic()
    reader = reader_factory(ref.path)
    try:
        nuclei = detect_slice(reader, mask, mask_downsample, cfg, detector)
        mpp = reader.mpp
    finally:
        reader.close()
    profiles = classify(nuclei, model, names)
    keep = _plausible(profiles, cfg)
    dropped = int((~keep).sum())
    if dropped:
        log.info("slice %d: %d profiles outside the radius limits of their class", ref.id, dropped)
        profiles = _subset(profiles, keep)
    meta = {
        "source": ref.path.name,
        "detector": detector.name,
        "detector_version": detector.version,
        "downsample": cfg.detect_downsample,
        "mpp_um": list(mpp),
        "classifier": model.name,
        "class_names": list(names),
        "n_blurred_tiles": nuclei.n_blurred_tiles,
        "tile_sharpness": [round(float(s), 1) for s in nuclei.tile_sharpness],
    }
    save_profiles(work, ref.id, profiles, meta)
    return {
        "n_profiles": len(profiles),
        "n_dropped_radius": dropped,
        "n_tiles": len(nuclei.tile_sharpness),
        "n_blurred_tiles": nuclei.n_blurred_tiles,
        "seconds": round(time.monotonic() - t0, 1),
    }


Proceed = Callable[[int, dict, list[int]], bool]
"""Asked after a slice is done while others are still to do: (finished slice id, its counts as
in the manifest, ids still to do) -> True to start the next one."""


def run_detect(
    catalog: Catalog,
    work: WorkDir,
    cfg: ProjectConfig,
    manifest: Manifest,
    reader_factory: ReaderFactory = SlideReader.open,
    overwrite: bool = False,
    detector: Detector | None = None,
    model: RulesV1 | None = None,
    proceed: Proceed | None = None,
) -> StageResult:
    """Detect and classify the nuclei of every slice of `catalog` (C13), skipping slices done
    with the same settings whose source, mask and cell files are unchanged. Settings changed
    since the last run: StageExistsError unless `overwrite` (then every slice is redone).
    `detector` defaults to `load_detector(cfg.detect_method)`, `model` to rules-v1.

    With `proceed`, the stage asks after each finished slice whether to start the next one
    (owner, 2026-11-08: a run of 140 slices takes days); on False it stops, and the slices not
    started are listed in `result.left`. A later run continues with them."""
    detector = detector or load_detector(cfg.detect_method)
    model = model or RulesV1.load()
    signature = _signature(cfg, detector, model)
    state = manifest.get(STAGE)
    if state is not None and state["signature"] != signature:
        if not overwrite:
            changed = sorted(k for k in signature if state["signature"].get(k) != signature[k])
            raise StageExistsError(
                f"{STAGE} results were made with other settings ({', '.join(changed)}); "
                "use overwrite to redo them"
            )
        state = None
    slices: dict[str, dict] = dict(state["slices"]) if state else {}
    result = StageResult()
    todo: list[tuple[SliceRef, dict[str, object]]] = []
    for ref in catalog.slices:
        inputs: dict[str, object] = {"source": fingerprint(ref.path)}
        if work.mask(ref.id).exists():
            inputs["mask_sha256"] = _sha256(work.mask(ref.id))
        done = slices.get(str(ref.id))
        files = (work.cell_profiles(ref.id), work.cell_meta(ref.id))
        if (
            done is not None
            and all(done.get(k) == v for k, v in inputs.items())
            and all(f.exists() for f in files)
            and done.get("cells") == fingerprint(work.cell_profiles(ref.id))
        ):
            result.skipped.append(ref.id)
        else:
            todo.append((ref, inputs))
    for i, (ref, inputs) in enumerate(todo):
        counts = detect_one(ref, work, cfg, detector, model, reader_factory)
        slices[str(ref.id)] = {**inputs, **counts,
                               "cells": fingerprint(work.cell_profiles(ref.id))}  # fmt: skip
        manifest.set(STAGE, {"signature": signature, "slices": slices})
        result.done.append(ref.id)
        log.info("detect: slice %d, %d profiles, %.0f s", ref.id, counts["n_profiles"],
                 counts["seconds"])  # fmt: skip
        rest = [r.id for r, _ in todo[i + 1 :]]
        if rest and proceed is not None and not proceed(ref.id, counts, rest):
            result.left = rest
            break
    return result
