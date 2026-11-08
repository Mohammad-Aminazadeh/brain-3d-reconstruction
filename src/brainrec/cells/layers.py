"""Neuroglancer precomputed annotation layers of cells (M7-07b; contract C17).

One layer per class: ellipsoids in µm (the volume frame, C16) with the C17 properties, a sharded
by-id index (one file) and a multi-level sharded spatial index (one file per level), so a layer
has about 10 to 20 files on the exFAT drive. Format: Neuroglancer's
`neuroglancer_annotations_v1` with `neuroglancer_uint64_sharded_v1` (identity hash, shard_bits
0, raw encodings). The writer is vectorised for tens of millions of cells; `read_layer` and
`read_by_id` decode it again for the tests.

Spatial index: level i has a grid of [2^i, 2^i, 1] chunks over the cells' bounds. Each chunk
holds at most `limit` cells, drawn at random from those not yet shown on a coarser level; the
finest level (all chunks within the limit, or level 12) takes the rest. Every cell is stored on
exactly one level.
"""

import json
import shutil
from pathlib import Path

import numpy as np

from brainrec.cells.place import Placed

MAX_LEVEL = 12
BY_ID_MINISHARD_BITS = 12
SPATIAL_MINISHARD_BITS = 6
# geometry (centre, radii) then the properties of C17, largest type first, padded to 4 bytes
RECORD = np.dtype(
    [
        ("geom", "<f4", 6),
        ("confidence", "<f4"),
        ("det_conf", "<f4"),
        ("radius_um", "<f4"),
        ("slice", "<u2"),
        ("n_profiles", "<u2"),
        ("nucleolus", "u1"),
        ("blurred", "u1"),
        ("pad", "u1", 2),
    ]
)
PROPERTIES = [
    {"id": "confidence", "type": "float32", "description": "top class probability"},
    {"id": "det_conf", "type": "float32", "description": "detection confidence (NaN: none)"},
    {"id": "radius_um", "type": "float32", "description": "sphere radius in µm"},
    {"id": "slice", "type": "uint16", "description": "slice of the representative profile"},
    {"id": "n_profiles", "type": "uint16", "description": "linked profiles"},
    {"id": "nucleolus", "type": "uint8", "enum_values": [0, 1], "enum_labels": ["no", "yes"]},
    {"id": "blurred", "type": "uint8", "enum_values": [0, 1], "enum_labels": ["no", "yes"]},
]
SHARDING = {"@type": "neuroglancer_uint64_sharded_v1", "preshift_bits": 0, "hash": "identity",
            "shard_bits": 0, "minishard_index_encoding": "raw", "data_encoding": "raw"}  # fmt: skip


def records(placed: Placed) -> np.ndarray:
    """The annotation records (RECORD) of `placed`, in its order."""
    rec = np.zeros(len(placed), RECORD)
    rec["geom"][:, :3], rec["geom"][:, 3:] = placed.centre, placed.radii
    for name in ("confidence", "det_conf", "radius_um", "n_profiles", "nucleolus", "blurred"):
        rec[name] = getattr(placed, name)
    rec["slice"] = placed.slice_id
    return rec


def write_shard(path: Path, keys: np.ndarray, chunks: np.ndarray | list[bytes],
                minishard_bits: int) -> dict:  # fmt: skip
    """One shard file (shard_bits 0, identity hash, raw encodings) with a chunk per key.
    `chunks` is (n, size) uint8 for chunks of one size, or a list of bytes. Returns the
    sharding spec for the info file. Keys must be unique."""
    keys = np.asarray(keys, np.uint64)
    n_mini = 1 << minishard_bits
    mask = np.uint64(n_mini - 1)
    mini = keys & mask
    order = np.lexsort((keys, mini))
    keys, mini = keys[order], mini[order].astype(np.int64)
    if isinstance(chunks, np.ndarray):
        data = np.ascontiguousarray(chunks[order]).tobytes()
        sizes = np.full(len(keys), chunks.shape[1], np.uint64)
    else:
        data = b"".join(chunks[i] for i in order)
        sizes = np.array([len(chunks[i]) for i in order], np.uint64)
    offsets = np.concatenate([[0], np.cumsum(sizes)[:-1]]).astype(np.uint64)
    pos = np.uint64(len(data))
    starts = np.searchsorted(mini, np.arange(n_mini), "left")
    ends = np.searchsorted(mini, np.arange(n_mini), "right")
    shard_index = np.zeros((n_mini, 2), np.uint64)
    parts = []
    for m in range(n_mini):
        a, b = starts[m], ends[m]
        if a == b:
            shard_index[m] = (pos, pos)
            continue
        k, off, size = keys[a:b], offsets[a:b], sizes[a:b]
        d_keys = np.diff(k, prepend=np.uint64(0))
        # offsets are relative to the end of the previous chunk of the minishard (the first:
        # to the start of the data); the chunks of a minishard are stored one after another
        prev_end = np.concatenate([[np.uint64(0)], off[:-1] + size[:-1]]).astype(np.uint64)
        arr = np.stack([d_keys, off - prev_end, size]).astype("<u8").tobytes()
        shard_index[m] = (pos, pos + np.uint64(len(arr)))
        parts.append(arr)
        pos += np.uint64(len(arr))
    with open(path, "wb") as f:
        f.write(shard_index.astype("<u8").tobytes())
        f.write(data)
        f.writelines(parts)
    return {**SHARDING, "minishard_bits": minishard_bits}


def _morton(pos: np.ndarray, grid: list[int]) -> np.ndarray:
    """Compressed Morton code of grid positions (n, 3), as in Neuroglancer's sharded format."""
    bits = [max(0, int(np.ceil(np.log2(g)))) for g in grid]
    code = np.zeros(len(pos), np.uint64)
    out_bit = 0
    for i in range(max(bits)):
        for d, nb in enumerate(bits):
            if i < nb:
                bit = (pos[:, d].astype(np.uint64) >> np.uint64(i)) & np.uint64(1)
                code |= bit << np.uint64(out_bit)
                out_bit += 1
    return code


def write_layer(
    out: Path,
    placed: Placed,
    bounds: tuple[np.ndarray, np.ndarray] | None = None,
    limit: int = 4000,
    seed: int = 0,
) -> None:
    """Write one ellipsoid layer to `out` (replaced if it exists; written to `out.tmp` and
    renamed, so an interrupted write leaves the old layer or none). `bounds` (lower, upper µm)
    defaults to the extent of the cells; layers of one volume should share it."""
    rec = records(placed)
    ids = placed.cell_id.astype(np.uint64)
    if len(np.unique(ids)) != len(ids):
        raise ValueError("cell ids must be unique within a layer")
    if bounds is None:
        if len(placed):
            bounds = ((placed.centre - placed.radii).min(0), (placed.centre + placed.radii).max(0))
        else:
            bounds = (np.zeros(3), np.ones(3))
    lo, hi = np.asarray(bounds[0], float), np.asarray(bounds[1], float)
    extent = np.maximum(hi - lo, 1e-3)
    tmp = out.with_name(out.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    (tmp / "by_id").mkdir(parents=True)
    info: dict = {
        "@type": "neuroglancer_annotations_v1",
        "dimensions": {d: [1e-6, "m"] for d in ("x", "y", "z")},
        "lower_bound": lo.tolist(),
        "upper_bound": hi.tolist(),
        "annotation_type": "ELLIPSOID",
        "properties": PROPERTIES,
        "relationships": [],
        "by_id": {"key": "by_id"},
        "spatial": [],
    }
    info["by_id"]["sharding"] = write_shard(
        tmp / "by_id" / "0.shard", ids, rec.view(np.uint8).reshape(len(rec), RECORD.itemsize),
        BY_ID_MINISHARD_BITS,
    )  # fmt: skip
    remaining = np.random.default_rng(seed).permutation(len(ids))
    level = 0
    while True:
        grid = [2**level, 2**level, 1]
        cell = extent / np.array(grid)
        pos = np.clip(((placed.centre[remaining] - lo) / cell).astype(np.int64), 0,
                      np.array(grid) - 1)  # fmt: skip
        key = _morton(pos, grid)
        order = np.argsort(key, kind="stable")  # keeps the random order within a chunk
        uniq, first, counts = np.unique(key[order], return_index=True, return_counts=True)
        finest = len(remaining) == 0 or counts.max() <= limit or level >= MAX_LEVEL
        take = np.zeros(len(remaining), bool)
        chunks = []
        for k0, c in zip(first, counts, strict=True):
            sel = order[k0 : k0 + (c if finest else min(c, limit))]
            take[sel] = True
            idx = remaining[sel]
            chunks.append(np.uint64(len(idx)).tobytes() + rec[idx].tobytes()
                          + ids[idx].astype("<u8").tobytes())  # fmt: skip
        name = f"spatial{level}"
        (tmp / name).mkdir()
        sharding = write_shard(tmp / name / "0.shard", uniq, chunks, SPATIAL_MINISHARD_BITS)
        info["spatial"].append({"key": name, "grid_shape": grid, "chunk_size": cell.tolist(),
                                "limit": limit, "sharding": sharding})  # fmt: skip
        remaining = remaining[~take]
        if finest:
            break
        level += 1
    (tmp / "info").write_text(json.dumps(info))
    if out.exists():
        shutil.rmtree(out)
    tmp.rename(out)


# --- reading (for the tests) ---------------------------------------------------------------


def _minishard(raw: bytes, n_mini: int, m: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Keys, data offsets (from the start of the data) and sizes listed in minishard `m`."""
    start, end = (int(v) for v in np.frombuffer(raw, "<u8", 2, 16 * m))
    if start == end:
        return np.zeros(0, np.uint64), np.zeros(0, np.uint64), np.zeros(0, np.uint64)
    arr = np.frombuffer(raw[16 * n_mini + start : 16 * n_mini + end], "<u8").reshape(3, -1)
    offs = np.cumsum(arr[1]) + np.concatenate([[0], np.cumsum(arr[2])[:-1]]).astype(np.uint64)
    return np.cumsum(arr[0]), offs, arr[2]


def _read_shard(path: Path, minishard_bits: int) -> dict[int, bytes]:
    """All chunks of a shard file: key -> bytes. ValueError if a key is listed in a minishard
    other than its own (key & (2^bits - 1)), where a viewer would not find it."""
    raw = path.read_bytes()
    n_mini = 1 << minishard_bits
    base = 16 * n_mini
    out: dict[int, bytes] = {}
    for m in range(n_mini):
        keys, offs, sizes = _minishard(raw, n_mini, m)
        if np.any((keys & np.uint64(n_mini - 1)) != m):
            raise ValueError(f"{path}: minishard {m} lists keys of other minishards")
        for k, o, n in zip(keys, offs, sizes, strict=True):
            out[int(k)] = raw[base + int(o) : base + int(o) + int(n)]
    return out


def read_by_id(layer: Path, cell_id: int) -> np.void | None:
    """The record of one cell, looked up as a viewer does: only in the minishard of its id."""
    info = json.loads((layer / "info").read_text())
    n_mini = 1 << info["by_id"]["sharding"]["minishard_bits"]
    raw = (layer / "by_id" / "0.shard").read_bytes()
    keys, offs, _ = _minishard(raw, n_mini, int(cell_id) & (n_mini - 1))
    hit = np.nonzero(keys == np.uint64(cell_id))[0]
    if not len(hit):
        return None
    o = 16 * n_mini + int(offs[hit[0]])
    return np.frombuffer(raw[o : o + RECORD.itemsize], RECORD)[0]


def read_layer(layer: Path) -> tuple[dict, list[tuple[np.ndarray, np.ndarray, np.ndarray]]]:
    """Info and, per spatial level, (chunk keys of the cells, records, cell ids)."""
    info = json.loads((layer / "info").read_text())
    levels = []
    for sp in info["spatial"]:
        chunks = _read_shard(layer / sp["key"] / "0.shard", sp["sharding"]["minishard_bits"])
        keys, recs, ids = [], [], []
        for k, b in chunks.items():
            n = int(np.frombuffer(b, "<u8", 1)[0])
            r = np.frombuffer(b, RECORD, n, 8)
            keys.append(np.full(n, k, np.uint64))
            recs.append(r)
            ids.append(np.frombuffer(b, "<u8", n, 8 + n * RECORD.itemsize))
        levels.append((
            np.concatenate(keys) if keys else np.zeros(0, np.uint64),
            np.concatenate(recs) if recs else np.zeros(0, RECORD),
            np.concatenate(ids) if ids else np.zeros(0, np.uint64),
        ))  # fmt: skip
    return info, levels
