from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from brainrec.catalog import ProjectConfig

REQUIRED = {"data_dir": "/data/slides", "work_dir": "/data/work", "z_spacing_um": 5}
EXAMPLE = Path(__file__).parents[2] / "config" / "project.example.yaml"


def _load(tmp_path, values):
    path = tmp_path / "project.yaml"
    path.write_text(yaml.safe_dump(values))
    return ProjectConfig.load(path)


def test_loads_valid_yaml(tmp_path):
    cfg = _load(tmp_path, REQUIRED)
    assert cfg.data_dir == Path("/data/slides")
    assert cfg.work_dir == Path("/data/work")
    assert cfg.z_spacing_um == 5.0
    assert (cfg.base_downsample, cfg.n_levels) == (4, 5)  # defaults


def test_values_override_defaults(tmp_path):
    cfg = _load(tmp_path, {**REQUIRED, "n_levels": 3, "reg_window_um": [800, 200]})
    assert cfg.n_levels == 3
    assert cfg.reg_window_um == (800.0, 200.0)  # YAML list -> tuple


@pytest.mark.parametrize("field", sorted(REQUIRED))
def test_rejects_missing_field(tmp_path, field):
    with pytest.raises(ValidationError) as exc:
        _load(tmp_path, {k: v for k, v in REQUIRED.items() if k != field})
    assert [(e["loc"], e["type"]) for e in exc.value.errors()] == [((field,), "missing")]


def test_rejects_wrong_type(tmp_path):
    with pytest.raises(ValidationError) as exc:
        _load(tmp_path, {**REQUIRED, "z_spacing_um": "thin"})
    assert [e["loc"] for e in exc.value.errors()] == [("z_spacing_um",)]


def test_rejects_unknown_setting(tmp_path):
    with pytest.raises(ValidationError) as exc:  # a misspelling must not fall back to the default
        _load(tmp_path, {**REQUIRED, "base_downsampel": 8})
    errors = exc.value.errors()
    assert [(e["loc"], e["type"]) for e in errors] == [(("base_downsampel",), "extra_forbidden")]


@pytest.mark.parametrize("text", ["", "# only a comment\n", "- data_dir\n- work_dir\n"])
def test_rejects_file_without_settings(tmp_path, text):
    path = tmp_path / "project.yaml"
    path.write_text(text)
    with pytest.raises(TypeError, match="holds no settings"):
        ProjectConfig.load(path)


def test_example_config_loads():
    cfg = ProjectConfig.load(EXAMPLE)  # the documented starting point (README)
    assert cfg.base_downsample == 4


def test_qc_thresholds_default_and_can_be_switched_off(tmp_path):
    cfg = _load(tmp_path, REQUIRED)
    assert (cfg.qc_min_ncc, cfg.qc_min_dice) == (0.40, 0.93)  # calibrated in M4-05
    cfg = _load(tmp_path, {**REQUIRED, "qc_min_ncc": 0.35, "qc_min_dice": None})
    assert (cfg.qc_min_ncc, cfg.qc_min_dice) == (0.35, None)


def test_detection_settings_default_to_contract_c9(tmp_path):
    cfg = _load(tmp_path, REQUIRED)
    assert (cfg.detect_downsample, cfg.detect_method, cfg.detect_tile_px) == (2, "instanseg", 1024)
    assert (cfg.detect_overlap_um, cfg.detect_min_area_um2) == (40.0, 8.0)
    assert (cfg.detect_patch_min_diameter_um, cfg.detect_min_sharpness) == (8.0, 100.0)
    cfg = _load(tmp_path, {**REQUIRED, "detect_method": "stardist", "detect_tile_px": 512})
    assert (cfg.detect_method, cfg.detect_tile_px) == ("stardist", 512)


def test_cell_types_default_to_c14_and_are_validated(tmp_path):
    cfg = _load(tmp_path, REQUIRED)
    assert [(t.id, t.name) for t in cfg.cell_types][:2] == [(1, "Neuron"), (2, "Astrocyte-like")]
    assert len(cfg.cell_types) == 6 and cfg.cell_types[2].radius_um == (1.5, 6.0)
    row = {"id": 1, "name": "Neuron", "colour": "#E69F00", "radius_um": [2, 15]}
    bad = [
        [row, {**row, "id": 3, "name": "Glia"}],  # ids not 1..n
        [row, {**row, "id": 2}],  # duplicate name
        [{**row, "radius_um": [5, 2]}],  # min above max
        [{**row, "colour": "orange"}],  # not #RRGGBB
        [{**row, "shape": "round"}],  # unknown field
    ]
    for types in bad:
        with pytest.raises(ValidationError):
            _load(tmp_path, {**REQUIRED, "cell_types": types})
