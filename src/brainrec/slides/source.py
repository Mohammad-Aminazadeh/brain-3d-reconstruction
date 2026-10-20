"""What the pipeline needs from a slide: `SlideReader` satisfies it, tests use fakes."""

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import numpy as np

from brainrec.slides.reader import LevelInfo


class SliceSource(Protocol):
    levels: list[LevelInfo]

    @property
    def mpp(self) -> tuple[float, float]: ...
    def level_for_downsample(self, downsample: float) -> int: ...
    def read_region(self, level: int, x: int, y: int, w: int, h: int) -> np.ndarray: ...
    def close(self) -> None: ...


ReaderFactory = Callable[[Path], SliceSource]
