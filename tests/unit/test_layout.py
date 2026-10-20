from pathlib import Path

import pytest

from brainrec.store import WorkDir

W = WorkDir(Path("/work"))
PER_ID = ("reg_image", "reg_meta", "mask", "pair_transform", "chained_transform", "qc_overlay")


def test_names_follow_the_contracts():
    assert W.manifest == Path("/work/manifest.json")
    assert W.volume == Path("/work/volume.zarr")
    assert W.reg_image(7) == Path("/work/reg/7.png")
    assert W.reg_meta(7) == Path("/work/reg/7.json")
    assert W.mask(7) == Path("/work/mask/7.png")
    assert W.pair_transform(7) == Path("/work/transforms/pair_7.json")
    assert W.chained_transform(7) == Path("/work/transforms/chained_7.json")
    assert W.qc_report == Path("/work/qc/report.json")
    assert W.qc_overlay(7) == Path("/work/qc/overlay_7.png")


def test_all_paths_under_root():
    paths = [W.manifest, W.volume, W.qc_report] + [
        getattr(W, n)(i) for n in PER_ID for i in (1, 140)
    ]
    assert all(p.is_relative_to(W.root) for p in paths)


def test_no_collisions_between_ids_or_kinds():
    paths = [getattr(W, n)(i) for n in PER_ID for i in range(1, 141)]
    assert len(set(paths)) == len(paths)  # e.g. 1 vs 11 vs 111 stay distinct


@pytest.mark.parametrize("bad", [0, -3, 1.5, "2", True, None])
def test_invalid_ids_rejected(bad):
    for name in PER_ID:
        with pytest.raises(ValueError, match="positive integer"):
            getattr(W, name)(bad)
