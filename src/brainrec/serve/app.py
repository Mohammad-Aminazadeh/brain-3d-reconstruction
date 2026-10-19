"""Static, CORS-enabled hosting of the work directory (Zarr chunks) and Neuroglancer state."""

import json
import urllib.parse
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

HOSTED_VIEWER = "https://neuroglancer-demo.appspot.com"

# The OME channel axis "c" must be a Neuroglancer channel dimension ("c^") for getDataValue(i).
_SHADER = (
    "void main() { emitRGB(vec3(toNormalized(getDataValue(0)), "
    "toNormalized(getDataValue(1)), toNormalized(getDataValue(2)))); }"
)


def create_app(root: Path) -> FastAPI:
    app = FastAPI(title="brainrec")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET"],
        allow_headers=["*"],
        expose_headers=["Content-Range", "Accept-Ranges"],
    )
    app.mount("/", StaticFiles(directory=str(root)), name="data")
    return app


def neuroglancer_state(
    volume_url: str,
    ome: dict[str, Any],
    layout: str = "xy",
    gpu_memory_mb: int = 512,
    system_memory_mb: int = 1024,
    concurrent_downloads: int = 8,
) -> dict[str, Any]:
    """Viewer state showing `volume_url` (OME-Zarr 0.5, axes c, z, y, x in um) as RGB.

    Defaults are deliberately light: Chrome's GPU process crashed with the 4-panel layout and
    Neuroglancer's default limits (M2-10), and the USB drive serves only about 27 chunks/s, so
    more parallel downloads only queue up.
    """
    _, sz, sy, sx = ome["multiscales"][0]["datasets"][0]["coordinateTransformations"][0]["scale"]
    return {
        "gpuMemoryLimit": gpu_memory_mb * 1_000_000,
        "systemMemoryLimit": system_memory_mb * 1_000_000,
        "concurrentDownloads": concurrent_downloads,
        "layers": [
            {
                "type": "image",
                "name": "brain",
                "source": {
                    "url": f"zarr3://{volume_url}",
                    "transform": {
                        "outputDimensions": {
                            "c^": [1, ""],
                            "z": [sz * 1e-6, "m"],
                            "y": [sy * 1e-6, "m"],
                            "x": [sx * 1e-6, "m"],
                        }
                    },
                },
                "shader": _SHADER,
            }
        ],
        "layout": layout,
    }


def hosted_url(state: dict[str, Any], viewer: str = HOSTED_VIEWER) -> str:
    """Link to the public Neuroglancer build (fallback if the local viewer is unavailable)."""
    return f"{viewer}/#!" + urllib.parse.quote(json.dumps(state, separators=(",", ":")))


def launch_local_viewer(state: dict[str, Any], port: int = 0) -> Any:
    """Start Neuroglancer's own server on 127.0.0.1:`port` (0 = any free port) in a background
    thread and return the `neuroglancer.Viewer`; `str(viewer)` is its URL.

    The caller must keep the returned object alive: Neuroglancer's server holds viewers only by
    weak reference, so a dropped viewer answers 404.
    """
    import neuroglancer

    neuroglancer.set_server_bind_address("127.0.0.1", bind_port=port)
    viewer = neuroglancer.Viewer()
    viewer.set_state(neuroglancer.ViewerState(state))
    return viewer
