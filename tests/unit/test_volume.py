import json

import pytest
import zarr

from brainrec.catalog import Catalog
from brainrec.store import Manifest
from brainrec.volume import VolumeExistsError, VolumeSpec, aligned_bands, build_volume
from tests.synthetic.fake_slide import FakeFactory, FakeSlide

SIZES = {"s_1_H.ndpi": (64, 48), "s_2_H.ndpi": (48, 32), "s_3_H.ndpi": (32, 32)}
# Small chunk/shard sizes so slices straddle shard boundaries.
SPEC = VolumeSpec(z_spacing_um=7.0, base_downsample=1, n_levels=3, chunk_xy=8, shard_xy=16)


def _catalog(tmp_path):
    for n in SIZES:
        (tmp_path / n).write_text("")
    return Catalog.scan(tmp_path)


def _build(tmp_path, spec=SPEC, factory=None, manifest=None, overwrite=False):
    return build_volume(_catalog(tmp_path), tmp_path / "v.zarr", spec,
                        manifest or Manifest(tmp_path / "m.json"),
                        factory or FakeFactory(SIZES), overwrite=overwrite)


def test_aligned_bands_split_at_multiples():
    assert aligned_bands(4, 40, 16) == [(4, 16), (16, 32), (32, 40)]
    assert aligned_bands(0, 32, 16) == [(0, 16), (16, 32)]
    assert aligned_bands(5, 5, 16) == []


def test_shard_must_be_multiple_of_chunk():
    with pytest.raises(ValueError):
        VolumeSpec(z_spacing_um=1.0, chunk_xy=512, shard_xy=1000)


def test_shapes_and_centred_placement(tmp_path):
    g = zarr.open_group(str(_build(tmp_path)), mode="r")
    assert g["0"].shape == (3, 3, 48, 64)
    assert g["1"].shape == (3, 3, 24, 32)
    assert g["2"].shape == (3, 3, 12, 16)
    ox, oy = g.attrs["brainrec"]["offsets_base_px"]["2"]
    assert (ox, oy) == (8, 8)  # centred: (64-48)//2 and (48-32)//2, both multiples of 4
    vol = g["0"][:, 1]
    assert (vol[:, oy + 8, ox + 16] == 0).all()  # slice 2's marker at its own (16, 8)
    assert (vol[:, oy + 5, ox + 5] == 128).all()  # tissue
    assert (vol[:, 0, 0] == 255).all()  # outside the slice: white background


def test_full_slice_content_matches_source(tmp_path):
    g = zarr.open_group(str(_build(tmp_path)), mode="r")
    for zi, (name, (w, h)) in enumerate(SIZES.items()):
        ox, oy = g.attrs["brainrec"]["offsets_base_px"][str(zi + 1)]
        expected = FakeSlide(w, h).base.transpose(2, 0, 1)
        assert (g["0"][:, zi, oy : oy + h, ox : ox + w] == expected).all(), name


def test_levels_stay_aligned(tmp_path):
    g = zarr.open_group(str(_build(tmp_path)), mode="r")
    for zi, sid in enumerate((1, 2, 3)):
        ox, oy = g.attrs["brainrec"]["offsets_base_px"][str(sid)]
        for k in range(3):
            f = 2**k
            assert g[str(k)][0, zi, (oy + 8) // f, (ox + 16) // f] == 0  # marker at every level


def test_ome_zarr_05_metadata_and_sharding(tmp_path):
    out = _build(tmp_path)
    ome = zarr.open_group(str(out), mode="r").attrs["ome"]
    assert ome["version"] == "0.5"
    ms = ome["multiscales"][0]
    assert [a["name"] for a in ms["axes"]] == ["c", "z", "y", "x"]
    assert ms["datasets"][0]["coordinateTransformations"][0]["scale"] == [1.0, 7.0, 0.25, 0.25]
    assert ms["datasets"][2]["coordinateTransformations"][0]["scale"] == [1.0, 7.0, 1.0, 1.0]
    meta = json.loads((out / "0" / "zarr.json").read_text())
    assert meta["zarr_format"] == 3
    assert meta["codecs"][0]["name"] == "sharding_indexed"


def test_resume_skips_finished_slices(tmp_path):
    _build(tmp_path)
    f2 = FakeFactory(SIZES)
    _build(tmp_path, factory=f2)
    assert sum(s.reads for s in f2.opened) == 0


def test_resume_after_interruption_completes(tmp_path):
    m = Manifest(tmp_path / "m.json")
    _build(tmp_path, manifest=m)
    state = m.get("volume")
    state["done"] = [1]  # pretend slices 2 and 3 never finished
    m.set("volume", state)
    f2 = FakeFactory(SIZES)
    out = _build(tmp_path, factory=f2)
    assert sum(s.reads for s in f2.opened) > 0
    assert (zarr.open_group(str(out), mode="r")["0"][:, 2, 16, 16] != 255).any()


def test_changed_spec_refuses_without_overwrite(tmp_path):
    _build(tmp_path)
    spec2 = VolumeSpec(z_spacing_um=9.0, base_downsample=1, n_levels=3, chunk_xy=8, shard_xy=16)
    with pytest.raises(VolumeExistsError):
        _build(tmp_path, spec=spec2)
    # the existing volume is untouched
    ome = zarr.open_group(str(tmp_path / "v.zarr"), mode="r").attrs["ome"]
    assert ome["multiscales"][0]["datasets"][0]["coordinateTransformations"][0]["scale"][1] == 7.0


def test_changed_spec_rebuilds_with_overwrite(tmp_path):
    _build(tmp_path)
    spec2 = VolumeSpec(z_spacing_um=9.0, base_downsample=1, n_levels=3, chunk_xy=8, shard_xy=16)
    f2 = FakeFactory(SIZES)
    _build(tmp_path, spec=spec2, factory=f2, overwrite=True)
    assert sum(s.reads for s in f2.opened) > 0
    ome = zarr.open_group(str(tmp_path / "v.zarr"), mode="r").attrs["ome"]
    assert ome["multiscales"][0]["datasets"][0]["coordinateTransformations"][0]["scale"][1] == 9.0


def test_existing_volume_without_manifest_is_protected(tmp_path):
    _build(tmp_path)
    (tmp_path / "m.json").unlink()
    with pytest.raises(VolumeExistsError):
        _build(tmp_path)


def test_mixed_pixel_sizes_rejected(tmp_path):
    class OddFactory(FakeFactory):
        def __call__(self, path):
            s = super().__call__(path)
            if path.name.startswith("s_2"):
                s.mpp = (0.5, 0.5)
            return s

    with pytest.raises(ValueError, match="pixel sizes"):
        _build(tmp_path, factory=OddFactory(SIZES))
