"""In-memory stand-in for SlideReader: a gray tissue rectangle with a black marker pixel."""

from pathlib import Path

import numpy as np

from brainrec.slides import LevelInfo


class FakeSlide:
    """Level 0 is w x h; levels at downsample 1, 2, 4, 8 are block-means of it."""

    def __init__(self, w: int, h: int, marker: tuple[int, int] = (16, 8)) -> None:
        base = np.full((h, w, 3), 128, np.uint8)
        base[marker[1], marker[0]] = 0
        self.base = base
        self.marker = marker
        self.reads = 0
        self.levels = []
        self._imgs = []
        for i, ds in enumerate((1, 2, 4, 8)):
            img = base[: h // ds * ds, : w // ds * ds]
            img = img.reshape(h // ds, ds, w // ds, ds, 3).min(axis=(1, 3))  # min keeps marker
            self._imgs.append(img)
            self.levels.append(LevelInfo(i, float(ds), img.shape[1], img.shape[0]))

    mpp = (0.25, 0.25)

    def level_for_downsample(self, downsample: float) -> int:
        for lv in self.levels:
            if lv.downsample == downsample:
                return lv.index
        raise ValueError(downsample)

    def read_region(self, level: int, x: int, y: int, w: int, h: int) -> np.ndarray:
        self.reads += 1
        return self._imgs[level][y : y + h, x : x + w].copy()

    def close(self) -> None:
        pass


class FakeFactory:
    """Maps path -> FakeSlide, remembering instances to count reads."""

    def __init__(self, sizes: dict[str, tuple[int, int]]) -> None:
        self.sizes = sizes
        self.opened: list[FakeSlide] = []

    def __call__(self, path: Path) -> FakeSlide:
        s = FakeSlide(*self.sizes[path.name])
        self.opened.append(s)
        return s
