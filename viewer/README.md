# viewer

The MVP viewer is [Neuroglancer](https://github.com/google/neuroglancer), served locally by the
`neuroglancer` Python package. There is no custom viewer code in this folder yet.

- Start it with `brainrec --config config/project.yaml serve` (see the README, "Viewer controls").
- The viewer state (layers, RGB shader with transparent background, black panels, display order
  x, y, z so the "xy" panel shows the section plane, memory limits) is built in `src/brainrec/serve/app.py`.
- Decision record: `docs/decisions/0002-local-viewer.md`.

This folder is reserved for a custom viewer, which is postponed beyond the MVP.
