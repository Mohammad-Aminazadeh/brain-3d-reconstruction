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
