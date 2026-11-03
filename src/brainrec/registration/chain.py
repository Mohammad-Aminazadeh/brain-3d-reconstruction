"""The `register` stage: pairwise registration of consecutive slices, chained to the first
slice of the range (contracts C2, C5, C6, C7, C12)."""

import hashlib
import itertools
import json
import logging

from brainrec.catalog import Catalog, ProjectConfig
from brainrec.registration.pairwise import register_pair
from brainrec.registration.transform import Transform2D
from brainrec.registration.types import PairResult, RegImage
from brainrec.slides import ReaderFactory, SlideReader
from brainrec.store import Manifest, StageExistsError, StageResult, WorkDir, write_atomic

log = logging.getLogger(__name__)
STAGE = "register"
VERSION = 1  # bump when the algorithm changes in a way that should invalidate old results


class RegistrationFailedError(RuntimeError):
    """A pair could not be registered (C12); nothing is chained past it."""


def chain(pairs: dict[int, Transform2D], ids: list[int]) -> dict[int, Transform2D]:
    """Slice -> reference transforms for `ids` (in order; reference = ids[0]).

    `pairs[k]` maps slice k onto the previous id; T(k -> ref) = T(prev -> ref) ∘ pairs[k] (C2).
    """
    out = {ids[0]: Transform2D.identity()}
    for prev, k in itertools.pairwise(ids):
        if k not in pairs:
            raise KeyError(f"no pair transform for slice {k}")
        out[k] = out[prev].compose(pairs[k])
    return out


def load_chained(work: WorkDir, ids: list[int]) -> dict[int, Transform2D]:
    """Chained transforms of `ids` (in catalog order) from `work`, checked to form one chain.

    A register run chains its range to the range's first slice; runs over other ranges leave
    transforms to other reference slices, which must never be mixed. So every slice after the
    first must satisfy chained[k] = chained[prev] ∘ pair[k] (C2), as within one run.
    Raises FileNotFoundError naming the slices without a chained transform, and ValueError
    naming the first two slices that were not chained together.
    """
    missing = [k for k in ids if not work.chained_transform(k).exists()]
    if missing:
        raise FileNotFoundError(f"slices {missing} have no chained transform")
    out = {k: Transform2D.load(work.chained_transform(k)) for k in ids}
    for prev, k in itertools.pairwise(ids):
        path = work.pair_transform(k)
        pr = PairResult.from_dict(json.loads(path.read_text())) if path.exists() else None
        if pr is None or pr.fixed != prev or not out[k].allclose(out[prev].compose(pr.transform)):
            raise ValueError(f"slices {prev} and {k} were not registered in the same run")
    return out


def _signature(cfg: ProjectConfig) -> dict:
    params = {k: v for k, v in cfg.model_dump().items() if k.startswith("reg_")}
    params["reg_window_um"] = list(params["reg_window_um"])
    return {"version": VERSION, **params, "boundary_min_iou": cfg.boundary_min_iou}


def _pair_inputs(pre: dict, fixed: int, moving: int) -> str:
    """Hash of everything a pair depends on upstream: preprocessing settings and both sources."""
    sources = pre.get("sources", {})
    if str(fixed) not in sources or str(moving) not in sources:
        missing = [i for i in (fixed, moving) if str(i) not in sources]
        raise FileNotFoundError(f"slices {missing} are not preprocessed; run preprocess first")
    blob = json.dumps([pre["signature"], sources[str(fixed)], sources[str(moving)]], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def run_register(
    catalog: Catalog,
    work: WorkDir,
    cfg: ProjectConfig,
    manifest: Manifest,
    reader_factory: ReaderFactory = SlideReader.open,  # unused: works on preprocessed files (C7)
    overwrite: bool = False,
) -> StageResult:
    """Register every consecutive pair of `catalog`, then write chained transforms to its first
    slice. `StageResult.done` / `.skipped` list moving-slice ids of pairs."""
    ids = [s.id for s in catalog.slices]
    signature = _signature(cfg)
    state = manifest.get(STAGE)
    if state is not None and state["signature"] != signature:
        if not overwrite:
            raise StageExistsError(
                f"{STAGE} results were made with other settings; use overwrite to redo them"
            )
        state = None
    inputs: dict[str, str] = dict(state["inputs"]) if state else {}
    pre = manifest.get("preprocess") or {}

    result = StageResult()
    pairs: dict[int, Transform2D] = {}
    failed: PairResult | None = None
    for fixed, moving in itertools.pairwise(ids):
        key = _pair_inputs(pre, fixed, moving)
        path = work.pair_transform(moving)
        if inputs.get(str(moving)) == key and path.exists():
            pr = PairResult.from_dict(json.loads(path.read_text()))
            if pr.fixed == fixed:  # same neighbour as before
                result.skipped.append(moving)
                pairs[moving] = pr.transform
                if pr.status == "failed":
                    failed = pr
                    break
                continue
        pr = register_pair(RegImage.load(work, fixed), RegImage.load(work, moving), cfg)
        write_atomic(path, json.dumps(pr.to_dict(), indent=2).encode())
        inputs[str(moving)] = key
        manifest.set(
            STAGE, {"signature": signature, "done": sorted(map(int, inputs)), "inputs": inputs}
        )
        result.done.append(moving)
        log.info(
            "register %d<-%d: %s, %d inliers, rms %s um, iou %.3f",
            fixed, moving, pr.status, pr.n_inliers, pr.rms_um, pr.mask_iou,
        )  # fmt: skip
        pairs[moving] = pr.transform
        if pr.status == "failed":
            failed = pr
            break

    chained_ids = ids[: ids.index(failed.moving)] if failed else ids
    for k, t in chain(pairs, chained_ids).items():
        write_atomic(work.chained_transform(k), json.dumps(t.to_dict(), indent=2).encode())
    for k in ids[len(chained_ids) :]:  # nothing chained past a failed pair, not even stale files
        work.chained_transform(k).unlink(missing_ok=True)
    if failed:
        raise RegistrationFailedError(
            f"pair {failed.fixed}<-{failed.moving} failed (inliers {failed.n_inliers}, "
            f"mask IoU {failed.mask_iou}); slices {failed.moving} and later are not chained"
        )
    return result
