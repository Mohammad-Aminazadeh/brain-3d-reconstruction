"""Stack slices into a multi-resolution OME-Zarr volume (M2: no registration, slices centred).

Layout: OME-Zarr 0.5 on Zarr v3, axes (c, z, y, x), uint8, one array per pyramid level named
"0", "1", ... Chunks are grouped into shard files (few large files instead of many small ones:
the external drive is exFAT with 512 KB blocks). Level k is read from the slide's own pyramid at
downsample `base_downsample * 2**k`, so no resampling is done here. Slices are placed centred on
a shared canvas; offsets are multiples of 2**(n_levels-1) base pixels so all levels stay aligned.
"""

import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

import numpy as np
import zarr
from zarr.codecs import ZstdCodec

from brainrec.catalog import Catalog
from brainrec.slides import LevelInfo, SlideReader
from brainrec.store import Manifest

log = logging.getLogger(__name__)

FORMAT = "ome-zarr-0.5-sharded"


class SliceSource(Protocol):
    levels: list[LevelInfo]

    @property
    def mpp(self) -> tuple[float, float]: ...
    def level_for_downsample(self, downsample: float) -> int: ...
    def read_region(self, level: int, x: int, y: int, w: int, h: int) -> np.ndarray: ...
    def close(self) -> None: ...


class VolumeExistsError(RuntimeError):
    """An existing volume was built with different settings and would be overwritten."""


@dataclass(frozen=True)
class VolumeSpec:
    z_spacing_um: float
    base_downsample: int = 4
    n_levels: int = 5
    chunk_xy: int = 512  # unit fetched by the viewer
    shard_xy: int = 4096  # unit stored as one file; also the band height read at once

    def __post_init__(self) -> None:
        if self.shard_xy % self.chunk_xy:
            raise ValueError("shard_xy must be a multiple of chunk_xy")


def _roundup(v: int, m: int) -> int:
    return -(-v // m) * m


def aligned_bands(start: int, stop: int, step: int) -> list[tuple[int, int]]:
    """Split [start, stop) at multiples of `step`, so no band crosses a shard boundary."""
    bands = []
    y = start
    while y < stop:
        end = min(stop, (y // step + 1) * step)
        bands.append((y, end))
        y = end
    return bands


def _grid(dim: int, chunk_xy: int, shard_xy: int) -> tuple[int, int]:
    """(chunk, shard) edge for one axis; the shard is always a multiple of the chunk."""
    if dim <= chunk_xy:
        return dim, dim
    return chunk_xy, min(shard_xy, _roundup(dim, chunk_xy))


def build_volume(
    catalog: Catalog,
    out: Path,
    spec: VolumeSpec,
    manifest: Manifest | None = None,
    reader_factory: Callable[[Path], SliceSource] = SlideReader.open,
    overwrite: bool = False,
) -> Path:
    """Write `out` (an OME-Zarr directory) and return it.

    Resumes per slice via `manifest` when the settings are unchanged. If `out` exists but was
    built with other settings, raises VolumeExistsError unless `overwrite` is True.
    """
    factors = [spec.base_downsample * 2**k for k in range(spec.n_levels)]

    # Pass 1: headers only.
    dims: dict[int, list[tuple[int, int]]] = {}
    mpps: set[tuple[float, float]] = set()
    for s in catalog.slices:
        r = reader_factory(s.path)
        try:
            lv = [r.levels[r.level_for_downsample(f)] for f in factors]
            dims[s.id] = [(x.width, x.height) for x in lv]
            mpps.add(r.mpp)
        finally:
            r.close()
    if len(mpps) != 1:
        raise ValueError(f"slices have different pixel sizes: {sorted(mpps)}")
    mpp = mpps.pop()

    align = 2 ** (spec.n_levels - 1)
    W0 = _roundup(max(d[0][0] for d in dims.values()), align)
    H0 = _roundup(max(d[0][1] for d in dims.values()), align)
    offsets = {
        i: (((W0 - d[0][0]) // 2) // align * align, ((H0 - d[0][1]) // 2) // align * align)
        for i, d in dims.items()
    }
    ids = [s.id for s in catalog.slices]
    signature = {"format": FORMAT, "spec": asdict(spec), "ids": ids, "canvas": [W0, H0]}

    state = manifest.get("volume") if manifest else None
    resume = state is not None and state["signature"] == signature and out.exists()
    if out.exists() and not resume and not overwrite:
        raise VolumeExistsError(
            f"{out} exists with different settings or unknown state; use overwrite to replace it"
        )
    done: list[int] = list(state["done"]) if resume and state else []

    root = zarr.open_group(str(out), mode="r+" if resume else "w", zarr_format=3)
    if not resume:
        if manifest:
            manifest.set("volume", {"signature": signature, "done": []})
        for k in range(spec.n_levels):
            h, w = H0 // 2**k, W0 // 2**k
            (cy, sy), (cx, sx) = (
                _grid(h, spec.chunk_xy, spec.shard_xy),
                _grid(w, spec.chunk_xy, spec.shard_xy),
            )
            root.create_array(
                str(k),
                shape=(3, len(ids), h, w),
                chunks=(3, 1, cy, cx),
                shards=(3, 1, sy, sx),
                dtype="uint8",
                fill_value=255,
                compressors=ZstdCodec(level=3),
            )
        root.attrs["ome"] = {
            "version": "0.5",
            "multiscales": [
                {
                    "name": "volume",
                    "axes": [
                        {"name": "c", "type": "channel"},
                        {"name": "z", "type": "space", "unit": "micrometer"},
                        {"name": "y", "type": "space", "unit": "micrometer"},
                        {"name": "x", "type": "space", "unit": "micrometer"},
                    ],
                    "datasets": [
                        {
                            "path": str(k),
                            "coordinateTransformations": [
                                {
                                    "type": "scale",
                                    "scale": [1.0, spec.z_spacing_um, mpp[1] * f, mpp[0] * f],
                                }
                            ],
                        }
                        for k, f in enumerate(factors)
                    ],
                }
            ],
        }
        root.attrs["brainrec"] = {
            "slice_ids": ids,
            "offsets_base_px": {str(i): list(o) for i, o in offsets.items()},
        }

    for zi, s in enumerate(catalog.slices):
        if s.id in done:
            log.info("slice %d: already done, skipping", s.id)
            continue
        r = reader_factory(s.path)
        try:
            for k, f in enumerate(factors):
                arr = root[str(k)]
                assert isinstance(arr, zarr.Array) and arr.shards is not None
                lvl = r.level_for_downsample(f)
                w, h = dims[s.id][k]
                ox, oy = offsets[s.id][0] // 2**k, offsets[s.id][1] // 2**k
                w = min(w, arr.shape[3] - ox)
                h = min(h, arr.shape[2] - oy)
                for y0, y1 in aligned_bands(oy, oy + h, arr.shards[2]):
                    band = r.read_region(lvl, 0, y0 - oy, w, y1 - y0)
                    arr[:, zi, y0:y1, ox : ox + w] = band.transpose(2, 0, 1)
                log.info("slice %d level %d done", s.id, k)
        finally:
            r.close()
        done.append(s.id)
        if manifest:
            manifest.set("volume", {"signature": signature, "done": done})
    return out
