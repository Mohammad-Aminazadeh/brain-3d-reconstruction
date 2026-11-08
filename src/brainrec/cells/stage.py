"""Cells stage: cell files of a slice range -> one Neuroglancer layer per class in the volume
frame (M7-07c; contracts C6, C7, C16, C17). Reads no slide pixels.

Until linking (M7-13) every profile is its own cell. The frame is the registered volume's
(C11 metadata in `volume.zarr`): the cells are placed with the transforms, canvas origin and z
spacing the volume was built with, so blobs and image coincide. The layers are rebuilt per
range; results of other inputs (range, cell files, volume, type table) are not replaced unless
`overwrite` is given.
"""

import hashlib
import json
import logging
import time
from typing import Any

import numpy as np
import zarr

from brainrec.catalog import Catalog, ProjectConfig
from brainrec.cells.layers import write_layer
from brainrec.cells.place import Placed, VolumeFrame, place_profiles
from brainrec.registration import Transform2D
from brainrec.registration.chain import load_chained
from brainrec.store import Manifest, StageExistsError, StageResult, WorkDir, fingerprint
from brainrec.store.cells import load_profiles

log = logging.getLogger(__name__)
STAGE = "cells"
VERSION = 1


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
    scale = attrs["ome"]["multiscales"][0]["datasets"][0]["coordinateTransformations"][0]
    return VolumeFrame(
        slice_ids=tuple(int(i) for i in meta["slice_ids"]),
        origin_um=(float(meta["canvas_origin_um"][0]), float(meta["canvas_origin_um"][1])),
        z_spacing_um=float(scale["scale"][1]),  # axes c, z, y, x
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


def run_cells(
    catalog: Catalog,
    work: WorkDir,
    cfg: ProjectConfig,
    manifest: Manifest,
    overwrite: bool = False,
) -> StageResult:
    """Place the profiles of the slices of `catalog` in the volume frame and write one layer
    per class (`cells/layer_<name>/`). Skips the work if the layers exist from the same inputs.
    Layers of other inputs: StageExistsError unless `overwrite`. FileNotFoundError without a
    volume or cell files; CellsInputError if volume, registration or cell files disagree."""
    ids = [s.id for s in catalog.slices]
    frame = volume_frame(work)
    _check_inputs(catalog, work, cfg, frame)
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
