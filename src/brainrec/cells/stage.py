"""Cells stage: cell files of a slice range -> local alignment and links per pair, then 3D
cells, one Neuroglancer layer per class in the volume frame (M7-07c, M7-12, M7-13; contracts C6,
C7, C15, C16, C17). Reads no slide pixels.

Per consecutive pair (M7-12, M7-13): the displacement field of slice k onto k-1 after the pair
transform, and the links between their profiles (`cells/pair_{k}.npz`, C15). Resumes per pair: a
pair is redone when one of its cell files or its pair transform changed; other alignment or link
settings need `overwrite`. The linked profiles form chains, one 3D cell each (C16).

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
from brainrec.cells.align import CHANCE_OFFSET_UM, METHOD, AlignParams, align_pair
from brainrec.cells.layers import write_layer
from brainrec.cells.link import (
    Links,
    LinkSide,
    build_cells,
    build_chains,
    link_profiles,
    place_cells,
)
from brainrec.cells.place import Placed, VolumeFrame, place_profiles
from brainrec.registration import PairResult, Transform2D
from brainrec.registration.chain import load_chained
from brainrec.store import Manifest, StageExistsError, StageResult, WorkDir, fingerprint
from brainrec.store.cells import Profiles, load_cell_pair, load_profiles, save_cell_pair

log = logging.getLogger(__name__)
STAGE = "cells"
VERSION = 2  # 2: one blob per linked chain (M7-13) instead of one per profile
ALIGN_VERSION = 2  # 2: pair files hold the links (M7-13); bump when fields or links change


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
        "pairs": {str(k): fingerprint(work.cell_pair(k)) for k in ids[1:]},
        "link_max_sections": cfg.link_max_sections,
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
        "link_tolerance_um": cfg.link_tolerance_um,
    }


def _pair_inputs(work: WorkDir, fixed: int, moving: int) -> dict:
    return {
        "fixed_cells": fingerprint(work.cell_profiles(fixed)),
        "moving_cells": fingerprint(work.cell_profiles(moving)),
        "pair_sha256": hashlib.sha256(work.pair_transform(moving).read_bytes()).hexdigest(),
    }


def _side(p: Profiles, xy: np.ndarray) -> LinkSide:
    return LinkSide(xy, p.r_um.astype(float), p.class_id, p.confidence)


def _link_summary(links: Links, chance: Links, n_fixed: int) -> dict:
    return {
        "n_links": len(links),
        "share_linked": round(len(links) / n_fixed, 4) if n_fixed else 0.0,
        "median_link_um": round(float(np.median(links.dist_um)), 3) if len(links) else None,
        "share_linked_chance": round(len(chance) / n_fixed, 4) if n_fixed else 0.0,
    }


def align_and_link_pairs(
    catalog: Catalog,
    work: WorkDir,
    cfg: ProjectConfig,
    manifest: Manifest,
    overwrite: bool = False,
) -> StageResult:
    """The displacement field and the links of every consecutive pair of `catalog` (C15), keyed
    by the moving slice; skips pairs whose cell files and pair transform are unchanged. A pair
    without recurring nuclei gets no links. `share_linked_chance` counts the links found at a
    deliberately wrong offset, an estimate of the false links. FileNotFoundError naming
    unregistered pairs; StageExistsError if pairs of other alignment or link settings exist,
    unless `overwrite` (then every pair is redone)."""
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
        fixed_xy, moving_xy = np.c_[a.x_um, a.y_um], pr.transform.apply(np.c_[b.x_um, b.y_um])
        aligned = align_pair(fixed_xy, moving_xy, params)
        links = chance = Links.empty()
        if aligned.status == "aligned":
            moved = moving_xy + aligned.field.displacement(moving_xy)
            links = link_profiles(_side(a, fixed_xy), _side(b, moved), cfg.link_tolerance_um)
            chance = link_profiles(_side(a, fixed_xy), _side(b, moved + CHANCE_OFFSET_UM),
                                   cfg.link_tolerance_um)  # fmt: skip
        summary = {**aligned.summary(), **_link_summary(links, chance, len(a)),
                   "seconds": round(time.monotonic() - t0, 1)}  # fmt: skip
        save_cell_pair(work, moving, {**aligned.field.arrays(), **links.arrays()},
                       {"fixed": fixed, **summary})  # fmt: skip
        done[str(moving)] = {"inputs": inputs, "summary": summary}
        manifest.set(STAGE, {**(manifest.get(STAGE) or {}),
                             "pairs": {"signature": signature, "pairs": done}})  # fmt: skip
        result.done.append(moving)
        log.info("cells: pair %d<-%d %s, %.0f%% of nodes locked, %.1f%% linked (chance %.1f%%)",
                 fixed, moving, summary["status"], 100 * summary["share_locked"],
                 100 * summary["share_linked"], 100 * summary["share_linked_chance"])  # fmt: skip
    return result


def _pair_links(work: WorkDir, ids: list[int]) -> list[Links]:
    out = []
    for fixed, moving in itertools.pairwise(ids):
        arrays, meta = load_cell_pair(work, moving)
        if meta["fixed"] != fixed:
            raise CellsInputError(f"cells/pair_{moving} links slice {moving} to {meta['fixed']}, "
                                  f"not {fixed}: run `brainrec cells --overwrite`")  # fmt: skip
        out.append(Links.from_arrays(arrays))
    return out


def run_cells(
    catalog: Catalog,
    work: WorkDir,
    cfg: ProjectConfig,
    manifest: Manifest,
    overwrite: bool = False,
) -> StageResult:
    """Align and link every consecutive pair (`align_and_link_pairs`), build 3D cells from the
    chains of linked profiles, place them in the volume frame and write one layer per class
    (`cells/layer_<name>/`). Skips the layers if they exist from the same inputs. Layers of other
    inputs: StageExistsError unless `overwrite`. FileNotFoundError without a volume, cell files
    or pair transforms; CellsInputError if volume, registration or cell files disagree.
    `StageResult` lists the slices of the layers; the pairs are in the manifest (`cells.pairs`)."""
    ids = [s.id for s in catalog.slices]
    frame = volume_frame(work)
    _check_inputs(catalog, work, cfg, frame)
    align_and_link_pairs(catalog, work, cfg, manifest, overwrite)
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
    parts, sizes, r_um, prob, seen, section = [], [], [], [], [], []
    for s, k in enumerate(ids):
        profiles, meta = load_profiles(work, k)
        if meta["class_names"] != names:
            raise CellsInputError(
                f"slice {k} was classified with the classes {meta['class_names']}, the config "
                f"has {names}: run `brainrec detect --overwrite`"
            )
        parts.append(place_profiles(profiles, k, frame, proportions))
        sizes.append(len(profiles))
        r_um.append(profiles.r_um)
        prob.append(profiles.class_prob)
        seen.append(profiles.nucleolus_seen)
        section.append(np.full(len(profiles), s))
    label, n_split = build_chains(sizes, _pair_links(work, ids), cfg.link_max_sections)
    cells = build_cells(label, np.concatenate(r_um), np.concatenate(prob), np.concatenate(seen),
                        np.concatenate(section), cfg.section_thickness_um)  # fmt: skip
    placed = place_cells(Placed.concat(parts), cells, proportions)
    if n_split:
        log.warning("cells: %d chains longer than %d sections were split at their weakest link",
                    n_split, cfg.link_max_sections)  # fmt: skip
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
    lengths = np.bincount(cells.n_profiles.astype(int))
    entry = {
        "signature": signature,
        "counts": counts,
        "n_profiles": int(sum(sizes)),
        "n_cells": len(cells),
        "n_split_chains": n_split,
        "chain_lengths": {str(n): int(c) for n, c in enumerate(lengths) if c},
        "seconds": round(time.monotonic() - t0, 1),
    }
    manifest.set(STAGE, {**(manifest.get(STAGE) or {}), "layers": entry})
    return StageResult(done=ids)
