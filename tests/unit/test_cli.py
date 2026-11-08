import functools
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
import zarr

from brainrec.pipeline import cli
from brainrec.preprocess import run_preprocess
from brainrec.registration import PairResult, Transform2D
from brainrec.store import StageResult, WorkDir, write_atomic
from brainrec.volume import VolumeSpec, build_volume
from tests.synthetic.fake_slide import FakeFactory, FakeSlide
from tests.synthetic.tissue import apply_similarity, make_tissue, perturb

SIZES = {f"s_{i}_H.ndpi": (64, 48) for i in range(1, 11)}
MPP = (0.2205, 0.2205)
MOTIONS = {1: (0.0, 1.0, 0.0, 0.0), 2: (6.0, 1.03, 120.0, -60.0)}  # angle °, scale, tx, ty µm


def _write_config(tmp_path, data, **settings):
    values = {
        "data_dir": str(data),
        "work_dir": str(tmp_path / "work"),
        "z_spacing_um": 7.0,
        "base_downsample": 1,
        "n_levels": 2,
        **settings,
    }
    path = tmp_path / "project.yaml"
    path.write_text(yaml.safe_dump(values))
    return path


def _config(tmp_path, **settings):
    """Ten placeholder slice files (read through FakeFactory) and a config for them."""
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    for name in SIZES:
        (data / name).write_text("")
    return _write_config(tmp_path, data, **settings)


def _synthetic(tmp_path, monkeypatch, **settings):
    """Two slices of one synthetic section (the second moved on its slide) as 32x slides, and
    a config for them; the stages read them through a fake reader."""
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    base = make_tissue(seed=5)
    images = {}
    for k, (a, s, tx, ty) in MOTIONS.items():
        (data / f"s_{k}_H.ndpi").write_bytes(f"slice {k}".encode())
        moved, mm, _ = apply_similarity(base.rgb, base.mask, a, s, tx, ty)
        images[k] = perturb(moved, mm, seed=k)

    def factory(path):
        return FakeSlide.from_image(images[int(path.name.split("_")[1])], 32, (2,), MPP)

    read = {"reader_factory": factory}
    monkeypatch.setattr(cli, "run_preprocess", functools.partial(run_preprocess, **read))
    monkeypatch.setattr(cli, "build_volume", functools.partial(build_volume, **read))
    return _write_config(tmp_path, data, base_downsample=32, **settings)


def _run(cfg, *args):
    cli.main(["--config", str(cfg), *args])


def _error(cfg, *args):
    """Run a command that must stop with a one-line `error:` message; returns the message."""
    with pytest.raises(SystemExit) as exc:
        _run(cfg, *args)
    # SystemExit with a message: Python prints it and exits with status 1, without a traceback
    message = exc.value.code
    assert isinstance(message, str) and message.startswith("error: ") and "\n" not in message
    return message


def _record(monkeypatch):
    """Replace the volume builder by a recorder of its arguments."""
    calls = []

    def fake(catalog, out, spec, manifest, overwrite, transforms=None, masks=None):
        ids = [s.id for s in catalog.slices]
        calls.append(
            {"ids": ids, "out": out, "spec": spec, "overwrite": overwrite, "transforms": transforms}
        )
        return out

    monkeypatch.setattr(cli, "build_volume", fake)
    return calls


def _z_spacing(volume):
    ms = zarr.open_group(str(volume), mode="r").attrs["ome"]["multiscales"][0]
    return ms["datasets"][0]["coordinateTransformations"][0]["scale"][1]


# --- arguments and configuration ------------------------------------------------------------


@pytest.mark.parametrize(("text", "ids"), [("3-7", [3, 4, 5, 6, 7]), ("5", [5])])
def test_slices_select_inclusive_range(tmp_path, monkeypatch, text, ids):
    calls = _record(monkeypatch)
    _run(_config(tmp_path), "volume", "--slices", text, "--unregistered")
    assert [c["ids"] for c in calls] == [ids]


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("abc", "not a slice range"),
        ("3-x", "not a slice range"),
        ("", "not a slice range"),
        ("5-", "not a slice range"),
        ("4-2", "first slice comes after the last"),
    ],
)
def test_malformed_slices_is_usage_error(tmp_path, monkeypatch, capsys, text, reason):
    calls = _record(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        _run(_config(tmp_path), "volume", "--slices", text, "--unregistered")
    assert exc.value.code == 2  # argparse usage error
    err = capsys.readouterr().err
    assert "error: argument --slices" in err and reason in err and "Traceback" not in err
    assert not calls


def test_volume_uses_config_and_work_dir(tmp_path, monkeypatch, capsys):
    calls = _record(monkeypatch)
    cfg = _config(tmp_path, volume_mask_margin_um=30.0)
    _run(cfg, "volume", "--slices", "1-2", "--overwrite", "--unregistered")
    (call,) = calls
    expected = VolumeSpec(z_spacing_um=7.0, base_downsample=1, n_levels=2, mask_margin_um=30.0)
    assert call["spec"] == expected
    assert call["out"] == WorkDir(tmp_path / "work").volume
    assert call["overwrite"] is True and call["transforms"] is None
    assert capsys.readouterr().out.strip() == str(call["out"])


def test_existing_volume_with_other_settings_exits_with_message(tmp_path, monkeypatch):
    builder = functools.partial(build_volume, reader_factory=FakeFactory(SIZES))
    monkeypatch.setattr(cli, "build_volume", builder)
    volume = WorkDir(tmp_path / "work").volume
    _run(_config(tmp_path, z_spacing_um=7.0), "volume", "--slices", "1-3", "--unregistered")
    cfg = _config(tmp_path, z_spacing_um=5.0)  # other settings
    assert "overwrite" in _error(cfg, "volume", "--slices", "1-3", "--unregistered")
    assert _z_spacing(volume) == 7.0  # the existing volume is kept
    _run(cfg, "volume", "--slices", "1-3", "--unregistered", "--overwrite")
    assert _z_spacing(volume) == 5.0


def test_missing_config_file(tmp_path):
    missing = tmp_path / "nope.yaml"
    assert _error(missing, "serve") == f"error: config file {missing} not found"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", "holds no settings"),
        ("data_dir: [unclosed\n", "is not valid YAML"),
        ("data_dir: /d\nwork_dir: /w\nbase_downsampel: 8\n", "base_downsampel: unknown setting"),
        ("data_dir: /d\nwork_dir: /w\n", "z_spacing_um: Field required"),
    ],
)
def test_invalid_config_is_reported(tmp_path, text, expected):
    path = tmp_path / "project.yaml"
    path.write_text(text)
    assert expected in _error(path, "serve")


def test_missing_data_dir(tmp_path):
    cfg = _write_config(tmp_path, tmp_path / "unplugged")
    assert "is the drive connected" in _error(cfg, "preprocess", "--slices", "1-2")


def test_missing_slice_ids(tmp_path):
    assert _error(_config(tmp_path), "preprocess", "--slices", "8-12").endswith("[11, 12]")


def test_programming_errors_keep_their_traceback(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise KeyError("bug")

    monkeypatch.setattr(cli, "run_preprocess", broken)
    with pytest.raises(KeyError):
        _run(_config(tmp_path), "preprocess", "--slices", "1-2")


def test_error_reaches_the_user_without_traceback(tmp_path):
    missing = tmp_path / "nope.yaml"
    r = subprocess.run(
        [str(Path(sys.executable).parent / "brainrec"), "--config", str(missing), "serve"],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 1
    assert r.stderr == f"error: config file {missing} not found\n"


# --- stages ---------------------------------------------------------------------------------


def test_stages_build_registered_volume(tmp_path, monkeypatch, capsys):
    cfg = _synthetic(tmp_path, monkeypatch, volume_mask_margin_um=30.0)
    work = WorkDir(tmp_path / "work")
    _run(cfg, "preprocess", "--slices", "1-2")
    _run(cfg, "register", "--slices", "1-2")
    _run(cfg, "volume", "--slices", "1-2")
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "preprocess (slices): 2 done, 0 up to date",
        "register (pairs): 1 done, 0 up to date; chained to slice 1",
        str(work.volume),
    ]
    attrs = zarr.open_group(str(work.volume), mode="r").attrs["brainrec"]
    assert attrs["mode"] == "registered" and attrs["reference_slice"] == 1
    assert attrs["mask_margin_um"] == 30.0
    chained = Transform2D.load(work.chained_transform(2))
    used = Transform2D.from_matrix(attrs["slice_to_reference_um"]["2"])
    assert used.allclose(chained) and not used.allclose(Transform2D.identity())

    _run(cfg, "preprocess", "--slices", "1-2")  # a second run finds everything done
    _run(cfg, "register", "--slices", "1-2")
    assert capsys.readouterr().out.splitlines() == [
        "preprocess (slices): 0 done, 2 up to date",
        "register (pairs): 0 done, 1 up to date; chained to slice 1",
    ]


def test_stage_results_of_other_settings_need_overwrite(tmp_path, monkeypatch, capsys):
    _run(_synthetic(tmp_path, monkeypatch), "preprocess", "--slices", "1-2")
    cfg = _synthetic(tmp_path, monkeypatch, mask_hysteresis=0.6)
    message = _error(cfg, "preprocess", "--slices", "1-2")
    assert "other settings" in message and "overwrite" in message
    _run(cfg, "preprocess", "--slices", "1-2", "--overwrite")
    assert capsys.readouterr().out.splitlines()[-1] == "preprocess (slices): 2 done, 0 up to date"


def test_register_before_preprocess(tmp_path):
    message = _error(_config(tmp_path), "register", "--slices", "1-2")
    assert message == "error: slices [1, 2] are not preprocessed; run preprocess first"


def test_failed_pair_is_reported_but_does_not_stop(tmp_path, monkeypatch, capsys):
    """M3-13: a failed pair ends in a warning naming it, exit status 0."""

    def register(catalog, work, cfg, manifest, overwrite):
        for k, status in ((2, "features"), (3, "failed")):
            pr = PairResult(k - 1, k, Transform2D.identity(), status, 10, 3, None, 0.4)
            write_atomic(work.pair_transform(k), json.dumps(pr.to_dict()).encode())
        return StageResult(done=[2, 3])

    monkeypatch.setattr(cli, "run_register", register)
    _run(_config(tmp_path), "register", "--slices", "1-3")
    assert capsys.readouterr().out.splitlines()[-2:] == [
        "register (pairs): 2 done, 0 up to date; chained to slice 1",
        "warning: pairs 2<-3 failed; best-effort alignment, check them in QC",
    ]


def test_qc_lists_pairs_and_flags(tmp_path, monkeypatch, capsys):
    cfg = _synthetic(tmp_path, monkeypatch, qc_min_ncc=None, qc_min_dice=0.9)
    work = WorkDir(tmp_path / "work")
    _run(cfg, "preprocess", "--slices", "1-2")
    _run(cfg, "register", "--slices", "1-2")
    capsys.readouterr()
    _run(cfg, "qc", "--slices", "1-2")
    out = capsys.readouterr().out.splitlines()
    (row,) = json.loads(work.qc_report.read_text())["pairs"]
    assert out[0].split() == ["pair", "status", "inliers", "rms_um", "ncc", "dice", "flag"]
    assert out[1].split() == [
        "1<-2", row["status"], str(row["n_inliers"]), f"{row['rms_um']:.1f}",
        f"{row['ncc']:.3f}", f"{row['dice']:.3f}",
    ]  # fmt: skip
    assert out[2:] == [
        "thresholds: qc_min_ncc not checked, qc_min_dice 0.9",
        f"qc (pairs): 1 done, 0 up to date; report {work.qc_report}",
        "flagged: none",
    ]
    assert work.qc_overlay(2).exists()

    # stricter thresholds: the pair is flagged without being scored again
    strict = _write_config(
        tmp_path, tmp_path / "data", base_downsample=32, qc_min_ncc=0.999, qc_min_dice=0.9
    )
    _run(strict, "qc", "--slices", "1-2")
    out = capsys.readouterr().out.splitlines()
    assert out[1].endswith("  FLAGGED")
    assert out[2:] == [
        "thresholds: qc_min_ncc 0.999, qc_min_dice 0.9",
        f"qc (pairs): 0 done, 1 up to date; report {work.qc_report}",
        "flagged: 1<-2",
    ]


def test_qc_before_register(tmp_path):
    message = _error(_config(tmp_path), "qc", "--slices", "1-3")
    assert message == "error: pairs 1<-2, 2<-3 are not registered; run register first"


def test_volume_before_register(tmp_path, monkeypatch):
    calls = _record(monkeypatch)
    message = _error(_config(tmp_path), "volume", "--slices", "1-3")
    assert message == (
        "error: slices [1, 2, 3] have no chained transform: "
        "run `brainrec register --slices 1-3` first, or use --unregistered"
    )
    assert not calls


def test_volume_refuses_transforms_of_separate_runs(tmp_path, monkeypatch):
    calls = _record(monkeypatch)
    work = WorkDir(tmp_path / "work")
    pair = Transform2D.similarity(0.1, 1.0, 50.0, 0.0)
    pr = PairResult(1, 2, pair, "features", 100, 80, 15.0, 0.95)
    write_atomic(work.pair_transform(2), json.dumps(pr.to_dict()).encode())
    Transform2D.identity().save(work.chained_transform(1))  # a run over slices 1-...
    Transform2D.identity().save(work.chained_transform(2))  # ... and one over 2-...
    message = _error(_config(tmp_path), "volume", "--slices", "1-2")
    assert "slices 1 and 2 were not registered in the same run" in message
    assert "register --slices 1-2" in message and not calls


def test_serve_without_volume(tmp_path):
    message = _error(_config(tmp_path), "serve")
    assert message.startswith(f"error: no volume at {WorkDir(tmp_path / 'work').volume}")


def test_serve_refuses_volume_without_ome_metadata(tmp_path):
    cfg = _config(tmp_path)
    zarr.open_group(str(WorkDir(tmp_path / "work").volume), mode="w", zarr_format=3)
    assert "no OME-Zarr 0.5 metadata" in _error(cfg, "serve")


def test_serve_starts_viewer_and_data_server(tmp_path, monkeypatch, capsys):
    builder = functools.partial(build_volume, reader_factory=FakeFactory(SIZES))
    monkeypatch.setattr(cli, "build_volume", builder)
    cfg = _config(tmp_path)
    _run(cfg, "volume", "--slices", "1-2", "--unregistered")
    started = {}

    def viewer(state, port):
        started["viewer"] = (state, port)
        return "VIEWER"

    def data_server(app, host, port):
        started["data"] = (host, port)

    monkeypatch.setattr(cli, "launch_local_viewer", viewer)
    monkeypatch.setattr(cli.uvicorn, "run", data_server)
    _run(cfg, "serve", "--port", "8100")
    state, viewer_port = started["viewer"]
    assert viewer_port == 8101 and started["data"] == ("127.0.0.1", 8100)
    assert "http://127.0.0.1:8100/volume.zarr" in json.dumps(state)
    assert "Viewer (open in Chrome): VIEWER" in capsys.readouterr().out


def _detect_setup(tmp_path, monkeypatch, **settings):
    """Two synthetic nucleus slides with their preprocess files; detect reads them through a
    fake reader with the stand-in detector and a test classifier."""
    from brainrec.detect.stage import run_detect
    from tests.synthetic.nuclei import make_nuclei
    from tests.unit.test_detect_stage import DETECTOR, MODEL, Factory, _setup

    images = {k: make_nuclei(shape=(1024, 1024), seed=k, um_per_px=0.2205, n_per_class=2,
                             touching_pairs=1, glass_px=128) for k in (1, 2)}  # fmt: skip
    _setup(tmp_path, images)  # data files, mask and metadata of preprocess
    reader = {"reader_factory": Factory(images), "detector": DETECTOR, "model": MODEL}
    monkeypatch.setattr(cli, "run_detect", functools.partial(run_detect, **reader))
    return _write_config(tmp_path, tmp_path / "data", detect_tile_px=300, **settings)


def test_detect_lists_slices_and_resumes(tmp_path, monkeypatch, capsys):
    cfg = _detect_setup(tmp_path, monkeypatch)
    _run(cfg, "detect", "--slices", "1-2", "--no-ask")
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["slice", "nuclei", "dropped", "blurred", "tiles", "seconds"]
    assert [ln.split()[0] for ln in lines[1:3]] == ["1", "2"]
    assert lines[-1].startswith("detect (slices): 2 done, 0 up to date; cell files in ")
    _run(cfg, "detect", "--slices", "1-2")
    assert capsys.readouterr().out.splitlines()[-1].startswith("detect (slices): 0 done, 2 up")
    cfg = _detect_setup(tmp_path, monkeypatch, detect_min_area_um2=12.0)
    message = _error(cfg, "detect", "--slices", "1-2")
    assert "detect_min_area_um2" in message and "overwrite" in message


def test_detect_before_preprocess(tmp_path, monkeypatch):
    cfg = _detect_setup(tmp_path, monkeypatch)
    (tmp_path / "work" / "mask" / "2.png").unlink()
    message = _error(cfg, "detect", "--slices", "1-2", "--no-ask")
    assert message == "error: slice 2 has no tissue mask: run preprocess first"


def test_detect_needs_the_classes_of_the_classifier(tmp_path, monkeypatch):
    types = [
        {"id": 1, "name": "Neuron", "colour": "#E69F00", "radius_um": [2, 15]},
        {"id": 2, "name": "Glia", "colour": "#56B4E9", "radius_um": [1.5, 8]},
    ]
    cfg = _detect_setup(tmp_path, monkeypatch, cell_types=types)
    message = _error(cfg, "detect", "--slices", "1-2")
    assert message.startswith("error: cell_types lacks Astrocyte-like, Oligodendrocyte-like")


def test_detect_asks_before_the_next_slice(tmp_path, monkeypatch, capsys):
    cfg = _detect_setup(tmp_path, monkeypatch)
    prompts = []

    def answer(text):
        prompts.append(text)
        return "n"

    monkeypatch.setattr("builtins.input", answer)
    _run(cfg, "detect", "--slices", "1-2")
    out = capsys.readouterr().out
    assert prompts == ["Start slice 2? [y/N] "]
    assert "slice 1 done: " in out and "1 slice(s) left" in out
    assert out.splitlines()[-1].endswith(
        "stopped, not started: 2; run the same command to continue"
    )
    assert "\n2 " not in out  # no row for the slice not started
    monkeypatch.setattr("builtins.input", lambda text: "y")
    _run(cfg, "detect", "--slices", "1-2")
    assert capsys.readouterr().out.splitlines()[-1].startswith("detect (slices): 1 done, 1 up")


def test_detect_without_a_terminal_stops_after_one_slice(tmp_path, monkeypatch, capsys):
    cfg = _detect_setup(tmp_path, monkeypatch)

    def no_terminal(text):
        raise EOFError

    monkeypatch.setattr("builtins.input", no_terminal)
    _run(cfg, "detect", "--slices", "1-2")
    assert "stopped, not started: 2" in capsys.readouterr().out.splitlines()[-1]


def test_detect_no_ask_never_asks(tmp_path, monkeypatch, capsys):
    cfg = _detect_setup(tmp_path, monkeypatch)

    def must_not_ask(text):
        raise AssertionError("asked although --no-ask")

    monkeypatch.setattr("builtins.input", must_not_ask)
    _run(cfg, "detect", "--slices", "1-2", "--no-ask")
    assert capsys.readouterr().out.splitlines()[-1].startswith("detect (slices): 2 done")


def test_cells_builds_layers_and_reports(tmp_path, capsys):
    from tests.unit.test_cells_stage import make_setup

    make_setup(tmp_path)
    cfg = _write_config(tmp_path, tmp_path / "data", z_spacing_um=5.0)
    _run(cfg, "cells", "--slices", "1-2")
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["class", "cells", "layer"]
    assert lines[1].startswith("Neuron") and lines[1].rstrip().endswith("layer_neuron")
    assert lines[-1].startswith("cells (slices 1-2): layers built, 80 cells")
    _run(cfg, "cells", "--slices", "1-2")
    assert "layers up to date" in capsys.readouterr().out.splitlines()[-1]


def test_cells_errors_are_one_line(tmp_path):
    from tests.unit.test_cells_stage import _volume, make_setup

    _, work, *_ = make_setup(tmp_path)
    cfg = _write_config(tmp_path, tmp_path / "data", z_spacing_um=5.0)
    _volume(work, [1])
    assert "build the volume for this range first" in _error(cfg, "cells", "--slices", "1-2")
    work.cell_meta(1).unlink()
    _volume(work, [1, 2])
    assert _error(cfg, "cells", "--slices", "1-2") == (
        "error: slices [1] have no cell file: run `brainrec detect`"
    )
