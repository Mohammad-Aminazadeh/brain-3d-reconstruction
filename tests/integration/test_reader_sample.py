import glob
from pathlib import Path

import pytest

from brainrec.slides import SlideReader

SAMPLE = glob.glob(str(Path(__file__).parents[2] / "Data Sample" / "*.ndpi"))

pytestmark = pytest.mark.skipif(not SAMPLE, reason="sample NDPI not present")


def test_sample_metadata_and_region():
    r = SlideReader.open(Path(SAMPLE[0]))
    try:
        assert (r.levels[0].width, r.levels[0].height) == (139776, 102144)
        assert abs(r.mpp[0] - 0.2205) < 1e-3
        lvl = r.level_for_downsample(4)
        assert r.levels[lvl].width == 34944
        region = r.read_region(lvl, 100, 200, 64, 32)
        assert region.shape == (32, 64, 3) and region.dtype == "uint8"
    finally:
        r.close()
