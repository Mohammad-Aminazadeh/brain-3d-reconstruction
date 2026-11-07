"""Interim CLI until `brainrec run` arrives in M5 (M2-08, M3-10, M4-04): `preprocess`,
`register`, `qc` and `volume` for a slice range, and `serve`.

Mistakes in the input, the settings or the environment end with a one-line `error: ...` and exit
status 1; anything else is a bug and keeps its traceback.
"""

import argparse
import itertools
import json
import logging
import sys
from pathlib import Path

import uvicorn
import yaml
import zarr
from pydantic import ValidationError

from brainrec.catalog import Catalog, ProjectConfig
from brainrec.preprocess import run_preprocess
from brainrec.qc import run_qc
from brainrec.registration import PairResult, Transform2D
from brainrec.registration.chain import load_chained, run_register
from brainrec.serve import create_app, hosted_url, launch_local_viewer, neuroglancer_state
from brainrec.store import Manifest, StageExistsError, StageResult, WorkDir
from brainrec.volume import VolumeExistsError, VolumeSpec, build_volume


class CliError(Exception):
    """A mistake in the input, the settings or the environment."""


# never bugs: reported in one line, without a traceback
USER_ERRORS = (CliError, OSError, StageExistsError, VolumeExistsError)


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
    v = stage("volume", "registered, tissue-only volume", "replace a volume of other inputs")
    v.add_argument("--unregistered", action="store_true", help="stack whole scans unaligned (M2)")
    s = sub.add_parser("serve", help="serve the volume and open it in a local Neuroglancer")
    s.add_argument("--port", type=int, default=8000, help="data port; the viewer uses port+1")
    s.add_argument(
        "--layout",
        default="xy",
        choices=["xy", "xz", "yz", "3d", "4panel"],
        help="initial panel layout (4panel is the heaviest)",
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


def _serve(args: argparse.Namespace, cfg: ProjectConfig, work: WorkDir) -> None:
    if not work.volume.exists():
        raise CliError(f"no volume at {work.volume}: run `brainrec volume` first")
    ome = zarr.open_group(str(work.volume), mode="r").attrs.get("ome")
    if not isinstance(ome, dict):
        raise CliError(f"{work.volume} has no OME-Zarr 0.5 metadata; rebuild it")
    state = neuroglancer_state(
        f"http://127.0.0.1:{args.port}/{work.volume.name}",
        ome,
        layout=args.layout,
        gpu_memory_mb=args.gpu_memory_mb,
    )
    viewer = launch_local_viewer(state, args.port + 1)  # keep referenced while serving
    print("Viewer (open in Chrome):", viewer, flush=True)
    print("Fallback, public Neuroglancer build:", hosted_url(state), flush=True)
    uvicorn.run(create_app(work.root), host="127.0.0.1", port=args.port)


COMMANDS = {
    "preprocess": _preprocess,
    "register": _register,
    "qc": _qc,
    "volume": _volume,
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
