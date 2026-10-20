"""Preprocessing stage: one read of the registration level per slice -> mask + registration
image + metadata in the work directory (contracts C4, C6, C7)."""

import json
import logging

import cv2
import numpy as np

from brainrec.catalog import Catalog, ProjectConfig, SliceRef
from brainrec.preprocess.mask import make_mask
from brainrec.preprocess.regimage import make_reg_image
from brainrec.registration.types import REGIMAGE_SCHEMA, RegImage
from brainrec.slides import ReaderFactory, SlideReader
from brainrec.store import (
    Manifest,
    StageExistsError,
    StageResult,
    WorkDir,
    fingerprint,
    write_atomic,
)  # fmt: skip

log = logging.getLogger(__name__)
STAGE = "preprocess"
VERSION = 1  # bump when the algorithm changes in a way that should invalidate old results


def _png(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise RuntimeError("PNG encoding failed")
    return buf.tobytes()


def preprocess_slice(
    ref: SliceRef,
    work: WorkDir,
    cfg: ProjectConfig,
    reader_factory: ReaderFactory = SlideReader.open,
) -> RegImage:
    """Read the `cfg.reg_downsample` level once; write mask, registration image and metadata."""
    reader = reader_factory(ref.path)
    try:
        level = reader.level_for_downsample(cfg.reg_downsample)
        lv = reader.levels[level]
        rgb = reader.read_region(level, 0, 0, lv.width, lv.height)
        mpp = reader.mpp
    finally:
        reader.close()
    mask = make_mask(rgb, mpp[0] * cfg.reg_downsample, cfg)
    image = make_reg_image(rgb, mask)
    meta = {
        "schema": REGIMAGE_SCHEMA,
        "slice_id": ref.id,
        "source": ref.path.name,
        "downsample": float(cfg.reg_downsample),
        "mpp_um": list(mpp),
        "width": lv.width,
        "height": lv.height,
    }
    write_atomic(work.mask(ref.id), _png(mask))
    write_atomic(work.reg_image(ref.id), _png(image))
    write_atomic(work.reg_meta(ref.id), json.dumps(meta, indent=2).encode())  # written last
    return RegImage(ref.id, image, mask, float(cfg.reg_downsample), (mpp[0], mpp[1]))


def _signature(cfg: ProjectConfig) -> dict:
    return {
        "version": VERSION,
        "reg_downsample": cfg.reg_downsample,
        "mask_hysteresis": cfg.mask_hysteresis,
        "mask_min_component_mm2": cfg.mask_min_component_mm2,
    }


def run_preprocess(
    catalog: Catalog,
    work: WorkDir,
    cfg: ProjectConfig,
    manifest: Manifest,
    reader_factory: ReaderFactory = SlideReader.open,
    overwrite: bool = False,
) -> StageResult:
    """Preprocess every slice of `catalog`, skipping slices already done with the same settings
    and an unchanged source file. Settings changed since the last run: StageExistsError unless
    `overwrite` (then everything is redone)."""
    signature = _signature(cfg)
    state = manifest.get(STAGE)
    if state is not None and state["signature"] != signature:
        if not overwrite:
            raise StageExistsError(
                f"{STAGE} results were made with other settings {state['signature']}; "
                "use overwrite to redo them"
            )
        state = None
    sources: dict[str, list] = dict(state["sources"]) if state else {}
    result = StageResult()
    for ref in catalog.slices:
        fp = fingerprint(ref.path)
        files = (work.mask(ref.id), work.reg_image(ref.id), work.reg_meta(ref.id))
        if sources.get(str(ref.id)) == fp and all(f.exists() for f in files):
            result.skipped.append(ref.id)
            continue
        preprocess_slice(ref, work, cfg, reader_factory)
        sources[str(ref.id)] = fp
        manifest.set(
            STAGE, {"signature": signature, "done": sorted(map(int, sources)), "sources": sources}
        )
        result.done.append(ref.id)
        log.info("preprocess: slice %d done", ref.id)
    return result
