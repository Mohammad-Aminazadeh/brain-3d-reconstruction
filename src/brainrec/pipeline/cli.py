"""Interim CLI for M2: `brainrec volume` and `brainrec serve`. `brainrec run` arrives in M5."""

import argparse
import logging
import sys
from pathlib import Path

import uvicorn
import zarr

from brainrec.catalog import Catalog, ProjectConfig
from brainrec.serve import create_app, hosted_url, launch_local_viewer, neuroglancer_state
from brainrec.store import Manifest, WorkDir
from brainrec.volume import VolumeExistsError, VolumeSpec, build_volume


def _range(text: str) -> tuple[int, int]:
    lo, _, hi = text.partition("-")
    return int(lo), int(hi or lo)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="brainrec")
    p.add_argument("--config", type=Path, required=True)
    sub = p.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("volume", help="stack slices into an OME-Zarr volume (no registration)")
    v.add_argument("--slices", type=_range, required=True, help="e.g. 1-10")
    v.add_argument("--overwrite", action="store_true",
                   help="replace an existing volume built with other settings")
    s = sub.add_parser("serve", help="serve the volume and open it in a local Neuroglancer")
    s.add_argument("--port", type=int, default=8000, help="data port; the viewer uses port+1")
    s.add_argument("--layout", default="xy", choices=["xy", "xz", "yz", "3d", "4panel"],
                   help="initial panel layout (4panel is the heaviest)")
    s.add_argument("--gpu-memory-mb", type=int, default=512)
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    cfg = ProjectConfig.load(args.config)
    work = WorkDir(cfg.work_dir)
    if args.cmd == "volume":
        catalog = Catalog.scan(cfg.data_dir, args.slices)
        spec = VolumeSpec(z_spacing_um=cfg.z_spacing_um, base_downsample=cfg.base_downsample,
                          n_levels=cfg.n_levels)
        try:
            print(build_volume(catalog, work.volume, spec, Manifest(work.manifest),
                               overwrite=args.overwrite))
        except VolumeExistsError as exc:
            sys.exit(f"error: {exc}")
    else:
        ome = zarr.open_group(str(work.volume), mode="r").attrs.get("ome")
        if not isinstance(ome, dict):
            sys.exit(f"error: {work.volume} has no OME-Zarr 0.5 metadata; rebuild it")
        state = neuroglancer_state(f"http://127.0.0.1:{args.port}/{work.volume.name}", ome,
                                   layout=args.layout, gpu_memory_mb=args.gpu_memory_mb)
        viewer = launch_local_viewer(state, args.port + 1)  # keep referenced while serving
        print("Viewer (open in Chrome):", viewer, flush=True)
        print("Fallback, public Neuroglancer build:", hosted_url(state), flush=True)
        uvicorn.run(create_app(work.root), host="127.0.0.1", port=args.port)
