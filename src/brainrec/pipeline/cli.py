"""Interim CLI until `brainrec run` arrives in M5 (M2-08, M3-10, M4-04, M7-06): `preprocess`,
`register`, `qc`, `volume`, `detect`, `cells` and `qc-cells` for a slice range, and `serve`.

Mistakes in the input, the settings or the environment end with a one-line `error: ...` and exit
status 1; anything else is a bug and keeps its traceback.
"""

import argparse
import itertools
import json
import logging
import sys
from pathlib import Path
from typing import Any

import uvicorn
import yaml
import zarr
from pydantic import ValidationError

from brainrec.catalog import Catalog, ProjectConfig
from brainrec.cells.stage import CellsInputError, run_cells
from brainrec.detect.classify import RULE_CLASSES
from brainrec.detect.stage import run_detect
from brainrec.preprocess import run_preprocess
from brainrec.qc import run_qc, run_qc_cells
from brainrec.registration import PairResult, Transform2D
from brainrec.registration.chain import load_chained, run_register
from brainrec.serve import (
    LAYOUTS,
    CellLayer,
    create_app,
    hosted_url,
    launch_local_viewer,
    neuroglancer_state,
)
from brainrec.store import Manifest, StageExistsError, StageResult, WorkDir
from brainrec.volume import VolumeExistsError, VolumeSpec, build_volume


class CliError(Exception):
    """A mistake in the input, the settings or the environment."""


# never bugs: reported in one line, without a traceback
USER_ERRORS = (CliError, OSError, StageExistsError, VolumeExistsError, CellsInputError)


def _range(text: str) -> tuple[int, int]:
    first, sep, last = text.partition("-")
    try:
        lo, hi = int(first), int(last if sep else first)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a slice range like 1-10 or 5") from None
    if lo > hi:
        raise argparse.ArgumentTypeError(f"{text}: the first slice comes after the last")
    return lo, hi


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="brainrec")
    p.add_argument("--config", type=Path, required=True)
    sub = p.add_subparsers(dest="cmd", required=True)

    def stage(name: str, text: str, redo: str) -> argparse.ArgumentParser:
        s = sub.add_parser(name, help=text)
        s.add_argument("--slices", type=_range, required=True, help="e.g. 1-10, or 5")
        s.add_argument("--overwrite", action="store_true", help=redo)
        return s

    stage("preprocess", "tissue masks and registration images", "redo results of other settings")
    stage("register", "align consecutive slices to the first", "redo pairs of other settings")
    stage("qc", "scores, overlays and flags of registered pairs", "redo pairs of other settings")
    stage("qc-cells", "detection QC: density, blur, folds, flags, review crops",
          "redo slices of other fold or crop settings")  # fmt: skip
    v = stage("volume", "registered, tissue-only volume", "replace a volume of other inputs")
    v.add_argument("--unregistered", action="store_true", help="stack whole scans unaligned (M2)")
    d = stage(
        "detect", "nuclei per slice, measured and classified", "redo slices of other settings"
    )
    d.add_argument(
        "--no-ask",
        action="store_true",
        help="run all slices without asking before each next one (a slice takes about 16 min)",
    )
    stage("cells", "cells in 3D: one viewer layer per class", "replace layers of other inputs")
    s = sub.add_parser("serve", help="serve the volume and open it in a local Neuroglancer")
    s.add_argument("--port", type=int, default=8000, help="data port; the viewer uses port+1")
    s.add_argument(
        "--layout",
        default="xy",
        choices=list(LAYOUTS),
        help="initial panel layout (xy-3d: section and 3D side by side; 4panel is the heaviest)",
    )
    s.add_argument(
        "--tissue-opacity",
        type=float,
        default=None,
        help="start opacity of the tissue (0 = transparent, 1 = opaque); also a slider in the viewer",
    )
    s.add_argument("--gpu-memory-mb", type=int, default=512)
    return p


def _config(path: Path) -> ProjectConfig:
    try:
        return ProjectConfig.load(path)
    except FileNotFoundError:
        raise CliError(f"config file {path} not found") from None
    except yaml.YAMLError as exc:
        raise CliError(f"{path} is not valid YAML: {exc}") from None
    except ValidationError as exc:
        problems = [
            f"{'.'.join(map(str, e['loc']))}: "
            + ("unknown setting" if e["type"] == "extra_forbidden" else e["msg"])
            for e in exc.errors()
        ]
        raise CliError(f"{path}: {'; '.join(problems)}") from None
    except TypeError as exc:  # no settings at all
        raise CliError(str(exc)) from None


def _catalog(cfg: ProjectConfig, slices: tuple[int, int]) -> Catalog:
    if not cfg.data_dir.is_dir():
        raise CliError(f"data_dir {cfg.data_dir} not found (is the drive connected?)")
    try:
        return Catalog.scan(cfg.data_dir, slices)
    except ValueError as exc:  # missing or duplicate slice ids
        raise CliError(str(exc)) from None


def _summary(stage: str, result: StageResult, items: str, note: str = "") -> None:
    print(f"{stage} ({items}): {len(result.done)} done, {len(result.skipped)} up to date{note}")


def _preprocess(args: argparse.Namespace, cfg: ProjectConfig, work: WorkDir) -> None:
    catalog = _catalog(cfg, args.slices)
    result = run_preprocess(catalog, work, cfg, Manifest(work.manifest), overwrite=args.overwrite)
    _summary("preprocess", result, "slices")


def _register(args: argparse.Namespace, cfg: ProjectConfig, work: WorkDir) -> None:
    catalog = _catalog(cfg, args.slices)
    result = run_register(catalog, work, cfg, Manifest(work.manifest), overwrite=args.overwrite)
    _summary("register", result, "pairs", f"; chained to slice {catalog.slices[0].id}")
    ids = [s.id for s in catalog.slices]
    failed = [
        f"{prev}<-{k}"
        for prev, k in itertools.pairwise(ids)
        if PairResult.from_dict(json.loads(work.pair_transform(k).read_text())).status == "failed"
    ]
    if failed:  # best effort, chained anyway (M3-13)
        print(f"warning: pairs {', '.join(failed)} failed; best-effort alignment, check them in QC")


def _qc(args: argparse.Namespace, cfg: ProjectConfig, work: WorkDir) -> None:
    catalog = _catalog(cfg, args.slices)
    result = run_qc(catalog, work, cfg, Manifest(work.manifest), overwrite=args.overwrite)
    report = json.loads(work.qc_report.read_text())
    rows = report["pairs"]
    if rows:
        print(f"{'pair':<10}{'status':<10}{'inliers':>8}{'rms_um':>8}{'ncc':>8}{'dice':>8}  flag")
    for r in rows:
        pair = f"{r['fixed']}<-{r['moving']}"
        rms = "-" if r["rms_um"] is None else f"{r['rms_um']:.1f}"
        line = (
            f"{pair:<10}{r['status']:<10}{r['n_inliers']:>8}{rms:>8}"
            f"{r['ncc']:>8.3f}{r['dice']:>8.3f}  {'FLAGGED' if r['flagged'] else ''}"
        )
        print(line.rstrip())
    limits = ", ".join(
        f"{name} {'not checked' if value is None else value}"
        for name, value in report["thresholds"].items()
    )
    print(f"thresholds: {limits}")
    _summary("qc", result, "pairs", f"; report {work.qc_report}")
    flagged = [f"{r['fixed']}<-{r['moving']}" for r in rows if r["flagged"]]
    print(f"flagged: {', '.join(flagged) or 'none'}")


def _qc_cells(args: argparse.Namespace, cfg: ProjectConfig, work: WorkDir) -> None:
    catalog = _catalog(cfg, args.slices)
    result = run_qc_cells(catalog, work, cfg, Manifest(work.manifest), overwrite=args.overwrite)
    report = json.loads(work.qc_cells_report.read_text())
    print(f"{'slice':<7}{'tissue mm2':>11}{'cells':>9}{'per mm2':>9}{'ratio':>7}"
          f"{'blurred tiles':>15}{'folds':>7}  flags")  # fmt: skip
    for r in report["slices"]:
        ratio = "-" if r["density_ratio"] is None else f"{r['density_ratio']:.2f}"
        line = (
            f"{r['slice_id']:<7}{r['tissue_mm2']:>11.1f}{r['n_profiles']:>9}"
            f"{r['density_mm2']:>9.0f}{ratio:>7}{100 * r['blurred_tile_share']:>14.0f}%"
            f"{100 * r['fold_share']:>6.1f}%  {', '.join(r['flags'])}"
        )
        print(line.rstrip())
    print("thresholds: " + ", ".join(f"{k} {v}" for k, v in report["thresholds"].items()))
    _summary("qc-cells", result, "slices",
             f"; report {work.qc_cells_report}; review crops {work.qc_dir}/cells_<id>.jpg")  # fmt: skip
    print(f"flagged: {', '.join(map(str, report['flagged'])) or 'none'}")


def _registration(work: WorkDir, ids: list[int]) -> tuple[dict[int, Transform2D], dict[int, Path]]:
    """Chained transforms and tissue masks of `ids` for the registered volume (C8)."""
    span = f"{ids[0]}-{ids[-1]}"
    try:
        transforms = load_chained(work, ids)
    except FileNotFoundError as exc:
        raise CliError(
            f"{exc}: run `brainrec register --slices {span}` first, or use --unregistered"
        ) from None
    except ValueError as exc:  # transforms of separate register runs
        raise CliError(f"{exc}: run `brainrec register --slices {span}` again") from None
    return transforms, {k: work.mask(k) for k in ids}


def _volume(args: argparse.Namespace, cfg: ProjectConfig, work: WorkDir) -> None:
    catalog = _catalog(cfg, args.slices)
    spec = VolumeSpec(
        z_spacing_um=cfg.z_spacing_um,
        base_downsample=cfg.base_downsample,
        n_levels=cfg.n_levels,
        mask_margin_um=cfg.volume_mask_margin_um,
    )
    transforms, masks = (
        (None, None) if args.unregistered else _registration(work, [s.id for s in catalog.slices])
    )
    out = build_volume(
        catalog,
        work.volume,
        spec,
        Manifest(work.manifest),
        overwrite=args.overwrite,
        transforms=transforms,
        masks=masks,
    )
    print(out)


def _ask_next(finished: int, counts: dict, rest: list[int]) -> bool:
    """Ask on the terminal whether to start the next slice; no answer (no terminal) is no."""
    hours = counts["seconds"] * len(rest) / 3600
    print(
        f"slice {finished} done: {counts['n_profiles']} nuclei in {counts['seconds']:.0f} s. "
        f"{len(rest)} slice(s) left (about {hours:.1f} h).",
        flush=True,
    )
    try:
        answer = input(f"Start slice {rest[0]}? [y/N] ")
    except EOFError:
        answer = ""
    return answer.strip().lower() in ("y", "yes")


def _detect(args: argparse.Namespace, cfg: ProjectConfig, work: WorkDir) -> None:
    catalog = _catalog(cfg, args.slices)
    names = [t.name for t in cfg.cell_types]
    missing = [c for c in RULE_CLASSES if c not in names]
    if missing:
        raise CliError(
            f"cell_types lacks {', '.join(missing)}, which the classifier rules-v1 gives"
        )
    manifest = Manifest(work.manifest)
    proceed = None if args.no_ask else _ask_next
    result = run_detect(catalog, work, cfg, manifest, overwrite=args.overwrite, proceed=proceed)
    slices = (manifest.get("detect") or {}).get("slices", {})
    print(f"{'slice':<7}{'nuclei':>9}{'dropped':>9}{'blurred tiles':>15}{'seconds':>9}")
    for ref in catalog.slices:
        if ref.id in result.left:
            continue
        row = slices[str(ref.id)]
        blurred = f"{row['n_blurred_tiles']}/{row['n_tiles']}"
        print(f"{ref.id:<7}{row['n_profiles']:>9}{row['n_dropped_radius']:>9}{blurred:>15}"
              f"{row['seconds']:>9.0f}")  # fmt: skip
    note = f"; cell files in {work.cells_dir}"
    if result.left:
        left = result.left
        which = ", ".join(map(str, left)) if len(left) <= 6 else f"{left[0]} ... {left[-1]}"
        note += f"; stopped, not started: {which}; run the same command to continue"
    _summary("detect", result, "slices", note)


def _cells(args: argparse.Namespace, cfg: ProjectConfig, work: WorkDir) -> None:
    catalog = _catalog(cfg, args.slices)
    manifest = Manifest(work.manifest)
    result = run_cells(catalog, work, cfg, manifest, overwrite=args.overwrite)
    counts = manifest.get("cells")["layers"]["counts"]
    print(f"{'class':<22}{'cells':>10}  layer")
    for t in cfg.cell_types:
        print(f"{t.name:<22}{counts[t.name]:>10}  {work.cell_layer(t.name)}")
    state = "built" if result.done else "up to date"
    print(f"cells (slices {args.slices[0]}-{args.slices[1]}): layers {state}, "
          f"{sum(counts.values())} cells, each profile its own cell until linking (M7-13)")  # fmt: skip


def _start(root: zarr.Group, manifest: Manifest) -> tuple[float, float, float] | None:
    """Start of the view (volume µm): the centre of the plane of the first slice with cells."""
    layers = dict((manifest.get("cells") or {}).get("layers") or {})
    ids = dict(layers.get("signature") or {}).get("ids")
    attrs: dict[str, Any] = dict(root.attrs)
    slice_ids = dict(attrs.get("brainrec") or {}).get("slice_ids", [])
    if not ids or ids[0] not in slice_ids or "0" not in root:
        return None
    transforms = attrs["ome"]["multiscales"][0]["datasets"][0]["coordinateTransformations"]
    _, sz, sy, sx = next(t["scale"] for t in transforms if t["type"] == "scale")
    level0 = root["0"]
    if not isinstance(level0, zarr.Array):
        return None
    _, _, h, w = level0.shape
    return (w * sx / 2, h * sy / 2, (slice_ids.index(ids[0]) + 0.5) * sz)


def _serve(args: argparse.Namespace, cfg: ProjectConfig, work: WorkDir) -> None:
    if not work.volume.exists():
        raise CliError(f"no volume at {work.volume}: run `brainrec volume` first")
    ome = zarr.open_group(str(work.volume), mode="r").attrs.get("ome")
    if not isinstance(ome, dict):
        raise CliError(f"{work.volume} has no OME-Zarr 0.5 metadata; rebuild it")
    if args.tissue_opacity is not None and not 0.0 <= args.tissue_opacity <= 1.0:
        raise CliError("--tissue-opacity must lie between 0 and 1")
    base = f"http://127.0.0.1:{args.port}"
    root = zarr.open_group(str(work.volume), mode="r")
    cells = [
        CellLayer(t.name, f"{base}/{work.cell_layer(t.name).relative_to(work.root).as_posix()}",
                  t.colour)
        for t in cfg.cell_types
        if (work.cell_layer(t.name) / "info").exists()
    ]  # fmt: skip
    state = neuroglancer_state(
        f"{base}/{work.volume.name}",
        ome,
        layout=args.layout,
        gpu_memory_mb=args.gpu_memory_mb,
        cell_layers=cells,
        tissue_opacity=args.tissue_opacity,
        position_um=_start(root, Manifest(work.manifest)) if cells else None,
    )
    if cells:
        print(f"Cell layers: {', '.join(c.name for c in cells)} (controls in each layer's panel)")
        ids = (Manifest(work.manifest).get("cells") or {}).get("layers", {}).get("signature", {})
        ids = ids.get("ids", [])
        if ids:
            print(f"Cells for slices {ids[0]}-{ids[-1]}; the view starts on slice {ids[0]}. "
                  "Mouse wheel: one section per step.")  # fmt: skip
    viewer = launch_local_viewer(state, args.port + 1)  # keep referenced while serving
    print("Viewer (open in Chrome):", viewer, flush=True)
    print("Fallback, public Neuroglancer build:", hosted_url(state), flush=True)
    uvicorn.run(create_app(work.root), host="127.0.0.1", port=args.port)


COMMANDS = {
    "preprocess": _preprocess,
    "register": _register,
    "qc": _qc,
    "qc-cells": _qc_cells,
    "volume": _volume,
    "detect": _detect,
    "cells": _cells,
    "serve": _serve,
}


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        cfg = _config(args.config)
        COMMANDS[args.cmd](args, cfg, WorkDir(cfg.work_dir))
    except USER_ERRORS as exc:
        sys.exit("error: " + " ".join(str(exc).split()))  # one line, exit status 1
