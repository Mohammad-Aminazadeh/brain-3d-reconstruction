"""Discover slice files, order them by the N in `..._N_H ...` and validate completeness."""

import re
from dataclasses import dataclass
from pathlib import Path

_PATTERN = re.compile(r"_(\d+)_H\b")


@dataclass(frozen=True)
class SliceRef:
    id: int
    path: Path


@dataclass(frozen=True)
class Catalog:
    slices: list[SliceRef]

    @classmethod
    def scan(cls, data_dir: Path, id_range: tuple[int, int] | None = None) -> "Catalog":
        """All slices in `data_dir` (or only ids lo..hi inclusive), sorted by id.

        Raises ValueError on duplicate ids or, when `id_range` is given, missing ids.
        """
        found: dict[int, Path] = {}
        for p in data_dir.glob("*.ndpi"):
            m = _PATTERN.search(p.name)
            if m is None:
                continue
            sid = int(m.group(1))
            if sid in found:
                raise ValueError(f"duplicate slice id {sid}: {found[sid].name}, {p.name}")
            found[sid] = p
        if id_range is not None:
            lo, hi = id_range
            missing = [i for i in range(lo, hi + 1) if i not in found]
            if missing:
                raise ValueError(f"missing slice ids: {missing}")
            found = {i: found[i] for i in range(lo, hi + 1)}
        if not found:
            raise ValueError(f"no slices found in {data_dir}")
        return cls([SliceRef(i, found[i]) for i in sorted(found)])
