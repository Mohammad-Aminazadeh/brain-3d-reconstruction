import functools

import pytest
import yaml
import zarr

from brainrec.pipeline import cli
from brainrec.store import WorkDir
from brainrec.volume import VolumeSpec, build_volume
from tests.synthetic.fake_slide import FakeFactory

SIZES = {f"s_{i}_H.ndpi": (64, 48) for i in range(1, 11)}


def _config(tmp_path, z_spacing_um=7.0):
    """Ten placeholder slice files and a config for them; returns the config path."""
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    for name in SIZES:
        (data / name).write_text("")
    values = {
        "data_dir": str(data),
        "work_dir": str(tmp_path / "work"),
        "z_spacing_um": z_spacing_um,
        "base_downsample": 1,
        "n_levels": 2,
    }
    path = tmp_path / "project.yaml"
    path.write_text(yaml.safe_dump(values))
    return path


def _volume(cfg, *args):
    cli.main(["--config", str(cfg), "volume", *args])


def _record(monkeypatch):
    """Replace the volume builder by a recorder of its arguments."""
    calls = []

    def fake(catalog, out, spec, manifest, overwrite):
        ids = [s.id for s in catalog.slices]
        calls.append({"ids": ids, "out": out, "spec": spec, "overwrite": overwrite})
        return out

    monkeypatch.setattr(cli, "build_volume", fake)
    return calls


def _z_spacing(volume):
    ms = zarr.open_group(str(volume), mode="r").attrs["ome"]["multiscales"][0]
    return ms["datasets"][0]["coordinateTransformations"][0]["scale"][1]


@pytest.mark.parametrize(("text", "ids"), [("3-7", [3, 4, 5, 6, 7]), ("5", [5])])
def test_slices_select_inclusive_range(tmp_path, monkeypatch, text, ids):
    calls = _record(monkeypatch)
    _volume(_config(tmp_path), "--slices", text)
    assert [c["ids"] for c in calls] == [ids]


@pytest.mark.parametrize("text", ["abc", "3-x", ""])
def test_malformed_slices_is_usage_error(tmp_path, monkeypatch, capsys, text):
    calls = _record(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        _volume(_config(tmp_path), "--slices", text)
    assert exc.value.code == 2  # argparse usage error
    assert "--slices" in capsys.readouterr().err
    assert not calls


def test_volume_uses_config_and_work_dir(tmp_path, monkeypatch, capsys):
    calls = _record(monkeypatch)
    _volume(_config(tmp_path), "--slices", "1-2", "--overwrite")
    (call,) = calls
    assert call["spec"] == VolumeSpec(z_spacing_um=7.0, base_downsample=1, n_levels=2)
    assert call["out"] == WorkDir(tmp_path / "work").volume
    assert call["overwrite"] is True
    assert capsys.readouterr().out.strip() == str(call["out"])


def test_existing_volume_with_other_settings_exits_with_message(tmp_path, monkeypatch):
    builder = functools.partial(build_volume, reader_factory=FakeFactory(SIZES))
    monkeypatch.setattr(cli, "build_volume", builder)
    volume = WorkDir(tmp_path / "work").volume
    _volume(_config(tmp_path, z_spacing_um=7.0), "--slices", "1-3")
    cfg = _config(tmp_path, z_spacing_um=5.0)  # other settings
    with pytest.raises(SystemExit) as exc:
        _volume(cfg, "--slices", "1-3")
    # SystemExit with a message: Python prints it and exits with status 1, without a traceback
    message = exc.value.code
    assert isinstance(message, str) and message.startswith("error:") and "overwrite" in message
    assert _z_spacing(volume) == 7.0  # the existing volume is kept
    _volume(cfg, "--slices", "1-3", "--overwrite")
    assert _z_spacing(volume) == 5.0


def test_serve_refuses_volume_without_ome_metadata(tmp_path):
    cfg = _config(tmp_path)
    zarr.open_group(str(WorkDir(tmp_path / "work").volume), mode="w", zarr_format=3)
    with pytest.raises(SystemExit) as exc:
        cli.main(["--config", str(cfg), "serve"])
    assert "no OME-Zarr 0.5 metadata" in str(exc.value.code)
