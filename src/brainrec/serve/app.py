"""Static, CORS-enabled hosting of the work directory (Zarr chunks) and Neuroglancer state."""

import json
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

HOSTED_VIEWER = "https://neuroglancer-demo.appspot.com"

# The OME channel axis "c" must be a Neuroglancer channel dimension ("c^") for getDataValue(i).
# Pure white is background (C11: fill value, never stored; tissue that is pure white is stored as
# 254), so it is drawn transparent and the dark panel shows through (decision 10, M3-12).
_SHADER = """void main() {
  uint r = toRaw(getDataValue(0));
  uint g = toRaw(getDataValue(1));
  uint b = toRaw(getDataValue(2));
  if (r == 255u && g == 255u && b == 255u) {
    emitTransparent();
  } else {
    emitRGB(vec3(float(r), float(g), float(b)) / 255.0);
  }
}
"""
BACKGROUND = "#000000"  # panel colour behind the transparent background, for contrast
LAYOUTS = ("xy", "xz", "yz", "3d", "xy-3d", "4panel")


@dataclass(frozen=True)
class CellLayer:
    """A cell layer to show (C17): its name, the URL of its precomputed annotation directory and
    its colour (C14, "#RRGGBB")."""

    name: str
    url: str
    colour: str


def cell_shader(colour: str) -> str:
    """Annotation shader of a cell layer (C17, M7-08): controls in the layer panel for the
    colour, the opacity, the minimum confidence and hiding cells of blurred tiles. Ellipsoids
    are drawn at their stored radii; Neuroglancer offers no size control for them."""
    return (
        f'#uicontrol vec3 colour color(default="{colour}")\n'
        "#uicontrol float opacity slider(min=0, max=1, step=0.05, default=1)\n"
        "#uicontrol float min_confidence slider(min=0, max=1, step=0.01, default=0)\n"
        "#uicontrol bool hide_blurred checkbox(default=false)\n"
        "void main() {\n"
        "  if (prop_confidence() < min_confidence) discard;\n"
        "  if (hide_blurred && prop_blurred() == 1u) discard;\n"
        "  setColor(vec4(colour, opacity));\n"
        "}\n"
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

    @app.middleware("http")
    async def revalidate(request: Request, call_next: Callable) -> Response:
        # files change when a stage is redone (M7-08: patched volume metadata stayed cached in
        # Chrome for hours); "no-cache" lets the browser keep files but check them each time
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-cache"
        return response

    app.mount("/", StaticFiles(directory=str(root)), name="data")
    return app


def neuroglancer_state(
    volume_url: str,
    ome: dict[str, Any],
    layout: str = "xy",
    gpu_memory_mb: int = 512,
    system_memory_mb: int = 1024,
    concurrent_downloads: int = 8,
    cell_layers: list[CellLayer] | None = None,
    tissue_opacity: float | None = None,
    position_um: tuple[float, float, float] | None = None,
) -> dict[str, Any]:
    """Viewer state showing `volume_url` (OME-Zarr 0.5, axes c, z, y, x in um) as RGB, with
    pure-white background transparent over dark panels (C11, M3-12).

    `cell_layers` adds one annotation layer per class after the tissue layer (M7-08), each with
    `cell_shader` controls. The tissue layer ("brain") fades with its own Opacity slider in the
    layer panel, down to fully transparent; `tissue_opacity` (0 to 1) sets its start value.

    The viewer works in the volume's voxel units (x, y: pixel size; z: section spacing), so one
    step of the mouse wheel is one section. Without this the cell layers (in µm) set the units
    and a step moved 1 µm, so a few steps left the 5 µm section and its cells (M7-08).
    `position_um` (volume frame, µm) is where the view starts.

    Defaults are deliberately light: Chrome's GPU process crashed with the 4-panel layout and
    Neuroglancer's default limits (M2-10), and the USB drive serves only about 27 chunks/s, so
    more parallel downloads only queue up.
    """
    if layout not in LAYOUTS:
        raise ValueError(f"layout {layout!r} is not one of {', '.join(LAYOUTS)}")
    if tissue_opacity is not None and not 0.0 <= tissue_opacity <= 1.0:
        raise ValueError("tissue_opacity must lie between 0 and 1")
    transforms = ome["multiscales"][0]["datasets"][0]["coordinateTransformations"]
    _, sz, sy, sx = next(t["scale"] for t in transforms if t["type"] == "scale")
    state: dict[str, Any] = {
        "gpuMemoryLimit": gpu_memory_mb * 1_000_000,
        "systemMemoryLimit": system_memory_mb * 1_000_000,
        "concurrentDownloads": concurrent_downloads,
        "crossSectionBackgroundColor": BACKGROUND,
        "projectionBackgroundColor": BACKGROUND,
        "dimensions": {"x": [sx * 1e-6, "m"], "y": [sy * 1e-6, "m"], "z": [sz * 1e-6, "m"]},
        # the volume's axes are z, y, x; without this the "xy" panel shows z-y, a side view
        "displayDimensions": ["x", "y", "z"],
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
    if tissue_opacity is not None:
        state["layers"][0]["opacity"] = tissue_opacity
    if position_um is not None:
        x, y, z = position_um
        state["position"] = [x / sx, y / sy, z / sz]
    for layer in cell_layers or []:
        state["layers"].append({
            "type": "annotation",
            "name": layer.name,
            "source": f"precomputed://{layer.url}",
            "shader": cell_shader(layer.colour),
        })  # fmt: skip
    return state


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
