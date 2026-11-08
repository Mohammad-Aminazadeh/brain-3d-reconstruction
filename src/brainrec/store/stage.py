"""Shared pieces of resumable stages (contracts C6, C7)."""

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class StageResult:
    done: list[int] = field(default_factory=list)  # ids processed in this run
    skipped: list[int] = field(default_factory=list)  # ids already up to date
    left: list[int] = field(default_factory=list)  # ids not started: the run was stopped (M7-06)


class StageExistsError(RuntimeError):
    """Earlier results of a stage were made with other settings; pass overwrite to replace."""


def fingerprint(path: Path) -> list:
    """Identity of a source file for resume checks: name, size, modification time (ns)."""
    st = path.stat()
    return [path.name, st.st_size, st.st_mtime_ns]


def write_atomic(path: Path, data: bytes) -> None:
    """Write via a temporary file and rename, so an interrupted run never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
