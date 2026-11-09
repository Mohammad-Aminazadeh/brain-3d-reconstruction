"""Cells stage: cell files of a slice range -> local alignment per pair, then one Neuroglancer
layer per class in the volume frame (M7-07c, M7-12; contracts C6, C7, C15, C16, C17). Reads no
slide pixels.

Local alignment (M7-12): per consecutive pair, the displacement field of slice k onto k-1 after
the pair transform (`cells/pair_{k}.npz`, C15). Resumes per pair: a pair is redone when one of its
cell files or its pair transform changed; other alignment settings need `overwrite`.

Until linking (M7-13) every profile is its own cell. The frame is the registered volume's
(C11 metadata in `volume.zarr`): the cells are placed with the transforms, canvas origin and z
spacing the volume was built with, so blobs and image coincide. The layers are rebuilt per
range; results of other inputs (range, cell files, volume, type table) are not replaced unless
`overwrite` is given.
"""

import hashlib
import itertools
import json
import logging
import time
from typing import Any

import numpy as np
import zarr

from brainrec.catalog import Catalog, ProjectConfig
from brainrec.cells.align import METHOD, AlignParams, align_pair
from brainrec.cells.layers import write_layer
from brainrec.cells.place import Placed, VolumeFrame, place_profiles
from brainrec.registration import PairResult, Transform2D
from brainrec.registration.chain import load_chained
from brainrec.store import Manifest, StageExistsError, StageResult, WorkDir, fingerprint
from brainrec.store.cells import load_profiles, save_cell_pair

log = logging.getLogger(__name__)
STAGE = "cells"
VERSION = 1
ALIGN_VERSION = 1  # bump when the alignment changes in a way that should invalidate old fields


class CellsInputError(ValueError):
    """The inputs of the cells stage do not fit together (volume, registration, cell files)."""


def volume_frame(work: WorkDir) -> VolumeFrame:
    """The frame of the registered volume from its metadata (C11). FileNotFoundError without a
    volume; CellsInputError for an unregistered volume."""
    if not (work.volume / "zarr.json").exists():
        raise FileNotFoundError(f"no volume at {work.volume}: run `brainrec volume` first")
    attrs: dict[str, Any] = dict(zarr.open_group(str(work.volume), mode="r").attrs)
    meta: dict[str, Any] = dict(attrs.get("brainrec") or {})
    if meta.get("mode") != "registered":
        raise CellsInputError("the volume is not registered: run `brainrec volume` without "
                              "--unregistered")  # fmt: skip
    transforms = attrs["ome"]["multiscales"][0]["datasets"][0]["coordinateTransformations"]
    scale = next(t["scale"] for t in transforms if t["type"] == "scale")
    return VolumeFrame(
        slice_ids=tuple(int(i) for i in meta["slice_ids"]),
        origin_um=(float(meta["canvas_origin_um"][0]), float(meta["canvas_origin_um"][1])),
        z_spacing_um=float(scale[1]),  # axes c, z, y, x
        transforms={
            int(k): Transform2D.from_matrix(np.array(m))
            for k, m in meta["slice_to_reference_um"].items()
        },
    )


def _check_inputs(catalog: Catalog, work: WorkDir, cfg: ProjectConfig, frame: VolumeFrame) -> None:
    ids = [s.id for s in catalog.slices]
    missing = [k for k in ids if k not in frame.slice_ids]
    if missing:
        raise CellsInputError(
            f"the volume holds slices {frame.slice_ids[0]}-{frame.slice_ids[-1]}, not {missing}: "
            "build the volume for this range first"
        )
    chained = load_chained(work, ids)
    changed = [k for k in ids if not chained[k].allclose(frame.transforms[k], atol_um=1e-3)]
    if changed:
        raise CellsInputError(
            f"slices {changed} were registered again after the volume was built: "
            "rebuild the volume (`brainrec volume --overwrite`)"
        )
    if abs(frame.z_spacing_um - cfg.z_spacing_um) > 1e-9:
        raise CellsInputError(
            f"the volume was built with z spacing {frame.z_spacing_um} µm, the config says "
            f"{cfg.z_spacing_um} µm: rebuild the volume"
        )
    no_cells = [k for k in ids if not work.cell_meta(k).exists()]
    if no_cells:
        raise FileNotFoundError(f"slices {no_cells} have no cell file: run `brainrec detect`")


def _signature(catalog: Catalog, work: WorkDir, cfg: ProjectConfig, frame: VolumeFrame) -> dict:
    ids = [s.id for s in catalog.slices]
    volume = json.dumps({"origin": frame.origin_um, "z": frame.z_spacing_um, "ids": frame.slice_ids,
                         "t": {k: frame.transforms[k].matrix.tolist() for k in ids}})  # fmt: skip
    return {
        "version": VERSION,
        "ids": ids,
        "cells": {str(k): fingerprint(work.cell_profiles(k)) for k in ids},
        "volume_sha256": hashlib.sha256(volume.encode()).hexdigest(),
        "cell_types": [t.model_dump(mode="json") for t in cfg.cell_types],
    }


def _align_signature(cfg: ProjectConfig) -> dict:
    return {
        "version": ALIGN_VERSION,
        "method": METHOD,
        "align_grid_um": cfg.align_grid_um,
        "align_window_um": cfg.align_window_um,
        "align_search_um": cfg.align_search_um,
        "align_min_peak_ratio": cfg.align_min_peak_ratio,
    }


def _pair_inputs(work: WorkDir, fixed: int, moving: int) -> dict:
    return {
        "fixed_cells": fingerprint(work.cell_profiles(fixed)),
        "moving_cells": fingerprint(work.cell_profiles(moving)),
        "pair_sha256": hashlib.sha256(work.pair_transform(moving).read_bytes()).hexdigest(),
    }


def align_pairs(
    catalog: Catalog,
    work: WorkDir,
    cfg: ProjectConfig,
    manifest: Manifest,
    overwrite: bool = False,
) -> StageResult:
    """The displacement field of every consecutive pair of `catalog` (C15), keyed by the moving
    slice; skips pairs whose cell files and pair transform are unchanged. FileNotFoundError naming
    unregistered pairs; StageExistsError if fields of other alignment settings exist, unless
    `overwrite` (then every pair is redone)."""
    ids = [s.id for s in catalog.slices]
    pairs = list(itertools.pairwise(ids))
    missing = [f"{a}<-{b}" for a, b in pairs if not work.pair_transform(b).exists()]
    if missing:
        raise FileNotFoundError(f"pairs {', '.join(missing)} are not registered: run "
                                "`brainrec register`")  # fmt: skip
    signature = _align_signature(cfg)
    state = (manifest.get(STAGE) or {}).get("pairs")
    if state is not None and state["signature"] != signature:
        if not overwrite:
            changed = sorted(k for k in signature if state["signature"].get(k) != signature[k])
            raise StageExistsError(
                f"cell alignment exists from other settings ({', '.join(changed)}); "
                "use overwrite to redo it"
            )
        state = None
    done: dict[str, dict] = dict(state["pairs"]) if state else {}
    params = AlignParams.from_config(cfg)
    result = StageResult()
    for fixed, moving in pairs:
        inputs = _pair_inputs(work, fixed, moving)
        old = done.get(str(moving))
        files = (work.cell_pair(moving), work.cell_pair_meta(moving))
        if old is not None and old["inputs"] == inputs and all(f.exists() for f in files):
            result.skipped.append(moving)
            continue
        t0 = time.monotonic()
        a, _ = load_profiles(work, fixed)
        b, _ = load_profiles(work, moving)
        pr = PairResult.from_dict(json.loads(work.pair_transform(moving).read_text()))
        aligned = align_pair(np.c_[a.x_um, a.y_um], pr.transform.apply(np.c_[b.x_um, b.y_um]),
                             params)  # fmt: skip
        summary = {**aligned.summary(), "seconds": round(time.monotonic() - t0, 1)}
        arrays = {**aligned.field.arrays(), "links": np.zeros((0, 2), np.int32),
                  "link_dist_um": np.zeros(0, np.float32)}  # fmt: skip
        save_cell_pair(work, moving, arrays, {"fixed": fixed, **summary})
        done[str(moving)] = {"inputs": inputs, "summary": summary}
        manifest.set(STAGE, {**(manifest.get(STAGE) or {}),
                             "pairs": {"signature": signature, "pairs": done}})  # fmt: skip
        result.done.append(moving)
        log.info("cells: pair %d<-%d %s, %.0f%% of nodes locked, recurrence %.1f%% (chance %.1f%%)",
                 fixed, moving, summary["status"], 100 * summary["share_locked"],
                 100 * summary["recurrence"], 100 * summary["recurrence_chance"])  # fmt: skip
    return result


def run_cells(
    catalog: Catalog,
    work: WorkDir,
    cfg: ProjectConfig,
    manifest: Manifest,
    overwrite: bool = False,
) -> StageResult:
    """Align every consecutive pair locally (`align_pairs`), then place the profiles of the
    slices of `catalog` in the volume frame and write one layer per class
    (`cells/layer_<name>/`). Skips the layers if they exist from the same inputs. Layers of other
    inputs: StageExistsError unless `overwrite`. FileNotFoundError without a volume, cell files
    or pair transforms; CellsInputError if volume, registration or cell files disagree.
    `StageResult` lists the slices of the layers; the pairs are in the manifest (`cells.pairs`)."""
    ids = [s.id for s in catalog.slices]
    frame = volume_frame(work)
    _check_inputs(catalog, work, cfg, frame)
    align_pairs(catalog, work, cfg, manifest, overwrite)
    signature = _signature(catalog, work, cfg, frame)
    state = (manifest.get(STAGE) or {}).get("layers")
    layers = [work.cell_layer(t.name) for t in cfg.cell_types]
    if state is not None and all((p / "info").exists() for p in layers):
        if state["signature"] == signature:
            return StageResult(skipped=ids)
        if not overwrite:
            changed = sorted(k for k in signature if state["signature"].get(k) != signature[k])
            raise StageExistsError(
                f"cell layers exist from other inputs ({', '.join(changed)}); "
                "use overwrite to replace them"
            )
    t0 = time.monotonic()
    names = [t.name for t in cfg.cell_types]
    proportions = np.array([t.proportions for t in cfg.cell_types], float)
    parts = []
    for k in ids:
        profiles, meta = load_profiles(work, k)
        if meta["class_names"] != names:
            raise CellsInputError(
                f"slice {k} was classified with the classes {meta['class_names']}, the config "
                f"has {names}: run `brainrec detect --overwrite`"
            )
        parts.append(place_profiles(profiles, k, frame, proportions))
    placed = Placed.concat(parts)
    if len(placed):
        bounds = ((placed.centre - placed.radii).min(0), (placed.centre + placed.radii).max(0))
    else:
        bounds = (np.zeros(3), np.ones(3))
    counts = {}
    for t, path in zip(cfg.cell_types, layers, strict=True):
        sel = placed.class_id == t.id
        write_layer(path, placed.subset(sel), bounds)
        counts[t.name] = int(sel.sum())
        log.info("cells: layer %s, %d cells", t.name, counts[t.name])
    entry = {"signature": signature, "counts": counts, "seconds": round(time.monotonic() - t0, 1)}
    manifest.set(STAGE, {**(manifest.get(STAGE) or {}), "layers": entry})
    return StageResult(done=ids)
