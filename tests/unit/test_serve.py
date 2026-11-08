import json
import urllib.parse
import urllib.request

import pytest
from fastapi.testclient import TestClient

from brainrec.serve import (
    LAYOUTS,
    CellLayer,
    cell_shader,
    create_app,
    hosted_url,
    launch_local_viewer,
    neuroglancer_state,
)

OME = {
    "version": "0.5",
    "multiscales": [
        {
            "datasets": [
                {
                    "path": "0",
                    "coordinateTransformations": [
                        {"type": "scale", "scale": [1.0, 5.0, 0.88, 0.88]}
                    ],
                }
            ]
        }
    ],
}


def test_serves_files_with_cors(tmp_path):
    (tmp_path / "volume.zarr").mkdir()
    (tmp_path / "volume.zarr" / "zarr.json").write_text('{"zarr_format": 3}')
    r = TestClient(create_app(tmp_path)).get(
        "/volume.zarr/zarr.json", headers={"Origin": "https://example.org"}
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "*"


def test_range_request(tmp_path):
    (tmp_path / "f.bin").write_bytes(b"0123456789")
    r = TestClient(create_app(tmp_path)).get("/f.bin", headers={"Range": "bytes=2-4"})
    assert r.status_code == 206 and r.content == b"234"


def test_state_maps_channel_axis_and_scales():
    state = neuroglancer_state("http://127.0.0.1:8000/volume.zarr", OME)
    src = state["layers"][0]["source"]
    assert src["url"] == "zarr3://http://127.0.0.1:8000/volume.zarr"
    dims = src["transform"]["outputDimensions"]
    assert list(dims) == ["c^", "z", "y", "x"]  # same order as the OME axes
    assert dims["z"] == [pytest.approx(5.0e-6), "m"]
    assert dims["x"] == [pytest.approx(0.88e-6), "m"]


def test_state_defaults_are_light_and_configurable():
    state = neuroglancer_state("http://127.0.0.1:8000/volume.zarr", OME)
    assert state["layout"] == "xy"
    assert state["gpuMemoryLimit"] == 512_000_000
    assert state["concurrentDownloads"] == 8
    heavy = neuroglancer_state("http://x/volume.zarr", OME, layout="4panel", gpu_memory_mb=1024)
    assert heavy["layout"] == "4panel" and heavy["gpuMemoryLimit"] == 1_024_000_000


def test_local_viewer_page_is_served():
    viewer = launch_local_viewer(neuroglancer_state("http://127.0.0.1:1/volume.zarr", OME))
    with urllib.request.urlopen(str(viewer), timeout=10) as r:
        assert r.status == 200
        assert b"<html" in r.read().lower()


def test_hosted_url_roundtrips_state():
    state = neuroglancer_state("http://127.0.0.1:8000/volume.zarr", OME)
    url = hosted_url(state)
    assert json.loads(urllib.parse.unquote(url.split("#!")[1])) == state


def test_background_is_transparent_over_dark_panels():
    state = neuroglancer_state("http://127.0.0.1:8000/volume.zarr", OME)
    shader = state["layers"][0]["shader"]
    # transparent only where all three channels are 255 (C11), RGB colour everywhere else
    assert "r == 255u && g == 255u && b == 255u" in shader and "emitTransparent()" in shader
    assert "emitRGB(vec3(float(r), float(g), float(b)) / 255.0)" in shader
    assert state["crossSectionBackgroundColor"] == state["projectionBackgroundColor"] == "#000000"


def test_neuroglancer_parser_accepts_state():
    import neuroglancer

    state = neuroglancer_state("http://127.0.0.1:8000/volume.zarr", OME)
    parsed = neuroglancer.ViewerState(state)
    assert parsed.layers["brain"].shader == state["layers"][0]["shader"]
    assert parsed.cross_section_background_color == "#000000"
    assert list(parsed.display_dimensions) == ["x", "y", "z"]
    assert parsed.to_json()["layers"][0]["source"] == state["layers"][0]["source"]


def test_xy_panel_shows_the_section_plane():
    state = neuroglancer_state("http://127.0.0.1:8000/volume.zarr", OME)
    # axes are stored z, y, x; the first two display dimensions form the "xy" panel
    assert state["displayDimensions"] == ["x", "y", "z"]


# --- cell layers (M7-08) -------------------------------------------------------------------

CELLS = [
    CellLayer("Neuron", "http://127.0.0.1:8000/cells/layer_neuron", "#E69F00"),
    CellLayer("Vessel-wall", "http://127.0.0.1:8000/cells/layer_vessel-wall", "#D55E00"),
]


def test_without_cell_layers_the_state_is_unchanged():
    plain = neuroglancer_state("http://127.0.0.1:8000/volume.zarr", OME)
    assert neuroglancer_state("http://127.0.0.1:8000/volume.zarr", OME, cell_layers=[]) == plain
    assert [layer["name"] for layer in plain["layers"]] == ["brain"]
    assert "opacity" not in plain["layers"][0]


def test_one_annotation_layer_per_class_after_the_tissue():
    state = neuroglancer_state("http://127.0.0.1:8000/volume.zarr", OME, cell_layers=CELLS)
    layers = state["layers"]
    assert [layer["name"] for layer in layers] == ["brain", "Neuron", "Vessel-wall"]
    assert layers[1]["type"] == "annotation"
    assert layers[1]["source"] == "precomputed://http://127.0.0.1:8000/cells/layer_neuron"
    assert layers[2]["shader"] == cell_shader("#D55E00")


def test_cell_shader_controls():
    shader = cell_shader("#E69F00")
    assert '#uicontrol vec3 colour color(default="#E69F00")' in shader
    assert "#uicontrol float opacity slider(min=0, max=1" in shader
    assert "#uicontrol float min_confidence slider(min=0, max=1" in shader
    assert "#uicontrol bool hide_blurred checkbox(default=false)" in shader
    # properties of C17: confidence is float32, blurred uint8 (a uint in the shader)
    assert "prop_confidence() < min_confidence" in shader and "prop_blurred() == 1u" in shader
    assert "setColor(vec4(colour, opacity))" in shader


def test_tissue_opacity_and_3d_layout():
    state = neuroglancer_state("http://x/volume.zarr", OME, layout="xy-3d", tissue_opacity=0.0)
    assert state["layers"][0]["opacity"] == 0.0 and state["layout"] == "xy-3d"
    with pytest.raises(ValueError, match="tissue_opacity"):
        neuroglancer_state("http://x/volume.zarr", OME, tissue_opacity=1.5)
    with pytest.raises(ValueError, match="layout"):
        neuroglancer_state("http://x/volume.zarr", OME, layout="side-by-side")
    assert set(LAYOUTS) >= {"xy", "3d", "xy-3d", "4panel"}


def test_neuroglancer_parser_accepts_cell_layers():
    import neuroglancer

    state = neuroglancer_state("http://127.0.0.1:8000/volume.zarr", OME, cell_layers=CELLS,
                               layout="xy-3d", tissue_opacity=0.3)  # fmt: skip
    parsed = neuroglancer.ViewerState(state)
    neuron = parsed.layers["Neuron"].layer
    assert isinstance(neuron, neuroglancer.AnnotationLayer)
    assert neuron.shader == cell_shader("#E69F00")
    assert parsed.layers["brain"].opacity == pytest.approx(0.3)
    assert parsed.layout.type == "xy-3d"
    assert parsed.to_json()["layers"][1]["source"] == state["layers"][1]["source"]


def test_local_viewer_takes_cell_layers():
    state = neuroglancer_state("http://127.0.0.1:1/volume.zarr", OME, cell_layers=CELLS)
    viewer = launch_local_viewer(state)
    assert [layer.name for layer in viewer.state.layers] == ["brain", "Neuron", "Vessel-wall"]


def test_browsers_recheck_files(tmp_path):
    (tmp_path / "zarr.json").write_text("{}")
    client = TestClient(create_app(tmp_path))
    r = client.get("/zarr.json")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
    again = client.get("/zarr.json", headers={"If-None-Match": r.headers["etag"]})
    assert again.status_code == 304  # unchanged files are not sent again


def test_voxel_units_and_start_position():
    state = neuroglancer_state("http://x/volume.zarr", OME, position_um=(88.0, 44.0, 12.5))
    assert state["dimensions"] == {"x": [pytest.approx(0.88e-6), "m"],
                                   "y": [pytest.approx(0.88e-6), "m"],
                                   "z": [pytest.approx(5e-6), "m"]}  # fmt: skip
    assert state["position"] == [pytest.approx(100.0), pytest.approx(50.0), pytest.approx(2.5)]
    assert "position" not in neuroglancer_state("http://x/volume.zarr", OME)
