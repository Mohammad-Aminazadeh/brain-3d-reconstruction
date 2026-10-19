import json
import urllib.parse
import urllib.request

import pytest
from fastapi.testclient import TestClient

from brainrec.serve import create_app, hosted_url, launch_local_viewer, neuroglancer_state

OME = {"version": "0.5", "multiscales": [{"datasets": [
    {"path": "0", "coordinateTransformations": [{"type": "scale", "scale": [1.0, 5.0, 0.88, 0.88]}]}
]}]}


def test_serves_files_with_cors(tmp_path):
    (tmp_path / "volume.zarr").mkdir()
    (tmp_path / "volume.zarr" / "zarr.json").write_text('{"zarr_format": 3}')
    r = TestClient(create_app(tmp_path)).get(
        "/volume.zarr/zarr.json", headers={"Origin": "https://example.org"})
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
